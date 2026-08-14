"""The engine: streaming, history, metrics, transcript, and the roster parser.

Everything downstream (visual/, prep/) depends on these staying true — the
transcript schema in particular, since it's the data the whole project exists
to produce.
"""

import asyncio
import datetime
import io
import json
import os
import shutil
import sys

import httpx
import pytest
from fakes import DEFAULT_CHUNKS, FakeClient, Usage, quota_exceeded, refused
from google.genai import errors as genai_errors
from rich.ansi import AnsiDecoder
from rich.console import Console
from rich.style import Style

import ghost

PARTY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "party.txt")

TRANSCRIPT_KEYS = {"session_id", "turn", "model", "ts", "user", "assistant",
                   "input_tokens", "output_tokens", "total_tokens",
                   "latency_ms", "ttft_ms"}


@pytest.fixture
def client():
    return FakeClient(expect_system=ghost.SYSTEM_PROMPT)


@pytest.fixture
def session(client, tmp_path):
    return ghost.GhostSession(client=client, transcript_dir=str(tmp_path))


def read_transcript(session):
    with open(session.transcript_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def test_sync_streams_every_chunk(session):
    seen = []
    session.send("hello", on_chunk=seen.append)
    assert seen == DEFAULT_CHUNKS


def test_sync_assembles_full_text(session):
    record = session.send("hello")
    assert record["assistant"] == "".join(DEFAULT_CHUNKS)


def test_async_matches_sync(client, tmp_path):
    session = ghost.GhostSession(client=client, transcript_dir=str(tmp_path))
    seen = []

    async def on_chunk(text):
        seen.append(text)

    record = asyncio.run(session.send_async("hello", on_chunk=on_chunk))
    assert seen == DEFAULT_CHUNKS
    assert record["assistant"] == "".join(DEFAULT_CHUNKS)
    assert read_transcript(session)[0]["turn"] == 1


def test_chunks_are_optional(session):
    session.send("hello")  # no on_chunk; must not blow up
    assert session.turn == 1


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def test_history_keeps_both_turns(session):
    session.send("hello")
    assert [c.role for c in session.contents] == ["user", "model"]
    assert session.turn == 1


def test_history_grows_across_turns(session):
    session.send("one")
    session.send("two")
    assert len(session.contents) == 4
    assert session.client.calls[-1][1] == 3, "the second call sends the prior turns"


def test_baseline_prompt_is_what_gets_sent(session):
    # FakeClient asserts this too; stated here so the intent is obvious.
    session.send("hello")
    assert session.client.calls[0][2] == ghost.SYSTEM_PROMPT


def test_system_prompt_stays_out_of_history(session):
    session.send("hello")
    assert all(ghost.SYSTEM_PROMPT not in c.parts[0].text for c in session.contents)


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------

def build(tmp_path, client, **kwargs):
    """A session whose backoff neither waits nor varies.

    `rand=lambda: 1.0` pins full jitter to its ceiling, so a test can assert on
    the exact delay; `sleep` records instead of waiting.
    """
    slept = []
    kwargs.setdefault("rand", lambda: 1.0)
    session = ghost.GhostSession(client=client, transcript_dir=str(tmp_path),
                                 sleep=slept.append, **kwargs)
    return session, slept


def test_transient_failure_is_retried_without_the_caller_noticing(tmp_path):
    client = FakeClient(fail_times=2)
    session, _ = build(tmp_path, client)

    record = session.send("Bram: I attack the nearest cultist")

    assert len(client.calls) == 3, "two failures, then the one that stuck"
    assert record["turn"] == 1, "failed attempts don't consume turn numbers"
    assert record["assistant"] == "".join(DEFAULT_CHUNKS)
    assert len(read_transcript(session)) == 1, "one turn, one record"


def test_retried_turn_reissues_an_identical_request(tmp_path):
    client = FakeClient(fail_times=2, expect_system=ghost.SYSTEM_PROMPT)
    session, _ = build(tmp_path, client)
    session.send("Bram: I attack")

    # Same model, same history depth, same system prompt on every attempt: a
    # retry must not quietly resend a different question.
    assert len(set(client.calls)) == 1, client.calls


def test_retries_are_finite_and_leave_no_trace(tmp_path):
    client = FakeClient(fail_times=99)
    session, slept = build(tmp_path, client)

    with pytest.raises(genai_errors.ServerError):
        session.send("doomed")

    assert len(client.calls) == ghost.MAX_ATTEMPTS
    assert len(slept) == ghost.MAX_ATTEMPTS - 1, "no wait after the last attempt"
    assert session.contents == [], "the unanswered user turn is dropped"
    assert session.turn == 0
    assert not os.path.exists(session.transcript_path)


def test_non_transient_failure_is_not_retried(tmp_path):
    client = FakeClient(fail_times=99,
                        error=genai_errors.ClientError(400, {"error": {"message": "bad model"}}))
    session, slept = build(tmp_path, client)

    with pytest.raises(genai_errors.ClientError):
        session.send("doomed")

    assert len(client.calls) == 1, "a 400 fails the same way every time"
    assert slept == []


def test_backoff_grows_and_is_capped():
    delays = [ghost.backoff_delay(n, rand=lambda: 1.0) for n in range(1, 9)]
    assert delays[:3] == [1.0, 2.0, 4.0]
    assert delays == sorted(delays), "each wait is at least the last"
    assert max(delays) == ghost.BACKOFF_CAP_S
    # Jitter is real: the same attempt with a different draw waits differently.
    assert ghost.backoff_delay(3, rand=lambda: 0.25) == 1.0


def test_session_sleeps_with_growing_delays(tmp_path):
    session, slept = build(tmp_path, FakeClient(fail_times=2))
    session.send("Bram: I attack")
    assert slept == [1.0, 2.0]


@pytest.mark.parametrize("exc, retried", [
    (genai_errors.ServerError(503, {}), True),
    (genai_errors.ServerError(500, {}), True),
    (genai_errors.ClientError(429, {}), True),
    (genai_errors.ClientError(400, {}), False),
    (genai_errors.ClientError(401, {}), False),
    (genai_errors.ClientError(404, {}), False),
    (httpx.ConnectError("dns"), True),
    (TimeoutError(), True),
    (ValueError("a bug in our own code"), False),
])
def test_transient_classification(exc, retried):
    assert ghost.is_transient(exc) is retried


# ---------------------------------------------------------------------------
# Free-tier quota
#
# The two 429s a free-tier key produces are the same status, the same status
# string and the same message. Everything here turns on reading the quotaId,
# because getting it wrong means either burning the backoff budget on a wall
# that won't move, or declaring a session over when it would have resumed.
# ---------------------------------------------------------------------------

def test_daily_exhaustion_is_not_retried(tmp_path):
    """Four attempts over 35 seconds cannot outlast a quota that resets at midnight."""
    client = FakeClient(fail_times=99, error=quota_exceeded(daily=True))
    session, slept = build(tmp_path, client)

    with pytest.raises(genai_errors.ClientError):
        session.send("Bram: I attack")

    assert len(client.calls) == 1, "waiting cannot clear a spent daily allowance"
    assert slept == []


def test_per_minute_limit_is_still_retried(tmp_path):
    """The burst limit clears itself, so it is worth waiting out."""
    client = FakeClient(fail_times=2, error=quota_exceeded(daily=False))
    session, _ = build(tmp_path, client)

    assert session.send("Bram: I attack")["turn"] == 1
    assert len(client.calls) == 3


def test_google_s_own_retry_delay_wins_over_our_backoff(tmp_path):
    """A per-minute window is longer than BACKOFF_CAP_S.

    Retrying on our own schedule would hit the same closed door three times in
    35 seconds and call the session dead, on a limit that clears in under one.
    """
    client = FakeClient(fail_times=2, error=quota_exceeded(daily=False, retry_delay="27s"))
    session, slept = build(tmp_path, client)
    session.send("Bram: I attack")

    # 27s asked, plus the jittered backoff (rand is pinned to 1.0 in build()).
    assert slept == [27.0 + 1.0, 27.0 + 2.0]
    assert all(s > ghost.BACKOFF_CAP_S for s in slept)


def test_an_absurd_retry_delay_cannot_park_the_session(tmp_path):
    client = FakeClient(fail_times=1, error=quota_exceeded(daily=False, retry_delay="86400s"))
    session, slept = build(tmp_path, client)
    session.send("Bram: I attack")

    assert slept == [ghost.RETRY_AFTER_CAP_S]


def test_an_unlabelled_429_is_treated_as_a_burst_limit(tmp_path):
    """Conservative on purpose: waiting is cheap, ending a session wrongly isn't."""
    bare = genai_errors.ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED"}})
    assert ghost.is_quota_error(bare) is True
    assert ghost.is_quota_exhausted(bare) is False
    assert ghost.is_transient(bare) is True


@pytest.mark.parametrize("details", [
    None, "not a dict", {}, {"error": "not a dict"},
    {"error": {"details": "not a list"}},
    {"error": {"details": [{"@type": "...QuotaFailure", "violations": "not a list"}]}},
    {"error": {"details": [{"@type": "...RetryInfo", "retryDelay": "soon"}]}},
])
def test_malformed_error_bodies_do_not_raise(details):
    """A parser that crashes while explaining a failure is worse than none."""
    exc = genai_errors.ClientError(429, details if isinstance(details, dict) else {})
    exc.details = details  # whatever google sent, including nothing usable

    assert ghost.quota_violations(exc) == []
    assert ghost.retry_after(exc) is None
    assert ghost.is_quota_exhausted(exc) is False
    ghost.describe(exc)  # must produce a line rather than blow up


def test_the_reason_is_stated_in_one_line_not_a_json_dump():
    """`str(APIError)` is the whole response body; that buries the one word
    that matters behind a paragraph of quota metadata."""
    daily = ghost.describe(quota_exceeded(daily=True))
    burst = ghost.describe(quota_exceeded(daily=False))

    assert daily == "google says daily allowance spent"
    assert burst == "google says rate limited"
    assert "@type" not in daily and "\n" not in daily


def test_quota_block_names_google_the_limit_and_the_transcript(tmp_path, monkeypatch):
    """The failure has to read as 'google stopped you', not 'the code broke'."""
    screen, records = run_cli(
        tmp_path, monkeypatch, ["Bram: I attack", ""],
        fail_times=1, error=quota_exceeded(daily=True))

    plain = "".join(line.plain for line in AnsiDecoder().decode(screen))
    assert "Google is refusing this key" in plain
    assert "free-tier daily allowance is spent" in plain
    assert "GenerateRequestsPerDayPerProjectPerModel-FreeTier" in plain
    assert "midnight Pacific" in plain
    assert ".jsonl" in plain, "it has to name where the play went"
    assert "[Error during generation" not in plain, "the raw JSON dump is the thing being replaced"


def test_the_quota_block_gives_a_clock_time_not_just_a_rule(tmp_path, monkeypatch):
    """"Resets at midnight Pacific" still needs mental arithmetic at 00:40 in a
    zone three hours ahead — which is exactly when it gets read."""
    session = ghost.GhostSession(client=FakeClient(), transcript_dir=str(tmp_path))
    screen = io.StringIO()
    monkeypatch.setattr(ghost, "console", Console(width=100, file=screen))
    now = datetime.datetime(2026, 8, 14, 0, 41, tzinfo=datetime.timezone(
        datetime.timedelta(hours=-4)))

    ghost.report_failure(quota_exceeded(daily=True), session, now=now)

    out = screen.getvalue()
    assert "03:00" in out, "the local clock time it comes back"
    assert "in 2h 19m" in out, "and how long that is from now"


def test_the_failure_report_reassures_that_play_so_far_is_saved(tmp_path, monkeypatch):
    """Losing forty turns is the fear a quota wall triggers. Answer it outright."""
    session = ghost.GhostSession(client=FakeClient(), transcript_dir=str(tmp_path))
    for _ in range(3):
        session.send("Bram: I attack")

    screen = io.StringIO()
    monkeypatch.setattr(ghost, "console", Console(width=100, file=screen))
    ghost.report_failure(quota_exceeded(daily=True), session)

    assert "3 turns are already saved" in screen.getvalue()
    assert "nothing is lost" in screen.getvalue()


def test_an_overloaded_model_is_not_reported_as_a_key_problem(tmp_path, monkeypatch):
    """The banner is the first thing read. A 503 wearing 'Google is refusing
    this key' sends you to the dashboard instead of just waiting."""
    session = ghost.GhostSession(client=FakeClient(), transcript_dir=str(tmp_path))
    screen = io.StringIO()
    monkeypatch.setattr(ghost, "console", Console(width=100, file=screen))

    ghost.report_failure(refused(503, "UNAVAILABLE", "high demand"), session)

    out = screen.getvalue()
    assert "refusing this key" not in out
    assert "overloaded" in out


def test_a_dead_end_does_not_invite_a_pointless_keypress(tmp_path, monkeypatch):
    """After a 404 the same turn cannot succeed, so 'press Enter to retry' is a
    lie. The quota wording — try again or end the session — is the honest one."""
    screen, _ = run_cli(tmp_path, monkeypatch, ["Bram: I attack", ""],
                        fail_times=1, error=refused(404, "NOT_FOUND"))

    plain = "".join(line.plain for line in AnsiDecoder().decode(screen))
    assert "Ctrl-D to end the session" in plain
    assert "Press Enter to retry the same turn" not in plain


def test_a_transient_failure_still_invites_a_plain_retry(tmp_path, monkeypatch):
    """The mirror of the above: a 503 really might work next time.

    fail_times matches MAX_ATTEMPTS so the automated retries are spent and the
    human-facing prompt is reached, with the next call succeeding so a
    transcript still lands. Backoff is flattened — this asserts on wording, and
    should not spend seven seconds sleeping to do it.
    """
    monkeypatch.setattr(ghost, "backoff_delay", lambda *a, **k: 0.0)
    screen, _ = run_cli(tmp_path, monkeypatch, ["Bram: I attack", ""],
                        fail_times=ghost.MAX_ATTEMPTS, error=refused(503, "UNAVAILABLE"))

    plain = "".join(line.plain for line in AnsiDecoder().decode(screen))
    assert "Press Enter to retry the same turn" in plain


def test_the_retry_notice_says_what_is_wrong_in_words(tmp_path, monkeypatch):
    """Mid-stream, "google returned 503 UNAVAILABLE" reads like something to go
    debug. It is something to sit through."""
    screen, _ = run_cli(tmp_path, monkeypatch, ["Bram: I attack", ""],
                        fail_times=1, error=refused(503, "UNAVAILABLE"))

    plain = "".join(line.plain for line in AnsiDecoder().decode(screen))
    assert "retry 1/" in plain
    assert "the model is overloaded" in plain


def test_a_spent_quota_still_leaves_the_session_open(tmp_path, monkeypatch):
    """History lives only in this process — ending a 40-turn session to wait
    out a reset throws the conversation away for nothing."""
    screen, records = run_cli(
        tmp_path, monkeypatch, ["Bram: I attack", ""],
        fail_times=1, error=quota_exceeded(daily=True))

    plain = "".join(line.plain for line in AnsiDecoder().decode(screen))
    assert "Ctrl-D to end the session" in plain
    # The roster turn died, was offered again, and landed.
    assert [r["turn"] for r in records] == [1, 2]


# ---------------------------------------------------------------------------
# explain(): the failure, as data a player can act on
#
# The bar for every case is the same — after reading it, does the player know
# whether to wait, to fix something, or to stop? A line that only restates the
# status code fails that bar even when it is accurate.
# ---------------------------------------------------------------------------

def test_a_spent_daily_allowance_says_waiting_is_the_only_fix():
    """The one 429 where retrying is exactly the wrong instinct."""
    result = ghost.explain(quota_exceeded(daily=True))

    assert result.retryable is False
    assert "daily allowance" in result.headline
    assert "not clear this" in result.advice


def test_a_burst_limit_says_the_opposite():
    """Same status code, same message, opposite advice — so they must differ."""
    daily = ghost.explain(quota_exceeded(daily=True))
    burst = ghost.explain(quota_exceeded(daily=False))

    assert burst.retryable is True and daily.retryable is False
    assert burst.headline != daily.headline
    assert burst.wait_s == 27.0, "google's own retryDelay is what to wait"


def test_the_quota_explanation_names_the_model_and_the_number():
    """The daily cap is per-model and the values differ enormously between
    them — 500 on one, 20 on another. 'Which model, and how many' is the
    difference between a workable plan and a wasted day."""
    facts = dict(ghost.explain(quota_exceeded(daily=True, model="gemini-3.7-flash")).facts)

    assert facts["model"] == "gemini-3.7-flash"
    assert facts["limit"] == "50"
    assert "PerDay" in facts["quota"]


def test_the_daily_reset_is_reported_in_the_callers_own_timezone():
    """Google's window turns over at midnight Pacific, so a caller east of it
    can be on tomorrow's date and still blocked. Reporting Pacific time would
    reproduce exactly that confusion."""
    from zoneinfo import ZoneInfo

    # 00:41 in Atlantic time on the 14th is still 21:41 Pacific on the 13th.
    now = datetime.datetime(2026, 8, 14, 0, 41, tzinfo=datetime.timezone(
        datetime.timedelta(hours=-4)))
    result = ghost.explain(quota_exceeded(daily=True), now=now)

    assert result.resets_at is not None
    assert result.resets_at.utcoffset() == now.utcoffset(), "answer in the caller's zone"
    assert (result.resets_at.hour, result.resets_at.minute) == (3, 0)
    assert result.resets_at.astimezone(ZoneInfo(ghost.PACIFIC)).hour == 0


def test_the_reset_is_the_next_one_not_one_that_already_passed():
    """Half an hour past midnight Pacific, the answer is tomorrow, not today."""
    pacific_0030 = datetime.datetime(2026, 8, 14, 0, 30, tzinfo=datetime.timezone(
        datetime.timedelta(hours=-7)))
    resets = ghost.daily_reset_after(pacific_0030)

    assert resets > pacific_0030
    assert (resets - pacific_0030) > datetime.timedelta(hours=23)


def test_an_overloaded_model_is_not_blamed_on_the_key():
    """A 503 during a quota scare reads as 'blocked again' unless it says
    otherwise. It is google-side load and it passes on its own."""
    result = ghost.explain(refused(503, "UNAVAILABLE", "high demand"))

    assert result.retryable is True
    assert "overloaded" in result.headline
    assert "not a problem with this key" in result.advice


def test_an_unknown_model_points_at_the_setting_to_change():
    result = ghost.explain(refused(404, "NOT_FOUND"))

    assert result.retryable is False
    assert "MODEL" in result.advice and ghost.MODEL in result.advice


def test_a_rejected_key_points_at_the_env_file():
    for code in (401, 403):
        result = ghost.explain(refused(code, "UNAUTHENTICATED"))
        assert result.retryable is False
        assert "GEMINI_API_KEY" in result.advice


def test_a_malformed_request_says_it_is_a_bug_not_a_blip():
    """400 is the case where waiting is pure waste: the identical retry fails
    identically."""
    result = ghost.explain(refused(400, "INVALID_ARGUMENT", "bad contents"))

    assert result.retryable is False
    assert "bug to fix" in result.advice


def test_a_network_failure_says_nothing_was_spent():
    """Never reaching google means no quota was consumed — worth saying when
    the player is rationing a daily allowance."""
    result = ghost.explain(httpx.ConnectError("dns went away"))

    assert result.retryable is True
    assert "reach google" in result.headline
    assert "nothing was spent" in result.advice


def test_retryability_agrees_with_the_retry_policy():
    """Two places decide whether to re-issue. If they disagree, one of them is
    lying to the player."""
    for exc in [quota_exceeded(daily=True), quota_exceeded(daily=False),
                refused(503, "UNAVAILABLE"), refused(500, "INTERNAL"),
                refused(404, "NOT_FOUND"), refused(400, "INVALID_ARGUMENT"),
                refused(403, "PERMISSION_DENIED"), httpx.ConnectError("x")]:
        assert ghost.explain(exc).retryable is ghost.is_transient(exc), exc


@pytest.mark.parametrize("details", [
    None, "not a dict", {}, {"error": "not a dict"},
    {"error": {"details": "not a list"}},
    {"error": {"details": [{"@type": "...QuotaFailure", "violations": [42]}]}},
    {"error": {"details": [{"@type": "...QuotaFailure",
                            "violations": [{"quotaDimensions": "not a dict"}]}]}},
])
def test_explaining_a_malformed_body_still_produces_advice(details):
    """An explainer that dies while explaining leaves the caller with less than
    it started with."""
    exc = genai_errors.ClientError(429, details if isinstance(details, dict) else {})
    exc.details = details

    result = ghost.explain(exc)
    assert result.headline and result.advice


def test_a_failure_that_cannot_be_parsed_still_carries_its_own_text():
    """The last resort has to be better than silence."""
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("even stringifying me fails")

    result = ghost.explain(Hostile())
    assert result.headline
    assert result.retryable is False


def test_explain_never_styles_anything():
    """Same rule as the transcript: data here, styling at print time only."""
    result = ghost.explain(quota_exceeded(daily=True))
    blob = result.headline + result.advice + "".join(v for _, v in result.facts)

    assert "\x1b[" not in blob and "[bold" not in blob


# ---------------------------------------------------------------------------
# Speaker prefix
#
# A display-time check only: `speaker_of` never edits the turn, it just tells
# the CLI whether to warn. What reaches the model is always what was typed.
# ---------------------------------------------------------------------------

NAMES = {"Bram", "Nix", "Vela", "Oro"}


@pytest.mark.parametrize("typed, speaker", [
    ("Bram: I attack the nearest cultist", "Bram"),
    ("Nix:I slip into the shadows", "Nix"),          # no space after the colon
    ("  Vela : I cast bless", "Vela"),               # sloppy spacing still reads
    ("I attack the cultist", None),                  # no prefix at all
    ("Greta: what do you want?", None),              # an NPC is not a player
    ("The door: is it locked?", None),               # a colon mid-sentence
    ("", None),
])
def test_speaker_of(typed, speaker):
    assert ghost.speaker_of(typed, NAMES) == speaker


def test_speaker_prefix_is_never_stripped_from_what_is_sent(tmp_path):
    """The prefix is data, not syntax — the model has to see it to attribute."""
    client = FakeClient()
    session, _ = build(tmp_path, client)
    record = session.send("Bram: I attack the nearest cultist")

    assert record["user"] == "Bram: I attack the nearest cultist"
    assert session.contents[0].parts[0].text == "Bram: I attack the nearest cultist"


def test_the_convention_is_never_explained_to_the_model():
    """Attribution is a CLI affordance, and has to stay one.

    party.txt is sent verbatim as turn 1, so anything written there is something
    the naive baseline was told. Declaring the notation measurably changed how
    the GM behaved — it began addressing characters individually and calling for
    initiative, which is one of the failures phase 0 exists to observe. The model
    sees bare `Bram: ...` prefixes and gets no help interpreting them.
    """
    with open(PARTY_PATH, encoding="utf-8") as f:
        text = f.read().lower()

    for leak in ("name: action", "speak as", "prefix", "attributable", "initiative"):
        assert leak not in text, f"party.txt now coaches the GM: {leak!r}"


# ---------------------------------------------------------------------------
# Turn tally
#
# failures.md lines are `s<session_id>/t<turn>`, so the CLI has to show the turn
# number while the turn is on screen. These drive the real `main()` and decode
# the ANSI back out, because "it stands out" is a claim about what the terminal
# actually paints, not about what the source says.
# ---------------------------------------------------------------------------

def run_cli(tmp_path, monkeypatch, typed, scripts=None, **fake_kwargs):
    """Play a whole CLI session; return (what the terminal showed, transcript).

    force_terminal, because rich strips styling when it isn't writing to a TTY
    and the tally would then be indistinguishable from surrounding text.
    """
    monkeypatch.chdir(tmp_path)  # party.txt and transcripts/ are relative paths
    shutil.copy(PARTY_PATH, tmp_path / "party.txt")

    fake = FakeClient(scripts=scripts or [["Scene set."], ["The GM replies."]],
                      **fake_kwargs)
    monkeypatch.setattr(ghost.genai, "Client", fake.factory())
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(typed + ["exit"]) + "\n"))

    screen = io.StringIO()
    monkeypatch.setattr(ghost, "console", Console(
        highlight=False, soft_wrap=True, force_terminal=True, width=100, file=screen))

    ghost.main()

    written = list((tmp_path / "transcripts").glob("*.jsonl"))
    assert len(written) == 1
    with open(written[0], encoding="utf-8") as f:
        return screen.getvalue(), [json.loads(line) for line in f if line.strip()]


def highlighted(screen):
    """Every run of text the terminal would paint on a background colour.

    The tally is the only thing that gets a background, so this doubles as the
    bleed check: a style set on a Text rather than an appended span survives
    concatenation, which silently paints the prompt and the GM label too.
    """
    runs = []
    for line in AnsiDecoder().decode(screen):
        for span in line.spans:
            if span.style.bgcolor is not None:
                runs.append(line.plain[span.start:span.end])
    return runs


def test_the_tally_names_the_turn_the_record_will_carry(tmp_path, monkeypatch):
    screen, records = run_cli(tmp_path, monkeypatch, ["Bram: I attack", "Nix: I hide"])

    # Turn 1 is the roster. Each turn is tagged twice — once on the prompt, once
    # on the GM's reply — so the number is on screen whichever half you're
    # reading when the failure shows up.
    assert [r["turn"] for r in records] == [1, 2, 3]
    assert highlighted(screen) == [
        " t1 ", " t1 ",   # roster
        " t2 ", " t2 ",
        " t3 ", " t3 ",
        " t4 ",           # the prompt that was answered with "exit"
        " 3 turns ",      # sign-off tally
    ]


def test_the_tally_stands_out_from_every_other_speaker(tmp_path, monkeypatch):
    """A colour already used by the GM or a party member wouldn't be findable."""
    taken = {ghost.GM_STYLE, ghost.YOU_STYLE, ghost.WARN_STYLE, *ghost.PLAYER_PALETTE}
    assert ghost.TURN_STYLE not in taken

    tally = Style.parse(ghost.TURN_STYLE)
    assert tally.bgcolor is not None, "a foreground colour alone doesn't stand out"
    for other in taken:
        assert Style.parse(other).color != tally.color or Style.parse(other).bgcolor


def test_a_failed_turn_does_not_advance_the_tally(tmp_path, monkeypatch):
    """Nothing was committed, so the number is still free — and must be reused.

    Otherwise the tally drifts ahead of the transcript and every failures.md
    line written after the first outage points at the wrong record.
    """
    # A 400 is not transient, so it raises without spending retries; the CLI
    # then offers the same turn again. FakeClient fails the first N calls, so
    # it's the roster turn that dies here and gets replayed by hand.
    bad_request = genai_errors.ClientError(400, {"error": {"message": "nope"}})
    screen, records = run_cli(
        tmp_path, monkeypatch,
        # The two blanks answer "Press Enter to retry the same turn".
        ["", "", "Bram: I attack"],
        scripts=[["Scene set."], ["Recovered."]],
        fail_times=2, error=bad_request)

    assert [r["turn"] for r in records] == [1, 2]
    assert highlighted(screen) == [
        " t1 ", " t1 ",   # roster: the prompt, then the reply that 400'd
        " t1 ",           # replay — no prompt, nothing was retyped, still turn 1
        " t1 ",           # replay again, and this one lands
        " t2 ", " t2 ",
        " t3 ",           # the next free number, at the prompt "exit" answered
        " 2 turns ",
    ]


def test_the_tally_never_reaches_the_transcript(tmp_path, monkeypatch):
    """Display only — the same rule the colouring follows."""
    _, records = run_cli(tmp_path, monkeypatch, ["Bram: I attack"])

    for record in records:
        blob = record["user"] + record["assistant"]
        assert "\x1b" not in blob
        assert f" t{record['turn']} " not in blob


def test_the_sign_off_counts_recorded_turns_against_the_target(tmp_path, monkeypatch):
    """A session that came up short should say so before you close the window."""
    screen, records = run_cli(tmp_path, monkeypatch, ["Bram: I attack"])

    plain = "".join(line.plain for line in AnsiDecoder().decode(screen))
    assert f" {len(records)} turns " in plain
    assert f"{ghost.SESSION_TURN_TARGET - len(records)} turns short" in plain


# ---------------------------------------------------------------------------
# Retrying mid-stream
# ---------------------------------------------------------------------------

def test_mid_stream_failure_discards_the_partial_reply(tmp_path):
    """A retry restarts the reply; the abandoned half must not survive."""
    client = FakeClient(fail_times=1, fail_mid_stream=1)
    session, _ = build(tmp_path, client)

    streamed = []
    record = session.send("Bram: I attack", on_chunk=streamed.append)

    # The display saw the abandoned fragment, but the record holds one clean reply.
    assert streamed[0] == DEFAULT_CHUNKS[0]
    assert record["assistant"] == "".join(DEFAULT_CHUNKS)
    assert record["assistant"].count(DEFAULT_CHUNKS[0]) == 1, "no doubled text"
    assert session.contents[-1].parts[0].text == record["assistant"]


def test_on_retry_reports_what_the_display_must_discard(tmp_path):
    client = FakeClient(fail_times=1, fail_mid_stream=1)
    session, _ = build(tmp_path, client)

    seen = []
    session.send("Bram: I attack", on_retry=lambda *a: seen.append(a))

    assert len(seen) == 1
    attempt, error, partial = seen[0]
    assert attempt == 1
    assert isinstance(error, genai_errors.ServerError)
    assert partial == DEFAULT_CHUNKS[0], "exactly the text already shown"


def test_async_send_retries_too(tmp_path):
    client = FakeClient(fail_times=2, fail_mid_stream=1)
    # rand=0 so the real asyncio.sleep the async path uses returns immediately.
    session, _ = build(tmp_path, client, rand=lambda: 0.0)

    seen = []

    async def on_retry(*args):
        seen.append(args)

    record = asyncio.run(session.send_async("Nix: I scout ahead", on_retry=on_retry))

    assert len(client.calls) == 3
    assert [a[0] for a in seen] == [1, 2]
    assert record["assistant"] == "".join(DEFAULT_CHUNKS)


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

def test_transcript_schema(session):
    session.send("hello")
    record = read_transcript(session)[0]
    assert set(record) == TRANSCRIPT_KEYS


def test_transcript_appends_one_record_per_turn(session):
    session.send("one")
    session.send("two")
    records = read_transcript(session)
    assert [r["turn"] for r in records] == [1, 2]
    assert [r["user"] for r in records] == ["one", "two"]


def test_transcript_carries_metrics(session):
    record = session.send("hello")
    assert (record["input_tokens"], record["output_tokens"], record["total_tokens"]) == (120, 40, 160)
    assert record["ttft_ms"] is not None
    assert record["latency_ms"] >= record["ttft_ms"]


def test_transcript_text_is_unstyled(session):
    session.send("hello")
    with open(session.transcript_path, encoding="utf-8") as f:
        assert "\x1b[" not in f.read(), "display styling must never reach the data"


def test_transcript_path_matches_session_id(session):
    assert session.transcript_path.endswith(f"{session.session_id}.jsonl")
    assert len(session.session_id) == 8


def test_sessions_get_their_own_transcripts(client, tmp_path):
    a = ghost.GhostSession(client=client, transcript_dir=str(tmp_path))
    b = ghost.GhostSession(client=client, transcript_dir=str(tmp_path))
    assert a.transcript_path != b.transcript_path


def test_usage_only_chunk_does_not_set_ttft(tmp_path):
    # A stream that never produces text should report ttft as unknown, not 0.
    client = FakeClient(chunks=[], usage=Usage())
    session = ghost.GhostSession(client=client, transcript_dir=str(tmp_path))
    record = session.send("hello")
    assert record["ttft_ms"] is None
    assert record["assistant"] == ""


# ---------------------------------------------------------------------------
# Roster parsing — shared with visual/ and prep/
# ---------------------------------------------------------------------------

def test_parses_the_real_party_file():
    members = ghost.parse_party(ghost.read_party_file(PARTY_PATH))
    assert [m["name"] for m in members] == ["Bram", "Nix", "Vela", "Oro"]
    assert members[0] == {"name": "Bram", "description": "human fighter", "ac": 18, "hp": 31}


def test_ignores_prose_lines():
    text = "Party of 1, D&D 5e, level 3:\n- Bram, human fighter. AC 18, HP 31.\n\nBegin."
    assert [m["name"] for m in ghost.parse_party(text)] == ["Bram"]


def test_skips_duplicate_names():
    assert len(ghost.parse_party("- Bram, fighter.\n- Bram, again.")) == 1


def test_invents_no_defaults():
    member = ghost.parse_party("- Zed, mystery man.")[0]
    assert (member["ac"], member["hp"]) == (None, None)


def test_accepts_asterisk_bullets():
    assert ghost.parse_party("* Nix, halfling rogue. AC 15, HP 24.")[0]["name"] == "Nix"


def test_missing_party_file_is_none(tmp_path):
    assert ghost.read_party_file(str(tmp_path / "nope.txt")) is None
