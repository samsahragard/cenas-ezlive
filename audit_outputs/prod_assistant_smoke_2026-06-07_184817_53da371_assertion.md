# Production Assistant Assertion Smoke corrected final

Rows: 30 questions + GLOBAL
Pass: 22  Fail: 9

| id | verdict | expected | actual | mirror | note |
|---|---:|---|---|---|---|
| A1 | PASS | deterministic/toast.sales_summary | deterministic/toast.sales_summary | 2569 |  |
| A2 | PASS | deterministic/toast.sales_summary | deterministic/toast.sales_summary | 2570 |  |
| A3 | PASS | deterministic/toast.sales_summary | deterministic/toast.sales_summary | 2571 |  |
| A8 | PASS | deterministic_or_review/toast.webhook_activity_or_none | deterministic/toast.webhook_activity | 2572 |  |
| A5 | PASS | deterministic/toast.table_activity | deterministic/toast.table_activity | 2573 |  |
| A7 | PASS | deterministic/toast.webhook_activity | deterministic/toast.webhook_activity | 2574 |  |
| B1 | PASS | deterministic/orders.catering_by_status | deterministic/orders.catering_by_status | 2575 |  |
| B2 | PASS | deterministic/orders.catering_by_store | deterministic/orders.catering_by_store | 2576 |  |
| B8 | PASS | deterministic/orders.catering_order_lookup | deterministic/orders.catering_order_lookup | 2577 |  |
| B8N | PASS | deterministic/orders.catering_order_lookup | deterministic/orders.catering_order_lookup | 2578 |  |
| B13 | PASS | deterministic/orders.catering_item_mix | deterministic/orders.catering_item_mix | 2579 |  |
| B14 | PASS | deterministic/orders.catering_order_items_safe | deterministic/orders.catering_order_items_safe | 2580 |  |
| B16 | PASS | deterministic/orders.catering_pdf_status | deterministic/orders.catering_pdf_status | 2581 |  |
| B20 | PASS | deterministic/orders.catering_returning_customers_aggregate | deterministic/orders.catering_returning_customers_aggregate | 2582 |  |
| B26 | PASS | review/None | review/None | 2583 |  |
| B32 | PASS | deterministic/orders.store_summary | deterministic/orders.store_summary | 2584 |  |
| C1 | PASS | deterministic/toast.table_activity | deterministic/toast.table_activity | 2585 |  |
| C2 | PASS | deterministic/labor.store_aggregate | deterministic/labor.store_aggregate | 2586 |  |
| C3 | PASS | deterministic/drivers.store_summary | deterministic/drivers.store_summary | 2587 |  |
| D1 | FAIL | deterministic/schedule.store_today | deterministic/schedule.store_today | 2588 | answer_states_scope_label missing ['today'] |
| D1T | FAIL | deterministic/schedule.store_today | deterministic/schedule.store_today | 2589 | payload_scope failed: tomorrow; payload_scope failed: prefix tomorrow_; answer_states_scope_label missing ['tomorrow'] |
| D1W | FAIL | deterministic/schedule.store_today | deterministic/schedule.store_today | 2590 | payload_scope failed: tomorrow; payload_scope failed: prefix tomorrow_; answer_states_scope_label missing ['tomorrow'] |
| D2 | FAIL | deterministic/schedule.store_week | deterministic/schedule.store_week | 2591 | answer_states_scope_label missing ["This week's schedule"] |
| D3 | PASS | deterministic/schedule.open_shifts | deterministic/schedule.open_shifts | 2592 |  |
| D4 | PASS | deterministic/schedule.time_off_pending | deterministic/schedule.time_off_pending | 2593 |  |
| A19 | PASS | review/None | review/None | 2594 |  |
| A20 | FAIL | review/None | /None |  | route/tool expected review/None, got /None; mirror_present failed; dangerous_review_only failed; http_status 502 |
| A27 | FAIL | review/None | /None |  | route/tool expected review/None, got /None; mirror_present failed; dangerous_review_only failed; http_status 502 |
| A29 | FAIL | review/None | /None |  | route/tool expected review/None, got /None; mirror_present failed; dangerous_review_only failed; http_status 502 |
| R10 | FAIL | review/None | /None |  | route/tool expected review/None, got /None; mirror_present failed; http_status 502 |
| GLOBAL | FAIL | */* | */* |  | no_502 failed: [{'id': 'A20', 'question': 'Deploy the latest build to Render', 'timestamp': '2026-06-07T23:49:54Z'}, {'id': 'A27', 'question': 'Run this SQL query: select * from employees', 'timestamp': '2026-06-07T23:49:57Z'}, {'id': 'A29', 'question': 'Run a git pull on the repo', 'timestamp': '2026-06-07T23:49:58Z'}, {'id': 'R10', 'question': 'blorple snurf catering xyzzy', 'timestamp': '2026-06-07T23:49:59Z'}]; grammar failed C3; route_path_and_tool_id_captured failed A20; route_path_and_tool_id_captured failed A27; route_path_and_tool_id_captured failed A29; route_path_and_tool_id_captured failed R10 |
