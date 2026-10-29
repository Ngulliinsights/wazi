# Deferred Items Requiring External Integration

Based on the Kenya-specific OCDS Edge-Case Checklist and lifecycle scope analysis (see FIT_PLAN.md section 6), the PPADA Act Validation Checklist (CHECKLIST.md), and practitioner research findings (ADR 001), the following features are **deferred** because they require data or integration not currently available in Kenya's OCDS exports or Wazi's scope.

**Note:** ADR 001 identifies critical gaps that must be added to current scope (tender security with AGPO check, evaluation method, payment terms, conflict of interest). These are not deferred; they are pending implementation.

## Implementation Data (Checklist Sections 4-5)

### What's Missing (Section 4: Implementation)
The checklist notes that PPIP's OCDS data shows **zero milestones** and **zero transactions**, so these cannot be tested:
- Delivered or certified quantity vs contracted quantity
- Acceptance or inspection dates for each delivery
- Partial delivery validation (sum to no more than contract quantity)
- Deliveries after contract end date

### What's Missing (Section 5: Invoice and Payment)
The checklist notes that PPIP's OCDS data shows **zero transactions**, so these cannot be tested:
- Paid amount vs certified delivered value (shadow-invoice reconciliation test)
- Total payments vs contract value plus amendments
- Duplicate payment detection
- Payments dated before acceptance
- Delivered but unpaid items (pending bills) aging
- Interest on overdue amounts

### Kenya Timeline
Kenya's 2023-2027 OGP commitment (KE0036) targets:
- Public e-GP portal with OCDS, CSV, PDF data by **December 2026**
- PPIP interoperability API by **October 2026**
- Treasury requires all procurement through e-GP from **1 July 2026**

When implementation data becomes available, add:
- `delivery_reconciliation()` - compare contract quantity vs certified delivered quantity
- `invoice_verification()` - shadow-invoice test (paid vs delivered)
- `payment_tracking()` - pending bills, duplicate payments, overdue interest

## PPADA Act Legal Limits (Section 3)

See CHECKLIST.md for detailed PPADA Act validation checks with governance mapping and shadow ledger concept.

### What's Implemented (Proxies)
- `amendment_check` - detects amendments, but **cannot** validate 15%/25% quantity limits or 12-month price variation rule because OCDS doesn't publish amendment details (old value, new value, quantity change)
- `ppada_date_order_check` - validates tender close → award order, but **cannot** validate 14-day award-to-contract signing requirement because contract signing date not in OCDS
- `single_bidder_check` - detects single-bidder tenders (flagged for special procedures)
- `termination_check` - detects terminated status, but **cannot** validate reason/date recording (not in OCDS)

### What's Deferred (Requires Data)
- Goods quantity variation ≤15% of original (section 139(4))
- Cumulative variation ≤25% of original price (section 139(4))
- No upward price variation within 12 months of signing (section 139(3)(a))
- Price variations after 12 months track KNBS CPI (section 139(4)(a))
- Repeated end-date extensions (currently flagged manually)
- Quarterly reports of varied contracts to PPRA (section 139(5))
- Attorney-General clearance for contracts > KES 5 billion (section 134)
- Termination reason and date recording

## External Integration Requirements

### Budget Commitment Control
The `budget_exposure` tool is a **proxy** - it estimates exposure from award values because actual budget commitment control data isn't in OCDS. Real integration would require:
- Integration with IFMIS/e-PROMIS budget commitment modules
- Access to budget availability and encumbrance data
- Real-time budget exposure tracking

### Identifier Quality (Beneficial Ownership)
The `counterparty_quality` tool checks basic ID/name consistency. Full beneficial ownership verification would require:
- Integration with company registry systems
- Access to shareholder/beneficial ownership data
- Cross-border entity linking (company identifiers across jurisdictions)
- Check against debarment lists (procurement-specific requirement from checklist)

### Digital Signatures
Current approval gate uses typed name + fingerprint. Real digital signatures would require:
- Integration with Kenya's e-signature infrastructure
- Certificate management
- Compliance with e-Signature laws

## Cross-System Integrity (Section 6)

### What's Implemented
- OCID-based linking within single data source
- Data quality checks (identifier consistency, missing fields)

### What's Deferred
- OCID linking across e-GP, PPIP legacy data, county portals (Makueni)
- Historical PPIP records vs new e-GP records (no double counting/dropping)
- Validation that only e-GP procurements are paid (Treasury requirement)
- Status transition validation (e.g., no payment on cancelled contract)

## Publication Quality (Section 7)

### What's Implemented
- `publication_quality_check` - flags missing documents, eligibility, buyer info, awards
- Documents check (PPIP shows zero documents across all tenders)

### What's Deferred
- Award notice publication on entity website and notice board (requires web scraping or API)
- Notice board reporting to PPRA within regulatory period (period figure not in Act, requires checking regulations)
- JSON/CSV download accessibility (requires live web check)
- Document link resolution (already partially implemented via fetch server)
- Missing-field rates per stage reporting (can be generated from audit log)

## Summary Table

| Checklist Item | Status | Implementation | Data Gap |
|---|---|---|---|
| Section 1: Supplier identifiers | Partial | `counterparty_quality` (ID/name consistency) | No debarment check, no beneficial ownership |
| Section 2: Date order | Partial | `ppada_date_order_check` (tender→award) | No contract signing date (14-day rule) |
| Section 2: Single-bidder flag | ✓ | `single_bidder_check` | None |
| Section 2: Unit/outlier detection | Partial | Already in `price_benchmark` | Currency/unit consistency check could be added |
| Section 3: Amendments | Partial | `amendment_check` (detects amendments) | No 15%/25%/12-month rule validation (no amendment details) |
| Section 3: AG clearance >5B | Deferred | None | Threshold check not implemented (needs contract value field) |
| Section 3: Termination | Partial | `termination_check` (detects status) | No reason/date recording validation |
| Section 4: Delivery data | Deferred | None | Zero milestones in PPIP data |
| Section 5: Invoice/payment | Deferred | None | Zero transactions in PPIP data |
| Section 6: Cross-system integrity | Deferred | None | e-GP/PPIP/county portal linkage |
| Section 7: Publication checks | Partial | `publication_quality_check` | No website/notice board validation |

## What This Means for Wazi

Current Wazi addresses **pre-award lifecycle** with Kenya-specific PPADA Act checks:
- ✓ Pre-trade checks (counterparty quality)
- ✓ Tender/award validation (date order, single-bidder, termination)
- ✓ Contract amendments (detection, not limit validation)
- ✓ Publication quality (missing fields per stage)
- ✓ Proxy for budget exposure and aged items

The **post-award implementation** (sections 4-5) is deferred until:
1. Kenya's end-to-end OCDS milestone (Dec 2026) includes implementation data
2. PPIP data includes milestones and transactions
3. Integration with e-GP financial systems is feasible

This is **not a bug** - it's a data availability constraint. Post-award implementation checks are deferred until Kenya's end-to-end OCDS milestone (Dec 2026) includes implementation data.
