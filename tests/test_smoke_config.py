"""The native smoke may use existing login, but must isolate runtime writes."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "devgod_native_smoke", Path(__file__).parents[1] / "scripts/native_smoke.py"
)
assert _SPEC is not None and _SPEC.loader is not None
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


@pytest.mark.parametrize(("pid", "argv", "expected"), [
    (100, [b"/runtime/codex", b"app-server", b"--listen", b"stdio://"], "native_app_server"),
    (101, [b"/venv/bin/python", b"-I", b"-m", b"devgod", b"mcp"], "devgod_mcp"),
    (102, [b"/venv/bin/python", b"-I", b"/package/devgod/launcher.py"], "verification_supervisor"),
    (103, [b"/runtime/codex", b"app-server", b"--listen", b"stdio://"], "verification_app_server"),
    (104, [b"/usr/bin/python3", b"-m", b"unittest"], "other"),
])
def test_process_roles_preserve_root_identity(pid, argv, expected):
    assert smoke._process_role(pid, 100, argv) == expected


@pytest.fixture
def shared_home(tmp_path: Path) -> Path:
    home = tmp_path / "shared"
    home.mkdir()
    (home / "auth.json").write_text('{"fixture_credential":"never-print-me"}\n')
    (home / "config.toml").write_text(
        'model = "fixture-model"\nmodel_reasoning_effort = "medium"\n'
        'model_provider = "selected"\napproval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\n'
        'persistent_instructions = "Do not copy global instructions"\n'
        '[model_providers.selected]\nname = "Fixture provider"\n'
        'base_url = "https://fixture.invalid/api"\nrequest_max_retries = 3\n'
        'supports_websockets = true\nhttp_headers = {"X-Fixture" = "private-value"}\n'
        '[model_providers.unused]\nname = "Do not copy this provider"\n'
        '[projects."/unrelated"]\ntrust_level = "trusted"\n'
        '[mcp_servers.unrelated]\ncommand = "do-not-run"\n'
        '[hooks]\nfixture = "do-not-run"\n'
        '[features]\nhooks = true\n'
        '[profiles.fixture]\nmodel = "do-not-copy"\n'
        '[plugins.fixture]\nenabled = true\n'
    )
    return home


def test_private_home_copies_only_selected_model_and_credentials(shared_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    originals = {name: (shared_home / name).read_bytes() for name in ("config.toml", "auth.json")}
    fixture = tmp_path / "report" / "consumer"
    fixture.mkdir(parents=True)
    isolated = smoke.PrivateCodexHome()
    with isolated:
        private = isolated.path
        assert private is not None
        assert not private.is_relative_to(fixture.parent)
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
        for name in originals:
            assert not (private / name).is_symlink()
            assert stat.S_IMODE((private / name).stat().st_mode) == 0o600
            assert (private / name).stat().st_ino != (shared_home / name).stat().st_ino
        assert (private / "auth.json").read_bytes() == originals["auth.json"]
        config = tomllib.loads((private / "config.toml").read_text())
        assert set(config) == {
            "model", "model_reasoning_effort", "model_provider", "model_providers",
            "cli_auth_credentials_store",
        }
        assert config["cli_auth_credentials_store"] == "file"
        assert set(config["model_providers"]) == {"selected"}
        assert config["model_providers"]["selected"]["http_headers"] == {"X-Fixture": "private-value"}
        # Simulate the observed native thread/start write and a credential refresh.
        with (private / "config.toml").open("a") as handle:
            handle.write(f'\n[projects.{json.dumps(str(fixture))}]\ntrust_level = "trusted"\n')
        (private / "auth.json").write_text('{"fixture_credential":"refreshed"}\n')
        isolated.capture(fixture)
        assert isolated.evidence["native_private_project_registered"]
        assert isolated.evidence["shared_hashes_before"] == isolated.evidence["shared_hashes_after"]
        assert os.environ["CODEX_HOME"] == str(shared_home)
    assert not private.exists()
    assert isolated.evidence["private_home_removed"]
    assert {name: (shared_home / name).read_bytes() for name in originals} == originals
    serialized = json.dumps(isolated.evidence)
    assert "never-print-me" not in serialized and "private-value" not in serialized


def test_private_home_is_removed_after_exception(shared_home):
    isolated = smoke.PrivateCodexHome(shared_home)
    with pytest.raises(RuntimeError, match="fixture failure"), isolated:
        private = isolated.path
        raise RuntimeError("fixture failure")
    assert private is not None and not private.exists()
    assert isolated.evidence["private_home_removed"]


def test_partial_setup_failure_removes_private_directory(shared_home, tmp_path, monkeypatch):
    target = tmp_path / "temporary-credential-home"

    def temporary(**_kwargs):
        target.mkdir()
        return str(target)

    monkeypatch.setattr(smoke.tempfile, "mkdtemp", temporary)
    (shared_home / "config.toml").write_text("not valid toml\n")
    with pytest.raises(tomllib.TOMLDecodeError), smoke.PrivateCodexHome(shared_home):
        pass
    assert not target.exists()


def test_credential_symlink_is_rejected_and_temporary_home_removed(shared_home):
    source = shared_home / "auth.json"
    original = source.read_bytes()
    actual = shared_home / "actual-credentials.json"
    source.rename(actual)
    source.symlink_to(actual)
    isolated = smoke.PrivateCodexHome(shared_home)
    with pytest.raises(ValueError, match="regular files"), isolated:
        pass
    assert isolated.path is None
    assert actual.read_bytes() == original


def test_tmpdir_cannot_place_credentials_inside_report(shared_home, tmp_path, monkeypatch):
    report = tmp_path / "report"
    report.mkdir()
    monkeypatch.setenv("TMPDIR", str(report))
    with smoke.PrivateCodexHome(shared_home, excluded_root=report) as isolated:
        assert not isolated.path.is_relative_to(report)


def test_excluded_root_is_checked_before_credentials_copy(shared_home, tmp_path, monkeypatch):
    report = tmp_path / "report"
    report.mkdir()
    private = report / "private"

    def misplaced(**_kwargs):
        private.mkdir()
        return str(private)

    monkeypatch.setattr(smoke.tempfile, "mkdtemp", misplaced)
    with pytest.raises(ValueError, match="outside"), smoke.PrivateCodexHome(
        shared_home, excluded_root=report,
    ):
        pass
    assert not private.exists()


def test_exercise_failure_records_shared_hashes_and_cleans_credentials(
    shared_home, tmp_path, monkeypatch,
):
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    output = tmp_path / "failed-report"
    output.mkdir()

    async def fail(*_args):
        raise RuntimeError("fixture failure before client start")

    monkeypatch.setattr(smoke, "_exercise", fail)
    with pytest.raises(RuntimeError, match="before client start"):
        asyncio.run(smoke.exercise(output, 1))
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "failed"
    assert not report["global_config_modified"]
    assert not report["global_auth_modified"]
    assert report["configuration_isolation"]["private_home_removed"]
    assert not Path(report["configuration_isolation"]["private_home"]).exists()
    assert "never-print-me" not in json.dumps(report)


VALID_FINAL_HANDOFF = """## Outcome
Implemented and verified the requested greeting behavior.

## Changes
Changed greetings.py and welcome.py, with coverage in test_greetings.py.

## Verification
Accepted check `/usr/bin/python3 -m unittest discover -v` passed with OK. Kernel status: branch `main` verified.

## Agents
Native delegated roles:
- Planner: concluded the design.
- Implementer: completed both modules.
- Verification agent: concluded all checks passed.

## Limitations
None.
"""


def test_final_handoff_validator_accepts_concrete_terminal_report():
    assert smoke.validate_final_handoff(VALID_FINAL_HANDOFF) == (True, None)


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (VALID_FINAL_HANDOFF.replace("## Limitations", "## Notes"), "terminal sections"),
        (VALID_FINAL_HANDOFF.replace(" and welcome.py", ""), "welcome.py"),
        (
            VALID_FINAL_HANDOFF.replace("/usr/bin/python3 -m unittest discover -v", "unittest"),
            "accepted unittest command",
        ),
        (VALID_FINAL_HANDOFF.replace("passed with OK", "was run"), "check result"),
        (VALID_FINAL_HANDOFF.replace("passed with OK", "not passed"), "check result"),
        (VALID_FINAL_HANDOFF.replace("passed with OK", "was not successful"), "check result"),
        (VALID_FINAL_HANDOFF.replace("passed with OK", "was not OK"), "check result"),
        (VALID_FINAL_HANDOFF.replace("branch `main` verified", "current revision"), "branch"),
        (
            VALID_FINAL_HANDOFF.replace(
                "Native delegated roles:\n- Planner: concluded the design.\n"
                "- Implementer: completed both modules.\n"
                "- Verification agent: concluded all checks passed.",
                "Several workers participated.",
            ),
            "separate native Planner and Implementer entries",
        ),
        (
            VALID_FINAL_HANDOFF.replace("- Planner: concluded the design.\n", ""),
            "separate native Planner and Implementer entries",
        ),
        (
            VALID_FINAL_HANDOFF.replace("- Implementer: completed both modules.\n", ""),
            "separate native Planner and Implementer entries",
        ),
        (
            VALID_FINAL_HANDOFF.replace(
                "- Planner: concluded the design.", "- Planner: participated."
            ),
            "separate native Planner and Implementer entries",
        ),
        (
            VALID_FINAL_HANDOFF.replace(
                "- Implementer: completed both modules.", "- Implementer: participated."
            ),
            "separate native Planner and Implementer entries",
        ),
    ],
)
def test_final_handoff_validator_rejects_missing_evidence(message, reason):
    valid, failure = smoke.validate_final_handoff(message)
    assert not valid
    assert failure is not None and reason in failure
    assert len(failure) <= smoke.FINAL_HANDOFF_FAILURE_LIMIT


def test_final_handoff_validator_bounds_failure_reason():
    message = "\n".join(f"## Unknown{i}\n" for i in range(1000))
    valid, failure = smoke.validate_final_handoff(message)
    assert not valid
    assert failure is not None
    assert len(failure) <= smoke.FINAL_HANDOFF_FAILURE_LIMIT


def test_final_handoff_report_bounds_message_and_preserves_identity():
    message = "x" * (smoke.FINAL_HANDOFF_REPORT_LIMIT + 10)

    report = smoke.final_handoff_report(message)

    assert report["final_message"] == message[: smoke.FINAL_HANDOFF_REPORT_LIMIT]
    assert report["final_message_truncated"] is True
    assert len(report["final_message_sha256"]) == 64
    assert report["final_message_sha256"] != smoke.final_handoff_report(message + "y")[
        "final_message_sha256"
    ]


@pytest.mark.parametrize(
    ("payload", "expected_id", "failure"),
    [
        ({"turn": {"id": "turn-1", "status": "completed", "error": None}}, "turn-1", None),
        ({"turn": {"id": "turn-2", "status": "completed", "error": None}}, "turn-1", "identity"),
        ({"turn": {"id": "turn-1", "status": "failed", "error": None}}, "turn-1", "failed"),
        ({"turn": {"id": "turn-1", "status": "completed", "error": {"message": "bad"}}}, "turn-1", "failed"),
    ],
)
def test_turn_completion_validator_requires_successful_requested_turn(payload, expected_id, failure):
    result = smoke.validate_turn_completion(payload, expected_id)
    if failure is None:
        assert result is None
    else:
        assert result is not None and failure in result
