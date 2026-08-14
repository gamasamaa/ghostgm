import asyncio
import dataclasses
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


# A per-minute limit routinely asks for longer than BACKOFF_CAP_S, so the wait
# google names is honoured over our own. Capped just past a one-minute window:
# anything asking for longer is not a burst limit and should surface instead of
# silently parking the session.
RETRY_AFTER_CAP_S = 65.0

# ---------------------------------------------------------------------------
# Free-tier quota
#
# A free-tier key hits two different 429s that need opposite handling. A
# per-minute rate limit clears itself within the minute, so it is worth waiting
# out. A spent daily allowance does not clear until google's quota window rolls
# over, so retrying only burns the backoff budget and then reports a timeout —
# which says nothing about why the session actually stopped.
#
# Both arrive as 429 RESOURCE_EXHAUSTED. What separates them is the QuotaFailure
# violation google attaches to the response body.
# ---------------------------------------------------------------------------

QUOTA_STATUS = "RESOURCE_EXHAUSTED"
# Google's naming for the daily buckets, e.g. the free tier's
# "GenerateRequestsPerDayPerProjectPerModel-FreeTier".
DAILY_QUOTA_MARKERS = ("perday", "per_day")


def _error_body(exc):
    """The `error` object off an APIError, or {}.

    `details` is whatever JSON came back, so nothing here may assume a shape —
    a parser that raises while explaining a failure is worse than no parser.
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return {}
    body = details.get("error", details)
    return body if isinstance(body, dict) else {}


def _detail_entries(exc, type_suffix):
    """google.rpc detail entries of one type, e.g. "QuotaFailure"."""
    entries = _error_body(exc).get("details")
    if not isinstance(entries, list):
        return []
    return [e for e in entries
            if isinstance(e, dict) and str(e.get("@type", "")).endswith(type_suffix)]


def quota_violations(exc):
    """Which allowances the request blew, as google reported them."""
    found = []
    for entry in _detail_entries(exc, "QuotaFailure"):
        violations = entry.get("violations")
        if isinstance(violations, list):
            found.extend(v for v in violations if isinstance(v, dict))
    return found


def is_quota_error(exc):
    """True for any 'you have used your allowance' refusal, daily or per-minute."""
    return isinstance(exc, genai_errors.APIError) and (
        exc.code == 429 or exc.status == QUOTA_STATUS)


def is_quota_exhausted(exc):
    """True when the allowance is spent for the day, not just for the minute.

    Deliberately conservative: an unrecognised 429 is treated as a burst limit
    and retried, because waiting is cheap and wrongly declaring a session over
    is not.
    """
    if not is_quota_error(exc):
        return False
    for violation in quota_violations(exc):
        name = f"{violation.get('quotaId', '')} {violation.get('quotaMetric', '')}".lower()
        if any(marker in name for marker in DAILY_QUOTA_MARKERS):
            return True
    return False


def retry_after(exc):
    """The wait google asked for in seconds, or None if it didn't say."""
    for entry in _detail_entries(exc, "RetryInfo"):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(entry.get("retryDelay", "")).strip())
        if match:
            return float(match.group(1))
    return None


def is_transient(exc):
    """True when re-issuing the same request could plausibly succeed."""
    if isinstance(exc, genai_errors.APIError):
        # 429 is in RETRYABLE_STATUS for the per-minute case. A spent daily
        # allowance is the one 429 that no amount of waiting inside a session
        # will clear, so it surfaces immediately with an explanation.
        if is_quota_exhausted(exc):
            return False
        return exc.code in RETRYABLE_STATUS
    # Never reached the model, or died on the way back: reset connection, DNS
    # blip, read timeout. httpx.TransportError covers the family; the builtins
    # catch anything that surfaces before httpx gets involved.
    return isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError))


# ---------------------------------------------------------------------------
# Explaining a failure
#
# `describe` gives the one line a retry notice needs. This is the other half:
# everything a player needs to decide what to do, pulled out of the response
# body and handed over as data. Nothing here formats or styles — the renderer
# owns that, the same way transcript text is only ever styled at print time.
#
# The cases are the ones this project has actually hit, not a guess at the API
# surface. A 503 that reads "google returned 503 UNAVAILABLE" is what sent an
# afternoon chasing a harness bug that did not exist.
# ---------------------------------------------------------------------------

# Where google's free-tier daily window rolls over. Confirmed against this
# project's key: the reset lands on midnight Pacific, not on the caller's date.
PACIFIC = "America/Los_Angeles"


@dataclasses.dataclass(frozen=True)
class Explanation:
    """What went wrong, whether waiting helps, and what to do about it.

    `facts` is label/value pairs lifted from the response body — model, quota
    id, numeric limit — so the renderer can lay them out without re-parsing.
    """
    headline: str
    advice: str
    retryable: bool
    wait_s: float = None
    resets_at: datetime.datetime = None
    facts: tuple = ()


def daily_reset_after(now=None):
    """Local time of the next midnight Pacific, or None if the tz is unavailable.

    The reset that matters is google's date, not the caller's: at 00:30 AST the
    local calendar has already turned over while Pacific still has three hours
    to go, which reads as "a fresh day that is somehow still blocked".
    """
    try:
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo(PACIFIC)
    except Exception:
        return None  # no tz database; the caller just omits the line
    now = now or datetime.datetime.now().astimezone()
    there = now.astimezone(pacific)
    midnight = (there + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(now.tzinfo)


def quota_model(exc):
    """Which model blew its allowance, if google named one."""
    for violation in quota_violations(exc):
        dimensions = violation.get("quotaDimensions")
        if isinstance(dimensions, dict) and dimensions.get("model"):
            return str(dimensions["model"])
    return None


def quota_limit(exc):
    """The numeric allowance google reported, as a string, if it gave one."""
    for violation in quota_violations(exc):
        if violation.get("quotaValue"):
            return str(violation["quotaValue"])
    return None


def _quota_facts(exc):
    facts = []
    model = quota_model(exc)
    if model:
        facts.append(("model", model))
    for violation in quota_violations(exc):
        name = violation.get("quotaId") or violation.get("quotaMetric")
        if name:
            facts.append(("quota", str(name)))
            break
    limit = quota_limit(exc)
    if limit:
        facts.append(("limit", limit))
    return tuple(facts)


def _explain(exc, now):
    if not isinstance(exc, genai_errors.APIError):
        if isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError)):
            return Explanation(
                headline="could not reach google",
                advice="The request never arrived, so nothing was spent. "
                       "Check the connection and try the turn again.",
                retryable=True,
                facts=(("cause", f"{type(exc).__name__}: {exc}"[:120]),))
        return Explanation(
            headline=f"unexpected {type(exc).__name__}",
            advice="This is not a provider error — it most likely comes from "
                   "this code rather than from google.",
            retryable=False,
            facts=(("detail", str(exc)[:200]),))

    code = exc.code
    asked = retry_after(exc)

    if is_quota_error(exc):
        if is_quota_exhausted(exc):
            return Explanation(
                headline="free-tier daily allowance is spent",
                advice="Retrying will not clear this. The window reopens on "
                       "google's clock, not yours.",
                retryable=False,
                resets_at=daily_reset_after(now),
                facts=_quota_facts(exc))
        return Explanation(
            headline="rate limited — too many requests too quickly",
            advice="This clears on its own. Waiting out the window is the fix; "
                   "the session stays open.",
            retryable=True,
            wait_s=asked,
            facts=_quota_facts(exc))

    if code == 503:
        return Explanation(
            headline="the model is overloaded",
            advice="Google-side demand, not a problem with this key or this "
                   "code. It usually passes within minutes.",
            retryable=True,
            wait_s=asked,
            facts=(("status", str(exc.status or "UNAVAILABLE")),))

    if code in (500, 502, 504):
        return Explanation(
            headline=f"google failed to answer ({code})",
            advice="A fault on google's side. Re-issuing the same turn is safe.",
            retryable=True,
            wait_s=asked)

    if code == 408:
        return Explanation(
            headline="the request timed out",
            advice="No reply arrived in time. Re-issuing the same turn is safe.",
            retryable=True,
            wait_s=asked)

    if code == 404:
        return Explanation(
            headline="google has no such model for this key",
            advice=f"Check MODEL in ghost.py (currently {MODEL!r}). Model "
                   "availability differs between keys and changes over time.",
            retryable=False)

    if code in (401, 403):
        return Explanation(
            headline="google rejected the key",
            advice="Check GEMINI_API_KEY in .env — missing, mistyped, revoked, "
                   "or not enabled for this model.",
            retryable=False)

    if code == 400:
        return Explanation(
            headline="google rejected the request as malformed",
            advice="Retrying sends the identical request and will fail the same "
                   "way. This is a bug to fix, not a blip to wait out.",
            retryable=False,
            facts=(("message", str(_error_body(exc).get("message", ""))[:200]),))

    return Explanation(
        headline=f"google returned {code} {exc.status or ''}".strip(),
        advice="Unrecognised refusal. The status above is what google sent.",
        retryable=code in RETRYABLE_STATUS,
        wait_s=asked)


def explain(exc, now=None):
    """Everything a player needs to act on a failure, as plain data.

    Never raises. An explainer that dies while explaining leaves the caller with
    strictly less than it started with, so an unrecognised shape degrades to the
    exception's own text rather than propagating.
    """
    try:
        return _explain(exc, now)
    except Exception:
        # Even the fallback cannot trust the exception: __str__ is caller code
        # and is free to raise, which would throw from inside the handler that
        # exists to stop exactly that.
        try:
            detail = str(exc)[:200]
        except Exception:
            detail = "(the error could not be rendered as text)"
        return Explanation(
            headline=f"{type(exc).__name__}",
            advice="The failure could not be parsed; its own text is below.",
            retryable=False,
            facts=(("detail", detail),))


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

        jitter = backoff_delay(attempt, self._rand)
        asked = retry_after(exc)
        if asked is None:
            return jitter
        # Google named a number. Wait at least that long — our own cap is
        # shorter than a per-minute window, so ignoring it would retry into the
        # same closed door three times and call the session dead.
        return min(asked + jitter, RETRY_AFTER_CAP_S)

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
# Reversed rather than colored: every foreground color is already spoken for by
# the GM, a party member or a warning, and the tally has to stay findable when
# you scroll back through a 30-turn session looking for one turn.
TURN_STYLE = "bold black on white"
# Reserved for the GM: magenta. Players draw from here, in party.txt order.
PLAYER_PALETTE = ["cyan", "green", "yellow", "blue", "red", "bright_cyan", "bright_green"]

# What the roadmap asks of one baseline session. Display only — nothing enforces
# it, it just saves counting transcript lines to find out where you stand.
SESSION_TURN_TARGET = 30


def turn_tag(n):
    """The turn number a record will carry, as a display label.

    failures.md lines are written `s<session_id>/t<turn>`, so this is the `t`
    half, readable at the moment the failure happens instead of reconstructed
    from the transcript afterwards.

    The style goes on an appended span, not on the Text itself: a base style
    survives concatenation and would tint everything printed after the tag.
    """
    tag = Text()
    tag.append(f" t{n} ", style=TURN_STYLE)
    return tag


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


def farewell(session):
    """Sign-off: where the transcript went, and how the count landed."""
    line = Text()
    line.append("Ending session —", style="dim")
    line.append(f" {session.turn} turns ", style=TURN_STYLE)
    line.append(f" recorded to {session.transcript_path}.", style="dim")
    console.print(line)

    short = SESSION_TURN_TARGET - session.turn
    if short > 0:
        console.print(Text(
            f"{short} turn{'s' if short != 1 else ''} short of the "
            f"{SESSION_TURN_TARGET}-turn target for a baseline session.",
            style=WARN_STYLE))


def describe(exc):
    """A one-line reason. APIError stringifies to the whole response body, which
    on a 429 is a wall of JSON that buries the one word that matters."""
    if is_quota_error(exc):
        which = "daily allowance spent" if is_quota_exhausted(exc) else "rate limited"
        return f"google says {which}"
    if isinstance(exc, genai_errors.APIError):
        return f"google returned {exc.code} {exc.status or ''}".strip()
    return f"{type(exc).__name__}: {exc}"


def _reset_line(resets_at, now=None):
    """The daily reset as a local clock time plus how long that is from now."""
    now = now or datetime.datetime.now().astimezone()
    left = max(0, int((resets_at - now).total_seconds()))
    stamp = resets_at.strftime("%H:%M %Z").strip()
    return f"{stamp} — midnight Pacific, in {left // 3600}h {left % 3600 // 60:02d}m"


def report_failure(exc, session, now=None):
    """Say plainly what google did and what to do about it.

    Replaces printing the exception. On a 429 `str(exc)` is a wall of JSON that
    buries the one word that matters; on a 503 it is a sentence that reads like
    this code broke. Both send you looking in the wrong place.

    Nothing is decided here — `explain` already did that. This only lays it out.
    """
    result = explain(exc, now)
    lead = "Google is refusing this key" if is_quota_error(exc) else "Turn failed"
    console.print(Text(f"[{lead} — {result.headline}]", style="bold red"))

    for label, value in result.facts:
        console.print(Text(f"  {label:<7} {value}", style="dim"))

    if result.resets_at is not None:
        console.print(Text(f"  {'resets':<7} {_reset_line(result.resets_at, now)}",
                           style="dim"))
    elif result.wait_s:
        console.print(Text(f"  {'wait':<7} google asked for {result.wait_s:.0f}s",
                           style="dim"))

    console.print(Text(f"  {result.advice}", style="dim"))

    # The turn that failed was never committed, so the count is what is on disk.
    # Losing a long session is the fear a wall like this triggers; answer it here.
    if session is None:
        return
    if session.turn:
        console.print(Text(
            f"  {session.turn} turns are already saved to {session.transcript_path} "
            "— nothing is lost.", style="dim"))
    else:
        console.print(Text(
            f"  Nothing recorded yet. The transcript is {session.transcript_path}.",
            style="dim"))


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
        # The turn this exchange will be recorded as. A failed turn is never
        # committed, so the number is still free on the next pass — the tally
        # counts what reached the transcript, not what was attempted.
        turn_no = session.turn + 1
        prompt = turn_tag(turn_no) + Text(" " + prompt_label, style=YOU_STYLE)

        if pending_input is not None:
            user_input, pending_input = pending_input, None
        elif session.turn == 0 and party_text is not None:
            user_input = party_text
            console.print(prompt + Text(f"[loaded {PARTY_FILE}]", style="dim"))
        else:
            try:
                user_input = console.input(prompt)
            except (EOFError, KeyboardInterrupt):
                console.print()
                user_input = "exit"
            if user_input.strip().lower() in ["exit", "quit"]:
                farewell(session)
                break
            # Warn, don't block: the turn goes to the model exactly as typed.
            if player_styles and speaker_of(user_input, player_styles) is None:
                console.print(
                    Text("[no speaker prefix — this turn won't be attributable]",
                         style=WARN_STYLE))

        console.print()
        console.print(Text("[Sending prompt to GM...]", style="dim"))
        console.print(turn_tag(turn_no) + Text(" GM > ", style=GM_STYLE), end="")

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
            waiting = retry_after(error)
            waited = f", waiting {waiting:.0f}s as asked" if waiting else ""
            # `explain`'s headline over `describe`'s: mid-session, "the model is
            # overloaded" tells you to sit tight, where "google returned 503
            # UNAVAILABLE" reads like something to go debug.
            console.print(
                Text(f"\n[retry {attempt}/{session.max_attempts - 1}: "
                     f"{explain(error).headline}"
                     f"{waited} — discarding the above, restarting the reply]",
                     style=WARN_STYLE))
            # Same request re-issued, so it is still the same turn number.
            console.print(turn_tag(turn_no) + Text(" GM > ", style=GM_STYLE), end="")

        try:
            session.send(user_input, on_chunk=on_chunk, on_retry=on_retry)
            if pending:
                console.print(colorize(pending, name_pattern, player_styles), end="")
        except Exception as e:
            if pending:
                console.print(colorize(pending, name_pattern, player_styles), end="")
            console.print()
            # Automated retries are spent (or the failure was never transient),
            # so the last resort is a human deciding to try again.
            report_failure(e, session)

            # Offered even when the failure is permanent: the history only lives
            # in this process, so ending a 40-turn session to wait out a reset
            # throws away the conversation. Staying open costs nothing. What
            # changes is the wording — inviting a plain retry after a 404 or a
            # spent daily cap would be inviting a known-futile keypress.
            hint = ("Press Enter to retry the same turn..."
                    if explain(e).retryable else
                    "Press Enter to try this turn again, or Ctrl-D to end the session...")
            pending_input = user_input  # replay the same turn on the next iteration
            try:
                console.input(Text(hint, style="dim"))
            except (EOFError, KeyboardInterrupt):
                console.print()
                farewell(session)
                break
            continue

        console.print("\n")


if __name__ == "__main__":
    main()
