"""The Issue reports section: on or off, and `gh` signed in when it is on.

`gh` is `FakeWorld.gh`; nothing here runs it.
"""

from keryx.config.store import ConfigStore
from keryx.issues import GH_LOGIN, GhStatus
from keryx.setup import issues as section
from keryx.setup import wizard

from .fakes import DEFAULT

SIGNED_IN = GhStatus(installed=True, signed_in=True, account="octocat")
SIGNED_OUT = GhStatus(installed=True, signed_in=False)


def test_turned_on_with_gh_already_signed_in(make_ctx, world):
    ctx = make_ctx([("bug reports and feature requests", True)])

    section.run_section(ctx)

    assert ctx.ui.done()
    assert ctx.settings.issue_reporting is True
    assert "gh is signed in as octocat" in ctx.ui.lines("success")
    assert [call for call in world.calls if call[0] == "login"] == []


def test_the_first_walk_recommends_it_and_a_later_one_offers_what_is_set(make_ctx):
    first = make_ctx([("bug reports and feature requests", DEFAULT)])
    section.run_section(first)
    assert first.settings.issue_reporting is True

    first.save({"ISSUE_REPORTING": False})
    ConfigStore().mark_walked("issues")
    again = make_ctx([("bug reports and feature requests", DEFAULT)])
    section.run_section(again)

    assert again.settings.issue_reporting is False


def test_signed_out_it_signs_gh_in_on_this_terminal_and_looks_again(make_ctx, world):
    world.gh = [SIGNED_OUT, SIGNED_IN]
    ctx = make_ctx([("bug reports and feature requests", True), ("Sign in now?", True)])

    section.run_section(ctx)

    assert ctx.ui.done()
    assert ("login", list(GH_LOGIN)) in world.calls
    assert [call for call in world.calls if call[0] == "gh"] == [("gh",), ("gh",)]
    assert "gh is signed in as octocat" in ctx.ui.lines("success")


def test_a_sign_in_that_did_not_take_is_said(make_ctx, world):
    world.gh = [SIGNED_OUT]
    ctx = make_ctx([("bug reports and feature requests", True), ("Sign in now?", True)])

    section.run_section(ctx)

    assert "gh is still not signed in" in ctx.ui.lines("warn")[0]
    assert ctx.settings.issue_reporting is True  # on: doctor keeps saying what is missing


def test_signing_in_later_is_fine(make_ctx, world):
    world.gh = [SIGNED_OUT]
    ctx = make_ctx([("bug reports and feature requests", True), ("Sign in now?", False)])

    section.run_section(ctx)

    assert not [call for call in world.calls if call[0] == "login"]
    assert "`gh auth login` when you are ready" in ctx.ui.lines("note")[-1]


def test_without_gh_it_says_where_to_get_it(make_ctx, world):
    world.gh = [GhStatus(installed=False)]
    ctx = make_ctx([("bug reports and feature requests", True)])

    section.run_section(ctx)

    assert "cli.github.com" in ctx.ui.lines("warn")[0]


def test_a_keyring_token_is_warned_about(make_ctx, world):
    world.gh = [GhStatus(installed=True, signed_in=True, account="octocat", keyring=True)]
    ctx = make_ctx([("bug reports and feature requests", True)])

    section.run_section(ctx)

    assert "--insecure-storage" in ctx.ui.lines("warn")[0]


def test_a_review_asks_which_repository(make_ctx):
    ctx = make_ctx(
        [("bug reports and feature requests", True), ("Which GitHub repository", "me/fork")],
        review=True,
    )

    section.run_section(ctx)

    assert ctx.settings.issue_repo == "me/fork"


def test_the_section_is_left_until_walked_even_though_off_passes_doctor(make_ctx):
    ctx = make_ctx([])
    checks = wizard.run_doctor_checks(ctx.settings, probe_mic=False)
    assert next(c for c in checks if c.name == "issue reports").ok

    found = wizard.statuses(ctx, checks)
    assert found["issues"] == wizard.MISSING
    assert "issues" in [s.key for s in wizard.pending(ctx, found)]

    ConfigStore().mark_walked("issues")
    assert wizard.statuses(ctx, checks)["issues"] == wizard.DONE
