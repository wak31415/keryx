"""The HTTP/websocket face of Jarvis: Twilio's webhooks and the media socket (spec §3.3).

One inbound call touches three of these routes:

1. `POST /twilio/voice` — Twilio's webhook. The request must carry a valid
   `X-Twilio-Signature` and come `From` an allowed caller; then it mints a one-time
   stream token and answers with TwiML that opens a media stream back to us.
2. `WS /twilio/media` — the audio socket. It has no signature of its own, so the token
   from step 1 (single-use, 60 s) is what authorizes it; a `VoiceSession` runs on top.
3. `POST /twilio/status` — call-progress callbacks, logged and acknowledged.

The ngrok tunnel makes these the only publicly reachable surface of the machine
(spec §5), so every handler here validates before it does anything else, and the
route bodies stay thin enough to read in one go — the checks live in helpers below.
"""

import logging

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from jarvis.app import AppState
from jarvis.config import Settings
from jarvis.session import VoiceSession
from jarvis.transports.twilio_ws import TransportError, TwilioTransport

log = logging.getLogger("jarvis.server")

PRIVATE_NUMBER_MESSAGE = "Sorry, this number is private."
# Websocket close code for a policy violation (an unknown or expired stream token).
POLICY_VIOLATION = 1008


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
        return _twiml(_stream_twiml(host, token=token, caller=caller))

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
    )
    await session.run()


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


def _stream_twiml(host: str, *, token: str, caller: str) -> VoiceResponse:
    """`<Connect><Stream>` with the stream token (the `url` cannot carry a query)."""
    response = VoiceResponse()
    stream = response.connect().stream(url=f"wss://{host}/twilio/media")
    stream.parameter(name="token", value=token)
    stream.parameter(name="caller", value=caller)
    return response


def _private_number_twiml() -> VoiceResponse:
    response = VoiceResponse()
    response.say(PRIVATE_NUMBER_MESSAGE)
    response.hangup()
    return response


def _twiml(response: VoiceResponse) -> Response:
    """TwiML always goes back with a 200 — Twilio ignores the body of anything else."""
    return Response(content=str(response), media_type="text/xml")
