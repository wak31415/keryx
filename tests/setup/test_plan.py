"""`keryx setup plan`: the steps the Mac app's Setup Assistant walks, decided from the same
doctor checks and wizard statuses `keryx setup` reads — so the two cannot drift."""

import dataclasses

import pytest
from wording import for_the_app

from keryx.agents.registry import BACKENDS
from keryx.config import write_enrolled_pin
from keryx.config.store import ConfigStore
from keryx.doctor import Check, _webhook_check, run_doctor_checks
from keryx.fixes import is_valid
from keryx.notify.twilio_out import TwilioError
from keryx.setup import plan as setup_plan
from keryx.setup.plan import (
    BLOCKED,
    DONE,
    FAILED,
    NEEDED,
    OPTIONAL,
    STEP_IDS,
    build_plan,
    format_plan,
    mark_step,
)
from keryx.setup.wizard import DONE as WIZARD_DONE
from keryx.setup.wizard import from_checks

REQUIRED = ["voice", "agents", "owner", "pin", "phone", "service"]
OPTIONALS = ["local", "google", "plugins", "issues", "profile", "projects"]
#: The required steps the owner may leave for later: a Mac that never gets a phone.
DEFERRABLE = {"phone", "service"}


def _signed_in(monkeypatch, ready: bool) -> None:
    spec = BACKENDS["claude"]
    monkeypatch.setitem(
        BACKENDS,
        "claude",
        dataclasses.replace(spec, auth=dataclasses.replace(spec.auth, stored_login=lambda: ready)),
    )


@pytest.fixture
def signed_out(monkeypatch):
    _signed_in(monkeypatch, False)
    monkeypatch.setattr("shutil.which", lambda name: None)


@pytest.fixture
def signed_in(monkeypatch):
    _signed_in(monkeypatch, True)
    monkeypatch.setattr("shutil.which", lambda name: None)


@pytest.fixture
def plan_of(make_ctx):
    """The plan for the scratch machine as it is now, as `keryx setup plan` builds it."""

    def make(*, active: bool = True, extra: list[Check] | None = None,
             first: list[Check] | None = None) -> dict:
        ctx = make_ctx([])
        checks = (first or []) + run_doctor_checks(ctx.settings, store=ctx.store) + (extra or [])
        return build_plan(ctx.settings, ctx.store, checks, service_active=lambda _: active)

    return make


def steps(plan: dict) -> dict[str, dict]:
    return {step["id"]: step for step in plan["steps"]}


def states(plan: dict) -> dict[str, str]:
    return {step["id"]: step["state"] for step in plan["steps"]}


@pytest.fixture
def set_up(make_ctx, monkeypatch, signed_in):
    """A machine moved to the XDG directories with every key the first call needs."""
    ConfigStore().set({
        "OPENAI_API_KEY": "sk-test",
        "OWNER_NAME": "Ada",
        "ALLOWED_CALLERS": "+15551234567",
        "TWILIO_ACCOUNT_SID": "AC1",
        "TWILIO_AUTH_TOKEN": "token",
        "TWILIO_NUMBER": "+15557654321",
        "PUBLIC_HOST": "keryx.example.com",
    })
    write_enrolled_pin(make_ctx([]).settings.config_dir, "482915")
    monkeypatch.setattr("shutil.which", lambda name: f"/opt/bin/{name}"
                        if name == "cloudflared" else None)
    monkeypatch.setattr(
        "keryx.doctor.service_manager_check",
        lambda settings: Check("service manager", True, "launchd dev.keryx.agent",
                               severity="soft", section="service"),
    )


def test_a_fresh_machine_needs_every_required_step(plan_of, signed_out):
    plan = plan_of()

    assert plan["schema"] == 1 and plan["ready"] is False
    assert [step["id"] for step in plan["steps"]] == REQUIRED + OPTIONALS
    assert states(plan) == {
        "voice": NEEDED, "agents": NEEDED, "owner": NEEDED, "pin": NEEDED,
        "phone": BLOCKED, "service": BLOCKED,
        **{key: OPTIONAL for key in OPTIONALS},
        "google": DONE,  # doctor's word: Claude's own connectors carry it
    }
    found = steps(plan)
    assert [s["required"] for s in plan["steps"]] == [True] * 6 + [False] * 6
    assert found["voice"]["why"] == "OpenAI API key: not set."
    assert found["voice"]["checks"] == [
        {"title": "OpenAI API key", "state": "missing",
         "detail": "not set — the voice session cannot start", "fix": "secret:OPENAI_API_KEY"}
    ]
    assert found["voice"]["fix"] == "secret:OPENAI_API_KEY"
    assert not any(step["walked"] for step in plan["steps"])


def test_every_step_has_the_shape_the_app_decodes(plan_of, signed_out):
    for step in plan_of()["steps"]:
        assert set(step) == {"id", "title", "required", "state", "why", "blocked_by",
                             "walked", "deferrable", "deferred", "fix", "checks"}
        assert step["deferrable"] is (step["id"] in DEFERRABLE)
        assert step["deferred"] is False
        assert step["title"] and isinstance(step["blocked_by"], list)
        assert (step["why"] is None) == (step["state"] == DONE)
        for check in step["checks"]:
            assert set(check) == {"title", "state", "detail", "fix"}


def test_a_set_up_machine_is_ready(plan_of, set_up):
    plan = plan_of()

    assert {key: states(plan)[key] for key in REQUIRED} == dict.fromkeys(REQUIRED, DONE)
    assert plan["ready"] is True
    assert steps(plan)["service"]["checks"][-1] == {
        "title": "Service running", "state": "ok", "detail": "running", "fix": None
    }


def test_a_blank_owner_name_holds_neither_the_owner_nor_ready(plan_of, set_up):
    ConfigStore().unset(["OWNER_NAME"])  # the wizard's "the owner"

    plan = plan_of()

    owner = steps(plan)["owner"]
    assert (owner["state"], owner["why"], plan["ready"]) == (DONE, None, True)
    # Still said, so the checklist can offer it: it just holds nothing back.
    assert {"title": "Owner name", "state": "missing"}.items() <= next(
        check for check in owner["checks"] if check["title"] == "Owner name").items()


def test_a_service_installed_and_stopped_failed(plan_of, set_up):
    plan = plan_of(active=False)

    service = steps(plan)["service"]
    assert service["state"] == FAILED and plan["ready"] is False
    assert service["why"] == "Service running: installed, but not running."
    assert service["fix"] == service["checks"][-1]["fix"] == "service:install"


def test_a_pin_left_to_the_first_call_is_done(plan_of, signed_out):
    assert steps(plan_of())["pin"]["state"] == NEEDED

    ConfigStore().mark_walked("pin")

    pin = steps(plan_of())["pin"]
    assert (pin["state"], pin["walked"], pin["why"]) == (DONE, True, None)


def test_the_wizards_owner_section_answers_the_pin_question_too(plan_of, signed_out):
    ConfigStore().mark_walked("owner")  # "Owner and PIN": the first call was let set it

    assert steps(plan_of())["pin"]["state"] == DONE


def test_a_required_step_walked_is_still_needed(plan_of, signed_out):
    ConfigStore().mark_walked("voice")

    voice = steps(plan_of())["voice"]
    assert (voice["state"], voice["walked"]) == (NEEDED, True)


def test_an_optional_step_walked_counts_as_done(plan_of, signed_out):
    for key in OPTIONALS:
        ConfigStore().mark_walked(key)

    found = states(plan_of())
    assert {key: found[key] for key in OPTIONALS} == dict.fromkeys(OPTIONALS, DONE)


def test_phone_waits_for_the_owner_and_the_service_for_three(plan_of, signed_out):
    found = steps(plan_of())

    assert found["phone"]["blocked_by"] == ["owner"]
    assert found["service"]["blocked_by"] == ["phone", "voice", "agents"]
    assert found["phone"]["why"] == "Waits for Owner."
    assert found["service"]["why"] == "Waits for Voice, Coding agents and Phone."
    assert all(found[key]["blocked_by"] == [] for key in ("voice", "agents", "owner", "pin"))


def test_blocked_gives_way_once_the_blockers_are_done(plan_of, signed_in):
    ConfigStore().set({"OWNER_NAME": "Ada", "ALLOWED_CALLERS": "+15551234567"})

    found = steps(plan_of())

    assert found["owner"]["state"] == DONE
    assert found["phone"]["state"] == NEEDED  # missing Twilio, the host and a tunnel
    assert found["phone"]["why"].startswith("Twilio credentials: missing TWILIO_ACCOUNT_SID")
    assert found["service"]["state"] == BLOCKED
    assert found["service"]["why"] == "Waits for Voice and Phone."


def test_a_check_that_failed_fails_its_step(plan_of, signed_out):
    ConfigStore().set({"AGENTS_ENABLED": "claude", "AGENT_BACKEND": "codex"})

    agents = steps(plan_of())["agents"]

    assert agents["state"] == FAILED
    assert agents["why"].startswith("Coding agents: AGENT_BACKEND is codex")


def test_failed_shows_through_blocked(plan_of, signed_out):
    """Something configured and broken is said, even before its turn."""
    webhook = Check("Twilio webhook", False, "calls go to https://old.example/twilio/voice, "
                    "not https://keryx.example.com/twilio/voice — `keryx setup` can point it "
                    "here", severity="soft", section="phone")

    phone = steps(plan_of(extra=[webhook]))["phone"]

    assert (phone["state"], phone["why"]) == (FAILED, "Twilio webhook: points elsewhere.")
    assert phone["checks"][-1]["detail"] == webhook.detail  # doctor's own words, untouched


def test_a_status_callback_elsewhere_is_said_as_pointing_elsewhere(plan_of, signed_out):
    webhook = Check("Twilio webhook", False, "call status goes to https://old.example/s, not "
                    "https://keryx.example.com/twilio/status — `keryx setup` can point it here",
                    severity="soft", section="phone")

    phone = steps(plan_of(extra=[webhook]))["phone"]

    assert (phone["state"], phone["why"]) == (FAILED, "Twilio webhook: points elsewhere.")


def test_soft_checks_of_another_agent_or_web_search_do_not_hold_a_step(plan_of, signed_in):
    ConfigStore().set({"AGENTS_ENABLED": "claude,codex", "VOICE_BASE_URL":
                       "http://127.0.0.1:9000/v1"})

    found = steps(plan_of())

    assert found["agents"]["state"] == DONE  # Codex is not signed in, and not the default
    assert found["voice"]["checks"][-1]["state"] == "missing"  # no key for web search
    assert found["voice"]["state"] == DONE


def test_a_local_voice_waits_for_the_local_models(plan_of, signed_in):
    ConfigStore().set({"VOICE_BASE_URL": "http://192.168.1.20:8765/v1"})
    down = Check("voice server", False, "could not reach 192.168.1.20", section="local")

    found = steps(plan_of(extra=[down]))

    assert found["voice"]["blocked_by"] == ["local"]
    assert found["local"]["required"] is True
    assert (found["local"]["state"], found["voice"]["state"]) == (FAILED, BLOCKED)
    assert found["voice"]["why"] == "Waits for Local models."
    assert found["service"]["why"] == "Waits for Voice and Phone."  # a chain waits too


@pytest.mark.parametrize("url", ["http://192.168.1.20:8765/v1", "https://voice.example.com/v1"])
def test_a_warning_about_the_voice_server_holds_nothing_back(plan_of, set_up, url):
    """Unencrypted on the LAN, or public with no key: doctor warns, and calls still work."""
    ConfigStore().set({"VOICE_BASE_URL": url})

    plan = plan_of()
    found = steps(plan)

    assert any(c["state"] == "failed" for c in found["local"]["checks"])  # still said
    assert (found["local"]["state"], found["voice"]["state"]) == (DONE, DONE)
    assert plan["ready"] is True


def test_a_non_default_local_agent_that_is_down_holds_nothing_back(plan_of, set_up):
    ConfigStore().set({"VOICE_BASE_URL": "http://127.0.0.1:9000/v1",
                       "AGENTS_ENABLED": "claude,local",
                       "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1"})
    down = Check("local agent server", False, "no answer", severity="soft", section="local")

    plan = plan_of(extra=[down])

    assert steps(plan)["voice"]["state"] == DONE
    assert plan["ready"] is True


class Unanswering:
    """A Twilio account whose every read fails with `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def numbers(self):
        raise self.error


@pytest.mark.parametrize("error", [
    TwilioError("x", cause="ConnectError"), TwilioError("x", status=503), TimeoutError("x"),
])
def test_twilio_not_answering_holds_neither_the_phone_nor_ready(plan_of, set_up, make_ctx,
                                                               error):
    """Could not ask is not an answer: a mismatch blocks, a moment's outage does not."""
    down = _webhook_check(make_ctx([]).settings, Unanswering(error))
    assert down.unknown and not down.ok

    plan = plan_of(extra=[down])

    phone = steps(plan)["phone"]
    assert (phone["state"], phone["why"]) == (DONE, None)
    assert plan["ready"] is True
    assert phone["checks"][-1]["state"] == "failed"  # still said, as a warning is


def test_twilio_refusing_the_credentials_holds_the_phone_back(plan_of, set_up, make_ctx):
    refused = _webhook_check(make_ctx([]).settings, Unanswering(TwilioError("x", status=401)))
    assert not refused.unknown

    plan = plan_of(extra=[refused])

    phone = steps(plan)["phone"]
    assert phone["state"] == FAILED and plan["ready"] is False
    assert phone["why"] == "Twilio webhook: Twilio didn't accept the account SID and auth token."


def test_a_check_that_could_not_be_asked_leaves_a_sections_status_alone():
    unknown = Check("Twilio webhook", False, "could not ask Twilio", severity="soft",
                    section="phone", unknown=True)
    ok = Check("PUBLIC_HOST", True, "keryx.example.com", section="phone")

    assert from_checks([ok, unknown]) == WIZARD_DONE


def test_a_hard_failure_no_step_owns_is_the_services_and_not_ready(plan_of, set_up):
    broken = Check("data dir writable", False, "the folder is read-only",
                   section="security", hint="/data: read-only file system")

    plan = plan_of(extra=[broken])

    service = steps(plan)["service"]
    assert plan["ready"] is False
    assert (service["state"], service["why"]) == (
        FAILED, "Data folder writable: the folder is read-only."
    )
    assert service["checks"][-1]["title"] == "Data folder writable"


def test_no_hard_failure_coexists_with_ready(plan_of, set_up):
    for section in ("security", "google", "plugins", "settings", "elsewhere"):
        broken = Check("something", False, "broken", section=section)
        assert plan_of(extra=[broken])["ready"] is False, section


def test_why_names_the_check_that_holds_the_step_back(plan_of, signed_out):
    """Not a soft one the step's state passed over: here the default agent, not Codex."""
    other = Check("Codex agent", False, "Codex CLI not found", severity="soft",
                  section="agents")

    agents = steps(plan_of(first=[other]))["agents"]

    assert agents["state"] == NEEDED
    assert agents["why"].startswith("Claude Code agent (default): ")


def test_a_required_local_step_is_not_done_by_walking_it(plan_of, signed_in):
    ConfigStore().set({"AGENT_BACKEND": "local"})
    ConfigStore().mark_walked("local")

    found = steps(plan_of())

    assert found["local"]["required"] is True
    assert found["local"]["state"] in (NEEDED, FAILED)


def test_a_local_default_agent_waits_for_the_local_models(plan_of, signed_in):
    ConfigStore().set({"AGENT_BACKEND": "local"})

    found = steps(plan_of())

    assert found["agents"]["blocked_by"] == ["local"]
    assert found["local"]["required"] is True


def test_a_step_whose_checks_pass_still_waits_for_a_blocker(plan_of, signed_in):
    """Blocked beats done: agents' own checks pass, but the local agent's server failed."""
    ConfigStore().set({"AGENT_BACKEND": "local",
                       "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1"})
    broken = Check("model server", False, "llama-server is not installed — `keryx setup` "
                   "installs it", section="local")

    found = steps(plan_of(extra=[broken]))

    assert all(check["state"] == "ok" for check in found["agents"]["checks"])
    assert found["local"]["state"] == FAILED
    assert (found["agents"]["state"], found["agents"]["why"]) == (
        BLOCKED, "Waits for Local models."
    )


def test_the_migration_comes_first_and_holds_everything_back(plan_of, signed_out, tmp_path):
    (tmp_path / ".env").write_text("OWNER_NAME=Ada\n")

    plan = plan_of()

    migrate = plan["steps"][0]
    assert (migrate["id"], migrate["required"], migrate["state"]) == ("migrate", True, NEEDED)
    assert ".env" in migrate["why"]
    assert all("migrate" in step["blocked_by"] for step in plan["steps"][1:])
    assert {step["state"] for step in plan["steps"][1:]} <= {BLOCKED, FAILED}
    assert steps(plan)["voice"]["why"] == "Waits for Migration."
    assert migrate["fix"] == "migrate" and steps(plan)["voice"]["fix"] is None


def test_the_checklist_marks_each_step(plan_of, signed_out):
    lines = format_plan(plan_of())

    assert lines[0] == "Not ready: 6 required steps left before the first call."
    assert "○  Voice — OpenAI API key: not set." in lines
    assert "…  Phone — Waits for Owner." in lines
    assert any(line.startswith("·  Plugins (optional) — ") for line in lines)
    assert "✓  Google (optional)" in lines


def test_a_ready_checklist_says_so(plan_of, set_up):
    assert format_plan(plan_of())[0] == "Ready for a first call."


def test_mark_records_the_step_as_walked():
    assert mark_step(ConfigStore(), "google") == {"id": "google", "walked": True,
                                                  "deferred": False}
    assert "google" in ConfigStore().walked_sections()


@pytest.mark.parametrize("step", sorted(DEFERRABLE))
def test_mark_leaves_a_deferrable_step_for_later(step):
    assert mark_step(ConfigStore(), step) == {"id": step, "walked": True, "deferred": True}
    assert step in ConfigStore().walked_sections()


@pytest.mark.parametrize("step", ["migrate", "voice", "agents", "owner"])
def test_mark_refuses_a_required_step(step):
    with pytest.raises(ValueError, match="is required; finish it in the app"):
        mark_step(ConfigStore(), step)

    assert ConfigStore().walked_sections() == []


def test_the_pin_is_the_one_required_step_that_may_be_marked(plan_of, signed_out):
    assert mark_step(ConfigStore(), "pin") == {"id": "pin", "walked": True, "deferred": False}
    assert steps(plan_of())["pin"]["state"] == DONE


@pytest.fixture
def no_phone(set_up):
    """Everything set up but the phone: no Twilio account yet."""
    ConfigStore().unset(["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMBER"])


def test_a_phone_left_for_later_is_deferred_and_still_not_ready(plan_of, no_phone):
    before = steps(plan_of())
    assert (before["phone"]["state"], before["phone"]["deferred"]) == (NEEDED, False)
    assert before["service"]["state"] == BLOCKED

    mark_step(ConfigStore(), "phone")
    plan = plan_of()

    phone, service = steps(plan)["phone"], steps(plan)["service"]
    # Still needed, and why: "Nearly Ready" — the checklist says "set up later" instead.
    assert (phone["state"], phone["deferred"], phone["walked"]) == (NEEDED, True, True)
    assert phone["why"].startswith("Twilio credentials: ")
    assert plan["ready"] is False
    # What waits for it does not: everything but the phone can be finished.
    assert (service["state"], service["deferred"], service["blocked_by"]) == (
        DONE, False, ["phone", "voice", "agents"])


def test_a_service_left_for_later_is_deferred(plan_of, set_up):
    not_installed = Check("service unit", False, "not installed", section="service",
                          unset=True)
    assert steps(plan_of(first=[not_installed]))["service"]["deferred"] is False

    mark_step(ConfigStore(), "service")
    plan = plan_of(first=[not_installed])

    service = steps(plan)["service"]
    assert (service["state"], service["deferred"], plan["ready"]) == (NEEDED, True, False)


def test_a_deferred_step_that_fails_is_failed_not_deferred(plan_of, set_up):
    mark_step(ConfigStore(), "phone")
    elsewhere = Check("Twilio webhook", False, "calls go to https://old.example.com/voice, "
                      "not https://keryx.example.com/twilio/voice", severity="soft",
                      section="phone", fix="phone:webhooks")

    phone = steps(plan_of(extra=[elsewhere]))["phone"]

    assert (phone["state"], phone["deferred"]) == (FAILED, False)


def test_a_deferred_step_once_done_is_done(plan_of, set_up):
    mark_step(ConfigStore(), "phone")

    plan = plan_of()

    assert (steps(plan)["phone"]["state"], steps(plan)["phone"]["deferred"]) == (DONE, False)
    assert plan["ready"] is True


def test_the_wizards_phone_section_walked_and_left_is_deferred_too(plan_of, no_phone):
    ConfigStore().mark_walked("phone")  # `keryx setup` walked it, and the owner said later

    assert steps(plan_of())["phone"]["deferred"] is True


def test_a_checklist_with_only_deferred_steps_left_is_nearly_ready(plan_of, no_phone):
    mark_step(ConfigStore(), "phone")

    lines = format_plan(plan_of())

    assert lines[0] == "Nearly ready: Phone is left for later."
    assert any(line.startswith("○  Phone (set up later) — ") for line in lines)


def test_an_unreadable_walked_list_is_an_empty_one(make_ctx, signed_out):
    ctx = make_ctx([])
    ctx.store.mark_walked("google")
    settings = ctx.settings  # read while the file still parsed
    ctx.store.config_path.write_text("this = = is not toml\n")

    plan = build_plan(settings, ctx.store, run_doctor_checks(settings, store=ctx.store),
                      service_active=lambda _: True)

    assert not any(step["walked"] for step in plan["steps"]) and plan["ready"] is False


def test_a_plan_without_settings_fails_every_step_with_why():
    plan = setup_plan.failed_plan("PORT Input should be a valid integer")

    assert plan["schema"] == 1 and plan["ready"] is False
    assert [step["id"] for step in plan["steps"]] == [*REQUIRED, *OPTIONALS]
    for step in plan["steps"]:
        assert step["state"] == FAILED
        assert step["why"] == "Settings: PORT Input should be a valid integer."
        assert step["fix"] is None
        assert step["checks"] == [{"title": "Settings", "state": "failed",
                                   "detail": "PORT Input should be a valid integer",
                                   "fix": None}]


def test_mark_refuses_a_step_that_is_not_one():
    with pytest.raises(ValueError, match="no setup step called nope"):
        mark_step(ConfigStore(), "nope")


def test_the_step_ids_are_the_contract():
    assert STEP_IDS == ("migrate", *REQUIRED, *OPTIONALS)
    assert setup_plan.SCHEMA == 1


def test_the_service_is_asked_from_outside_whether_it_runs(monkeypatch, make_ctx):
    from keryx.restart.service import ServiceTarget

    target = ServiceTarget("launchd", "dev.keryx.agent")
    asked = []
    monkeypatch.setattr("keryx.restart.service.resolve_target",
                        lambda settings, from_outside: asked.append(from_outside) or target)
    monkeypatch.setattr("keryx.restart.service.is_active", lambda found: found is target)

    assert setup_plan._service_active(make_ctx([]).settings) is True
    assert asked == [True]

    monkeypatch.setattr("keryx.restart.service.resolve_target", lambda settings, **_: None)
    assert setup_plan._service_active(make_ctx([]).settings) is False


#: Details with a command, a path or a traceback in them, as a check made before doctor
#: moved them to `hint` had: `why` turns each into a phrase the app can show.
COMMAND_DETAILS = [
    ("model server", "llama-server is not installed — `keryx setup` installs it"),
    ("Codex agent", "not installed — uv sync --extra codex"),
    ("service manager", "dev.keryx.agent is not installed (scripts/install-launchd.sh) — "
     "`keryx restart` and the voice's restart_service refuse"),
    ("send_to_slack", "off (optional) — `keryx setup` → Plugins, or `keryx plugins install "
     "send_to_slack`"),
    ("Claude Code agent", "/venv/bin/claude (2.1.0); no credential — `keryx auth login claude`"),
    ("tunnel", "run `brew install cloudflared`"),
    ("voice server program", "uv tool install speech-to-speech"),
    ("storage", "/home/you/.env is configuration Keryx no longer reads — run `keryx migrate`"),
    ("Claude Code agent", "no login found; run claude and /login once"),
    ("voice server", "refused the session: Traceback (most recent call last)"),
    ("data dir writable", "the folder at /data is read-only"),
]


@pytest.mark.parametrize(("name", "detail"), COMMAND_DETAILS)
def test_why_never_names_a_command(plan_of, signed_out, name, detail):
    bad = Check(name, False, detail, section="voice")

    voice = steps(plan_of(extra=[bad]))["voice"]

    assert voice["state"] == FAILED
    assert voice["why"].endswith(".") and ": " in voice["why"]
    assert for_the_app(voice["why"]), voice["why"]
    assert detail in [check["detail"] for check in voice["checks"]]


def test_no_why_on_a_machine_with_everything_missing_names_a_command(
    plan_of, signed_out, tmp_path
):
    plans = [plan_of(active=False)]
    (tmp_path / ".env").write_text("OWNER_NAME=Ada\n")
    plans.append(plan_of())

    whys = [step["why"] for plan in plans for step in plan["steps"] if step["why"]]

    assert whys and not [why for why in whys if not for_the_app(why)]


def _plans(plan_of, tmp_path, monkeypatch) -> list[dict]:
    """The plan on a fresh machine, with the service stopped, part-way set up, with a
    migration to run, and without settings at all."""
    found = [plan_of(active=False)]
    ConfigStore().set({"OWNER_NAME": "Ada", "OPENAI_API_KEY": "sk-test",
                       "PUBLIC_HOST": "https://not-a-host.example.com",
                       "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t",
                       "TWILIO_NUMBER": "+15557654321", "ISSUE_REPORTING": "true"})
    found.append(plan_of())
    (tmp_path / ".env").write_text("OWNER_NAME=Ada\n")
    found.append(plan_of())
    found.append(setup_plan.failed_plan("PORT Input should be a valid integer"))
    return found


def test_no_why_detail_or_fix_the_app_reads_is_a_terminals(plan_of, signed_out, tmp_path,
                                                          monkeypatch):
    for plan in _plans(plan_of, tmp_path, monkeypatch):
        for step in plan["steps"]:
            assert step["why"] is None or for_the_app(step["why"]), step
            assert is_valid(step["fix"]), step
            for check in step["checks"]:
                assert for_the_app(check["detail"]), check
                assert is_valid(check["fix"]), check
                assert check["fix"] is None or check["state"] != "ok", check


def test_a_steps_fix_is_its_first_problems(plan_of, signed_out, tmp_path, monkeypatch):
    """The fix of the check `why` names — the first failed, else the first missing — and
    none for a step that is done, or waits for another."""
    for plan in _plans(plan_of, tmp_path, monkeypatch)[:-1]:
        for step in plan["steps"]:
            if step["state"] in (DONE, BLOCKED):
                assert step["fix"] is None, step
                continue
            want = "failed" if step["state"] == FAILED else "missing"
            first = next((c for c in step["checks"] if c["state"] == want), None)
            if first is not None and step["why"].startswith(first["title"] + ":"):
                assert step["fix"] == first["fix"], step
            elif step["why"] is not None and ":" not in step["why"]:
                assert step["fix"] is None, step  # the step's own offer: no check behind it


def test_the_steps_the_app_can_mend_say_how(plan_of, signed_out):
    found = steps(plan_of())

    assert {key: found[key]["fix"] for key in ("voice", "agents", "owner", "pin")} == {
        "voice": "secret:OPENAI_API_KEY", "agents": "signin:claude",
        "owner": "setting:ALLOWED_CALLERS", "pin": "pin",
    }
