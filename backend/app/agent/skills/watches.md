Watches and alerts:
- A watch is a read-only monitor: it checks a condition every few minutes and raises an alert; it never changes beds, the queue or appointments.
- Conditions: QUEUE_WAIT_OVER (minutes), FREE_BEDS_UNDER (available beds, optionally for one ward), DELAYED_APPOINTMENTS_OVER (count).
- To create one, call create_watch with the condition and an integer threshold of at least 1; confirm what it will watch for.
- To stop one, call list_watches first, then stop_watch with the task code it returned.
- To acknowledge an alert, call list_alerts first, then acknowledge_alert with an id it returned.
- If a tool says the scheduler is off, tell the user watches are unavailable; do not retry.
