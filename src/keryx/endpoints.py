"""Where a model is: a base URL, an optional key, and a model id.

Every model Keryx talks to — the voice and a local agent alike — is described the way
every OpenAI-compatible client describes one: the `.../v1` root of a server, a Bearer key
when the server wants one, and the server's own name for the model. The provider library
lives inside each agent harness, not here; this is only the address.

It is also the one place the localhost-versus-server rule lives. A server on this machine,
on the LAN or on a tailnet may run without a key (Ollama has none), so a client sends a
placeholder where its SDK refuses an empty one. A public host is warned about — never
refused — when it has no key, or when plain `http://` would carry what it is sent across
the internet in clear text.
"""

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

#: The voice endpoint when `VOICE_BASE_URL` is blank.
OPENAI_BASE_URL = "https://api.openai.com/v1"
#: What a client sends where its SDK refuses an empty key and the server checks none.
PLACEHOLDER_BEARER = "keryx-local"
PROBE_TIMEOUT_S = 10.0

#: Name suffixes that never leave a private network: mDNS, Tailscale's MagicDNS, and the
#: names home routers and RFC 8375 hand out.
PRIVATE_SUFFIXES = (".local", ".ts.net", ".lan", ".home.arpa", ".internal")
#: Tailscale's addresses: the CGNAT range, and its IPv6 prefix.
TAILNET_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


@dataclass(frozen=True)
class Endpoint:
    """One server's `.../v1` root, its optional Bearer key, and a model on it."""

    base_url: str
    api_key: str | None = field(default=None, repr=False)
    model: str = ""

    @classmethod
    def parse(cls, url: str, *, api_key: str | None = None, model: str = "") -> "Endpoint":
        """`url` as a `.../v1` root: `http://box:11434` becomes `http://box:11434/v1`.

        A path other than nothing is kept as it was given (`https://gw/openai/v1`): only a
        bare host gets the `/v1` every OpenAI-compatible server serves under.
        """
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http:// or https:// address")
        if parts.username or parts.password:
            # A key in the address would be printed wherever the address is: doctor, logs.
            raise ValueError("must not carry a user or password; the key has a setting of its own")
        path = parts.path.rstrip("/") or "/v1"
        base = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        return cls(base, api_key or None, model)

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def root(self) -> str:
        """The server without `/v1`: what Claude Code's `ANTHROPIC_BASE_URL` wants."""
        return self.base_url.removesuffix("/v1")

    @property
    def is_openai(self) -> bool:
        return self.host == urlsplit(OPENAI_BASE_URL).hostname

    @property
    def bearer(self) -> str:
        """The key, or the placeholder for an SDK that refuses to send none."""
        return self.api_key or PLACEHOLDER_BEARER

    def headers(self) -> dict[str, str]:
        """`Authorization: Bearer …` when there is a key; nothing when there is not."""
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def ws_url(self, path: str, **query: str) -> str:
        """`path` under the root as a websocket address: http→ws, https→wss."""
        parts = urlsplit(self.url(path))
        scheme = "wss" if parts.scheme == "https" else "ws"
        return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), ""))

    @property
    def is_loopback(self) -> bool:
        host = self.host
        if host == "localhost":
            return True
        address = _address(host)
        return address is not None and address.is_loopback

    @property
    def is_tailnet(self) -> bool:
        """Reached over a tailnet, whose WireGuard tunnel encrypts it whatever the scheme."""
        host = self.host
        if host.endswith(".ts.net"):
            return True
        address = _address(host)
        return address is not None and any(address in net for net in TAILNET_NETWORKS)

    @property
    def is_private(self) -> bool:
        """This machine, the LAN or a tailnet: loopback, RFC 1918, link-local, CGNAT, a
        private name suffix, or a bare host name with no dot in it."""
        host = self.host
        if self.is_loopback or self.is_tailnet:
            return True
        address = _address(host)
        if address is not None:
            return address.is_private or address.is_link_local
        return "." not in host or host.endswith(PRIVATE_SUFFIXES)


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def warnings(endpoint: Endpoint, *, carries_voice: bool = False) -> list[str]:
    """What is worth knowing about where `endpoint` is, as sentences; empty when nothing.

    Never a refusal: a public server with no key may be behind a firewall we cannot see.
    `carries_voice` is the voice endpoint, which hears every call and so the spoken PIN.
    """
    found = []
    scheme = urlsplit(endpoint.base_url).scheme
    if not endpoint.is_private:
        if not endpoint.api_key:
            found.append(
                f"{endpoint.host} is a public address and no key is set — anyone who finds "
                "it can use the server"
            )
        if scheme == "http":
            found.append(
                f"http:// to {endpoint.host} crosses the internet in clear text — use https"
            )
    elif carries_voice and scheme == "http" and not (endpoint.is_loopback or endpoint.is_tailnet):
        found.append(
            f"http:// to {endpoint.host} carries the call audio, the spoken PIN included, "
            "unencrypted across your network — use Tailscale or https"
        )
    return found


@dataclass(frozen=True)
class Probe:
    """What a server said when asked for its models."""

    ok: bool
    models: tuple[str, ...] = ()
    problem: str | None = None


def probe(endpoint: Endpoint, *, get: Callable[..., Any] = httpx.get) -> Probe:
    """`GET {base}/models`: the model ids the server lists, or why it would not say.

    The one read every OpenAI-compatible server answers, OpenAI included, so a key and an
    address are checked by the same request. `models[].name` is Ollama's own shape, read
    as well in case a server answers with that alone.
    """
    try:
        response = get(
            endpoint.url("models"), headers=endpoint.headers(), timeout=PROBE_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        return Probe(False, problem=f"could not reach {endpoint.host} ({type(exc).__name__})")
    if response.status_code == 401:
        return Probe(False, problem=f"{endpoint.host} does not accept that key")
    if response.status_code != 200:
        return Probe(False, problem=f"{endpoint.host} answered HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        return Probe(False, problem=f"{endpoint.host} did not answer with a model list")
    ids = [item.get("id") for item in body.get("data") or [] if isinstance(item, dict)]
    ids += [
        item.get("model") or item.get("name")
        for item in body.get("models") or []
        if isinstance(item, dict)
    ]
    return Probe(True, tuple(dict.fromkeys(str(name) for name in ids if name)))
