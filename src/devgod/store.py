"""Transactional workflow records. This module is the only owner of SQL.

Private evidence methods are used by the execution controller, never exposed as
MCP tools. Leases fence both receipts and completion, including late results.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StoreError(ValueError):
    """A rejected state change with a recoverable explanation."""


def _json(value: Any, *, max_bytes: int | None = 2_000_000) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if max_bytes is not None and len(text.encode()) > max_bytes:
        raise StoreError("Workflow record exceeds the 2 MB limit; summarize its context.")
    return text


def _dict(value: Any) -> dict:
    return json.loads(_json(value))


def _token(lease: Any, key: str) -> Any:
    return lease[key] if isinstance(lease, Mapping) else getattr(lease, key)


def _process_identity(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _process_alive(pid: int, identity: str | None) -> bool | None:
    if identity is not None and (observed := _process_identity(pid)) is not None and observed != identity:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


class Store:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).expanduser().absolute()
        if self.state_dir.is_symlink():
            raise StoreError("Private state directory must not be a symlink.")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.path = self.state_dir / "state.sqlite3"
        if self.path.is_symlink():
            raise StoreError("Private state database must not be a symlink.")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self.path.chmod(0o600)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise StoreError(f"Unsupported state schema {version}; use a compatible DevGod release.")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, worktree_id TEXT NOT NULL, state TEXT NOT NULL,
                spec TEXT NOT NULL, baseline TEXT, checkpoint TEXT, gate TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_worktree ON runs(worktree_id)
                WHERE state NOT IN ('verified','cancelled');
            CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT NOT NULL REFERENCES runs(run_id), task_id TEXT NOT NULL,
                state TEXT NOT NULL, spec TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(run_id,task_id)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                kind TEXT NOT NULL, role TEXT, state TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE, candidate TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT, error TEXT,
                attempt INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_expires_at REAL,
                owner TEXT, process_id INTEGER, process_identity TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id,state);
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                job_id TEXT NOT NULL REFERENCES jobs(job_id), attempt INTEGER NOT NULL,
                kind TEXT NOT NULL, invocation_id TEXT NOT NULL,
                candidate_digest TEXT NOT NULL, checks_digest TEXT NOT NULL,
                payload TEXT NOT NULL, created_at REAL NOT NULL,
                UNIQUE(job_id,attempt,kind,invocation_id)
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                payload TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id), job_id TEXT,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
                event_key TEXT UNIQUE
            );
            PRAGMA user_version=1;
        """)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None, label: str = "record") -> dict:
        if row is None:
            raise StoreError(f"Unknown {label}; restore the current run with status or resume.")
        value = dict(row)
        for key in ("spec", "baseline", "checkpoint", "gate", "candidate", "payload", "result"):
            if key in value and value[key] is not None:
                value[key] = json.loads(value[key])
        if isinstance(value.get("error"), str) and value["error"].startswith("{"):
            try:
                value["error"] = json.loads(value["error"])
            except ValueError:
                pass
        return value

    @staticmethod
    def _event(db: sqlite3.Connection, run_id: str, kind: str, payload: Any,
               job_id: str | None = None, event_key: str | None = None) -> None:
        db.execute("INSERT OR IGNORE INTO events(run_id,job_id,kind,payload,created_at,event_key) VALUES(?,?,?,?,?,?)",
                   (run_id, job_id, kind, _json(payload), time.time(), event_key))

    def append_event(self, run_id: str, kind: str, payload: dict, *,
                     job_id: str | None = None, event_key: str | None = None) -> None:
        with self._transaction() as db:
            self._event(db, run_id, kind, payload, job_id, event_key)

    def events(self, run_id: str, *, after: int = 0, limit: int = 100) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._db.execute(
                "SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (run_id, after, min(max(limit, 1), 1000)))]

    def job_events(self, job_id: str, kind: str | None = None) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._db.execute(
                "SELECT * FROM events WHERE job_id=? AND (? IS NULL OR kind=?) ORDER BY sequence",
                (job_id, kind, kind))]

    def create_run(self, spec: Any, tasks: Sequence[Any] = (), *, run_id: str | None = None,
                   baseline: dict | None = None,
                   _prepare: Callable[[], tuple[Any, dict]] | None = None) -> dict:
        data = _dict(spec)
        worktree_id = data.get("worktree_id")
        if not worktree_id:
            raise StoreError("A run requires its canonical worktree identity.")
        run_id = run_id or data.get("run_id") or secrets.token_hex(12)
        now = time.time()
        with self._transaction() as db:
            active = db.execute("SELECT run_id FROM runs WHERE worktree_id=? AND state NOT IN ('verified','cancelled')",
                                (worktree_id,)).fetchone()
            if active:
                raise StoreError(f"Worktree already has active run {active['run_id']}; resume it automatically.")
            if _prepare is not None:
                prepared, baseline = _prepare()
                data = _dict(prepared)
                if data["worktree_id"] != worktree_id or data["run_id"] != run_id:
                    raise StoreError("Prepared delivery must preserve its claimed run and worktree identity.")
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,NULL,NULL,?,?)",
                       (run_id, worktree_id, "active" if tasks else "planning", _json(data), _json(baseline or {}, max_bytes=None), now, now))
            for task in tasks:
                item = _dict(task)
                db.execute("INSERT INTO tasks(run_id,task_id,state,spec) VALUES(?,?,?,?)",
                           (run_id, item["task_id"], "planned", _json(item)))
            self._event(db, run_id, "run.created", {"task_count": len(tasks)})
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), "run")

    def list_runs(self, worktree_id: str | None = None) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM runs WHERE (? IS NULL OR worktree_id=?) ORDER BY created_at DESC",
                                    (worktree_id, worktree_id))
            return [self._row(r) for r in rows]

    def list_tasks(self, run_id: str) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY rowid", (run_id,))]

    def add_tasks(self, run_id: str, tasks: Sequence[Any]) -> None:
        with self._transaction() as db:
            run = self._row(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), "run")
            if run["state"] in ("verified", "cancelled", "verifying"):
                raise StoreError("Tasks cannot be changed while verifying or after completion; resume into repair first.")
            for item in map(_dict, tasks):
                db.execute("INSERT INTO tasks(run_id,task_id,state,spec) VALUES(?,?,?,?)",
                           (run_id, item["task_id"], "planned", _json(item)))
            db.execute("UPDATE runs SET state='active',updated_at=? WHERE run_id=?", (time.time(), run_id))
            self._event(db, run_id, "tasks.added", {"task_count": len(tasks)})

    def replace_plan(self, run_id: str, tasks: Sequence[Any], checks: Sequence[Any], *,
                      expected_updated_at: float) -> dict:
        """Atomically amend a validated plan and invalidate affected claims."""
        with self._transaction() as db:
            run = self._row(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), "run")
            if run["updated_at"] != expected_updated_at:
                raise StoreError("Plan changed concurrently; reload and reapply the amendment automatically.")
            if run["state"] == "cancelled":
                raise StoreError("Cancelled runs cannot change their plan.")
            if db.execute("SELECT 1 FROM jobs WHERE run_id=? AND state IN ('queued','running')", (run_id,)).fetchone():
                raise StoreError("Wait for active verification before amending its plan.")
            existing = {r["task_id"]: self._row(r) for r in db.execute("SELECT * FROM tasks WHERE run_id=?", (run_id,))}
            incoming = {item["task_id"]: item for item in map(_dict, tasks)}
            if set(existing) - set(incoming):
                raise StoreError("Plan amendments must preserve accepted tasks.")
            changed = {key for key, item in incoming.items() if key not in existing or item != existing[key]["spec"]}
            affected = set(changed)
            while True:
                dependents = {key for key, item in incoming.items() if set(item.get("depends_on", [])) & affected}
                if dependents <= affected:
                    break
                affected |= dependents
            if any(existing[key]["state"] == "implementing" for key in affected & existing.keys()):
                raise StoreError("Checkpoint the active implementation before changing its task or dependency scope.")
            for task_id, item in incoming.items():
                if task_id not in existing:
                    db.execute("INSERT INTO tasks(run_id,task_id,state,spec) VALUES(?,?,?,?)", (run_id, task_id, "planned", _json(item)))
                elif task_id in affected:
                    state = "planned" if existing[task_id]["state"] == "planned" else "repair"
                    db.execute("UPDATE tasks SET state=?,spec=?,summary='' WHERE run_id=? AND task_id=?", (state, _json(item), run_id, task_id))
            spec = run["spec"] | {"tasks": list(incoming.values()), "checks": [_dict(item) for item in checks]}
            if spec != run["spec"]:
                db.execute("UPDATE runs SET spec=?,state='active',gate=NULL,updated_at=? WHERE run_id=?", (_json(spec), time.time(), run_id))
                self._event(db, run_id, "plan.amended", {"affected_tasks": sorted(affected), "check_names": [item["name"] for item in spec["checks"]]})
        return self.get_run(run_id)

    def update_task(self, run_id: str, task_id: str, state: str, *, summary: str = "",
                    conflict: Callable[[dict, dict], bool] | None = None) -> dict:
        transitions = {
            "planned": {"implementing", "blocked"},
            "implementing": {"verifying", "repair", "blocked"},
            "verifying": {"implementing", "repair", "blocked"},
            "repair": {"implementing", "blocked"},
            "blocked": {"planned", "implementing", "repair"},
            "verified": {"repair", "implementing"},
        }
        with self._transaction() as db:
            run = self._row(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), "run")
            if run["state"] == "cancelled":
                raise StoreError("Cancelled run cannot accept implementation updates.")
            tasks = [self._row(r) for r in db.execute("SELECT * FROM tasks WHERE run_id=?", (run_id,))]
            task = next((t for t in tasks if t["task_id"] == task_id), None)
            if task is None:
                raise StoreError(f"Unknown task {task_id}.")
            if state == "verified" or (state != task["state"] and state not in transitions[task["state"]]):
                raise StoreError(f"Task transition {task['state']} -> {state} is not allowed; verification owns completion.")
            if state == "implementing":
                states = {t["task_id"]: t["state"] for t in tasks}
                unmet = [d for d in task["spec"].get("depends_on", []) if states.get(d) not in ("verifying", "verified")]
                if unmet:
                    raise StoreError(f"Complete task dependencies first: {', '.join(unmet)}.")
                if conflict:
                    collisions = [t["task_id"] for t in tasks if t["task_id"] != task_id and t["state"] == "implementing"
                                  and conflict(task["spec"], t["spec"])]
                    if collisions:
                        raise StoreError(f"Write scope is owned by active task(s) {', '.join(collisions)}; finish them before dispatch.")
            if state in {"repair", "implementing"} and task["state"] in {"verifying", "verified"}:
                affected = {task_id}
                while True:
                    dependent = {t["task_id"] for t in tasks if set(t["spec"].get("depends_on", [])) & affected}
                    if dependent <= affected:
                        break
                    affected |= dependent
                if any(t["state"] == "implementing" and t["task_id"] in affected - {task_id} for t in tasks):
                    raise StoreError("Checkpoint dependent implementation before reopening its prerequisite.")
                for dependent_id in affected - {task_id}:
                    db.execute("UPDATE tasks SET state='repair',summary='' WHERE run_id=? AND task_id=? AND state IN ('verifying','verified')", (run_id, dependent_id))
            db.execute("UPDATE tasks SET state=?,summary=? WHERE run_id=? AND task_id=?", (state, summary, run_id, task_id))
            run_state = "repair" if state == "repair" or run["state"] == "verified" else "active"
            db.execute("UPDATE runs SET state=?,gate=NULL,updated_at=? WHERE run_id=?", (run_state, time.time(), run_id))
            self._event(db, run_id, "task.updated", {"task_id": task_id, "state": state, "summary": summary})
        return next(t for t in self.list_tasks(run_id) if t["task_id"] == task_id)

    def set_run_state(self, run_id: str, state: str, *, reason: str = "") -> dict:
        if state not in {"planning", "active", "verifying", "repair", "blocked", "paused"}:
            raise StoreError("Run state is controlled by cancellation and evidence-backed completion.")
        with self._transaction() as db:
            run = self._row(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), "run")
            if run["state"] == "cancelled":
                raise StoreError("Cancelled runs cannot be resumed.")
            db.execute("UPDATE runs SET state=?,gate=NULL,updated_at=? WHERE run_id=?", (state, time.time(), run_id))
            self._event(db, run_id, "run.state", {"state": state, "reason": reason})
        return self.get_run(run_id)

    def save_checkpoint(self, run_id: str, payload: dict) -> dict:
        checkpoint_id = secrets.token_hex(12)
        data = {"checkpoint_id": checkpoint_id, "context": payload, "created_at": time.time()}
        with self._transaction() as db:
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?)", (checkpoint_id, run_id, _json(data), time.time()))
            db.execute("UPDATE runs SET checkpoint=?,updated_at=? WHERE run_id=?", (_json(data), time.time(), run_id))
            self._event(db, run_id, "checkpoint.saved", {"checkpoint_id": checkpoint_id})
        return data

    def enqueue_job(self, run_id: str, kind: str, candidate: Any, *, idempotency_key: str,
                    role: str | None = None, payload: dict | None = None) -> dict:
        now, job_id = time.time(), secrets.token_hex(12)
        candidate_data = _dict(candidate)
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                result = self._row(existing)
                identity = {key: value for key, value in candidate_data.items() if key != "snapshot_path"}
                previous = {key: value for key, value in result["candidate"].items() if key != "snapshot_path"}
                if result["run_id"] != run_id or result["kind"] != kind or previous != identity or result["role"] != role:
                    raise StoreError("Idempotency key was reused for a different operation.")
                return result
            run = self._row(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), "run")
            if run["state"] in ("cancelled", "verified"):
                raise StoreError("Cannot dispatch jobs for a completed or cancelled run.")
            db.execute("INSERT INTO jobs(job_id,run_id,kind,role,state,idempotency_key,candidate,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (job_id, run_id, kind, role, "queued", idempotency_key, _json(candidate_data), _json(payload or {}), now, now))
            self._event(db, run_id, "job.queued", {"kind": kind, "role": role}, job_id)
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone(), "job")

    def list_jobs(self, run_id: str) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._db.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY created_at", (run_id,))]

    @staticmethod
    def _owned(db: sqlite3.Connection, lease: Any, *, now: float | None = None) -> dict | None:
        row = db.execute("SELECT * FROM jobs WHERE job_id=? AND state='running' AND attempt=? AND lease_token=? AND lease_expires_at>?",
                         (_token(lease, "job_id"), _token(lease, "attempt"), _token(lease, "lease_token"), now or time.time())).fetchone()
        return Store._row(row) if row else None

    def claim_job(self, job_id: str, owner: str, *, lease_seconds: float = 60) -> Any | None:
        from devgod.models import JobLease
        if not 0 < lease_seconds <= 3600:
            raise StoreError("Job lease must be between 0 and 3600 seconds.")
        with self._transaction() as db:
            job = self._row(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone(), "job")
            if job["state"] != "queued":
                return None
            run = db.execute("SELECT state FROM runs WHERE run_id=?", (job["run_id"],)).fetchone()
            if run["state"] == "cancelled":
                return None
            token, expires = secrets.token_hex(24), time.time() + lease_seconds
            attempt = job["attempt"] + 1
            db.execute("UPDATE jobs SET state='running',attempt=?,lease_token=?,lease_expires_at=?,owner=?,updated_at=? WHERE job_id=?",
                       (attempt, token, expires, owner, time.time(), job_id))
            self._event(db, job["run_id"], "job.claimed", {"attempt": attempt, "owner": owner}, job_id)
        return JobLease(job_id=job_id, attempt=attempt, lease_token=token,
                        lease_expires_at=datetime.fromtimestamp(expires, UTC).isoformat())

    def heartbeat(self, lease: Any, *, lease_seconds: float = 60, process_id: int | None = None,
                  process_identity: str | None = None) -> Any:
        from devgod.models import JobLease
        if not 0 < lease_seconds <= 3600:
            raise StoreError("Job lease must be between 0 and 3600 seconds.")
        if process_id is not None and process_identity is None:
            process_identity = _process_identity(process_id)
        with self._transaction() as db:
            job = self._owned(db, lease)
            if not job:
                raise StoreError("Attempt lost its lease; discard its result and reconcile automatically.")
            expires = time.time() + lease_seconds
            db.execute("UPDATE jobs SET lease_expires_at=?,process_id=COALESCE(?,process_id),process_identity=COALESCE(?,process_identity),updated_at=? WHERE job_id=?",
                       (expires, process_id, process_identity, time.time(), job["job_id"]))
        return JobLease(job_id=job["job_id"], attempt=job["attempt"], lease_token=job["lease_token"],
                        lease_expires_at=datetime.fromtimestamp(expires, UTC).isoformat())

    def finish_job(self, lease: Any, state: str, *, result: dict | None = None, error: Any = None) -> bool:
        if state not in {"succeeded", "failed", "interrupted", "cancelled"}:
            raise StoreError("Only a terminal attempt result may be recorded.")
        with self._transaction() as db:
            job = self._owned(db, lease)
            if not job:
                return False
            db.execute("UPDATE jobs SET state=?,result=?,error=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?",
                       (state, _json(result or {}), _json(error) if isinstance(error, dict) else error, time.time(), job["job_id"]))
            self._event(db, job["run_id"], "job.finished", {"state": state, "attempt": job["attempt"], "error": error}, job["job_id"])
            return True

    def reconcile_jobs(self, *, now: float | None = None,
                       process_alive: Callable[[int, str | None], bool | None] | None = None) -> list[dict]:
        """Fence expired owners, retry read-only work, inspect ambiguous checks.

        A live or unidentifiable process is never killed or assumed stopped.
        Its old attempt cannot publish evidence once expired.
        """
        now = time.time() if now is None else now
        process_alive = process_alive or _process_alive
        recovered = []
        with self._transaction() as db:
            jobs = [self._row(r) for r in db.execute("SELECT * FROM jobs WHERE state='running' AND lease_expires_at<=?", (now,))]
            for job in jobs:
                alive = process_alive(job["process_id"], job["process_identity"]) if job["process_id"] else None
                safe = job["kind"] in {"review", "verification"} and alive is not True
                state = "queued" if safe and job["attempt"] < 3 else "interrupted"
                reason = "Expired read-only/coordinator attempt; resume from durable child receipts." if state == "queued" else "Expired attempt may have effects; inspect its artifacts and candidate before retrying."
                db.execute("UPDATE jobs SET state=?,error=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?",
                           (state, reason, now, job["job_id"]))
                self._event(db, job["run_id"], "job.reconciled", {"state": state, "reason": reason, "process_alive": alive}, job["job_id"])
                recovered.append({"job_id": job["job_id"], "state": state, "reason": reason})
            interrupted_parents = {
                json.loads(row["payload"]).get("parent_job_id")
                for row in db.execute("SELECT payload FROM jobs WHERE kind='check' AND state='interrupted'")
            }
            for parent_id in interrupted_parents - {None}:
                parent = db.execute("SELECT job_id,run_id FROM jobs WHERE job_id=? AND kind='verification' AND state='queued'", (parent_id,)).fetchone()
                if parent is None:
                    continue
                reason = "An interrupted child needs effect inspection before its coordinator can resume."
                db.execute("UPDATE jobs SET state='interrupted',error=?,updated_at=? WHERE job_id=?", (reason, now, parent_id))
                self._event(db, parent["run_id"], "job.reconciled", {"state": "interrupted", "reason": reason}, parent_id)
                recovered = [item for item in recovered if item["job_id"] != parent_id]
                recovered.append({"job_id": parent_id, "state": "interrupted", "reason": reason})
        return recovered

    def retry_job(self, job_id: str, *, reason: str, inspected: bool = False) -> dict:
        """Controller-only recovery; consequential jobs require an inspection."""
        return self._retry_jobs([job_id], reason=reason, inspected=inspected)[0]

    def _invalidate_job(self, job_id: str, *, reason: str) -> dict:
        """Withdraw damaged terminal evidence; retain the observed outcome."""
        with self._transaction() as db:
            job = self._row(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone(), "job")
            if job["state"] not in {"succeeded", "failed", "interrupted"}:
                raise StoreError("Only terminal evidence can be invalidated for fresh execution.")
            db.execute("UPDATE jobs SET state='interrupted',error=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?",
                       (reason, time.time(), job_id))
            db.execute("UPDATE runs SET state='repair',gate=NULL,updated_at=? WHERE run_id=? AND state!='cancelled'", (time.time(), job["run_id"]))
            self._event(db, job["run_id"], "evidence.invalidated", {"reason": reason, "previous_state": job["state"], "attempt": job["attempt"]}, job_id)
        return self.get_job(job_id)

    def _replace_job_snapshot(self, job_id: str, candidate: Any) -> dict:
        """Refresh disposable snapshots without changing execution identity."""
        data = _dict(candidate)
        with self._transaction() as db:
            job = self._row(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone(), "job")
            old = {key: value for key, value in job["candidate"].items() if key != "snapshot_path"}
            new = {key: value for key, value in data.items() if key != "snapshot_path"}
            if job["state"] != "queued" or old != new:
                raise StoreError("Only a queued job's identical candidate snapshot can be refreshed.")
            db.execute("UPDATE jobs SET candidate=?,updated_at=? WHERE job_id=?", (_json(data), time.time(), job_id))
            self._event(db, job["run_id"], "snapshot.refreshed", {"candidate_digest": data["candidate_digest"]}, job_id)
        return self.get_job(job_id)

    def _rebuild_jobs(self, job_ids: Sequence[str], *, reason: str, inspected: bool = False) -> list[dict]:
        return self._retry_jobs(job_ids, reason=reason, inspected=inspected, _rebuild=True)

    def _retry_jobs(self, job_ids: Sequence[str], *, reason: str, inspected: bool = False,
                    _rebuild: bool = False) -> list[dict]:
        """Publish a recovery cohort atomically, preventing stranded child jobs."""
        with self._transaction() as db:
            jobs = [self._row(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone(), "job") for job_id in dict.fromkeys(job_ids)]
            for job in jobs:
                permitted = {"failed", "interrupted", "succeeded"} if _rebuild else {"failed", "interrupted"}
                if job["state"] not in permitted:
                    raise StoreError("Recovery cohort changed; reload its current attempts automatically.")
                if job["attempt"] >= 3:
                    raise StoreError("Retry budget exhausted; diagnose the cause and choose a materially different repair.")
                if job["kind"] not in {"review", "verification"} and not inspected:
                    raise StoreError("Inspect the interrupted command's effects before retrying.")
                run = db.execute("SELECT state FROM runs WHERE run_id=?", (job["run_id"],)).fetchone()
                if run["state"] == "cancelled":
                    raise StoreError("Cancelled runs cannot resume execution.")
            for job in jobs:
                db.execute("UPDATE jobs SET state='queued',result=NULL,error=NULL,process_id=NULL,process_identity=NULL,updated_at=? WHERE job_id=?", (time.time(), job["job_id"]))
                self._event(db, job["run_id"], "job.retried", {"reason": reason}, job["job_id"])
            if _rebuild:
                for run_id in {job["run_id"] for job in jobs}:
                    db.execute("UPDATE runs SET state='verifying',gate=NULL,updated_at=? WHERE run_id=?", (time.time(), run_id))
        return [self.get_job(job_id) for job_id in dict.fromkeys(job_ids)]

    def _record_evidence(self, lease: Any, evidence: dict) -> dict:
        with self._transaction() as db:
            job = self._owned(db, lease)
            if not job:
                raise StoreError("Expired or superseded attempt cannot publish evidence.")
            for field in ("candidate_digest", "checks_digest"):
                if evidence.get(field) != job["candidate"].get(field):
                    raise StoreError(f"Evidence {field} does not match its dispatched candidate.")
            kind, invocation = evidence.get("kind"), evidence.get("invocation_id")
            if kind not in {"check", "review"} or not isinstance(invocation, str) or not invocation:
                raise StoreError("Evidence requires an observed check/review invocation.")
            if kind != job["kind"] or (kind == "review" and evidence.get("payload", {}).get("role") != job["role"]):
                raise StoreError("Evidence must retain its controller-assigned job kind and review role.")
            existing = db.execute("SELECT * FROM evidence WHERE job_id=? AND attempt=? AND kind=? AND invocation_id=?",
                                  (job["job_id"], job["attempt"], kind, invocation)).fetchone()
            if existing:
                item = self._row(existing)
                if item["payload"] != evidence.get("payload", {}):
                    raise StoreError("Conflicting duplicate invocation result.")
                return item
            evidence_id = secrets.token_hex(12)
            db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (evidence_id, job["run_id"], job["job_id"], job["attempt"], kind, invocation,
                        evidence["candidate_digest"], evidence["checks_digest"], _json(evidence.get("payload", {})), time.time()))
            self._event(db, job["run_id"], "evidence.observed", {"evidence_id": evidence_id, "kind": kind, "invocation_id": invocation}, job["job_id"])
            return self._row(db.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone())

    def list_evidence(self, run_id: str, candidate_digest: str | None = None,
                      checks_digest: str | None = None) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._db.execute(
                "SELECT e.* FROM evidence e JOIN jobs j ON j.job_id=e.job_id WHERE e.run_id=? AND (? IS NULL OR e.candidate_digest=?) AND (? IS NULL OR e.checks_digest=?) AND e.attempt=j.attempt AND j.state IN ('succeeded','failed') ORDER BY e.created_at",
                (run_id, candidate_digest, candidate_digest, checks_digest, checks_digest))]

    def list_reviews(self, run_id: str, candidate_digest: str | None = None,
                     checks_digest: str | None = None) -> list[dict]:
        return [e for e in self.list_evidence(run_id, candidate_digest, checks_digest) if e["kind"] == "review"]

    def _finalize_gate(self, run_id: str, gate: Any, *, expected_updated_at: float | None = None,
                       expected_spec: dict | None = None) -> dict:
        data = _dict(gate)
        with self._transaction() as db:
            run = self._row(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), "run")
            if expected_updated_at is not None and run["updated_at"] != expected_updated_at:
                raise StoreError("Run changed during gate evaluation; reload the current plan and evaluate fresh evidence.")
            if expected_spec is not None and run["spec"] != expected_spec:
                raise StoreError("Accepted plan changed during gate evaluation; execute its current checks.")
            if run["state"] == "cancelled":
                raise StoreError("Cancelled runs cannot become verified.")
            if data.get("verified"):
                tasks = list(db.execute("SELECT state FROM tasks WHERE run_id=?", (run_id,)))
                if not tasks or any(t["state"] not in {"verifying", "verified"} for t in tasks):
                    raise StoreError("Every task must have an implementation claim before completion.")
                if data.get("unmet_requirements") or not data.get("evidence_ids"):
                    raise StoreError("Incomplete evidence cannot satisfy the final gate.")
                pending = db.execute("SELECT 1 FROM jobs WHERE run_id=? AND state IN ('queued','running')", (run_id,)).fetchone()
                if pending:
                    raise StoreError("Pending jobs must finish before completion.")
                for evidence_id in data["evidence_ids"]:
                    receipt = db.execute("SELECT e.* FROM evidence e JOIN jobs j ON e.job_id=j.job_id WHERE e.evidence_id=? AND e.run_id=? AND e.attempt=j.attempt AND j.state='succeeded'", (evidence_id, run_id)).fetchone()
                    if not receipt or any(receipt[key] != data[key] for key in ("candidate_digest", "checks_digest")):
                        raise StoreError("Gate references missing or stale evidence.")
                db.execute("UPDATE tasks SET state='verified' WHERE run_id=?", (run_id,))
            state = "verified" if data.get("verified") else "repair"
            db.execute("UPDATE runs SET state=?,gate=?,updated_at=? WHERE run_id=?", (state, _json(data), time.time(), run_id))
            self._event(db, run_id, "verification.gate", data)
        return self.get_run(run_id)

    def cancel_run(self, run_id: str) -> list[dict]:
        with self._transaction() as db:
            self._row(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), "run")
            jobs = [self._row(r) for r in db.execute("SELECT * FROM jobs WHERE run_id=? AND state IN ('queued','running')", (run_id,))]
            db.execute("UPDATE jobs SET state='cancelled',lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE run_id=? AND state IN ('queued','running')", (time.time(), run_id))
            db.execute("UPDATE runs SET state='cancelled',gate=NULL,updated_at=? WHERE run_id=?", (time.time(), run_id))
            self._event(db, run_id, "run.cancelled", {"owned_jobs": [j["job_id"] for j in jobs]})
            return jobs
