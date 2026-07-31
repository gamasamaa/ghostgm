"""The engine: streaming, history, metrics, transcript, and the roster parser.

Everything downstream (visual/, prep/) depends on these staying true — the
transcript schema in particular, since it's the data the whole project exists
to produce.
"""

import asyncio
import json
import os

import pytest
from fakes import DEFAULT_CHUNKS, FakeClient, Usage

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

def test_failed_turn_leaves_history_clean(client, tmp_path):
    session = ghost.GhostSession(client=FakeClient(fail_times=1), transcript_dir=str(tmp_path))
    with pytest.raises(RuntimeError):
        session.send("doomed")

    assert session.contents == [], "the unanswered user turn is dropped"
    assert session.turn == 0


def test_failed_turn_writes_no_transcript(tmp_path):
    session = ghost.GhostSession(client=FakeClient(fail_times=1), transcript_dir=str(tmp_path))
    with pytest.raises(RuntimeError):
        session.send("doomed")
    assert not os.path.exists(session.transcript_path)


def test_retry_after_failure_succeeds(tmp_path):
    session = ghost.GhostSession(client=FakeClient(fail_times=1), transcript_dir=str(tmp_path))
    with pytest.raises(RuntimeError):
        session.send("same turn")

    record = session.send("same turn")
    assert record["turn"] == 1, "the failed attempt didn't consume a turn number"
    assert len(read_transcript(session)) == 1


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
