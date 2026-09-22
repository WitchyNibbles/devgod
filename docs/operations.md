# Operating DevGod

## Daily workflow

Use your existing Codex conversation. The manager performs planning, delegation, bookkeeping, checks, review dispatch, repair, and resumption. Product questions belong before design completion. Routine implementation choices and internal recovery belong to the manager afterward.

DevGod follows repository instructions, applicable skills, custom agent roles, and quality gates. Native implementation specialists receive bounded scopes. Independent code, QA, and security verification uses separate SDK sessions with recorded provider thread and turn identities.

## Model routes

Use **GPT-5.6 Terra at medium reasoning effort** for the host conversation that invokes `devgod-manager`. Codex custom-agent configuration cannot override that active root conversation, so choose it in Codex before starting substantive DevGod work. Setup deliberately does not write a root `model` or `model_reasoning_effort` into the user's configuration.

| Route | Enforced project agent | When the manager dispatches it |
| --- | --- | --- |
| Worker | `devgod-luna-worker` — Luna / medium | Clear, bounded coding tasks with explicit acceptance criteria, path ownership, and checks; focused test work; mechanical refactors; known-path repairs. |
| Lead | `devgod-terra-lead` — Terra / medium | Planning, decomposition, integration, ordinary multi-file debugging, API/schema decisions, and Luna escalations. |
| Expert | `devgod-sol-expert` — Sol / high | Evidence-backed hard blockers, material design disagreement, difficult root cause analysis, and high-risk security or data-integrity decisions. |

Escalate a Luna worker to Terra for unclear behavior, a public API/schema/persistence/security/concurrency boundary, a scope that crosses assigned components, or an unexplained failed repair. Escalate Terra to Sol only after a focused investigation package names the attempted approaches, observed evidence, affected paths, acceptance criteria, and remaining question. Do not use Luna at maximum effort as a routine alternative to Terra, and do not use Sol for normal implementation or self-approval.

The gate requires complete implementation claims, task coverage of accepted criteria, passing checks, all three independent reviews, and current source and artifact integrity. Reviews must account for the accepted criteria and cite supplied evidence. A completion message, checkpoint field, or passing test alone cannot grant verification.

## Installation ownership

Install the Python distribution in a stable environment outside the consuming repository, then run `devgod --repo PATH init`. The recommended `uv tool install` provides that separation. Generated MCP and hook commands refer to the installed executable. If that environment moves, rerun `init` from the replacement installation. Managed checks must not be able to rewrite the controller or its runtime through a repository-local virtual environment.

| Location | Purpose |
| --- | --- |
| `AGENTS.md` managed section | Route substantive work to the manager. |
| `.agents/skills/devgod-manager/` | Manager instructions and metadata; numbered on collision. |
| `.codex/agents/devgod-*.toml` | Managed project-scoped Luna worker, Terra lead, and Sol expert routes; an existing or edited role file remains active rather than being overwritten. |
| `.codex/config.toml` managed section | Local stdio MCP service and scoped automatic tool approval. |
| `.codex/hooks.json` selected entries | Restore context, observe lifecycle, and request bounded continuation. |
| `.devgod/native-install.json` | Ownership, content hashes, and runtime paths. |

User instructions and unrelated configuration remain intact. An unowned MCP name collision receives a distinct namespace. Repeating unchanged setup is idempotent. User edits to managed files are preserved; the installer reports preserved edits and backup locations.

The generated configuration enables and explicitly approves the known DevGod tool names within that repository's server entry. Codex's `auto` mode alone can still request approval for verification tools with external execution annotations; the native workflow test exposed that distinction. Other tool names are not enabled through this entry. Deliberate user edits to an existing DevGod approval policy are preserved. [Codex MCP approval configuration](https://learn.chatgpt.com/docs/extend/mcp).

Codex owns project trust and exact-definition hook trust. Installation does not change that trust store. Initial or changed hooks may require host review through `/hooks` in the CLI. The skill and MCP workflow remain useful without hooks. Hook errors cannot grant verification; hooks do not intercept every tool or guarantee execution while Codex is closed.

The distribution also includes a Codex plugin manifest and supporting assets. Repository `init` is the documented setup path; it does not install a global marketplace entry.

## State and workspace safety

The default private root is `$XDG_STATE_HOME/devgod/repos/`, falling back to `~/.local/state/devgod/repos/`. Each canonical Git repository has separate SQLite state and evidence. Linked worktrees share repository storage while runs remain bound to their originating worktree. `--state-home PATH` selects an alternate external root; repository-local state is rejected.

Private storage contains the database, job artifacts, snapshots, lifecycle counters, and installation backups. Do not edit these to force transitions. The public service accepts plans, implementation claims, checkpoints, and recovery actions; it does not accept successful check records or authoritative review receipts.

A run creates a delivery branch at the current commit without checkout, stash, reset, or clean operations. Existing staged, unstaged, and untracked contents remain in place and are included in the candidate. Task scopes describe changes made after that baseline. The final branch contains local implementation changes; it need not contain a new commit.

Fingerprints cover source contents, relevant new files, modes, symlink text, Git identity, and the accepted plan and policy. Ignored dependencies are not copied into review snapshots. Active `.codex` files become inert snapshot data with an explicit path mapping, so repository configuration cannot activate additional reviewer tools.

Checks execute in the active worktree to use existing dependencies. Source hashes before and after execution bind results to the candidate. Reviews inspect frozen copies. Source, branch, plan, snapshot, or artifact changes prevent stale approval from being reused.

This protects against ordinary worker writes and stale results. It is not a security boundary against an unrestricted process running as the same user, nor a proof that model review catches every defect.

## Execution and recovery

Checks use the pinned Codex app-server `command/exec` interface, an explicit workspace sandbox, network disabled, and a private temporary scratch directory. The MCP service does not directly execute arbitrary repository commands on the host. Review sessions use a read-only sandbox and deny approval requests; unrelated MCP servers, apps, plugins, and further delegation are disabled. Trusted host policies remain effective.

The native manager can prepare dependencies through normal Codex tools. A real host permission boundary uses the host permission flow. DevGod does not silently broaden its verification policy to make a blocked operation pass.

MCP verification returns a persistent job ID and continues in the service's event loop between tool calls. CLI `verify` waits for completion because it has no resident event loop after exit. Duplicate requests reuse an existing operation.

Interruption preserves state. Leases and attempt tokens reject late results. Lease expiry does not establish that a command had no effects; uncertain work requires manager inspection before a safe retry. Provider errors and malformed reviews receive bounded retries. Exhausted retries call for diagnosis and another safe approach, never user-authored internal records.

Each managed Codex runtime has a Linux child-subreaper supervisor. Its private receipt is written after all descendants have been stopped and reaped, including detached children. After a service crash, recovery reconciles that recorded supervisor and still requires inspection of already-completed effects before replaying a check. If the supervisor itself was forcibly destroyed without its receipt, DevGod cannot certify termination and does not invent a passing result. See the [design's process-ownership rationale](design.md).

Status exposes current review findings and bounded check-output excerpts, with links to the full controller artifacts. The manager can use those reports to repair failures without editing or querying the private database. Historical successful execution and evidence accepted by the current gate are reported separately.

Checkpoints retain explicit decisions, progress, and next actions. Hooks can restore recorded information but cannot reconstruct unrecorded decisions. Continuation bounds prevent repeated no-progress loops. Neither hooks nor storage promise autonomous implementation while Codex is closed.

## Migrating the original harness

```sh
devgod --repo /absolute/path/to/your-project init --migrate
```

Migration recognizes original managed instruction markers, exact known legacy hook commands, and the legacy Node administrative MCP configuration. It backs up and removes those controls. Unchanged legacy manifest-owned DevGod skills and agents are archived by recorded content hash, preventing the old workflow from continuing alongside the replacement.

Modified or unrecognized files remain. Historical `.devgod` data, old plans, and memory are retained as reference material, not imported as authoritative new evidence. Migration does not guess the origin of general sandbox settings or remove unrelated services. Output reports migrated files and backups.

Update with `uv tool install --reinstall /path/to/devgod-recovery`, then rerun `init`. Review changed hook definitions if Codex requests it. `devgod --repo PATH uninstall` removes owned integration while retaining user edits, the delivery branch, and private history.

## Administrative commands

Options `--repo`, `--state-home`, and `--json` precede the subcommand. Each command has `--help`. These expose the same service as MCP; users do not need to supply planning files between normal conversation steps.

| Command | Behavior |
| --- | --- |
| `init [--migrate]`, `doctor`, `uninstall` | Install, inspect local capabilities, or remove owned integration. |
| `status [RUN]`, `next [RUN]`, `resume [RUN]` | Inspect and restore the workflow. |
| `start --goal TEXT --acceptance ID:DESCRIPTION` | Record a goal and create its delivery branch. |
| `plan --tasks FILE --checks FILE` | Amend tasks or checks. |
| `task add`, `task update`, `task list` | Record scopes and implementation claims. |
| `checkpoint save`, `checkpoint show` | Save or inspect continuation context. |
| `verify [RUN]` | Run actual checks and independent reviews; wait for the gate. |
| `recover JOB --attempt N --candidate-digest HASH --checks-digest HASH --observations TEXT` | Record manager inspection and retry a stopped attempt; wait for fresh evidence. |
| `wait JOB --timeout SECONDS` | Observe a live service's job, for up to 60 seconds. |
| `cancel [RUN]` | Cancel owned jobs while preserving work and history. |
| `mcp`, `hook` | Generated native host entry points. |

Errors explain the failing operation and next recovery action. Diagnostic output never replaces MCP protocol messages on stdout. `doctor` reports installed dependency capabilities without claiming a live authenticated invocation.
