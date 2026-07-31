"""Phase 0 conformance: the visual layer must be invisible to the GM.

The baseline experiment only holds if a session played through the web UI asks
the model exactly what a bare `python ghost.py` session would. This drives both
interfaces through the same fake client with the same inputs and compares the
transcripts they produce.

If this fails, the visual layer has started influencing the GM — a prompt
tweak, a different model, an extra turn, injected board state — and the data it
produces is no longer comparable to a CLI baseline.
"""

import json
import shutil

import pytest
from fakes import FakeClient
from fastapi.testclient import TestClient
from support import drain_turn, receive_json

import ghost

# Fields that legitimately differ between two runs of anything.
VOLATILE = {"session_id", "ts", "latency_ms", "ttft_ms"}

PLAYER_INPUTS = [
    "Bram attacks the cultist leader.",
    "Nix looks for a way around the flank.",
]

SCRIPTS = [
    ["The sewer tunnel drips. Four figures move through the dark."],
    ["Bram swings and the Cultist Leader takes 9 damage."],
    ["Nix slips into the shadows along the eastern wall."],
]


def comparable(path):
    """Transcript records with the run-to-run noise stripped out."""
    with open(path, encoding="utf-8") as f:
        return [{k: v for k, v in json.loads(line).items() if k not in VOLATILE}
                for line in f if line.strip()]


@pytest.fixture
def party_file(tmp_path):
    """A copy of the real roster, so both runs open on identical bytes."""
    src = ghost.PARTY_FILE
    dst = tmp_path / "party.txt"
    shutil.copy(src if "/" in src else f"./{src}", dst)
    return dst


def run_cli(tmp_path, monkeypatch, party_file):
    """Drive ghost.main() with scripted input, in an isolated working dir."""
    monkeypatch.chdir(tmp_path)  # party.txt and transcripts/ are relative paths
    fake = FakeClient(scripts=SCRIPTS, expect_system=ghost.SYSTEM_PROMPT)
    monkeypatch.setattr(ghost.genai, "Client", fake.factory())

    typed = list(PLAYER_INPUTS) + ["exit"]
    monkeypatch.setattr(ghost.console, "input", lambda *a, **k: typed.pop(0))

    ghost.main()

    written = list((tmp_path / "transcripts").glob("*.jsonl"))
    assert len(written) == 1
    return comparable(written[0]), fake


def run_visual(tmp_path, monkeypatch, party_file):
    """Drive the web UI with the same inputs, over a websocket."""
    import visual.app as va

    fake = FakeClient(scripts=SCRIPTS, expect_system=ghost.SYSTEM_PROMPT)
    monkeypatch.setattr(ghost.genai, "Client", fake.factory())
    monkeypatch.setattr(va, "TRANSCRIPT_DIR", str(tmp_path / "visual_transcripts"))
    monkeypatch.setattr(va, "PARTY_PATH", str(party_file))
    va.sessions.clear()

    client = TestClient(va.app)
    with client.websocket_connect("/ws") as ws:
        init = receive_json(ws, expecting="the init payload")
        # The roster turn is unprompted; a missing one must fail here rather
        # than leave the suite waiting for a message that never comes.
        drain_turn(ws)
        for text in PLAYER_INPUTS:
            ws.send_json({"text": text})
            drain_turn(ws)

    session = va.sessions[init["session_id"]]
    va.sessions.clear()
    return comparable(session.ghost.transcript_path), fake


def test_visual_and_cli_produce_identical_transcripts(tmp_path, monkeypatch, party_file):
    visual_records, visual_fake = run_visual(tmp_path, monkeypatch, party_file)
    cli_records, cli_fake = run_cli(tmp_path, monkeypatch, party_file)

    assert visual_records == cli_records
    assert len(visual_records) == 3, "roster turn plus two player turns"


def test_both_interfaces_send_the_same_prompt_and_model(tmp_path, monkeypatch, party_file):
    _, visual_fake = run_visual(tmp_path, monkeypatch, party_file)
    _, cli_fake = run_cli(tmp_path, monkeypatch, party_file)

    assert visual_fake.calls == cli_fake.calls, "same model, history depth, system prompt"
    assert all(call[2] == ghost.SYSTEM_PROMPT for call in visual_fake.calls)


def test_visual_sends_nothing_extra_to_the_model(tmp_path, monkeypatch, party_file):
    """Board state must never leak into the conversation."""
    _, visual_fake = run_visual(tmp_path, monkeypatch, party_file)

    # One call per turn — no hidden extraction or summarisation calls on the
    # GM's own client, and no turns the player didn't take.
    assert len(visual_fake.calls) == 3
    # History grows 2 per turn (user + model) and nothing else is spliced in.
    assert [call[1] for call in visual_fake.calls] == [1, 3, 5]
