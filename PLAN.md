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
| CP13 | this | Charts and README — the first thing a judge reads |

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

Next is D5: the three-screen viewer and the voice clips.
