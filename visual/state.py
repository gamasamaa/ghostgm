from typing import Dict, List, Optional
from pydantic import BaseModel

import ghost

PLAYER_COLORS = [
    "#06b6d4",  # cyan
    "#22c55e",  # green
    "#eab308",  # yellow
    "#3b82f6",  # blue
    "#ef4444",  # red
    "#a855f7",  # purple
]

ENEMY_COLOR = "#f43f5e"  # bright crimson red

# party.txt states AC/HP for everyone in practice; these only cover a roster
# line that leaves them out. ghost.parse_party deliberately reports None there.
DEFAULT_AC = 10
DEFAULT_HP = 20


class Token(BaseModel):
    id: str
    name: str
    class_info: str = "Adventurer"
    ac: int = 10
    hp: int = 10
    max_hp: int = 10
    x: int = 0
    y: int = 0
    color: str = "#06b6d4"
    status: List[str] = []
    is_enemy: bool = False


class GameState(BaseModel):
    grid_cols: int = 12
    grid_rows: int = 12
    party_location: str = "Unknown Location"
    tokens: Dict[str, Token] = {}
    enemies: Dict[str, Token] = {}
    turn: int = 0
    combat_active: bool = False


def party_tokens(party_text: Optional[str]) -> List[Token]:
    """Turn the roster into starting tokens, down the left edge of the grid."""
    tokens = []
    if not party_text:
        return tokens

    for i, member in enumerate(ghost.parse_party(party_text)):
        hp = member["hp"] if member["hp"] is not None else DEFAULT_HP
        tokens.append(
            Token(
                id=member["name"].lower(),
                name=member["name"],
                class_info=member["description"] or "Hero",
                ac=member["ac"] if member["ac"] is not None else DEFAULT_AC,
                hp=hp,
                max_hp=hp,
                x=2,
                y=4 + i * 2,
                color=PLAYER_COLORS[i % len(PLAYER_COLORS)],
                is_enemy=False,
            )
        )
    return tokens


def demo_enemies() -> List[Token]:
    """Placeholder opposition until enemies can be added at runtime."""
    return [
        Token(id="cultist_1", name="Cultist Leader", class_info="Fanatic",
              ac=13, hp=22, max_hp=22, x=8, y=5, color=ENEMY_COLOR, is_enemy=True),
        Token(id="cultist_2", name="Sewer Cultist", class_info="Initiate",
              ac=11, hp=12, max_hp=12, x=8, y=7, color=ENEMY_COLOR, is_enemy=True),
    ]


class StateManager:
    def __init__(self, party_path: str = ghost.PARTY_FILE):
        self.party_path = party_path
        self.state = GameState()
        self.reset_from_party()

    def reset_from_party(self):
        """Rebuild the board from scratch: full HP, starting positions, both sides."""
        party_text = ghost.read_party_file(self.party_path)
        self.state.tokens = {t.id: t for t in party_tokens(party_text)}
        self.state.enemies = {t.id: t for t in demo_enemies()}
        self.state.turn = 0
        self.state.combat_active = False

    def get_token(self, token_id: str) -> Optional[Token]:
        return self.state.tokens.get(token_id) or self.state.enemies.get(token_id)

    def all_tokens(self) -> List[Token]:
        return list(self.state.tokens.values()) + list(self.state.enemies.values())

    def move_token(self, token_id: str, x: int, y: int) -> Optional[Token]:
        target = self.get_token(token_id)
        if target is None:
            return None
        # Clamp within grid bounds
        target.x = max(0, min(self.state.grid_cols - 1, x))
        target.y = max(0, min(self.state.grid_rows - 1, y))
        return target

    def update_hp(self, token_id: str, delta: int) -> Optional[Token]:
        target = self.get_token(token_id)
        if target:
            target.hp = max(0, min(target.max_hp, target.hp + delta))
            return target
        return None
