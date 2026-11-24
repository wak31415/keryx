"""The filters one Slack read takes, and the one plan made from them. Slack is a fake."""

import pytest

from integrations.test_slack_read import (
    COL,
    EVE,
    NOW,
    OWNER,
    SAM,
    FakeSlackApi,
    match,
    member,
    message,
    reader,
    ts,
)
from keryx.integrations import slack_query
from keryx.integrations.slack_context import REF
from keryx.integrations.slack_query import (
    MENTIONS,
    PLACE,
    SEARCH,
    THREAD,
    WAITING,
    Filters,
    answer,
    find,
    parse,
)
from keryx.integrations.slack_read import (
    MAX_CANDIDATES,
    MAX_ITEMS,
    MAX_MESSAGES,
    MAX_REQUESTS,
    READ_METHODS,
    SlackReadError,
    Unclear,
)

MENTION = f"<@{OWNER}>"


@pytest.fixture
def api():
    return FakeSlackApi()


async def ask(api, **raw):
    one = reader(api)
    return await answer(one, parse(raw)), one


def texts(context) -> list[str]:
    return [said.text for said in context.messages]


# --- the filters: validated in one place -----------------------------------------------------


def test_no_filter_at_all_is_what_is_waiting_in_the_default_window():
    filters = parse({})

    assert filters == Filters(only=WAITING, days=7, limit=MAX_ITEMS)
    assert filters.kind == WAITING
    assert parse({"days": None, "text": "", "conversation": "  "}) == filters


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ({"only": "waiting"}, WAITING),
        ({"only": "Mentions"}, MENTIONS),
        ({"only": "mentions", "conversation": "general", "from": "Sam"}, MENTIONS),
        ({"conversation": "general"}, PLACE),
        ({"conversation": "general", "from": "Sam"}, PLACE),
        ({"thread": "C0GEN:1700000000.000100"}, THREAD),
        ({"thread": "C0GEN:1700000000.000100", "text": "budget", "from": "Sam"}, THREAD),
        ({"text": "budget"}, SEARCH),
        ({"text": "budget", "conversation": "general", "from": "Sam"}, SEARCH),
        ({"from": "Sam"}, SEARCH),
    ],
)
def test_every_combination_comes_to_one_of_five_reads(raw, kind):
    assert parse(raw).kind == kind


@pytest.mark.parametrize(
    ("raw", "says"),
    [
        ({"only": "unread"}, "only is one of waiting, mentions"),
        ({"thread": "general"}, "a thread is named by the ref of a message in it"),
        ({"thread": "C0GEN"}, "a thread is named by the ref of a message in it"),
        ({"thread": "C0GEN:1700000000.000100", "only": "mentions"}, "a thread is read whole"),
        ({"thread": "C0GEN:1700000000.000100", "conversation": "C0OTHER"},
         "that thread is not in that conversation"),
        ({"conversation": "x" * 200}, "named in a few words"),
        ({"before": "yesterday"}, "before is a message's timestamp"),
        ({"conversation": "general", "before": "999999999999.0"},
         "before is a message's timestamp"),
        ({"only": "mentions", "from": "me"}, "cannot be narrowed to their own messages"),
        ({"only": "waiting", "from": "Myself"}, "cannot be narrowed to their own messages"),
        ({"before": "1700000000.000100"}, "what is waiting is always the latest"),
        ({"conversation": "general", "limit": "many"}, "limit is a number"),
    ],
)
def test_a_filter_that_is_not_one_or_that_contradicts_another_is_unclear(raw, says):
    with pytest.raises(Unclear, match=says) as refused:
        parse(raw)

    assert refused.value.status == "unclear"


def test_the_window_and_the_limit_are_held_to_their_bounds():
    held = parse({"conversation": "general", "days": 400, "limit": 9999})

    assert (held.days, held.limit) == (30, MAX_MESSAGES)
    assert parse({"conversation": "general", "limit": 0}).limit == 1
    assert parse({"conversation": "general"}, limit=MAX_MESSAGES).limit == MAX_MESSAGES


def test_before_may_be_a_ref_and_the_words_are_one_clipped_line():
    filters = parse({"conversation": "general", "before": "C0GEN:1700000000.000100",
                     "text": "the   budget\n\nplan " + "x" * 500})

    assert filters.before == "1700000000.000100"
    assert filters.text.startswith("the budget plan ") and len(filters.text) <= 200


# --- names: nothing is guessed ---------------------------------------------------------------


async def test_a_channel_is_found_by_its_name_however_it_was_said(api):
    one = reader(api)

    for said in ("general", "#general", "the General channel", "budget 2027", "budget-2027"):
        assert (await find(one, said)).id in ("C0GEN", "C0BUD")
    assert (await find(one, "leads")).kind == "private"


async def test_a_person_names_their_direct_conversation_with_the_owner(api):
    one = reader(api)

    assert (await find(one, "Col League")).id == "D0COL"
    assert (await find(one, "eve")).id == "D0EVE"
    assert (await find(one, "@Eve Adams")).id == "D0EVE"


async def test_a_ref_from_an_earlier_result_names_its_conversation_and_asks_no_member_list(api):
    one = reader(api)

    assert (await find(one, "C0GEN:1700000000.000100")).name == "general"
    assert (await find(one, "D0COL")).user == COL
    assert api.made("users.list") == []


async def test_a_name_that_could_be_several_things_is_unclear_with_the_candidates(api):
    api.members.append(member("U0COL2", "Col Other"))
    api.channels += [{"id": "D0COL2", "is_im": True, "user": "U0COL2"},
                     {"id": "C0COL", "name": "col"}, {"id": "C0COLT", "name": "col-team"}]

    with pytest.raises(Unclear) as several:
        await find(reader(api), "col")

    assert several.value.status == "unclear"
    assert [(each.ref, each.label) for each in several.value.candidates] == [
        ("C0COL", "#col"),
        ("D0COL", "Col League, a direct message"),
        ("D0COL2", "Col Other, a direct message"),
        ("C0COLT", "#col-team"),
    ]
    assert "Ask which they mean" in str(several.value)


async def test_a_channels_whole_name_wins_over_a_part_of_another_channels(api):
    api.channels.append({"id": "C0GEN2", "name": "general-chat"})

    assert (await find(reader(api), "general")).id == "C0GEN"


async def test_a_whole_name_does_not_win_when_a_looser_reading_adds_another_person(api):
    """"Lee" is exactly one member's display name and part of another's full name: the
    one meant may be either, so neither is chosen."""
    api.members += [member("U0IMP", "Somebody Else", display="Lee"), member(SAM + "2", "Pat Lee")]

    with pytest.raises(Unclear) as several:
        await find(reader(api), "Lee", channels=False, people="all")

    assert several.value.status == "unclear"
    assert [(each.ref, each.label) for each in several.value.candidates] == [
        ("U0IMP", "Somebody Else"), (SAM, "Sam Lee"), (SAM + "2", "Pat Lee")]


async def test_a_single_match_in_a_list_that_was_cut_short_is_asked_about_not_chosen(api):
    """The one meant may be on a page that was never read."""
    one = reader(api)
    await one.people()
    one.people_cut = True

    with pytest.raises(Unclear) as cut_short:
        await find(one, "Sam", channels=False, people="all")

    assert cut_short.value.status == "unclear"
    assert "more people or conversations than could be read" in str(cut_short.value)
    assert [(each.ref, each.label) for each in cut_short.value.candidates] == [(SAM, "Sam Lee")]
    # And the ref it was offered by names exactly that member, with nothing looked up.
    assert (await find(one, SAM, channels=False, people="all")).id == SAM


async def test_a_conversation_list_that_was_cut_short_is_treated_the_same(api):
    one = reader(api)
    await one.places()
    one.places_cut = True

    with pytest.raises(Unclear, match="than could be read"):
        await find(one, "#general")
    with pytest.raises(Unclear, match="than could be read"):
        await find(one, "@Col League")
    assert (await find(one, "C0GEN")).name == "general", "a ref is exact"


async def test_a_members_id_names_exactly_that_member_or_their_direct_conversation(api):
    one = reader(api)

    assert (await find(one, COL)).id == "D0COL"
    assert (await find(one, COL, channels=False, people="all")).label == "Col League"
    assert (await find(one, OWNER, channels=False, people="all")).id == OWNER
    with pytest.raises(Unclear) as none:
        await find(one, SAM)
    assert none.value.status == "no_match"


async def test_a_word_in_capitals_is_a_name_and_never_taken_for_an_id(api):
    """Slack's ids have a digit in them: `DAN` and `DEV` are what somebody said."""
    api.members.append(member("U0DAN", "Dan Brown"))
    api.channels += [{"id": "C0DEV", "name": "dev"}, {"id": "D0DAN", "is_im": True,
                                                      "user": "U0DAN"}]
    one = reader(api)

    assert (await find(one, "DEV")).id == "C0DEV"
    assert (await find(one, "DAN")).id == "D0DAN"
    assert (await find(one, "GENERAL")).id == "C0GEN"
    assert slack_query.as_ref("C0DEV") and slack_query.as_ref(f"CDEV:{ts(1)}")
    assert not slack_query.as_ref("CDEV") and not slack_query.member_id("USAM")


async def test_two_people_who_share_a_first_name_are_never_chosen_between(api):
    """A first name is how a person is said: one called just that does not win."""
    api.members.append(member("U0EVE2", "Eve"))
    api.channels.append({"id": "D0EVE2", "is_im": True, "user": "U0EVE2"})

    with pytest.raises(Unclear) as several:
        await find(reader(api), "Eve")

    assert [each.label for each in several.value.candidates] == [
        "Eve Adams, a direct message", "Eve, a direct message"]


async def test_the_kind_asked_for_settles_between_a_channel_and_a_person(api):
    api.channels.append({"id": "C0EVE", "name": "eve"})
    one = reader(api)

    assert (await find(one, "#eve")).id == "C0EVE"
    assert (await find(one, "@eve")).id == "D0EVE"
    with pytest.raises(Unclear):
        await find(one, "eve")


async def test_a_name_that_matches_nothing_says_so(api):
    with pytest.raises(Unclear) as nothing:
        await find(reader(api), "the moon committee")

    assert nothing.value.status == "no_match" and not nothing.value.candidates
    assert "Nothing on their Slack is called the moon committee" in str(nothing.value)


async def test_a_member_the_owner_has_no_direct_conversation_with_is_said_as_that(api):
    with pytest.raises(Unclear) as none:
        await find(reader(api), "Sam Lee")

    assert none.value.status == "no_match"
    assert "no direct conversation with Sam Lee" in str(none.value)


async def test_whose_messages_to_keep_is_any_member_and_me_is_the_owner(api):
    one = reader(api)

    assert (await find(one, "Sam", channels=False, people="all")).id == SAM
    assert (await find(one, "me", channels=False, people="all")).id == OWNER
    with pytest.raises(Unclear) as nobody:
        await find(one, "general", channels=False, people="all")
    assert nobody.value.status == "no_match"


async def test_a_name_is_found_however_its_accents_were_heard(api):
    """Speech has no accents: a name said on a call arrives without the ones Slack
    spells it with, and one typed with them finds a name spelled without."""
    api.members += [member("U0ZOE", "Zoë Müller"), member("U0JOSE", "Jose Peña")]
    api.channels += [{"id": "D0ZOE", "is_im": True, "user": "U0ZOE"},
                     {"id": "C0CAFE", "name": "café-run"}]
    one = reader(api)

    assert (await find(one, "Zoe", channels=False, people="all")).id == "U0ZOE"
    assert (await find(one, "zoe muller")).id == "D0ZOE"
    assert (await find(one, "José Pena", channels=False, people="all")).id == "U0JOSE"
    assert (await find(one, "cafe run")).id == "C0CAFE"


async def test_two_names_that_differ_only_by_an_accent_are_asked_about_not_chosen(api):
    api.members += [member("U0RENE", "René Blanc"), member("U0RENE2", "Rene Noir")]

    with pytest.raises(Unclear) as unclear:
        await find(reader(api), "Rene", channels=False, people="all")

    assert [each.ref for each in unclear.value.candidates] == ["U0RENE", "U0RENE2"]


async def test_words_are_matched_without_their_accents_too(api):
    api.threads[("C0GEN", ts(2))] = [message(COL, "the café is closed", ts(2)),
                                     message(EVE, "the budget is fine", ts(1))]

    context, _ = await ask(api, thread=f"C0GEN:{ts(2)}", text="cafe")

    assert texts(context) == ["the café is closed"]


async def test_only_so_many_candidates_are_ever_named(api):
    api.channels += [{"id": f"C0TEAM{n}", "name": f"team-{n}"} for n in range(20)]

    with pytest.raises(Unclear) as several:
        await find(reader(api), "team")

    assert len(several.value.candidates) == MAX_CANDIDATES


# --- one place, read in order ----------------------------------------------------------------


async def test_a_conversation_is_read_in_order_within_the_window(api):
    api.histories["C0GEN"] = [message(COL, "newest", ts(1)), message(EVE, "older", ts(2)),
                              message(COL, "oldest", ts(3))]

    context, _ = await ask(api, conversation="general", days=5)

    assert texts(context) == ["oldest", "older", "newest"]
    assert [said.where for said in context.messages] == ["#general"] * 3
    [params] = api.made("conversations.history")
    assert (params["channel"], params["limit"]) == ("C0GEN", str(MAX_ITEMS))
    assert float(params["oldest"]) == NOW - 5 * 86400
    assert not context.more and context.before == ""


async def test_when_slack_had_more_than_was_asked_for_it_says_so_and_where_to_go_on(api):
    api.histories["C0GEN"] = [message(COL, f"m{n}", ts(1, 9 - n)) for n in range(3)]
    api.has_more.add("C0GEN")

    context, _ = await ask(api, conversation="C0GEN", limit=3)

    assert context.more and context.before == ts(1, 7), "the oldest it read"


async def test_only_what_one_person_wrote_there(api):
    api.histories["C0GEN"] = [message(COL, "from col", ts(1)), message(SAM, "from sam", ts(2)),
                              message(SAM, "sam again", ts(3))]

    context, _ = await ask(api, **{"conversation": "general", "from": "sam"})

    assert texts(context) == ["sam again", "from sam"]


async def test_reading_further_back_is_the_same_read_before_a_message(api):
    api.histories["C0GEN"] = [message(COL, "older", ts(3))]

    await ask(api, conversation="general", before=f"C0GEN:{ts(2)}")

    assert api.made("conversations.history")[0]["latest"] == ts(2)


async def test_a_thread_is_read_in_order_and_the_latest_of_it_when_it_is_long(api):
    root = ts(2)
    api.threads[("C0GEN", root)] = [message(COL, "the question", root, thread_ts=root)] + [
        message(EVE, f"reply {n}", ts(1, n), thread_ts=root) for n in range(12)
    ]

    context, _ = await ask(api, thread=f"C0GEN:{root}")

    assert texts(context) == [f"reply {n}" for n in range(4, 12)] and context.more
    assert {said.ref for said in context.messages} == {f"C0GEN:{root}"}
    assert api.made("conversations.history") == [] and api.made("search.messages") == []


async def test_words_and_a_person_narrow_a_thread_without_a_search(api):
    root = ts(2)
    api.threads[("C0GEN", root)] = [
        message(COL, "what about the Budget?", root, thread_ts=root),
        message(SAM, "the budget is fine", ts(1), thread_ts=root),
        message(SAM, "lunch?", ts(1, 1), thread_ts=root),
    ]

    context, _ = await ask(api, **{"thread": f"C0GEN:{root}", "text": "budget", "from": "Sam"})

    assert texts(context) == ["the budget is fine"]
    assert api.made("search.messages") == []


async def test_a_thread_and_the_name_of_another_conversation_is_unclear_and_reads_nothing(api):
    with pytest.raises(Unclear, match="that thread is not in that conversation"):
        await ask(api, thread=f"C0GEN:{ts(1)}", conversation="budget-2027")

    assert api.made("conversations.replies") == []


async def test_a_thread_and_the_name_of_its_own_conversation_is_fine(api):
    api.threads[("C0GEN", ts(1))] = [message(COL, "hello", ts(1))]

    context, _ = await ask(api, thread=f"C0GEN:{ts(1)}", conversation="general")

    assert texts(context) == ["hello"]


# --- a search, narrowed by the other filters -------------------------------------------------


async def test_words_alone_are_one_search_everywhere_the_owner_can_see(api):
    api.searches["budget"] = [match(COL, "the budget is done", ts(1), "C0GEN", kind="channel",
                                    name="general")]

    context, _ = await ask(api, text="budget")

    assert texts(context) == ["the budget is done"]
    assert [name for name, _ in api.calls] == ["search.messages", "users.info"]


async def test_a_place_and_a_person_narrow_the_search_twice(api):
    """Once in the query, with the modifiers Slack documents, and again on what comes
    back, which is the half that is trusted."""
    api.searches["budget"] = [
        match(SAM, "sam in general", ts(1), "C0GEN", kind="channel", name="general"),
        match(COL, "col in general", ts(1, 1), "C0GEN", kind="channel", name="general"),
        match(SAM, "sam elsewhere", ts(1, 2), "C0BUD", kind="channel", name="budget-2027"),
    ]

    context, _ = await ask(api, **{"text": "budget", "conversation": "general", "from": "Sam"})

    assert api.made("search.messages")[0]["query"].startswith(
        f"budget from:<@{SAM}> in:general after:")
    assert texts(context) == ["sam in general"]


async def test_a_direct_conversation_is_searched_by_its_member(api):
    await ask(api, text="budget", conversation="Col")

    assert f"in:<@{COL}>" in api.made("search.messages")[0]["query"]


async def test_one_person_alone_is_a_search_for_what_they_wrote(api):
    api.searches[f"from:<@{SAM}>"] = [match(SAM, "anything", ts(1), "C0GEN", kind="channel")]

    context, _ = await ask(api, **{"from": "Sam Lee"})

    assert texts(context) == ["anything"]


async def test_the_window_is_held_on_what_comes_back_whatever_slack_returned(api):
    api.searches["budget"] = [match(COL, "inside", ts(2)), match(COL, "outside", ts(12))]

    context, _ = await ask(api, text="budget", days=7)

    assert texts(context) == ["inside"]


async def test_more_matches_than_are_listed_is_more(api):
    api.searches["budget"] = [match(COL, f"m{n}", ts(1, n)) for n in range(MAX_ITEMS + 3)]

    context, _ = await ask(api, text="budget")

    assert len(context.messages) == MAX_ITEMS and context.more and context.before


async def test_mentions_are_one_search_for_the_owners_mention_answered_or_not(api):
    api.searches[MENTION] = [
        match(COL, f"{MENTION} thoughts?", ts(1), "C0GEN", kind="channel", name="general"),
        match(OWNER, f"note to {MENTION}", ts(1, 1), "C0GEN", kind="channel"),
        match("", f"{MENTION} build failed", ts(1, 2), "C0GEN", kind="channel"),
    ]

    context, _ = await ask(api, only="mentions")

    assert texts(context) == ["@you thoughts?"], "not their own, and not an app's"
    assert api.made("conversations.replies") == []


async def test_mentions_combine_with_a_place_a_person_and_words(api):
    api.searches[MENTION] = [
        match(SAM, f"{MENTION} the budget?", ts(1), "C0GEN", kind="channel", name="general"),
        match(COL, f"{MENTION} the budget!", ts(1, 1), "C0GEN", kind="channel", name="general"),
    ]

    context, _ = await ask(api, **{"only": "mentions", "conversation": "#general",
                                   "from": "Sam", "text": "budget"})

    assert api.made("search.messages")[0]["query"].startswith(
        f"{MENTION} budget from:<@{SAM}> in:general ")
    assert texts(context) == ["@you the budget?"]


# --- what is waiting: a best effort ----------------------------------------------------------


async def test_a_direct_message_whose_last_word_is_somebody_elses_is_waiting(api):
    api.searches["to:me"] = [match(COL, "can you review my PR?", ts(1))]
    api.histories["D0COL"] = [message(COL, "can you review my PR?", ts(1))]

    context, one = await ask(api)

    [said] = context.messages
    assert (said.who, said.where, said.text) == (
        "Col League", "a direct message", "can you review my PR?")
    assert said.ref == f"D0COL:{ts(1)}" and said.link.startswith("https://")
    assert not context.more and not context.incomplete
    assert [params["query"].split(" after:")[0] for params in api.made("search.messages")] == [
        "to:me", MENTION]


async def test_one_the_owner_has_answered_since_is_not(api):
    api.searches["to:me"] = [match(COL, "can you review my PR?", ts(1))]
    api.histories["D0COL"] = [message(OWNER, "done", ts(0.5)),
                              message(COL, "can you review my PR?", ts(1))]

    context, _ = await ask(api)

    assert context.messages == ()


async def test_one_the_owner_answered_in_its_thread_is_not(api):
    """The conversation's last message is still theirs: the answer is under it."""
    api.searches["to:me"] = [match(COL, "can you review my PR?", ts(1))]
    api.histories["D0COL"] = [message(COL, "can you review my PR?", ts(1), reply_count=1,
                                      reply_users=[OWNER], reply_users_count=1)]

    context, _ = await ask(api)

    assert context.messages == ()


async def test_when_who_replied_under_it_is_not_all_listed_it_is_not_claimed(api):
    api.searches["to:me"] = [match(COL, "thoughts?", ts(1), "G0MPIM", kind="mpim")]
    api.histories["G0MPIM"] = [message(COL, "thoughts?", ts(1), reply_users=[EVE, SAM],
                                       reply_users_count=6)]

    context, _ = await ask(api)

    assert context.messages == () and context.more


async def test_a_file_somebody_shared_is_a_person_writing_and_a_bot_is_not(api):
    api.searches["to:me"] = [match(COL, "here is the deck", ts(1)),
                             match(EVE, "ping", ts(2), "D0EVE")]
    api.histories["D0COL"] = [message(COL, "here is the deck", ts(1), subtype="file_share")]
    api.histories["D0EVE"] = [message("", "reminder: stand-up", ts(1), bot_id="B0REMIND"),
                              message(OWNER, "on it", ts(1.5)), message(EVE, "ping", ts(2))]

    context, _ = await ask(api)

    assert texts(context) == ["here is the deck"]


async def test_slacks_own_notice_after_a_message_does_not_hide_it(api):
    api.searches["to:me"] = [match(COL, "are you in?", ts(1), "G0MPIM", kind="mpim")]
    api.histories["G0MPIM"] = [message(EVE, "", ts(0.5), subtype="channel_join"),
                               message(COL, "are you in?", ts(1))]

    context, _ = await ask(api)

    assert texts(context) == ["are you in?"]


async def test_a_conversation_counts_once_however_many_messages_it_holds(api):
    api.searches["to:me"] = [match(COL, f"m{n}", ts(1, 9 - n)) for n in range(5)]
    api.histories["D0COL"] = [message(COL, "m0", ts(1, 9))]

    context, _ = await ask(api)

    assert len(context.messages) == 1 and len(api.made("conversations.history")) == 1


async def test_a_mention_in_a_channel_the_owner_has_not_written_in_since_is_waiting(api):
    asked = ts(1)
    api.searches[MENTION] = [match(COL, f"{MENTION} can you look?", asked, "C0GEN",
                                   kind="channel", name="general")]
    api.threads[("C0GEN", asked)] = [message(COL, f"{MENTION} can you look?", asked)]
    api.histories["C0GEN"] = [message(EVE, "unrelated", ts(0.5))]

    context, _ = await ask(api)

    assert [(said.where, said.text) for said in context.messages] == [
        ("#general", "@you can you look?")]


@pytest.mark.parametrize("where", ["thread", "channel"])
async def test_a_mention_the_owner_wrote_after_is_not(api, where):
    asked = ts(1)
    api.searches[MENTION] = [match(COL, f"{MENTION} can you look?", asked, "C0GEN",
                                   kind="channel")]
    reply = message(OWNER, "looking", ts(0.5))
    api.threads[("C0GEN", asked)] = [reply] if where == "thread" else []
    api.histories["C0GEN"] = [reply] if where == "channel" else []

    context, _ = await ask(api)

    assert context.messages == ()


async def test_a_mention_inside_a_thread_is_judged_by_that_thread_alone(api):
    root, asked = ts(2), ts(1)
    api.searches[MENTION] = [match(COL, f"{MENTION} ?", asked, "C0GEN", kind="channel",
                                   thread=root)]
    api.threads[("C0GEN", root)] = [message(COL, f"{MENTION} ?", asked, thread_ts=root)]
    api.histories["C0GEN"] = [message(OWNER, "elsewhere in the channel", ts(0.5))]

    context, _ = await ask(api)

    assert len(context.messages) == 1 and api.made("conversations.history") == []


async def test_when_more_was_written_after_a_mention_than_was_read_it_is_not_claimed(api):
    """The owner's answer may be in the part that was not read."""
    asked = ts(1)
    api.searches[MENTION] = [match(COL, f"{MENTION} can you look?", asked, "C0GEN",
                                   kind="channel")]
    api.histories["C0GEN"] = [message(EVE, "chatter", ts(0.5))]
    api.has_more.add("C0GEN")

    context, _ = await ask(api)

    assert context.messages == () and context.more and not context.incomplete


async def test_half_of_the_question_failing_makes_the_answer_incomplete_and_says_so(api):
    api.searches["to:me"] = [match(COL, "can you review my PR?", ts(1))]
    api.histories["D0COL"] = [message(COL, "can you review my PR?", ts(1))]
    api.errors[f"search.messages:{MENTION}"] = SlackReadError("rate_limited")

    context, _ = await ask(api)

    assert texts(context) == ["can you review my PR?"] and context.incomplete
    assert "part of Slack could not be read" in context.fenced()


async def test_both_halves_failing_is_the_failure_it_is(api):
    api.errors["search.messages"] = SlackReadError("rate_limited")

    with pytest.raises(SlackReadError) as failed:
        await ask(api)

    assert failed.value.code == "rate_limited"


async def test_one_conversation_that_cannot_be_read_does_not_cost_the_rest(api):
    api.searches["to:me"] = [match(COL, "one", ts(1)), match(EVE, "two", ts(2), "D0EVE")]
    api.histories["D0EVE"] = [message(EVE, "two", ts(2))]
    api.errors["conversations.history:D0COL"] = SlackReadError("not_found")

    context, _ = await ask(api)

    assert texts(context) == ["two"] and context.incomplete


async def test_only_so_many_are_looked_into_and_the_rest_is_more(api):
    api.searches["to:me"] = [match(COL, f"m{n}", ts(1, n), f"D0X{n:03d}") for n in range(20)]

    context, one = await ask(api)

    assert len(api.made("conversations.history")) == MAX_CANDIDATES and context.more
    assert len(api.calls) <= MAX_REQUESTS


async def test_the_most_waiting_can_cost_fits_the_budget_with_names_to_spare(api):
    """Two searches, then every candidate of both halves confirmed, with names resolved."""
    api.searches["to:me"] = [match(COL, f"m{n}", ts(1, n), f"D0X{n:03d}") for n in range(20)]
    api.searches[MENTION] = [match(EVE, f"{MENTION} {n}", ts(2, n), f"C0Y{n:03d}",
                                   kind="channel") for n in range(20)]
    for n in range(20):
        api.histories[f"D0X{n:03d}"] = [message(COL, f"m{n}", ts(1, n))]

    context, one = await ask(api)

    reads = [name for name, _ in api.calls if name != "users.info"]
    assert len(reads) == 2 + MAX_CANDIDATES + 2 * MAX_CANDIDATES <= MAX_REQUESTS - 6
    assert not context.incomplete and context.more
    assert {said.who for said in context.messages} == {"Col League", "Eve Adams"}


async def test_what_is_waiting_from_the_owner_themselves_is_unclear_and_reads_nothing(api):
    """By their own name it costs the member list to find out; by `me`, nothing at all."""
    with pytest.raises(Unclear, match="cannot be narrowed to their own messages"):
        await ask(api, **{"only": "waiting", "from": "Owner Person"})

    assert [name for name, _ in api.calls] == ["users.list"]
    with pytest.raises(Unclear):
        parse({"only": "waiting", "from": "me"})


async def test_nothing_found_while_candidates_were_left_unread_says_so(api):
    """Seven wrote and the owner answered the newest six: the seventh was never looked at."""
    api.searches["to:me"] = []
    for n in range(MAX_CANDIDATES + 1):
        dm, when = f"D0P{n:02d}", ts(0.1 * (n + 1))
        api.searches["to:me"].append(match(COL, f"q{n}?", when, dm))
        mine = [message(OWNER, "done", ts(0.1 * (n + 1) - 0.01))] if n < MAX_CANDIDATES else []
        api.histories[dm] = mine + [message(COL, f"q{n}?", when)]

    context, _ = await ask(api)

    assert context.messages == () and context.more
    assert "(no messages in what was read)" in context.fenced()
    assert "not all of it was read" in context.fenced()


async def test_waiting_combines_with_a_place_a_person_and_words(api):
    api.searches["to:me"] = [match(COL, "the budget?", ts(1)),
                             match(EVE, "lunch?", ts(2), "D0EVE")]
    api.histories["D0COL"] = [message(COL, "the budget?", ts(1))]
    api.histories["D0EVE"] = [message(EVE, "lunch?", ts(2))]

    by_place, _ = await ask(api, only="waiting", conversation="Eve")
    by_person, _ = await ask(api, **{"only": "waiting", "from": "Col"})
    by_words, _ = await ask(api, only="waiting", text="budget")

    assert texts(by_place) == ["lunch?"]
    assert texts(by_person) == texts(by_words) == ["the budget?"]


# --- refs, and what never reaches Slack ------------------------------------------------------


async def test_every_message_handed_back_carries_a_ref_that_a_later_call_can_use(api):
    root = ts(2)
    api.searches["budget"] = [match(COL, "in a thread", ts(1), "C0GEN", kind="channel",
                                    name="general", thread=root)]
    api.threads[("C0GEN", root)] = [message(COL, "the thread's root", root)]

    found, _ = await ask(api, text="budget")
    [said] = found.messages
    assert REF.fullmatch(said.ref) and f"[{said.ref}]" in found.fenced()

    again, _ = await ask(api, thread=said.ref)
    assert texts(again) == ["the thread's root"]


@pytest.mark.parametrize(
    "raw",
    [
        {"conversation": "../../chat.postMessage"},
        {"conversation": "general; rm -rf ~"},
        {"conversation": "C0GEN&channel=C0SECRET"},
        {"from": "<@U0EVE> in:#secret"},
        {"text": "x&method=chat.postMessage", "conversation": "$(whoami)"},
        {"conversation": "conversations.mark"},
    ],
)
async def test_nothing_the_caller_says_is_a_method_a_path_or_a_command(api, raw):
    """A name is only ever compared with what Slack listed; the words are one parameter."""
    try:
        await ask(api, **raw)
    except Unclear:
        pass

    for method, params in api.calls:
        assert method in READ_METHODS
        assert set(params) <= {"query", "count", "sort", "sort_dir", "channel", "limit",
                               "oldest", "latest", "ts", "cursor", "types", "user",
                               "exclude_archived"}
        assert "postMessage" not in method and all(
            value in ("C0GEN", "C0BUD", "G0PRIV", "D0COL", "D0EVE")
            for key, value in params.items() if key == "channel")


async def test_a_conversations_name_goes_into_a_query_only_as_one_bare_word(api):
    api.channels.append({"id": "C0ODD", "name": "odd name from:<@U0EVE>"})

    await ask(api, text="budget", conversation="C0ODD")

    assert "in:" not in api.made("search.messages")[0]["query"]


def test_the_module_plans_and_never_asks_slack_itself():
    """Every request goes through the reader, which is where the bounds are."""
    import inspect

    source = inspect.getsource(slack_query)
    assert "_api" not in source and "httpx" not in source and "._get(" not in source
