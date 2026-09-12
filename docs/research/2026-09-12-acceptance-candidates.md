# DevGod acceptance candidates and implementation decomposition

Research date: 2026-09-12. Status: **research-stage acceptance matrix, superseded by the completed design and implementation plan**. This document preserves the decomposition rationale. Final requirements and execution evidence are recorded in [the design](../design.md), [implementation plan](../implementation-plan.md), and [verification record](../verification.md).

## Resolved user decisions — 2026-09-12

1. The user stays in the existing Codex app or CLI. DevGod must preserve that entrypoint rather than require a separate conversation interface.
2. Delivery ends with an implemented, verified local branch ready for human review. This does not authorize creating PRs, publishing, merging, deploying, or changing global settings.
3. Repeated permissions and user-authored administrative work caused by DevGod's internal limits were the principal failures to eliminate.

The resulting design is a native Codex manager and subagents, supported by a local kernel that actually dispatches independent reviewers and verification through a supported Codex adapter. Kernel completion is authoritative for DevGod's verified state; native conversation output alone is not. Routine administrative work and recovery remain the manager's responsibility.

## Product interpretation

The original purpose is a persistent engineering workflow: receive a goal, clarify it, delegate scoped work, validate results independently, repair failures, preserve progress across interruptions, and finish only with evidence. The old role catalog, PostgreSQL service, dashboard, and exact context threshold are proposed mechanisms rather than requirements for the replacement. The final design retains the original SDD's reviewer, QA, and security gates for substantive delivery, while small administrative work remains lightweight.

Evidence: [original SDD](../../../devgod/docs/devgod_agentic_loop_SDD.md), especially sections 1–5, 14–16, and 20–21; [original TDD](../../../devgod/docs/devgod_agentic_loop_TDD.md), especially sections 7–11 and 18–20; [project archaeology](2026-09-12-original-project.md); [current platform research](2026-09-12-codex-platform.md). The old planner, architect, product, runtime, QA, and security profiles informed ownership and review boundaries, without adopting their model settings or output conventions.

The first implementation milestone should prove one real delivery loop, including reviewer dispatch and crash recovery. Building elaborate context or debate subsystems before that would leave the old execution failure unresolved.

## Candidate acceptance matrix

| ID | Candidate outcome | Measurable verification | Product or architecture dependency |
| --- | --- | --- | --- |
| AC-01 | Safe consuming-repo installation | Install twice without overwriting user-owned instructions or settings; removal preserves preexisting content. | Native integration and packaging details; existing app/CLI entrypoint is resolved. |
| AC-02 | Useful autonomous delivery | From the existing Codex app/CLI, a fixture with two dependent implementation tasks finishes both as a verified local branch ready for human review, without an operator supplying action JSON between phases. | Detailed approval and local-branch completion policy. |
| AC-03 | Actual delegation | Managed worker and reviewer sessions are independently launched, observed, assigned scopes, and associated with validated results. | Supported native or controller session identities. |
| AC-04 | Evidence-backed completion | Missing task, failed validation, absent required review, or unresolved blocking finding prevents authoritative verified-local-branch completion. Agent prose cannot grant it. | Detailed branch identity, acceptance, and verification policy. |
| AC-05 | Fresh evidence | Change the candidate tree or relevant verification configuration; prior affected acceptance evidence becomes stale. | Candidate identity and evidence model. |
| AC-06 | Repair loop | A deliberately failing check or reviewer finding triggers repair, affected verification, and fresh review without a user repairing workflow state. | Bounded retry and approval policy. |
| AC-07 | Resumability | Interrupt at dispatch, result ingestion, and review boundaries; restart reconciles unfinished jobs without losing accepted scope or duplicating completed effects. Ambiguous effects are reconciled explicitly. | Host lifetime, resume interface, and process ownership. |
| AC-08 | Honest operational status | Status distinguishes running, awaiting approval, blocked, exhausted, failed, cancelled, and complete; each unfinished state exposes a reason and next action. | User-facing interface; labels can change during design. |
| AC-09 | Bounded autonomy | Concurrency, delegation depth, runtime, and retry limits remain effective under malformed results and repeated failures. | Observed versus controller-owned job boundary. |
| AC-10 | Context continuity | Forced rotation starts a successor from durable decisions, task state, checkpoint, and evidence. Usage identifies measured versus unknown data and its observation boundary. | Actual telemetry and supported lifecycle controls. |
| AC-11 | Independent final review | Required reviewer, QA, and security decisions belong to independently dispatched review jobs and the current candidate snapshot. | Whether the original universal final review trio remains policy. |
| AC-12 | Real Codex integration | An authenticated smoke run uses the production adapter to complete a small consuming-repo change and its evidence/review loop. | Authentication, selected adapter, and supported runtime version. |

Passing simulated adapter tests proves deterministic transitions and fault handling; it does not prove authenticated Codex delivery. Passing workflow gates also does not mathematically establish semantic correctness: the report must identify the tests and review evidence supporting each acceptance criterion.

## Work packages and dependencies

| Package | Deliverable and exit evidence | Dependencies | Candidate ownership |
| --- | --- | --- | --- |
| WP-01 Product contract and capability spike | Preserve the resolved app/CLI entrypoint and local-branch endpoint; freeze approvals, supported environments, and acceptance IDs. Exercise native hook/Goal integration with a local kernel and supported reviewer/verification adapter. | Remaining user pain-point clarification and current platform research. | Product strategist, architect, runtime specialist. |
| WP-02 Domain kernel and persistence | Explicit run/task/job transitions, dependencies, transactional records, recovery ownership, and deterministic gates with fault tests. | WP-01 contracts. | Runtime/domain specialist. |
| WP-03 Codex execution adapter | Start/resume/interrupt sessions; stream events; validate results; report capabilities/version and normalized failures. Demonstrate real execution. | WP-01 adapter decision; interface co-designed with WP-02. | Codex integration specialist. |
| WP-04 Workspace and evidence boundary | Repository/candidate identity, writer ownership, path checks, command evidence, artifact digests, and stale-evidence invalidation. | WP-01 trust decisions and WP-02 state contracts. | Workspace/infrastructure specialist, security reviewer. |
| WP-05 Complete delivery loop | Plan → worker → verification → independent review → repair → final gates, with durable checkpoints and bounded retries. | WP-02 through WP-04. | Orchestration specialist; QA validates behavior. |
| WP-06 Consumer interface and distribution | Native app/CLI integration; project settings, status, cancel/resume, idempotent installation, removal, and migration instructions. | WP-01 integration contracts; WP-05 production behavior. | Integration/tooling specialist, technical writer. |
| WP-07 Independent release verification | Fault injection, stale/forged evidence cases, scope checks, clean-repo installation, live smoke, and documented guarantee limits. | Begins with WP-02; final gate depends on WP-05 and WP-06. | Independent reviewer, QA engineer, security reviewer. |

Write ownership should follow these component boundaries, with one writer for overlapping files. Define shared interfaces before parallel implementation; do not invent a large role hierarchy solely to mirror the old catalog.

## Candidate interfaces

- `TaskSpec`: objective, acceptance IDs, dependencies, role, scope, evidence requirements, and budget.
- `JobResult`: proposed outcome, changed paths, evidence references, findings, checkpoint, and next action. It cannot grant completion authority.
- `Evidence`: producer job, command or observation, candidate digest, timestamp, artifact digest, and exit/result.
- `ReviewDecision`: independent reviewer identity, reviewed candidate digest, findings and dispositions, and decision.
- `GateDecision`: requested transition, permission result, and explicit unmet requirements.
- `ExecutionAdapter`: supported capabilities, session lifecycle, normalized event stream, interruption, and failure/recovery outcomes.

These are language-neutral responsibility boundaries, not committed field names or schemas. Evidence provenance and independent reviewer identity need an explicit threat model: a local kernel cannot claim protection against arbitrary same-user host compromise merely because its records are structured.

## Core versus optional boundaries

Candidate core: complete delivery loop, durable state, independent final review, bounded execution, installation, actionable status, and recovery. Context continuity belongs in core; a universal exact 70% pre-tool gate does not follow automatically from that need.

Optional unless clarification establishes a need: dashboard, shared database service, semantic memory, large specialist catalog, scheduling, remote infrastructure, and a multi-round debate engine. High-risk design critique can initially use ordinary independent review jobs with recorded dissent and its disposition.

## Enforcement alternatives and limitations

The resolved user entrypoint supports the native manager plus local kernel design direction. Current Codex Goal mode and trusted lifecycle hooks provide the existing conversation experience and observed workflow checks. The local kernel owns verification/reviewer dispatch and authoritative completion while the native host owns reasoning and continuation. Detailed integration still requires a capability spike and design review; it must avoid two competing task managers.

The current platform report establishes material hook limitations: some hosted/specialized tool paths bypass hooks; `write_stdin` has no fresh pre-tool hook; SubagentStart cannot prevent launch; hook failures can permit continuation; asynchronous hooks cannot start another turn; private transcript formats are unstable. A Stop hook can request another turn, but this does not make hooks a process supervisor or comprehensive permission boundary. Native continuation is constrained by host lifetime.

A dedicated controller was considered as an architectural alternative. Requiring a separate conversation launcher would conflict with the resolved user entrypoint. A controller behind the native interface can still own managed dispatch, interruption, recovery, and final state, but cannot promise interception of every internal Codex action without a supported control surface. File-scope verification detects changes at its observation boundary; stronger write confinement needs supported sandbox enforcement. No integration should claim guarantees it has not demonstrated.

Cached input still occupies context. Cumulative or post-turn token usage does not establish exact pre-action occupancy. The original TDD's cache subtraction and every-action 70% middleware assumptions need correction. Acceptance should specify the actual observed and controlled boundary, supported measurement confidence, and behavior when telemetry is unavailable.

## Resolved design defaults

The user's answers establish native Codex interaction, a verified local branch, and autonomous execution without internal administrative chores. No additional product questions remain pending.

Reasonable proposed defaults, rather than additional blocking questions: one professional using multiple local repositories; existing Codex login; repo-scoped setup; automatic routing of substantive asks after a repository has been intentionally enabled; small tasks remain lightweight; original reviewer/QA/security final gates retained; durable resume across sessions without promising execution while the native host is closed. These defaults do not authorize global installation, automatic enrollment of unrelated repositories, production actions, or PR publication.

The final design treats context continuity as a core outcome, using explicit checkpoints and supported lifecycle events with candid telemetry limits. It makes no exact universal 70% claim or promise of implementation while Codex is closed. Final acceptance distinguishes user decisions, implementation defaults, and empirically verified capabilities.
