# FIT_PLAN.md — Anticipated Fit Plan (NOT validated research)

**Status:** Hypotheses drawn from public sources and the open-contracting data schema. **Not yet validated with a practitioner.**  
**Date:** 2026-10-15

No practitioner has been interviewed. This document records assumptions to be tested. Nothing here is evidence of fit.

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
  - Full audit log (`var/audit.jsonl`) records every tool call with inputs, outputs, timestamps, run ID
  - `python -m bidbox.cli verify <id>` independently re-derives evidence and checks grounding
- **Still unresolved:** Meaning-level hallucination (a fluent sentence using correct numbers but drawing the wrong conclusion) is not caught by number grounding.

### Legal: The committee must decide; the agent must never award
- **Objection:** If the tool looks like it "recommends," it could be treated as a decision aid that displaces human judgement.
- **Why plausible:** Procurement law assigns award authority to the evaluation committee and accounting officer.
- **How Wazi currently handles it:**
  - The only acting tool is `file_brief`, which queues a brief for human approval — it cannot award
  - Approval gate requires a named human approver and the exact brief fingerprint (SHA-256)
  - Verdict guard rejects statements like "you should bid," "we recommend," "award to X"
  - Scope guard refuses award/legal requests at intake
- **Still unresolved:** The brief's framing ("worth a look, not proof") may still be read as a recommendation by a busy officer. No practitioner has confirmed the language is neutral enough.

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
  - Free-tier OpenAI-compatible endpoints are supported for development only (`BIDBOX_LLM_API=openai`), and every brief records `"remote": true`
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
  - `python -m bidbox.bench --model qwen3:4b --runs 2` measures seconds per run, tokens/sec, template-fallback rate
  - Audit log records `model_seconds`, `tokens_in`, `tokens_out`, `llm_calls` per run
  - Free-tier mode has `BIDBOX_RPM`, `BIDBOX_MAX_CALLS`, `BIDBOX_MAX_TOKENS` budgets
- **Still unresolved:** **Not yet measured** against a real 26-day manual process. The `bench` command has been run locally (qwen3:4b on RTX 3050 Ti 4 GB: ~60–120 s per run), but no practitioner has timed their current process against the agent.

---

## 3. Validation Plan

| # | Question | Who to Ask (Role) | Result That Would Change Design | Target Date | Status |
|---|---|---|---|---|---|
| 1 | Does the brief's structure (deadline → eligibility → price position → buyer history → window → gaps → what to verify) match the order you actually check? | Procurement Officer / Evaluation Committee Secretary at a County or State Corporation | If the order is wrong, reorder the evidence pipeline and brief layout | 2026-11-15 | NOT YET DONE |
| 2 | Are the "signal" labels (supplier concentration, short bidding window) useful, or do they create false alarm fatigue? | Same as above | If signals are ignored or distrusted, remove or reweight them; add a confidence score | 2026-11-15 | NOT YET DONE |
| 3 | Is the approval gate (named human + fingerprint) acceptable, or does your process require a digital signature / workflow integration? | Head of Procurement / Legal Officer | If digital signature or workflow integration is required, add API for e-signature or integrate with existing approval system | 2026-11-30 | NOT YET DONE |
| 4 | When the brief says "cannot be assessed" (missing start date, thin history), do you treat that as a blocker or a known gap? | Procurement Officer | If "cannot be assessed" is a blocker, the agent must refuse to file; if a gap, current behaviour is correct | 2026-11-15 | NOT YET DONE |
| 5 | Would you use a local open-weights model (Ollama) on-premise, or is a hosted endpoint required/forbidden? | ICT / Data Protection Officer | If hosted is forbidden, local-only is confirmed; if local is forbidden, we need a certified on-premise model server | 2026-11-30 | NOT YET DONE |

---

## 4. Honesty Note: Assumed vs Verified

| Claim | Assumed / Verified | Evidence in Repo |
|---|---|---|
| Agent produces a sourced brief with grounded numbers | **Verified** | `tests/test_agent.py::test_happy_path_queues_for_human_and_every_finding_is_grounded` |
| Model cannot invent numbers that get filed | **Verified** | `tests/test_agent.py::test_ungrounded_first_attempt_is_rejected_then_retry_recovers`, `test_persistently_ungrounded_model_never_gets_its_numbers_filed` |
| Approval gate requires exact fingerprint + named human | **Verified** | `tests/test_backend.py`, `tests/test_api.py::test_gate_over_http` |
| Local model runs without network | **Verified** | `OllamaLLM` defaults to `http://localhost:11434`; `is_remote()` returns `False` for localhost |
| Borrowed fetch server handles robots.txt, redirects, truncation | **Verified** | `bidbox/agent/fetcher.py` calls `mcp_server_fetch` in `raw` mode; tests in `test_fetch_mcp.py` |
| Data quality issues (outliers, missing fields) are reported, not guessed | **Verified** | `store.py` load counts; brief includes `data_quality` findings |
| Joint ventures counted correctly | **Verified** | `tests/test_joint_ventures.py` |
| Scope guard refuses award requests | **Verified** | `tests/test_agent.py::test_refuses_award_request_and_calls_no_tools` |
| **Real Kenya OCDS export loads without error** | **Assumed** | Not tested — `data/sample_ocds.json` is synthetic |
| **Brief layout matches practitioner workflow** | **Assumed** | No practitioner has reviewed it |
| **"Signal" labels are actionable, not noise** | **Assumed** | No practitioner has seen them |
| **Local model satisfies data sovereignty requirements** | **Assumed** | No legal review |
| **Cost/speed beats manual process** | **Assumed** | `bench` runs measured locally; no manual baseline timed |
| **Adoption friction is low enough** | **Assumed** | No workflow integration tested |
| **No bid documents = acceptable scope limit** | **Assumed** | No practitioner confirmed |

---

## 5. Next Steps

1. Load a real Kenya OCDS export (when available) and run the agent on closed tenders.
2. Identify 1–2 practitioners via competition community or direct outreach.
3. Run the validation plan questions above. Record responses using `python -m bidbox.review packet` → `record` → `fit -o FIT.md`.
4. Update this file with actual results (replace "assumption" with "verified" or "rejected" and cite the review ID).