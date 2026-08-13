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
