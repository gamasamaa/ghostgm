import asyncio
import datetime
import json
import os
import random
import re
import time
import uuid
import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from rich.console import Console
from rich.text import Text

load_dotenv()

MODEL = "gemini-3.5-flash-lite"

# Deliberately naive baseline system prompt — do not improve.
SYSTEM_PROMPT = "You are a Dungeons & Dragons 5e game master. Run the session."

PARTY_FILE = "party.txt"
TRANSCRIPT_DIR = "transcripts"

# ---------------------------------------------------------------------------
# Retry policy
#
# A 30-turn session is a long time to hold a provider blip against. A transient
# failure re-issues the *identical* request — same history, same config, same
# model — so a retried turn is indistinguishable from a first-attempt one in the
# transcript, and the baseline stays comparable.
# ---------------------------------------------------------------------------

# Worth re-issuing: rate limits, timeouts, and the 5xx family. Everything else
# (bad key, unknown model, malformed request, safety block) fails identically on
# a second try, so it surfaces immediately instead of burning the backoff budget.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

MAX_ATTEMPTS = 4        # the first try plus three retries
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 20.0


def is_transient(exc):
    """True when re-issuing the same request could plausibly succeed."""
    if isinstance(exc, genai_errors.APIError):
        return exc.code in RETRYABLE_STATUS
    # Never reached the model, or died on the way back: reset connection, DNS
    # blip, read timeout. httpx.TransportError covers the family; the builtins
    # catch anything that surfaces before httpx gets involved.
    return isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError))


def backoff_delay(attempt, rand=random.random):
    """Seconds to wait before re-issuing. Exponential, capped, full jitter.

    Jitter matters even for a single client: without it, every session that hit
    the same provider blip retries on the same second and blips it again.
    """
    ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempt - 1)))
    return ceiling * rand()


def parse_party(text):
    """Pull the roster out of party.txt lines ('- Bram, human fighter. AC 18, HP 31.').

    Returns dicts with `name` plus whatever else the line happened to state;
    `description`, `ac` and `hp` are None when absent. No D&D defaults are
    invented here — that is a decision for whoever consumes the roster.
    """
    members = []
    seen = set()
    for line in text.splitlines():
        m = re.match(r"\s*[-*]\s*([A-Z][A-Za-z'’-]+)(?:,\s*([^.]+))?", line)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        ac = re.search(r"AC\s*(\d+)", line, re.IGNORECASE)
        hp = re.search(r"HP\s*(\d+)", line, re.IGNORECASE)
        members.append({
            "name": m.group(1),
            "description": m.group(2).strip() if m.group(2) else None,
            "ac": int(ac.group(1)) if ac else None,
            "hp": int(hp.group(1)) if hp else None,
        })
    return members


def read_party_file(path=PARTY_FILE):
    """Raw text of the party file, or None when there isn't one."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


class _Turn:
    """Accumulator for one in-flight generation: text plus its metrics."""

    def __init__(self):
        self.text = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.ttft_ms = None
        self.start = time.perf_counter()

    def absorb(self, chunk):
        """Fold one stream chunk in and hand back its text (empty if none)."""
        text = chunk.text
        if text:
            if self.ttft_ms is None:
                self.ttft_ms = (time.perf_counter() - self.start) * 1000
            self.text += text  # raw text, never styled
        if chunk.usage_metadata:
            self.input_tokens = chunk.usage_metadata.prompt_token_count or self.input_tokens
            self.output_tokens = chunk.usage_metadata.candidates_token_count or self.output_tokens
            self.total_tokens = chunk.usage_metadata.total_token_count or self.total_tokens
        return text or ""


class GhostSession:
    """One GM conversation: history, streaming, and the transcript on disk.

    This is the whole engine. It has no display of its own — callers stream
    through `on_chunk` and render however they like — and it knows nothing
    about anything built on top of it.
    """

    def __init__(self, model=MODEL, system_prompt=SYSTEM_PROMPT,
                 transcript_dir=TRANSCRIPT_DIR, client=None, session_id=None,
                 max_attempts=MAX_ATTEMPTS, sleep=time.sleep, rand=random.random):
        self.model = model
        self.system_prompt = system_prompt
        self.config = types.GenerateContentConfig(system_instruction=system_prompt)
        self.client = client if client is not None else genai.Client()

        # Injectable so tests can exercise the retry path without waiting on it.
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._rand = rand

        self.session_id = session_id or uuid.uuid4().hex[:8]
        os.makedirs(transcript_dir, exist_ok=True)
        self.transcript_path = os.path.join(transcript_dir, f"{self.session_id}.jsonl")

        # Local conversation history (system prompt lives in config, not here)
        self.contents = []
        self.turn = 0

    def send(self, user_input, on_chunk=None, on_retry=None):
        """Blocking turn. `on_chunk(text)` fires per stream chunk.

        Transient failures are retried automatically. A retry abandons whatever
        the failed attempt streamed and starts the reply over, so `on_retry
        (attempt, error, partial_text)` fires first — a display that has already
        shown `partial_text` needs to discard it.
        """
        self._begin(user_input)
        attempt = 1
        while True:
            turn = _Turn()
            try:
                stream = self.client.models.generate_content_stream(
                    model=self.model,
                    contents=self.contents,
                    config=self.config,
                )
                for chunk in stream:
                    text = turn.absorb(chunk)
                    if text and on_chunk:
                        on_chunk(text)
            except Exception as exc:
                delay = self._retry_delay(exc, attempt)
                if delay is None:
                    self._abort()
                    raise
                if on_retry:
                    on_retry(attempt, exc, turn.text)
                self._sleep(delay)
                attempt += 1
                continue
            # Committing sits outside the try: a disk error writing the
            # transcript is not something to re-ask the model about.
            return self._commit(user_input, turn)

    async def send_async(self, user_input, on_chunk=None, on_retry=None):
        """Same turn over the async client; `on_chunk` and `on_retry` are awaited.

        Backoff here always waits on `asyncio.sleep` rather than the injected
        `sleep`, which would block the event loop; tests pass `rand` to make the
        delays zero.
        """
        self._begin(user_input)
        attempt = 1
        while True:
            turn = _Turn()
            try:
                stream = await self.client.aio.models.generate_content_stream(
                    model=self.model,
                    contents=self.contents,
                    config=self.config,
                )
                async for chunk in stream:
                    text = turn.absorb(chunk)
                    if text and on_chunk:
                        await on_chunk(text)
            except Exception as exc:
                delay = self._retry_delay(exc, attempt)
                if delay is None:
                    self._abort()
                    raise
                if on_retry:
                    await on_retry(attempt, exc, turn.text)
                await asyncio.sleep(delay)
                attempt += 1
                continue
            return self._commit(user_input, turn)

    def _retry_delay(self, exc, attempt):
        """Seconds to wait before re-issuing, or None to give up and raise."""
        if attempt >= self.max_attempts or not is_transient(exc):
            return None
        return backoff_delay(attempt, self._rand)

    def _begin(self, user_input):
        self.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_input)]))

    def _abort(self):
        self.contents.pop()  # drop the unanswered user turn; keep prior history intact

    def _commit(self, user_input, turn):
        self.contents.append(types.Content(role="model", parts=[types.Part.from_text(text=turn.text)]))
        self.turn += 1

        record = {
            "session_id": self.session_id,
            "turn": self.turn,
            "model": self.model,
            "ts": datetime.datetime.now().isoformat(),
            "user": user_input,
            "assistant": turn.text,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "total_tokens": turn.total_tokens,
            "latency_ms": round((time.perf_counter() - turn.start) * 1000, 2),
            "ttft_ms": round(turn.ttft_ms, 2) if turn.ttft_ms is not None else None,
        }
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return record


# ---------------------------------------------------------------------------
# CLI. Everything below is presentation only: colors are applied at print time
# to throwaway Text objects, never to the strings the session keeps in
# `contents` or writes to the transcript. Rich also drops color automatically
# when stdout is not a TTY (piped/redirected) and honors NO_COLOR.
# ---------------------------------------------------------------------------

# highlight=False: no auto-recoloring of numbers/quotes.
# soft_wrap=True: let the terminal wrap, so rich never reflows the GM's prose.
console = Console(highlight=False, soft_wrap=True)

GM_STYLE = "bold magenta"
YOU_STYLE = "bold white"
WARN_STYLE = "yellow"
# Reserved for the GM: magenta. Players draw from here, in party.txt order.
PLAYER_PALETTE = ["cyan", "green", "yellow", "blue", "red", "bright_cyan", "bright_green"]


def speaker_of(user_input, names):
    """The party member a turn is spoken as, or None if it carries no prefix.

    The convention is `Name: action`, and it lives entirely here — the model is
    never told it exists, because party.txt is turn 1 and explaining the
    notation there measurably changed how the GM ran the table. Nothing enforces
    it either: unprefixed turns are sent exactly as typed. But a turn without a
    speaker can't be attributed to a character when the transcripts are
    labelled, so the CLI says so at the time, while it's cheap to retype.
    """
    head, sep, _ = user_input.partition(":")
    if not sep:
        return None
    return head.strip() if head.strip() in names else None


def build_name_pattern(player_styles):
    """One regex covering every party member, longest name first so overlapping
    names ("Bram" vs "Brammit") match the more specific one."""
    if not player_styles:
        return None
    alternation = "|".join(re.escape(n) for n in sorted(player_styles, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternation})\b")


def colorize(s, name_pattern, player_styles):
    """Return a styled copy of `s` for display. `s` itself is untouched."""
    text = Text(s)
    if name_pattern:
        for match in name_pattern.finditer(s):
            text.stylize(player_styles[match.group(0)], match.start(), match.end())
    return text


def main():
    party_text = read_party_file()

    player_styles = {}
    if party_text:
        for i, member in enumerate(parse_party(party_text)):
            player_styles[member["name"]] = PLAYER_PALETTE[i % len(PLAYER_PALETTE)]
    name_pattern = build_name_pattern(player_styles)

    session = GhostSession()

    console.print(
        f"Starting D&D Game Session (Session ID: {session.session_id}, type 'exit' or 'quit' to end)...",
        style="dim",
    )
    if player_styles:
        legend = Text("Party: ", style="dim")
        for i, (name, style) in enumerate(player_styles.items()):
            if i:
                legend.append("  ")
            legend.append(name, style=style)
        legend.append("   GM", style=GM_STYLE)
        console.print(legend)
        console.print(
            Text(f'Speak as a character: "{next(iter(player_styles))}: I attack the nearest cultist"',
                 style="dim"))
    console.print()

    # The prompt itself carries the convention, so it's in front of you on every
    # turn of a 30-turn session rather than only in the banner.
    prompt_label = "You (Name: action) > " if player_styles else "You > "

    # Set when a turn fails, so the next iteration replays it instead of
    # asking for fresh input.
    pending_input = None

    while True:
        if pending_input is not None:
            user_input, pending_input = pending_input, None
        elif session.turn == 0 and party_text is not None:
            user_input = party_text
            console.print(Text(prompt_label, style=YOU_STYLE) + Text(f"[loaded {PARTY_FILE}]", style="dim"))
        else:
            try:
                user_input = console.input(Text(prompt_label, style=YOU_STYLE))
            except (EOFError, KeyboardInterrupt):
                console.print()
                user_input = "exit"
            if user_input.strip().lower() in ["exit", "quit"]:
                console.print(
                    f"Ending session. Transcript saved to {session.transcript_path}. Farewell!",
                    style="dim",
                )
                break
            # Warn, don't block: the turn goes to the model exactly as typed.
            if player_styles and speaker_of(user_input, player_styles) is None:
                console.print(
                    Text("[no speaker prefix — this turn won't be attributable]",
                         style=WARN_STYLE))

        console.print()
        console.print(Text("[Sending prompt to GM...]", style="dim"))
        console.print(Text("GM > ", style=GM_STYLE), end="")

        # Display buffer: holds back the trailing partial word so a character
        # name split across two stream chunks still gets matched as one name.
        pending = ""

        def on_chunk(text):
            nonlocal pending
            pending += text
            cut = max(pending.rfind(" "), pending.rfind("\n"))
            if cut != -1:
                console.print(colorize(pending[: cut + 1], name_pattern, player_styles), end="")
                pending = pending[cut + 1 :]

        def on_retry(attempt, error, partial):
            # The abandoned attempt's text is already on screen and can't be
            # unprinted, so say plainly that what follows replaces it.
            nonlocal pending
            pending = ""
            console.print(
                Text(f"\n[retry {attempt}/{session.max_attempts - 1}: {error} "
                     f"— discarding the above, restarting the reply]", style=WARN_STYLE))
            console.print(Text("GM > ", style=GM_STYLE), end="")

        try:
            session.send(user_input, on_chunk=on_chunk, on_retry=on_retry)
            if pending:
                console.print(colorize(pending, name_pattern, player_styles), end="")
        except Exception as e:
            if pending:
                console.print(colorize(pending, name_pattern, player_styles), end="")
            # Automated retries are spent (or the failure was never transient),
            # so the last resort is a human deciding to try again.
            console.print(Text(f"\n[Error during generation: {e}]", style="bold red"))
            pending_input = user_input  # replay the same turn on the next iteration
            try:
                console.input(Text("Press Enter to retry the same turn...", style="dim"))
            except (EOFError, KeyboardInterrupt):
                console.print()
                console.print(
                    f"Ending session. Transcript saved to {session.transcript_path}. Farewell!",
                    style="dim",
                )
                break
            continue

        console.print("\n")


if __name__ == "__main__":
    main()
