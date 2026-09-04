# settle — PLAN

Checkpoint log. CC owns this file (SPEC §18).

It read `(pending)` from CP0 to CP12, because PLAN.md was on no checkpoint's
allowlist between CP4 and CP12 — the one file CC owns was the one file CC was
never permitted to touch. Fixed at CP12.1 by A109: PLAN.md goes on every
allowlist by default from here.

The entries below CP12 are reconstructed from commit subjects rather than from
notes taken at the time, and are deliberately one line each. They are a record
of what shipped, not a retrospective invented after the fact.

| Checkpoint | Commit | What shipped |
|---|---|---|
| CP0–CP1 | `3a11fa1` | Spec freeze, schema contracts, gate script |
| CP2 | `2881249` | Batch generator, indexed random streams, hidden-truth separation |
| CP2.2 | `37b825f` | Complete INV-10 coverage |
| CP2.3 | `4404b77` | Liquidity window width into PARAMS |
| CP3 | `b199a4c` | Diagnosis, gates, stops |
| CP3.1 | `a8e4e03` | Whitelist taxonomy, two more gates, escalation moved out of sim |
| CP4 | `b0af955` | Case runner, hash-chained ledger, executor boundary |
| CP4.1 | `e6f63f5` | Cadence into PARAMS, slow marker, G4 counts submissions |
| CP5 | `cd13d6e` | EXPLORE, the action grid, remaining baselines |
| CP5.1 | `786e491` | Scheduling — a retry offset is a commitment, not a label |
| CP6 | `8f0c238` | Reconciliation, silent-failure auditor, distorting reporting layer |
| CP6.1 | `7fb894f` | Natural recovery, replies consumed, shared reporting streams |
| CP7.0 | `0aa42c4` | The honest escalation rate |
| CP7 | `07d77b5` | The estimator, and the confound it exposed |
| CP7.1 | `ecbef08` | Timing features, thin-cell oversampling, text-keyed escalation cache |
| CP8 | `de07efe` | The OURS policy — dead heat on recovery, rout on everything else |
| CP10 | `56144fe` | Fix uplift resolution, OURS beats B2 |
| CP11 + CP11.1 | `1d27c60` | Sourcing, sensitivity, two fixes the sourcing found |
| CP12 | `55b6e47` | Razorpay test mode, real at the edges |
| CP12.1 | `1444842` | Self-verifying artefact, then done with Razorpay |
| CP12.2 | `1aa5de8` | Three loose ends, then charts |
| CP13 | `4be14ea` | Charts and README — the first thing a judge reads |
| CP13.1 | `e7d93e8` | 10k run, the class breakdown in results, the reconciliation finding |
| CP13.2 | this | Timing figures corrected, the two hypotheses split |

## CP12 — Razorpay test mode, real at the edges

`settle/integrations/razorpay_client.py`, `settle/integrations/idempotency.py`,
`settle/api/webhook.py`, `settle/api/app.py`. 20 new tests, WBH-1..6 and
RZP-1..3. 684 green.

One real Razorpay test-mode payment link (`plink_TWr7e2EFJ8ITvn`) for
`case_000000`, paid, three webhooks delivered through ngrok, HMAC-verified
before parsing, joined on `case_id` from the link notes, written to a
hash-chained edge ledger under arm `EDGE`.

Nothing in the simulation moved: no arm, no policy, no metric, no prior.

What the checkpoint actually established, beyond the ids:

- Real-vs-synthetic labelling enforced by the type. `RAZORPAY_TEST_MODE` or
  `MOCK_SANDBOX` on every record, the pairing validated, records frozen. Mock
  ids wear `MOCK_plink_`; mock URLs sit on the reserved `.invalid` TLD.
- `RAZORPAY_MOCK_MODE` defaults true, so a clone with no keys runs.
- Signature verification strictly before body parsing (WBH-3).
- Processing after the response, inside Razorpay's 5-second budget (WBH-5).

Two things the checkpoint could not do, recorded rather than worked around: the
demo runner had no allowlisted home, and `httpx` was unpinned so `TestClient`
was unavailable. Both closed at CP12.1.

## CP12.1 — self-verifying artefact, then done with Razorpay

The CP12 artefact had one real weakness. Razorpay's checkout SMS-verifies the
payer's phone number, so the real payment carried a real mobile number, and the
first fix was to hash the raw event and strip the number before committing.

That is unverifiable, and worse than it looks. A hash computed over content the
reader cannot see, published beside content they can, reduces to "trust me, it
verified before I edited it" — the exact claim about payment outcomes this
project exists to refuse. An artefact making it about its own integrity would be
self-refuting.

Fixed by defining the published chain over a **contact-free projection** built
from a fixed allow-list of fields. Contact fields are absent from the schema, not
blanked. The hash covers what is published, so a reader recomputes over exactly
the bytes in front of them. RZP-4 is that recomputation, and it is the artefact's
whole value.

Also closed here:

- `scripts/razorpay_demo.py` — the entry point, mock by default (F8).
- `httpx==0.28.1` pinned; the ASGI-direct webhook tests kept, because only they
  can timestamp `http.response.start` (F9).
- WBH-4's interpretation recorded in SPEC §16: the ledger records every
  delivery, the store records the event once (F10).
- PLAN.md restored to every allowlist (F11).
- Raw events gitignored at `out/razorpay_raw*.json`.
- The declined attempt kept in the artefact, with the cause named so it is not
  read as our bug.

The payment was not redone. The existing one stands.

## CP12.2 — three loose ends, then charts

No code changed. A docstring, the docs, and one API call.

- `plink_TWrboR36RZ13fH` cancelled (F13). It was raised for `case_000001` when
  Razorpay refused a second link for `case_000000`, never paid, and still live
  after the tunnel went back to an unrelated app — a payment on it would have
  put Razorpay into a 24-hour retry loop against a 404 and then disabled the
  webhook. One paid link and one cancelled link remain; nothing is live.
- `tests/test_webhook.py`'s "No TestClient" docstring corrected (F14). It
  claimed httpx was unpinned after CP12.1 pinned it, which made a preference
  read as a constraint. Discharges CP12.1's BLOCKED note.
- Known Limitations opened in README (F15): one link per case from
  `reference_id = case_id`, the projection scope and what it does and does not
  let a reader verify, the international-card decline as an account property
  rather than our behaviour, and the edge being one link rather than a load
  test.

Found while doing F15: "Known Limitations" is referenced six times across
SPEC.md and PRIORS.md and had never been written — every reference has pointed
at nothing since CP2. The section now exists, and its first paragraph names all
six outstanding entries rather than implying the four present ones are the whole
list. Writing them needs SPEC §12 and PRIORS open; that is D5.

Razorpay is done.

## CP13 — charts and README

`settle/eval/report.py` and `settle/eval/charts.py`, `KNOWN_LIMITATIONS.md`, the
README in SPEC §19's fixed order, and CHT-1/2/3. 697 tests green.

The structural decision is the seam between the two new modules. `report.py`
runs five arms and reconciles each, then writes `out/charts/metrics.json`;
`charts.py` draws only from that file. Without the seam, CHT-1's determinism
check would need a simulation in the loop and the committed PNGs would not be
reproducible by anyone cloning the repo.

CHT-3 is the one that carries the checkpoint. It pulls every number out of the
committed README and requires each to appear in a committed artefact. Verified
adversarially rather than assumed: injecting a fabricated 34.71% into the
headline table fails it, and a real figure attributed to the wrong arm fails the
companion test.

### What the by-class breakdown found

The entire OURS margin is one decline class. `auth_abandoned` goes to OURS by
48.9 points. It LOSES to B2 on three of six: `dead_instrument` by 10.7,
`transient` by 3.8, and `time_shiftable` — 899 of 2,000 cases — by 3.4. The
aggregate 27.90% against 25.65% hides that completely.

Nothing had broken the incremental rate down by class before. The sweep varies
priors and the headline table aggregates; neither asks where the money comes
from. It is reported in the README results section rather than in Known
Limitations, because it is a fact about the result and not a caveat on it.

The obvious next experiment follows from it and has not been run: a hybrid using
OURS on `auth_abandoned` and the ladder elsewhere would likely beat both.

### Known Limitations, discharged

The six references that had pointed at a non-existent section since CP2 are
written, alongside the measured negative results: withdrawn retry timing, 184 of
188 priors asserted, the calibration trade stated as two numbers, the 4x flip on
an asserted prior, the auditor's simulation-only validation, and the three world
bugs that each invalidated a headline before being fixed.

## CP13.1 — 10k, and what F21 caught

Every headline number is now the 10,000-case run SPEC §3 specifies. The
2,000-case figures live in the artefact's `comparison` block so a reader who has
seen both can see the divergence rather than wonder which is current.

`report.py` now runs each arm exactly once — `by_decline_class` takes the runs
instead of making its own, which at 10,000 cases removes three redundant arm
runs from the critical path. They had to agree with the headline table by
construction, so the duplication could only ever have been a bug.

### F21 found that the figures it was auditing were wrong

The README carried "median spread 3.7 points across eight offsets; timing
features rank 26-37 of 45". Recomputing with train.py's own parameters
reproduces neither, and the feature count is 46 — SPEC's A93 says so, and A93 is
what changed them. The stale numbers predate A93, which recomputed
`days_since_last_attempt` at the dispatch moment and added
`hours_to_contact_window`. They were carried from an old training log through
CP13's prompt into the README without the training being re-run.

`train.py` also groups four features as "timing" and they are not one thing.
A83's withdrawn claim was about reaching a LIQUIDITY WINDOW —
`day_of_month_at_dispatch`, `days_to_month_start`, `in_liquidity_window`.
`days_since_last_attempt` measures recency, a different hypothesis and a much
stronger feature. Reporting the four together understated one and overstated the
other, so `timing_block` separates them.

A83's conclusion survives. What changed is that the numbers supporting it are
recomputed from a committed artefact and checked by a test, rather than quoted
from a log nobody re-ran. The figures sat in the README for exactly one
checkpoint, and they were only caught because F21 forced them into an artefact.
That is the argument for CHT-3 in a single incident.

### The reconciliation finding, reframed

Reported-minus-reconciled is negative for every arm. The auditor was built
expecting overstatement — money claimed and never settled — and the measured
error runs the other way. The concrete harm is not a wrong dashboard number; it
is customers who had already paid and were still being chased.

## CP13.2 — the two timing hypotheses, separated

No code changed. Documents, one new artefact, and two corrections.

A83's "retry timing" was two hypotheses reported as one, and separating them
changes what each says. LIQUIDITY TIMING — retries near payday recover more —
was the stated differentiator and it is dead: the three features rank 22, 34 and
40 of 46, and `world.liquidity_window_days` moves the headline 0.30 points
across the full 0.25x–4x sweep, having been a REQUIRED sweep member since CP2.3
precisely because we expected it to matter. RECENCY survived:
`days_since_last_attempt` ranks 2 of 46, and the probability moves a median 6.0
points across the eight offsets. Reporting them together understated the second
and overstated the first. A83's withdrawal stands, unchanged in scope.

`out/model_report.json` carries both, plus the superseded figures, so the
correction is auditable rather than a silent edit.

### Two prescribed numbers did not survive checking

The feature-to-rank pairing was transposed — `day_of_month_at_dispatch` is 22
and `days_to_month_start` is 40, not the reverse — and the liquidity sweep moves
the headline 0.30 points, not 0.6. Both corrected before being written. The
conclusion is unaffected and is stronger for the second.

### SF-2's bare count was hiding the mechanism

F23's prescribed explanation for B3 was that it "exhausts contact budgets
earlier". It does not: B3 makes 24,780 contacts against B2's 14,027, and it runs
in OBSERVE where the gates that impose a budget do not bind.

Measured instead. SF-2 needs a settlement the agent never heard about AND a
contact after it, so the blind set is the denominator. B3's blind set is 384
against B2's 984, because acting more means more settlements get reported at
all. Per blind case B3 is worse — 9.1% against 8.7%. Fewer opportunities, not
more discipline.

OURS has the largest blind set of any acting arm, 976, and converts 0.1% of it.
That is a better statement of the restraint claim than the count of 1.

### The margin narrows with sample size

2.25 points at 2,000 cases, 1.72 at 10,000, with B2 gaining more. Per-class is
less stable still: `ambiguous` moves from +2.3 to −2.9, so the six per-class
figures CP13 reported at 2,000 cases were not reportable. Recorded in Known
Limitations with what it costs — the contact ratio is the robust half of the
result, the recovery gap is not.

§19 is rewritten once and frozen at nine sections. It had been amended in three
consecutive checkpoints.

## CP14 — the viewer

`viewer/index.html`, one hand-written file: vanilla JS, hand-written CSS, no
framework, no npm, no CDN, no build step. `settle/eval/report.py` writes
`out/viewer_data.json` and the page renders it.

The rule that shapes it: **JS renders, Python computes.** Every number on every
screen arrives pre-formatted — percentages, rupee strings, labels, counts — and
VIW-2 scans the file for arithmetic. A viewer that derived a rate would be a
second implementation of a metric that already exists in `report.py`, and the
two would disagree the first time either changed. It is the same span-locate /
code-evaluate split the text reader uses, applied to the UI.

Screen 2 is the one that earns the checkpoint. Every decision expands to every
option the policy priced, with P(settle), EV, whether a gate would have allowed
it, and which gate blocked it when not. Sorted by expected value, the chosen row
marked, blocked rows in among them rather than in a footnote — an option that
would have won on economics and was stopped by a gate is the most interesting
row on the screen. Filters by arm, decline class, reconciliation outcome and
gate blocks, with three demo cases pre-selected by id so a demo does not depend
on hunting.

Screen 3 is a working uploader against a route that returns 501. It renders the
501 as the honest answer it is and names the checkpoint the endpoint lands in.

### The data is embedded as well as written

A browser will not `fetch()` a sibling file from a `file://` origin — Chrome
treats each as an opaque origin. So `report.py` writes the JSON file *and*
inlines a copy into the page between markers. Served, the page prefers the file
so a regenerated run shows on reload; from disk, the embedded copy is the only
one a browser will read. That is what makes VIW-4 true rather than aspirational.

Traces are capped at 40 cases per arm plus any with a silent failure, and at 8
decisions each. A single OURS case holds thirty daily decisions enumerating the
whole grid — 330 alternatives — and sixty of those was a four-megabyte page. The
alternatives inside a shown decision are never capped, because those are what
VIW-3 is about and a truncated option list is a decision log that has started
lying.

### VIW-4 executes rather than asserts

"Opens from file:// with no server" is easy to assert about and hard to assert
of. There is no jsdom and adding one would mean an npm dependency in a project
whose viewer constraint is "no build step", so the test carries a small DOM shim
and runs the real script under Node with `location.protocol = "file:"` and
`fetch` throwing. A page that depended on a fetch to render screens 1 or 2 fails
there rather than in front of a judge.

## CP15 — voice, and what gpt-4o-transcribe actually returned

`settle/text/voice.py`, `settle/text/promise.py`, `settle/api/voice.py`, screen 3,
and VOI-1..8. All four clips transcribe, extract and replay from cache.

### Three transcription findings, and they were not small

DECISIONS warned that gpt-transcribe returns Devanagari for Hindi regardless of
the `language` parameter. Measured, it is worse in three ways.

**It returned Urdu.** With `language="hi"` and no prompt, all four clips came
back in Arabic script — a third script no parser was written for. Every clip
would have classified as unclear.

**It truncated.** Clip 1's un-prompted transcript stops at "agle mahine kar
dunga" and drops the self-correction, which is the only reason that clip exists.
The audio contains it; the transcription discarded it.

**It was not deterministic.** At the default temperature the same file
transcribed four times gave three different strings, one of them truncated.

A romanised-Hinglish `prompt` fixes the script and recovers the clause;
`temperature=0` makes it 4/4 identical. Both are correctness settings, not
preferences, and `PROMPT_VERSION` is in the cache key so a steering change
invalidates the transcripts it produced rather than serving them under a
configuration that no longer exists.

### promise.py adds no judgement

It is a trace layer over `classify.py`, which already implements the CP7.0
contract — locate, parse, validate, decide. Re-implementing any of it would give
the batch path and the voice path two different notions of what a promise is,
and the first divergence would be invisible until a demo.

It adds one span kind: `next_month`, always rejected, because a month with no day
is not a commitment. Located anyway so clip 1's "agle mahine" can be shown being
considered and set aside rather than never looked at. Without it, VOI-3 would
pass vacuously on a transcript with only one span.

### The four verdicts

    clip 1  promise   2026-01-15   agle mahine rejected, pandrah tareekh accepted
    clip 2  promise   2026-01-17   ek hafte mein, anchored to created_at
    clip 3  hedged    none         sets nothing — no date, no suppression
    clip 4  opt_out   none         opted_out set, S4 fires

Clip 3 is the demo. Anyone can extract a date from clip 1; refusing to log a
polite brush-off as a promise, and therefore not suppressing contact for weeks on
a customer who was being courteous, is the judgement no competing submission
shows.

### Carry-forward

F31: A127 and A128 applied, pending since CP13.3. F32: `GET /` serves the viewer
and the CP12 assertion that it returns 501 is updated — both files were open,
which is why it waited.

## CP16 — final tidy

The last code commit before submission. No behaviour changed that a judge will
see; four things that were owed got paid.

**The `.gitignore` audit.** `out/razorpay_demo.json` and `out/voice_demo.json`
were tracked only because they had been force-added at checkpoints whose
allowlists excluded `.gitignore`. Third occurrence, so this was an audit rather
than a fourth one-line fix: every path in `git ls-files out/` was checked with
`git check-ignore --no-index`, which is the only way to ask the question of an
already-tracked file. Nine committed artefacts, nine negations, and every run
artefact still ignored.

**`python-multipart` pinned, `/voice/extract` on `UploadFile`.** The CP15 raw-body
shape is still accepted, deliberately: the viewer posts that way and
`viewer/index.html` was not on this allowlist. A route that broke its only caller
to adopt a tidier signature would be a refactor charged to the demo.

**The transcription findings moved into README results.** They were the strongest
"what broke" material in the project and they were sitting in a limitations file.
Urdu instead of Devanagari, a silently truncated clause, and three different
strings from four calls on identical audio — with the before/after on the same
clip, so a reader can see what one sentence of prompt was worth.

**Known Limitations gained two entries.** `next_month` is located on the demo
path only, so the voice lab is a preview of the batch plus one span the batch
would not see — identical verdicts, but identical because the difference is
always discarded, which is weaker than identical. And clip 2's transcript differs
from what was said: same meaning, date unaffected, not re-recorded, because
re-recording until the transcript matches would be selecting the fixture to
flatter the model.

**F37, the hand check.** Every number in the README prose was extracted and
matched against the committed artefacts — 46 curated figures plus a full token
sweep. One gap: the declined payment id was in `out/razorpay_demo.json` and not
in the README, so the "a real decline beside a real capture" claim named only the
capture. Added. Nothing in the README is unbacked.

The code is done. Next is the video.

## CP17 — the HYBRID arm

The margin was concentrated in one decline class and the obvious experiment had
not been run. It has now.

HYBRID routes `auth_abandoned` to OURS and every other class to B2's ladder. It
composes two arms already in the table — one `OursArm`, one `FixedLadderArm`,
delegated to — and adds no model, no mechanism and no parameter. Routing is per
case, on `classify(case.decline_code)`, so a case belongs to one delegate for its
whole life; that is what makes ARM-7's byte-identity possible at all.

    incremental rate    OURS 28.37%    HYBRID 31.87%    B2 26.65%
    contacts                      36            9,192           14,027
    opt-outs induced               1              285              411
    cost per Rs100           0.0892           0.2084           0.2765
    SF-2                           1               50               86
    blind set                  1,114              929            1,112
    SF-2 / blind set            0.1%             5.4%             7.7%

It recovers 3.50 points more and contacts 255 times as many people. The restraint
result does not survive routing: HYBRID's contact volume is 66% of the fixed
ladder's. It goes in README Next Steps, not Results. OURS remains submitted.

The per-class rates are B2's exactly on every ladder-routed class and OURS's
exactly on `auth_abandoned` — the composition visible in the output rather than
asserted. Nothing outside HYBRID's rows moved, checked by diffing metrics.json
against its pre-run copy field by field.

`opt_outs_induced` was listed in §14.4 from CP0 and produced by nothing. HYBRID
is what made the gap conspicuous, and it is counted now from the ledger.
