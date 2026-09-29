"""What the month has cost so far, read back to the voice model.

Strictly read-only: every request this module makes is a `GET`, and `_get` refuses to
build anything else. Nothing here can change a plan, a limit or a key.

Two providers, because Jarvis spends on two accounts. **OpenAI is the default** — it is
the key the voice agent itself runs on, the one paying for the call in progress —
and Anthropic is what the subagents cost. The `check_billing` plugin's `provider` picks
between them (`auto` → OpenAI); the voice tool can also ask for one by name.

Both providers' month-to-date figures come from an *admin*-scoped credential, which is
not the key the voice agent talks to the model with:

- OpenAI wants an Admin key (`sk-admin-…`, from the organization's Admin keys page) on
  `/v1/organization/costs`. `OPENAI_ADMIN_KEY` supplies it; with none set we still try
  `OPENAI_API_KEY`, because trying and reporting a clear 401 beats refusing to look.
- Anthropic wants an Admin key (`sk-ant-admin…`) on `/v1/organizations/cost_report`,
  from `ANTHROPIC_ADMIN_KEY`, falling back the same way to `ANTHROPIC_API_KEY`.

The number is an estimate and is labelled one. Neither provider serves the invoice; both
serve accrued cost for the period, which is what the console's own dashboard shows.

`BillingReader` is the seam the tests use: no test ever reaches the network.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from jarvis.config import PLACEHOLDER_KEY

log = logging.getLogger("jarvis.billing")

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
OPENAI_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"
ANTHROPIC_COST_URL = "https://api.anthropic.com/v1/organizations/cost_report"
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/v1/organizations/usage_report/messages"
ANTHROPIC_VERSION = "2023-06-01"

REQUEST_TIMEOUT_S = 20.0
#: A month is at most 31 daily buckets, which is also both providers' page maximum — so
#: one page is the whole month and pagination is a safety net, not the normal path.
MAX_BUCKETS = 31
MAX_PAGES = 4
#: One retry, briefly, on a 429 or a 5xx. This runs inside a phone call: a second attempt
#: is worth the wait, a third is a silence the caller notices.
RETRY_DELAY_S = 1.0

Provider = Literal["openai", "anthropic"]
ErrorCode = Literal["not_configured", "auth", "rate_limited", "unavailable"]

#: What the model is told to say, per failure. Written to be spoken, and never to name a
#: value that could be a secret — only the *name* of the setting that is missing.
MESSAGES: dict[ErrorCode, str] = {
    "not_configured": (
        "billing is not set up on this machine; say so plainly and offer to have Claude "
        "wire it up"
    ),
    "auth": (
        "the billing credential was refused — it needs an admin key, not the ordinary API "
        "key; say that much and offer to have Claude sort it out"
    ),
    "rate_limited": "the billing API is rate-limiting us; offer to try again in a minute",
    "unavailable": (
        "the billing API did not answer; say the figure is not available right now and "
        "offer to try again"
    ),
}


class BillingError(Exception):
    """A billing lookup that failed in a way worth saying out loud.

    `code` is what went wrong, `detail` is for the log, and `spoken` is the sentence the
    voice model is handed. The detail never carries a credential: callers pass the *name*
    of a setting, never its value.
    """

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        self.spoken = MESSAGES[code]
        super().__init__(detail or code)


def redact(secret: str | None) -> str:
    """A credential reduced to something safe to log: `sk-admin-…f4a2`, or `<unset>`.

    Four trailing characters identify which key is in play without being one. Anything
    short enough that four characters would be most of it is redacted whole.
    """
    if not secret:
        return "<unset>"
    if len(secret) < 12:
        return "<redacted>"
    prefix = secret[:8] if secret.startswith(("sk-admin-", "sk-ant-")) else secret[:3]
    return f"{prefix}…{secret[-4:]}"


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """The calendar month `now` falls in, as `(first instant, first instant after)`, UTC.

    Both providers bill on the UTC calendar month, so that is the period reported — not a
    rolling thirty days, which would be a different and smaller number.
    """
    now = now.astimezone(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days = monthrange(start.year, start.month)[1]
    return start, start + timedelta(days=days)


@dataclass
class BillingReport:
    """One provider's month to date, in the shape the voice tool hands back."""

    provider: Provider
    currency: str
    spend: float
    period_start: datetime
    period_end: datetime
    as_of: datetime
    #: Countable work behind the spend — tokens, requests. Empty when the usage call
    #: failed, which never fails the report: the spend is the headline.
    usage: dict[str, int] = field(default_factory=dict)
    #: The largest cost lines, biggest first, as `{"name": …, "amount": …}`.
    top_line_items: list[dict] = field(default_factory=list)
    #: A configured monthly budget, if there is one; neither provider serves theirs.
    budget: float | None = None
    #: What the spend figure actually covers — the whole organization, or one project.
    scope: str = "organization"

    @property
    def days_elapsed(self) -> float:
        """Days from the start of the month to `as_of`, floored at one.

        The floor is not just about dividing by zero. A run rate over the first few
        minutes of a month is nonsense — a dollar spent at half past midnight on the 1st
        extrapolates to two and a half thousand — and it would be read out loud as if it
        meant something. Flooring at a day caps the first day's projection at
        "this much again, every day", which is at least a sentence that can be true.
        """
        elapsed = (self.as_of - self.period_start).total_seconds() / 86400
        return max(elapsed, 1.0)

    @property
    def projected(self) -> float:
        """Month-end spend at the run rate so far. A straight-line guess, labelled one."""
        days_in_month = (self.period_end - self.period_start).total_seconds() / 86400
        return round(self.spend / self.days_elapsed * days_in_month, 2)

    @property
    def budget_used_percent(self) -> float | None:
        if not self.budget:
            return None
        return round(self.spend / self.budget * 100, 1)

    def spoken(self) -> str:
        """One sentence the model can read out without doing arithmetic of its own."""
        who = "OpenAI" if self.provider == "openai" else "Anthropic"
        line = (
            f"{who} so far this month: {self.spend:.2f} {self.currency}, "
            f"on track for about {self.projected:.0f} by month end"
        )
        if self.budget_used_percent is not None:
            line += f", which is {self.budget_used_percent:.0f} percent of the budget"
        return line + "."

    def as_dict(self) -> dict:
        """The tool's payload. Carries no credential, not even a redacted one."""
        payload = {
            "provider": self.provider,
            "scope": self.scope,
            "currency": self.currency,
            "spend_to_date": round(self.spend, 4),
            "projected_month_end": self.projected,
            "estimate": True,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "as_of": self.as_of.isoformat(),
            "usage": self.usage,
            "spoken": self.spoken(),
        }
        if self.top_line_items:
            payload["top_line_items"] = self.top_line_items
        if self.budget is not None:
            payload["monthly_budget"] = self.budget
            payload["budget_used_percent"] = self.budget_used_percent
        return payload


class BillingReader(Protocol):
    """Anything that can say what this month has cost."""

    #: Which provider this reader reports on, so the tool can name it without asking.
    provider: Provider

    async def month_to_date(self, *, now: datetime | None = None) -> BillingReport:
        """This calendar month's spend, or raise `BillingError`."""
        ...


# --- transport -------------------------------------------------------------


def _get(url: str, params: dict, headers: dict) -> dict:
    """One blocking read-only JSON GET. Called in a worker thread, never on the loop.

    `GET` is not a default here, it is the only thing this function can do: there is no
    body and no method argument, so no caller can turn it into a write.
    """
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    request = urllib.request.Request(f"{url}?{query}", headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


def classify(exc: Exception) -> BillingError:
    """The `BillingError` for a transport failure, without echoing the response body.

    A provider's error body can quote back what was sent, which is how a key ends up in a
    log. Only the status code and the reason phrase are kept.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return BillingError("auth", f"HTTP {exc.code}")
        if exc.code == 429:
            return BillingError("rate_limited", "HTTP 429")
        return BillingError("unavailable", f"HTTP {exc.code}")
    return BillingError("unavailable", f"{type(exc).__name__}")


def _is_retryable(error: BillingError) -> bool:
    return error.code in ("rate_limited", "unavailable")


async def fetch_pages(
    url: str,
    params: dict,
    headers: dict,
    *,
    get=_get,
    sleep=asyncio.sleep,
    max_pages: int = MAX_PAGES,
) -> list[dict]:
    """Every bucket at `url`, following `next_page`, retrying a 429 or 5xx once.

    Returns the flattened `data` buckets. Both providers wrap them the same way; OpenAI
    has also been seen returning the bare list, so both shapes are accepted.
    """
    buckets: list[dict] = []
    page: str | None = None
    for _ in range(max_pages):
        body = await _get_once({**params, "page": page}, url, headers, get=get, sleep=sleep)
        if isinstance(body, list):
            buckets.extend(body)
            break
        buckets.extend(body.get("data") or [])
        page = body.get("next_page")
        if not page:
            break
    return buckets


async def _get_once(params: dict, url: str, headers: dict, *, get, sleep) -> dict | list:
    """One GET with a single retry on the failures that are worth retrying."""
    try:
        return await asyncio.to_thread(get, url, params, headers)
    except Exception as exc:  # noqa: BLE001 - every transport failure is classified below
        error = classify(exc)
        if not _is_retryable(error):
            raise error from exc
        log.warning("billing GET %s failed (%s); retrying once", url, error.detail)
        await sleep(RETRY_DELAY_S)
    try:
        return await asyncio.to_thread(get, url, params, headers)
    except Exception as exc:  # noqa: BLE001 - same classification, no second retry
        raise classify(exc) from exc


def _top_items(totals: dict[str, float], limit: int = 3) -> list[dict]:
    """The biggest cost lines, biggest first, rounded to something speakable."""
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    return [{"name": name, "amount": round(amount, 4)} for name, amount in ranked[:limit] if amount]


# --- OpenAI ----------------------------------------------------------------


class OpenAIBilling:
    """`BillingReader` over the OpenAI Admin API's costs and completions-usage endpoints.

    Costs cannot be filtered to a single API key — the endpoint takes `project_ids` and
    nothing finer — so `scope` says what the figure really covers and the tool repeats it.
    Token usage *can* be narrowed to one key, and is when `api_key_id` is configured.
    """

    provider: Provider = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        project_id: str | None = None,
        api_key_id: str | None = None,
        budget: float | None = None,
        get=_get,
    ) -> None:
        self._api_key = api_key
        self._project_id = project_id
        self._api_key_id = api_key_id
        self._budget = budget
        self._get = get

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def month_to_date(self, *, now: datetime | None = None) -> BillingReport:
        as_of = (now or datetime.now(UTC)).astimezone(UTC)
        start, end = month_bounds(as_of)
        log.info(
            "reading OpenAI spend for %s with %s",
            start.strftime("%Y-%m"),
            redact(self._api_key),
        )

        buckets = await fetch_pages(
            OPENAI_COSTS_URL,
            {
                "start_time": int(start.timestamp()),
                "end_time": int(as_of.timestamp()),
                "bucket_width": "1d",
                "limit": MAX_BUCKETS,
                "group_by": ["line_item"],
                "project_ids": [self._project_id] if self._project_id else None,
            },
            self._headers,
            get=self._get,
        )
        spend, currency, totals = self._sum_costs(buckets)

        return BillingReport(
            provider="openai",
            currency=currency,
            spend=spend,
            period_start=start,
            period_end=end,
            as_of=as_of,
            usage=await self._usage(start, as_of),
            top_line_items=_top_items(totals),
            budget=self._budget,
            scope=f"project:{self._project_id}" if self._project_id else "organization",
        )

    @staticmethod
    def _sum_costs(buckets: list[dict]) -> tuple[float, str, dict[str, float]]:
        """Total spend, its currency, and the per-line-item totals behind it."""
        total = 0.0
        currency = "usd"
        totals: dict[str, float] = {}
        for bucket in buckets:
            for result in bucket.get("results") or []:
                amount = result.get("amount") or {}
                value = float(amount.get("value") or 0.0)
                currency = amount.get("currency") or currency
                total += value
                name = result.get("line_item") or "other"
                totals[name] = totals.get(name, 0.0) + value
        return total, currency.upper(), totals

    async def _usage(self, start: datetime, as_of: datetime) -> dict[str, int]:
        """Token and request counts, or `{}` — a usage outage must not lose the spend."""
        try:
            buckets = await fetch_pages(
                OPENAI_USAGE_URL,
                {
                    "start_time": int(start.timestamp()),
                    "end_time": int(as_of.timestamp()),
                    "bucket_width": "1d",
                    "limit": MAX_BUCKETS,
                    "project_ids": [self._project_id] if self._project_id else None,
                    "api_key_ids": [self._api_key_id] if self._api_key_id else None,
                },
                self._headers,
                get=self._get,
            )
        except BillingError as exc:
            log.warning("OpenAI usage unavailable (%s); reporting spend only", exc.detail)
            return {}
        return self._sum_usage(buckets)

    @staticmethod
    def _sum_usage(buckets: list[dict]) -> dict[str, int]:
        fields = (
            "input_tokens",
            "output_tokens",
            "input_cached_tokens",
            "input_audio_tokens",
            "output_audio_tokens",
            "num_model_requests",
        )
        totals = dict.fromkeys(fields, 0)
        for bucket in buckets:
            for result in bucket.get("results") or []:
                for name in fields:
                    totals[name] += int(result.get(name) or 0)
        totals["requests"] = totals.pop("num_model_requests")
        return totals


# --- Anthropic -------------------------------------------------------------


class AnthropicBilling:
    """`BillingReader` over the Anthropic Admin API's cost and messages-usage reports.

    Amounts arrive as decimal *strings in the lowest currency unit* — `"123.45"` USD is
    one dollar twenty-three, not a hundred and twenty-three — so every one is divided by
    a hundred on the way in. Getting that wrong is a hundred-fold error in something
    read out loud as money, which is why it has a test of its own.
    """

    provider: Provider = "anthropic"
    #: Cents per unit of `currency`. The API documents no currency but USD today.
    MINOR_UNITS = 100

    def __init__(
        self,
        api_key: str,
        *,
        workspace_id: str | None = None,
        budget: float | None = None,
        get=_get,
    ) -> None:
        self._api_key = api_key
        self._workspace_id = workspace_id
        self._budget = budget
        self._get = get

    @property
    def _headers(self) -> dict:
        """Admin keys authenticate with `x-api-key`; console OAuth tokens with a bearer."""
        auth = (
            {"x-api-key": self._api_key}
            if self._api_key.startswith("sk-ant-")
            else {"Authorization": f"Bearer {self._api_key}"}
        )
        return {**auth, "anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}

    async def month_to_date(self, *, now: datetime | None = None) -> BillingReport:
        as_of = (now or datetime.now(UTC)).astimezone(UTC)
        start, end = month_bounds(as_of)
        log.info(
            "reading Anthropic spend for %s with %s",
            start.strftime("%Y-%m"),
            redact(self._api_key),
        )

        buckets = await fetch_pages(
            ANTHROPIC_COST_URL,
            {
                "starting_at": _rfc3339(start),
                "ending_at": _rfc3339(as_of),
                "bucket_width": "1d",
                "limit": MAX_BUCKETS,
                "group_by": ["description"],
            },
            self._headers,
            get=self._get,
        )
        spend, currency, totals = self._sum_costs(buckets)

        return BillingReport(
            provider="anthropic",
            currency=currency,
            spend=spend,
            period_start=start,
            period_end=end,
            as_of=as_of,
            usage=await self._usage(start, as_of),
            top_line_items=_top_items(totals),
            budget=self._budget,
            scope=f"workspace:{self._workspace_id}" if self._workspace_id else "organization",
        )

    @classmethod
    def _sum_costs(cls, buckets: list[dict]) -> tuple[float, str, dict[str, float]]:
        total = 0.0
        currency = "USD"
        totals: dict[str, float] = {}
        for bucket in buckets:
            for result in bucket.get("results") or []:
                try:
                    value = float(result.get("amount") or 0.0) / cls.MINOR_UNITS
                except (TypeError, ValueError):
                    log.warning("skipping a cost line with an unreadable amount")
                    continue
                currency = result.get("currency") or currency
                total += value
                name = result.get("description") or result.get("cost_type") or "other"
                totals[name] = totals.get(name, 0.0) + value
        return total, currency.upper(), totals

    async def _usage(self, start: datetime, as_of: datetime) -> dict[str, int]:
        try:
            buckets = await fetch_pages(
                ANTHROPIC_USAGE_URL,
                {
                    "starting_at": _rfc3339(start),
                    "ending_at": _rfc3339(as_of),
                    "bucket_width": "1d",
                    "limit": MAX_BUCKETS,
                    "workspace_ids": [self._workspace_id] if self._workspace_id else None,
                },
                self._headers,
                get=self._get,
            )
        except BillingError as exc:
            log.warning("Anthropic usage unavailable (%s); reporting spend only", exc.detail)
            return {}
        return self._sum_usage(buckets)

    @staticmethod
    def _sum_usage(buckets: list[dict]) -> dict[str, int]:
        totals = dict.fromkeys(
            ("input_tokens", "output_tokens", "input_cached_tokens", "web_search_requests"), 0
        )
        for bucket in buckets:
            for result in bucket.get("results") or []:
                cache_creation = result.get("cache_creation") or {}
                totals["input_tokens"] += int(result.get("uncached_input_tokens") or 0) + sum(
                    int(value or 0) for value in cache_creation.values()
                )
                totals["output_tokens"] += int(result.get("output_tokens") or 0)
                totals["input_cached_tokens"] += int(result.get("cache_read_input_tokens") or 0)
                tools = result.get("server_tool_use") or {}
                totals["web_search_requests"] += int(tools.get("web_search_requests") or 0)
        return totals


def _rfc3339(moment: datetime) -> str:
    """`moment` as the `2026-08-01T00:00:00Z` the Anthropic Admin API asks for."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- wiring ----------------------------------------------------------------


def build_billing_reader(
    provider: str | None = None,
    *,
    openai_admin_key: str | None = None,
    openai_api_key: str | None = None,
    anthropic_admin_key: str | None = None,
    anthropic_api_key: str | None = None,
    budget: float | None = None,
    openai_project_id: str | None = None,
    openai_api_key_id: str | None = None,
    anthropic_workspace_id: str | None = None,
) -> BillingReader:
    """The reader for `provider` (`auto`, or None, is OpenAI), from explicit values.

    The `check_billing` plugin passes its own settings and the admin keys from the store.
    Raises `BillingError("not_configured")` rather than returning None: "no credential" is
    a sentence the voice model should say, not a tool that quietly does not exist. Which
    key was used is logged redacted; none of it reaches the caller.
    """
    chosen = (provider or "auto").strip().lower()
    if chosen == "auto":
        chosen = "openai"
    if chosen not in ("openai", "anthropic"):
        raise BillingError("not_configured", f"unknown billing provider {chosen!r}")

    if chosen == "anthropic":
        key = anthropic_admin_key or anthropic_api_key
        if not key:
            raise BillingError("not_configured", "ANTHROPIC_ADMIN_KEY is unset")
        return AnthropicBilling(key, workspace_id=anthropic_workspace_id or None, budget=budget)

    key = openai_admin_key or openai_api_key
    if not key or key == PLACEHOLDER_KEY:
        raise BillingError("not_configured", "OPENAI_ADMIN_KEY is unset")
    return OpenAIBilling(
        key,
        project_id=openai_project_id or None,
        api_key_id=openai_api_key_id or None,
        budget=budget,
    )
