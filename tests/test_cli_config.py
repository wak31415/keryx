"""`jarvis config`, `jarvis auth`, `jarvis setup` and `jarvis doctor --json`: the command line
a coding agent sets Jarvis up with. The store is the test's own `JARVIS_HOME`."""

import json
import stat

import pytest
from typer.testing import CliRunner

from jarvis.cli import app
from jarvis.config.files import read_toml
from jarvis.config.store import ConfigStore

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch, every_agent_installed):
    """Settings from the store alone, with the data directory kept in the scratch space."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path / "projects"))
    return ConfigStore()


def run(*args, input=None, env=None):
    return runner.invoke(app, list(args), input=input, env=env)


# --- config set / get / unset --------------------------------------------------------------


def test_set_takes_several_pairs_and_get_reads_them_back(home):
    result = run("config", "set", "OPENAI_VOICE", "marin", "port", "9000")

    assert result.exit_code == 0, result.output
    assert "OPENAI_VOICE saved to config.toml" in result.output
    assert run("config", "get", "OPENAI_VOICE", "PORT").output == "marin\n9000\n"


def test_get_shell_prints_lines_a_shell_can_eval(home):
    run("config", "set", "PROJECTS", '{"a b": "/x y"}')

    output = run("config", "get", "--shell", "PROJECTS", "PUBLIC_HOST").output

    assert output == "PROJECTS='{\"a b\": \"/x y\"}'\nPUBLIC_HOST=''\n"


def test_a_secret_on_the_command_line_is_refused_and_not_written(home):
    result = run("config", "set", "OPENAI_API_KEY", "sk-leak")

    assert result.exit_code == 2
    assert "never on the command line" in result.output
    assert "sk-leak" not in result.output
    assert not home.secrets_path.exists()


def test_a_secret_goes_in_on_stdin(home):
    result = run("config", "set", "OPENAI_API_KEY", "--stdin", input="sk-live\n")

    assert result.exit_code == 0, result.output
    assert read_toml(home.secrets_path) == {"OPENAI_API_KEY": "sk-live"}
    assert stat.S_IMODE(home.secrets_path.stat().st_mode) == 0o600
    assert "sk-live" not in result.output


def test_a_secret_goes_in_from_a_variable(home, monkeypatch):
    monkeypatch.setenv("MY_TWILIO_TOKEN", "tok-1")

    result = run("config", "set", "TWILIO_AUTH_TOKEN", "--from-env", "MY_TWILIO_TOKEN")

    assert result.exit_code == 0
    assert read_toml(home.secrets_path) == {"TWILIO_AUTH_TOKEN": "tok-1"}


def test_from_env_with_nothing_in_the_variable_is_a_wrong_command_line(home):
    assert run("config", "set", "OPENAI_API_KEY", "--from-env", "NOPE_NOT_SET").exit_code == 2


def test_stdin_takes_exactly_one_key(home):
    assert run("config", "set", "A", "B", "--stdin", input="x").exit_code == 2


def test_an_odd_number_of_words_is_a_wrong_command_line(home):
    assert run("config", "set", "OPENAI_VOICE").exit_code == 2


def test_an_invalid_value_is_refused_with_the_reason(home):
    result = run("config", "set", "PORT", "eighty")

    assert result.exit_code == 1
    assert "PORT" in result.output
    assert not home.config_path.exists()


def test_an_unknown_key_is_a_wrong_command_line(home):
    assert run("config", "set", "NOPE", "1").exit_code == 2
    assert run("config", "get", "NOPE").exit_code == 2


def test_get_refuses_a_secret(home):
    run("config", "set", "OPENAI_API_KEY", "--stdin", input="sk-live")

    result = run("config", "get", "OPENAI_API_KEY")

    assert result.exit_code == 1
    assert "sk-live" not in result.output and "never printed" in result.output


def test_the_pin_is_never_a_config_setting(home):
    result = run("config", "set", "JARVIS_PIN", "--stdin", input="482915")

    assert result.exit_code == 1
    assert "jarvis setup" in result.output


def test_unset_returns_a_key_to_its_default(home):
    run("config", "set", "OPENAI_VOICE", "marin")

    result = run("config", "unset", "OPENAI_VOICE")

    assert result.exit_code == 0
    assert "OPENAI_VOICE unset" in result.output
    assert run("config", "get", "OPENAI_VOICE").output == "cedar\n"


def test_the_environment_winning_is_said(home, monkeypatch):
    monkeypatch.setenv("OPENAI_VOICE", "ash")

    result = run("config", "set", "OPENAI_VOICE", "marin")

    assert "also set in the environment, which wins" in result.output


# --- config list -------------------------------------------------------------------------


def test_list_json_carries_everything_and_never_a_secret(home):
    run("config", "set", "OPENAI_API_KEY", "--stdin", input="sk-live")
    run("config", "set", "OPENAI_VOICE", "marin")

    result = run("config", "list", "--json")

    assert result.exit_code == 0
    rows = {row["key"]: row for row in json.loads(result.output)}
    assert "sk-live" not in result.output
    key = rows["OPENAI_API_KEY"]
    assert (key["set"], key["secret"], key["value"], key["source"]) == (
        True, True, None, "secrets.toml"
    )
    assert key["required"] is True and key["protected"] is True
    voice = rows["OPENAI_VOICE"]
    assert (voice["value"], voice["source"], voice["service_writable"]) == (
        "marin", "config.toml", True
    )
    assert rows["PORT"]["set"] is False and rows["PORT"]["group"] == "phone"


def test_list_by_group_as_a_table(home):
    run("config", "set", "OPENAI_API_KEY", "--stdin", input="sk-live")

    result = run("config", "list", "--group", "voice")

    assert result.exit_code == 0
    assert "(secret, set)" in result.output and "sk-live" not in result.output
    assert "TWILIO" not in result.output
    assert run("config", "list", "--group", "nope").exit_code == 2


# --- lock / unlock / path / import-env -------------------------------------------------------


def test_lock_and_unlock(home):
    assert run("config", "lock", "openai_voice").exit_code == 0
    assert run("config", "unlock", "PROJECTS_ROOT").exit_code == 0

    assert home.overrides() == {"OPENAI_VOICE": False, "PROJECTS_ROOT": True}


def test_unlock_is_refused_on_a_protected_key(home):
    result = run("config", "unlock", "ALLOWED_CALLERS")

    assert result.exit_code == 1 and "protected" in result.output
    assert home.overrides() == {}


def test_inside_a_task_set_is_the_service_and_lock_is_refused(home, monkeypatch):
    monkeypatch.setenv("JARVIS_ACTOR", "service")

    allowed = run("config", "set", "OPENAI_VOICE", "marin")
    refused = run("config", "set", "PUBLIC_HOST", "evil.example.com")
    unlocking = run("config", "unlock", "PROJECTS_ROOT")

    assert allowed.exit_code == 0 and "takes effect after a restart" in allowed.output
    assert refused.exit_code == 1 and "protected" in refused.output
    assert unlocking.exit_code == 1 and "only the owner" in unlocking.output
    assert "PUBLIC_HOST" not in home.stored()


def test_path_names_all_three(home):
    output = run("config", "path").output

    assert str(home.home) in output and "config.toml" in output and "secrets.toml" in output


def test_import_env_moves_a_legacy_file(home, tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-old\nOPENAI_VOICE=marin\nWHO=me\n")

    result = run("config", "import-env")

    assert result.exit_code == 0, result.output
    assert "imported: OPENAI_API_KEY, OPENAI_VOICE" in result.output
    assert "not settings, not imported: WHO" in result.output
    assert not (tmp_path / ".env").exists()
    assert home.stored()["OPENAI_VOICE"] == "marin"


def test_import_env_that_is_refused_exits_1(home, tmp_path):
    (tmp_path / ".env").write_text("PORT=eighty\n")

    assert run("config", "import-env").exit_code == 1
    assert (tmp_path / ".env").exists()


# --- doctor --json / --fix --------------------------------------------------------------------


def test_doctor_json_is_one_document_with_sections_and_states(home):
    result = run("doctor", "--json", "--no-mic")

    document = json.loads(result.output)
    assert result.exit_code == (0 if document["ok"] else 1)
    voice = next(check for check in document["checks"] if check["name"] == "OPENAI_API_KEY")
    assert (voice["state"], voice["section"]) == ("missing", "voice")


def test_doctor_fix_tightens_a_loose_secrets_file(home):
    run("config", "set", "OPENAI_API_KEY", "--stdin", input="sk-live")
    home.secrets_path.chmod(0o644)

    result = run("doctor", "--fix", "--no-mic")

    assert f"fix: {home.secrets_path}: 0644 → 0600" in result.output
    assert stat.S_IMODE(home.secrets_path.stat().st_mode) == 0o600


# --- auth ---------------------------------------------------------------------------------------


def test_auth_status_json_lists_every_sign_in(home):
    result = run("auth", "status", "--json")

    report = json.loads(result.output)
    assert set(report) == {"openai", "claude", "codex", "gmail", "google-workspace", "twilio"}
    assert report["openai"]["state"] == "missing"
    assert report["gmail"]["state"] == "missing"
    assert report["codex"]["enabled"] is False


def test_auth_login_knows_only_what_it_can_sign_in_to(home):
    assert run("auth", "login", "twitter").exit_code == 2


def test_auth_login_gmail_prints_a_link_then_finishes_with_the_callback(
    home, tmp_path, monkeypatch
):
    client = tmp_path / "client.json"
    client.write_text('{"installed": {"client_id": "cid", "client_secret": "cs"}}')

    first = run("auth", "login", "gmail", "--client-file", str(client))

    assert first.exit_code == 0, first.output
    assert "accounts.google.com" in first.output and "--callback-url" in first.output
    assert home.stored()["GOOGLE_CLIENT_SECRETS_FILE"].endswith("google_client_secret.json")

    state = first.output.split("state=")[1].split("&")[0]

    def post(url, data, timeout):
        return type("R", (), {"status_code": 200, "json": lambda self: {"refresh_token": "r"}})()

    monkeypatch.setattr("jarvis.setup.google.httpx.post", post)
    second = run(
        "auth", "login", "gmail", "--callback-url",
        f"http://localhost:1/?state={state}&code=c&scope=https://www.googleapis.com/auth/gmail.readonly",
    )

    assert second.exit_code == 0, second.output
    assert "signed in" in second.output
    assert (tmp_path / "jarvis" / "gmail_token.json").is_file()


def test_auth_login_gmail_without_a_client_says_how_to_get_one(home):
    result = run("auth", "login", "gmail")

    assert result.exit_code == 1
    assert "--client-file" in result.output


def test_auth_login_of_an_agent_that_is_not_installed_says_how(home, monkeypatch):
    monkeypatch.setattr("jarvis.agents.registry.installed", lambda agent: False)

    result = run("auth", "login", "codex")

    assert result.exit_code == 1 and "uv sync --extra codex" in result.output


def test_auth_login_runs_the_agents_own_login(home, monkeypatch):
    ran = []
    monkeypatch.setattr("jarvis.cli.run_command", lambda argv: ran.append(argv) or 0)

    result = run("auth", "login", "codex", "--browser")

    assert result.exit_code == 0, result.output
    assert ran == [["/venv/bin/codex", "login"]]


# --- setup ----------------------------------------------------------------------------------------


def test_setup_needs_a_terminal_and_says_what_an_agent_should_run(home):
    result = run("setup")

    assert result.exit_code == 2
    assert "--agent-instructions" in result.output


def test_setup_agent_instructions_print_without_asking(home):
    result = run("setup", "--agent-instructions")

    assert result.exit_code == 0
    assert "jarvis config list --json" in result.output
    assert str(home.home) in result.output


# --- the rest of the wiring ------------------------------------------------------------------


def test_doctor_asks_twilio_only_with_credentials_and_a_host(home, monkeypatch):
    from jarvis import cli

    monkeypatch.undo()  # the suite's stub of `_twilio_admin` included
    settings = cli._load_settings_optional()
    assert cli._twilio_admin(settings) is None

    configured = settings.model_copy(
        update={"twilio_account_sid": "AC1", "twilio_auth_token": "t", "public_host": "h.io"}
    )
    assert cli._twilio_admin(configured) is not None


def test_doctor_fix_with_nothing_loose_says_so(home):
    result = run("doctor", "--fix", "--no-mic")

    assert "fix: nothing to tighten" in result.output


def test_setup_runs_the_wizard_on_a_terminal(home, monkeypatch):
    from jarvis import cli

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("jarvis.setup.ui.RichPrompter", lambda: object())
    reviews = []
    monkeypatch.setattr(cli, "run_wizard", lambda ctx, review_all: reviews.append(review_all) or 1)

    result = run("setup", "--all")

    assert reviews == [True]
    assert result.exit_code == 1


def test_ctrl_c_in_setup_says_what_was_kept(home, monkeypatch):
    from jarvis import cli
    from jarvis.setup.ui import Aborted

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("jarvis.setup.ui.RichPrompter", lambda: object())

    def stop(ctx, review_all):
        raise Aborted

    monkeypatch.setattr(cli, "run_wizard", stop)

    result = run("setup")

    assert result.exit_code == 130
    assert "What was saved stays saved" in result.output


def test_a_secret_typed_at_a_terminal_is_asked_for_hidden(home, monkeypatch):
    from jarvis import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    asked = []
    monkeypatch.setattr(
        cli.typer, "prompt", lambda key, hide_input: asked.append(hide_input) or " sk-typed "
    )

    assert cli._read_secret_input("OPENAI_API_KEY") == "sk-typed"
    assert asked == [True]


def test_import_env_says_where_the_pin_and_client_went(home, tmp_path):
    data_dir = tmp_path / "jarvis"
    (tmp_path / ".env").write_text(f"JARVIS_PIN=482915\nDATA_DIR={data_dir}\nPORT=8080\n")
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "client_secret.json").write_text(
        '{"installed": {"client_id": "i", "client_secret": "s"}}'
    )

    result = run("config", "import-env")

    assert "PIN: moved to DATA_DIR/pin" in result.output
    assert "Google client file:" in result.output
    assert "left at their defaults: PORT" in result.output


def test_lock_of_a_key_that_is_not_there_is_refused(home):
    assert run("config", "lock", "JARVIS_PIN").exit_code == 1


def test_auth_status_prints_a_table_and_exits_1_on_a_failure(home, monkeypatch):
    monkeypatch.setattr(
        "jarvis.setup.auth.status",
        lambda settings, store, smoke: {"claude": {"state": "failed", "detail": "no key"}},
    )

    result = run("auth", "status")

    assert result.exit_code == 1
    assert "claude  failed    no key" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ("config", "import-env"),
        ("auth", "login", "google-workspace"),
        ("memory", "seed", "--file", "-", "--force"),
        ("setup",),
    ],
)
def test_inside_a_task_the_owners_commands_are_refused(home, tmp_path, monkeypatch, args):
    (tmp_path / ".env").write_text("ALLOWED_CALLERS=+15550000000\n")
    monkeypatch.setenv("JARVIS_ACTOR", "service")

    result = run(*args, input="A fact.\n")

    assert result.exit_code == 1
    assert "only the owner" in result.output
    assert home.stored() == {}
    assert (tmp_path / ".env").exists()


def test_config_get_refuses_a_config_that_does_not_parse(home):
    home.home.mkdir(parents=True, exist_ok=True)
    home.config_path.write_text("PORT = \n")

    result = run("config", "get", "PORT")

    assert result.exit_code == 1 and "does not parse" in result.output


def test_serve_refuses_a_config_that_does_not_parse_in_one_line(home):
    home.home.mkdir(parents=True, exist_ok=True)
    home.config_path.write_text("PORT = \n")

    result = run("serve", "--no-phone", "--no-wakeword")

    assert result.exit_code == 2
    assert "jarvis cannot start" in result.output and "Traceback" not in result.output


def test_doctor_reports_a_config_that_does_not_parse_rather_than_crashing(home):
    home.home.mkdir(parents=True, exist_ok=True)
    home.config_path.write_text("PORT = \n")

    result = run("doctor", "--json", "--no-mic")

    document = json.loads(result.output)
    check = next(c for c in document["checks"] if c["name"] == "config.toml")
    assert (check["state"], check["severity"]) == ("failed", "hard")
    assert result.exit_code == 1
