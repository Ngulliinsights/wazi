# PPADA Act Validation Checklist

Based on practitioner analysis of Kenya's Public Procurement and Asset Disposal Act, 2015, and the "shadow ledger" concept for independent evaluation recomputation.

**Source:** Public Procurement and Asset Disposal Act, 2015 (Cap. 412C)
**Date:** 2026-10-29
**Status:** Concrete validation checks implementable with current tender/award data. Note: Regulation-dependent rules require validity flags per ADR 001 (2020 Regulations under legal challenge).

---

## Governance Mapping

| Energy Trading Role | Evaluation Equivalent | PPADA Act Hook |
|---|---|---|
| Front office (decision) | Evaluation committee scores and compares tenders | Sections 46, 80 |
| Middle office (four-eyes) | Head of procurement's signed professional opinion, then accounting officer approval | Sections 84, 85, 80(5) |
| Back office (confirmation) | Post-qualification due diligence: verify winner's claims, references from past clients | Section 83 |

**Weakness:** Section 83 says the committee "may" run due diligence, and only on the lowest evaluated responsive tenderer. This is discretionary and covers one bidder, unlike energy trading's standing back-office function that checks every deal.

---

## Evaluation-Phase Shadow Ledger Checks

The shadow ledger is an independent running record, per contract, of what the award, variations and certified deliveries imply should have been paid. It is kept apart from the payment system and compared against it.

### Starting Entries (Evaluation Phase)

These checks can be implemented with current tender/award data:

#### 1. Re-derive Ranking and Evaluated Price
- **What:** Independently recompute the ranking and evaluated price from bid data and the tender document's stated method
- **PPADA Act:** Section 86 allows lowest evaluated price, highest combined score, lowest total cost of ownership, or highest technical score
- **Implementation:** Use whichever method the tender document names
- **Data required:** Tender document (method), bid data (not currently published by portals)
- **Status:** **DEFERRED** - bid documents are not published by Kenyan portals

#### 2. Corrections and Deviations Consistency
- **What:** Verify that minor deviations were quantified and taken into account, and corrections were applied consistently
- **PPADA Act:** Section 79(3) (minor deviations quantified), Section 82 (corrections)
- **Implementation:** A recomputation shows whether corrections/deviations were applied consistently
- **Data required:** Tender document (correction rules), bid data (deviations)
- **Status:** **DEFERRED** - bid documents are not published by Kenyan portals

#### 3. Award Value vs Evaluated Price Comparison
- **What:** Compare the award value with the evaluated price from the committee's report
- **PPADA Act:** Implied by Sections 80-86 (evaluation and award process)
- **Implementation:** Award value should equal evaluated price. A gap means an unexplained adjustment.
- **Data required:** Award value, evaluated price (not currently in OCDS)
- **Status:** **DEFERRED** - evaluated price is not published in OCDS

#### 4. Lowest-Bidder Check
- **What:** If lowest evaluated price method is used, verify the award went to the lowest evaluated bidder
- **PPADA Act:** Section 86 (lowest evaluated price method)
- **Implementation:** Compare awarded supplier with lowest evaluated bidder
- **Data required:** All bid evaluations (not currently published)
- **Status:** **DEFERRED** - bid evaluations are not published by Kenyan portals

### What This Catches at Evaluation
- Scoring arithmetic errors
- Award to a non-lowest bidder with no stated reason
- Criteria that differ from those in the tender documents
- An evaluation running past the 30-day limit
- A report missing member signatures

---

## Time Limit Checks

These are concrete date-based checks implementable with current data:

#### 5. Evaluation Time Limit (30 Days)
- **What:** Verify evaluation does not exceed 30 days from bid opening
- **PPADA Act:** Section 80 (evaluation timeline - exact period to be verified in regulations)
- **Implementation:** Compare evaluation report date with bid opening date
- **Data required:** Bid opening date, evaluation report date
- **Status:** **DATA GAP** - bid opening date may not be in OCDS; evaluation report date not published
- **Proxy Implementation:** `process_timeline_check` calculates decision window from tender deadline to award date (flags unusually long gaps >60 days as potential process delays, presented as data observation not legal verdict)

#### 6. Report Signature Completeness
- **What:** Verify evaluation report includes all required member signatures
- **PPADA Act:** Section 80 (report requirements)
- **Implementation:** Check document metadata or extracted signatures
- **Data required:** Evaluation report document
- **Status:** **DEFERRED** - evaluation reports are not published by Kenyan portals

---

## Post-Award Shadow Ledger (Delivery & Payment)

The evaluated price becomes the opening balance of the ledger. Variations adjust it within Section 139(4) limits, certified deliveries reduce what is owed, and payments and Section 140 interest follow.

### Ledger Entries (Require Implementation Data)

#### 7. Variation Tracking (Section 139)
- **What:** Track contract variations and ensure they stay within legal limits
- **PPADA Act:** Section 139 (variations)
  - 139(4): Quantity variation up to 15%; cumulative variation up to 25%
  - 139(3)(a): No upward price variation within 12 months of signing
  - 139(4)(a): Price variations after 12 months track KNBS CPI
  - 139(5): Quarterly reports of varied contracts to PPRA
  - 139(6): Above-25% variation should be separately tendered
- **Implementation:** `amendment_check` detects amendments but cannot validate limits (no amendment details in OCDS)
- **Data required:** Amendment details (old value, new value, quantity change, variation date)
- **Status:** **PARTIAL** - `amendment_check` detects amendments; limit validation deferred until amendment details are published

#### 8. Certified Delivery Reconciliation
- **What:** Compare certified delivered quantity against contract quantity
- **PPADA Act:** Implied by Sections 139-140 (delivery and payment)
- **Implementation:** Delivery vs contract reconciliation
- **Data required:** Milestones (delivery records)
- **Status:** **DEFERRED** - PPIP OCDS has zero milestones

#### 9. Payment Verification (Shadow Invoice Test)
- **What:** Paid amount should not exceed certified delivered value
- **PPADA Act:** Section 140 (payment terms, interest on overdue amounts)
- **Implementation:** Invoice verification against delivery
- **Data required:** Transactions (payment records)
- **Status:** **DEFERRED** - PPIP OCDS has zero transactions

#### 10. Pending Bills Aging
- **What:** Track delivered but unpaid items (accruals gap)
- **PPADA Act:** Implied by Section 140 (overdue interest)
- **Implementation:** `budget_exposure` provides a proxy using award history
- **Data required:** Payment dates, delivery dates
- **Status:** **PROXY** - `budget_exposure` estimates from award history; real implementation requires payment data

---

## Summary Table

| Check | Status | Implementation | Data Gap |
|---|---|---|---|
| Re-derive ranking/evaluated price | Deferred | None | Bid documents not published |
| Corrections/deviations consistency | Deferred | None | Bid documents not published |
| Award value vs evaluated price | Deferred | None | Evaluated price not in OCDS |
| Lowest-bidder check | Deferred | None | Bid evaluations not published |
| Evaluation time limit (30 days) | Proxy | `process_timeline_check` (decision window from deadline to award) | Bid opening date, evaluation report date not in OCDS |
| Report signature completeness | Deferred | None | Evaluation reports not published |
| Variation tracking (Section 139) | Partial | `amendment_check` detects amendments | Amendment details (old/new values, quantity change) not in OCDS |
| Certified delivery reconciliation | Deferred | None | Zero milestones in PPIP data |
| Payment verification (shadow invoice) | Deferred | None | Zero transactions in PPIP data |
| Pending bills aging | Proxy | `budget_exposure` from award history | Payment/delivery dates not in OCDS |

---

## What Is Implementable Now

With current tender/award data (PPIP OCDS), Wazi can implement:

1. **Date order validation** (`ppada_date_order_check`) - tender close → award order
2. **Single-bidder detection** (`single_bidder_check`) - flags tenders requiring special procedures
3. **Termination checks** (`termination_check`) - detects terminated status
4. **Amendment detection** (`amendment_check`) - detects amendments, not limit validation
5. **Publication quality** (`publication_quality_check`) - missing fields per stage
6. **Buyer data validation** (`counterparty_quality`) - pre-check for analysis readiness
7. **Process timeline check** (`process_timeline_check`) - decision window from deadline to award (proxy for evaluation time limit)

### What Requires Bid Documents

The evaluation-phase shadow ledger checks (1-4 above) require bid documents and evaluation reports, which Kenyan portals do not publish. These cannot be implemented without either:
- Portal policy change to publish bid evaluations
- Internal system access (not public)

### What Requires Implementation Data

The post-award shadow ledger checks (7-10 above) require milestones and transactions, which PPIP OCDS does not publish. These are deferred until Kenya's December 2026 end-to-end OCDS milestone.

---

## Reference

**Public Procurement and Asset Disposal Act, 2015 (Cap. 412C)**
- Official source: [Attorney-General's website](https://attorneygeneral.go.ke/) (exact URL to be added)
- Key sections referenced: 46, 79(3), 80, 82, 83, 84, 85, 86, 139, 140

**Analysis Source:** Practitioner governance mapping and shadow ledger concept (2026-10-29 discussion)
