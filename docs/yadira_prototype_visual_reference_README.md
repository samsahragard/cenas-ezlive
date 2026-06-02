# Yadira Prototype Visual Reference

Created for CK/Mini_IT13 visual parity work on 2026-06-02.

This branch contains visual/template reference files only. It is not production-ready app code and does not approve any data logic.

Important safety notes:

- Treat all prototype-derived surfaces as fresh audit surfaces.
- Do not ship service/signal sections unless every field has a real employee-specific source, privacy classification, implementation proof, and AiCk/samai audit.
- BOH/non-tipped/no-tip payloads must not carry tips, tip percentage, tips/hour, tip ranks, combined tipped ranks, sales, or sales-derived fields server-side.
- Do not ship Local test/debug/cache/internal/synced wording.
- Do not infer approval from this branch for `employee_performance_center.py` or any backend prototype logic. That backend prototype was intentionally not included.

Included reference files:

- app/templates/employee_dashboard.html
- app/templates/employee_performance_detail.html
- app/templates/employee_roster.html
- app/templates/employee_service.html

Use these as visual and route/detail-layout reference only, then map each section through the proof gates in docs/ck_employee_performance_db_verification.md.
