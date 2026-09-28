"""Tests for the tokenized report endpoint, `GET /reports/{id}?t=…`.

The HMAC token is the only thing standing between a task report and the public internet,
so these tests care about the failure modes as much as the happy path: a wrong token, a
missing token, and a token that is right for a task whose report is not there.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from jarvis.app import build_app_state
from jarvis.config import Settings
from jarvis.notify.reports import report_token, verify_report_token
from jarvis.server import create_app
from jarvis.tasks.models import Task, TaskKind

SECRET = "a-report-secret"
REPORT = "# Task 1 — research\n\nfind the thing\n\n---\n\nI found the thing.\n"


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "openai_api_key": "test",
        "data_dir": tmp_path / "jarvis",
        "public_host": "jarvis.example",
        "report_secret": SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def state(tmp_path):
    return build_app_state(make_settings(tmp_path))


@pytest.fixture
def client(state):
    with TestClient(create_app(state)) as test_client:
        yield test_client


def make_task(state, *, report: str | None = REPORT, write: bool = True) -> int:
    """A row in the store, with its report on disk unless the test says otherwise."""
    task = asyncio.run(
        state.store.create(Task(id=None, kind=TaskKind.AGENT, description="find the thing"))
    )
    if report is not None:
        path = state.settings.data_dir / "tasks" / f"{task.id}.md"
        if write:
            path.write_text(report, encoding="utf-8")
        asyncio.run(state.store.update(task.id, report_path=str(path)))
    return task.id


def get_report(client, task_id: int, token: str | None):
    query = "" if token is None else f"?t={token}"
    return client.get(f"/reports/{task_id}{query}")


# --- the tokens ------------------------------------------------------------


def test_a_report_token_is_a_short_stable_hex_digest():
    token = report_token(7, SECRET)

    assert token == report_token(7, SECRET)
    assert len(token) == 32
    assert int(token, 16) >= 0  # hex all the way through


def test_every_task_and_every_secret_gets_its_own_token():
    assert report_token(7, SECRET) != report_token(8, SECRET)
    assert report_token(7, SECRET) != report_token(7, "another-secret")


def test_verifying_accepts_only_the_matching_token():
    assert verify_report_token(7, report_token(7, SECRET), SECRET) is True
    assert verify_report_token(7, report_token(8, SECRET), SECRET) is False
    assert verify_report_token(7, report_token(7, "another-secret"), SECRET) is False
    assert verify_report_token(7, "", SECRET) is False


def test_verifying_a_token_that_is_not_ascii_is_false_not_an_error():
    # `hmac.compare_digest` raises TypeError on non-ASCII `str` operands; whatever
    # arrives in the query string has to come back as a plain "no".
    assert verify_report_token(7, "é" * 32, SECRET) is False
    assert verify_report_token(7, "🔓" + report_token(7, SECRET)[1:], SECRET) is False


# --- GET /reports/{id} -----------------------------------------------------


def test_a_valid_token_serves_the_report_as_markdown(client, state):
    task_id = make_task(state)

    response = get_report(client, task_id, report_token(task_id, SECRET))

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.text == REPORT


def test_a_wrong_token_is_refused(client, state):
    task_id = make_task(state)

    response = get_report(client, task_id, report_token(task_id + 1, SECRET))

    assert response.status_code == 403


def test_a_missing_token_is_refused(client, state):
    task_id = make_task(state)

    assert get_report(client, task_id, None).status_code == 403
    assert get_report(client, task_id, "").status_code == 403


def test_a_token_that_is_not_ascii_is_refused_rather_than_crashing(client, state):
    task_id = make_task(state)

    assert get_report(client, task_id, "é" * 32).status_code == 403
    assert get_report(client, task_id, "%C3%A9" * 32).status_code == 403


def test_a_wrong_token_says_nothing_about_whether_the_task_exists(client, state):
    task_id = make_task(state)
    bad = "0" * 32

    assert get_report(client, task_id, bad).status_code == 403
    assert get_report(client, 9999, bad).status_code == 403


def test_a_valid_token_for_a_task_that_does_not_exist_is_a_404(client):
    response = get_report(client, 9999, report_token(9999, SECRET))

    assert response.status_code == 404


def test_a_task_with_no_report_yet_is_a_404(client, state):
    task_id = make_task(state, report=None)

    response = get_report(client, task_id, report_token(task_id, SECRET))

    assert response.status_code == 404


def test_a_report_that_is_no_longer_on_disk_is_a_404(client, state):
    task_id = make_task(state, write=False)

    response = get_report(client, task_id, report_token(task_id, SECRET))

    assert response.status_code == 404
