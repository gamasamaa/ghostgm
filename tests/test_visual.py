"""The visual layer: board state, the narration reader, and the server.

The load-bearing claim here is separation — the visual layer may watch the GM
and track state beside it, but it must never change what the GM is asked. The
fake client asserts the baseline prompt on every call.
"""

import json
import os

import pytest
from fakes import FakeClient
from fastapi.testclient import TestClient
from support import drain_turn, receive_json

import ghost
from visual.narration import HPExtractor
from visual.state import StateManager

PARTY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "party.txt")

OPENING = "The sewer tunnel drips. Four figures move through the dark."
REPLY = "Bram swings and the Cultist Leader takes 9 damage. Vela heals Bram for 5."


# ---------------------------------------------------------------------------
# Board state
# ---------------------------------------------------------------------------

@pytest.fixture
def board():
    return StateManager(PARTY_PATH)


def test_board_starts_from_the_roster(board):
    assert sorted(board.state.tokens) == ["bram", "nix", "oro", "vela"]
    assert board.get_token("bram").hp == 31
    assert board.get_token("bram").ac == 18


def test_enemies_are_seeded(board):
    assert board.get_token("cultist_1").is_enemy
    assert board.get_token("cultist_1").hp == 22


def test_reset_restores_both_sides(board):
    board.update_hp("bram", -20)
    board.update_hp("cultist_1", -20)
    board.move_token("bram", 9, 9)

    board.reset_from_party()

    assert board.get_token("bram").hp == 31
    assert board.get_token("cultist_1").hp == 22, "enemies reset too, not just the party"
    assert (board.get_token("bram").x, board.get_token("bram").y) == (4, 6)


def test_hp_is_clamped(board):
    assert board.update_hp("nix", -999).hp == 0
    assert board.update_hp("nix", 999).hp == 24


def test_moves_are_clamped_to_the_grid(board):
    token = board.move_token("nix", 99, -5)
    assert (token.x, token.y) == (board.state.grid_cols - 1, 0)


def test_unknown_tokens_are_refused(board):
    assert board.update_hp("gandalf", -1) is None
    assert board.move_token("gandalf", 1, 1) is None


# ---------------------------------------------------------------------------
# Narration reader
# ---------------------------------------------------------------------------

@pytest.fixture
def extractor(board):
    return HPExtractor(board.all_tokens())


@pytest.mark.parametrize("text,expected", [
    ("Bram takes 6 damage from the blade.", [("bram", -6)]),
    # The attacker must not be charged for the target's damage.
    ("Bram swings his longsword and the Cultist Leader takes 9 damage.", [("cultist_1", -9)]),
    ("Bram takes 6 damage. Then Bram takes 4 more damage.", [("bram", -6), ("bram", -4)]),
    ("Vela heals Bram for 8 hit points.", [("bram", 8)]),
    ("The cultist strikes.\nBram takes 7 damage.", [("bram", -7)]),
    ("Nix suffers 5 points of piercing damage, and Oro takes 3 fire damage.",
     [("nix", -5), ("oro", -3)]),
    ("Vela regains 10 hit points.", [("vela", 10)]),
    ("Nix dodges; the arrow misses entirely.", []),
    ("Bram's blade misses the Sewer Cultist.", []),
    # The subject can sit mid-sentence — GM prose rarely opens with the name.
    ("Vela watches in horror as Bram takes 6 damage.", [("bram", -6)]),
    ("With a sickening crunch Nix takes 4 damage.", [("nix", -4)]),
    ("Rolling a 17, the Sewer Cultist takes 5 damage.", [("cultist_2", -5)]),
    # A possessive breaks the adjacency, so the gear takes the hit, not the PC.
    ("Bram's shield takes 9 damage.", []),
])
def test_narration_reads_hp_changes(extractor, text, expected):
    assert [(d.token_id, d.hp_delta) for d in extractor.extract(text)] == expected


def test_deltas_carry_their_evidence(extractor):
    delta = extractor.extract("Bram takes 6 damage.")[0]
    assert "takes 6 damage" in delta.phrase


def test_deltas_come_back_in_narrated_order(extractor):
    deltas = extractor.extract("Vela heals Bram for 5. Later, Nix takes 3 damage.")
    assert [d.token_id for d in deltas] == ["bram", "nix"]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

@pytest.fixture
def app(monkeypatch, tmp_path):
    """The visual app wired to a fake Gemini client and a throwaway transcript dir."""
    import visual.app as va

    # Call 1 is the roster turn (scene-setting, no damage); call 2 onward is play.
    fake = FakeClient(scripts=[[OPENING], [REPLY[:20], REPLY[20:]]],
                      expect_system=ghost.SYSTEM_PROMPT)
    monkeypatch.setattr(ghost.genai, "Client", fake.factory())
    monkeypatch.setattr(va, "TRANSCRIPT_DIR", str(tmp_path))
    va.sessions.clear()

    va.fake = fake
    yield va
    va.sessions.clear()


@pytest.fixture
def client(app):
    return TestClient(app.app)


@pytest.fixture
def live(client):
    """An open socket, past the init payload and the automatic roster turn."""
    with client.websocket_connect("/ws") as ws:
        init = receive_json(ws)
        drain_turn(ws)  # the GM's opening on party.txt
        yield ws, init


def play_turn(ws, text="Bram attacks the cultist."):
    ws.send_json({"text": text})
    return drain_turn(ws)


# --- the opening roster turn -----------------------------------------------

def test_new_session_opens_on_the_roster(client, app):
    """Without this the GM invents its own party and the board can never match."""
    with client.websocket_connect("/ws") as ws:
        init = receive_json(ws)
        notice = receive_json(ws)
        assert notice["type"] == "notice" and "party.txt" in notice["text"]

        messages = drain_turn(ws)
        assert messages[0]["type"] == "stream_start"
        assert messages[-1]["type"] == "stream_done"
        assert messages[-1]["metrics"]["turn"] == 1

    session = app.sessions[init["session_id"]]
    sent = session.ghost.contents[0].parts[0].text
    assert "Bram" in sent and "Nix" in sent, "the GM got the actual roster"
    assert sent == ghost.read_party_file(PARTY_PATH)


def test_roster_turn_is_recorded_like_any_other(client, app):
    with client.websocket_connect("/ws") as ws:
        init = receive_json(ws)
        drain_turn(ws)

    session = app.sessions[init["session_id"]]
    with open(session.ghost.transcript_path, encoding="utf-8") as f:
        first = json.loads(f.readline())
    assert first["turn"] == 1
    assert first["user"] == ghost.read_party_file(PARTY_PATH)


def test_player_turns_follow_the_roster(live, app):
    ws, init = live
    play_turn(ws)
    session = app.sessions[init["session_id"]]
    assert session.ghost.turn == 2, "the roster turn counts, the player's is the second"


def test_reconnect_does_not_replay_the_roster(client, app):
    with client.websocket_connect("/ws") as ws:
        sid = receive_json(ws)["session_id"]
        drain_turn(ws)

    with client.websocket_connect(f"/ws?session_id={sid}") as ws:
        assert receive_json(ws)["session_id"] == sid
        # No roster turn this time: the next message is only whatever we ask for.
        play_turn(ws)

    assert app.sessions[sid].ghost.turn == 2, "one roster turn, one player turn"


def test_init_carries_session_and_board(live):
    _, init = live
    assert init["type"] == "init"
    assert len(init["session_id"]) == 8
    assert init["transcript"] == f"{init['session_id']}.jsonl"
    assert init["state"]["tokens"]["bram"]["hp"] == 31


def test_turn_streams_then_finishes(live):
    ws, _ = live
    messages = play_turn(ws)
    assert [m["type"] for m in messages][0] == "stream_start"
    assert messages[-1]["type"] == "stream_done"
    assert "".join(m["text"] for m in messages if m["type"] == "chunk") == REPLY


def test_turn_reports_metrics(live):
    ws, _ = live
    metrics = play_turn(ws)[-1]["metrics"]
    assert metrics["turn"] == 2, "turn 1 was the roster"
    assert (metrics["input_tokens"], metrics["output_tokens"]) == (120, 40)
    assert metrics["latency_ms"] >= 0


def test_narration_updates_the_board(live):
    ws, _ = live
    done = play_turn(ws)[-1]

    assert [(a["name"], a["hp_delta"]) for a in done["applied"]] == [
        ("Cultist Leader", -9), ("Bram", 5)]
    assert done["state"]["enemies"]["cultist_1"]["hp"] == 13
    assert done["state"]["tokens"]["bram"]["hp"] == 31, "attacking cost Bram nothing"
    assert done["state"]["turn"] == 2


def test_transcript_is_written_by_the_visual_layer(live, app):
    ws, init = live
    play_turn(ws)

    session = app.sessions[init["session_id"]]
    with open(session.ghost.transcript_path, encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]
    assert len(lines) == 2, "the roster turn and the player's turn"
    assert REPLY in lines[1]


def test_rest_endpoints_are_scoped_to_a_session(live, client):
    _, init = live
    sid = init["session_id"]

    assert client.get(f"/api/state/{sid}").json()["tokens"]["bram"]["hp"] == 31

    moved = client.post("/api/token/move",
                        json={"session_id": sid, "token_id": "bram", "x": 99, "y": 3}).json()
    edge = moved["state"]["grid_cols"] - 1
    assert (moved["state"]["tokens"]["bram"]["x"], moved["state"]["tokens"]["bram"]["y"]) == (edge, 3)

    hurt = client.post("/api/token/hp",
                       json={"session_id": sid, "token_id": "bram", "delta": -7}).json()
    assert hurt["state"]["tokens"]["bram"]["hp"] == 24


def test_unknown_token_reports_error(live, client):
    _, init = live
    response = client.post("/api/token/hp", json={
        "session_id": init["session_id"], "token_id": "nobody", "delta": -7}).json()
    assert response["status"] == "error"


def test_unknown_session_is_404(client):
    assert client.get("/api/state/deadbeef").status_code == 404
    assert client.post("/api/token/hp", json={
        "session_id": "deadbeef", "token_id": "bram", "delta": 1}).status_code == 404


def test_reset_restores_the_board_mid_session(live, client):
    ws, init = live
    play_turn(ws)

    reset = client.post("/api/state/reset", json={"session_id": init["session_id"]}).json()
    assert reset["state"]["enemies"]["cultist_1"]["hp"] == 22
    assert reset["state"]["tokens"]["bram"]["hp"] == 31


def test_reconnect_resumes_the_same_session(client, app):
    with client.websocket_connect("/ws") as ws:
        sid = receive_json(ws)["session_id"]
        play_turn(ws)

    with client.websocket_connect(f"/ws?session_id={sid}") as ws:
        assert receive_json(ws)["session_id"] == sid

    assert len(app.sessions[sid].ghost.contents) == 4, "roster turn + player turn survived"
    assert len(app.sessions) == 1, "no orphan session was created"


def test_clients_get_their_own_boards(client, live):
    _, first = live
    with client.websocket_connect("/ws") as ws:
        second = receive_json(ws)

    assert second["session_id"] != first["session_id"]
    assert second["state"]["enemies"]["cultist_1"]["hp"] == 22


def test_static_files_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
