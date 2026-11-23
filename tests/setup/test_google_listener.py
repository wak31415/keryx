"""The sign-in listener over a real socket on 127.0.0.1.

Everything else about the sign-in is tested with a scripted connection. These few drive the
real server loop, because what they are about only exists there: connections arriving at
once, one of them held open, and which request is served first. The socket is loopback
inside this process; nothing leaves the machine.
"""

import http.client
import socket
import threading
import time

import pytest

from keryx.setup import google
from keryx.setup.google import GoogleSetupError

CONSENT = "https://accounts.google.com/o/oauth2/v2/auth?state=the-state&code_challenge=c"


class Waiting:
    """A real listener with `wait()` running on a thread, as `browser_signin` runs it."""

    def __init__(self, timeout_s=10.0):
        self.listener = google.listen()
        self.port = int(self.listener.redirect_uri.rsplit(":", 1)[1])
        self.start = self.listener.start_page(CONSENT)
        self.result: dict = {}
        self.held: list[socket.socket] = []
        self._thread = threading.Thread(target=self._wait, args=(timeout_s,), daemon=True)
        self._thread.start()

    def _wait(self, timeout_s):
        began = time.monotonic()
        try:
            self.result["answer"] = self.listener.wait("the-state", timeout_s)
        except GoogleSetupError as error:
            self.result["error"] = str(error)
        self.result["took"] = time.monotonic() - began

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            response.read()
            return response.status, response.getheader("Location")
        finally:
            connection.close()

    def hold(self) -> socket.socket:
        """A connection that says nothing, as something trying to get in the way would."""
        held = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.held.append(held)
        return held

    def finished(self) -> dict:
        self._thread.join(10)
        self.listener.close()
        return self.result


@pytest.fixture
def waiting():
    made = []

    def make(**kwargs) -> Waiting:
        made.append(Waiting(**kwargs))
        return made[-1]

    yield make
    for one in made:
        for held in one.held:
            held.close()
        one.listener.close()


def test_the_real_listener_redirects_once_and_takes_the_answer(waiting):
    sign_in = waiting()

    assert sign_in.start == f"http://127.0.0.1:{sign_in.port}/start"
    assert sign_in.get("/start") == (302, CONSENT)
    assert sign_in.get("/favicon.ico")[0] == 404
    assert sign_in.get("/?state=someone-elses&code=x")[0] == 400
    assert sign_in.get("/?state=the-state&code=4/abc")[0] == 200

    assert sign_in.finished()["answer"] == {"state": "the-state", "code": "4/abc"}
    with pytest.raises(OSError):  # and it is shut
        socket.create_connection(("127.0.0.1", sign_in.port), timeout=1)


def test_held_connections_cannot_keep_the_owners_browser_waiting(waiting):
    """Something else on the machine took `/start` first and holds connections open, so that
    the owner's browser queues behind them while it finishes the sign-in as another account.
    The browser's own `/start` has to be served on arrival: it is the second, and stops it."""
    sign_in = waiting()
    theirs = sign_in.get("/start")
    held = [sign_in.hold() for _ in range(8)]

    began = time.monotonic()
    owners = sign_in.get("/start")
    served_in = time.monotonic() - began

    assert theirs == (302, CONSENT)
    assert owners[0] == 410 and served_in < 2.0
    # The other party now delivers a code with the right state, on a connection it held.
    held[-1].sendall(b"GET /?state=the-state&code=THEIRS HTTP/1.0\r\n\r\n")
    assert b"200" not in held[-1].recv(4096).split(b"\r\n")[0]

    result = sign_in.finished()
    assert result["answer"] == {"error": google.OPENED_TWICE}


def test_a_connection_that_says_nothing_is_cut_and_holds_nothing_up(waiting, monkeypatch):
    monkeypatch.setattr(google, "CALLBACK_READ_TIMEOUT_S", 0.2)
    monkeypatch.setattr(google._Callback, "timeout", 0.2)
    sign_in = waiting()
    silent = sign_in.hold()

    assert sign_in.get("/start")[0] == 302  # served while the silent one is still open
    assert silent.recv(1) == b""  # and the silent one is closed under it, on the clock
    assert sign_in.get("/?state=the-state&code=c")[0] == 200
    sign_in.finished()


def test_a_browser_that_never_comes_back_is_given_up_on(waiting):
    sign_in = waiting(timeout_s=0.3)

    result = sign_in.finished()

    assert "did not come back" in result["error"] and result["took"] < 2.0
