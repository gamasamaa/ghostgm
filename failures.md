# Phase 0 failure log

One line per observed failure, recorded during play. This list is the taxonomy
every later phase maps back to — a phase that doesn't fix a failure written down
here doesn't need to exist.

Format:

```
s<session>/t<turn> | category | what happened, with the evidence
```

`s<session>` is the transcript's session id, `t<turn>` its `turn` field, so
every line points at a record in `transcripts/<session_id>.jsonl`.

Categories to watch for (add others as they show up):

- `invented dice` — a result stated with no roll requested and no modifier shown
- `state drift` — HP, conditions or inventory that don't follow from prior turns
- `spell slots` — casting past what the sheet allows
- `rules error` — mechanics that contradict the SRD
- `continuity` — an NPC's name, trait or location changing between turns
- `no turn order` — combat run without initiative, or the party addressed as one entity
- `unenforced` — the party breaks a rule and the GM allows it

## Log

<!-- Example of the format; delete once the first real entry lands.
s3/t14 | invented dice | "you rolled 17" — no roll requested, no modifier shown
-->
s1/t3-4 | combat-order | the gm rolled for initiative on its own, didnt ask to roll for the players, rolled all attack rolls for players without prompting for dice rolls
s1/t3-4 | rules-error | added the bless roll onto bram's attack swing damage, but it should only be added to attack rolls, not damage rolls, rolled all the dice for bram's attack
s1/t3-4 | combat-order | ran all the turns of the characters sequentially, didnt ask for turns, didnt enforce turn orders
s1/t5-4 | combat-order | didnt enforce the turn order, allowed vela to go first even though the first turn is nix
s1/t5-4 | combat-order | didnt enforce the turn order, oro attack before nix or bram
s1/t5-6 | invented-dice | the gm fully played bram's turn without asking for any dice rolls from the player, invented a number of outcomes and applied them to the narrative, no dice were rolled
s1/t7 | state-drift | after combat, noted vela's health as 25/24, corrected itself to 25/25 later
s1/t8 | invented-dice | on a lockpicking attempt, the gm rolled the dice itself without prompting for a dice roll, only stated the outcome of the action
s1/t10 | invented-dice | rolled bram's attack without prompting, it was a hit, rolled damage without prompting
s1/t12 | rules-error | trap was disabled without any type of check being requested, nor any description of the check or attempted action given
s1/t15 | rules-error | oro used arcane recovery, the gm decided for him what spells slots to recover, didnt prompt the player
s1/t17 | invented-dice | nix attacked, the gm invented the rolls without prompting, and later just played out the rest of the turns completely independently without prompting anything else
s1/t18 | invented dice | vela wasnt prompted to roll dice to cure wounds
s1/t18 | combat-order | no initiative was rolled, no turn other is enforced
s1/t20 | combat-order | nix played his turn, then the gm played all the other turns without any prompting, didnt allow oro to act
s1/t21 | combat-order | nix played his turn, then the gm played all the other turns without any prompting, didnt allow oro to act
s1/t22 | rules-error | the gm didnt allow oro to use other spells while concentrating on web

## Alternative reading — AI-assisted labeling

**Everything below this line was produced by Claude, not by hand.** The log above
is the human record and stays authoritative. This section only re-points those
same observations at the transcript, and adds two findings the hand pass missed.
Delete it wholesale if it is not wanted; nothing above depends on it.

The `s1/tN` numbers above were counted by hand during play, before the CLI
showed a turn tally, and they do not line up with the `turn` field in
`transcripts/f037b7c5.jsonl` — the offset is not constant, so it cannot be fixed
by arithmetic. Each line below was matched to its real turn by finding the
described event in the transcript. Session id is the real one, `f037b7c5`.

Confidence is noted where the match is not exact. Categories are left exactly as
the human wrote them, including `combat-order`, which is that log's name for
what the category list at the top calls `no turn order`.

| original | real turn | matched on |
|---|---|---|
| s1/t3-4 ×3 | **t5** | initiative order listed with no roll; Nix's attack rolled by the GM |
| s1/t5-4 | **t8** | GM writes "**Nix:** (Hasn't taken action yet this round)" while resolving Vela |
| s1/t5-4 | **t9** | Oro's three scorching rays resolved while Nix and Bram still owe turns |
| s1/t5-6 | **t10** *(uncertain — could be t5)* | Bram's whole turn played inside Nix's response |
| s1/t7 | **t10** | verbatim "25/24... wait, 25/25 HP" |
| s1/t8 | **t18** | the only lockpicking attempt in the session |
| s1/t10 | **t22** | "Bram Longsword Attack: 17 + 5 = 22", damage "1d8 (5) + 3" |
| s1/t12 | **t25** | "Oro: disables the trap" — GM response contains no check |
| s1/t15 | **t30** | Arcane Recovery |
| s1/t17 | **t35** | Nix's attack rolled, then Bram's and Oro's turns played in the same reply |
| s1/t18 | **t37** | "Healing rolled: 1d8 (7) + Wisdom modifier (+3)" |
| s1/t18 | **t35** | boss fight opens with no initiative |
| s1/t20 | **t38** | Nix acts, GM then plays Bram and Oro unprompted |
| s1/t21 | **t40** | same pattern against the lizardfolk |
| s1/t22 | **t38** *(or t40)* | Oro "maintains his concentration on the *Web* spell" |

### The same log, renumbered

sf037b7c5/t5 | combat-order | gm rolled for initiative on its own, didnt ask to roll for the players, rolled all attack rolls for players without prompting for dice rolls
sf037b7c5/t5 | rules-error | added the bless roll onto bram's attack swing damage, but it should only be added to attack rolls, not damage rolls — verbatim "*Bless:* +3 = **12 total damage!**"
sf037b7c5/t5 | combat-order | ran all the turns of the characters sequentially, didnt ask for turns, didnt enforce turn orders
sf037b7c5/t8 | combat-order | didnt enforce the turn order, allowed vela to go first even though the first turn is nix
sf037b7c5/t9 | combat-order | didnt enforce the turn order, oro attacked before nix or bram
sf037b7c5/t10 | invented-dice | the gm fully played bram's turn without asking for any dice rolls from the player
sf037b7c5/t10 | state-drift | noted vela's health as 25/24, corrected itself to 25/25 in the same sentence
sf037b7c5/t18 | invented-dice | on a lockpicking attempt, the gm rolled the dice itself without prompting, only stated the outcome
sf037b7c5/t22 | invented-dice | rolled bram's attack without prompting, it was a hit, rolled damage without prompting
sf037b7c5/t25 | rules-error | trap was disabled without any type of check being requested, nor any description of the check given
sf037b7c5/t30 | rules-error | oro used arcane recovery, the gm decided for him what spell slots to recover, didnt prompt the player
sf037b7c5/t35 | invented-dice | nix attacked, the gm invented the rolls without prompting, and played out the rest of the turns independently
sf037b7c5/t35 | combat-order | no initiative was rolled, no turn order is enforced
sf037b7c5/t37 | invented-dice | vela wasnt prompted to roll dice to cure wounds
sf037b7c5/t38 | combat-order | nix played his turn, then the gm played all the other turns without any prompting, didnt allow oro to act
sf037b7c5/t40 | combat-order | nix played his turn, then the gm played all the other turns without any prompting, didnt allow oro to act
sf037b7c5/t38 | rules-error | the gm didnt allow oro to use other spells while concentrating on web

### Not in the original log — two findings from re-reading the transcript

The first is the largest single failure in the session and is invisible turn by
turn: it only appears if you keep a ledger across thirty turns.

sf037b7c5/t11 | spell-slots | vela's t8 cure wounds was never deducted — at t11 the gm still reports "2 / 3 remaining (used 1 for Bless)", and repeats 2x 1st-level at t14, t19, t28 and t36
sf037b7c5/t39 | spell-slots | across the session vela casts five 1st-level spells from a 3-slot pool — bless t4, cure wounds t8, bless t33, cure wounds t37, cure wounds t39 — and the gm's counter only drops to 0 at t39
sf037b7c5/t28 | spell-slots | oro's t20 web (2nd level) was never deducted — the gm still reports "3x 1st-level slots, 1x 2nd-level slot" at t28 and repeats it as "unchanged" at t29
sf037b7c5/t40 | spell-slots | oro is reported with "2x 2nd-level slots left" after three 2nd-level casts (scorching ray t9, web t20, web t34) from a 2-slot pool — even crediting arcane recovery at t30 he cannot be above 0
sf037b7c5/t29 | rules-error | the whole party is restored to full hp — "Bram: 31/31 HP (Fully healed!)" — in the same breath as "Spell slots unchanged: 2x 1st, 2x 2nd", so the healing cost nothing at all
sf037b7c5/t29 | rules-error | a "solid ten-minute breather" is treated as a rest that restores hp; a short rest is an hour and returns hp only by spending hit dice

The two slot failures share one mechanism, which is what makes them worth a
phase of their own: a cast is narrated correctly, is never deducted, and the GM
then states the wrong remaining count with complete confidence for twenty-plus
turns. It is undetectable from any single turn.

Checked and cleared, recorded so they are not re-opened: Oro's AC rising from 12
to 15 is correct (Mage Armor at t5, 13 + Dex); his drop to 3x 1st-level is
correct for that cast; and Bram's hp arithmetic is right at every point except
t29 — 31 to 19 across t5, healed to 28 at t8, 17 after the t35 necrotic hit,
27 at t37, 18 at t38, 29 at t39.

**Negative result, worth as much as the positives:** `continuity` really is zero
across all 43 turns, not merely unlogged. The Scale-Witch holds her name from t3
to t10, Malakor from t27 to t42, and no NPC changes name, role or location. This
GM does not drift on identity, so a later phase aimed at continuity would be
fixing a failure this baseline does not exhibit.


