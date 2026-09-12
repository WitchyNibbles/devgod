# Verification record — 2026-09-12

This record distinguishes actual Codex execution, native conversation behavior, simulated failure tests, and installation checks. The automated, security, packaging, production-service, and native delivery gates below have passed against the final source or its matching distribution. The earlier shared-configuration failure has been traced to Codex app-server project-trust registration during thread initialization; the cause and test-harness correction are recorded below.

## Automated checks

The blocking command is `bash scripts/check.sh`: Ruff over source, tests, and scripts; mypy over product modules; and the complete non-live pytest suite. CI runs the same checks on Linux with Python 3.12 and 3.13 and builds the distribution. Local execution uses Python 3.12.3, `openai-codex` and its bundled runtime 0.154.0, and MCP 2.2.0.

Final local result: **212 tests passed in 32.05 seconds**, Ruff passed, and mypy passed over all 13 product modules. This includes 12 additional regressions for native-test configuration isolation, credential cleanup, and process classification. CI is configured; a remote CI run is not claimed.

The suite exercises:

| Area | Evidence |
| --- | --- |
| Delivery and repair | Fresh Git fixture, failed actual test command, independent review rejection, repairs, dependent task revalidation, and current verified branch. Only Codex transport is substituted in these acceptance tests. |
| No forged completion | Caller claims, checkpoint data, incomplete criteria, missing reviews, wrong invocation/session identity, and late lease results cannot produce verification. |
| Freshness | Source, branch, check-plan, snapshot, and artifact mutation revoke verification. Concurrent plan changes cannot race an old gate into success. |
| Recovery | Duplicate dispatch, interrupted work, expired owners, unchanged-source inspected retry, and rebuilding missing or corrupt evidence without artificial source edits. |
| Diagnostics | Actual rejecting findings and failed-check output are visible through public tools; symlinks and FIFOs cannot redirect diagnostic reads. |
| Workspace | Staged, unstaged, and untracked baseline preservation; linked worktrees; submodules; path/symlink defenses; snapshot race detection; inert Codex configuration. |
| Host boundary | Isolated Python launchers; disabled Git hooks, filters, and external drivers; rejected Git worktree redirection; explicit denied SDK approval callbacks and restricted reviewer tools. |
| Native integration | Real stdio MCP initialization and tools, background job lifetime, CLI exit codes, checkpoints, bounded continuation, and managed-review hook suppression. |
| Installation | Repeated setup, migration, uninstall, namespace collisions, edited-policy preservation, private backup paths, packaged assets, and explicit known-tool approval policy. |
| Process ownership | Real OS child supervision, detached descendants, parent death, missing/forged receipt rejection, and refusal to equate a main process exit with completed descendant cleanup. |

Simulated reviewer payloads test gate behavior; they are not described as live independent model reviews.

## Authenticated production execution

The production service smoke uses `Workspace`, `Store`, `DevGodService`, `VerificationRunner`, and `CodexAdapter` directly. The fixture implementation is scripted. Its first arithmetic check really fails; after repair, a fresh sandboxed check and three actual independent Codex sessions approve the candidate. The public kernel reaches `verified`, and a subsequent source edit returns it to `repair`.

The final successful integrated run was `run_4740b70c153743e9818c252e7d0ebd61`, with branch `devgod/run_4740b70c153743e9818c252e7d0ebd61`. It completed in 32.76 seconds, and its recorded source hashes match the frozen runtime. Reviewer thread IDs were:

- Code review: `01a096fb-9dfd-79f3-afa3-9dc57877a622`.
- QA: `01a096fb-9e04-7840-ae36-0ec6844cdc28`.
- Security: `01a096fb-9e04-7ed2-9aa1-ffe5ae54f597`.

The checked candidate digest was `93c6858dbbf68049af991efe09ad844ba99f6f548a83062c7bccb124b3c7592b`. [Retained service evidence](evidence/2026-09-12-live-service.json) includes the failed and successful check outcomes, independent review identities, current gate, source hashes, and subsequent invalidation.

The separate implemented-adapter smoke also passed after adding the subreaper supervisor: an actual command returned exit code 0 and a structured reviewer approved its observed result, thread `01a096f5-d387-7290-8dbb-b19761b7b747`. The [SDK spike report](research/2026-09-12-sdk-spike.md) documents protocol details and clearly labels its placeholder candidate digests as adapter-only evidence.

Reproduce complete service verification with:

```sh
uv run --locked python scripts/live_smoke.py
```

It uses local Codex authentication and model quota, retains a report in its temporary fixture directory, and records the tested runtime source fingerprints. It is not part of ordinary CI.

## Native Codex conversation

The native test installs the built wheel in an isolated environment, creates a fresh consuming repository, invokes the installed manager skill in a real Codex thread, and observes actual native subagent and MCP events. Passing requires two dependent tasks, a current kernel gate, three independent reviews, and zero approval callbacks or MCP approval-policy errors. Prose cannot satisfy it.

The first run exposed an integration defect: `auto` approval mode permitted bookkeeping but blocked `verify`. Codex correctly reported the work incomplete. Explicit approval of the known DevGod tools fixed this; a second run completed native planning, both implementation specialists, checks, and all three reviews without an approval request.

The earlier successful native delivery used the same wheel hash as the passing package smoke and completed in 291.00 seconds. Manager thread `01a096fc-41f4-7351-9ca8-93aa535b7be4` started an actual native planner and two implementation specialists. Both dependent tasks reached `verified` on branch `devgod/run_8f31087c2bfe413daf6032cc5e444748`. The accepted command passed all 11 fixture regression tests, and all three independent review jobs approved the current candidate. There were 17 MCP calls, zero approval callbacks, and zero MCP approval-policy errors. The manager corrected one task-role schema error itself.

The test supplies the generated repository MCP configuration through the supported app-server test-client configuration. The pinned runtime registers project trust during thread initialization; the corrected test confines that registration to a disposable Codex home. It does not grant hook trust, exercise the graphical app, or establish first-use UI behavior. Actual hook payload and continuation behavior is tested separately.

The earlier wrapper's shared-configuration assertion failed because app-server persisted a project-trust entry for the temporary consuming repository. Removing only that 75-byte entry in memory reproduces the recorded original hash exactly. An isolated reproduction then traced the app-server worker's file write and atomic rename between `initialize` and `thread/start`, before any model turn. Shared configuration remained unchanged during this reproduction. Independent historical tool-call review agrees with this attribution. The previous unexplained-host-activity hypothesis is superseded; see the [cause investigation](research/2026-09-12-config-write.md) and [retained syscall evidence](evidence/2026-09-12-config-write.json).

The [original native evidence](evidence/2026-09-12-native.json) retains its actual `delivery_status: passed` and `wrapper_status: failed` with the resolved attribution. The corrected test uses private settings and authentication files, checks shared configuration and authentication hashes, and removes private credential material on exit. Production modules and the tested wheel are unchanged.

The isolated native rerun completed delivery in **283.52 seconds**, thread `01a0973a-427b-73f1-adfa-f03a840bc5d1`, on branch `devgod/run_63bb5cc6455c4594a19ce6a6fe910b76`. Three native children, both dependent tasks, the accepted check, and all three independent reviews completed successfully. There were 17 MCP calls and zero approval requests or approval-policy errors. Both shared `config.toml` and `auth.json` remained byte-identical. Project trust was recorded only in the private home, and that home and its credential copies were removed.

The rerun's process monitor initially mislabeled the main app-server as a reviewer because its command line also contained `app-server`. Its recorded PID, parent relationship, and private-home match were correct. The final classifier gives the known root PID precedence; re-evaluating the unaltered observations establishes all four required runtime roles with private homes. Independent security review and 12 isolation/classifier regression tests validate the correction. The [retained isolated-run evidence](evidence/2026-09-12-native-isolated.json) preserves the original failed label assertion alongside this explicit correction; it does not describe the correction as another model execution.

Run the native smoke from the isolated wheel environment produced by the package smoke:

```sh
/path/from/package-report/venv/bin/python -I scripts/native_smoke.py
```

## Distribution

`scripts/package_smoke.py` builds a wheel and source archive, checks hidden plugin/MCP files and manager skill assets, installs the wheel into a fresh temporary environment, and imports it without the checkout on `sys.path`. It verifies setup, diagnostics, byte-idempotent reinstallation, and removal while preserving existing user instructions and configuration. README metadata and the isolated launcher are also checked.

The final package smoke passed using wheel SHA-256 `a39f98248167df81f06774887401684cae9cc8f2bd1b682738c919120d74c235`. [Retained packaging evidence](evidence/2026-09-12-package.json) records the checks and external import location.

```sh
uv run --locked python scripts/package_smoke.py --online
```

The optional `--online` allows dependency downloads; omit it when dependencies are cached. It does not install globally or grant Codex trust.

## Independent review and limits

Architecture and decomposition agents established the product contract and module boundaries. Implementation specialists built the kernel, workspace, adapter, verifier, and native interfaces. Independent QA and security reviews found and drove fixes for interrupted-work dead ends, inaccessible findings, stale gates, import shadowing, Git filter execution, workspace redirection, policy-alias bypass, and detached-child termination claims.

The final independent security review passed with **53 adapter/security tests** and no remaining concrete high/medium finding. Root independently reviewed the security reviewer's workspace fixes. QA's fresh-repository acceptance tests pass under the final termination and public-diagnostics contracts. The architecture reviewer independently closed the earlier hard-crash recovery blocker after reviewing the implemented supervisor, restart reconciliation, and the corresponding regression evidence. The subsequent native-test isolation and process-classifier corrections also passed independent security review and all 12 focused tests.

Managed execution currently requires Linux. An existing Git commit anchors the delivery branch. Codex retains project/hook trust and real host authorization boundaries. Hooks can fail open, and closing Codex can stop implementation; persistence supports resumption. A supervisor forcibly destroyed without its termination receipt cannot certify that all effects stopped. Read-only review is not secret-read confinement, and unrestricted same-user host compromise is outside the state-integrity boundary. Model review and tests provide evidence, not a proof of complete program correctness.

The original sibling project was researched without modifying it. The replacement is developed in this checkout; no global installation, PR, merge, or deployment is part of this delivery.
