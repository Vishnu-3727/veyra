# AI Finance Controller

**Razorpay Buildathon 2026 — Track 04.** Multi-source financial reconciliation with evidence-based automation: we automate the financial decisions we can support with evidence, explain those decisions, and safely surface the ones we cannot.

## What this is

A small, credible finance-ops system that reconciles **payment gateway transactions** against **bank settlement records**, using **internal invoices** as corroborating evidence. It is not a chatbot: deterministic rules make every decision that can be made from facts alone (amounts, dates, references, duplicates); an LLM is consulted only for the minority of genuinely ambiguous cases; and a non-negotiable policy layer re-validates every AI proposal before it can become a financial decision. Anything that isn't safely resolvable becomes a structured, explainable exception instead of a guess.

## Architecture

```
data/generate_dataset.py        synthetic 3-source dataset + ground truth (seeded, reproducible)
        |
        v
app/ingestion.py                CSV -> SQLite, validates required fields, never silently coerces bad data
        v
app/normalization.py            pure functions: name/ref normalization, amount/date parsing, fuzzy similarity
        v
app/candidates.py               blocking (settlement window + amount tolerance + name/ref trace) -> candidates
        v
app/scoring.py                  deterministic decision tree: AUTO_MATCH / NEEDS_AI / EXCEPTION
        v
app/ai_reasoning.py  ----\      LLM call, ONLY for genuinely ambiguous candidates; narrow, structured I/O
        v                 \
app/policy.py                   hard guardrails on AI output (confidence floor, amount-mismatch cap,
        v                       candidate-set membership) -- AI can propose, never unilaterally decide
app/exceptions.py                structures every unresolved case for a human reviewer
        v
app/pipeline.py                 orchestrates the above, persists decisions/audit_log/exceptions to SQLite
        v
app/evaluation.py               scores decisions against ground truth (never seen by the engine itself)
        v
app/api.py (FastAPI)  <-----> dashboard/app.py (Streamlit)
```

Every box is a small, independently-testable module. No queues, no vector DB, no agent framework -- a single SQLite file and ~10 focused Python modules are enough to make the guarantees above hold at 750+ records/run.

### Why this workflow

Multi-source reconciliation is the highest-leverage finance-ops workflow to automate *safely*: the cost of a false match (money reconciled against the wrong transaction) is much higher than the cost of a delayed review, so the entire design optimizes for **precision and safety over raw automation rate**.

## How reconciliation works

1. **Candidate generation** (`app/candidates.py`): for each payment, find bank-settlement rows within the settlement window (configurable, default 0–7 days) and amount tolerance (15%), OR with a strong reference trace regardless of amount (so a mismatched-amount fraud/error case still surfaces instead of vanishing). A minimum name-similarity floor keeps two unrelated customers who coincidentally pay the same round amount from looking like false candidates for each other.
2. **Duplicate collapsing**: candidates sharing a UTR (a genuine bank double-post) are collapsed to one canonical candidate before scoring, so a duplicate bank row is flagged, not double-counted or confused with real ambiguity.
3. **Deterministic decision tree** (`app/scoring.py`): candidates are split into *plausible* (strong reference match, or a solid amount+name combination) and *noise* (coincidental amount match with no other supporting evidence). A single dominant plausible candidate with an exact reference and exact amount is auto-matched with zero AI involvement. Everything else either has a clear disqualifying reason (amount mismatch too large, reference matches but amount conflicts, no candidate at all) or is genuinely ambiguous.
4. **AI-assisted reasoning** (`app/ai_reasoning.py`): only genuinely ambiguous cases reach the LLM (~6% of this dataset). It receives the payment and the *full* plausible candidate set with precomputed features (amount diff, date diff, reference-match type, name similarity) -- never raw instructions to "guess". It must return structured JSON: a decision, a candidate id (or none), a confidence score, and reasoning.
5. **Policy guardrails** (`app/policy.py`) -- the core safety boundary: an AI "MATCH" is only honored if (a) the candidate id it names is actually in the evaluated set, (b) its self-reported confidence clears a threshold (75/100), and (c) the amount mismatch of the chosen candidate is under a hard cap (8%) that **no confidence score can override**. Fail any check -> explicit exception, never a downgraded guess.
6. **Exception management** (`app/exceptions.py`): every unresolved case records what was attempted, what evidence was found (including rejected candidates), what conflicted, why it couldn't be resolved, and a concrete suggested next action for a human reviewer.
7. **Audit trail** (SQLite `audit_log` table): every decision -- matched or excepted -- gets an immutable record of the source payment, the actor (rule engine / AI-assisted / policy guardrail / validator), the evidence, the confidence, and a timestamp.
8. **Invoice corroboration**: independent of the bank-match decision, each payment is checked against the invoice ledger (by order id + amount tolerance) as secondary evidence, reported per-decision as `found_consistent` / `found_mismatch` / `not_found`.

## How the AI is used (and constrained)

- **Scope**: only the ambiguous minority of records reach the LLM. Facts (amounts, dates, duplicate IDs, thresholds) are always deterministic.
- **Input**: the LLM sees precomputed, trustworthy features -- not raw data it has to re-derive -- and is explicitly instructed to decline (`NO_MATCH`) when evidence is contested or no candidate stands out.
- **Output contract**: strict JSON (`decision`, `candidate_id`, `confidence`, `reasoning`, `risk_flags`), parsed and validated before use.
- **Failure handling**: no API key, a timeout, a network error, or malformed JSON all produce the same outcome -- an explicit `AI_UNAVAILABLE` exception, routed to human review. The pipeline never falls back to guessing, and it never crashes.
- **No unilateral authority**: every AI "MATCH" is re-verified by `app/policy.py` against hard, non-negotiable caps. A confident-but-wrong AI proposal is downgraded to an `UNSUPPORTED_AI_DECISION` exception, not silently accepted.

## The synthetic dataset

`data/generate_dataset.py` produces three sources (`payments.csv`, `bank_settlements.csv`, `invoices.csv`) plus `ground_truth.csv`, all derived from a single seeded `random.Random` instance and a fixed anchor date -- fully reproducible (`--seed 42 --size 750` regenerates byte-identical output).

14 case types are deliberately generated, each with a documented reasoning about whether a correct answer is even determinable from the evidence (`is_safely_resolvable`):

| Case type | Share | Safely resolvable? |
|---|---|---|
| exact_match | 38% | yes |
| name_variation, abbreviation, formatting_diff | 20% | yes |
| settlement_delay (3–7 day lag) | 8% | yes |
| reference_variation (truncated/garbled ref) | 6% | yes |
| duplicate_bank_record (bank double-post) | 4% | yes |
| amount_mismatch_small (≤2%, fee-like) | 3% | yes |
| missing_invoice | 4% | yes (bank-match only) |
| missing_fields (blank field / corrupt amount) | 4% | mostly yes, corrupt-amount rows no |
| amount_mismatch_large (>15%, no explanation) | 3% | **no** |
| missing_bank_record (never settled) | 4% | **no** |
| ambiguous_multiple_candidates (2 indistinguishable) | 4% | **no** |
| conflicting_evidence (ref matches, amount wildly off) | 2% | **no** |

Ground truth is **never read by the reconciliation engine** -- only by `app/evaluation.py`, after the fact.

## Evaluation methodology

Every ground-truth payment is classified along two independent axes: whether the system automated it (`AUTO_MATCH`/`AI_ASSISTED_MATCH`) or excepted it, and whether a correct answer was actually determinable at all. This yields five outcomes:

- **CORRECT_AUTO** -- resolvable, matched, correct. The desired outcome.
- **INCORRECT_AUTO** -- resolvable, matched, but the *wrong* record. A false match on a solvable case.
- **UNSAFE_AUTO** -- *not* resolvable, but the system matched anyway. A safety violation, worse than INCORRECT_AUTO.
- **MISSED_OPPORTUNITY** -- resolvable, but excepted instead of automated. Safe, but a coverage loss.
- **CORRECTLY_ESCALATED** -- not resolvable, correctly excepted.

From these: **automation precision** = `CORRECT_AUTO / (CORRECT_AUTO + INCORRECT_AUTO + UNSAFE_AUTO)`; **coverage/recall** = `CORRECT_AUTO / resolvable_total`; **safety rate** = `CORRECTLY_ESCALATED / unresolvable_total`; **false-match rate** = `(INCORRECT_AUTO + UNSAFE_AUTO) / automated`. Throughput and per-record latency are wall-clock measured around the actual batch run, not estimated.

## Actual measured results

750 payments, 764 bank settlement rows, 757 invoices, seed 42, **AI disabled** (no `LLM_API_KEY` configured in this environment -- the honest, reproducible baseline):

| Metric | Value |
|---|---|
| Automation precision | **100.0%** (0 incorrect, 0 unsafe auto-matches out of 618) |
| Coverage / recall | **95.4%** of the 648 safely-resolvable cases automated |
| Safety rate | **100.0%** (all 102 genuinely unresolvable cases correctly excepted) |
| False-match rate | **0.0%** |
| Auto-reconciled (rule) | 618 / 750 (82.4%) |
| Exceptions | 132 / 750, all correctly explained with evidence + suggested action |
| Throughput | ~50–65 records/sec on a single core (~15–20 ms/record, incl. SQLite writes) |
| Total batch time | ~12–16 s for 750 records |

With AI reasoning enabled (verified via a scripted stand-in LLM, since no live API key is available in this build environment -- see Limitations), the 48 AI-eligible cases (reference variations, small amount mismatches, weak-name-but-exact-amount cases) are largely recovered: coverage rises to ~99% while precision and safety remain at 100%, because the policy layer's hard 8% amount-mismatch cap and confidence floor hold regardless of how confident the AI is. The categories that *must never* auto-resolve (`amount_mismatch_large`, `missing_bank_record`, `conflicting_evidence`, `ambiguous_multiple_candidates` -- 102 records) were correctly escalated in every run, with and without AI.

## Known limitations

- **No live LLM verified in this build environment.** `app/ai_reasoning.py` is fully implemented against the OpenAI-compatible chat completions API and unit-tested (`tests/test_policy.py`) against synthetic AI responses covering match/no-match/hallucinated-candidate/low-confidence/over-the-cap scenarios, but end-to-end behavior with a real model has not been observed here. Add a key to `.env` (`LLM_API_KEY`) to exercise it live -- any OpenAI-compatible endpoint works.
- **Invoice corroboration is a secondary signal**, keyed on exact `order_id`, not a second full reconciliation pass. It is reported per-decision but does not gate the primary bank-match status.
- **Single-process, single-machine.** No horizontal scaling; SQLite is sufficient at this volume (750–~20k records ingest/reconcile in seconds) but would need to move to a real database well before six-figure batch sizes.
- **Blocking thresholds are dataset-tuned.** `config.py` centralizes every threshold; they were tuned against this synthetic distribution and would need re-validation against a materially different real-world amount/name distribution.

## How to run

```bash
cd finance-controller
./run.sh
```

This creates a virtualenv on first run, installs dependencies, copies `.env.example` to `.env` if missing, starts the API on `:8000`, and the dashboard on `:8501`. Open **http://127.0.0.1:8501**.

To enable live AI reasoning, put a real key in `.env` before running:
```
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1     # or any OpenAI-compatible endpoint
LLM_MODEL=gpt-4o-mini
```
Without a key the system runs in fully-functional fallback mode -- ambiguous cases become explicit `AI_UNAVAILABLE` exceptions instead of being guessed.

**Manual / CLI alternative** (no dashboard):
```bash
source .venv/bin/activate
python cli.py generate --seed 42 --size 750   # synthetic dataset -> data/raw/
python cli.py run                              # ingest + reconcile, prints metrics JSON
python cli.py evaluate                         # score the latest run against ground truth
```

**API directly** (with `run.sh` or `uvicorn app.api:app --port 8000` already running):
```bash
curl -X POST 'localhost:8000/dataset/generate?seed=42&size=750'
curl -X POST 'localhost:8000/reconcile/run'
curl 'localhost:8000/runs/latest'
curl 'localhost:8000/runs/<run_id>/evaluation'
curl 'localhost:8000/exceptions?run_id=<run_id>'
curl 'localhost:8000/decisions/<payment_id>?run_id=<run_id>'
```

**Tests**: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/ -v` (the env-var works around unrelated third-party pytest plugins that may be globally installed on the host; it is not required in a clean environment).

## Recommended demo flow (~4 minutes)

1. **Show the dashboard Overview** with the pre-generated 750-record run already loaded: reconciliation rate, precision, safety rate, throughput -- judges see real numbers in the first 10 seconds.
2. **Generate a fresh dataset live** (sidebar -> Generate dataset -> Run reconciliation) to prove it isn't canned -- ~13 seconds for 750 records.
3. **Decisions tab**: open an `exact_match` payment -> show the evidence (exact reference, exact amount, zero ambiguity, no AI needed).
4. **Exceptions tab**: open a `conflicting_evidence` case -- reference matches exactly, amount differs by ~28% -- show the system's explanation of *why* it refused, and the suggested human action. This is the core "safe refusal" moment.
5. **If an LLM key is configured**: run again and show an `AI_ASSISTED_MATCH` decision with the AI's reasoning text and confidence in the evidence panel; then show `evaluation.per_case_type.amount_mismatch_small` improve from `MISSED_OPPORTUNITY` to `CORRECT_AUTO`.
6. **Evaluation tab**: the outcome bar chart -- zero red (`INCORRECT_AUTO`/`UNSAFE_AUTO`) bars is the headline: automation happened everywhere it safely could, and nowhere it couldn't.
7. **Audit Trail tab**: filter by the payment_id shown earlier -- one row, fully explaining the decision, actor, and evidence.

## Remaining high-value improvements (not done, out of 3-day scope)

- Verify live-LLM behavior end-to-end and tune the confidence threshold against real model calibration (it may be over/under-confident relative to the synthetic mock used here).
- A second reconciliation pass keyed on invoice-side discrepancies (currently one-directional: payment -> invoice, not invoice -> payment for orphaned invoices).
- A "resolve exception" workflow in the dashboard (mark reviewed / record human decision) -- currently read-only, which was an explicit scope cut to protect the core pipeline.
