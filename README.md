Phase: 0 Baseline commited before working on testing

## Layout

```
ghost.py     the engine + CLI. Knows nothing about anything built on it.
visual/      state tracking UI. Imports ghost; never changes what the GM is asked.
party.txt    the roster, sent as the first turn.
transcripts/ one JSONL file per session, one record per turn.
```

`ghost.GhostSession` is the whole engine: history, streaming, metrics and the
transcript. It has no display of its own — callers stream through `on_chunk` and
render however they like. Forks should build on it rather than copy it, so a fix
to the engine lands everywhere and every fork's transcripts stay comparable.

## Running

CLI:

```
pip install -r requirements.txt
python ghost.py
```

Visual layer (adds the state tracker on top of the same engine):

```
pip install -r requirements.txt -r visual/requirements.txt
uvicorn visual.app:app --host 127.0.0.1 --port 8000
```

Both write to `transcripts/<session_id>.jsonl` in the same format. Each browser
connection is its own session with its own board and transcript; a reconnect
resumes the session it left.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

No network, no API key, no spend: `tests/fakes.py` stands in for the Gemini
client and asserts on every call that the baseline `SYSTEM_PROMPT` reached the
model unaltered, so any layer that quietly rewrites it fails the suite.

## Ground rules

- `SYSTEM_PROMPT` is deliberately naive. It's the control — don't improve it.
- Transcript data stays pure: styling happens at print time, never in the
  strings that go into history or onto disk.
- The visual layer observes, it does not inject. Tracked state is never fed
  back into the prompt; if that changes it's a new phase, behind a flag.
