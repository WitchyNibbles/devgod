# Native test configuration write — resolved

The Codex app-server bundled with SDK 0.154.0 added the temporary consuming repository to its project-trust configuration while handling `thread/start`. This happened before any model tool ran. The native test had inherited the user's Codex home, so the runtime persisted this entry in the shared `config.toml`.

## Historical evidence

The original successful delivery used `/tmp/devgod-native-5nu3_qy8/consumer`. Removing only its 75-byte project-trust table **in memory** reproduces the recorded before-test SHA-256 exactly:

| Configuration | SHA-256 |
| --- | --- |
| Before original test | `83964ffbff99a83bc86f45063047697d475cd05f9c8619b650e37c2c611277dc` |
| After original test | `b7dce39a127a80ccc5c87ad0ecf8d0ef2d129fd1aa1bbd1ebfa2aaa541758ca3` |

This accounts for every changed byte. No restoration or edit of the shared configuration was necessary. Its modification timestamp was about 13 milliseconds before the native thread ID was created and over 12 seconds before the first model tool. Independent review of the launching agent's actual tool call found the wheel installation and smoke invocation, with no explicit trust edit.

## Causal reproduction

A fresh Git fixture received DevGod through the tested wheel. The diagnostic process used a private Codex home containing regular copies of configuration and authentication, with directory mode `0700` and file mode `0600`. Those copies were removed after the probe. Configuration values and credential contents were excluded from the diagnostic output.

The hashes remained unchanged through Git setup, DevGod installation, SDK construction, process startup, and initialization. A workspace-write `thread/start` then added exactly `projects.<fixture>.trust_level` to the private configuration. No model turn was started, no approval callback occurred, and the shared configuration remained byte-identical.

Filesystem tracing identifies the actor and operation:

1. SDK Python process `64399` launched app-server process `64428`.
2. App-server threads `64431` and `64442` shared that process's address space and file descriptors, established by `CLONE_THREAD`.
3. Thread `64442` created a mode-`0600` temporary configuration file, wrote it, and used `renameat` to replace the private `config.toml` at Unix time `1789243181.556167`.
4. That write occurred after `initialize` returned and before `thread/start` returned.

The earlier empty read-only probe did not exercise this behavior. The earlier hypothesis of unrelated host activity is superseded by the exact historical byte reconstruction and this direct reproduction. The evidence establishes behavior for this pinned runtime and native test configuration; it does not generalize the write to every Codex session mode.

See the [retained stage hashes and syscall evidence](../evidence/2026-09-12-config-write.json). Write buffers were traced as pointers and byte counts; neither configuration contents nor credentials were traced.

## Correction

The native smoke test uses a private, temporary Codex home outside the consuming repository and retained reports. It inherits only the required model/provider settings and uses a private file-based credential cache. Child environment settings carry that home into the native manager, MCP service, and managed reviewers. The test checks the shared configuration and authentication hashes and removes private credential material on exit.

Runtime project registration is observed inside this disposable home. The test continues to use the generated DevGod MCP configuration and real native delegation. It does not test first-use graphical behavior or grant hook trust. Production DevGod modules and reviewer authentication are unchanged.

The real native rerun completed both dependent tasks, checks, and all three reviews with zero approval requests. Shared configuration and authentication files remained byte-identical; the private copies were removed. The monitor's recorded environment checks passed for the native process, MCP service, supervisors, and reviewer runtimes. One process-role label was subsequently corrected using the already-recorded root PID, with 12 regression tests and independent review. The [isolated-run evidence](../evidence/2026-09-12-native-isolated.json) retains that correction and the original observations explicitly.

This uses the supported [`CODEX_HOME` environment setting](https://learn.chatgpt.com/docs/config-file/environment-variables) and [`cli_auth_credentials_store` configuration](https://learn.chatgpt.com/docs/config-file/config-reference). [Authentication documentation](https://learn.chatgpt.com/docs/auth) describes the local credential cache. The actual runtime write described above is established by the local trace, rather than inferred from these documentation pages.
