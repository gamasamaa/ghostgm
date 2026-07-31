"""Party <-> party.txt.

party.txt is the uniform format every layer reads, so rendering is held to
byte-equality: `render_party` on the canonical fixture must reproduce the
existing file exactly (see tests/test_render.py).

A character line is segments joined by ". " and closed with ".":

    - Nix, halfling rogue. AC 15, HP 24. Shortbow +6, 1d6+4. Sneak attack 2d6.
      \\_____ identity ____/  \\_ defenses _/  \\____ attacks ___/  \\__ notes __/

with `Spell save DC 13` and `3x 1st, 2x 2nd` following in that order when the
character has them.
"""

import re
from typing import Dict, List, Optional

import ghost
from prep.sheet import Attack, CharacterSheet, Party

ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th",
            6: "6th", 7: "7th", 8: "8th", 9: "9th"}

# Segment shapes, used to classify the tail of an imported line.
DEFENSE_RE = re.compile(r"^AC (\d+), HP (\d+)$")
ATTACK_RE = re.compile(r"^(?P<name>.+?) (?P<bonus>[+-]\d+), (?P<damage>\d+d\d+(?:\s*[+-]\s*\d+)?)$")
DC_RE = re.compile(r"^Spell save DC (\d+)$")
SLOTS_RE = re.compile(r"^\d+x \d+(?:st|nd|rd|th)(?:, \d+x \d+(?:st|nd|rd|th))*$")
SLOT_RE = re.compile(r"(\d+)x (\d+)(?:st|nd|rd|th)")


# ---------------------------------------------------------------------------
# Party -> text
# ---------------------------------------------------------------------------

def render_slots(slots: Dict[int, int]) -> str:
    """{1: 3, 2: 2} -> '3x 1st, 2x 2nd'."""
    return ", ".join(f"{slots[lvl]}x {ORDINALS[lvl]}" for lvl in sorted(slots))


def render_character(sheet: CharacterSheet) -> str:
    if sheet.raw_line:
        return sheet.raw_line

    segments = [f"{sheet.name}, {sheet.race} {sheet.char_class}".rstrip(),
                f"AC {sheet.ac}, HP {sheet.hp}"]
    segments += [a.render() for a in sheet.attacks]
    segments += sheet.notes
    if sheet.spell_save_dc is not None:
        segments.append(f"Spell save DC {sheet.spell_save_dc}")
    if sheet.spell_slots:
        segments.append(render_slots(sheet.spell_slots))

    return "- " + ". ".join(segments) + "."


def render_party(party: Party) -> str:
    """The full file. No trailing newline — party.txt doesn't have one."""
    lines = [f"Party of {len(party.characters)}, {party.system}, level {party.level}:"]
    lines += [render_character(c) for c in party.characters]
    if party.premise:
        lines += ["", party.premise]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# text -> Party (importing a party.txt someone already wrote)
# ---------------------------------------------------------------------------

HEADER_RE = re.compile(r"^Party of \d+, (?P<system>.+?), level (?P<level>\d+):", re.IGNORECASE)


def split_segments(line: str) -> List[str]:
    """Split a roster line on '. ' without breaking '1d8+3.' style tails."""
    body = line.strip().lstrip("-*").strip().rstrip(".")
    return [s.strip() for s in body.split(". ") if s.strip()]


def parse_character(line: str) -> CharacterSheet:
    """Best-effort import of one roster line.

    Identity/AC/HP come from ghost.parse_party so the two layers agree on what
    a roster line is; everything after that is classified segment by segment.
    """
    parsed = ghost.parse_party(line)
    if not parsed:
        raise ValueError(f"not a roster line: {line!r}")
    member = parsed[0]

    race, _, char_class = (member["description"] or "").rpartition(" ")
    sheet = CharacterSheet(
        name=member["name"],
        race=race,
        char_class=char_class,
        ac=member["ac"] if member["ac"] is not None else 10,
        hp=member["hp"] if member["hp"] is not None else 1,
    )

    for segment in split_segments(line)[1:]:  # [0] is identity, already handled
        if DEFENSE_RE.match(segment):
            continue  # ghost.parse_party already read AC/HP off the raw line
        dc = DC_RE.match(segment)
        if dc:
            sheet.spell_save_dc = int(dc.group(1))
            continue
        if SLOTS_RE.match(segment):
            sheet.spell_slots = {int(lvl): int(n) for n, lvl in SLOT_RE.findall(segment)}
            continue
        attack = ATTACK_RE.match(segment)
        if attack:
            sheet.attacks.append(Attack(
                name=attack.group("name"),
                bonus=int(attack.group("bonus")),
                damage=attack.group("damage").replace(" ", ""),
            ))
            continue
        sheet.notes.append(segment)

    # Self-check: if we can't reproduce the line we were given, keep it verbatim
    # rather than silently rewriting someone's roster.
    if render_character(sheet) != line.strip():
        sheet.raw_line = line.strip()
    return sheet


def parse_party_text(text: str) -> Party:
    party = Party()
    body_lines = []

    for line in text.splitlines():
        header = HEADER_RE.match(line.strip())
        if header:
            party.system = header.group("system")
            party.level = int(header.group("level"))
            continue
        if re.match(r"\s*[-*]\s", line):
            party.characters.append(parse_character(line))
            continue
        if line.strip():
            body_lines.append(line.strip())

    # Whatever prose is left over after the header and the roster is the premise.
    party.premise = " ".join(body_lines)
    return party


def load_party_file(path: str = ghost.PARTY_FILE) -> Optional[Party]:
    text = ghost.read_party_file(path)
    return parse_party_text(text) if text is not None else None
