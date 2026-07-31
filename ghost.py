import datetime
import json
import os
import re
import time
import uuid
from dotenv import load_dotenv
from google import genai
from google.genai import types
from rich.console import Console
from rich.text import Text

load_dotenv()

MODEL = "gemini-3.5-flash-lite"

# Deliberately naive baseline system prompt — do not improve.
SYSTEM_PROMPT = "You are a Dungeons & Dragons 5e game master. Run the session."

PARTY_FILE = "party.txt"
TRANSCRIPT_DIR = "transcripts"


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
                 transcript_dir=TRANSCRIPT_DIR, client=None, session_id=None):
        self.model = model
        self.system_prompt = system_prompt
        self.config = types.GenerateContentConfig(system_instruction=system_prompt)
        self.client = client if client is not None else genai.Client()

        self.session_id = session_id or uuid.uuid4().hex[:8]
        os.makedirs(transcript_dir, exist_ok=True)
        self.transcript_path = os.path.join(transcript_dir, f"{self.session_id}.jsonl")

        # Local conversation history (system prompt lives in config, not here)
        self.contents = []
        self.turn = 0

    def send(self, user_input, on_chunk=None):
        """Blocking turn. `on_chunk(text)` fires per stream chunk."""
        turn = self._begin(user_input)
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
        except Exception:
            self._abort()
            raise
        return self._commit(user_input, turn)

    async def send_async(self, user_input, on_chunk=None):
        """Same turn over the async client; `on_chunk` is awaited."""
        turn = self._begin(user_input)
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
        except Exception:
            self._abort()
            raise
        return self._commit(user_input, turn)

    def _begin(self, user_input):
        self.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_input)]))
        return _Turn()

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
# Reserved for the GM: magenta. Players draw from here, in party.txt order.
PLAYER_PALETTE = ["cyan", "green", "yellow", "blue", "red", "bright_cyan", "bright_green"]


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
    console.print()

    # Set when a turn fails, so the next iteration replays it instead of
    # asking for fresh input.
    pending_input = None

    while True:
        if pending_input is not None:
            user_input, pending_input = pending_input, None
        elif session.turn == 0 and party_text is not None:
            user_input = party_text
            console.print(Text("You > ", style=YOU_STYLE) + Text(f"[loaded {PARTY_FILE}]", style="dim"))
        else:
            try:
                user_input = console.input(Text("You > ", style=YOU_STYLE))
            except (EOFError, KeyboardInterrupt):
                console.print()
                user_input = "exit"
            if user_input.strip().lower() in ["exit", "quit"]:
                console.print(
                    f"Ending session. Transcript saved to {session.transcript_path}. Farewell!",
                    style="dim",
                )
                break

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

        try:
            session.send(user_input, on_chunk=on_chunk)
            if pending:
                console.print(colorize(pending, name_pattern, player_styles), end="")
        except Exception as e:
            if pending:
                console.print(colorize(pending, name_pattern, player_styles), end="")
            console.print(Text(f"\n[Error during generation: {e}]", style="bold red"))
            pending_input = user_input  # retry the same turn on the next iteration
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
