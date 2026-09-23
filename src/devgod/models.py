"""Validated workflow contracts; evidence envelopes are runtime-owned.

These models validate data, not authority. Public services must never accept a
CommandResult, ReviewResult, or verified status as caller-granted evidence.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=16_384)]
ShortText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
PathText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]
Role = Literal["reviewer", "qa_engineer", "security_reviewer"]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)


class RunStatus(StrEnum):
    PLANNING = "planning"
    ACTIVE = "active"
    VERIFYING = "verifying"
    REPAIR = "repair"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    REPAIR = "repair"
    VERIFIED = "verified"
    BLOCKED = "blocked"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    BLOCKED = "blocked"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


def relative_path(value: str) -> str:
    """Validate portable repo-relative paths/scopes without resolving symlinks."""
    if not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError("path must be nonempty, bounded, and contain no control characters")
    if "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise ValueError("path must use repository-relative POSIX notation")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must stay within the repository")
    return value


class Policy(Model):
    sandbox_mode: Literal["workspace-write"] = "workspace-write"
    review_sandbox_mode: Literal["read-only"] = "read-only"
    approval_policy: Literal["never"] = "never"
    network_access: Literal[False] = False
    review_model: ShortText | None = None
    review_routes: dict[Role, ModelRoute] = Field(default_factory=dict)
    max_parallel_reviews: int = Field(default=3, strict=True, ge=1, le=3)
    max_attempts: int = Field(default=3, strict=True, ge=1, le=5)
    command_timeout_seconds: int = Field(default=600, strict=True, ge=1, le=3600)
    review_timeout_seconds: int = Field(default=900, strict=True, ge=1, le=3600)
    max_output_bytes: int = Field(default=1_048_576, strict=True, ge=1024, le=16_777_216)

    @field_validator("review_model")
    @classmethod
    def nonblank_model(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("review model must not be blank")
        return value

    def review_route(self, role: Role) -> ModelRoute:
        """Resolve a reviewer's explicit route without inheriting host settings."""
        if route := self.review_routes.get(role):
            return route
        if self.review_model is not None:
            return ModelRoute(model=self.review_model, reasoning_effort="high")
        return ModelRoute(model="gpt-5.6-sol", reasoning_effort="high")


class ModelRoute(Model):
    """Pinned model and reasoning level for a role-owned Codex invocation."""

    model: ShortText
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    @field_validator("model")
    @classmethod
    def nonblank_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("route model must not be blank")
        return value


# Compatibility name used by service and adapter implementations.
ExecutionPolicy = Policy


class CheckSpec(Model):
    name: Identifier
    argv: list[Annotated[str, StringConstraints(strict=True, max_length=8192)]] = Field(
        min_length=1, max_length=128
    )
    cwd: PathText = "."
    timeout_seconds: int = Field(default=600, strict=True, ge=1, le=3600)
    acceptance_ids: list[Identifier] = Field(default_factory=list, max_length=256)

    _relative_cwd = field_validator("cwd")(relative_path)

    @field_validator("argv")
    @classmethod
    def bounded_argv(cls, value: list[str]) -> list[str]:
        if not value[0].strip():
            raise ValueError("command executable must not be empty")
        if any("\0" in arg for arg in value) or sum(len(arg) for arg in value) > 65_536:
            raise ValueError("command argv contains NUL or exceeds 65536 characters")
        return value


class AcceptanceCriterion(Model):
    acceptance_id: Identifier
    description: Text


class TaskSpec(Model):
    task_id: Identifier
    title: ShortText
    acceptance: list[Identifier] = Field(min_length=1, max_length=256)
    allowed_paths: list[PathText] = Field(min_length=1, max_length=256)
    owner_role: Identifier = "implementer"
    depends_on: list[Identifier] = Field(default_factory=list, max_length=256)

    @field_validator("allowed_paths")
    @classmethod
    def relative_scopes(cls, value: list[str]) -> list[str]:
        return [relative_path(path) for path in value]

    @model_validator(mode="after")
    def no_self_dependency(self) -> TaskSpec:
        if self.task_id in self.depends_on:
            raise ValueError("a task cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("task dependencies must be unique")
        return self


class RunSpec(Model):
    run_id: Identifier = Field(default_factory=lambda: f"run_{uuid4().hex}")
    repo_id: Identifier
    repo_root: PathText
    goal: Text
    acceptance: list[AcceptanceCriterion] = Field(min_length=1, max_length=256)
    tasks: list[TaskSpec] = Field(default_factory=list, max_length=4096)
    checks: list[CheckSpec] = Field(default_factory=list, max_length=256)
    decisions: dict[ShortText, Text] = Field(default_factory=dict, max_length=256)
    policy: Policy = Field(default_factory=Policy)
    branch: Annotated[str, StringConstraints(strict=True, max_length=256)] = ""
    base_revision: Annotated[str, StringConstraints(strict=True, max_length=128)] = ""
    worktree_id: Annotated[str, StringConstraints(strict=True, max_length=96)] = ""

    @model_validator(mode="after")
    def unique_specs(self) -> RunSpec:
        acceptance_ids = {item.acceptance_id for item in self.acceptance}
        if len(acceptance_ids) != len(self.acceptance):
            raise ValueError("acceptance IDs must be unique within a run")
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("task IDs must be unique within a run")
        if len({check.name for check in self.checks}) != len(self.checks):
            raise ValueError("check names must be unique within a run")
        if any(set(task.acceptance) - acceptance_ids for task in self.tasks):
            raise ValueError("task acceptance must reference declared acceptance IDs")
        if any(set(check.acceptance_ids) - acceptance_ids for check in self.checks):
            raise ValueError("check acceptance must reference declared acceptance IDs")
        return self


class Candidate(Model):
    repo_id: Identifier
    repo_root: PathText
    branch: ShortText
    base_revision: Annotated[str, StringConstraints(strict=True, max_length=128)]
    head_revision: Annotated[str, StringConstraints(strict=True, max_length=128)]
    candidate_digest: Digest
    checks_digest: Digest
    snapshot_path: PathText | None = None
    worktree_id: Annotated[str, StringConstraints(strict=True, max_length=96)] = ""


class JobLease(Model):
    job_id: Identifier
    attempt: int = Field(strict=True, ge=1)
    lease_token: ShortText
    lease_expires_at: ShortText


class NextAction(Model):
    action: Identifier
    run_id: Identifier
    reason: Text
    task_id: Identifier | None = None
    inputs: dict[str, JsonValue] = Field(default_factory=dict)


class CommandResult(Model):
    invocation_id: Identifier
    exit_code: int | None = Field(strict=True)
    argv: list[str] = Field(default_factory=list, max_length=128)
    cwd: str = ""
    stdout: str = Field(default="", max_length=16_777_216)
    stderr: str = Field(default="", max_length=16_777_216)
    error: Text | None = None
    timed_out: StrictBool = False
    interrupted: StrictBool = False
    truncated: StrictBool = False
    duration_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    started_at: ShortText | None = None
    finished_at: ShortText | None = None
    stdout_path: PathText | None = None
    stderr_path: PathText | None = None

    @property
    def succeeded(self) -> bool:
        return (
            self.exit_code == 0
            and self.error is None
            and not self.timed_out
            and not self.interrupted
        )


class Finding(Model):
    severity: Severity
    summary: Text
    path: PathText | None = None
    line: int | None = Field(default=None, strict=True, ge=1)
    recommendation: str = Field(default="", max_length=16_384)
    evidence_refs: list[PathText] = Field(default_factory=list, max_length=256)

    @field_validator("path")
    @classmethod
    def relative_finding(cls, value: str | None) -> str | None:
        return relative_path(value) if value is not None else value


class ReviewPayload(Model):
    decision: ReviewDecision
    summary: Text
    findings: list[Finding] = Field(default_factory=list, max_length=256)
    acceptance_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    evidence_refs: list[PathText] = Field(default_factory=list, max_length=256)


class ReviewResult(Model):
    invocation_id: Identifier
    role: Role
    candidate_digest: Digest
    checks_digest: Digest
    payload: ReviewPayload | None = None
    thread_id: ShortText | None = None
    turn_id: ShortText | None = None
    error: Text | None = None
    duration_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)

    @property
    def succeeded(self) -> bool:
        return self.payload is not None and self.error is None


class GateResult(Model):
    verified: StrictBool
    candidate_digest: Digest
    checks_digest: Digest
    unmet_requirements: list[Text] = Field(default_factory=list, max_length=4096)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=4096)

    @model_validator(mode="after")
    def consistent_result(self) -> GateResult:
        if self.verified and self.unmet_requirements:
            raise ValueError("verified gate cannot have unmet requirements")
        return self


class VerificationResult(Model):
    job_id: Identifier
    run_id: Identifier
    candidate: Candidate
    checks: list[CommandResult] = Field(default_factory=list, max_length=256)
    reviews: list[ReviewResult] = Field(default_factory=list, max_length=32)
    gate: GateResult | None = None
    error: Text | None = None


class ExecutionAdapter(Protocol):
    async def run_command(
        self,
        spec: CheckSpec,
        candidate: Candidate,
        policy: Policy,
        on_event: EventCallback | None = None,
        *,
        invocation_id: str | None = None,
    ) -> CommandResult: ...

    async def run_review(
        self,
        role: Role,
        candidate: Candidate,
        packet: dict[str, Any],
        policy: Policy,
        on_event: EventCallback | None = None,
        *,
        invocation_id: str | None = None,
    ) -> ReviewResult: ...

    async def cancel(self, invocation_id: str) -> None: ...

    def termination_confirmed(self, invocation_id: str) -> bool:
        """True only after the owned supervisor proves all descendants stopped."""
        ...

    async def recover_termination(self, identity: dict[str, Any]) -> bool:
        """Reconcile controller-persisted ownership; model claims are not proof."""
        ...

    async def capabilities(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...
