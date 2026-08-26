"""The HTTP/websocket face of Jarvis: Twilio's webhooks and the media socket (spec §3.3).

One inbound call touches three of these routes:

1. `POST /twilio/voice` — Twilio's webhook. The request must carry a valid
   `X-Twilio-Signature` and come `From` an allowed caller; then it mints a one-time
   stream token and answers with TwiML that opens a media stream back to us.
2. `WS /twilio/media` — the audio socket. It has no signature of its own, so the token
   from step 1 (single-use, 60 s, and only valid for the call it was minted for) is what
   authorizes it; a `VoiceSession` runs on top.
3. `POST /twilio/status` — call-progress callbacks, logged and acknowledged.

`GET /reports/{id}?t=…` is the fourth public route: the link the Notifier texts, guarded
by the HMAC token in `t` rather than by a signature (spec §5).

The ngrok tunnel makes these the only publicly reachable surface of the machine
(spec §5), so every handler here validates before it does anything else, and the
route bodies stay thin enough to read in one go — the checks live in helpers below.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from starlette.datastructures import FormData
from starlette.responses import PlainTextResponse
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from jarvis.app import AppState
from jarvis.config import Settings
from jarvis.notify.notifier import verify_report_token
from jarvis.notify.twilio_out import stream_twiml
from jarvis.session import VoiceSession
from jarvis.transports.twilio_ws import TransportError, TwilioTransport

log = logging.getLogger("jarvis.server")

PRIVATE_NUMBER_MESSAGE = "Sorry, this number is private."
# Websocket close code for a policy violation (an unknown or expired stream token).
POLICY_VIOLATION = 1008
REPORT_MEDIA_TYPE = "text/markdown; charset=utf-8"


def create_app(state: AppState) -> FastAPI:
    """Build the FastAPI app around one `AppState`."""
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)
    app.state.jarvis = state  # so later routes (and tests) can reach the shared wiring
    settings = state.settings

    @app.post("/twilio/voice")
    async def twilio_voice(request: Request) -> Response:
        """Answer an inbound call with TwiML that opens the media stream."""
        form = await request.form()
        if not verify_twilio_request(request, form, settings):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")

        caller = str(form.get("From") or "")
        if caller not in settings.allowed_callers:
            log.warning("refused a call from %s: not in ALLOWED_CALLERS", caller or "<unknown>")
            return _twiml(_private_number_twiml())

        token = state.stream_tokens.issue(
            caller=caller, extra={"call_sid": str(form.get("CallSid") or "")}
        )
        host = settings.public_host or external_host(request)
        log.info("answering a call from %s; streaming to %s", caller, host)
        return _twiml(stream_twiml(host, {"token": token, "caller": caller}))

    @app.post("/twilio/status")
    async def twilio_status(request: Request) -> Response:
        """Log a call-progress callback (spec §4 `status_callback`)."""
        form = await request.form()
        if not verify_twilio_request(request, form, settings):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")

        log.info("call %s is now %s", form.get("CallSid"), form.get("CallStatus"))
        return Response(status_code=204)

    @app.websocket("/twilio/media")
    async def twilio_media(websocket: WebSocket) -> None:
        """Run one voice session over a Twilio media stream."""
        await websocket.accept()
        transport = TwilioTransport(websocket)
        try:
            await _run_media_session(state, transport)
        except TransportError as error:
            log.warning("a media stream never started: %s", error)
        except Exception:
            log.exception("the media stream failed")
        finally:
            await transport.hangup()

    @app.get("/reports/{task_id}")
    async def report(task_id: int, t: str = "") -> Response:
        """Serve one task report to whoever holds its token (spec §5).

        The token is checked *before* the task is looked up, so a wrong token tells the
        holder nothing about which task ids exist.
        """
        if not t or not verify_report_token(task_id, t, settings.report_secret_value()):
            log.warning("refused a report request for task %s: bad token", task_id)
            raise HTTPException(status_code=403, detail="invalid report token")

        content = await _read_report(state, task_id)
        if content is None:
            raise HTTPException(status_code=404, detail="no such report")
        return PlainTextResponse(content, media_type=REPORT_MEDIA_TYPE)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "live_sessions": len(state.sessions.live())}

    return app


async def _run_media_session(state: AppState, transport: TwilioTransport) -> None:
    """Redeem the stream token, then run the session until the call ends."""
    info = await transport.start()
    token_info = state.stream_tokens.redeem(info.custom_parameters.get("token", ""))
    if token_info is None:
        log.warning("closing media stream %s: unknown or expired token", info.stream_sid)
        await transport.hangup(POLICY_VIOLATION)
        return

    expected_call = token_info.extra.get("call_sid")
    if expected_call and expected_call != info.call_sid:
        # The token was minted for a different call: someone replayed a `<Parameter>`.
        log.warning("closing media stream %s: the token belongs to another call", info.stream_sid)
        await transport.hangup(POLICY_VIOLATION)
        return

    # The caller from the token was signature-validated at `/twilio/voice`; the one in
    # `customParameters` merely came back over an unauthenticated socket.
    transport.caller = token_info.caller
    session = VoiceSession(
        transport,
        state.provider_factory(),
        state.settings,
        state.registry,
        state.bus,
        authorized=False,  # the phone channel earns authorization with the PIN (spec §5)
        opening_context=token_info.extra.get("opening_context"),
        registry=state.sessions,
        briefer=state.briefer,
    )
    await session.run()


async def _read_report(state: AppState, task_id: int) -> str | None:
    """The markdown of `task_id`'s report, or None if there is no readable file."""
    task = await state.manager.get(task_id) if state.manager is not None else None
    if task is None or not task.report_path:
        return None
    try:
        return await asyncio.to_thread(Path(task.report_path).read_text, encoding="utf-8")
    except OSError:
        log.warning("the report of task %s is not readable at %s", task_id, task.report_path)
        return None


# --- request authentication -------------------------------------------------


def verify_twilio_request(request: Request, form: FormData, settings: Settings) -> bool:
    """True if `request` really came from Twilio (spec §4, §5).

    The signature is computed over the URL Twilio actually called, which is not the URL
    that reaches us: ngrok terminates TLS and forwards to localhost, so the public scheme
    and host come from `x-forwarded-proto` / `x-forwarded-host` when present.

    Validation is skipped only when `DEBUG_SKIP_TWILIO_VALIDATION` is set. Without an
    auth token there is nothing to verify, so every request is refused instead.
    """
    if settings.debug_skip_twilio_validation:
        log.debug("skipping Twilio signature validation (DEBUG_SKIP_TWILIO_VALIDATION)")
        return True
    if not settings.twilio_auth_token:
        log.warning("refusing %s: TWILIO_AUTH_TOKEN is not configured", request.url.path)
        return False

    signature = request.headers.get("X-Twilio-Signature", "")
    valid = RequestValidator(settings.twilio_auth_token).validate(
        twilio_request_url(request), dict(form), signature
    )
    if not valid:
        log.warning("refusing %s: the Twilio signature did not match", request.url.path)
    return valid


def twilio_request_url(request: Request) -> str:
    """The public URL Twilio signed, rebuilt from the proxy headers."""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    url = f"{scheme}://{external_host(request)}{request.url.path}"
    return f"{url}?{request.url.query}" if request.url.query else url


def external_host(request: Request) -> str:
    """The host the outside world used, preferring what the tunnel forwarded."""
    return (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )


# --- TwiML ------------------------------------------------------------------


def _private_number_twiml() -> VoiceResponse:
    response = VoiceResponse()
    response.say(PRIVATE_NUMBER_MESSAGE)
    response.hangup()
    return response


def _twiml(response: VoiceResponse | str) -> Response:
    """TwiML always goes back with a 200 — Twilio ignores the body of anything else."""
    return Response(content=str(response), media_type="text/xml")
