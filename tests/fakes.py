"""A stand-in for the google-genai client: no network, no API key, no cost.

Mirrors just enough of the real surface for GhostSession to run against it —
`client.models.generate_content_stream` (sync) and
`client.aio.models.generate_content_stream` (async, awaited then iterated).
"""

from google.genai import errors as genai_errors

DEFAULT_CHUNKS = ["The sewer reeks. Bram lea", "ds; Nix scouts ahead."]


def overloaded():
    """The transient failure the retry path exists for: a real 503 APIError.

    Tests raise the provider's own error type rather than a stand-in, so
    `ghost.is_transient` is exercised on the thing it actually has to classify.
    """
    return genai_errors.ServerError(503, {"error": {"message": "overloaded"}})


def quota_exceeded(daily=True, retry_delay="27s", model="gemini-3.5-flash-lite"):
    """A free-tier 429, shaped like the one the API actually sends.

    Daily and per-minute exhaustion are the same status code and the same
    message; only the quotaId separates them, which is the whole reason the
    classifier has to read this deep. Pass `daily=False` for the burst limit.

    `quotaDimensions` carries the model, exactly as the live API sends it. It
    matters because the daily allowance is per-model and the values differ
    wildly between them, so "which model" is half the answer.
    """
    period = "PerDay" if daily else "PerMinute"
    violation = {
        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
        "quotaId": f"GenerateRequests{period}PerProjectPerModel-FreeTier",
        "quotaValue": "50" if daily else "15",
    }
    if model:
        violation["quotaDimensions"] = {"location": "global", "model": model}
    details = [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [violation],
    }]
    if retry_delay:
        details.append({
            "@type": "type.googleapis.com/google.rpc.RetryInfo",
            "retryDelay": retry_delay,
        })
    return genai_errors.ClientError(429, {"error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "status": "RESOURCE_EXHAUSTED",
        "details": details,
    }})


def refused(code, status, message="refused"):
    """Any other APIError, by status code.

    ClientError and ServerError split at 500 in the provider's own hierarchy,
    so the right class is picked here rather than left to the caller.
    """
    body = {"error": {"code": code, "status": status, "message": message}}
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, body)


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
    error         — what those calls raise; a transient 503 by default, so a
                    non-transient failure has to be asked for explicitly
    fail_mid_stream — raise after this many chunks instead of before the first,
                    the case where a retry has to discard already-streamed text
    expect_system — assert every call carries this system_instruction, so a
                    layer that quietly rewrites the baseline prompt fails loudly
    """

    def __init__(self, chunks=None, scripts=None, usage=None, fail_times=0,
                 expect_system=None, error=None, fail_mid_stream=None):
        # `scripts` gives a different reply per call (call N uses scripts[N-1],
        # the last one repeating) — needed once a session opens on the roster
        # and the opening shouldn't say the same thing as a combat turn.
        if scripts is not None:
            self.scripts = [list(s) for s in scripts]
        else:
            self.scripts = [list(DEFAULT_CHUNKS if chunks is None else chunks)]
        self.usage = usage or Usage()
        self.fail_times = fail_times
        self.error = error
        self.fail_mid_stream = fail_mid_stream
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
        if self._failing() and self.fail_mid_stream is None:
            raise self.error or overloaded()

    def _failing(self):
        return len(self.calls) <= self.fail_times

    def _stream(self):
        """Chunks for the current call, raising part-way through when asked."""
        script = self.scripts[min(len(self.calls) - 1, len(self.scripts) - 1)]
        chunks = [Chunk(c) for c in script] + [Chunk("", self.usage)]
        if self._failing() and self.fail_mid_stream is not None:
            for chunk in chunks[: self.fail_mid_stream]:
                yield chunk
            raise self.error or overloaded()
        yield from chunks


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
