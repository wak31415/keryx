"""The `check_billing` plugin: what the API bill is so far this month, and where it is heading.

Read-only end to end (`integrations.billing`: `_get` takes no method and no body), so it is
not PIN-gated: it changes nothing, and asking what a number is should not need a PIN. Its
settings are `check_billing.toml` (the default provider, the budget, the scoping ids); the
admin keys are `OPENAI_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY` in `secrets.toml`, read when it
is asked, so a key saved since the service started is used without a restart.

Every failure comes back as a `status` with a sentence to say, never a raised exception and
never with a credential in it.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from jarvis import plugins
from jarvis.config import Settings
from jarvis.integrations.billing import (
    REQUEST_TIMEOUT_S,
    BillingError,
    BillingReader,
    build_billing_reader,
)

log = logging.getLogger("jarvis.plugins.billing")

TOOL = "check_billing"

DESCRIPTION = (
    "What the API bill is so far this month, and what it is on track to be. No PIN needed: "
    'it only reads. Call it for "what am I spending", "how much has this cost", "what\'s '
    'the bill" rather than guessing or dispatching. Say "one moment", then the money to '
    'the nearest dollar or two — "about thirty-one dollars so far, on track for ninety-odd" '
    "— never every decimal, once, and call the month-end figure an estimate, because it is "
    "a straight-line projection. Leave provider out for the configured default; ask for "
    "anthropic when they mean what Claude and the subagents have cost. If it comes back "
    "with a status other than ok, say the one thing it tells you to say and do not "
    "speculate about why."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "provider": {
            "type": "string",
            "enum": ["openai", "anthropic"],
            "description": "Whose bill. Leave it out for the configured default.",
        }
    },
    "required": [],
}

BillingFactory = Callable[[str | None], BillingReader]


def reader_for(settings: Settings, config_path: Path, provider: str | None) -> BillingReader:
    """The reader for `provider` (None: the TOML's), with the keys the store holds now."""
    try:
        values = plugins.read_config_file(TOOL, config_path)
    except plugins.PluginConfigError as error:
        raise BillingError("not_configured", str(error)) from None
    budget = values["monthly_budget"]
    return build_billing_reader(
        provider or values["provider"],
        openai_admin_key=plugins.secret(settings, "openai_admin_key"),
        openai_api_key=settings.openai_api_key,
        anthropic_admin_key=plugins.secret(settings, "anthropic_admin_key"),
        anthropic_api_key=plugins.secret(settings, "anthropic_api_key"),
        budget=float(budget) if budget else None,
        openai_project_id=values["openai_project_id"],
        openai_api_key_id=values["openai_api_key_id"],
        anthropic_workspace_id=values["anthropic_workspace_id"],
    )


def check_billing_tool(config_path: Path, *, factory: BillingFactory | None = None):
    """The tool `check_billing.py` defines, configured by the TOML at `config_path`."""
    from jarvis.tools.custom import CustomTool, ToolUnavailable

    settings = plugins.loading_settings()
    try:
        plugins.read_config_file(TOOL, config_path)
    except plugins.PluginConfigError as error:
        raise ToolUnavailable(str(error)) from None
    if factory is None:

        def factory(provider: str | None) -> BillingReader:
            return reader_for(settings, config_path, provider)

    async def check_billing(ctx, arguments: dict) -> dict:
        provider = str(arguments.get("provider") or "").strip().lower() or None
        try:
            report = await factory(provider).month_to_date()
        except BillingError as exc:
            log.warning("billing lookup failed: %s (%s)", exc.code, exc.detail)
            return {"status": exc.code, "message": exc.spoken}
        log.info("billing: %s %.2f %s month to date", report.provider, report.spend,
                 report.currency)
        return {"status": "ok", **report.as_dict()}

    return CustomTool(
        name=TOOL,
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=check_billing,
        needs_pin=False,
        # Two requests, each allowed REQUEST_TIMEOUT_S, and one brief retry.
        timeout_s=REQUEST_TIMEOUT_S * 2 + 5,
    )
