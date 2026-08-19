"""Tests for `jarvis doctor`'s checks: pure functions, no hardware and no network."""

from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.doctor import Check, format_check, has_hard_failure, run_doctor_checks


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

    credentials = tmp_path / "jarvis" / "google"
    credentials.mkdir(parents=True)
    (credentials / "credentials.json").write_text("{}")

    return Settings(
        _env_file=None,
        openai_api_key="sk-test",
        anthropic_api_key="sk-ant-test",
        twilio_account_sid="AC123",
        twilio_auth_token="token",
        twilio_number="+15550000000",
        allowed_callers=["+15551234567"],
        pin="1234",
        public_host="jarvis.ngrok.app",
        data_dir=tmp_path / "jarvis",
        google_oauth_client_id="client-id",
        google_oauth_client_secret="client-secret",
    )


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


def test_a_missing_anthropic_key_is_a_hard_failure(healthy):
    settings = healthy.model_copy(update={"anthropic_api_key": None})

    check = by_name(run_doctor_checks(settings, probe_mic=False))["ANTHROPIC_API_KEY"]
    assert (check.ok, check.severity) == (False, "hard")


def test_a_missing_claude_cli_is_a_hard_failure(healthy, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "claude" else "/bin/" + name)

    checks = by_name(run_doctor_checks(healthy, probe_mic=False))
    assert checks["claude CLI"].ok is False
    assert checks["ngrok"].ok is True


def test_a_missing_ngrok_is_a_hard_failure(healthy, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "ngrok" else "/bin/" + name)

    assert by_name(run_doctor_checks(healthy, probe_mic=False))["ngrok"].ok is False


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
    assert "coding" in check.detail
    assert has_hard_failure(checks) is False


def test_an_undownloaded_wake_word_model_points_at_download_models(healthy, monkeypatch, tmp_path):
    empty = tmp_path / "empty-models"
    empty.mkdir()
    monkeypatch.setattr("jarvis.doctor._wakeword_models_dir", lambda: empty)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["wake-word model"]
    assert check.ok is False
    assert "download-models" in check.detail


def test_an_unimportable_openwakeword_is_reported_not_raised(healthy, monkeypatch):
    def explode():
        raise ImportError("no openwakeword here")

    monkeypatch.setattr("jarvis.doctor._wakeword_models_dir", explode)

    check = by_name(run_doctor_checks(healthy, probe_mic=False))["wake-word model"]
    assert check.ok is False
    assert "openwakeword" in check.detail


def test_an_unwritable_data_dir_is_a_hard_failure(healthy, tmp_path):
    settings = healthy.model_copy(update={"data_dir": Path("/dev/null/nope")})

    check = by_name(run_doctor_checks(settings, probe_mic=False))["data dir writable"]
    assert (check.ok, check.severity) == (False, "hard")


def test_google_credentials_only_warn_when_the_oauth_client_is_configured(healthy, tmp_path):
    for path in (tmp_path / "jarvis" / "google").iterdir():
        path.unlink()

    checks = run_doctor_checks(healthy, probe_mic=False)
    check = by_name(checks)["Google credentials"]
    assert (check.ok, check.severity) == (False, "soft")
    assert "setup-google" in check.detail
    assert has_hard_failure(checks) is False


def test_google_is_reported_as_not_configured_without_an_oauth_client(healthy):
    settings = healthy.model_copy(
        update={"google_oauth_client_id": None, "google_oauth_client_secret": None}
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
    line = format_check(Check("ngrok", True, "/usr/local/bin/ngrok"))

    assert "ngrok" in line
    assert "/usr/local/bin/ngrok" in line
