"""Tests for the month-to-date billing read. No network: the transport is injected.

Two things get most of the attention here, because both are the kind of bug you only
notice once it has been read out loud as money: Anthropic's amounts arriving in *cents*,
and the failure paths, which must come back as a sentence rather than an exception and
must never carry a credential.
"""

import logging
import urllib.error
from datetime import UTC, datetime

import pytest

from jarvis.billing import (
    ANTHROPIC_COST_URL,
    ANTHROPIC_USAGE_URL,
    OPENAI_COSTS_URL,
    OPENAI_USAGE_URL,
    AnthropicBilling,
    BillingError,
    BillingReport,
    OpenAIBilling,
    build_billing_reader,
    classify,
    fetch_pages,
    month_bounds,
    redact,
)
from jarvis.config import Settings

ADMIN_KEY = "sk-admin-0123456789abcdefSECRET"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


# --- harness ---------------------------------------------------------------


class FakeApi:
    """A recording stand-in for `_get`, answering per URL. Every call is a GET."""

    def __init__(self, **bodies: object) -> None:
        self.bodies = bodies
        self.calls: list[dict] = []

    def __call__(self, url: str, params: dict, headers: dict) -> dict:
        self.calls.append({"url": url, "params": params, "headers": headers})
        for key, body in self.bodies.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected URL {url}")

    def params_for(self, url: str) -> dict:
        return next(call["params"] for call in self.calls if call["url"] == url)


def openai_costs(*values: float) -> dict:
    return {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": 1754006400,
                "end_time": 1754092800,
                "results": [
                    {
                        "object": "organization.costs.result",
                        "amount": {"value": value, "currency": "usd"},
                        "line_item": f"gpt-realtime-2.1, {'input' if index else 'output'}",
                        "project_id": None,
                    }
                ],
            }
            for index, value in enumerate(values)
        ],
        "has_more": False,
        "next_page": None,
    }


def openai_usage(**overrides: int) -> dict:
    result = {
        "object": "organization.usage.completions.result",
        "input_tokens": 1000,
        "output_tokens": 200,
        "input_cached_tokens": 50,
        "input_audio_tokens": 7000,
        "output_audio_tokens": 3000,
        "num_model_requests": 12,
        **overrides,
    }
    return {"object": "page", "data": [{"results": [result]}], "next_page": None}


def anthropic_costs(*amounts: str) -> dict:
    return {
        "data": [
            {
                "starting_at": "2026-08-01T00:00:00Z",
                "ending_at": "2026-08-02T00:00:00Z",
                "results": [
                    {
                        "amount": amount,
                        "currency": "USD",
                        "cost_type": "tokens",
                        "description": "Claude Opus 5 Usage - Input Tokens",
                    }
                    for amount in amounts
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }


def settings(**overrides: object) -> Settings:
    return Settings(**{"openai_api_key": "sk-proj-abcdefghijkl", **overrides})


# --- the period ------------------------------------------------------------


def test_the_period_is_the_utc_calendar_month():
    start, end = month_bounds(NOW)

    assert start == datetime(2026, 8, 1, tzinfo=UTC)
    assert end == datetime(2026, 9, 1, tzinfo=UTC)


def test_a_february_and_a_leap_february_both_end_on_the_first():
    assert month_bounds(datetime(2026, 2, 14, tzinfo=UTC))[1] == datetime(2026, 3, 1, tzinfo=UTC)
    assert month_bounds(datetime(2028, 2, 14, tzinfo=UTC))[1] == datetime(2028, 3, 1, tzinfo=UTC)


def test_a_local_time_is_converted_before_the_month_is_taken():
    """A call at half past midnight Berlin time on the first is still July in UTC."""
    from datetime import timedelta, timezone

    berlin = datetime(2026, 8, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))

    assert month_bounds(berlin)[0] == datetime(2026, 7, 1, tzinfo=UTC)


# --- redaction -------------------------------------------------------------


def test_a_key_is_logged_as_a_stub_that_is_not_the_key():
    stub = redact(ADMIN_KEY)

    assert stub == "sk-admin…CRET"
    assert ADMIN_KEY not in stub
    assert "0123456789" not in stub


def test_an_absent_or_tiny_credential_reveals_nothing():
    assert redact(None) == "<unset>"
    assert redact("") == "<unset>"
    assert redact("sk-short") == "<redacted>"


# --- OpenAI ----------------------------------------------------------------


async def test_openai_sums_the_month_and_reports_the_period():
    api = FakeApi(costs=openai_costs(1.25, 2.50), completions=openai_usage())

    report = await OpenAIBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert report.provider == "openai"
    assert report.spend == pytest.approx(3.75)
    assert report.currency == "USD"
    assert report.period_start == datetime(2026, 8, 1, tzinfo=UTC)
    assert report.period_end == datetime(2026, 9, 1, tzinfo=UTC)
    assert report.as_of == NOW
    assert report.scope == "organization"


async def test_openai_asks_only_for_this_month_by_the_day():
    api = FakeApi(costs=openai_costs(1.0), completions=openai_usage())

    await OpenAIBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    params = api.params_for(OPENAI_COSTS_URL)
    assert params["start_time"] == int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
    assert params["end_time"] == int(NOW.timestamp())
    assert params["bucket_width"] == "1d"
    assert params["group_by"] == ["line_item"]


async def test_openai_authenticates_with_a_bearer_and_nothing_else_leaks():
    api = FakeApi(costs=openai_costs(1.0), completions=openai_usage())

    await OpenAIBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert api.calls[0]["headers"]["Authorization"] == f"Bearer {ADMIN_KEY}"


async def test_openai_reports_the_tokens_behind_the_spend():
    api = FakeApi(costs=openai_costs(1.0), completions=openai_usage())

    report = await OpenAIBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert report.usage["input_tokens"] == 1000
    assert report.usage["output_audio_tokens"] == 3000
    assert report.usage["requests"] == 12
    assert "num_model_requests" not in report.usage


async def test_a_project_narrows_both_calls_and_is_named_in_the_scope():
    api = FakeApi(costs=openai_costs(1.0), completions=openai_usage())

    report = await OpenAIBilling(ADMIN_KEY, project_id="proj_42", get=api).month_to_date(now=NOW)

    assert api.params_for(OPENAI_COSTS_URL)["project_ids"] == ["proj_42"]
    assert api.params_for(OPENAI_USAGE_URL)["project_ids"] == ["proj_42"]
    assert report.scope == "project:proj_42"


async def test_a_key_id_narrows_usage_only_because_costs_cannot_be_narrowed():
    """The costs endpoint has no per-key filter; claiming one would be a lie in dollars."""
    api = FakeApi(costs=openai_costs(1.0), completions=openai_usage())

    await OpenAIBilling(ADMIN_KEY, api_key_id="key_abc", get=api).month_to_date(now=NOW)

    assert api.params_for(OPENAI_USAGE_URL)["api_key_ids"] == ["key_abc"]
    assert "api_key_ids" not in api.params_for(OPENAI_COSTS_URL)


async def test_the_biggest_cost_lines_come_back_ranked():
    api = FakeApi(costs=openai_costs(1.0, 9.0), completions=openai_usage())

    report = await OpenAIBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert [item["amount"] for item in report.top_line_items] == [9.0, 1.0]


async def test_a_bare_list_of_buckets_is_read_the_same_as_a_page():
    api = FakeApi(costs=openai_costs(2.0)["data"], completions=openai_usage())

    report = await OpenAIBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert report.spend == pytest.approx(2.0)


async def test_a_usage_outage_still_reports_the_spend():
    """Spend is the headline; losing the token counts must not lose the number."""
    api = FakeApi(costs=openai_costs(4.0), completions=OSError("no route to host"))

    report = await OpenAIBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert report.spend == pytest.approx(4.0)
    assert report.usage == {}


async def test_an_empty_month_is_zero_not_an_error():
    api = FakeApi(costs={"data": [], "next_page": None}, completions={"data": []})

    report = await OpenAIBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert report.spend == 0.0
    assert report.projected == 0.0


# --- Anthropic -------------------------------------------------------------


async def test_anthropic_amounts_are_cents_and_are_converted():
    """`"123.45"` USD is one dollar twenty-three. Getting this wrong is a 100x error."""
    api = FakeApi(cost_report=anthropic_costs("123.45"), usage_report={"data": []})

    report = await AnthropicBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert report.spend == pytest.approx(1.2345)
    assert report.currency == "USD"


async def test_anthropic_sums_every_line_in_every_bucket():
    api = FakeApi(cost_report=anthropic_costs("100", "250.5"), usage_report={"data": []})

    report = await AnthropicBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert report.spend == pytest.approx(3.505)


async def test_an_unreadable_amount_is_skipped_rather_than_crashing_the_call():
    api = FakeApi(
        cost_report={"data": [{"results": [{"amount": "n/a"}, {"amount": "500"}]}]},
        usage_report={"data": []},
    )

    report = await AnthropicBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert report.spend == pytest.approx(5.0)


async def test_anthropic_asks_in_rfc3339_and_sends_the_version_header():
    api = FakeApi(cost_report=anthropic_costs("0"), usage_report={"data": []})

    await AnthropicBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    params = api.params_for(ANTHROPIC_COST_URL)
    assert params["starting_at"] == "2026-08-01T00:00:00Z"
    assert params["ending_at"] == "2026-08-26T12:00:00Z"
    assert api.calls[0]["headers"]["anthropic-version"] == "2023-06-01"


async def test_an_admin_key_authenticates_with_x_api_key_and_a_token_with_a_bearer():
    api = FakeApi(cost_report=anthropic_costs("0"), usage_report={"data": []})

    await AnthropicBilling("sk-ant-admin01-abcdefgh", get=api).month_to_date(now=NOW)
    assert api.calls[0]["headers"]["x-api-key"] == "sk-ant-admin01-abcdefgh"
    assert "Authorization" not in api.calls[0]["headers"]

    api.calls.clear()
    await AnthropicBilling("oauth-token-abcdefghijkl", get=api).month_to_date(now=NOW)
    assert api.calls[0]["headers"]["Authorization"] == "Bearer oauth-token-abcdefghijkl"


async def test_anthropic_folds_cache_writes_into_input_tokens():
    api = FakeApi(
        cost_report=anthropic_costs("0"),
        usage_report={
            "data": [
                {
                    "results": [
                        {
                            "uncached_input_tokens": 1500,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 1000,
                                "ephemeral_5m_input_tokens": 500,
                            },
                            "cache_read_input_tokens": 200,
                            "output_tokens": 500,
                            "server_tool_use": {"web_search_requests": 10},
                        }
                    ]
                }
            ]
        },
    )

    report = await AnthropicBilling(ADMIN_KEY, workspace_id="wrkspc_1", get=api).month_to_date(
        now=NOW
    )

    assert api.params_for(ANTHROPIC_USAGE_URL)["workspace_ids"] == ["wrkspc_1"]
    assert report.scope == "workspace:wrkspc_1"
    assert report.usage["input_tokens"] == 3000
    assert report.usage["input_cached_tokens"] == 200
    assert report.usage["output_tokens"] == 500
    assert report.usage["web_search_requests"] == 10


# --- pagination, retries and failures --------------------------------------


async def test_pagination_follows_the_cursor_until_it_runs_out():
    pages = [
        {"data": [{"results": []}], "next_page": "cursor2"},
        {"data": [{"results": []}], "next_page": None},
    ]
    seen: list[str | None] = []

    def get(url: str, params: dict, headers: dict) -> dict:
        seen.append(params.get("page"))
        return pages[len(seen) - 1]

    buckets = await fetch_pages("https://example.test/x", {}, {}, get=get)

    assert seen == [None, "cursor2"]
    assert len(buckets) == 2


async def test_pagination_stops_at_the_page_cap_rather_than_looping_forever():
    def get(url: str, params: dict, headers: dict) -> dict:
        return {"data": [{"results": []}], "next_page": "always"}

    buckets = await fetch_pages("https://example.test/x", {}, {}, get=get, max_pages=3)

    assert len(buckets) == 3


async def test_a_rate_limit_is_retried_once_and_then_reported():
    slept: list[float] = []
    attempts = 0

    def get(url: str, params: dict, headers: dict) -> dict:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    with pytest.raises(BillingError) as caught:
        await fetch_pages("https://example.test/x", {}, {}, get=get, sleep=sleep)

    assert caught.value.code == "rate_limited"
    assert attempts == 2
    assert slept == [1.0]


async def test_a_retry_that_succeeds_is_not_an_error():
    attempts = 0

    def get(url: str, params: dict, headers: dict) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        return {"data": [{"results": []}], "next_page": None}

    async def sleep(seconds: float) -> None:
        return None

    assert len(await fetch_pages("https://x.test/y", {}, {}, get=get, sleep=sleep)) == 1


async def test_an_auth_failure_is_not_retried():
    attempts = 0

    def get(url: str, params: dict, headers: dict) -> dict:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    with pytest.raises(BillingError) as caught:
        await fetch_pages("https://example.test/x", {}, {}, get=get)

    assert caught.value.code == "auth"
    assert attempts == 1


def test_every_transport_failure_maps_to_something_sayable():
    assert classify(urllib.error.HTTPError("u", 403, "", {}, None)).code == "auth"
    assert classify(urllib.error.HTTPError("u", 429, "", {}, None)).code == "rate_limited"
    assert classify(urllib.error.HTTPError("u", 500, "", {}, None)).code == "unavailable"
    assert classify(urllib.error.URLError("dns")).code == "unavailable"
    assert classify(TimeoutError()).code == "unavailable"
    # Every code has a sentence: the tool hands `spoken` straight to the model.
    for status in (401, 403, 429, 500, 502):
        assert classify(urllib.error.HTTPError("u", status, "", {}, None)).spoken


def test_a_failure_detail_never_carries_the_response_body():
    """A provider's 401 body can quote the credential back; only the status is kept."""
    body = urllib.error.HTTPError("u", 401, f"invalid key {ADMIN_KEY}", {}, None)

    error = classify(body)

    assert error.detail == "HTTP 401"
    assert ADMIN_KEY not in error.detail
    assert ADMIN_KEY not in error.spoken


async def test_nothing_is_logged_that_contains_the_key(caplog):
    api = FakeApi(costs=openai_costs(1.0), completions=openai_usage())

    with caplog.at_level(logging.DEBUG, logger="jarvis.billing"):
        await OpenAIBilling(ADMIN_KEY, get=api).month_to_date(now=NOW)

    assert caplog.text
    assert ADMIN_KEY not in caplog.text
    assert "sk-admin…CRET" in caplog.text


# --- the report ------------------------------------------------------------


def make_report(**overrides) -> BillingReport:
    defaults = dict(
        provider="openai",
        currency="USD",
        spend=31.0,
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, tzinfo=UTC),
        as_of=datetime(2026, 8, 11, tzinfo=UTC),
    )
    return BillingReport(**{**defaults, **overrides})


def test_the_projection_is_the_run_rate_to_the_end_of_the_month():
    """31 dollars over the first ten days of a 31-day month projects to about 96."""
    assert make_report().projected == pytest.approx(96.1, abs=0.1)


def test_a_projection_in_the_first_minutes_of_a_month_stays_a_sane_number():
    """No dividing by zero, and no "on track for two and a half thousand" at 00:30."""
    report = make_report(as_of=datetime(2026, 8, 1, 0, 30, tzinfo=UTC))

    assert report.projected == pytest.approx(31.0 * 31)  # a day's floor, not half an hour


def test_the_floor_stops_applying_once_a_real_day_has_passed():
    report = make_report(as_of=datetime(2026, 8, 3, tzinfo=UTC))

    assert report.projected == pytest.approx(31.0 / 2 * 31)


def test_a_budget_turns_into_a_percentage_and_no_budget_into_none():
    assert make_report(budget=100.0).budget_used_percent == 31.0
    assert make_report().budget_used_percent is None
    assert make_report(budget=0.0).budget_used_percent is None


def test_the_spoken_line_names_the_provider_the_figure_and_the_projection():
    spoken = make_report(budget=100.0).spoken()

    assert spoken.startswith("OpenAI so far this month: 31.00 USD")
    assert "96" in spoken
    assert "31 percent of the budget" in spoken
    assert spoken.endswith(".")


def test_the_payload_is_json_shaped_and_labels_itself_an_estimate():
    payload = make_report(usage={"input_tokens": 5}).as_dict()

    assert payload["provider"] == "openai"
    assert payload["spend_to_date"] == 31.0
    assert payload["estimate"] is True
    assert payload["period_start"] == "2026-08-01T00:00:00+00:00"
    assert payload["as_of"] == "2026-08-11T00:00:00+00:00"
    assert payload["usage"] == {"input_tokens": 5}
    assert "monthly_budget" not in payload

    import json

    json.dumps(payload)  # the tool result has to survive serialisation


def test_the_payload_never_carries_a_credential():
    payload = make_report().as_dict()

    assert not any(ADMIN_KEY in str(value) for value in payload.values())
    assert not any("key" in name and "id" not in name for name in payload)


# --- provider selection ----------------------------------------------------


def test_auto_is_openai_because_that_is_the_key_this_call_runs_on():
    reader = build_billing_reader(settings(openai_admin_key=ADMIN_KEY))

    assert reader.provider == "openai"


def test_the_default_provider_is_configurable():
    reader = build_billing_reader(
        settings(billing_provider="anthropic", anthropic_admin_key="sk-ant-admin01-x")
    )

    assert reader.provider == "anthropic"


def test_the_caller_can_override_the_configured_default():
    config = settings(billing_provider="anthropic", openai_admin_key=ADMIN_KEY)

    assert build_billing_reader(config, "openai").provider == "openai"


def test_the_admin_key_wins_over_the_ordinary_one():
    reader = build_billing_reader(settings(openai_admin_key=ADMIN_KEY))

    assert reader._api_key == ADMIN_KEY


def test_with_no_admin_key_the_ordinary_key_is_tried_rather_than_refusing():
    """A clear 401 from the provider beats "not configured" when we have not looked."""
    reader = build_billing_reader(settings())

    assert reader._api_key == "sk-proj-abcdefghijkl"


def test_a_missing_anthropic_credential_names_the_setting_and_no_value():
    with pytest.raises(BillingError) as caught:
        build_billing_reader(settings(billing_provider="anthropic"))

    assert caught.value.code == "not_configured"
    assert caught.value.detail == "ANTHROPIC_ADMIN_KEY is unset"


def test_a_placeholder_openai_key_is_not_a_credential():
    with pytest.raises(BillingError) as caught:
        build_billing_reader(settings(openai_api_key="unset"))

    assert caught.value.code == "not_configured"


def test_a_provider_nobody_has_heard_of_is_refused_not_guessed():
    with pytest.raises(BillingError) as caught:
        build_billing_reader(settings(openai_admin_key=ADMIN_KEY), "aws")

    assert caught.value.code == "not_configured"


def test_the_budget_and_scoping_settings_reach_the_reader():
    reader = build_billing_reader(
        settings(
            openai_admin_key=ADMIN_KEY,
            openai_billing_project_id="proj_9",
            openai_billing_api_key_id="key_9",
            billing_monthly_budget=250.0,
        )
    )

    assert reader._project_id == "proj_9"
    assert reader._api_key_id == "key_9"
    assert reader._budget == 250.0
