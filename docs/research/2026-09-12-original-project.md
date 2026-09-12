# Original DevGod: purpose, evidence, and design implications

Research date: 2026-09-12. Scope: read-only inspection of `/home/eimi/projects/devgod`; replacement work belongs in `devgod-recovery`. This report distinguishes observed implementation from historical claims. The old test suite and live database were not run, so it does not establish their current pass/fail status.

## What the project is for

DevGod aims to give a software professional a persistent engineering team inside consuming repositories: clarify a request, understand the codebase, design, decompose work, dispatch specialists, verify changes, obtain independent reviews, repair failures, and continue until the requested outcome is complete or genuinely blocked. Its central promise is continuity and credible completion, rather than merely producing a plausible answer.

The [June 20 product vision](/home/eimi/projects/devgod/docs/devgod_agentic_loop_SDD.md:12) describes this directly. Its [target users](/home/eimi/projects/devgod/docs/devgod_agentic_loop_SDD.md:59) include solo maintainers, legacy-codebase developers, tech leads, and reviewers. The source package owns reusable assets; consuming repositories own their live work, local policy, and configuration.

The intended experience is manager-led first contact, targeted clarification during design, bounded specialist execution, and continued delivery without repeatedly asking the user to restart the process. The existing [global setup guide](/home/eimi/projects/devgod/docs/global-setup.md) instead exposes substantial installation, registration, review-identity, queue-repair, and status machinery to each consuming repository.

## Requirements versus historical implementation choices

The [original requirements](/home/eimi/projects/devgod/docs/devgod_agentic_loop_SDD.md:76) establish autonomous continuation, explicit ownership, validated handoffs, bounded delegation, evidence-backed reviews, observable blockers, and recovery independent of conversational memory. Completion must distinguish done, partial, blocked, and failed.

The following details deserve fresh evaluation rather than automatic inheritance:

- Postgres and pgvector, instead of simpler local transactional storage.
- Extensive copied skills, templates, TOML role profiles, and managed Markdown.
- External authenticated review adapters for a single local operator.
- The number and naming of departments and micro-agent roles.
- Mandatory debate for broad categories of work.
- A hard 70% context threshold. The old SDD makes this a priority requirement, but the underlying objective is reliable continuity; whether the exact percentage matters remains a product question.

The old design explicitly excludes replacing Codex, unlimited delegation, automatic production deployment, and treating memory as authority. Those boundaries remain useful unless the user changes them.

## Concrete enforcement gaps

The [README](/home/eimi/projects/devgod/README.md) and [current-state document](/home/eimi/projects/devgod/docs/current-state.md) report successful package, installation-fixture, and specific June runtime proofs. Those are historical claims with stated scope. They do not prove an autonomous delivery loop works today in a fresh consuming repository.

Later, the [June 20 roadmap](/home/eimi/projects/devgod/docs/plans/2026-06-20-devgod-agentic-company-loop-roadmap.md:22) explicitly says enforcement remains the principal gap and some documentation overstates shipped autonomy. Inspection found supporting mechanisms:

| Observed code | Practical implication |
|---|---|
| [Review dispatch](/home/eimi/projects/devgod/src/admin.ts:10296) reads an action-file queue and blocks when usable review files are absent. | This path does not itself launch the missing independent reviewers; recorded review authority and actual reviewer execution are separate concerns. |
| [Worker launch](/home/eimi/projects/devgod/src/admin.ts:2916) starts `codex exec`, buffers output, waits for process exit, and only then parses JSONL. | This adapter cannot use those events for live progress or context intervention during the turn. |
| [Directive execution](/home/eimi/projects/devgod/src/core/service.ts:2187) returns blocked for subagent dispatch, inventory, tracing, checkpoint, and migration-replanning directives. | Some apparent autonomous actions remain instructions for another worker or operator in this path. |
| [Daemon locking](/home/eimi/projects/devgod/src/admin.ts:2820) creates an exclusive file and removes it in `finally`. | Process death can leave a stale lock; this helper does not recover it. |
| [Codex configuration](/home/eimi/projects/devgod/.codex/config.toml:4) sets `approval_policy = "never"` and `sandbox_mode = "danger-full-access"`. | The package default is broader than its stated least-privilege goal. |

The approximately 12,700-line `src/admin.ts`, many overlapping workflow representations, and documented historical template/hook drift increase maintenance risk. Size alone does not prove a bug. The substantive problem is unclear responsibility for turning a required next action into actual execution and trustworthy evidence.

## Architecture alternatives still under consideration

### Native Codex extension with a small local kernel

Keep the professional's normal Codex app, CLI, or IDE conversation. Install a focused plugin/skill integration and use supported hooks, native subagents, and Goal continuation. A local kernel owns transactional task state, recorded evidence, review requirements, and generated status views.

Current platform research reports native Goal support and hooks including subagent lifecycle, compaction, and stop events. These make this alternative materially stronger than the old overlay. The [official hook documentation](https://learn.chatgpt.com/docs/hooks), checked by the platform researcher, describes hooks as guardrails rather than a complete enforcement boundary: hosted tools and some special paths lack coverage; `write_stdin` does not receive a new pre-tool check; hook errors can fail open. `SubagentStart` can inject context but cannot prevent launch through `continue: false`. A blocking Stop creates another continuation prompt; asynchronous hook completion does not itself start a turn. Hook trust requires an approved content hash.

Advantages: familiar interaction, native approvals and session controls, and less duplicated orchestration. The kernel can attest only to executions and events it actually observes. Stop validation can reject incomplete completion; it does not by itself prove that the next required reviewer will launch. Capability tests must establish those guarantees. Transcript parsing should not become a security or lifecycle dependency because the documented format is unstable.

### Controller-owned Codex execution

A dedicated `devgod` entry point owns Codex worker/reviewer sessions through a supported SDK or app-server protocol, consumes events, and advances a durable task graph.

Advantages: explicit dispatch, leases, retries, cancellation, and review provenance. Costs: another interaction surface, protocol compatibility work, and duplicated host behavior. It is justified if user workflow accepts it and the native extension cannot enforce the required lifecycle.

Both alternatives can share a small local state/evidence kernel. Neither requires a shared service, retrieval database, dashboard, or elaborate role catalog initially. No final architecture choice has been made.

## Acceptance evidence that will distinguish a replacement

A fresh consuming repository must demonstrate actual decomposition, code change, command verification, independent review, rejection and repair, and verified completion. Interrupting the process must preserve unfinished work and allow safe resumption. Changing the candidate after review must invalidate approval. A worker-authored “passed” record must not substitute for observed verification or independent review identity.

The first proof should exercise that complete loop, including the old missing-reviewer case, before expanding the organizational model.

## Product decisions after this investigation

The user confirmed that daily interaction stays in the existing Codex app or CLI and delivery ends with an implemented, verified local branch ready for review. Their main pain was repeated permission requests and manual chores caused by DevGod's own limitations.

The final design targets one professional using local repositories, retains independent verification, and uses supported context restoration rather than an invented universal context threshold. These implementation defaults and the accepted user decisions are recorded in [the completed design](../design.md). No product questions remain pending.
