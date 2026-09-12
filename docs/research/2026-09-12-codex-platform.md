# Codex platform research — 2026-09-12

Research date: 2026-09-12. Official pages were opened and their substantive sections read on this date. This report records platform evidence; the resolved product choices are in [the design](../design.md), and implementation evidence is recorded separately in [verification](../verification.md).

Implementation follow-up: a live native conversation confirmed that server approval mode `auto` still blocks the externally annotated verification tool when host approval policy is `never`. Repository setup therefore uses explicit `tools.<name>.approval_mode = "approve"` overrides for its known workflow tools, retaining their accurate annotations and execution sandbox. This is scoped to DevGod, not a global approval-policy change. [Official MCP tool-policy reference](https://learn.chatgpt.com/docs/extend/mcp).

## Findings that affect the architecture

### Codex already owns the inner agent loop

Current local clients support specialist subagents, their activity and thread inspection. Delegation can be requested by the user or repository/skill instructions. Custom roles can specify instructions and model settings; otherwise settings are inherited. This supports keeping a human's requirements conversation in the normal Codex interface while delegating bounded engineering work. Concurrent writing still needs explicit ownership and integration. [Official subagent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents).

Goal mode is available in the desktop app, interactive CLI and IDE. It retains an objective across continuing work and supports user steering. It preserves the existing permission boundary. It does not itself establish that a repository's tests passed or that independent reviewers accepted the final candidate. [Official long-running work documentation](https://learn.chatgpt.com/docs/long-running-work).

Design inference: rebuilding conversation UI, model tool execution, or compaction from scratch would duplicate capabilities. DevGod should own the engineering contract and independently recorded evidence; Codex should own reasoning and tools.

### A supported local control protocol exists

The app server exposes a versioned JSON-RPC interface, including thread creation/resume, streamed turn/item events, interruption, review, goals, and approval requests. The CLI can generate schemas matching its installed version. Stable and experimental surfaces are distinguished; unsupported capabilities must be surfaced explicitly. [Official app-server documentation](https://learn.chatgpt.com/docs/app-server).

Local read-only verification found `codex-cli 0.154.0`. Running `codex app-server generate-json-schema --out /tmp/devgod-codex-0154-schema` succeeded. The generated schema includes `ThreadStartParams`, `TurnStartParams.outputSchema`, `ThreadGoalSetParams`, and `ThreadTokenUsageUpdatedNotification` with last/total usage and nullable model context window. This proves schema availability, not successful authenticated model execution.

Design inference: an execution service can use supported events instead of parsing terminal prose or scraping private transcript formats. Protocol conformance must be tested against the installed runtime, and live model tests must be distinguished from simulated provider tests.

### Official SDK choices have changed

The TypeScript SDK supports starting and resuming local Codex jobs. The current Python SDK is documented as stable, uses app-server JSON-RPC, supports synchronous/asynchronous use, and ships a pinned CLI runtime dependency. It exposes sandbox presets. The old `codex mcp-server` integration has been removed; current integrations should use app server. [Official SDK documentation](https://learn.chatgpt.com/docs/codex-sdk).

Python 3.12.3, Node 24.18.0 and `uv` are available locally. The Python SDK is not installed in system Python. No dependency installation or authenticated execution has been attempted during this research.

Language selection should follow the final integration boundary. Python offers mature built-in SQLite, process control and a current official SDK. TypeScript offers strong wire types, natural plugin tooling, and the existing team's familiarity. Rust/Go could simplify standalone distribution but require more binding and integration work for this product. No language repairs a missing execution or verification contract on its own.

### Hooks help integration, with important limits

Hooks support session/subagent lifecycle, compaction, tool events and stop continuation. A synchronous Stop decision can request another turn. SubagentStart can inject context but cannot prevent launch. Hooks require trust in their exact definitions. Most local tools are covered; hosted and some specialized paths are not, and `write_stdin` does not receive a fresh pre-tool check. Hook failures may permit execution to continue. Background hooks cannot block or start continuation. Transcript formats are explicitly unstable. [Official hook documentation](https://learn.chatgpt.com/docs/hooks).

Design inference: hooks are appropriate for context restoration, reminders and observed workflow checks. They should not be advertised as an unbypassable sandbox, universal tool interceptor, process supervisor, or proof that all work is complete.

### Distribution need not copy an entire control tree

Plugins package skills and MCP services, with optional UI. Local marketplaces support development. A small skill is suitable while a workflow is still evolving; plugins package a shared stable capability. [Official plugin documentation](https://learn.chatgpt.com/docs/build-plugins).

Design inference: consuming projects should receive a small, inspectable integration and project settings. Avoid overwriting their existing instructions, injecting unrelated quality tools into their dependencies, or copying dozens of generic skills.

## Broader harness evidence

Anthropic's earlier long-running work separates initialization from incremental coding and preserves feature status and handoffs between sessions. It identifies premature completion and half-finished, undocumented work as recurring failures. These are directly relevant failure cases for DevGod's acceptance suite. This is evidence from a particular harness/model setup, not proof of universal superiority. [Effective harnesses for long-running agents, 2025-11-26](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents).

The March 2026 follow-up uses planner, generator and evaluator roles, structured handoffs, and methodical simplification. It argues for testing which harness components actually improve outcomes as models change. DevGod should therefore compare native Codex against the additional workflow under equivalent tasks, rather than assume that more roles or paperwork improve delivery. [Harness design for long-running application development, 2026-03-24](https://www.anthropic.com/engineering/harness-design-long-running-apps).

Agent evaluation should grade the resulting environment, not just what a transcript claims. Trials, traces and outcome checks should be recorded separately. For DevGod, a successful test is a correct change with actual check results and rejection/recovery behavior, not a fixture that pre-populates passing approval records. [Demystifying evals for AI agents, 2026-01-09](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).

## Architecture alternatives

| Option | Professional's workflow | Strong point | Constraint |
| --- | --- | --- | --- |
| Native extension | Continue talking to Codex; install a focused skill/plugin and local project integration | Familiar interaction; uses native delegation and goals | Host lifecycle and hook coverage limit enforcement and unattended recovery |
| Local execution controller | Start and inspect work through DevGod, which controls Codex sessions | Owns scheduling, recovery, reviewer dispatch and completion state | Additional interface and process lifecycle to build and operate |
| Native entry with local execution service | Codex remains the conversation; it calls a service that runs managed jobs and records evidence | Familiar entry with one durable engineering state store | Must clearly show which work is managed and avoid two competing managers |

The third option is a candidate, not a selected design. It only earns its added complexity if it fits the desired user experience and survives an end-to-end test.

## Requirements to carry into design

1. A run cannot become verified because a model emitted “done.”
2. Checks and independent reviews must identify the exact candidate they assessed; subsequent edits invalidate that result.
3. Missing, failed, interrupted and unavailable evidence are distinct states.
4. Checkpoints preserve accepted user decisions, remaining work and evidence references. Cached tokens must not be subtracted from context occupancy.
5. Context metrics must state their measurement boundary. The schema alone does not prove a precise, universal pre-tool 70% handoff guarantee.
6. Installation, upgrade and removal preserve consuming-repository content and settings.
7. Offline protocol/transition tests and live Codex delivery evidence are reported separately.
8. A supervised, local product should not acquire remote infrastructure or enterprise identity dependencies without a demonstrated user need.

## Still to establish

The user's preferred entry point and finish line, most painful original failures, whether operation is personal/local or shared, and whether the original exact 70% handoff rule is a product requirement or an implementation tactic. Authentication and permission behavior must be exercised during implementation after the architecture is settled.
