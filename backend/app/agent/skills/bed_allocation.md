Bed allocation:
- Call search_queue (status WAITING) to get the patient_code, then search_beds (status AVAILABLE, optionally a ward_code) to get the bed_code.
- Admit with assign_bed only using those two codes; the patient must be WAITING with no bed, the bed AVAILABLE.
- Release with release_bed, and mark_bed_clean once a released bed is in CLEANING, using a bed_code from search_beds.
- If the user did not name a patient or bed, show the most urgent waiting patients and the available beds and ask which pair they want; never pick for them.
- If the result says the change is only proposed, say so and tell the user to press Confirm or Cancel in the chat.
- If a call is rejected, state the reason from the result in one sentence and offer a fresh search; do not retry the same codes.
