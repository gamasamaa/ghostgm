"""Read HP changes out of the GM's prose.

This is a heuristic and it lives entirely in the visual layer — the GM is
never asked to phrase things a particular way, so the baseline transcript
stays comparable to a bare `ghost.py` session.

Deliberately conservative: a name only counts when it is the subject sitting
immediately in front of the verb. "Bram swings and the Cultist Leader takes 9
damage" must not cost Bram 9 HP.
"""

import re
from typing import List, NamedTuple


class Delta(NamedTuple):
    token_id: str
    name: str
    hp_delta: int
    phrase: str  # the sentence fragment we matched, so the UI can justify itself


# The name has to sit immediately in front of the verb. That adjacency is what
# does the work: "Bram swings and the Cultist Leader takes 9 damage" can't
# charge Bram, because "Bram" is followed by "swings". A possessive breaks it
# the same way, so "Bram's shield takes 9 damage" is ignored too.
#
# An earlier version also demanded a clause boundary before the name. It
# prevented nothing that adjacency doesn't already prevent, and it threw away
# real hits ("Vela watches in horror as Bram takes 6 damage"), so it's gone.
SUBJECT = r"\b(?:the\s+)?({names})"

DAMAGE = r"\s+(?:takes|suffers|is hit for|takes a total of)\s+(\d+)\s*(?:points?\s+of\s+)?(?:\w+\s+)?damage"
HEAL_SELF = r"\s+(?:regains|recovers|heals for|is healed for)\s+(\d+)"
HEAL_TARGET = r"(?:heals|restores|cures)\s+(?:the\s+)?({names})\s+(?:for|by)\s+(\d+)"


def _alternation(names):
    """Longest name first, so "Bram" never shadows "Brammit"."""
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


class HPExtractor:
    def __init__(self, tokens):
        """`tokens` is any iterable of objects with `.id` and `.name`."""
        self.by_name = {t.name.lower(): t.id for t in tokens}
        self.patterns = []
        if not self.by_name:
            return

        names = _alternation(t.name for t in tokens)
        subject = SUBJECT.format(names=names)
        self.patterns = [
            (re.compile(subject + DAMAGE, re.IGNORECASE), -1),
            (re.compile(subject + HEAL_SELF, re.IGNORECASE), 1),
            (re.compile(HEAL_TARGET.format(names=names), re.IGNORECASE), 1),
        ]

    def extract(self, text: str) -> List[Delta]:
        """Every HP change stated in `text`, in the order it was narrated."""
        found = []
        for pattern, sign in self.patterns:
            for m in pattern.finditer(text):
                name, amount = m.group(1), int(m.group(2))
                token_id = self.by_name.get(name.lower())
                if token_id is None:
                    continue
                found.append((m.start(), Delta(
                    token_id=token_id,
                    name=name,
                    hp_delta=sign * amount,
                    phrase=" ".join(m.group(0).split()).lstrip(".,;:—– "),
                )))
        return [delta for _, delta in sorted(found, key=lambda pair: pair[0])]
