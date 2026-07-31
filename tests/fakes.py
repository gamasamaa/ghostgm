"""A stand-in for the google-genai client: no network, no API key, no cost.

Mirrors just enough of the real surface for GhostSession to run against it —
`client.models.generate_content_stream` (sync) and
`client.aio.models.generate_content_stream` (async, awaited then iterated).
"""

DEFAULT_CHUNKS = ["The sewer reeks. Bram lea", "ds; Nix scouts ahead."]


class Usage:
    def __init__(self, prompt=120, candidates=40, total=160):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.total_token_count = total


class Chunk:
    def __init__(self, text, usage=None):
        self.text = text
        self.usage_metadata = usage


class FakeClient:
    """Replays `chunks`, then one usage-only chunk, exactly like the real stream.

    fail_times    — raise on the first N calls, to exercise retry paths
    expect_system — assert every call carries this system_instruction, so a
                    layer that quietly rewrites the baseline prompt fails loudly
    """

    def __init__(self, chunks=None, scripts=None, usage=None, fail_times=0, expect_system=None):
        # `scripts` gives a different reply per call (call N uses scripts[N-1],
        # the last one repeating) — needed once a session opens on the roster
        # and the opening shouldn't say the same thing as a combat turn.
        if scripts is not None:
            self.scripts = [list(s) for s in scripts]
        else:
            self.scripts = [list(DEFAULT_CHUNKS if chunks is None else chunks)]
        self.usage = usage or Usage()
        self.fail_times = fail_times
        self.expect_system = expect_system
        self.calls = []  # one (model, history_length, system_instruction) per call

        self.models = _SyncModels(self)
        self.aio = _Aio(self)

    def factory(self):
        """`ghost.genai.Client` replacement: hands back this same instance."""
        return lambda *args, **kwargs: self

    def _begin(self, model, contents, config):
        if self.expect_system is not None:
            assert config.system_instruction == self.expect_system, (
                f"system prompt was altered: {config.system_instruction!r}")
        self.calls.append((model, len(contents), config.system_instruction))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("503 overloaded")

    def _stream(self):
        script = self.scripts[min(len(self.calls) - 1, len(self.scripts) - 1)]
        return [Chunk(c) for c in script] + [Chunk("", self.usage)]


class _SyncModels:
    def __init__(self, client):
        self.client = client

    def generate_content_stream(self, *, model, contents, config):
        self.client._begin(model, contents, config)
        return iter(self.client._stream())


class _AsyncModels:
    def __init__(self, client):
        self.client = client

    async def generate_content_stream(self, *, model, contents, config):
        self.client._begin(model, contents, config)

        async def stream():
            for chunk in self.client._stream():
                yield chunk

        return stream()


class _Aio:
    def __init__(self, client):
        self.models = _AsyncModels(client)
