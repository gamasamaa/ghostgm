"""The uniformity guarantee: prep must reproduce party.txt byte for byte.

The fixture below is the existing party expressed as sheets. Rendering it has
to equal the real file exactly — that is what lets the prepper write party.txt
without any layer downstream noticing a difference.

The derived values (ac/hp/attacks/dc/slots) are stated literally here for now.
Once prep.rules lands they get computed from the `abilities`/`armor`/`weapons`
inputs already recorded below, and this same assertion proves the rules engine.
"""

import os

import pytest

from prep.render import (parse_party_text, render_character, render_party,
                         render_slots)
from prep.sheet import Attack, CharacterSheet, Party

PARTY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "party.txt")


@pytest.fixture
def party_text():
    with open(PARTY_PATH, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def party():
    return Party(
        system="D&D 5e",
        level=3,
        premise="We're in the sewers under Waterdeep hunting a cult. Begin the session.",
        characters=[
            CharacterSheet(
                name="Bram", race="human", char_class="fighter", level=3,
                abilities={"STR": 16, "DEX": 12, "CON": 16, "INT": 8, "WIS": 13, "CHA": 10},
                armor="chain mail", shield=True, weapons=["longsword"],
                ac=18, hp=31, prof=2,
                attacks=[Attack(name="Longsword", bonus=5, damage="1d8+3")],
            ),
            CharacterSheet(
                name="Nix", race="halfling", char_class="rogue", level=3,
                abilities={"STR": 10, "DEX": 18, "CON": 14, "INT": 12, "WIS": 13, "CHA": 8},
                armor="leather", weapons=["shortbow"],
                ac=15, hp=24, prof=2,
                attacks=[Attack(name="Shortbow", bonus=6, damage="1d6+4")],
                notes=["Sneak attack 2d6"],
            ),
            CharacterSheet(
                # HP 25 is one over the fixed average (24) — legal rolled HP,
                # and the reason hp_mode exists.
                name="Vela", race="half-elf", char_class="cleric", level=3,
                abilities={"STR": 12, "DEX": 14, "CON": 14, "INT": 10, "WIS": 16, "CHA": 13},
                armor="scale mail", hp_mode="rolled",
                ac=16, hp=25, prof=2,
                spell_save_dc=13, spell_slots={1: 3, 2: 2},
            ),
            CharacterSheet(
                name="Oro", race="dwarf", char_class="wizard", level=3,
                abilities={"STR": 8, "DEX": 14, "CON": 14, "INT": 18, "WIS": 12, "CHA": 10},
                ac=12, hp=20, prof=2,
                spell_save_dc=14, spell_slots={1: 4, 2: 2},
            ),
        ],
    )


def test_renders_party_txt_byte_for_byte(party, party_text):
    assert render_party(party) == party_text


def test_no_trailing_newline(party):
    # party.txt has none; adding one would show up as a diff in every fork.
    assert not render_party(party).endswith("\n")


def test_import_round_trips(party_text):
    assert render_party(parse_party_text(party_text)) == party_text


def test_import_reads_the_structure(party_text):
    imported = parse_party_text(party_text)

    assert imported.level == 3
    assert imported.system == "D&D 5e"
    assert [c.name for c in imported.characters] == ["Bram", "Nix", "Vela", "Oro"]
    assert imported.premise.endswith("Begin the session.")

    bram = imported.get("bram")
    assert (bram.race, bram.char_class) == ("human", "fighter")
    assert (bram.ac, bram.hp) == (18, 31)
    assert bram.attacks[0].model_dump() == {"name": "Longsword", "bonus": 5, "damage": "1d8+3"}

    nix = imported.get("Nix")
    assert nix.notes == ["Sneak attack 2d6"], "a rider is a note, not an attack"
    assert nix.attacks[0].name == "Shortbow"

    vela = imported.get("Vela")
    assert vela.race == "half-elf", "hyphenated races stay whole"
    assert vela.spell_save_dc == 13
    assert vela.spell_slots == {1: 3, 2: 2}
    assert vela.attacks == []

    assert imported.get("Oro").spell_slots == {1: 4, 2: 2}


def test_nothing_imported_needed_the_escape_hatch(party_text):
    # raw_line is only set when a line can't be reproduced from its fields.
    assert all(c.raw_line is None for c in parse_party_text(party_text).characters)


def import_one(line):
    return parse_party_text(f"Party of 1, D&D 5e, level 3:\n{line}\n\nGo.").characters[0]


def test_prose_segment_survives_as_a_note():
    line = "- Zed, tiefling warlock. AC 13, HP 22. Eldritch blast: two beams, 1d10 each."
    sheet = import_one(line)
    assert sheet.notes == ["Eldritch blast: two beams, 1d10 each"]
    assert sheet.raw_line is None, "it reproduces from its fields, so no escape hatch"
    assert render_character(sheet) == line


def test_reordered_line_falls_back_to_verbatim():
    # Slots before notes is not the order we emit, so this can't be rebuilt
    # from fields — it must be kept exactly as written rather than rewritten.
    line = "- Zed, tiefling warlock. AC 13, HP 22. 3x 1st. Agonizing blast."
    sheet = import_one(line)
    assert sheet.raw_line == line
    assert render_character(sheet) == line


def test_render_slots():
    assert render_slots({1: 4, 2: 2}) == "4x 1st, 2x 2nd"
    assert render_slots({2: 2, 1: 3}) == "3x 1st, 2x 2nd", "levels come out in order"
    assert render_slots({1: 4, 2: 3, 3: 2}) == "4x 1st, 3x 2nd, 2x 3rd"


def test_segment_order_is_stable():
    sheet = CharacterSheet(
        name="Ash", race="tiefling", char_class="paladin", ac=19, hp=28,
        attacks=[Attack(name="Warhammer", bonus=6, damage="1d8+4")],
        notes=["Divine sense 2/day"], spell_save_dc=14, spell_slots={1: 3},
    )
    assert render_character(sheet) == (
        "- Ash, tiefling paladin. AC 19, HP 28. Warhammer +6, 1d8+4. "
        "Divine sense 2/day. Spell save DC 14. 3x 1st."
    )


def test_minimal_character_renders():
    sheet = CharacterSheet(name="Mote", race="human", char_class="commoner", ac=10, hp=4)
    assert render_character(sheet) == "- Mote, human commoner. AC 10, HP 4."
