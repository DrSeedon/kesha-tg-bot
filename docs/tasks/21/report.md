# #21 — Report: accuracy fixes to the compact prompt + code-guaranteed verbatim tail

Gate decision: implement the three fixes justified by **observed defects**; defer the 10→7 section
merge until the current prompt has accumulated enough compactions to measure against.

## What shipped

### 1. Both polarities banned + `UNKNOWN — source gap`
The prompt banned only the *positive* unsupported claim ("do not claim a file was read"). Measured
in 18 real production summaries: **7 inverted claims**, including the textbook one:

```
`CLAUDE.md` — Not modified this session
```

New wording bans both directions, supplies `no evidence of X`, and adds `UNKNOWN — source gap`.

**Deliberately narrow:** 6 of those 7 negatives were *legitimate* — Kesha's PENDING genuinely
tracks "КБЖУ July 21 file — NOT created yet". A blanket ban would have destroyed real backlog
tracking. The rule therefore permits a flat negative when *the conversation itself established it*,
and forbids it only when it is inferred from missing tool evidence.

### 2. Explicit zero-exit on the file-write permission
Added "Otherwise do not write any file. Never create a new note, CLAUDE.md, TODO.md, or BUGS.md
solely for compaction."

**Not** Orchestra's hard ban — see the measurement below; ours earns its keep.

### 3. Verbatim tail guaranteed by code (`append_verbatim_tail`)
`compact.py` now appends the last real user messages under
`[VERBATIM TAIL — appended by runtime]`, after validation and through the redactor. The model still
produces `RECENT VERBATIM`: code guarantees *presence*, the model supplies *interpretation*.

## Measurements (mine, on our own data — not ported)

**Summary size is not our problem** — this retired the original "shrink 3601 → 1500" framing:

```
our summaries: n=18, median 8232 B ~= 2058 tok = 0.206 % of the 1M window
post-compact floor (12 real compactions): 4-5 % ~= 45 000 tok
summary share of that floor: 4.6 %
halving the summary would save 0.103 % of the window
```

**Section overlap (why the merge is right but deferred):** 25 overlapping concrete anchors in the
one real new-prompt summary; worst offenders `FILES↔TEMPORAL STATE` (3) and
`PENDING↔TEMPORAL STATE` (3). `TEMPORAL STATE` is an *attribute*, not a question.

**Stray writes — the hypothesis that failed:**

```
22 compaction windows audited -> 2 file writes total, 1 distinct file
(Orchestra: 218 stray writes / 126 runs)
```

Both writes were durable user knowledge (an OMS/medical finding appended to an existing
`CLAUDE.md`), not compaction noise. Porting the hard ban would have deleted a feature.

**Tail filtering, verified against the live DB at implementation time:**

```
last 60 role=user rows: 10 are reminders/system (17 %)
naive "last 3" windows containing a non-user row: 18/57 = 32 %

on the current production window:
  naive: 3 rows -> 2 misattributed as user speech
  ours:  1 row  -> 0 misattributed
```

Two of the three newest `role='user'` rows were fired reminders with `message_id=None`. A naive
deterministic tail — the "obvious 5-line win" — would have presented them as the user's own words.

## Files

| File | ± | What |
|---|---|---|
| `compact.py` | +64/-4 | both-polarity rule, zero-exit, `append_verbatim_tail`, `recent_rows` param |
| `chat_state.py` | +18 | `_recent_user_rows()` + wiring |
| `tests/test_compact_prompt.py` | +100 | 9 new tests |
| `docs/tasks/21/` | new | research.md, report.md, prod-summaries-18.json |

## Tests — 198 passed (was 189)

The #14 harness (10 fixtures, scorer, validator tests) ran green before and after: **no regression**
on the transaction, `_GARBAGE_PATTERNS`, or `_validate_summary_sections`.

Mutation matrix — revert the guard, the test must go RED. All 5 confirmed:

| # | Mutation | Result |
|---|---|---|
| M1 | drop the negative-polarity half | `test_prompt_bans_both_polarities...` RED |
| M2 | remove the zero-exit | `test_prompt_gives_the_write_permission...` RED |
| M3 | stop filtering reminders | 2 tail tests RED |
| M4 | wrong slice direction (newest-first) | `test_recent_user_rows_are_oldest_first...` RED |
| M5 | let tail failure escape | `test_recent_user_rows_never_raises...` RED |

## Hard constraint (#14) — how it was protected

- **Append happens AFTER `_validate_summary_sections`**, so runtime-supplied user text can never
  satisfy the structural contract, and it is re-run through `_redact_high_confidence_secrets`.
- The validator anchors on the **last** `CONTINUATION` match. I probed this directly: appending a
  block containing a bare `CONTINUATION`, `OBJECTIVE`, or `RECENT VERBATIM` header line still
  validates (the duplicate check only scans the prefix before `RECENT VERBATIM`). A test pins it.
- The transaction, `_GARBAGE_PATTERNS`, and `SUMMARY_SECTIONS` are untouched.
- Tail retrieval is wrapped so a DB failure returns `[]` and never aborts a compaction.

## Bugs found in my own code before commit

1. **`get_history` is `ORDER BY id DESC`** — my first `users[-limit:]` took the *oldest* three
   messages. Caught by reading the SQL, pinned by M4.
2. **`message_id` can be `None`, not just `0`** — surfaced on live data (a `TypeError` in my own
   analysis script). The truthiness filter handles it; noted so nobody "tidies" it into `!= 0`.

## Deferred (orchestrator's call, agreed)

10 → 7 sections (`TEMPORAL STATE` inline into PENDING; `COMMANDS AND TOOL OUTCOMES` merged into
`STATE AND ARTIFACTS`). Justified by the 25-anchor overlap, but its benefit is only provable by a
recall comparison, and the "before" baseline is n=1. It is also the only change that rewrites the
validator contract (`SUMMARY_SECTIONS[:-2]`) and the #14 fixtures. Revisit after 5–10 compactions.

## Codex

No verdict — quota exhausted until 2026-08-08. Not substituted, not faked.

## Lesson (reusable)

A measured baseline can be the wrong population. 17 of my 18 "current" samples turned out to be
from the *previous* prompt; the change under review had run exactly once in production. Check what
version produced your evidence before comparing against it — otherwise you are measuring the thing
you already replaced.
