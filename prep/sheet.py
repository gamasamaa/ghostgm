"""The canonical party: richer than party.txt, and the thing prep actually saves.

party.txt stays the uniform interchange format — `prep.render` writes it — but
it only carries a slice of what a sheet knows. The JSON here is the source of
truth; the text file is a projection of it.

Fields split three ways:
  inputs   — what a player decides (abilities, armor, weapons)
  derived  — what the rules compute from those (ac, hp, attacks, dc, slots)
  claimed  — what a model asserted, kept only so it can be scored against derived

Nothing in this module computes anything; `prep.rules` owns that.
"""

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel

ABILITIES = ("STR", "DEX", "CON", "INT", "WIS", "CHA")


class Attack(BaseModel):
    name: str  # "Longsword"
    bonus: int  # 5
    damage: str  # "1d8+3"

    def render(self) -> str:
        """The party.txt form: 'Longsword +5, 1d8+3'."""
        return f"{self.name} {self.bonus:+d}, {self.damage}"


class CharacterSheet(BaseModel):
    # --- identity ---
    name: str
    race: str = ""
    char_class: str = ""
    level: int = 1
    background: Optional[str] = None

    # --- inputs the rules work from ---
    abilities: Dict[str, int] = {}
    armor: Optional[str] = None  # srd key, e.g. "chain mail"; None = unarmored
    shield: bool = False
    weapons: List[str] = []  # srd keys, e.g. ["longsword"]
    hp_mode: Literal["average", "rolled"] = "average"

    # --- derived (prep.rules fills these; stored so the JSON stands alone) ---
    ac: int = 10
    hp: int = 1
    prof: int = 2
    attacks: List[Attack] = []
    spell_save_dc: Optional[int] = None
    spell_slots: Dict[int, int] = {}  # {1: 4, 2: 2}
    notes: List[str] = []  # free riders: "Sneak attack 2d6"

    # --- assisted mode only: what the model asserted, for scoring ---
    model_claimed: Dict[str, Any] = {}

    # Escape hatch for imported lines this model can't reproduce exactly. Set
    # only by the importer, and only when a re-render wouldn't match the
    # original; editing any field should clear it.
    raw_line: Optional[str] = None

    def modifier(self, ability: str) -> int:
        """Convenience for callers that just need a mod off the stored scores."""
        return (self.abilities.get(ability, 10) - 10) // 2


class Party(BaseModel):
    system: str = "D&D 5e"
    level: int = 1
    characters: List[CharacterSheet] = []
    premise: str = ""  # the trailing line of party.txt, sent as the first turn

    def get(self, name: str) -> Optional[CharacterSheet]:
        for c in self.characters:
            if c.name.lower() == name.lower():
                return c
        return None