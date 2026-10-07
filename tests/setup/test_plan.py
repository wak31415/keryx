"""`keryx setup plan`: the steps the Mac app's Setup Assistant walks, decided from the same
doctor checks and wizard statuses `keryx setup` reads — so the two cannot drift."""

import dataclasses

import pytest

from keryx.agents.registry import BACKENDS
from keryx.config import write_enrolled_pin
from keryx.config.store import ConfigStore
from keryx.doctor import Check, run_doctor_checks
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

REQUIRED = ["voice", "agents", "owner", "pin", "phone", "service"]
OPTIONALS = ["local", "google", "plugins", "issues", "profile", "projects"]


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

    def make(*, active: bool = True, extra: list[Check] | None = None) -> dict:
        ctx = make_ctx([])
        checks = run_doctor_checks(ctx.settings, store=ctx.store) + (extra or [])
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
    assert found["voice"]["why"] == "OpenAI API key: not set — the voice session cannot start."
    assert found["voice"]["checks"] == [
        {"title": "OpenAI API key", "state": "missing",
         "detail": "not set — the voice session cannot start"}
    ]
    assert not any(step["walked"] for step in plan["steps"])


def test_every_step_has_the_shape_the_app_decodes(plan_of, signed_out):
    for step in plan_of()["steps"]:
        assert set(step) == {"id", "title", "required", "state", "why", "blocked_by",
                             "walked", "checks"}
        assert step["title"] and isinstance(step["blocked_by"], list)
        assert (step["why"] is None) == (step["state"] == DONE)
        for check in step["checks"]:
            assert set(check) == {"title", "state", "detail"}


def test_a_set_up_machine_is_ready(plan_of, set_up):
    plan = plan_of()

    assert {key: states(plan)[key] for key in REQUIRED} == dict.fromkeys(REQUIRED, DONE)
    assert plan["ready"] is True
    assert steps(plan)["service"]["checks"][-1] == {
        "title": "Service running", "state": "ok", "detail": "running"
    }


def test_a_service_installed_and_stopped_failed(plan_of, set_up):
    plan = plan_of(active=False)

    service = steps(plan)["service"]
    assert service["state"] == FAILED and plan["ready"] is False
    assert service["why"] == "Service running: installed, but not running."


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
    assert found["phone"]["why"] == "Finish Owner first."
    assert found["service"]["why"] == "Finish Voice, Coding agents and Phone first."
    assert all(found[key]["blocked_by"] == [] for key in ("voice", "agents", "owner", "pin"))


def test_blocked_gives_way_once_the_blockers_are_done(plan_of, signed_in):
    ConfigStore().set({"OWNER_NAME": "Ada", "ALLOWED_CALLERS": "+15551234567"})

    found = steps(plan_of())

    assert found["owner"]["state"] == DONE
    assert found["phone"]["state"] == NEEDED  # missing Twilio, the host and a tunnel
    assert found["phone"]["why"].startswith("Twilio credentials: missing TWILIO_ACCOUNT_SID")
    assert found["service"]["state"] == BLOCKED
    assert found["service"]["why"] == "Finish Voice and Phone first."


def test_a_check_that_failed_fails_its_step(plan_of, signed_out):
    ConfigStore().set({"AGENTS_ENABLED": "claude", "AGENT_BACKEND": "codex"})

    agents = steps(plan_of())["agents"]

    assert agents["state"] == FAILED
    assert agents["why"].startswith("Coding agents: AGENT_BACKEND is codex")


def test_failed_shows_through_blocked(plan_of, signed_out):
    """Something configured and broken is said, even before its turn."""
    webhook = Check("Twilio webhook", False, "calls go to elsewhere", severity="soft",
                    section="phone")

    phone = steps(plan_of(extra=[webhook]))["phone"]

    assert (phone["state"], phone["why"]) == (FAILED, "Twilio webhook: calls go to elsewhere.")


def test_soft_checks_of_another_agent_or_web_search_do_not_hold_a_step(plan_of, signed_in):
    ConfigStore().set({"AGENTS_ENABLED": "claude,codex", "VOICE_BASE_URL":
                       "http://127.0.0.1:9000/v1"})

    found = steps(plan_of())

    assert found["agents"]["state"] == DONE  # Codex is not signed in, and not the default
    assert found["voice"]["checks"][-1]["state"] == "missing"  # no key for web search
    assert found["voice"]["state"] == DONE


def test_a_local_voice_waits_for_the_local_models(plan_of, signed_in):
    ConfigStore().set({"VOICE_BASE_URL": "http://192.168.1.20:8765/v1"})  # unencrypted

    found = steps(plan_of())

    assert found["voice"]["blocked_by"] == ["local"]
    assert found["local"]["required"] is True
    # Done is what the voice's own checks say; the local step carries what is wrong.
    assert (found["local"]["state"], found["voice"]["state"]) == (FAILED, DONE)
    assert plan_of()["ready"] is False


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


def test_the_migration_comes_first_and_holds_everything_back(plan_of, signed_out, tmp_path):
    (tmp_path / ".env").write_text("OWNER_NAME=Ada\n")

    plan = plan_of()

    migrate = plan["steps"][0]
    assert (migrate["id"], migrate["required"], migrate["state"]) == ("migrate", True, NEEDED)
    assert ".env" in migrate["why"]
    assert all("migrate" in step["blocked_by"] for step in plan["steps"][1:])
    found = states(plan)
    assert (found["voice"], found["owner"], found["phone"]) == (BLOCKED, BLOCKED, BLOCKED)
    assert steps(plan)["voice"]["why"] == "Finish Migration first."


def test_the_checklist_marks_each_step(plan_of, signed_out):
    lines = format_plan(plan_of())

    assert lines[0] == "Not ready: 6 required steps left before the first call."
    assert "○  Voice — OpenAI API key: not set — the voice session cannot start." in lines
    assert "…  Phone — Finish Owner first." in lines
    assert any(line.startswith("·  Plugins (optional) — ") for line in lines)
    assert "✓  Google (optional)" in lines


def test_a_ready_checklist_says_so(plan_of, set_up):
    assert format_plan(plan_of())[0] == "Ready for a first call."


def test_mark_records_the_step_as_walked():
    assert mark_step(ConfigStore(), "google") == {"id": "google", "walked": True}
    assert "google" in ConfigStore().walked_sections()


def test_marking_the_migration_marks_the_wizards_import():
    mark_step(ConfigStore(), "migrate")

    assert ConfigStore().walked_sections() == ["import"]


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
