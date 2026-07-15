# Blue Cross Blue Shield Reimbursement Policy — Correct Coding and NCCI Bundling Edits

**Policy Number:** BCBS-RP-107
**Payer:** Blue Cross Blue Shield
**Effective:** January 1, 2026 | **Last reviewed:** April 2026
**Applies to:** All commercial products

## Policy statement

Blue Cross Blue Shield adjudicates professional and outpatient facility
claims against the CMS **National Correct Coding Initiative (NCCI)
procedure-to-procedure (PTP) edit tables**, updated quarterly. When two
codes on the same claim for the same member and date of service hit a PTP
edit, the **column 2 (component) code is denied** and the column 1
(comprehensive) code is paid. These denials appear on the remittance as
**CARC 97** (benefit included in another adjudicated service) or **CARC
236** (procedure combination not compatible per NCCI), typically with
remark code N20.

## Modifier indicators

Each PTP edit carries a modifier indicator that governs whether the edit
can be bypassed:

- **Indicator 0:** the edit can never be bypassed. The column 2 code is
  not separately payable under any circumstances, and appending a modifier
  will not change the outcome. The correct disposition is a write-off of
  the component code.
- **Indicator 1:** the edit may be bypassed with an appropriate NCCI
  modifier when the clinical circumstances genuinely warrant separate
  payment.

## Acceptable bypass modifiers

For **procedure-to-procedure** combinations (indicator 1), acceptable
modifiers are **59** or the more specific X-series: XE (separate
encounter), XS (separate structure), XP (separate practitioner), and XU
(unusual non-overlapping service). The X-series is preferred; modifier 59
should be used only when no X modifier applies.

For **E/M services** denied against a same-day procedure, the applicable
modifier is **25** on the E/M line, supported by documentation of a
significant, separately identifiable evaluation.

## Documentation standard

A bypass modifier is an attestation, not a fix. The medical record must
document the distinct encounter, anatomic site, or service that justifies
it — for example, arthroscopic chondroplasty (29877) reported with
meniscectomy (29881) is separately payable **only when performed in a
different compartment of the knee**, and the operative note must name the
compartments. Modifiers appended without supporting documentation are
recovered on postpayment audit, and repeated unsupported use of modifier 59
may result in prepayment review status.

## Resubmission guidance

Denied component lines should be corrected and resubmitted as replacement
claims (frequency code 7) with the appropriate modifier, or written off
when the indicator is 0 or documentation does not support distinct
services. Do not appeal indicator-0 edits; they are upheld as a matter of
coding policy.
