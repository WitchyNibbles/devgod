# DevGod

DevGod adds an autonomous engineering workflow to the Codex app and CLI. You describe the work and resolve design questions in your normal conversation. Codex delegates implementation; DevGod saves progress, runs checks, launches independent reviews, and returns findings for repair. The finish line is a verified local branch ready for your review.

The replacement is designed around the failures of the original harness: repeated permission requests, missing reviewer execution, and internal bookkeeping that became manual work for the user. Routine task tracking, checkpoints, review dispatch, and recovery happen through tools in the conversation.

## Architecture

Codex owns the manager conversation and native implementation specialists. A local Python MCP service owns transactional SQLite state and verification. There is no database server to operate and no separate daily interface.

The service runs configured checks through Codex's sandboxed command interface. It launches separate code-review, QA, and security sessions against a frozen candidate. Passing results identify the candidate they assessed; edits invalidate stale evidence. A worker's completion message cannot mark a run verified.

See the [design](docs/design.md), [implementation plan](docs/implementation-plan.md), and [research as of September 12, 2026](docs/research/2026-09-12-codex-platform.md). The [original-project investigation](docs/research/2026-09-12-original-project.md) records the observed legacy failures.

## Install in a consuming repository

Requirements: Linux, Python 3.12+, an existing Git repository with an initial commit, [uv](https://docs.astral.sh/uv/), and an authenticated Codex installation. Managed execution uses Linux process supervision. The package pins the Python Codex SDK and compatible runtime to 0.154.0.

From this checkout:

```sh
uv tool install --python 3.12 .
devgod --repo /absolute/path/to/your-project init
devgod --repo /absolute/path/to/your-project doctor
```

Open or reconnect that project in Codex to load the integration. Codex controls project trust and trust for each exact hook definition; review initial or changed hooks through `/hooks` in the CLI. DevGod cannot grant that trust. The manager skill and MCP workflow work independently of optional lifecycle hooks. `doctor` checks installation and dependency metadata; it does not claim a successful authenticated model invocation.

Setup preserves existing instructions and configuration. It adds a small managed `AGENTS.md` section, a manager skill, a project MCP entry, lifecycle hooks, and an ownership manifest. Automatic tool approval is scoped to the DevGod MCP entry. Global Codex permissions remain under your control.

## Use it in Codex

Start with an ordinary request:

> Use DevGod to fix invoice rounding. Clarify the design with me, then implement and verify the complete change on a local branch ready for review.

The installed instructions route substantive work through `devgod-manager`; you can also explicitly invoke `$devgod-manager`. Small questions and administrative changes stay lightweight.

The manager records acceptance criteria, task scopes and dependencies, and actual verification commands. Native Codex specialists implement the work. The service executes checks and launches independent code, QA, and security sessions against a frozen source snapshot. The manager repairs failures and requests fresh verification until the current candidate passes.

You do not write action JSON, review receipts, checkpoints, or queue transitions. Progress and findings stay in the Codex conversation. Separate SDK review sessions appear in DevGod status; they are distinct from native implementation subagents.

The finish line is an implemented, verified local branch. Starting work preserves existing staged, unstaged, and untracked changes. DevGod does not automatically commit, publish a pull request, merge, or deploy.

## Resume, migrate, and update

Say “Resume the DevGod task.” The manager restores the checkpoint and inspects interrupted work before retrying it. Closing Codex may stop execution; saved state supports recovery when it reopens.

These commands are available for diagnostics:

```sh
devgod --repo /absolute/path/to/your-project status
devgod --repo /absolute/path/to/your-project next
devgod --repo /absolute/path/to/your-project resume
```

For an old DevGod installation, use `init --migrate`. It archives and disables recognizable legacy controls while preserving user modifications and historical data. Update with `uv tool install --reinstall /path/to/devgod-recovery`, then rerun `init`. `uninstall` removes identifiable owned integration and retains delivery work and private history.

See [operation and migration details](docs/operations.md) for paths, commands, recovery, and execution boundaries.

## Development and verification

```sh
uv sync --locked
bash scripts/check.sh
uv build --no-sources
```

The blocking development gate runs Ruff, mypy, and the non-live tests. The [verification record](docs/verification.md) separates simulated failure tests from authenticated live execution. Live smoke scripts are opt-in: they require local Codex authentication and use model quota. They are excluded from ordinary CI.
