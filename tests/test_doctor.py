"""Tests for `jarvis doctor`'s checks: pure functions, no hardware and no network."""

from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.doctor import (
    Check,
    _data_dir_privacy_check,
    format_check,
    has_hard_failure,
    run_doctor_checks,
)


@pytest.fixture
def healthy(tmp_path, monkeypatch):
    """Settings + environment where every check passes, so tests can break one at a time."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\n")

    models = tmp_path / "models"
    models.mkdir()
    (models / "hey_jarvis_v0.1.onnx").write_bytes(b"")
    monkeypatch.setattr("jarvis.doctor._wakeword_models_dir", lambda: models)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
    # The service is installed. Asked of a fake: the real question goes to `systemctl`.
    monkeypatch.setattr("jarvis.restart.service.is_installed", lambda target: True)

    credentials = tmp_path / "jarvis" / "google"
    credentials.mkdir(parents=True)
    (credentials / "credentials.json").write_text("{}")

    settings = Settings(
        _env_file=None,
        openai_api_key="sk-test",
        anthropic_api_key="sk-ant-test",
        twilio_account_sid="AC123",
        twilio_auth_token="token",
        twilio_number="+15550000000",
        allowed_callers=["+15551234567"],
        pin="123456",
        public_host="jarvis.example.com",
        data_dir=tmp_path / "jarvis",
        google_oauth_client_id="client-id",
        google_oauth_client_secret="client-secret",
    )
    # As every entry point does before running anything — it is what makes `data_dir`
    # owner-only, which the privacy check then looks at.
    settings.ensure_dirs()
    return settings


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


# --- the happy path --------------------------------------------------------


def test_a_fully_configured_install_passes_every_check(healthy):
    checks = run_doctor_checks(healthy, probe_mic=False)

    failed = [check.name for check in checks if not check.ok]
    assert failed == []
    assert has_hard_failure(checks) is False


def test_the_mic_probe_is_skipped_when_asked(healthy):
    names = {check.name for check in run_doctor_checks(healthy, probe_mic=False)}

    assert not any("mic" in name for name in names)


def test_the_mic_probe_never_raises_without_a_device(healthy, monkeypatch):
    def explode(**kwargs):
        raise OSError("no input device")

    monkeypatch.setattr("jarvis.doctor._query_input_device", explode)

    mic = [check for check in run_doctor_checks(healthy) if "mic" in check.name]
    assert len(mic) == 1
    assert mic[0].ok is False
    assert mic[0].severity == "soft"  # a headless Mac is not a hard failure


# --- individual failures ---------------------------------------------------


def test_a_missing_env_file_is_a_hard_failure(healthy, tmp_path):
    (tmp_path / ".env").unlink()

    check = by_name(run_doctor_checks(healthy, probe_mic=False))[".env"]
    assert (check.ok, check.severity) == (False, "hard")


def test_a_missing_openai_key_is_reported_not_raised(healthy, monkeypatch):
    from jarvis import doctor

    settings = healthy.model_copy(update={"openai_api_key": doctor.PLACEHOLDER_KEY})

    check = by_name(run_doctor_checks(settings, probe_mic=False))["OPENAI_API_KEY"]
    assert (check.ok, check.severity) == (False, "hard")


def test_an_api_key_satisfies_subagent_auth(healthy):
    check = by_name(run_doctor_checks(healthy, probe_mic=False))["subagent auth"]
    assert (check.ok, "pay-per-token" in check.detail) == (True, True)


def test_an_oauth_token_satisfies_subagent_auth(healthy):
    settings = healthy.model_copy(
        update={"anthropic_api_key": None, "claude_code_oauth_token": "tok"}
    )

    check = by_name(run_doctor_checks(settings, probe_mic=False))["subagent auth"]
    assert (check.ok, "subscription" in check.detail) == (True, True)


def test_a_cli_login_satisfies_subagent_auth(healthy, monkeypatch):
    monkeypatch.setattr("jarvis.doctor._has_claude_subscription_login", lambda: True)
    settings = healthy.model_copy(update={"anthropic_api_key": None})

    check = by_name(run_doctor_checks(settings, probe_mic=False))["subagent auth"]
    assert (check.ok, "login" in check.detail) == (True, True)


def test_no_subagent_auth_at_all_is_a_soft_failure(healthy, monkeypatch):
    monkeypatch.setattr("jarvis.doctor._has_claude_subscription_login", lambda: False)
    settings = healthy.model_copy(update={"anthropic_api_key": None})

    check = by_name(run_doctor_checks(settings, probe_mic=False))["subagent auth"]
    assert (check.ok, check.severity) == (False, "soft")


def test_the_bundled_claude_cli_counts_even_without_one_on_path(healthy, monkeypatch, tmp_path):
    """claude-agent-sdk 0.2 ships its own `claude`, and prefers it over PATH."""
    bundled = tmp_path / "_bundled" / "claude"
    bundled.parent.mkdir()
    bundled.write_text("#!/bin/sh\n")
    monkeypatch.setattr("jarvis.doctor._bundled_claude_cli", lambda: bundled)
    monkeypatch.setattr("shutil.which", lambda name: None if name == "claude" else "/bin/" + name)

    checks = by_name(run_doctor_checks(healthy, probe_mic=False))
    assert checks["claude CLI"].ok is True
    assert str(bundled) in checks["claude CLI"].detail
    assert checks["tunnel"].ok is True


def test_no_claude_cli_anywhere_is_a_soft_failure(healthy, monkeypatch):
    monkeypatch.setattr("jarvis.doctor._bundled_claude_cli", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: None if name == "claude" else "/bin/" + name)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["claude CLI"]
    assert (check.ok, check.severity) == (False, "soft")


def test_a_claude_cli_on_path_is_enough(healthy, monkeypatch):
    monkeypatch.setattr("jarvis.doctor._bundled_claude_cli", lambda: None)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["claude CLI"]
    assert (check.ok, check.detail) == (True, "/usr/local/bin/claude")


def test_cloudflared_satisfies_the_tunnel_check(healthy, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "ngrok" else "/bin/" + name)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["tunnel"]
    assert (check.ok, check.detail) == (True, "/bin/cloudflared")


def test_ngrok_still_counts_as_a_tunnel(healthy, monkeypatch):
    """The deployment moved to Cloudflare, but a machine with only ngrok is not broken."""
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name == "cloudflared" else "/bin/" + name
    )

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["tunnel"]
    assert (check.ok, check.detail) == (True, "/bin/ngrok")


def test_no_tunnel_binary_at_all_is_a_hard_failure(healthy, monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name in {"cloudflared", "ngrok"} else "/bin/" + name
    )

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["tunnel"]
    assert (check.ok, check.severity) == (False, "hard")
    assert "cloudflared" in check.detail


def test_incomplete_twilio_credentials_name_what_is_missing(healthy):
    settings = healthy.model_copy(update={"twilio_auth_token": None, "twilio_number": None})

    check = by_name(run_doctor_checks(settings, probe_mic=False))["Twilio credentials"]
    assert check.ok is False
    assert "TWILIO_AUTH_TOKEN" in check.detail
    assert "TWILIO_NUMBER" in check.detail
    assert "TWILIO_ACCOUNT_SID" not in check.detail


def test_a_missing_public_host_is_a_hard_failure(healthy):
    settings = healthy.model_copy(update={"public_host": None})

    assert by_name(run_doctor_checks(settings, probe_mic=False))["PUBLIC_HOST"].ok is False


def test_an_empty_caller_allowlist_is_a_hard_failure(healthy):
    settings = healthy.model_copy(update={"allowed_callers": []})

    assert by_name(run_doctor_checks(settings, probe_mic=False))["allowed callers"].ok is False


def test_a_missing_pin_only_warns(healthy):
    settings = healthy.model_copy(update={"pin": None})

    checks = run_doctor_checks(settings, probe_mic=False)
    check = by_name(checks)["PIN"]
    assert (check.ok, check.severity) == (False, "soft")
    # Not "coding/cowork": those kinds have not existed since 2026-08-24, and the PIN gates
    # every dispatch now.
    assert "every task is refused" in check.detail
    assert has_hard_failure(checks) is False


def test_an_undownloaded_wake_word_model_points_at_download_models(healthy, monkeypatch, tmp_path):
    empty = tmp_path / "empty-models"
    empty.mkdir()
    monkeypatch.setattr("jarvis.doctor._wakeword_models_dir", lambda: empty)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["wake-word model"]
    assert check.ok is False
    assert "download-models" in check.detail


def test_an_uninstalled_openwakeword_only_warns(healthy, monkeypatch):
    """openwakeword is macOS-only, so a Linux phone-only host is not a broken install."""

    def explode():
        raise ImportError("no openwakeword here")

    monkeypatch.setattr("jarvis.doctor._wakeword_models_dir", explode)

    checks = run_doctor_checks(healthy, probe_mic=False)
    check = by_name(checks)["wake-word model"]
    assert (check.ok, check.severity) == (False, "soft")
    assert "phone channel alone" in check.detail
    assert has_hard_failure(checks) is False


def test_a_broken_openwakeword_install_is_reported_not_raised(healthy, monkeypatch):
    def explode():
        raise OSError("resources are gone")

    monkeypatch.setattr("jarvis.doctor._wakeword_models_dir", explode)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["wake-word model"]
    assert check.ok is False
    assert "openwakeword" in check.detail


def test_an_installed_service_is_reported(healthy):
    check = by_name(run_doctor_checks(healthy, probe_mic=False))["service manager"]

    assert check.ok is True
    assert check.detail.split()[0] in {"systemd", "launchd"}


def test_a_service_manager_with_nothing_installed_is_not_a_tick(healthy, monkeypatch):
    """`systemctl` on PATH used to be enough for a ✅ naming a unit that did not exist."""
    monkeypatch.setattr("jarvis.restart.service.is_installed", lambda target: False)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["service manager"]

    assert (check.ok, check.severity) == (False, "soft")
    assert "not installed" in check.detail
    assert "install-" in check.detail  # which installer puts it there
    assert "restart_service" in check.detail


def test_nothing_supervising_the_process_is_a_warning_that_says_what_is_lost(healthy):
    """`SERVICE_MANAGER=none` is a supported way to run; the consequence is just silent."""
    settings = healthy.model_copy(update={"service_manager": "none"})

    check = by_name(run_doctor_checks(settings, probe_mic=False))["service manager"]

    assert (check.ok, check.severity) == (False, "soft")
    assert "restart_service" in check.detail


def test_a_missing_git_is_reported_next_to_the_service_manager(healthy, monkeypatch):
    """Version reporting degrades quietly without it, which is worth saying once."""
    monkeypatch.setattr("shutil.which", lambda name: None if name == "git" else f"/bin/{name}")

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["service manager"]

    assert check.ok is True
    assert "no git on PATH" in check.detail


def test_an_unwritable_data_dir_is_a_hard_failure(healthy, tmp_path):
    settings = healthy.model_copy(update={"data_dir": Path("/dev/null/nope")})

    check = by_name(run_doctor_checks(settings, probe_mic=False))["data dir writable"]
    assert (check.ok, check.severity) == (False, "hard")


def test_a_world_readable_data_dir_only_warns(healthy):
    """A shared host is where this matters; a single-user one is not worth refusing over."""
    healthy.data_dir.chmod(0o755)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["data dir private"]

    assert (check.ok, check.severity) == (False, "soft")
    assert "0755" in check.detail
    assert "chmod 0700" in check.detail


def test_a_group_readable_data_dir_is_reported_too(healthy):
    healthy.data_dir.chmod(0o740)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["data dir private"]

    assert check.ok is False


def test_an_owner_only_data_dir_passes(healthy):
    check = by_name(run_doctor_checks(healthy, probe_mic=False))["data dir private"]

    assert (check.ok, check.detail) == (True, "0700 (owner only)")


def test_doctor_creates_a_missing_data_dir_owner_only(healthy, tmp_path):
    """The write probe creates the directory; it must not leave a loose one behind."""
    settings = healthy.model_copy(update={"data_dir": tmp_path / "never-created"})

    check = by_name(run_doctor_checks(settings, probe_mic=False))["data dir private"]

    assert (check.ok, check.detail) == (True, "0700 (owner only)")


def test_a_data_dir_that_cannot_be_read_is_reported_not_raised(healthy):
    check = _data_dir_privacy_check(healthy.model_copy(update={"data_dir": Path("/dev/null/nope")}))

    assert (check.ok, check.severity) == (False, "soft")


def test_google_is_reported_as_unused_while_workspace_mcp_is_off(healthy):
    check = by_name(run_doctor_checks(healthy, probe_mic=False))["Google credentials"]

    assert (check.ok, check.severity) == (True, "soft")
    assert "connectors" in check.detail


def test_google_credentials_only_warn_when_the_oauth_client_is_configured(healthy, tmp_path):
    healthy.google_workspace_mcp = True
    for path in (tmp_path / "jarvis" / "google").iterdir():
        path.unlink()

    checks = run_doctor_checks(healthy, probe_mic=False)
    check = by_name(checks)["Google credentials"]
    assert (check.ok, check.severity) == (False, "soft")
    assert "setup-google" in check.detail
    assert has_hard_failure(checks) is False


def test_google_is_reported_as_not_configured_without_an_oauth_client(healthy):
    settings = healthy.model_copy(
        update={
            "google_workspace_mcp": True,
            "google_oauth_client_id": None,
            "google_oauth_client_secret": None,
        }
    )

    check = by_name(run_doctor_checks(settings, probe_mic=False))["Google credentials"]
    assert (check.ok, check.severity) == (True, "soft")
    assert "not configured" in check.detail


# --- formatting ------------------------------------------------------------


def test_each_severity_gets_its_own_marker():
    assert format_check(Check("a", True, "fine")).startswith("✅")
    assert format_check(Check("b", False, "broken")).startswith("❌")
    assert format_check(Check("c", False, "meh", severity="soft")).startswith("⚠️")


def test_a_formatted_check_carries_its_name_and_detail():
    line = format_check(Check("tunnel", True, "/usr/local/bin/cloudflared"))

    assert "tunnel" in line
    assert "/usr/local/bin/cloudflared" in line
