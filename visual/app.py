"""Visual layer on top of ghost.py.

The GM engine — model, prompt, history, transcript — is `ghost.GhostSession`,
imported as-is. Nothing here alters what the GM is asked or told; this layer
only watches the output and tracks state beside it. `ghost.py` stays runnable
on its own and has no idea this exists.
"""

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ghost
from visual.narration import HPExtractor
from visual.state import StateManager

# Resolve against the repo, not the working directory, so the server can be
# started from anywhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
PARTY_PATH = str(REPO_ROOT / ghost.PARTY_FILE)
TRANSCRIPT_DIR = str(REPO_ROOT / ghost.TRANSCRIPT_DIR)

app = FastAPI(title="GhostGM Visual Interface")

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


class VisualSession:
    """One ghost conversation plus the board that tracks it."""

    def __init__(self):
        self.ghost = ghost.GhostSession(transcript_dir=TRANSCRIPT_DIR)
        self.state_manager = StateManager(PARTY_PATH)
        self.extractor = HPExtractor(self.state_manager.all_tokens())
        # The same roster the board is built from, sent to the GM as the opening
        # turn exactly like ghost.py's CLI does. Without it the GM invents its
        # own party and nothing on the board can ever match the narration.
        self.party_text = ghost.read_party_file(PARTY_PATH)

    @property
    def id(self):
        return self.ghost.session_id

    def snapshot(self):
        return self.state_manager.state.model_dump()

    def reset(self):
        """Clear the board. The conversation and its transcript are left alone —
        a session is one transcript, start to finish."""
        self.state_manager.reset_from_party()
        self.extractor = HPExtractor(self.state_manager.all_tokens())

    def apply_narration(self, text):
        """Fold any HP changes the GM narrated into the board."""
        applied = []
        for delta in self.extractor.extract(text):
            token = self.state_manager.update_hp(delta.token_id, delta.hp_delta)
            if token:
                applied.append({
                    "name": delta.name,
                    "hp_delta": delta.hp_delta,
                    "hp": token.hp,
                    "max_hp": token.max_hp,
                    "phrase": delta.phrase,
                })
        self.state_manager.state.turn = self.ghost.turn
        return applied


sessions = {}


def get_session(session_id):
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    return session


class MoveRequest(BaseModel):
    session_id: str
    token_id: str
    x: int
    y: int


class HPRequest(BaseModel):
    session_id: str
    token_id: str
    delta: int


class SessionRequest(BaseModel):
    session_id: str


@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return open(index_file, encoding="utf-8").read()
    return "<h1>GhostGM Visual Interface starting up...</h1>"


@app.get("/api/state/{session_id}")
async def get_state(session_id: str):
    return get_session(session_id).snapshot()


@app.post("/api/state/reset")
async def reset_state(req: SessionRequest):
    session = get_session(req.session_id)
    session.reset()
    return {"status": "ok", "state": session.snapshot()}


@app.post("/api/token/move")
async def move_token(req: MoveRequest):
    session = get_session(req.session_id)
    updated = session.state_manager.move_token(req.token_id, req.x, req.y)
    return {"status": "ok" if updated else "error", "state": session.snapshot()}


@app.post("/api/token/hp")
async def update_hp(req: HPRequest):
    session = get_session(req.session_id)
    updated = session.state_manager.update_hp(req.token_id, req.delta)
    return {"status": "ok" if updated else "error", "state": session.snapshot()}


async def run_turn(websocket, session, user_input):
    """One exchange: stream the GM's reply, then reconcile the board with it."""
    await websocket.send_json({"type": "stream_start"})

    async def on_chunk(text):
        await websocket.send_json({"type": "chunk", "text": text})

    async def on_retry(attempt, error, partial):
        # The browser has already rendered `partial`; the retry replaces the
        # reply rather than continuing it, so tell it to clear.
        await websocket.send_json({
            "type": "stream_retry",
            "attempt": attempt,
            "of": session.ghost.max_attempts - 1,
            "message": str(error),
        })

    try:
        record = await session.ghost.send_async(
            user_input, on_chunk=on_chunk, on_retry=on_retry)
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        return None

    applied = session.apply_narration(record["assistant"])

    await websocket.send_json({
        "type": "stream_done",
        "state": session.snapshot(),
        "applied": applied,
        "metrics": {
            "turn": record["turn"],
            "input_tokens": record["input_tokens"],
            "output_tokens": record["output_tokens"],
            "latency_ms": record["latency_ms"],
            "ttft_ms": record["ttft_ms"],
        },
    })
    return record


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, session_id: str = None):
    await websocket.accept()

    # Reconnects pass their id back so a dropped socket resumes the same
    # conversation and keeps appending to the same transcript.
    session = sessions.get(session_id)
    is_new = session is None
    if is_new:
        session = VisualSession()
        sessions[session.id] = session

    await websocket.send_json({
        "type": "init",
        "session_id": session.id,
        "transcript": os.path.basename(session.ghost.transcript_path),
        "state": session.snapshot(),
    })

    try:
        # A fresh session opens on the roster, so the GM and the board are
        # talking about the same characters. A reconnect must not replay it.
        if is_new and session.party_text:
            await websocket.send_json({
                "type": "notice",
                "text": f"Loaded {os.path.basename(PARTY_PATH)} — sending the roster to the GM.",
            })
            await run_turn(websocket, session, session.party_text)

        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            user_input = msg.get("text", "").strip()
            if not user_input:
                continue
            await run_turn(websocket, session, user_input)

    except WebSocketDisconnect:
        pass
