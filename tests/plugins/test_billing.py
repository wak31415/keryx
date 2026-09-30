"""The `check_billing` plugin: this month's bill, read-only, and so no PIN."""

from datetime import UTC, datetime

import pytest

from jarvis import plugins
from jarvis.integrations.billing import BillingError, BillingReport
from jarvis.plugins.billing import check_billing_tool, reader_for
from jarvis.trust import TrustLevel
from plugins.helpers import call, loading, names, offered, refusal, turn_on

NAME = "check_billing"


class FakeBilling:
    """A `BillingReader` that answers from a script, or raises the given `BillingError`."""

    provider = "openai"

    def __init__(self, report: BillingReport | BillingError) -> None:
        self.report = report

    async def month_to_date(self, *, now=None) -> BillingReport:
        if isinstance(self.report, BillingError):
            raise self.report
        return self.report


def billing_factory(answer):
    """A factory recording which provider the model asked for."""
    asked: list[str | None] = []

    def factory(provider: str | None):
        asked.append(provider)
        return FakeBilling(answer)

    factory.asked = asked  # type: ignore[attr-defined]
    return factory


def a_report(**overrides) -> BillingReport:
    defaults = dict(
        provider="openai",
        currency="USD",
        spend=31.0,
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, tzinfo=UTC),
        as_of=datetime(2026, 8, 11, tzinfo=UTC),
        usage={"input_tokens": 5, "requests": 2},
    )
    return BillingReport(**{**defaults, **overrides})


def offer(settings, factory, **values):
    path = plugins.write_config(settings, NAME, values)
    with loading(settings):
        return offered(check_billing_tool(path, factory=factory), settings)


async def test_it_hands_back_the_month_with_a_sentence_to_say(settings):
    result = await call(offer(settings, billing_factory(a_report())), NAME)

    assert result["status"] == "ok"
    assert result["spend_to_date"] == 31.0
    assert result["estimate"] is True
    assert "OpenAI so far this month" in result["spoken"]


async def test_the_model_may_name_the_provider_and_the_default_is_none(settings):
    factory = billing_factory(a_report())
    registry = offer(settings, factory)

    await call(registry, NAME)
    await call(registry, NAME, {"provider": "Anthropic"})

    assert factory.asked == [None, "anthropic"]


@pytest.mark.parametrize("code", ["not_configured", "auth", "rate_limited", "unavailable"])
async def test_every_failure_is_a_status_with_a_sentence(settings, code):
    """Never a raised exception, and never the credential a 401 body can quote back."""
    registry = offer(settings, billing_factory(BillingError(code, "HTTP 401 sk-admin-SECRET")))

    result = await call(registry, NAME)

    assert result["status"] == code
    assert result["message"]
    assert "sk-admin" not in str(result)


async def test_it_needs_no_pin_because_it_only_reads(settings):
    registry = offer(settings, billing_factory(a_report()))

    assert (await call(registry, NAME, trust=TrustLevel.NONE))["status"] == "ok"


def test_the_schema_offers_exactly_the_two_providers(settings):
    schema = offer(settings, billing_factory(a_report())).schemas()[0]

    assert schema["parameters"]["properties"]["provider"]["enum"] == ["openai", "anthropic"]
    assert schema["parameters"]["required"] == []
    assert "No PIN needed" in schema["description"]


# --- the real reader: its file, and the keys the store holds now -----------------------


def test_the_reader_takes_the_default_provider_budget_and_scope_from_its_file(settings):
    settings = settings.model_copy(update={"anthropic_admin_key": "sk-ant-admin01-x"})
    path = plugins.write_config(settings, NAME, {
        "provider": "anthropic", "monthly_budget": "250", "anthropic_workspace_id": "wrk_1",
    })

    reader = reader_for(settings, path, None)

    assert reader.provider == "anthropic"
    assert (reader._api_key, reader._workspace_id, reader._budget) == (
        "sk-ant-admin01-x", "wrk_1", 250.0
    )


def test_the_caller_can_ask_for_the_other_provider(settings):
    settings = settings.model_copy(update={"openai_admin_key": "sk-admin-x"})
    path = plugins.write_config(settings, NAME, {"provider": "anthropic",
                                                 "openai_project_id": "proj_9"})

    reader = reader_for(settings, path, "openai")

    assert (reader.provider, reader._project_id, reader._budget) == ("openai", "proj_9", None)


def test_an_admin_key_saved_since_startup_is_used_without_a_restart(settings, monkeypatch):
    monkeypatch.setattr(plugins, "current_settings",
                        lambda: settings.model_copy(update={"openai_admin_key": "sk-admin-new"}))
    path = plugins.write_config(settings, NAME, {})

    assert reader_for(settings, path, None)._api_key == "sk-admin-new"


def test_a_file_that_went_bad_is_a_sentence_not_a_crash(settings):
    path = plugins.write_config(settings, NAME, {})
    path.write_text('provider = "aws"\n')

    with pytest.raises(BillingError) as caught:
        reader_for(settings, path, None)

    assert caught.value.code == "not_configured"


# --- installed and loaded ----------------------------------------------------------------


def test_installed_it_loads_from_its_template_with_nothing_contacted(settings):
    turn_on(settings, NAME, monthly_budget=40)

    assert NAME in names(settings)


async def test_installed_with_no_key_at_all_it_says_so(settings, monkeypatch):
    monkeypatch.setattr(plugins, "current_settings", lambda: settings)
    settings = settings.model_copy(update={"openai_api_key": "unset"})
    turn_on(settings, NAME)
    from jarvis.tools import ToolRegistry
    from jarvis.tools.custom import register_custom_tools

    registry = ToolRegistry()
    register_custom_tools(registry, settings)

    result = await call(registry, NAME)

    assert result["status"] == "not_configured"


def test_a_budget_that_is_not_a_number_is_refused(settings):
    turn_on(settings, NAME)
    plugins.config_path(settings, NAME).write_text('monthly_budget = "lots"\n')

    assert "monthly_budget must be a number" in refusal(settings, NAME)
