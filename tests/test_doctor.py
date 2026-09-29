"""Tests for `jarvis doctor`'s checks: pure functions, no hardware and no network."""

import dataclasses
import importlib.util
from pathlib import Path

import pytest

from jarvis import plugins
from jarvis.agents.registry import BACKENDS
from jarvis.config import Settings, pin_file
from jarvis.config.store import ConfigStore
from jarvis.continuity.memory import memory_path, seed_memory
from jarvis.doctor import (
    Check,
    _data_dir_privacy_check,
    format_check,
    has_hard_failure,
    run_doctor_checks,
)
from jarvis.integrations.gmail import token_path
from jarvis.logging_util import mask_number


@pytest.fixture
def healthy(tmp_path, monkeypatch, every_agent_installed):
    """Settings + environment where every check passes, so tests can break one at a time."""
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
    # The service is installed. Asked of a fake: the real question goes to `systemctl`.
    monkeypatch.setattr("jarvis.restart.service.is_installed", lambda target: True)

    credentials = tmp_path / "jarvis" / "google"
    credentials.mkdir(parents=True, mode=0o700)
    (credentials / "credentials.json").write_text("{}")
    (credentials / "credentials.json").chmod(0o600)
    (tmp_path / "jarvis" / "gmail_token.json").write_text("{}")
    (tmp_path / "jarvis" / "gmail_token.json").chmod(0o600)

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
        owner_name="Sam",
        projects_root=tmp_path / "projects",
        slack_bot_token="xoxb-test",
        openai_admin_key="sk-admin-test",
    )
    # As every entry point does before running anything — it is what makes `data_dir`
    # owner-only, which the privacy check then looks at.
    settings.ensure_dirs()
    settings.projects_root.mkdir()
    seed_memory(settings.data_dir, owner="Sam", facts=["Works nights."])
    # Every plugin on: loading them contacts nothing (Slack posts, Gmail reads, ssh runs
    # only when a tool is called).
    with_agent(monkeypatch, "claude", cli="/bin/claude")
    for name, values in {
        "send_to_slack": {"channel_id": "D123"},
        "check_email": {},
        "check_billing": {"monthly_budget": 40},
        "cluster_stats": {"clusters": {"alpha": "gpu"}},
    }.items():
        plugins.write_config(settings, name, values)
        plugins.install(settings, name)
    return settings


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


# --- the happy path --------------------------------------------------------


def test_a_fully_configured_install_passes_every_check(healthy):
    checks = run_doctor_checks(healthy)

    failed = [check.name for check in checks if not check.ok]
    assert failed == []
    assert has_hard_failure(checks) is False


def test_files_left_in_the_old_home_are_a_migration_still_to_run(healthy):
    legacy = Path.home() / ".jarvis"
    legacy.mkdir(parents=True)
    (legacy / "tasks.db").touch()

    check = by_name(run_doctor_checks(healthy))["storage"]

    assert (check.ok, check.severity, check.state) == (False, "hard", "missing")
    assert "jarvis migrate" in check.detail and str(legacy) in check.detail
    assert check.section == "import"


def test_a_env_in_the_working_directory_is_a_migration_still_to_run(healthy):
    Path(".env").write_text("OPENAI_VOICE=marin\n")

    check = by_name(run_doctor_checks(healthy))["storage"]

    assert check.ok is False and "jarvis migrate" in check.detail


def test_nothing_legacy_is_fine(healthy):
    checks = by_name(run_doctor_checks(healthy))

    assert checks["storage"].ok is True
    assert checks["configuration"].ok is True


def test_a_missing_openai_key_is_reported_not_raised(healthy, monkeypatch):
    from jarvis import doctor

    settings = healthy.model_copy(update={"openai_api_key": doctor.PLACEHOLDER_KEY})

    check = by_name(run_doctor_checks(settings))["OPENAI_API_KEY"]
    assert (check.ok, check.severity) == (False, "hard")


CLAUDE = "Claude Code agent (default)"


def with_agent(monkeypatch, name, *, cli="/bin/agent", login=True):
    """Pretend `name`'s CLI is at `cli` (None: missing) and its stored login is `login`."""
    spec = BACKENDS[name]
    monkeypatch.setitem(
        BACKENDS,
        name,
        dataclasses.replace(
            spec,
            find_cli=lambda: cli,
            auth=dataclasses.replace(spec.auth, stored_login=lambda: login),
        ),
    )


def test_an_api_key_satisfies_the_default_agent(healthy):
    check = by_name(run_doctor_checks(healthy))[CLAUDE]
    assert (check.ok, "pay per token" in check.detail) == (True, True)


def test_an_oauth_token_satisfies_the_default_agent(healthy):
    settings = healthy.model_copy(
        update={"anthropic_api_key": None, "claude_code_oauth_token": "tok"}
    )

    check = by_name(run_doctor_checks(settings))[CLAUDE]
    assert (check.ok, "subscription" in check.detail) == (True, True)


def test_a_stored_login_satisfies_the_default_agent(healthy, monkeypatch):
    with_agent(monkeypatch, "claude", login=True)
    settings = healthy.model_copy(update={"anthropic_api_key": None})

    check = by_name(run_doctor_checks(settings))[CLAUDE]
    assert (check.ok, "login" in check.detail) == (True, True)


def test_a_default_agent_with_no_auth_at_all_is_a_hard_failure(healthy, monkeypatch):
    """Every task nobody named an agent for goes to it."""
    with_agent(monkeypatch, "claude", login=False)
    settings = healthy.model_copy(update={"anthropic_api_key": None})

    check = by_name(run_doctor_checks(settings))[CLAUDE]
    assert (check.ok, check.severity) == (False, "hard")
    assert "claude setup-token" in check.detail


def test_a_default_agent_that_is_not_installed_says_how_to_install_it(healthy, monkeypatch):
    with_agent(monkeypatch, "claude", cli=None)

    check = by_name(run_doctor_checks(healthy))[CLAUDE]
    assert (check.ok, check.severity) == (False, "hard")
    assert "uv sync" in check.detail


def test_the_agents_cli_is_named_when_it_is_there(healthy, monkeypatch):
    with_agent(monkeypatch, "claude", cli="/opt/claude")

    check = by_name(run_doctor_checks(healthy))[CLAUDE]
    assert check.detail.startswith("/opt/claude; ")


def test_an_agent_that_is_not_installed_says_which_extra_installs_it(healthy, monkeypatch):
    monkeypatch.setattr("jarvis.doctor.installed", lambda agent: agent != "codex")
    settings = healthy.model_copy(update={"agents_enabled": ["claude", "codex"]})

    check = by_name(run_doctor_checks(settings))["Codex agent"]

    assert (check.ok, check.severity) == (False, "soft")
    assert check.detail == (
        "not installed — uv sync --extra codex (the openai-codex SDK bundles the codex CLI)"
    )


@pytest.mark.skipif(importlib.util.find_spec("openai_codex") is None, reason="codex extra")
def test_codex_is_named_by_its_bundled_binary_and_version(healthy, monkeypatch):
    with_agent(monkeypatch, "codex", cli="/venv/codex_cli_bin/bin/codex")
    settings = healthy.model_copy(update={"agents_enabled": ["claude", "codex"]})

    check = by_name(run_doctor_checks(settings))["Codex agent"]

    assert check.detail.startswith("/venv/codex_cli_bin/bin/codex (codex-cli 0.157.1); ")


def test_a_second_agent_that_is_not_ready_is_only_a_warning(healthy, monkeypatch):
    with_agent(monkeypatch, "codex", login=False)
    settings = healthy.model_copy(update={"agents_enabled": ["claude", "codex"]})

    checks = by_name(run_doctor_checks(settings))
    assert (checks["Codex agent"].ok, checks["Codex agent"].severity) == (False, "soft")
    assert "codex login" in checks["Codex agent"].detail
    assert checks[CLAUDE].ok is True
    assert checks["coding agents"].detail == "claude by default; enabled: claude, codex"


def test_a_default_agent_that_is_not_enabled_is_a_hard_failure(healthy):
    settings = healthy.model_copy(
        update={"agents_enabled": ["claude"], "agent_backend": "codex"}
    )

    check = by_name(run_doctor_checks(settings))["coding agents"]
    assert (check.ok, check.severity) == (False, "hard")


def test_codex_without_workspace_mcp_has_no_mailbox_and_says_so(healthy, monkeypatch):
    with_agent(monkeypatch, "codex")
    settings = healthy.model_copy(update={"agents_enabled": ["claude", "codex"]})

    check = by_name(run_doctor_checks(settings))["Google for agents"]
    assert (check.ok, check.severity, check.state) == (False, "soft", "missing")
    assert "not set up (optional)" in check.detail and "jarvis setup" in check.detail


def test_cloudflared_satisfies_the_tunnel_check(healthy, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "ngrok" else "/bin/" + name)

    check = by_name(run_doctor_checks(healthy))["tunnel"]
    assert (check.ok, check.detail) == (True, "/bin/cloudflared")


def test_ngrok_still_counts_as_a_tunnel(healthy, monkeypatch):
    """The deployment moved to Cloudflare, but a machine with only ngrok is not broken."""
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name == "cloudflared" else "/bin/" + name
    )

    check = by_name(run_doctor_checks(healthy))["tunnel"]
    assert (check.ok, check.detail) == (True, "/bin/ngrok")


def test_no_tunnel_binary_at_all_is_a_hard_failure(healthy, monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name in {"cloudflared", "ngrok"} else "/bin/" + name
    )

    check = by_name(run_doctor_checks(healthy))["tunnel"]
    assert (check.ok, check.severity) == (False, "hard")
    assert "cloudflared" in check.detail


def test_incomplete_twilio_credentials_name_what_is_missing(healthy):
    settings = healthy.model_copy(update={"twilio_auth_token": None, "twilio_number": None})

    check = by_name(run_doctor_checks(settings))["Twilio credentials"]
    assert check.ok is False
    assert "TWILIO_AUTH_TOKEN" in check.detail
    assert "TWILIO_NUMBER" in check.detail
    assert "TWILIO_ACCOUNT_SID" not in check.detail


def test_a_missing_public_host_is_a_hard_failure(healthy):
    settings = healthy.model_copy(update={"public_host": None})

    assert by_name(run_doctor_checks(settings))["PUBLIC_HOST"].ok is False


def test_an_empty_caller_allowlist_is_a_hard_failure(healthy):
    settings = healthy.model_copy(update={"allowed_callers": []})

    assert by_name(run_doctor_checks(settings))["allowed callers"].ok is False


def test_skipping_signature_checks_behind_a_public_host_is_a_hard_failure(healthy):
    settings = healthy.model_copy(update={"debug_skip_twilio_validation": True})

    checks = run_doctor_checks(settings)
    check = by_name(checks)["Twilio signatures"]
    assert (check.ok, check.severity) == (False, "hard")
    assert "DEBUG_SKIP_TWILIO_VALIDATION" in check.detail
    assert has_hard_failure(checks) is True


def test_skipping_signature_checks_without_a_public_host_only_warns(healthy):
    settings = healthy.model_copy(
        update={"debug_skip_twilio_validation": True, "public_host": None}
    )

    check = by_name(run_doctor_checks(settings))["Twilio signatures"]
    assert (check.ok, check.severity) == (False, "soft")


def test_signature_checks_left_on_pass(healthy):
    assert by_name(run_doctor_checks(healthy))["Twilio signatures"].ok is True


def test_the_numbers_doctor_prints_are_masked(healthy):
    """A terminal is somewhere a number gets written down too (`logging_util`)."""
    checks = by_name(run_doctor_checks(healthy))

    assert checks["allowed callers"].detail == mask_number("+15551234567")
    assert checks["Twilio credentials"].detail == mask_number("+15550000000")


def test_a_missing_pin_only_warns_and_says_the_first_call_can_set_one(healthy):
    """Unset is no longer a dead end: the phone can enrol one, and until it does the
    machine reads nothing of theirs out loud."""
    settings = healthy.model_copy(update={"pin": None})

    checks = run_doctor_checks(settings)
    check = by_name(checks)["PIN"]
    assert (check.ok, check.severity) == (False, "soft")
    assert "no PIN yet" in check.detail
    assert "`jarvis setup` or the first call can set one" in check.detail
    assert "nothing of yours is read out until then" in check.detail
    assert check.state == "missing"
    assert has_hard_failure(checks) is False


def test_a_pin_from_the_environment_says_so(healthy):
    check = by_name(run_doctor_checks(healthy))["PIN"]

    assert check.ok is True
    assert check.detail.startswith("set from the environment")
    assert "123456" not in check.detail


def test_a_pin_in_its_own_file_says_when_and_where(healthy):
    """`JARVIS_HOME/pin` is the PIN's store now, whoever wrote it — setup or a first call."""
    settings = healthy.model_copy(update={"pin": None})
    assert settings.enrol_pin("987654") is True

    check = by_name(run_doctor_checks(settings))["PIN"]

    assert check.ok is True
    assert check.detail.startswith("set on ")
    assert str(pin_file(settings.config_dir)) in check.detail
    assert "987654" not in check.detail


def test_the_environment_wins_over_the_same_digits_in_the_file(healthy):
    """The same digits in both places are the environment's: a source read off the file's
    contents would say otherwise."""
    copied = healthy.model_copy(update={"pin": None})
    assert copied.enrol_pin("123456") is True  # the digits `healthy` has in its environment

    check = by_name(run_doctor_checks(healthy))["PIN"]

    assert check.detail.startswith("set from the environment")


def test_a_pin_file_that_is_not_a_pin_is_reported_as_the_dead_end_it_is(healthy):
    settings = healthy.model_copy(update={"pin": None})
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    pin_file(settings.config_dir).write_text("not-a-pin\n", encoding="utf-8")

    checks = run_doctor_checks(settings)
    check = by_name(checks)["PIN"]

    assert (check.ok, check.severity) == (False, "soft")
    assert str(pin_file(settings.config_dir)) in check.detail
    assert "no call can set one" in check.detail
    assert has_hard_failure(checks) is False


def test_an_installed_service_is_reported(healthy):
    check = by_name(run_doctor_checks(healthy))["service manager"]

    assert check.ok is True
    assert check.detail.split()[0] in {"systemd", "launchd"}


def test_a_service_manager_with_nothing_installed_is_not_a_tick(healthy, monkeypatch):
    """`systemctl` on PATH used to be enough for a ✅ naming a unit that did not exist."""
    monkeypatch.setattr("jarvis.restart.service.is_installed", lambda target: False)

    check = by_name(run_doctor_checks(healthy))["service manager"]

    assert (check.ok, check.severity) == (False, "soft")
    assert "not installed" in check.detail
    assert "install-" in check.detail  # which installer puts it there
    assert "restart_service" in check.detail


def test_nothing_supervising_the_process_is_a_warning_that_says_what_is_lost(healthy):
    """`SERVICE_MANAGER=none` is a supported way to run; the consequence is just silent."""
    settings = healthy.model_copy(update={"service_manager": "none"})

    check = by_name(run_doctor_checks(settings))["service manager"]

    assert (check.ok, check.severity) == (False, "soft")
    assert "restart_service" in check.detail


def test_no_service_manager_at_all_says_so_rather_than_naming_a_unit(healthy, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)

    check = by_name(run_doctor_checks(healthy))["service manager"]

    assert (check.ok, check.severity) == (False, "soft")
    assert check.detail.startswith("no service manager on this machine")


def test_a_missing_git_is_reported_next_to_the_service_manager(healthy, monkeypatch):
    """Version reporting degrades quietly without it, which is worth saying once."""
    monkeypatch.setattr("shutil.which", lambda name: None if name == "git" else f"/bin/{name}")

    check = by_name(run_doctor_checks(healthy))["service manager"]

    assert check.ok is True
    assert "no git on PATH" in check.detail


def test_an_unwritable_data_dir_is_a_hard_failure(healthy, tmp_path):
    settings = healthy.model_copy(update={"data_dir": Path("/dev/null/nope")})

    check = by_name(run_doctor_checks(settings))["data dir writable"]
    assert (check.ok, check.severity) == (False, "hard")


def test_a_world_readable_data_dir_only_warns(healthy):
    """A shared host is where this matters; a single-user one is not worth refusing over."""
    healthy.data_dir.chmod(0o755)

    check = by_name(run_doctor_checks(healthy))["data dir private"]

    assert (check.ok, check.severity) == (False, "soft")
    assert "0755" in check.detail
    assert "jarvis doctor --fix" in check.detail


def test_a_group_readable_data_dir_is_reported_too(healthy):
    healthy.data_dir.chmod(0o740)

    check = by_name(run_doctor_checks(healthy))["data dir private"]

    assert check.ok is False


def test_an_owner_only_data_dir_passes(healthy):
    check = by_name(run_doctor_checks(healthy))["data dir private"]

    assert (check.ok, check.detail) == (True, "0700 (owner only)")


def test_doctor_creates_a_missing_data_dir_owner_only(healthy, tmp_path):
    """The write probe creates the directory; it must not leave a loose one behind."""
    settings = healthy.model_copy(update={"data_dir": tmp_path / "never-created"})

    check = by_name(run_doctor_checks(settings))["data dir private"]

    assert (check.ok, check.detail) == (True, "0700 (owner only)")


def test_a_data_dir_that_cannot_be_read_is_reported_not_raised(healthy):
    check = _data_dir_privacy_check(healthy.model_copy(update={"data_dir": Path("/dev/null/nope")}))

    assert (check.ok, check.severity) == (False, "soft")


def test_google_is_reported_as_unused_while_workspace_mcp_is_off(healthy):
    check = by_name(run_doctor_checks(healthy))["Google for agents"]

    assert (check.ok, check.severity) == (True, "soft")
    assert "connectors" in check.detail


def test_google_credentials_only_warn_when_the_oauth_client_is_configured(healthy, tmp_path):
    healthy.google_workspace_mcp = True
    for path in (tmp_path / "jarvis" / "google").iterdir():
        path.unlink()

    checks = run_doctor_checks(healthy)
    check = by_name(checks)["Google for agents"]
    assert (check.ok, check.severity) == (False, "soft")
    assert "jarvis auth login google-workspace" in check.detail
    assert has_hard_failure(checks) is False


def test_google_is_reported_as_not_configured_without_an_oauth_client(healthy):
    settings = healthy.model_copy(
        update={
            "google_workspace_mcp": True,
            "google_oauth_client_id": None,
            "google_oauth_client_secret": None,
        }
    )

    check = by_name(run_doctor_checks(settings))["Google for agents"]
    assert (check.ok, check.severity, check.state) == (False, "soft", "failed")
    assert "no OAuth client" in check.detail


# --- what a stranger needs to know about their own install -----------------
#
# Every one of these is a warning at most. A Jarvis with no name for its owner, no memory,
# no projects root, no cluster and no Slack still works — it just knows less and offers less,
# and this is where somebody who did not write it finds out why.


def test_no_owner_name_only_warns(healthy):
    settings = healthy.model_copy(update={"owner_name": None})

    check = by_name(run_doctor_checks(settings))["owner name"]
    assert (check.ok, check.severity) == (False, "soft")
    assert "OWNER_NAME" in check.detail and "the owner" in check.detail


def test_an_owner_name_is_reported(healthy):
    check = by_name(run_doctor_checks(healthy))["owner name"]
    assert (check.ok, check.detail) == (True, "Sam")


def test_an_empty_memory_points_at_setup(healthy):
    memory_path(healthy.data_dir).unlink()

    checks = run_doctor_checks(healthy)
    check = by_name(checks)["memory"]
    assert (check.ok, check.severity) == (False, "soft")
    assert "jarvis setup" in check.detail
    assert has_hard_failure(checks) is False


def test_a_seeded_memory_says_how_much_every_call_carries(healthy):
    check = by_name(run_doctor_checks(healthy))["memory"]
    assert check.ok is True
    assert "characters" in check.detail


def test_a_missing_projects_root_only_warns_and_says_where_tasks_go(healthy):
    healthy.projects_root.rmdir()

    check = by_name(run_doctor_checks(healthy))["projects root"]
    assert (check.ok, check.severity) == (False, "soft")
    assert "PROJECTS_ROOT" in check.detail
    assert "workspace" in check.detail
    assert not healthy.projects_root.exists()  # reported, never created


def test_every_plugin_on_names_what_it_does(healthy):
    checks = by_name(run_doctor_checks(healthy))

    assert checks["send_to_slack"].detail == "on — posts to D123"
    assert checks["check_email"].detail == "on — claude-opus-5-5, low effort"
    assert checks["check_billing"].detail == "on — auto, budget $40"
    assert checks["cluster_stats"].detail == "on — alpha through the built-in guard"
    assert all(checks[name].section == "plugins" for name in plugins.PLUGINS)
    assert "xoxb-test" not in str(checks)


def test_a_plugin_that_is_off_is_optional_and_says_how(healthy):
    plugins.remove(healthy, "cluster_stats")
    checks = run_doctor_checks(healthy)
    check = by_name(checks)["cluster_stats"]

    assert (check.ok, check.state, check.severity) == (False, "missing", "soft")
    assert "jarvis plugins install cluster_stats" in check.detail
    assert format_check(check).startswith("○")
    assert has_hard_failure(checks) is False


def test_a_plugin_that_is_on_and_refused_says_why(healthy):
    plugins.config_path(healthy, "cluster_stats").write_text("[clusters]\n")

    check = by_name(run_doctor_checks(healthy))["cluster_stats"]

    assert (check.ok, check.state) == (False, "failed")
    assert "names no host" in check.detail


@pytest.mark.parametrize(
    ("name", "says"),
    [
        ("send_to_slack", "SLACK_BOT_TOKEN is set but send_to_slack is off, so PIN-lockout "
                          "alerts are not going to Slack"),
        ("check_billing", "OPENAI_ADMIN_KEY is set but check_billing is off"),
        ("check_email", "a Gmail sign-in is set but check_email is off"),
    ],
)
def test_a_secret_set_for_a_plugin_that_is_off_is_a_warning(healthy, name, says):
    plugins.remove(healthy, name)

    check = by_name(run_doctor_checks(healthy))[name]

    assert (check.ok, check.state, check.severity) == (False, "failed", "soft")
    assert says in check.detail
    assert f"jarvis plugins install {name}" in check.detail


def test_email_signed_in_without_the_claude_cli_is_refused_with_how(healthy, monkeypatch):
    with_agent(monkeypatch, "claude", cli=None)

    check = by_name(run_doctor_checks(healthy))["check_email"]

    assert (check.ok, check.severity) == (False, "soft")
    assert "uv sync --extra claude" in check.detail


def test_settings_a_plugin_replaced_point_at_the_one_command(healthy):
    ConfigStore().config_path.parent.mkdir(parents=True, exist_ok=True)
    ConfigStore().config_path.write_text('SLACK_MCP_SERVER = "chat"\n\n[CLUSTERS]\na = "b"\n')

    check = by_name(run_doctor_checks(healthy))["retired settings"]

    assert (check.ok, check.severity, check.section) == (False, "soft", "plugins")
    assert "CLUSTERS, SLACK_MCP_SERVER" in check.detail
    assert "jarvis plugins install --from-settings" in check.detail


# --- formatting ------------------------------------------------------------


def test_each_severity_gets_its_own_marker():
    assert format_check(Check("a", True, "fine")).startswith("✅")
    assert format_check(Check("b", False, "broken")).startswith("❌")
    assert format_check(Check("c", False, "meh", severity="soft")).startswith("⚠️")


def test_a_formatted_check_carries_its_name_and_detail():
    line = format_check(Check("tunnel", True, "/usr/local/bin/cloudflared"))

    assert "tunnel" in line
    assert "/usr/local/bin/cloudflared" in line


# --- where the secrets are ----------------------------------------------------


def test_a_loose_secret_file_is_named_and_fix_tightens_only_modes(healthy):
    from jarvis.config.store import ConfigStore
    from jarvis.doctor import fix_permissions

    store = ConfigStore()
    store.set({"OPENAI_API_KEY": "sk-1"})
    store.secrets_path.chmod(0o644)
    before = store.secrets_path.read_text()

    check = by_name(run_doctor_checks(healthy, store=store))[
        "secret files private"
    ]
    assert (check.ok, check.severity) == (False, "soft")
    assert str(store.secrets_path) in check.detail and "0644" in check.detail
    # After the checks: loading the email plugin tightens its token by itself.
    token_path(healthy).chmod(0o640)

    changed = fix_permissions(healthy, store)

    assert len(changed) == 2
    assert store.secrets_path.stat().st_mode & 0o777 == 0o600
    assert token_path(healthy).stat().st_mode & 0o777 == 0o600
    assert store.secrets_path.read_text() == before
    assert by_name(run_doctor_checks(healthy, store=store))[
        "secret files private"
    ].ok


def test_a_config_inside_a_git_work_tree_warns(healthy, tmp_path, monkeypatch):
    from jarvis.config.store import ConfigStore

    (tmp_path / "repo" / ".git").mkdir(parents=True)
    store = ConfigStore(tmp_path / "repo" / "jarvis-home")

    check = by_name(run_doctor_checks(healthy, store=store))["outside git"]

    assert (check.ok, check.severity) == (False, "soft")
    assert str(tmp_path / "repo") in check.detail


def test_a_relative_config_path_that_steps_out_of_a_work_tree_does_not_warn(healthy):
    """`fresh/../jh` is not inside `fresh`, even before it exists."""
    from jarvis.config.store import ConfigStore

    (Path("fresh") / ".git").mkdir(parents=True)
    store = ConfigStore(Path("fresh/../jh"))

    assert by_name(run_doctor_checks(healthy, store=store))["outside git"].ok


def test_a_secret_written_into_config_toml_by_hand_warns(healthy):
    from jarvis.config.files import dump_toml, write_private
    from jarvis.config.store import ConfigStore

    store = ConfigStore()
    write_private(store.config_path, dump_toml({"TWILIO_AUTH_TOKEN": "tok"}))

    check = by_name(run_doctor_checks(healthy, store=store))["config.toml"]

    assert (check.ok, check.severity) == (False, "soft")
    assert "TWILIO_AUTH_TOKEN" in check.detail and "tok" not in check.detail


def test_a_protected_key_unlocked_by_hand_is_reported(healthy):
    from jarvis.config.files import dump_toml, write_private
    from jarvis.config.store import ConfigStore

    store = ConfigStore()
    write_private(store.config_path, dump_toml({"service_writable": {"PORT": True}}))

    check = by_name(run_doctor_checks(healthy, store=store))[
        "protected settings"
    ]

    assert (check.ok, check.severity) == (False, "soft")
    assert "PORT" in check.detail


def test_an_imported_env_left_behind_is_reported(healthy, tmp_path, monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", str(tmp_path / ".env"))
    (tmp_path / ".env.imported-2026-09-27").write_text("OPENAI_API_KEY=sk\n")

    check = by_name(run_doctor_checks(healthy))["old .env"]

    assert (check.ok, check.severity) == (False, "soft")
    assert ".env.imported-2026-09-27" in check.detail


# --- the Twilio webhook -----------------------------------------------------------


class FakeTwilio:
    def __init__(self, voice_url=None, *, fail=False):
        from jarvis.notify.twilio_out import TwilioNumber

        self.fail = fail
        self.number = TwilioNumber("PN1", "+15550000000", voice_url, None)

    def numbers(self):
        if self.fail:
            raise RuntimeError("network down")
        return [self.number]


def test_the_webhook_is_checked_only_when_a_client_is_given(healthy):
    assert "Twilio webhook" not in by_name(run_doctor_checks(healthy))


def test_a_webhook_pointed_here_passes(healthy):
    twilio = FakeTwilio("https://jarvis.example.com/twilio/voice")

    check = by_name(run_doctor_checks(healthy, twilio=twilio))["Twilio webhook"]

    assert check.ok is True


def test_a_webhook_pointed_elsewhere_is_a_warning_that_says_setup_fixes_it(healthy):
    twilio = FakeTwilio("https://old.example.com/voice")

    check = by_name(run_doctor_checks(healthy, twilio=twilio))["Twilio webhook"]

    assert (check.ok, check.severity) == (False, "soft")
    assert "old.example.com" in check.detail and "jarvis setup" in check.detail


def test_twilio_down_is_a_warning_not_a_crash(healthy):
    check = by_name(
        run_doctor_checks(healthy, twilio=FakeTwilio(fail=True))
    )["Twilio webhook"]

    assert (check.ok, check.severity) == (False, "soft")


def test_a_number_not_on_the_account_is_a_failure(healthy):
    settings = healthy.model_copy(update={"twilio_number": "+15559999999"})

    check = by_name(
        run_doctor_checks(settings, twilio=FakeTwilio("x"))
    )["Twilio webhook"]

    assert (check.ok, check.severity) == (False, "hard")
    assert "+15559999999" not in check.detail


def test_every_check_has_a_section_and_a_state(healthy):
    for check in run_doctor_checks(healthy):
        assert check.section
        assert check.as_dict()["state"] in {"ok", "missing", "failed"}
