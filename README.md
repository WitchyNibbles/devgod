<!-- A little witchcraft in the README. Evidence in the workflow. -->

<div align="center">

<img src="docs/assets/devgod-logo.png" width="220" height="220" alt="DevGod emblem: an ivory candle between code brackets, beneath a golden crescent moon on a dark plum seal" />

<h1>DevGod</h1>

<p><em>Summon the agents. Keep the receipts.</em></p>

<p>Autonomous engineering for your existing Codex app and CLI.</p>

[![Verify](https://github.com/WitchyNibbles/devgod/actions/workflows/ci.yml/badge.svg)](https://github.com/WitchyNibbles/devgod/actions/workflows/ci.yml) [![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-b69a62?style=flat-square&labelColor=17121f)](pyproject.toml) [![Linux](https://img.shields.io/badge/platform-Linux-9b88b0?style=flat-square&labelColor=17121f)](docs/operations.md) [![MIT license](https://img.shields.io/badge/license-MIT-b74353?style=flat-square&labelColor=17121f)](LICENSE)

[The grimoire](docs/design.md) · [Field notes](docs/operations.md) · [Verification record](docs/verification.md)

</div>

---

## 🖤 What it does

DevGod gives Codex a persistent engineering workflow inside the conversation you already use. Describe the work, settle the design, and let the manager delegate implementation, run checks, dispatch independent reviews, and repair failures.

**The finish line: an implemented, verified local branch, ready for your review.**

The original harness had a habit of turning its own limitations into chores: repeated permission requests, missing reviewer execution, and manual bookkeeping. This rebuild puts routine task tracking, checkpoints, review dispatch, and recovery behind tools the agents operate themselves.

## 🌙 The ritual

| Step | What happens |
| --- | --- |
| **Set the intention** | You and Codex agree on the design, acceptance criteria, and scope. |
| **Summon the specialists** | Native Codex agents implement the work across planned tasks. |
| **Test the spell** | DevGod runs configured checks and separate code, QA, and security reviews. Findings return to the manager for repair. |
| **Bring it into the light** | Fresh evidence supports the current candidate; the local branch is ready for you to review. |

A worker saying “done” cannot mark a run verified. Checks and reviews identify the source snapshot they assessed, and later edits invalidate stale evidence.

## 🕯️ Installation

Bring these to the circle:

- **Linux** and **Python 3.12+**. Managed execution uses Linux process supervision.
- [**uv**](https://docs.astral.sh/uv/) and an **authenticated Codex installation**.
- A consuming **Git repository with an initial commit**.

Clone DevGod and install it in a separate tool environment:

```sh
git clone https://github.com/WitchyNibbles/devgod.git
cd devgod
uv tool install --python 3.12 .
```

Connect your project, replacing the path below with its absolute path:

```sh
devgod --repo /absolute/path/to/your-project init
devgod --repo /absolute/path/to/your-project doctor
```

Open or reconnect that project in Codex to load the integration. Codex controls project trust and trust for each exact hook definition; review initial or changed hooks through `/hooks` in the CLI. DevGod cannot grant that trust. The manager skill and MCP workflow work independently of optional lifecycle hooks.

Setup preserves existing instructions and configuration. It adds a small managed `AGENTS.md` section, a manager skill, a project MCP entry, lifecycle hooks, and an ownership manifest. Automatic tool approval is scoped to the DevGod MCP entry. Global Codex permissions remain under your control.

Repository `init` also installs `.codex/agents/devgod-*.toml`, which enrolls that repository for the Luna, Terra, and Sol subagent routes. Installing the DevGod distribution, plugin, or manager skill alone does not enroll a repository. Current local Codex supports subagents requested by applicable skill and project instructions. Reopen the Codex session after `init` or after changing `.codex/config.toml` so the host reloads those files. DevGod's isolated SDK review sessions deliberately disable further delegation to keep each review independent and bounded.

### Role-based model routing

DevGod installs project-scoped Codex agents that route clear implementation work to **GPT-5.6 Luna / medium**, ordinary planning and integration to **GPT-5.6 Terra / medium**, and evidence-backed hard escalations to **GPT-5.6 Sol / high**. The manager is intentionally a host conversation: select **GPT-5.6 Terra / medium** for that conversation before starting substantive DevGod work. Installation never rewrites your root model setting.

Luna workers receive explicit acceptance criteria, owned paths, dependencies, and checks. They escalate to Terra for unclear requirements; API, schema, persistence, security, concurrency, or cross-component work; and unexplained failed repairs. Terra escalates to Sol only after focused investigation leaves a material hard blocker, high-risk decision, or unresolved disagreement. The final DevGod check and independent reviews remain separate from all implementation roles.

`doctor` checks installation, dependency metadata, and local configuration; it reports `[features].multi_agent = false` with the setting needed to enable native subagents. It cannot observe the selected UI model, project trust, hook approval, or a successful subagent spawn, and it does not claim a successful authenticated model invocation. The package pins the Python Codex SDK and compatible runtime to **0.154.0**.

## 🗝️ Your everyday spellbook

Keep talking to Codex as usual:

> Use DevGod to fix invoice rounding. Clarify the design with me, then implement and verify the complete change on a local branch ready for review.

The installed instructions route substantive work through `devgod-manager`; you can also explicitly invoke `$devgod-manager`. Small questions and administrative changes stay lightweight.

The manager records acceptance criteria, task scopes and dependencies, and actual verification commands. Native Codex specialists implement the work through the installed role routes. The service executes checks and launches independent code, QA, and security sessions against a frozen source snapshot. The manager repairs failures and requests fresh verification until the current candidate passes.

You do not write action JSON, review receipts, checkpoints, or queue transitions. Progress and findings stay in the Codex conversation. Separate SDK review sessions appear in DevGod status; they are distinct from native implementation subagents.

Starting work preserves existing staged, unstaged, and untracked changes. DevGod finishes on a local branch; it does not automatically commit, publish a pull request, merge, or deploy.

## 🕸️ Pick up the thread

Say **“Resume the DevGod task.”** The manager restores the checkpoint and inspects interrupted work before retrying it. Closing Codex may stop execution; saved state supports recovery when it reopens.

For diagnostics:

```sh
devgod --repo /absolute/path/to/your-project status
devgod --repo /absolute/path/to/your-project next
devgod --repo /absolute/path/to/your-project resume
```

**Migrating an old installation?** Use `init --migrate`. It archives and disables recognizable legacy controls while preserving user modifications and historical data.

**Updating?** Pull the latest version into your DevGod checkout, reinstall it, and rerun `init` for each consuming project:

```sh
git -C /absolute/path/to/devgod pull --ff-only
uv tool install --reinstall /absolute/path/to/devgod
devgod --repo /absolute/path/to/your-project init
```

`uninstall` removes identifiable owned integration and retains delivery work and private history. The [operations guide](docs/operations.md) covers paths, commands, recovery, and execution boundaries.

## 🔮 Inside the grimoire

Codex owns the manager conversation and native implementation specialists. A local Python MCP service owns transactional SQLite state and verification. There is no database server to operate and no separate daily interface.

The service runs configured checks through Codex's sandboxed command interface. Independent code-review, QA, and security sessions assess a frozen candidate. Verification stays tied to the candidate that earned it.

- [Design](docs/design.md) — architecture, autonomy, and delivery rules.
- [Implementation plan](docs/implementation-plan.md) — the components and their responsibilities.
- [Platform research · September 12, 2026](docs/research/2026-09-12-codex-platform.md) — the evidence behind the design.
- [Original-project investigation](docs/research/2026-09-12-original-project.md) — a record of the old haunting.

## ⚗️ Development & contributions

From your DevGod checkout:

```sh
uv sync --locked
bash scripts/check.sh
uv build --no-sources
```

The blocking development gate runs Ruff, mypy, and the non-live tests. The [verification record](docs/verification.md) separates simulated failure tests from authenticated live execution. Live smoke scripts are opt-in: they require local Codex authentication and use model quota. They are excluded from ordinary CI.

Bug reports and contributions are welcome. [Open an issue](https://github.com/WitchyNibbles/devgod/issues) with the behavior you expected and steps to reproduce, or send a pull request with a focused change and its verification results. Keep credentials and private project data out of reports.

## 📜 License

[MIT](LICENSE) · Copyright (c) 2026 Eimi (WitchyNibbles).

---

<div align="center">

🕯️ <em>The code may be haunted. The checks should pass.</em> 🕯️

</div>
