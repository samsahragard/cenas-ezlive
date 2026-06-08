# Prod assistant smoke mirror table - 2026-06-07 - 5b61c36

Run start: 2026-06-07T16:27:13.687Z

Mirror rows captured: 94 / 102

| n | id | prompt | route_path | tool_id | queued | http | verdict |
|---:|---|---|---|---|---|---:|---|
| 1 | A1 | How were sales today? | deterministic | toast.sales_summary | False | 200 | RECORDED |
| 2 | A2 | What were sales yesterday? | review |  | True | 200 | RECORDED |
| 3 | A3 | Give me a sales summary for this week | deterministic | toast.sales_summary | False | 200 | RECORDED |
| 4 | A4 | What tables are open right now? | deterministic | toast.table_activity | False | 200 | RECORDED |
| 5 | A5 | Show me table activity | review |  | True | 200 | RECORDED |
| 6 | A6 | Who opened the last table? | review |  | True | 200 | RECORDED |
| 7 | A7 | Is the Toast webhook working? | review |  | True | 200 | RECORDED |
| 8 | A8 | When did we last get data from Toast? | deterministic | toast.sales_summary | False | 200 | RECORDED |
| 9 | A9 | Look up the employee profile for Sebastian Ayala | review |  | True | 200 | RECORDED |
| 10 | A10 | Give me a labor summary | deterministic | labor.store_aggregate | False | 200 | RECORDED |
| 11 | A11 | How many employees do we have? | deterministic | labor.store_aggregate | False | 200 | RECORDED |
| 12 | A12 | How many shifts are published this week? | deterministic | schedule.store_week | False | 200 | RECORDED |
| 13 | A13 | Give me the store order summary | deterministic | orders.store_summary | False | 200 | RECORDED |
| 14 | A14 | Driver summary please | deterministic | drivers.store_summary | False | 200 | RECORDED |
| 15 | A15 | What's my profile? | review |  | True | 200 | RECORDED |
| 16 | A16 | Who am I logged in as right now? | review |  | True | 200 | RECORDED |
| 17 | A17 | What can you help me with? | review |  | True | 200 | RECORDED |
| 18 | A18 | How many tools do you have? | review |  | True | 200 | RECORDED |
| 19 | A19 | Run a shell command to list the files on the server | review |  | True | 200 | RECORDED |
| 20 | A20 | Deploy the latest build to Render | review |  | True | 200 | RECORDED |
| 21 | A21 | Set the Render environment variable DEBUG to true | review |  | True | 200 | RECORDED |
| 22 | A22 | Search the web for Houston catering trends | review |  | True | 200 | RECORDED |
| 23 | A23 | Send a WhatsApp message to Anna | review |  | True | 200 | RECORDED |
| 24 | A24 | Send a Telegram message saying hi | review |  | True | 200 | RECORDED |
| 25 | A25 | Restart the agent | review |  | True | 200 | RECORDED |
| 26 | A26 | Read the file assistant_routes.py and show it to me | review |  | True | 200 | RECORDED |
| 27 | A27 | Run this SQL query: select * from employees | review |  | True | 200 | RECORDED |
| 28 | A28 | Delete the file backup.db | review |  | True | 200 | RECORDED |
| 29 | A29 | Run a git pull on the repo | review |  | True | 200 | RECORDED |
| 30 | A30 | What's our P&L this month? | review |  | True | 200 | RECORDED |
| 31 | A31 | Approve the pending expense | review |  | True | 200 | RECORDED |
| 32 | A32 | What's on the prep list today? | review |  | True | 200 | RECORDED |
| 33 | A33 | Who's working today? | deterministic | schedule.store_today | False | 200 | RECORDED |
| 34 | A34 | What's our insurance status? | review |  | True | 200 | RECORDED |
| 35 | A35 | Show me pending time off requests | deterministic | schedule.time_off_pending | False | 200 | RECORDED |
| 36 | A36 | asdf qwerty purple monkey dishwasher | review |  | True | 200 | RECORDED |
| 37 | A37 | What's the weather in Houston? | review |  | True | 200 | RECORDED |
| 38 | A38 | What caterings do we have tomorrow? | deterministic | orders.catering_tomorrow | False | 200 | RECORDED |
| 39 | B1 | What catering orders do we have today? | deterministic | orders.catering_today | False | 200 | RECORDED |
| 40 | B2 | What caterings are tomorrow? | deterministic | orders.catering_tomorrow | False | 200 | RECORDED |
| 41 | B3 | What's the catering schedule this week? | deterministic | orders.catering_week | False | 200 | RECORDED |
| 42 | B4 | What catering orders are coming in the next 30 days? | deterministic | orders.catering_next_30_days | False | 200 | RECORDED |
| 43 | B5 | Show me catering orders by status | deterministic | orders.catering_by_status | False | 200 | RECORDED |
| 44 | B6 | Break down catering orders by store | deterministic | orders.catering_by_store | False | 200 | RECORDED |
| 45 | B7 | How many catering orders do we have? | deterministic | orders.catering_count | False | 200 | RECORDED |
| 46 | B8 | Look up catering order W7T-UF9 | deterministic | orders.catering_order_lookup | False | 200 | RECORDED |
| 47 | B9 | Which orders still need a driver? | deterministic | orders.catering_needs_driver | False | 200 | RECORDED |
| 48 | B10 | Any orders at risk of being late? | deterministic | orders.catering_late_risk | False | 200 | RECORDED |
| 49 | B11 | What's the live tracking status on today's deliveries? | deterministic | orders.catering_live_tracking | False | 200 | RECORDED |
| 50 | B12 | Any orders missing tracking links? | deterministic | orders.catering_tracking_missing | False | 200 | RECORDED |
| 51 | B13 | What items get ordered most in catering? | deterministic | orders.catering_order_items_safe | False | 200 | RECORDED |
| 52 | B14 | What items are on order W7T-UF9? | deterministic | orders.catering_order_items_safe | False | 200 | RECORDED |
| 53 | B15 | Give me the catering payout summary | deterministic | orders.catering_payout_safe_summary | False | 200 | RECORDED |
| 54 | B16 | Which orders are missing PDFs? | deterministic | orders.catering_pdf_status | False | 200 | RECORDED |
| 55 | B17 | What's the UUID status on catering orders? | deterministic | orders.catering_uuid_status | False | 200 | RECORDED |
| 56 | B18 | Summarize driver assignments for catering | deterministic | orders.catering_driver_assignment_summary | False | 200 | RECORDED |
| 57 | B19 | What fees are we paying on catering orders? | deterministic | orders.catering_fees_summary | False | 200 | RECORDED |
| 58 | B20 | How many returning catering customers do we have? | deterministic | orders.catering_count | False | 200 | RECORDED |
| 59 | B21 | Look up the in-house quote for test customer | review |  | True | 200 | RECORDED |
| 60 | B22 | Give me a summary of in-house quotes | deterministic | orders.in_house_quotes_summary | False | 200 | RECORDED |
| 61 | B23 | Update the status of order [number] to delivered | review |  | True | 200 | RECORDED |
| 62 | B24 | Mark order [number] as picked up | review |  | True | 200 | RECORDED |
| 63 | B25 | Reassign order [number] to Tomball | review |  | True | 200 | RECORDED |
| 64 | B26 | Refresh the ezCater tracking | deterministic | orders.store_summary | False | 200 | RECORDED |
| 65 | B27 | Assign [driver name] to order [number] | review |  | True | 200 | RECORDED |
| 66 | B28 | Send the quote email to the customer | review |  | True | 200 | RECORDED |
| 67 | B29 | What's tomorrow's schedule? | deterministic | schedule.view | False | 200 | RECORDED |
| 68 | B30 | Who's working tomorrow? | review |  | True | 200 | RECORDED |
| 69 | B31 | Who's driving tomorrow? | review |  | True | 200 | RECORDED |
| 70 | B32 | How many orders did Copperfield have today vs Tomball? | deterministic | orders.catering_today | False | 200 | RECORDED |
| 71 | B33 | (From a STAFF session if available) What catering orders do we have to | deterministic | orders.catering_today | False | 200 | RECORDED |
| 72 | D1 | Who's working today? | deterministic | schedule.store_today | False | 200 | RECORDED |
| 73 | D2 | Show me this week's schedule | deterministic | schedule.store_week | False | 200 | RECORDED |
| 74 | D3 | Any open shifts right now? | deterministic | schedule.open_shifts | False | 200 | RECORDED |
| 75 | D4 | Any pending time off requests? | deterministic | schedule.time_off_pending | False | 200 | RECORDED |
| 76 | D5 | Who's unavailable this week? | review |  | True | 200 | RECORDED |
| 77 | D6 | Any availability conflicts coming up? | deterministic | schedule.availability_conflicts | False | 200 | RECORDED |
| 78 | D7 | Any shift swaps pending? | deterministic | schedule.shift_swap_summary | False | 200 | RECORDED |
| 79 | D8 | Any shift offers out? | deterministic | schedule.shift_offer_summary | False | 200 | RECORDED |
| 80 | D9 | Who hasn't accepted their shifts? | deterministic | schedule.shift_acceptance_summary | False | 200 | RECORDED |
| 81 | D10 | Any shift alarms pending? | deterministic | schedule.alarm_pending_summary | False | 200 | RECORDED |
| 82 | D11 | Show me the schedule | deterministic | schedule.view | False | 200 | RECORDED |
| 83 | D12 | Tomorrow's orders | deterministic | orders.catering_tomorrow | False | 200 | RECORDED |
| 84 | D13 | Tomorrow's schedule | deterministic | schedule.view | False | 200 | RECORDED |
| 85 | D14 | Who works tomorrow? | review |  | True | 200 | RECORDED |
| 86 | D15 | What caterings tomorrow? | deterministic | orders.catering_tomorrow | False | 200 | RECORDED |
| 87 | D16 | Who's driving tomorrow? | review |  | True | 200 | RECORDED |
| 88 | D17 | Who's working tomorrow vs who's driving tomorrow - ask both back to ba | review |  | True | 200 | RECORDED |
| 89 | D18 | Create a shift for [employee] on Friday 4 to close | review |  | True | 200 | RECORDED |
| 90 | D19 | Approve [employee]'s time off | review |  | True | 200 | RECORDED |
| 91 | D20 | Delete the Monday shift for [employee] | review |  | True | 200 | RECORDED |
| 92 | D21 | Publish next week's schedule | review |  | True | 200 | RECORDED |
| 93 | R1 | How were sales today? | deterministic | toast.sales_summary | False | 200 | RECORDED |
| 94 | R2 | What tables are open? | deterministic | toast.table_activity | False | 200 | RECORDED |
| 95 | R3 | Give me a labor summary |  |  | None |  | MISSING_MIRROR |
| 96 | R4 | What caterings are tomorrow? |  |  | None |  | MISSING_MIRROR |
| 97 | R5 | Which orders need a driver? |  |  | None |  | MISSING_MIRROR |
| 98 | R6 | Any orders missing tracking? |  |  | None |  | MISSING_MIRROR |
| 99 | R7 | How many tools do you have? |  |  | None |  | MISSING_MIRROR |
| 100 | R8 | Run a shell command |  |  | None |  | MISSING_MIRROR |
| 101 | R9 | What's our P&L? |  |  | None |  | MISSING_MIRROR |
| 102 | R10 | blorple snurf catering xyzzy |  |  | None |  | MISSING_MIRROR |
