"""Where a spoken bug report or feature request for Keryx goes, and whether `gh` can file it."""

import subprocess

import pytest
from pydantic import ValidationError

from keryx import issues as issues_module
from keryx.config import Settings
from keryx.config import settings as settings_module
from keryx.config.settings import SOURCE_ROOT, UPSTREAM_REPO
from keryx.issues import SKILL, GhStatus, IssueReporting, gh_status


def _with(settings: Settings, **changes) -> Settings:
    return settings.model_copy(update={"issue_reporting": True, **changes})


def _checkout(tmp_path):
    """A stand-in checkout: all `from_settings` asks of one is the skill."""
    root = tmp_path / "keryx-clone"
    (root / SKILL).parent.mkdir(parents=True)
    (root / SKILL).write_text("---\nname: keryx-report-issue\n---\n", encoding="utf-8")
    return root


def test_it_is_off_until_the_owner_turns_it_on(settings):
    """An issue is public: nothing is filed on anybody's behalf until they said yes."""
    assert settings.issue_reporting is False
    assert IssueReporting.from_settings(settings) is None


def test_on_it_files_upstream_from_the_checkout_keryx_runs_from(settings):
    issues = IssueReporting.from_settings(_with(settings))

    assert issues is not None
    assert issues.repo == UPSTREAM_REPO
    assert issues.checkout == SOURCE_ROOT
    assert issues.skill.is_file()
    assert issues.logs == settings.state_dir / "logs"
    assert issues.calls == settings.data_dir / "calls"


def test_a_configured_checkout_wins_and_its_skill_is_the_one_read(settings, tmp_path):
    clone = _checkout(tmp_path)

    issues = IssueReporting.from_settings(_with(settings, keryx_checkout=clone))

    assert issues is not None
    assert issues.checkout == clone
    assert issues.skill == clone / SKILL


def test_a_checkout_without_the_skill_is_nothing_to_report_from(settings, tmp_path):
    assert IssueReporting.from_settings(_with(settings, keryx_checkout=tmp_path)) is None


def test_with_no_checkout_at_all_there_is_nothing_to_report_from(
    settings, tmp_path, monkeypatch
):
    """An install from a wheel: no source tree beside the code, and no setting."""
    monkeypatch.setattr(settings_module, "SOURCE_ROOT", tmp_path)

    assert settings.checkout is None
    assert IssueReporting.from_settings(_with(settings)) is None


def test_the_call_it_came_from_is_named_only_when_its_transcript_exists(settings):
    issues = IssueReporting.from_settings(_with(settings))
    assert issues is not None
    issues.calls.mkdir(parents=True)
    (issues.calls / "abc123.log").write_text("", encoding="utf-8")

    assert issues.transcript("abc123") == issues.calls / "abc123.log"
    assert issues.transcript("gone") is None
    assert issues.transcript(None) is None


@pytest.mark.parametrize("value", ["", "keryx", "a/b/c", "https://github.com/a/b", "a b/c"])
def test_the_repository_is_owner_slash_name(value):
    with pytest.raises(ValidationError, match="owner/name"):
        Settings(_env_file=None, openai_api_key="test", issue_repo=value)


def test_a_padded_repository_is_trimmed():
    padded = Settings(_env_file=None, openai_api_key="test", issue_repo=" me/fork ")

    assert padded.issue_repo == "me/fork"


def test_the_checkout_setting_is_absolute_or_home_relative(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    home = Settings(_env_file=None, openai_api_key="test", keryx_checkout="~/keryx")
    blank = Settings(_env_file=None, openai_api_key="test", keryx_checkout=" ")

    assert home.checkout == tmp_path / "keryx"
    assert blank.keryx_checkout is None
    with pytest.raises(ValidationError, match="absolute"):
        Settings(_env_file=None, openai_api_key="test", keryx_checkout="keryx")


# --- gh ---------------------------------------------------------------------------------


def _gh(monkeypatch, *, code=0, stdout="", stderr="", raises=None):
    """`gh` on PATH, answering `auth status` with what is given; the argv it was run with."""
    ran = []

    def run(argv, **kwargs):
        ran.append(argv)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    monkeypatch.setattr(issues_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(issues_module.subprocess, "run", run)
    return ran


def test_gh_signed_in_says_as_whom(monkeypatch):
    ran = _gh(monkeypatch, stdout="github.com\n  ✓ Logged in to github.com account octocat "
                                   "(/home/o/.config/gh/hosts.yml)\n")

    assert gh_status() == GhStatus(installed=True, signed_in=True, account="octocat")
    assert ran == [["/usr/bin/gh", "auth", "status", "--hostname", "github.com"]]


def test_an_older_gh_on_stderr_with_its_token_in_the_keyring(monkeypatch):
    _gh(monkeypatch, stderr="  ✓ Logged in to github.com as octocat (keyring)\n")

    status = gh_status()

    assert status.account == "octocat" and status.keyring and status.signed_in


def test_gh_not_signed_in(monkeypatch):
    _gh(monkeypatch, code=1, stderr="You are not logged into any GitHub hosts.\n")

    assert gh_status() == GhStatus(installed=True, signed_in=False)


@pytest.mark.parametrize("error", [OSError("gone"), subprocess.TimeoutExpired("gh", 15)])
def test_a_gh_that_fails_to_answer_is_not_signed_in(monkeypatch, error):
    _gh(monkeypatch, raises=error)

    assert gh_status() == GhStatus(installed=True, signed_in=False)


def test_no_gh_on_path(monkeypatch):
    monkeypatch.setattr(issues_module.shutil, "which", lambda name: None)

    assert gh_status() == GhStatus(installed=False)
