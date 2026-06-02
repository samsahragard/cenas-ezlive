# Toast TimeEntry field provenance (N1 — aick #2953)

Raw Toast **Labor API `timeEntries`** object, captured live from Yadira's pull (2026-06-01).
Documents the fields the CK importer uses — so tip/auto-close handling is sourced, not folklore.

## Fields the CK importer EXTRACTS (employee-own, sales-free)
- `regularHours`, `overtimeHours` — hours. OT is **weekly/FLSA** (Toast computes it; >40h/week, no daily OT in TX). Importer takes them verbatim.
- `hourlyWage` — the employee's OWN wage. **INTERNAL on CK** — only the computed `base_pay` (= reg*w + ot*w*1.5) is pushed; the raw rate never leaves the box (no rate column in PerfShiftCache).
- `nonCashTips` — credit-card tips, GUID-keyed on the entry.
- `declaredCashTips` — declared cash tips, GUID-keyed.
- `inDate` / `outDate` — clock in/out.
- `businessDate` — yyyymmdd (int or string).
- **`autoClockedOut`** — Toast's OWN boolean: `true` = the shift was auto-clocked-out (forgotten punch). **N5 uses THIS** (authoritative, zero false positives) — not a round-clock-out heuristic.
- `employeeReference.guid` — the attribution anchor (== `cena_toast_link.toast_id`).
- `deleted` — filtered out.

`tips` pushed = `nonCashTips + declaredCashTips` (per-shift, GUID-keyed). `tips_declared` = either present (N4: null vs $0).

## Raw sample (Yadira, 2026-05-04, autoClockedOut=false)
`{"inDate":"2026-05-04T15:03:31.348+0000","outDate":"2026-05-04T21:22:04.983+0000","regularHours":6.31,"overtimeHours":0.0,"hourlyWage":2.13,"nonCashTips":119.81,"declaredCashTips":0.0,"businessDate":"20260504","autoClockedOut":false}`

## IMPORTANT — the timeEntry DOES carry SALES fields (we do NOT extract them)
The raw object ALSO contains: `cashSales`, `nonCashSales`, `cashGratuityServiceCharges`,
`nonCashGratuityServiceCharges`, `nonCashTipsRoundingLoss`, `tipsWithheld`.

So the source is **NOT** sales-free (correcting the earlier "Labor timeEntry has no sales field" assumption, aick #2927). CK is sales-safe because the importer **extracts only the whitelist above** — it never reads `cashSales`/`nonCashSales`. The N3 push-guard (`PUSH_SALES_RE`) is the backstop: it refuses to push if any sales-$ term appears in the payload.

## Full raw field list (26)
autoClockedOut, breaks, businessDate, cashGratuityServiceCharges, cashSales, createdDate,
declaredCashTips, deleted, deletedDate, employeeReference, entityType, externalId, guid,
hourlyWage, inDate, jobReference, modifiedDate, nonCashGratuityServiceCharges, nonCashSales,
nonCashTips, nonCashTipsRoundingLoss, outDate, overtimeHours, regularHours, shiftReference, tipsWithheld
