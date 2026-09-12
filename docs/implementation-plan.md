# DevGod replacement implementation plan

Date: 2026-09-12. Scope: implementation sequencing and shared contracts for the design in [design.md](design.md). The user has supplied the necessary product decisions; implementation proceeds without further product questions. This document is a plan, not verification evidence that the product exists.

## Delivery contract

The user keeps working in the existing Codex app or CLI. An intentionally enabled consuming repository automatically routes substantive engineering work through DevGod. Delivery ends with an implemented, verified local branch ready for human review. No PR publication, merge, deployment, or global settings change follows from that endpoint.

The user's main failures were unnecessary permission requests and manual work caused by DevGod's internal limitations. Ordinary authorized research, implementation, checks, review dispatch, repair, and resumption must proceed automatically. Internal quotas, missing action files, and administrative workflow steps must not become requests for the user to do engineering work. Existing sandbox and authorization boundaries remain effective.

The selected implementation direction is Python package `devgod`, a local SQLite kernel, supported Codex execution adapters, native manager/implementation subagents, and a small repository integration. Kernel-owned evidence decides verified completion. Codex owns the conversation and overall reasoning/continuation loop. Reviewers and deterministic checks are actually dispatched by the kernel; they are not represented by files an operator must supply.

Initial dependency contract: Python 3.12+, `openai-codex==0.154.0` with its pinned compatible CLI runtime, and `mcp==2.2.0`. The MCP SDK exposes `MCPServer`; do not implement against older `FastMCP` examples. Do not pin an agent model: preserve resolved user/repository defaults. The supported-protocol spike and public SDK signatures remain the implementation reference.

## Component ownership and package order

The root session integrates shared contracts, packaging, and cross-component changes. Each specialist owns the listed modules and their corresponding tests. A specialist proposes shared-contract changes to the integrator before changing another owner's files.

| Package | Sole write scope | Dependencies and exit evidence |
| --- | --- | --- |
| P0 Contracts and scaffold — integrator | `pyproject.toml`, package entrypoints, `models.py`, shared test fixtures, CI configuration | Adopt the contracts below and confirm the supported SDK/command adapter spike. Installable package skeleton and schema validation available before integration. |
| P1 Durable kernel — domain specialist | `store.py`, `service.py`, domain/store tests | P0. Transactional state transitions, task graph, deterministic next action, checkpoints, idempotency, claims/reconciliation, and completion evaluation pass fault tests. |
| P2 Workspace and consumer installation — workspace specialist | `workspace.py`, `install.py`, `hooks.py`, packaged integration assets, workspace/install/hook tests | P0 contracts; consult P1 state paths. Canonical repository identity, candidate fingerprints/snapshots, safe managed installation/removal, and native continuation/context guidance verified in clean and preconfigured fixtures. |
| P3 Supported Codex adapter — runtime specialist | `codex_adapter.py`, adapter tests and recorded public-protocol fixtures | P0 and capability spike. Streamed sandboxed `command/exec`, structured read-only reviewer turns, cancellation, normalized errors, and actual authenticated smoke execution. |
| P4 Verification execution — verification specialist | `verification.py`, `worker.py`, verification/worker tests | Stable P0 interfaces; consumes P1/P2/P3. Durable asynchronous job starts actual checks and all three reviewers, persists results, handles interruption, invalidates stale evidence, and produces gate inputs. |
| P5 Interface, packaging, and integration — integrator | `cli.py`, `mcp_server.py`, public API glue, README, operator/migration documentation, distribution metadata | P1–P4 contracts; integrate continuously. CLI and MCP use the same service methods, all documented commands work, plugin assets ship in a built distribution, and a clean installation completes a real local-branch workflow. |
| P6 Independent release gates — reviewer, QA, security agents | Findings and evidence artifacts; implementation edits remain with their owners | Begins during P1–P5; final after integration. Acceptance matrix, fault cases, live smoke, installation/removal, and security boundary review pass against the final candidate. |

P1, P2, and P3 can start in parallel after P0 contracts are agreed. P4 can implement against those interfaces while upstream modules mature. P5 should integrate each real capability rather than postpone integration until every module appears finished. Do not add extra ownership layers or a large role catalog.

The first integrated milestone is: create a run with two dependent tasks, complete native implementation, request asynchronous verification, observe real checks and reviewer jobs, repair a forced failure, and reach verified local-branch status. Checkpoint/context refinements follow that working spine.

## Shared Python contracts

Use typed, validated domain records with JSON serialization at public boundaries. Standard-library dataclasses/enums are sufficient; SDK wire models stay inside the adapter. The signatures below define responsibility and required data. The integrator may resolve naming details before specialist work, but fields needed for evidence binding and recovery must survive.

```python
@dataclass(frozen=True)
class CheckSpec:
    name: str
    argv: tuple[str, ...]
    cwd: str                  # repository-relative directory
    timeout_seconds: int
    acceptance_ids: tuple[str, ...]

@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    title: str
    owner_role: str
    acceptance: tuple[str, ...]
    depends_on: tuple[str, ...]
    allowed_paths: tuple[str, ...]

@dataclass(frozen=True)
class Candidate:
    repo_id: str
    branch: str
    base_revision: str
    head_revision: str
    candidate_digest: str
    checks_digest: str        # normalized checks and execution policy
    snapshot_path: str

@dataclass(frozen=True)
class JobLease:
    job_id: str
    attempt: int
    lease_token: str          # internal ownership token, never model-authored
    lease_expires_at: str

@dataclass(frozen=True)
class NextAction:
    action: str
    run_id: str
    task_id: str | None
    reason: str
    inputs: dict

@dataclass(frozen=True)
class GateResult:
    verified: bool
    candidate_digest: str
    checks_digest: str
    unmet_requirements: tuple[str, ...]
    evidence_ids: tuple[str, ...]
```

`RunSpec` additionally records the goal, acceptance criteria with stable IDs, repository identity, branch/base identity, relevant user decisions, allowed execution policy, and normalized check specs. A task's completion claim is an input to verification, not permission to set its state to verified.

Reviewer model output is a bounded `ReviewPayload`: decision (`approve`, `request_changes`, `blocked`), findings with severity and source references, acceptance IDs assessed, and evidence references. The runtime wraps it in `ReviewResult` with the assigned role, invocation ID, provider thread/turn IDs when available, candidate/checks digests, and artifact IDs. A model cannot choose those authoritative envelope fields or submit a review through the public task API. Reject unknown decisions, malformed payloads, and source references outside the assigned review snapshot.

`CommandResult` records invocation ID, normalized argv/cwd, sandbox/policy identity, start/end times, exit code or interrupted/error result, stdout/stderr artifact references, and timeout/truncation metadata. Output limits must bound memory without silently converting a truncated or incomplete execution into a passing check.

The adapter exposes asynchronous operations equivalent to:

```python
async def run_command(spec, candidate, policy, on_event) -> CommandResult: ...
async def run_review(role, candidate, packet, policy, on_event) -> ReviewResult: ...
async def cancel(invocation_id: str) -> None: ...
async def capabilities() -> dict: ...
```

Deterministic checks use supported app-server `command/exec` with explicit sandbox policy or the user's configured policy. The SDK spike must prove that path. **Do not replace it with raw host shell execution from MCP.** Starting the Codex transport or a DevGod worker process is distinct from executing arbitrary repository commands. Explicitly install a deny/escalation-reporting approval handler rather than accepting the SDK's permissive low-level default. Reviewer turns use read-only access and a structured output schema, with DevGod MCP/hooks disabled to prevent recursive jobs or private state writes. A check definition found in a repository is proposed configuration, not authority to escape the sandbox; normalize it during planning and bind it with the policy digest.

## Persistence and service boundary

Use one SQLite database per canonical repository under `XDG_STATE_HOME/devgod/repos/<repo-id>/`, with standard fallback when that variable is unset. Keep authority and evidence outside the implementation worktree. Canonical repository identity must distinguish unrelated repositories and handle linked worktrees deliberately.

`store.py` is the sole SQL owner. Minimal records are:

| Record | Required contents |
| --- | --- |
| Run | Goal/acceptance, repository and branch identity, state, policy/checks digest, timestamps, current checkpoint. |
| Task | Run ID, validated task spec, state, ownership and dependency information, implementation/evidence claims. |
| Job | Run/task/candidate, kind and role, state, idempotency key, attempts, process/session identity, claim token and lease, result/error references. |
| Evidence | Producer invocation, candidate/checks digests, kind, execution outcome, artifact path/digest, timestamps. |
| Review | Assigned independent role/invocation, candidate/checks digests, payload and finding dispositions, evidence references. |
| Checkpoint | Accepted decisions, completed/open tasks, concrete next actions, evidence references, and handoff context. |
| Event | Append-only sequence, event kind, owning run/job, timestamp, bounded structured payload. |

Related state and event updates occur in one transaction. Use schema versioning, foreign-key enforcement, bounded busy handling, and atomic job claims. Concurrent MCP clients must not duplicate the same verification request. The lease owner alone may finish an attempt; a stale worker cannot overwrite its successor's result.

States agreed with architecture:

- Run: `planning`, `active`, `verifying`, `repair`, `verified`, `blocked`, `paused`, `cancelled`.
- Task: `planned`, `implementing`, `verifying`, `repair`, `verified`, `blocked`.
- Job: `queued`, `running`, `succeeded`, `failed`, `interrupted`, `cancelled`.

`service.py` provides validated operations for initialization/diagnostics, run creation/resume, task updates, checkpoints, status, next action, verification requests, verification status, and cancellation. Public CLI/MCP calls share these operations. Only gate evaluation may set verified state. No public API accepts passing check records, arbitrary review authority, or a force-verified boolean.

Representative store operations are create/get run, upsert/list tasks, append checkpoint/event, enqueue/get/claim/heartbeat job, finish owned attempt, reconcile interrupted jobs, and query evidence/reviews for a specific candidate. Completion is evaluated by the service from those records and a newly computed candidate fingerprint, not stored as an unconditional caller assertion.

## Verification, recovery, and autonomy

`verification.start` returns a persisted job ID promptly. A claimed worker advances deterministic checks and independent reviewer, QA, and security jobs while status remains queryable. It must not depend on an MCP request remaining open. Native manager continuation remains host-owned; persistence permits resuming after the app closes without promising that the full engineering loop continues while Codex is closed.

Freeze the candidate and checks/policy identity. Preserve staged, unstaged, and untracked user changes across branch creation and failures. Include tracked files even when an ignore pattern matches them, applicable nonignored untracked source, path changes, executable bits, and relevant configuration in the fingerprint. Hashing and snapshots have explicit exclusions for private/generated content; copy symlinks as links and hash their text without dereferencing them. Source manifests before/after copying and the copied manifest must agree; reject a copy race. Review an immutable candidate snapshot; bind sandboxed check execution to the active candidate and recompute its fingerprint before accepting results. A concurrent edit or altered check plan invalidates affected results and schedules fresh verification.

Active-root sandboxed checks retain normal consuming-project dependencies and require before/after source digests. If isolated check snapshots are supported, their dependencies must truly be isolated: an editable `.venv` importing the mutable original tree is not isolated verification. Never silently symlink an external dependency tree or broaden sandbox roots to make a snapshot appear self-contained.

Git plumbing can execute repository hooks or configured helpers even with a fixed argv vector. Branch creation suppresses hooks, inherited `GIT_*` overrides are sanitized, and snapshot/diff operations avoid filters, text conversion, and external diff commands. Do not stash, reset, or clean user work to prepare the delivery branch.

Run the three required reviewers as independent invocations with their assigned remit and the actual candidate/evidence packet. A passing test plus an owner-authored review summary is insufficient. Record high/critical findings and contradictory results; repaired candidates require fresh affected checks/reviews. When all tasks and acceptance obligations are satisfied, every required check and review passes for the current candidate, and no blocking finding or unresolved job remains, the service may publish verified local-branch status and its evidence summary.

Bound individual retries, command runtime, reviewer concurrency, and malformed-output repairs. A bounded retry expiring is an execution fact, not proof that the product goal is impossible. Return a repair/diagnostic next action so the native manager can investigate and try a materially different safe route. Do not ask the user to manufacture JSON, run internal queue transitions, reduce their goal because of an arbitrary task count, or repair DevGod's own administrative records.

On restart, reconcile live/dead process identity, session availability, attempt ownership, lease expiry, and durable results. Do not equate lease expiry with proof that a command had no effect. Completed results are ingested idempotently; interrupted or ambiguous effects are inspected before retry. Unexpected worker death never creates passing evidence. Recovery must terminate obsolete managed workers when safely identifiable, preserve existing artifacts, and expose the next automatic action.

Only a real external dependency, authorization boundary, or explicit user-imposed pause/budget should require intervention. Routine authorized steps do not acquire a second DevGod permission layer. Any unavoidable approval request identifies the concrete action and actual platform boundary; prior authorization remains effective.

## Consumer surface and distribution

The CLI supports `init`, `doctor`, `status`, `next`, run start/resume/status/cancel, task add/update/list, checkpoint save/show, verification start/status, MCP serving, and uninstall. Exact subcommand spelling is an integrator decision; every documented action must be implemented. Administrative commands are diagnostics and recovery tools, not required manual steps between normal work phases.

The MCP surface stays small: run creation/resume, task updates, checkpoint, status, next action, verification start/status, and cancellation. Return structured domain errors with actionable automatic recovery guidance. Transport stdout contains protocol messages only; diagnostics go to stderr or bounded artifacts. JSON schemas must constrain IDs, enums, paths, argv, payload sizes, and required fields. Install repository-only auto approval for these narrowly scoped DevGod tools using supported MCP `default_tools_approval_mode`/per-tool `approval_mode` settings, with accurate read/write annotations. This does not change host shell sandboxing, general tool approvals, or global settings.

Ship the focused native workflow skill, optional trusted hooks, and MCP/plugin assets with the Python distribution. Repository setup adds small managed sections/assets with an installation manifest and original-content fingerprints. Preserve existing AGENTS instructions and configuration; repeat setup is idempotent; user-edited managed material is reconciled without silently overwriting it. Uninstall removes only identifiable DevGod-owned content. Global configuration changes and enrollment of other repositories require their own scope.

Native instructions make substantive routing, automatic delegation, checkpoints, progress, and evidence-backed completion clear. Hooks restore context and request continuation from kernel status where supported. Hook failure cannot falsely mark a run verified, and documentation states that hooks are guardrails rather than universal tool interception. Small administrative tasks avoid unnecessary full-run machinery.

## Acceptance and release evidence

Carry AC-01 through AC-12 from [the research acceptance matrix](research/2026-09-12-acceptance-candidates.md) into concrete tests, with this plan and the final design superseding its former pending product questions. Add the user-driven acceptance requirements:

| ID | Required behavior | Verification |
| --- | --- | --- |
| AC-13 | Ordinary authorized delivery does not repeatedly ask permission. | End-to-end in-policy fixture records zero extra DevGod approval prompts across planning, task dispatch, check/review execution, repair, checkpoint, and resume; a genuine permission boundary remains enforced. |
| AC-14 | Internal limits do not become manual user tasks. | Missing administrative state, expired worker leases, malformed review output, and exhausted same-path retries produce automatic recovery or a concrete native repair directive, never an operator action-JSON requirement. |
| AC-15 | Work remains in the existing Codex UX and reaches the agreed endpoint. | Installed repository workflow starts from native instructions/MCP, reports ongoing status, and finishes with a current verified local branch; no PR or deployment side effect occurs. |

The deterministic suite must cover missing reviewer dispatch, forged caller-supplied completion, mutation after review, check-plan edits, duplicate requests/events, process interruption, expired-attempt late results, dependency cycles, path traversal/symlink escape, snapshot copy races, tracked ignored files, Git-hook/config execution, check timeouts, malformed model output, staged/unstaged/untracked preservation, reviewer recursion/private-state write denial, and install/upgrade/uninstall in an already configured repository.

Run focused component tests during development, then the documented project quality gates, built-package installation checks, independent correctness/QA/security review, and an authenticated live Codex workflow against the final candidate. Record simulated and live evidence separately. Live proof must show actual command execution and all required reviewer invocations, plus repair/reverification where the fixture requires it. If authentication or a platform capability is unavailable, report that exact external limitation; do not relabel a simulator result as live verification.

Release requires clean evidence for the final implementation, usable consumer documentation, reproducible installation, and no unresolved critical/high security finding. A passed phase, plan, or adapter smoke alone is not finished DevGod.
