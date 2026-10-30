# ADR 002: Gap Resolutions Based on Evaluation Findings

**Status:** Accepted
**Date:** 2026-10-30
**Context:** Evaluation of the Wazi agent against four pillars (Relevance, Perception/Memory, Reasoning/Planning, Execution/Tools) identified eight gaps requiring resolution.

## Context

The evaluation revealed the following gaps:

1. **Three-tier gap classification** not implemented despite ADR 001 requirement
2. **Signal testing failures** in peer comparison logic (4/19 loop mode test cases)
3. **Keyword-based scope guard** misses paraphrased award requests
4. **No real-data testing framework** for Kenya PPIP OCDS
5. **No practitioner feedback mechanism** in briefs
6. **Fixed document text limit** (2000 chars) not configurable
7. **No incremental data loading** support
8. **No audit log anomaly detection**

This ADR documents the resolution approach for each gap, with alternatives considered and rejected.

---

## Decision 1: Three-Tier Gap Classification

### Problem
ADR 001 requires three-tier gap classification (blocker/must-confirm/known gap) with confidence banners in briefs. The implementation was incomplete for gaps that can be detected from available OCDS fields; some gaps (tender security, payment terms) require additional evidence sources and are deferred to ADR 001 Phase 3.

### Resolution
**Status:** PARTIAL

Implemented three-tier classification in `_render()` in `tools.py` for data_quality findings that can be detected from the available OCDS fields:

- **Blocker**: Missing deadline, eligibility, required documents, buyer identity
- **Must-confirm**: Missing start date
- **Known gap**: Thin history (<5 awards), low-confidence benchmark (<3 comparable tenders)

**Not yet implemented:**
- Tender security (requires AGPO exemption check, per ADR 001 Phase 3)
- Payment terms (document extraction not yet implemented, per ADR 001 Phase 3)

These are documented as "not established" in README.md and deferred pending evidence availability and legal basis verification (ADR 001 Section "Remaining Work / Acceptance Gate").

Confidence banners:
- >1 blocker: "LOW CONFIDENCE - CRITICAL DATA MISSING"
- >2 must-confirm: "MEDIUM CONFIDENCE - REQUIRES VERIFICATION"
- >5 known gaps: "MEDIUM CONFIDENCE - LIMITED HISTORY"

### Alternatives Considered and Rejected

**Alternative 1: Numeric confidence score (0-100)**
- **Rejected:** Arbitrary and hard to interpret. Practitioners struggle to act on "confidence: 67". Qualitative tiers (blocker/must-confirm/known gap) are clearer and actionable.

**Alternative 2: Refuse to file briefs with any gaps**
- **Rejected:** Would make the tool unusable on real data. Practitioners need to know what's missing, not be blocked from getting partial information. The brief should surface gaps, not hide them.

**Alternative 3: Binary classification (gap vs. no gap)**
- **Rejected:** Doesn't help practitioners prioritize. All gaps are not equal; missing deadline is critical, thin history is acceptable. Tiers enable prioritization.

**Alternative 4: Make gap thresholds configurable per organization**
- **Rejected:** Adds complexity without clear benefit. The thresholds (1 blocker, 2 must-confirm, 5 known gaps) are based on ADR 001 practitioner research and should be consistent across users.

---

## Decision 2: Signal Testing Failures in Peer Comparison

### Problem
Evaluation shows loop mode fails to properly test signals against peers in 4/19 test cases:
- `signal_downgraded_when_typical`: signal not tested against peers
- `signal_kept_when_above_peers`: signal lost or untested
- `window_signal_survives_typical_concentration`: signals interfered

### Resolution
**Status:** COMPLETED

Fixed signal interference in `_add_peers()` in `evidence.py`:

- Track buyer_history signal indices BEFORE peer comparison to avoid interference
- When peer comparison shows "typical_for_peers", downgrade ALL buyer_history signals deterministically
- Ensure signal state is captured at a single point in time, not during iteration

### Alternatives Considered and Rejected

**Alternative 1: Make the model responsible for signal downgrade**
- **Rejected:** The model should not decide whether a signal stands or falls. Code enforces this rule deterministically. Model responsibility is writing synthesis, not signal validation.

**Alternative 2: Remove peer comparison entirely**
- **Rejected:** Peer comparison provides valuable context when legal minima are not available. Removing it would lose useful signal capability. The fix is to make it work correctly, not remove it.

**Alternative 3: Add confidence intervals to peer comparison**
- **Rejected:** Adds complexity without addressing the root issue (signal interference). The problem is deterministic state management, not statistical uncertainty.

**Alternative 4: Separate signal tracking from evidence list**
- **Rejected:** Evidence list is the single source of truth. Duplicating signal state creates sync risks. The fix is to capture indices before modification, not create parallel state.

---

## Decision 3: Keyword-Based Scope Guard

### Problem
Scope guard is keyword-based and misses paraphrased award requests (e.g., "paraphrased_award_request" test case fails). ADR 001 calls for an intent classifier.

### Resolution
**Status:** PARTIAL (Improved keywords, deferred intent classifier)

Improved keyword-based guard with:
- Expanded patterns (e.g., "tell me who to award", "decide the winner")
- Explicit documentation that this is a known limitation
- Clear refusal message when guard triggers

Intent classifier deferred until:
- Real-data practitioner review confirms the need
- Cost/quotas for model calls are acceptable in production
- Evaluation shows paraphrased requests are a real problem, not just a test case

### Alternatives Considered and Rejected

**Alternative 1: Model-based intent classifier (call LLM on every request)**
- **Rejected:**
  - Cost: Every request would consume quota, making the tool expensive at scale
  - Latency: Adds LLM call latency to every intake
  - Reliability: Model hallucination risk (false positives blocking legitimate requests, false negatives allowing out-of-scope requests)
  - Testability: Model behavior changes with updates, breaking reproducibility
  - Defense: Undermines code-based safety principle

**Alternative 2: Expanded exhaustive keyword list**
- **Rejected:** Paraphrases are infinite. No finite keyword list can catch all variations. This would be a never-ending maintenance burden with diminishing returns.

**Alternative 3: Hybrid (keywords + model fallback for ambiguous cases)**
- **Rejected:** Creates inconsistent behavior (sometimes uses rules, sometimes uses model). Makes testing harder and behavior unpredictable. Users cannot rely on deterministic safety.

**Alternative 4: No scope guard at all (trust the model to refuse)**
- **Rejected:** The model cannot be trusted with safety-critical decisions. The system must enforce scope in code, not prompts. Code enforcement is auditable and reproducible; model behavior is not.

**CHOSEN APPROACH:** Improved keywords with explicit documentation of limitation. This keeps the guard deterministic, testable, and zero-cost. When it fails, it refuses explicitly with a clear message. Intent classifier will be added if practitioner review shows paraphrased requests are a real problem in production use.

---

## Decision 4: Real-Data Testing Framework

### Problem
Current evaluation uses synthetic data; the real Kenya PPIP OCDS feed is untested at scale. No framework exists for testing with real data.

### Resolution
**Status:** DEFERRED

Real-data testing framework deferred until:
- Kenya PPIP OCDS real feed is accessible for testing
- Practitioner review confirms tool direction
- Synthetic evaluation shows acceptable performance

### Alternatives Considered and Rejected

**Alternative 1: Scrape Kenya PPIP portal for real data**
- **Rejected:** 
  - Legal: Terms of service may prohibit scraping
  - Technical: Portal may have rate limits, CAPTCHAs, or authentication
  - Data quality: Scraped data may not match the official OCDS export format
  - Maintenance: Scraping is brittle; portal changes break the pipeline

**Alternative 2: Mock real data based on portal inspection**
- **Rejected:** Mock data is not real data. Mocks cannot reveal real-world data quality issues (inverted windows, missing fields, malformed values). The evaluation would be meaningless.

**Alternative 3: Partner with Kenya PPRA for test data access**
- **Rejected:** Requires formal partnership agreement, which is outside current scope. This is a strategic relationship decision, not a technical implementation choice.

**Alternative 4: Wait for December 2026 end-to-end OCDS milestone**
- **Rejected:** Too long to wait. The tool should be tested with whatever real data is available now. Synthetic data is acceptable for initial development; real-data testing should begin as soon as any real data is accessible.

**CHOSEN APPROACH:** Defer real-data framework until (1) real data is accessible, (2) practitioner review confirms tool direction. The current synthetic evaluation is sufficient for development-phase quality assurance. Real-data testing will be added when it becomes feasible.

---

## Decision 5: Practitioner Feedback Mechanism

### Problem
No mechanism exists for practitioners to provide feedback on briefs in production. The `review.py` module exists for structured reviews but is not integrated into briefs.

### Resolution
**Status:** DEFERRED

Practitioner feedback mechanism in briefs deferred until:
- Real-data trials are running
- Practitioners are actively using the tool
- Feedback volume justifies automation

### Alternatives Considered and Rejected

**Alternative 1: Add feedback form to every brief (HTML/inline form)**
- **Rejected:** 
  - Briefs are Markdown; adding HTML forms breaks format
  - Feedback on synthetic data is meaningless (practitioners need real tender context)
  - No feedback loop exists yet (who processes feedback? how is it acted on?)

**Alternative 2: Link to external feedback survey (e.g., Google Forms)**
- **Rejected:** 
  - External dependency breaks offline/local-first deployment
  - Survey responses are not bound to specific brief fingerprints
  - No control over survey format or data retention

**Alternative 3: Add "report issue" button in UI that sends to maintainer**
- **Rejected:** 
  - Creates maintainer burden without scaling
  - Issues should be tracked in the review system, not ad-hoc email
  - No structured format for actionable feedback

**Alternative 4: Auto-generate review packets for every filed brief**
- **Rejected:** 
  - Generates clutter (most briefs are not reviewed)
  - Review packets should be generated on-demand for specific briefs
  - The existing `python -m wazi.review packet` command is sufficient

**CHOSEN APPROACH:** Defer until real-data trials. The existing `review.py` module provides structured practitioner review capabilities. It should be used when practitioners are actively reviewing real briefs. Adding feedback UI before real use creates false signal and maintenance burden.

---

## Decision 6: Configurable Document Text Limit

### Problem
Document text is capped at 2000 characters (hardcoded in `evidence.py`). This limit is not configurable and may be too short or too long for different use cases.

### Resolution
**Status:** IMPLEMENTED

Made document text limit configurable via environment variable `WAZI_MAX_DOC_CHARS` (default 2000).

### Alternatives Considered and Rejected

**Alternative 1: Remove limit entirely**
- **Rejected:** 
  - Security: Untrusted document text could contain injection attempts
  - Performance: Large documents (MBs) would slow evidence building
  - Storage: Manifests would become bloated with document text
  - Privacy: Storing full document text may raise data protection concerns

**Alternative 2: Dynamic limit based on document type**
- **Rejected:** 
  - Adds complexity without clear benefit
  - No reliable way to detect document type from URL alone
  - Paradox: If we're reading the document to detect its type, we've already committed to reading it

**Alternative 3: Per-filing configuration (in brief metadata)**
- **Rejected:** 
  - Defeats reproducibility: same document, different limit = different evidence
  - Adds UI complexity for practitioners (who don't care about this limit)
  - The limit is a system safety parameter, not a user choice

**Alternative 4: Token-based limit instead of character limit**
- **Rejected:** 
  - Tokenization is model-specific; different models tokenize differently
  - Adds dependency on tokenizer libraries
  - Character limit is simpler and model-agnostic

**CHOSEN APPROACH:** Environment variable configuration. This allows operators to tune the limit for their deployment without changing code. Default remains 2000 for safety. Operators who understand the risks can increase it if needed.

---

## Decision 7: Incremental Data Loading

### Problem
Data loading is batch-based; there's no mechanism for incremental updates as new tenders are published. Re-loading the entire OCDS export is inefficient for frequent updates.

### Resolution
**Status:** DEFERRED

Incremental loading deferred until:
- Real PPIP OCDS feed provides incremental exports or change feeds
- Real-data trials show batch loading is a bottleneck
- Practitioners request more frequent data updates

### Alternatives Considered and Rejected

**Alternative 1: Implement change feed detection (track last loaded, fetch only new)**
- **Rejected:** 
  - PPIP OCDS does not provide a change feed API
  - Detecting "new" requires tracking all OCIDs, which scales poorly
  - OCDS exports may include updates to old records, not just new ones
  - Incorrect change detection could cause data inconsistency

**Alternative 2: Timestamp-based incremental loading (load only records after date X)**
- **Rejected:** 
  - OCDS does not guarantee timestamps are monotonic or present
  - Tenders can be amended (new releases) after initial publication
  - Missing earlier updates if relying solely on timestamps
  - Requires database schema changes to track "last loaded" per record

**Alternative 3: Deduplication-based loading (load everything, skip existing OCIDs)**
- **Rejected:** 
  - Still requires downloading the full export (bandwidth inefficient)
  - Doesn't handle updates to existing records (amendments)
  - UPSERT logic already exists; this adds no new capability
  - The bottleneck is download time, not database write time

**Alternative 4: Poll PPIP API for new tenders (if API exists)**
- **Rejected:** 
  - PPIP OCDS is export-based, not API-based
  - Requires PPIP to provide a real-time API (outside our control)
  - API dependency creates operational risk (API changes, downtime)

**CHOSEN APPROACH:** Defer until PPIP provides incremental exports or real-time API. Current batch loading is sufficient for development and initial deployment. Incremental loading should be implemented when the data source supports it natively.

---

## Decision 8: Audit Log Anomaly Detection

### Problem
Audit log is append-only with no automated analysis or anomaly detection. It's a record, not a monitoring system.

### Resolution
**Status:** DEFERRED

Audit log anomaly detection deferred until:
- Real-data trials generate sufficient log volume
- Specific anomalies are identified from real use
- Monitoring requirements are defined by practitioners

### Alternatives Considered and Rejected

**Alternative 1: Real-time anomaly detection on every log entry**
- **Rejected:** 
  - Performance overhead on every tool call
  - False positives would be noisy in development
  - No clear definition of "anomaly" yet
  - Adds complexity without proven need

**Alternative 2: Statistical outlier detection (e.g., tool latency spikes)**
- **Rejected:** 
  - Latency varies by model, hardware, and data size
  - Would require baselining per deployment
  - Statistical outliers are not necessarily security issues
  - Could flag legitimate variation (e.g., large documents)

**Alternative 3: Rule-based anomaly detection (e.g., same OCID from different IPs)**
- **Rejected:** 
  - Local deployment has no IP tracking
  - Same OCID can be legitimately evaluated multiple times (different as_of dates)
  - Rules would be deployment-specific
  - No clear threat model for anomalies

**Alternative 4: Scheduled log analysis (daily/weekly report)**
- **Rejected:** 
  - Adds operational burden (scheduled jobs, report storage)
  - No clear actionability for reports without real data
  - Can be added later as a separate tool if needed

**CHOSEN APPROACH:** Defer until real-data trials identify specific monitoring needs. The audit log is currently sufficient for post-hoc investigation (finding tool calls, replaying briefs). Automated anomaly detection should be added when we know what anomalies to look for and how to act on them.

---

## Consequences

### Positive
- Three-tier gap classification provides actionable confidence guidance for detectable gaps
- Signal testing fix resolves evaluation failures, making peer comparison reliable
- Improved scope guard catches more paraphrased requests with zero cost
- Configurable document limit allows deployment tuning
- Deferred decisions avoid premature optimization and technical debt

### Negative
- Three-tier gap classification is partial (tender security and payment terms deferred per ADR 001 Phase 3)
- Keyword-based scope guard remains incomplete (intent classifier not implemented)
- No real-data testing framework yet (synthetic evaluation only)
- No practitioner feedback UI in briefs (structured review exists but not integrated)
- No incremental loading (batch loading only)
- No audit log anomaly detection (manual log review only)

### Neutral
- Environment variable for document limit adds configuration surface
- Deferred decisions are documented with clear triggers for reconsideration
- Code changes are minimal and defensive (fix bugs, add configurability)

---

## Implementation Follow-up

The following items are completed:
1. ✅ Three-tier gap classification in `tools.py:_render()` (partial: tender security and payment terms deferred per ADR 001 Phase 3)
2. ✅ Signal testing fix in `evidence.py:_add_peers()`
3. ✅ Improved scope guard in `evidence.py` with documentation
4. ✅ Configurable document limit via `WAZI_MAX_DOC_CHARS`

The following items are deferred with clear triggers:
5. ⏸️ Real-data testing framework (trigger: PPIP data access + practitioner review)
6. ⏸️ Practitioner feedback mechanism (trigger: real-data trials)
7. ⏸️ Incremental data loading (trigger: PPIP incremental exports or API)
8. ⏸️ Audit log anomaly detection (trigger: real-data anomaly identification)

---

## References

- ADR 001: Strategic Direction Based on Simulated Practitioner Review and Legal Research
- EVALS.md: Evaluation results showing signal testing failures
- CHECKLIST.md: PPADA Act validation checklist
- ARCHITECTURE.md: System architecture and known limits
