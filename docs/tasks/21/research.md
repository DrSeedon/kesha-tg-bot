# #21 — Should Kesha's COMPACT_PROMPT adopt Orchestra's #106 findings?

## Question

- **Context**: `compact.py:12` `COMPACT_PROMPT`, 3601 chars, 10 sections. Orchestra rewrote its own
  compaction prompt (#106) and measured size −61%, exact recall 87→95%, stray file writes 218→0,
  false claims 8→0.
- **Change under test**: port Orchestra's six principles (esp. fewer non-overlapping sections,
  both-polarity bans, explicit `UNKNOWN — source gap`, deterministic verbatim tail).
- **Baseline**: our current 10-section prompt as it behaves on real Kesha traffic.
- **Measurable outcome**: on *our* production summaries — section load, section overlap, stray
  writes during compaction, false/inverted claims, and the real cost of summary size.

## Hypotheses considered

| # | Hypothesis | Falsifier | Verdict |
|---|---|---|---|
| H1 | Our prompt is too big; shrinking it is the main win (as it was for Orchestra) | Compute what summary size actually costs in our context budget | **REFUTED** |
| H2 | Our 10 sections overlap, so material lands unpredictably (their principle 4) | Look for the same concrete anchor in 2+ sections of a real summary | **CONFIRMED** |
| H3 | Our permission-to-write clause causes stray writes (their 218→0 story) | Count files actually written during real compaction windows | **REFUTED for us** |
| H4 | Our single-polarity ban produces inverted false claims (their principle 2) | Grep real summaries for "not read / не читались" style claims | **CONFIRMED** |
| H5 | Verbatim recall should move from prompt to code (their principle 5) | Check whether our message log can reconstruct "last 3 user messages" faithfully | **PARTLY — see F6** |

## Findings

### F0 — The measured baseline is almost entirely the OLD prompt. CONFIRMED (important caveat)
Evidence tier 1. I recovered **18 real production summaries** from the prod journal
(`docs/tasks/21/prod-summaries-18.json`). Section headings show **17 use the pre-#14 7-section
prompt** (`INTENT/FILES/PENDING/BUGS/IMPORTANT CONTEXT`) and only **1 uses the current 10-section
prompt** — #14 landed `1aba008` on 2026-07-31 and only one compaction has run under it since
(2026-08-01 23:03, 10722 B).

**Consequence:** any "before/after" I report for the current prompt rests on n=1. The orchestrator's
demand for a before/after measurement on real compactions is only partly satisfiable today, and I
will not dress up n=1 as a trend. This is itself a finding: **we changed the prompt in #14 and
shipped it essentially unmeasured.**

### F1 — Summary size is NOT our problem. REFUTED (H1)
Evidence tier 1 (measurement + arithmetic on our own numbers):

```
our summaries:  n=18  median 8232 B  mean 8401 B  (min 5856, max 13454)
old prompt (n=17): median 7997 B     new prompt (n=1): 10722 B
Orchestra's new median: 2046 B
```

So yes, we are ~4× their new median and worse than their *old* one. But the cost:

```
median summary 8232 B ~= 2058 tokens  = 0.206 % of our 1M window
post-compact context floor (measured): 4-5 %  ~= 45 000 tokens
summary share of that floor: 4.6 %   (the rest is system prompt + MCP schemas + CLAUDE.md)
halving the summary saves ~1029 tokens = 0.103 % of the window
```

Measured post-compact floors, 12 consecutive real compactions: `20→5, 20→5, 25→4, 20→4, 29→4,
35→4, 20→4, 42→4, 25→5, 21→5, 26→5, 64→5` (%). Orchestra runs code agents on a tight budget where
halving the handoff matters; **we land at 4–5 % either way.** Optimising bytes here buys ~0.1 % of
context and risks losing knowledge that is genuinely expensive to reconstruct.

**Therefore: do not adopt "shorter" as the goal.** Adopt the *accuracy* parts.

### F2 — Sections DO overlap, and `TEMPORAL STATE` is the worst offender. CONFIRMED (H2)
Evidence tier 1. On the single real new-prompt summary, I extracted concrete anchors (file paths,
3+ digit numbers, hashes) per section and counted anchors appearing in 2+ sections — **25
overlapping anchor pairs**, top ones:

```
 3  FILES AND ARTIFACTS  <-> TEMPORAL STATE        ['110','111','2026']
 3  PENDING AND BLOCKERS <-> TEMPORAL STATE        ['110','111','200']
 2  FILES AND ARTIFACTS  <-> PENDING AND BLOCKERS  ['110','111']
 2  FILES AND ARTIFACTS  <-> CONTINUATION          ['.../food-log/2026-08-01.md','2026']
```

This is exactly their principle-4 test ("a fact that legitimately fits two sections ⇒ bad design").
`TEMPORAL STATE` is not a *question the next session asks* — it is an **attribute** of a deadline,
a file, or a pending item, so it necessarily duplicates them. Same for `COMMANDS AND TOOL OUTCOMES`
vs `FILES AND ARTIFACTS`: a command's outcome usually *is* a file state.

Per-section load, current prompt (n=1) and old prompt (n=17):

| current (n=1) | B | % | | old (n=17) | median B | present |
|---|---|---|---|---|---|---|
| FILES AND ARTIFACTS | 1716 | 16.0 | | IMPORTANT CONTEXT | 1696 | 17/17 |
| UNCERTAINTY AND CONFLICTS | 1613 | 15.0 | | DECISIONS | 1272 | 17/17 |
| PENDING AND BLOCKERS | 1142 | 10.7 | | RECENT | 1306 | **9/17** |
| USER FACTS AND PREFERENCES | 1055 | 9.8 | | PENDING | 1127 | 17/17 |
| DECISIONS | 991 | 9.2 | | FILES | 1046 | 17/17 |
| COMMANDS AND TOOL OUTCOMES | 968 | 9.0 | | BUGS | 650 | 17/17 |
| RECENT VERBATIM | 895 | 8.3 | | INTENT | 452 | 17/17 |
| TEMPORAL STATE | 836 | 7.8 | | | | |
| OBJECTIVE | 735 | 6.9 | | | | |
| CONTINUATION | 547 | 5.1 | | | | |

Every section is populated — none is dead weight. The problem is **boundaries, not count.**

### F3 — Stray writes during compaction: we have essentially none. REFUTED (H3)
Evidence tier 1. I isolated every compaction window in the journal (from
`Compact: requesting summary` to `got summary`/`failed`) across **22 windows** and counted
`Write`/`Edit` tool calls inside them:

```
compaction windows: 22
distinct files written DURING compaction: 1
   2x /opt/cog-second-brain/CLAUDE.md
```

**2 writes in 22 compactions**, versus Orchestra's 218 stray writes / 126 runs. And the content is
*not* noise — both are durable user knowledge, e.g. a real medical/OMS finding:

```
Edit CLAUDE.md new_string: "## 🏥 Медицина по ОМС — что реально работает (опыт 28.07.2026)
- **Бот «Здравоохранение Красноярского края» в мессенджере MAX** — прямая запись к
  специалистам, без регистратуры и без направления. ЛОР записан за минуту..."
```

This is exactly what the orchestrator told me to check before banning. **Recommendation: do NOT
port their hard ban.** Our clause already differs from `_ORCH_PRESAVE` (it says "update an
*existing canonical* note", not "append to CLAUDE.md/TODO.md/BUGS.md NOW"), and the measured
behaviour is 2 useful writes, not 218 junk ones.

**But their principle 3 still applies in weakened form:** our clause has *no explicit zero-exit*.
Code check of our prompt:

```
NO   explicit zero-exit for file writes ("otherwise do not write")
NO   ban on creating a note solely for compaction
NO   BOTH polarities banned (absence != evidence)
NO   explicit UNKNOWN token for gaps
YES  verbatim last-N user messages
YES  secret redaction
```

Adding "if no existing canonical destination is named, write nothing" costs nothing and preserves
the 2 good writes (both targeted an existing named file).

### F4 — The single-polarity ban is ALREADY producing inverted claims in prod. CONFIRMED (H4)
Evidence tier 1. Their principle 2 predicts that banning only the positive claim makes the model
assert the negative. Grepping our 18 real summaries: **7 instances**, verbatim:

```
[2]  `CLAUDE.md` — Not modified this session
[2]  "Понимай" = priority evening read ... Not read yet
[6]  July 21 tracked but **file NOT created**: ~1671 kcal
[6]  **КБЖУ July 21 file** — NOT created yet (data recorded: ~1671 kcal)
[6]  **Daily dump July 22** — not created yet
[7]  **КБЖУ 21.07 file** — still NOT created (data: ~1671 kcal)
[7]  **Daily dump 24.07** — not created yet
```

`CLAUDE.md — Not modified this session` is precisely the failure mode they describe: an absence of
tool evidence rendered as a positive claim about the world.

**Nuance worth keeping (counter-evidence):** most of the other six are *legitimate* — "file not
created yet" in PENDING is a real, user-confirmed backlog item, not a hallucinated negative. So the
fix is **not** "ban all negatives"; it is their exact three-way formulation: assert only what
evidence supports, write `no evidence of X` when the tool record is silent, and reserve flat
negatives for facts the conversation established. This distinction matters for us more than for
Orchestra, because Kesha's PENDING legitimately tracks "not done yet" items.

### F5 — Adding `UNKNOWN — source gap` is free and we lack it. CONFIRMED
Evidence tier 2 (prompt inspection, above). We say "never guess to fill a gap" but give no token to
write instead. Their principle: without a third option the model picks one of two lies.

### F6 — Deterministic verbatim tail: feasible, but NOT the 5-line win it looks like. PARTLY (H5)
Evidence tier 1. `message_log.get_history()` exists and `chat_state.py:695` logs the full batched
prompt (transcribed voice included), so appending the tail in code is mechanically possible.

But measuring the actual rows shows the trap. `role='user'` includes **reminder firings**:

```
last 60 role=user rows: 10 are reminders/system (17%), 50 real user messages
naive last-3 windows containing a non-user row: 18/57 = 32%
```

A naive deterministic "last 3 user messages" would present a **fired reminder as the user's own
words 32 % of the time**. Filtering `message_id != 0` helps but does not close it (1/60 rows still
carries `REMINDER FIRED`, because a *batch* row can bundle a reminder with `msg_id=0`).

Meanwhile the model, on the one real new-prompt sample, handled this correctly *because it
understood the semantics*:

```
3. `[2026-08-01 20:00]` — не сообщение пользователя, а сработавшая напоминалка #87 (КБЖУ-чек).
   **После моего ответа на неё пользователь не отвечал.**
```

It also repaired a garbled transcription and flagged it. A dumb tail cannot do either.

**Counter-evidence for the prompt side:** under the OLD prompt `RECENT` appeared in only **9/17**
summaries — asking is demonstrably unreliable. Under the new prompt it appeared 1/1.

**Recommendation:** implement the deterministic tail, but as an **additive, clearly-labelled block
appended by `compact.py`** (`[VERBATIM TAIL — appended by runtime]`), keeping the model's
`RECENT VERBATIM` section. Code guarantees presence; the model supplies interpretation. Do **not**
let the runtime block replace the model section, and do not ship a naive "last 3 rows".
Requires filtering non-user rows and re-validating `_summary_sections_ordered` (see risks).

## Counter-evidence / what argues against acting

- **Their numbers are not ours.** Different task (code orchestration vs personal assistant with a
  knowledge base), different runtime, different prompt. Per the orchestrator, recall 24→100 % came
  from a harness appender that does not exist in prod — **not cited here as support.**
- **n=1 for the current prompt.** Any claim that the 10-section prompt is "worse in practice" is
  unproven. The overlap finding (F2) rests on that single sample plus prompt structure.
- **Kesha's writes are valuable** (F3). Porting the strict ban would destroy a feature.
- **Shrinking risks real loss** (F1): the win is ~0.1 % of context; the downside is losing durable
  knowledge that took a long conversation to establish.

## Recommendation (for the gate — NOT implemented)

Adopt the **accuracy** principles (2, 3-weakened, 4, 5), reject the **size** goal.

1. **7 sections, merged by question — not by "get to 4".** Each answers a distinct question:
   - `OBJECTIVE` — where am I and why (keep)
   - `USER FACTS AND PREFERENCES` — what is durably true about the user (keep; this is Kesha's core, absent from Orchestra's 4)
   - `DECISIONS` — what is settled and must not be relitigated (keep)
   - `STATE AND ARTIFACTS` — **merge** `FILES AND ARTIFACTS` + `COMMANDS AND TOOL OUTCOMES`; a command outcome is a state change, and they shared anchors
   - `PENDING AND BLOCKERS` — what is unfinished, **with its own deadline inline** (absorbs `TEMPORAL STATE`, which F2 shows is an attribute, not a slot)
   - `UNCERTAINTY AND CONFLICTS` — what is unresolved (keep; 15 % of the real summary, and it is what stops false consensus)
   - `RECENT VERBATIM` + `CONTINUATION` — keep both (validator depends on their order; `CONTINUATION` is the single next action)
   Net: 10 → 7, removing exactly the two slots proven to overlap. Justified by non-overlap, not by a target number.
2. **Both-polarity rule + `UNKNOWN — source gap`**, phrased to preserve legitimate PENDING negatives (F4).
3. **Explicit zero-exit on the write permission** — keep the permission (F3 proves it earns its
   keep), add "otherwise write nothing; never create a note solely for compaction".
4. **Deterministic verbatim tail in `compact.py`**, additive and labelled, with non-user rows
   filtered (F6).

## Affected files, risks, edge cases

- `compact.py` — `COMPACT_PROMPT`, `SUMMARY_SECTIONS`, and the tail appender.
- **HARD CONSTRAINT (#14):** `_summary_sections_ordered` (`compact.py:141-183`) validates section
  presence/order and treats `RECENT VERBATIM`/`CONTINUATION` specially; `_GARBAGE_PATTERNS` and the
  transaction must not change. Any section rename/removal **changes the validator contract** —
  `SUMMARY_SECTIONS[:-2]` is load-bearing. Appending a tail after `CONTINUATION` must be proven not
  to break ordering validation (the validator uses the *last* `CONTINUATION` match, so an appended
  block containing that literal would shift it).
- `tests/test_compact_prompt.py`, `tests/compact_summary_scorer.py`,
  `tests/fixtures/compact_summary_cases.json`, `scripts/evaluate_compact_prompt.py` — the #14
  harness already exists (10 fixtures) and encodes the old 10-section contract; renaming sections
  requires updating fixtures, or the eval measures the wrong thing.
- Risk: prod has run the current prompt **once**. A second rewrite before the first is measured
  means we never learn which change did what. Mitigation in the plan: keep the eval harness as the
  before/after instrument and report per-fixture deltas.

## Sources

1. Direct measurement — 18 real production summaries extracted from the Contabo journal, saved to
   `docs/tasks/21/prod-summaries-18.json`; section/anchor/negation analysis run this session.
2. Direct measurement — compaction-window write audit (22 windows) and `messages.db` row analysis
   on the prod DB, read-only.
3. Primary source — `compact.py` (this repo) and `/mnt/data/Projects/Python/orchestra/app/session.py:1187`
   (read-only), both read this session.
4. Primary source — `compact-prompt-explained-106.html`, read in full this session (6 principles,
   the `_ORCH_PRESAVE` rationale, and the author's own caveat that recall 24→100 % came from the
   harness appender).
