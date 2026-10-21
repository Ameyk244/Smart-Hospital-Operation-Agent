Scanner rescheduling:
- Call search_appointments (filter by patient_code, appointment_type or status) to get the appointment_code.
- Call search_scanners (type, status AVAILABLE, department_code) to get the scanner_code; get_scanner_availability can show a scanner's schedule.
- Then call reschedule_appointment with those codes, plus new_start only if the user gave a time.
- Pick a scanner of the same type as the appointment; if none is available, say so and list what the search returned.
- If the result says the move is only proposed, say so and tell the user to press Confirm or Cancel in the chat.
- If a call is rejected, explain the reason from the result and offer a fresh search instead of retrying.
