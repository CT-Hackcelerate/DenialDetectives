# UnitedHealthcare Payment Policy — Duplicate Claim Logic and Corrected Claims

**Policy Number:** UHC-CP-071
**Payer:** UnitedHealthcare
**Effective:** January 1, 2026 | **Last reviewed:** March 2026
**Applies to:** Commercial and Medicare Advantage products

## Policy statement

UnitedHealthcare's claim system identifies duplicate submissions by
comparing member ID, rendering provider NPI, date of service, procedure
code (including modifiers), and billed amount against previously received
claims. An exact match to a claim that is paid, in process, or finalized is
denied with **CARC 18** (exact duplicate claim/service), typically with
remark code **N522**.

A "suspect duplicate" — same member, provider, date, and procedure but a
different billed amount or modifier — pends for manual review rather than
auto-denying.

## What a duplicate denial means

A CARC 18 denial is informational: it indicates the original claim already
exists in the system. It is **not** a request for a corrected claim, and it
does not change the disposition of the original. Before taking any action,
locate the original claim and its status:

- **Original paid:** post the payment and close the duplicate. No further
  submission is appropriate; repeated resubmission of paid services is
  flagged by program integrity analytics.
- **Original denied:** work the original denial on its own merits. The
  duplicate denial itself carries no appeal rights.
- **Original in process:** allow adjudication to complete (standard
  turnaround is thirty days) before any follow-up.

## Corrected claims are not duplicates

To change a previously submitted claim — adding a modifier, correcting
units, or fixing a diagnosis pointer — submit a **replacement claim with
frequency code 7** (or a void with frequency code 8) in the 2300 loop,
CLM05-3 element, referencing the original claim number in the REF*F8
segment. Replacement claims bypass duplicate logic and re-adjudicate the
original in full.

Submitting the corrected version as a brand-new claim, without frequency
code 7, is the most common cause of avoidable CARC 18 denials: the system
sees the same member/provider/date/procedure and rejects it before the
correction is ever evaluated.

## Timeliness

Replacement claims must be received within the contractual filing limit
measured from the date of service, not from the duplicate denial date.
Duplicate denials do not extend filing deadlines.

## Practical guidance

When a denial work queue shows CARC 18, the highest-value first step is
retrieving the original claim's outcome. If the original paid at the
expected allowable, the correct disposition is closure with no
resubmission. Balance-billing the member for a duplicate denial is
prohibited under the participation agreement.
