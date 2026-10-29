# ADR 001: Strategic Direction Based on Practitioner Research Findings

**Status:** Accepted
**Date:** 2026-10-29
**Context:** Practitioner review and legal research briefing

## Context

Two documents provided critical research findings:
1. **PRACTITIONER_REVIEW.md** - Simulated practitioner review (buyer-side procurement manager + small supplier's supply chain manager)
2. **Wazi_Procurement_Research_Brief_Final_29Oct2026.md** - Legal research briefing on Kenyan procurement law and data landscape

These findings revealed fundamental issues with brief structure, signal methodology, approval gate, data handling, and strategic gaps that require immediate attention before proceeding with practitioner outreach.

## Decision

### 1. Add Validity Flags to Regulation-Dependent Rules

**Problem:** The 2020 Regulations (LN 69 of 2020) are under legal challenge following *RCECA v AG* (High Court judgment of 4 Dec 2025) declaring them void *ab initio*. The scope of the ruling and any suspension or appeal are unknown.

**Decision:**
- Every rule that depends on the Regulations must carry a `"validity_contested": true` flag in evidence and findings
- Rules based on the Act alone (s.80(6) 30-day evaluation limit, s.87 notification, s.135(3) 14-day standstill, s.86(2) 20% citizen preference) do not require this flag
- Store each rule with its legal basis (Act vs Regulation), provision citation, source date, confidence level, and review-needed flag
- Route any brief touching the e-GP order, regulation validity, Bill 48, variation computation, interest computation, or data localisation to a named human reviewer

**Alternatives Considered and Rejected:**
- **Alternative:** Remove all regulation-dependent checks entirely
  - **Rejected:** This would discard useful information (e.g., 7-day minimum tender periods, 14-day publication deadlines) that practitioners need, even if the legal basis is contested
- **Alternative:** Assume regulations are valid until proven otherwise
  - **Rejected:** This would expose practitioners to legal risk if the regulations are struck down
- **Alternative:** Research the current court status ourselves
  - **Rejected:** Legal research is outside the scope; certified court orders are required from practitioners

### 2. Reorder Brief Structure for Buyer-Side Workflow

**Problem:** Current brief order (deadline → eligibility → price → buyer history) suits suppliers who ask "can I bid?" first. Buyer-side managers who sign off tenders check: procurement plan presence, budget availability, method justification, evaluation criteria in tender documents (s.80(2)), and advertised period meeting legal minimum (s.97).

**Decision:**
- Reorder brief sections to lead with "Paperwork and Process Readiness" for the primary user
- New order: Paperwork and process readiness → Timing → Eligibility and paperwork → Price position → Buyer behaviour → Tender details → Gaps → What to verify → What the brief does not cover
- "Paperwork and process readiness" includes: procurement plan check (if data available), budget availability (if data available), method justification, evaluation criteria presence, advertised period vs legal minimum

**Alternatives Considered and Rejected:**
- **Alternative:** Create two separate briefs (one for buyers, one for suppliers)
  - **Rejected:** This doubles maintenance burden and creates version control complexity; a single re-ordered brief serves both users with different emphasis
- **Alternative:** Keep current order and add a "for buyer" and "for supplier" toggle
  - **Rejected:** This adds UI complexity without solving the fundamental mismatch; the brief structure itself should match the primary user's workflow
- **Alternative:** Make sections reorderable by the user
  - **Rejected:** This prevents consistent documentation and makes verification difficult; the tool should prescribe the correct order based on user research

### 3. Anchor Signals in Act Citations, Not Peer Medians

**Problem:** Signals based on peer medians are being questioned as "noise." Examples: "45 days may indicate delays" should compare with s.80(6) 30-day evaluation limit; "7 days against peer median of 21" should cite the prescribed minimum under s.97 (7 days for open tenders).

**Decision:**
- Decision window signals must cite s.80(6) 30-day evaluation limit explicitly
- Bidding window signals must cite the prescribed minimum under s.97 (7 days for open tenders per regs 86, 87, 89(7), 114, 119) with validity flag
- When comparing to peers, state the legal minimum first, then the peer comparison as additional context
- Remove peer-median-only signals (those without a legal anchor)
- Concentration signals with small samples (e.g., "one supplier won 60%" of 5 awards) must show counts, not just percentages; if sample < 10, report as "insufficient data" not a signal

**Alternatives Considered and Rejected:**
- **Alternative:** Remove all peer comparison signals
  - **Rejected:** Peer comparison provides useful context when legal minima are not available (e.g., evaluation method is not prescribed by law); removing all would lose valuable signal
- **Alternative:** Keep peer-median signals but add confidence scores
  - **Rejected:** This adds complexity without addressing the root issue: signals should be legally grounded where possible
- **Alternative:** Only use peer comparison when sample size > 20
  - **Rejected:** This is arbitrary; the right approach is to prioritize legal citations over peer comparison

### 4. Strengthen Approval Gate for Statutory File Acceptance

**Problem:** A typed, self-asserted name is weak for procurement files that require: every evaluation member signing the report (s.80(7)), post-qualification pages initialled (s.83(3)), head of procurement signing professional opinion (s.84). Current gate has no authentication, no recorded role, no date, no printable fingerprint.

**Decision:**
- Add approver role field (required): must be one of "Head of Procurement", "Accounting Officer", "Evaluation Committee Chair", "Evaluation Committee Member", "Procurement Officer"
- Add approver date field (required): ISO 8601 date
- Add printable fingerprint page: one-page PDF showing brief fingerprint, approver name, role, date, and verification QR code
- Brief must explicitly state: "This brief is not part of the statutory evaluation record" in a prominent location
- Do NOT integrate with IFMIS/e-PROMIS (beyond current scope, requires extensive integration work)

**Alternatives Considered and Rejected:**
- **Alternative:** Add full digital signature integration with Kenya's e-signature infrastructure
  - **Rejected:** This requires external infrastructure and regulatory compliance that is beyond current scope; typed name + role + date + printable fingerprint is sufficient for immediate use
- **Alternative:** Keep current simple typed name only
  - **Rejected:** Practitioner feedback indicates this is insufficient for statutory files; the brief would not be accepted in procurement files
- **Alternative:** Require photograph or biometric authentication
  - **Rejected:** This adds privacy and infrastructure requirements that are disproportionate to the task; typed name with role and date is standard for procurement sign-offs

### 5. Implement Three-Tier "Cannot Be Assessed" Classification

**Problem:** Currently binary ("cannot be assessed" or not). Should distinguish between blockers, must-confirm items, and known gaps. Multiple gaps in one brief should mark the whole brief low confidence.

**Decision:**
- Implement three-tier classification:
  - **Blocker:** missing deadline, missing eligibility criteria, missing required documents, unknown tender security requirement, missing buyer identity for history section
  - **Must confirm:** missing start date, missing payment terms
  - **Known gap:** thin history (< 5 awards), low-confidence benchmark (< 3 comparable tenders)
- Count gaps by tier in the brief
- If blocker count > 0, brief is marked "LOW CONFIDENCE - CRITICAL DATA MISSING" at the top
- If must-confirm count > 2, brief is marked "MEDIUM CONFIDENCE - REQUIRES VERIFICATION"
- If known gap count > 5, brief is marked "MEDIUM CONFIDENCE - LIMITED HISTORY"

**Alternatives Considered and Rejected:**
- **Alternative:** Keep binary classification
  - **Rejected:** This doesn't help practitioners prioritize what needs fixing vs what's acceptable
- **Alternative:** Add numeric confidence score (0-100)
  - **Rejected:** This is arbitrary and hard to interpret; qualitative tiers (blocker/must-confirm/known gap) are clearer
- **Alternative:** Refuse to file briefs with any gaps
  - **Rejected:** This would make the tool unusable on real data; practitioners need to know what's missing, not be blocked from getting partial information

### 6. Add Critical Gap Coverage (Priority Order)

**Problem:** Brief omits items that materially affect supplier pricing and risk: tender security (with AGPO exemption check), reserved status, evaluation method, payment terms and record, conflict of interest, lead time and delivery location.

**Decision:**
- Add checks in priority order:
  1. **Tender security with AGPO check:** Check if tender is reserved for AGPO/enterprises reserved for small enterprises, women, youth, PWD (s.61(5) bars tender security for these). If reservation status unknown, report as "ask buyer" (not as blocker)
  2. **Reserved status:** Check s.53(6) 30% budget reservation requirement (if data available in procurement plan)
  3. **Evaluation method:** Extract from tender document (s.86 allows lowest evaluated price, highest combined score, lowest TCO, or highest technical score). If not found, report as "ask buyer"
  4. **Payment terms and record:** Extract from tender document if available. If not found, report as "ask buyer" (not as gap - this is contract-specific)
  5. **Conflict of interest:** Check if beneficial ownership data is available in PPIP/e-GP export. If not, report as "cannot be assessed - data not published"
  6. **Lead time and delivery location:** Extract from tender document if available. If not found, report as "ask buyer"
- Remove "expected number of bidders" from "what the brief does not cover" (real PPIP data can't supply it)

**Alternatives Considered and Rejected:**
- **Alternative:** Infer conflict of interest from company names or descriptions
  - **Rejected:** This is unreliable and could create false accusations; only use published beneficial ownership data
- **Alternative:** Add all gaps immediately
  - **Rejected:** Implementation priority matters; focus on high-impact gaps first (tender security with AGPO, evaluation method, payment terms)
- **Alternative:** Use external databases for conflict of interest
  - **Rejected:** This requires integration work and data licensing; start with what's in the public OCDS export

### 7. Handle Real Data Limitations Transparently

**Problem:** PPIP OCDS has zero tenderers, planning records, amendments, and milestones. `single_bidder_check`, `amendment_check`, and `termination_check` will report nothing on real data. "No amendments recorded" must not read as "no variations happened."

**Decision:**
- Add data source metadata to every brief: "Data source: PPIP OCDS (synthetic)" or "Data source: PPIP OCDS (real)"
- For tools that rely on data known to be absent in PPIP:
  - `single_bidder_check`: If tenderers array is empty, report "Cannot be assessed - tenderers not published in data source"
  - `amendment_check`: If amendments array is empty, report "Cannot be assessed - amendments not published in data source (this does not mean no variations occurred)"
  - `termination_check`: If termination reason is missing, report "Cannot be assessed - termination details not published"
- Drop `expected_number_of_bidders` from briefs entirely (cannot be sourced from PPIP data)

**Alternatives Considered and Rejected:**
- **Alternative:** Infer amendments from contract value changes
  - **Rejected:** This is unreliable without explicit amendment records; value changes could be corrections, price variations, or data errors
- **Alternative:** Scrape tender documents to find amendment references
  - **Rejected:** This is brittle and computationally expensive; only use structured data
- **Alternative:** Assume zero amendments means no amendments
  - **Rejected:** This is false; PPIP simply doesn't publish amendment data, so absence != zero

### 8. Separate Fact from Interpretation

**Problem:** Sample line "below the median suggests the estimate is conservative" is interpretation. A buyer may read the same fact as a risk of underestimating the budget.

**Decision:**
- State comparisons only: "Estimated value KES 15,000,000 is below the historical median of KES 18,500,000 for comparable tenders (n=12)"
- Remove interpretive language: "suggests", "indicates", "implies", "may mean"
- If interpretation is necessary, add a separate "Interpretation guidance" section clearly labelled as "For your consideration" not as finding

**Alternatives Considered and Rejected:**
- **Alternative:** Keep interpretation but add confidence score
  - **Rejected:** Interpretation should not be the tool's job; practitioners should draw their own conclusions from the facts
- **Alternative:** Remove all comparisons, only report the numbers
  - **Rejected:** Comparisons are useful; just remove the interpretive wrapper around them
- **Alternative:** Provide multiple interpretations (e.g., "could mean X or Y")
  - **Rejected:** This adds confusion; the tool should provide facts, not a menu of possible meanings

### 9. Verify Data Protection Requirements Before Hosted Deployment

**Problem:** Kenya's Data Protection Act (s.49, s.50) restricts cross-border transfer of sensitive personal data. Whether procurement data counts as "public finance administration" under reg 26(2) is an open interpretive question. The Commissioner treats offshore cloud as a transfer.

**Decision:**
- Default to local deployment (Ollama)
- Hosted deployment is opt-in only, with explicit disclosure in the brief: "This brief was generated using a hosted model endpoint at [URL]"
- Do NOT claim that local deployment satisfies data sovereignty requirements without legal review
- Document the open question in a "Data Protection Note" section of the brief when using hosted endpoint
- Prioritize local model support (Ollama) over hosted in documentation and examples

**Alternatives Considered and Rejected:**
- **Alternative:** Ban hosted deployment entirely
  - **Rejected:** Some users may have legal confirmation that hosted deployment is acceptable; opt-in with disclosure is safer
- **Alternative:** Assume local deployment is always compliant
  - **Rejected:** This is a legal question that requires expert review; we cannot make this assumption
- **Alternative:** Add automatic data classification (personal vs non-personal data)
  - **Rejected:** This is complex and error-prone; simpler to default local and disclose when hosted

### 10. Do Not Encode Bill 48 of 2024 Provisions

**Problem:** Bill 48 of 2024 (PPAD Amendment) was returned unsigned on 28 Jul 2026. Outcome after 28 Jul 2026 not found. No later outcome was found.

**Decision:**
- Do not encode any Bill 48 provisions
- If practitioners ask about Bill 48, direct them to check the current status on Parliament's website
- Document Bill 48 as "Not law as of 29 Oct 2026" in any reference material

**Alternatives Considered and Rejected:**
- **Alternative:** Encode Bill 48 provisions but mark them as "pending"
  - **Rejected:** This creates confusion; practitioners might assume the provisions are law
- **Alternative:** Track Bill 48 status automatically via web scraping
  - **Rejected:** This is brittle and outside scope; practitioners should check official sources
- **Alternative:** Wait for Bill 48 to become law before encoding anything
  - **Rejected:** This is already the decision; we don't encode it now

## Consequences

### Positive
- Briefs will be legally grounded where possible, reducing legal risk for practitioners
- Brief structure will match buyer-side workflow, increasing adoption likelihood
- Approval gate will be acceptable for statutory files
- Signal quality will improve (Act-anchored, not peer-median noise)
- Data gaps will be clearly categorized, helping practitioners prioritize
- Critical missing coverage (tender security, evaluation method) will be added
- Real data limitations will be transparent, avoiding false conclusions
- Briefs will be factual, not interpretive, letting practitioners draw their own conclusions
- Data protection risks will be mitigated with local-first deployment
- No provisions from non-law Bills will be encoded

### Negative
- Implementation work: brief reordering, signal refactoring, approval gate enhancement, gap classification, new checks
- Briefs may become longer with additional sections and classifications
- Some peer-median signals will be removed, potentially losing useful context
- Approval gate becomes slightly more complex (role + date fields)
- Hosted deployment will be discouraged, potentially limiting accessibility for users without local hardware

### Neutral
- PRACTITIONER_REVIEW.md and Wazi_Procurement_Research_Brief_Final_29Oct2026.md will be deleted (dev docs no longer needed after ADR)
- Regulation-dependent rules will carry validity flags, adding metadata complexity
- Brief metadata will include data source, adding one more field to track

## Implementation Plan

1. **Phase 1 (Immediate):** Add validity flags to regulation-dependent rules; separate fact from interpretation in synthesize node
2. **Phase 2 (Short-term):** Reorder brief structure; implement three-tier gap classification; strengthen approval gate
3. **Phase 3 (Medium-term):** Refactor signals to be Act-anchored; add critical gap coverage (tender security, evaluation method); handle real data limitations transparently
4. **Phase 4 (Research):** Verify data protection requirements with legal counsel before recommending hosted deployment

## References

- FIT_PLAN.md (updated with validated practitioner feedback)
- ARCHITECTURE.md (updated with strategic direction)
- CHECKLIST.md (PPADA Act validation checks)
- DEFERRED.md (data-dependent roadmap)
- Original practitioner review findings (documented in ADR for historical reference)
