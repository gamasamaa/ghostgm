"""The engine: streaming, history, metrics, transcript, and the roster parser.

Everything downstream (visual/, prep/) depends on these staying true — the
transcript schema in particular, since it's the data the whole project exists
to produce.
"""

import asyncio
import json
import os

import httpx
import pytest
from fakes import DEFAULT_CHUNKS, FakeClient, Usage
from google.genai import errors as genai_errors

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
