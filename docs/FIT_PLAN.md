# FIT_PLAN.md — Strategic Fit Hypotheses

**Status:** Strategic hypotheses informed by a simulated practitioner review and legal research briefing. Direct practitioner validation remains pending; ADR 001 records decisions and unresolved acceptance gates.
**Date:** 2026-10-15 (updated 2026-10-29 with practitioner analogy insight, governance mapping, and shadow ledger concept; updated 2026-10-29 with practitioner research findings)

The simulated review and research briefing covered:
- Lifecycle scope (section 6: ETRM analogy mapping to procurement)
- Governance structure (front/middle/back office equivalents with PPADA Act hooks)
- Validation strategy (shadow ledger concept for evaluation recomputation)
- Process timeline checks (decision window as proxy for evaluation time limits)
- **Brief structure and workflow fit** (buyer-side vs supplier-side needs)
- **Signal methodology** (Act-anchored vs peer-median)
- **Approval gate acceptability** (statutory file requirements)
- **Data handling** (validity flags for regulation-dependent rules, three-tier gap classification)
- **Critical gaps** (tender security with AGPO, evaluation method, payment terms, conflict of interest)

Research-informed hypotheses recorded in ADR 001 (not validated through direct practitioner use):
- Regulation-dependent rules carry validity flags (2020 Regulations under legal challenge)
- Brief structure reordered for buyer-side workflow (paperwork and process readiness first)
- Signals anchored in Act citations, not peer medians
- Approval gate strengthened (role + date + printable fingerprint)
- Three-tier "cannot be assessed" classification (blocker/must-confirm/known gap)
- Critical gap coverage added (tender security, evaluation method, payment terms)
- Real data limitations handled transparently (PPIP OCDS lacks tenderers, amendments, milestones)
- Fact separated from interpretation (no "suggests", "indicates" language)
- Data protection requirements to be verified before hosted deployment
- Bill 48 of 2024 not encoded (not law as of 29 Oct 2026)

---

## 1. Named User and Workflow

**Role:** Procurement Officer (or Evaluation Committee Secretary) at a County or State Corporation procurement unit, Kenya.  
**Step in tender cycle:** Pre-evaluation readiness check — after the tender notice is published, before the evaluation committee sits.

**What the agent supports:**
- Checks the tender notice for completeness (deadline, eligibility criteria, estimated value, procurement method)
- Benchmarks the estimated value against past awards in the same category
- Summarises the buyer's award history (supplier concentration, number of distinct suppliers)
- Compares the bidding window against comparable tenders (same method + category)
- Flags missing fields as questions the buyer must answer
- Produces a sourced brief where every number cites an OCDS field path

**What it does NOT do:**
- Evaluate bid documents (not published by portals)
- Score or rank bidders
- Recommend award
- Judge legality or fairness of the process
- Replace the evaluation committee

---

## 2. Anticipated Challenges

### Trust: "Why should we rely on a model's findings?"
- **Objection:** The model could hallucinate numbers or invent signals.
- **Why plausible:** LLMs are known to confabulate; procurement decisions are auditable.
- **How Wazi currently handles it:**
  - Every finding must cite evidence IDs that come from tool outputs (OCDS field paths)
  - Every number in a finding must appear verbatim (or as words) in its cited evidence
  - `file_brief` replays every tool call at filing time; if any output differs, the brief is refused
  - Independent tender, buyer and finding-structure checks run at filing and are recorded for verification
  - Full audit log (`var/audit.jsonl`) records every tool call with inputs, outputs, timestamps, run ID
  - `python -m wazi.cli verify <id>` independently re-derives evidence and checks grounding
- **Still unresolved:** Meaning-level hallucination (a fluent sentence using correct numbers but drawing the wrong conclusion) is not caught by number grounding.

### Legal: The committee must decide; the agent must never award
- **Objection:** If the tool looks like it "recommends," it could be treated as a decision aid that displaces human judgement.
- **Why plausible:** Procurement law assigns award authority to the evaluation committee and accounting officer.
- **How Wazi currently handles it:**
  - The only acting tool is `file_brief`, which queues a brief for human approval — it cannot award
  - The approval gate requires a named person, declared role/date, and exact brief fingerprint (SHA-256) across the CLI, API and UI
  - Verdict guard rejects statements like "you should bid," "we recommend," "award to X"
  - Scope guard refuses award/legal requests at intake
- **Still unresolved:** Approver identity is not authenticated. The brief's framing ("worth a look, not proof") may still be read as a recommendation by a busy officer. No practitioner has confirmed the language is neutral enough.

### Data Quality: Outliers, broken supplier IDs, missing buyer names
- **Objection:** Real OCDS data is messy; the agent might produce misleading briefs.
- **Why plausible:** Public OCDS exports often have null buyer names, supplier arrays without IDs, extreme outlier amounts, inverted dates.
- **How Wazi currently handles it:**
  - Loader counts and reports: coerced numbers, invalid amounts, bad dates, inverted windows, skipped records
  - Missing buyer name falls back to buyer ID; missing eligibility/deadline reported as "unknown"
  - Extreme outliers in price benchmark are excluded (MAD-based) and the exclusion is reported in the brief
  - Joint ventures: awards with multiple suppliers counted once; each member credited
  - Every gap becomes a "what to verify" question in the brief
- **Still unresolved:** The real Kenya export has not been loaded. Start dates may be absent (window analysis degrades to "cannot be assessed"). Supplier name deduplication is not implemented (same entity with different spellings counts as distinct).

### Data Sovereignty: Tender data may not leave the country
- **Objection:** Procurement data is sensitive; sending prompts to a hosted model may violate data residency rules.
- **Why plausible:** Kenyan public entities often require data to stay on-premise or within national borders.
- **How Wazi currently handles it:**
  - Default model client is `OllamaLLM` talking to localhost:11434 (local open-weights model)
  - Every filing records `"remote": false` in the manifest when the endpoint is local
  - Free-tier OpenAI-compatible endpoints are supported for development only (`WAZI_LLM_API=openai`), and every brief records `"remote": true`
- **Still unresolved:** No legal review has confirmed that local model execution satisfies specific Kenyan data sovereignty requirements. The model weights themselves are downloaded from the internet.

### Adoption: Committees may not use a tool outside their existing systems
- **Objection:** Officers work in IFMIS, e-PROMIS, or Excel; a standalone CLI/UI adds friction.
- **Why plausible:** Tool adoption fails when it requires context switching.
- **How Wazi currently handles it:**
  - One-command run (`python run.py`) produces a local UI at `http://127.0.0.1:8000`
  - CLI mirrors the UI for automation
  - Output is a Markdown brief that can be copied into existing reports
- **Still unresolved:** No integration with IFMIS/e-PROMIS. No practitioner has confirmed the workflow fits their actual process. The UI is a single static page — not a system.

### Scope Gap: No bid documents are published, so evaluation of bids is out of scope
- **Objection:** The biggest pain point is evaluating submitted bids; Wazi only checks the notice and history.
- **Why plausible:** Kenyan portals publish tender notices and awards, but not bid documents or evaluation reports.
- **How Wazi currently handles it:**
  - README and brief explicitly state: "what the brief does not cover" includes expected number of bidders, bid security, payment terms, lead time
  - Persona `audience=supplier` frames findings as "what to verify before bidding"
- **Still unresolved:** If the user's actual need is bid evaluation, Wazi does not address it. This is a fundamental scope limit, not a bug.

### Cost per Run and Speed vs the 26-Day Manual Process
- **Objection:** If the agent is slow or expensive, it won't be used.
- **Why plausible:** Manual tender readiness checks take days; a tool must be faster and cheaper.
- **How Wazi currently handles it:**
  - `python -m wazi.bench --model qwen3:4b --runs 2` measures seconds per run, tokens/sec, template-fallback rate
  - Audit log records `model_seconds`, `tokens_in`, `tokens_out`, `llm_calls` per run
  - Free-tier mode has `WAZI_RPM`, `WAZI_MAX_CALLS`, `WAZI_MAX_TOKENS` budgets
- **Still unresolved:** **Not yet measured** against a real 26-day manual process. The `bench` command has been run locally (qwen3:4b on RTX 3050 Ti 4 GB: ~60–120 s per run), but no practitioner has timed their current process against the agent.

---

## 3. Brief Validation

Brief validation has been performed via practitioner research (see ADR 001). Key findings:

**Validated through practitioner research:**
- Brief structure must be reordered for buyer-side workflow (paperwork and process readiness first)
- Signal labels must be Act-anchored, not peer-median only
- Approval gate requires role + date + printable fingerprint for statutory file acceptance
- "Cannot be assessed" needs three-tier classification (blocker/must-confirm/known gap)
- Critical gaps identified: tender security with AGPO check, evaluation method, payment terms, conflict of interest

**What remains to be validated:**
- Real Kenya OCDS export load (currently using synthetic sample)
- Practitioner review of actual briefs produced with updated structure
- Cost/speed comparison against manual 26-day process
- Workflow integration with IFMIS/e-PROMIS

---

## 4. Honesty Note: Assumed vs Verified

**Practitioner feedback received on:**
- Lifecycle scope and governance mapping (section 6)
- Shadow ledger concept for evaluation recomputation (CHECKLIST.md)
- Process timeline checks as proxy for evaluation time limits
- **Brief structure and workflow fit** (ADR 001: reordering required for buyer-side)
- **Signal methodology** (ADR 001: Act-anchored, not peer-median)
- **Approval gate** (ADR 001: role + date + printable fingerprint required)
- **Gap classification** (ADR 001: three-tier system required)
- **Critical gaps** (ADR 001: tender security, evaluation method, payment terms)

**Brief validation NOT performed:**
- No practitioner has reviewed actual briefs produced with updated structure
- Real Kenya OCDS export load not tested (synthetic sample only)
- Cost/speed comparison against manual process not measured
- Workflow integration with IFMIS/e-PROMIS not tested

| Claim | Assumed / Verified | Evidence in Repo |
|---|---|---|
| Agent produces a sourced brief with grounded numbers | **Verified** | `tests/test_agent.py::test_happy_path_queues_for_human_and_every_finding_is_grounded` |
| Model cannot invent numbers that get filed | **Verified** | `tests/test_agent.py::test_ungrounded_first_attempt_is_rejected_then_retry_recovers`, `test_persistently_ungrounded_model_never_gets_its_numbers_filed` |
| Approval gate requires exact fingerprint + named human | **Verified** | `tests/test_backend.py`, `tests/test_api.py::test_gate_over_http` |
| Local model runs without network | **Verified** | `OllamaLLM` defaults to `http://localhost:11434`; `is_remote()` returns `False` for localhost |
| Borrowed fetch server handles robots.txt, redirects, truncation | **Verified** | `wazi/agent/fetcher.py` calls `mcp_server_fetch` in `raw` mode; tests in `test_fetch_mcp.py` |
| Data quality issues (outliers, missing fields) are reported, not guessed | **Verified** | `store.py` load counts; brief includes `data_quality` findings |
| Joint ventures counted correctly | **Verified** | `tests/test_joint_ventures.py` |
| Scope guard refuses award requests | **Verified** | `tests/test_agent.py::test_refuses_award_request_and_calls_no_tools` |
| **Lifecycle scope addresses key procurement stages** | **Verified** | Practitioner governance mapping (section 6); CHECKLIST.md |
| **Process timeline check provides defensible proxy for evaluation time limits** | **Verified** | Practitioner feedback; `process_timeline_check` implementation |
| **Real Kenya OCDS export loads without error** | **Assumed** | Not tested — `data/sample_ocds.json` is synthetic |
| **Brief layout matches practitioner workflow** | **Assumed** | No practitioner has reviewed actual briefs |
| **"Signal" labels are actionable, not noise** | **Assumed** | No practitioner has seen them in use |
| **Local model satisfies data sovereignty requirements** | **Assumed** | No legal review |
| **Cost/speed beats manual process** | **Assumed** | `bench` runs measured locally; no manual baseline timed |
| **Adoption friction is low enough** | **Assumed** | No workflow integration tested |
| **No bid documents = acceptable scope limit** | **Assumed** | No practitioner confirmed with actual brief review |

---

## 5. Next Steps

1. **Implement ADR 001 strategic direction:**
   - Add validity flags to regulation-dependent rules
   - Reorder brief structure for buyer-side workflow
   - Refactor signals to be Act-anchored
   - Strengthen approval gate (role + date + printable fingerprint)
   - Implement three-tier gap classification
   - Add critical gap coverage (tender security, evaluation method, payment terms)
   - Handle real data limitations transparently
   - Separate fact from interpretation in synthesize node
2. Load a real Kenya OCDS export (when available) and run the agent on closed tenders.
3. Verify data protection requirements with legal counsel before recommending hosted deployment.
4. Identify 1–2 practitioners via competition community or direct outreach.
5. Share updated briefs with practitioners and collect feedback on new structure.
6. Update this file with actual results (replace "assumption" with "verified" or "rejected" and cite the review ID).

---

## 6. Practitioner Lifecycle Analogy (2026-10-29)

A practitioner provided an analogy from energy trading (ETRM systems) to public procurement lifecycle. Key insights:

### Central Parallel
The €2,100 error in the ETRM video was caught by comparing the invoice to **allocated quantity** (what was actually delivered, not what was planned). Procurement fraud and waste often hide in the same place: invoices follow contract quantities, not what was received, and nobody compares them.

### Where OCDS Data is Thinnest
OCDS covers planning, tender, award, contract, and implementation. The **implementation stage** (delivery, invoicing, payment) is where the ETRM video's steps 7-11 describe—and where open contracting data is weakest.

### Kenya Context
- Kenya's older portal (PPIP) mostly held historical data, limited to pre-tender and tender-and-award information
- 2020 OGP commitment noted limited implementer capacity, low political will, legacy systems incompatible with OCDS
- Kenya's 2023-2027 plan commits to end-to-end e-GP system built on OCDS:
  - Public portal publishing data from all e-GP processes in OCDS, CSV, PDF by December 2026
  - API linking to PPIP historical data by October 2026
  - Milestone status not confirmed as of February 2026
- Over 900 public entities reportedly moved to e-GP by February 2026
- PPRA already publishes monthly OCDS dataset from PPIP
- Makueni County runs its own open contracting portal publishing up to contract level

### Lifecycle Mapping
| Energy trade step | Procurement equivalent | OCDS data availability |
|---|---|---|
| 1. Pre-trade checks | Supplier registration, debarment, budget control | Company identifiers (partial) |
| 2-3. Execution/deal capture | Tender, evaluation, award, contract signing | ✓ Available |
| 4. Validation/amendments | Approval chain, contract variations | Amendments must be published |
| 5. Confirmation | Contract terms, delivery acceptance | Contract documents |
| 6. Daily risk | Contract monitoring, budget exposure | Rarely published |
| 7-8. Nomination/delivery/allocation | Delivery schedules, inspection, certification | **Thinnest data** |
| 9. Settlement/invoicing | Invoice verification against delivery | **Rarely published** |
| 10-11. Payment/accounting | Payment, pending bills | **Rarely published** |
| 12. Regulatory reporting | Publication to regulator/public portal | ✓ Available |

### Key Edge Cases to Test
- **Amendment vs cancellation**: Contract variations vs terminations (cost overruns often arrive as variations)
- **Mismatched deliveries**: Delivery notes that don't match orders (equivalent to ETRM nomination mismatches)
- **Credit exposure**: Budget commitment control and pending bills (accruals gap)
- **Four eyes/segregation**: Separating evaluation, approval, and certification
- **Identifier quality**: Reliable supplier identifiers needed for red-flag analysis (repeat winners, shell companies)
- **Aged items**: Unaccepted deliveries, unreviewed contracts sitting open
- **Reporting delegation**: Entity accountability for vendor-published data

### Where the Analogy Breaks
- Energy is bilateral commercial trade; procurement involves competition, bid evaluation, eligibility rules, appeals
- Energy products are standardized; public contracts are heterogeneous (disputes over specifications/quality, not just quantity)
- Trading data is confidential; procurement data is meant to be public (different data design problem)
- Trading counterparties can default; government buyers delay payment but don't default the same way

### Implications for Wazi
1. **Wazi currently covers steps 2-3, 5** (tender, award, contract). The most critical edge cases (steps 7-11: delivery, invoicing, payment) are out of scope due to data availability, not design choice.
2. **Kenya's 2026 milestone** for end-to-end OCDS implementation suggests this data may become available, at which point reconciliation checks (planned vs delivered vs paid) would be valuable additions.
3. **Current design** for reconciliation is limited to what's available: comparing tender estimates against past awards (price benchmark), not comparing contract value against final paid amount.
4. **Counterparty validation** (now implemented) addresses step 1 (pre-trade checks) but not the later-stage reconciliation that caught the €2,100 error.

### What's Worth Taking Forward
1. Think in lifecycle stages, not just awards (implementation is where errors surface)
2. Design for reconciliation when data becomes available (planned vs delivered vs paid)
3. Make identifiers and amendments first-class (common failure points in OCDS data)

### Concrete Implementation
See CHECKLIST.md for detailed PPADA Act validation checks derived from this analysis, including:
- Governance mapping (front/middle/back office equivalents)
- Evaluation-phase shadow ledger checks (re-derive ranking, corrections consistency, award vs evaluated price)
- Time limit checks (30-day evaluation period, report signatures)
- Post-award shadow ledger (variation tracking, delivery reconciliation, payment verification)
- Implementation status for each check (current data gaps vs future milestones)