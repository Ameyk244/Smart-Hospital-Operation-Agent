"""Reproducible synthetic data seeding.

Why it exists: every test and every demo needs a known, non-trivial dataset
— multiple delayed MRI appointments, multiple compatible available scanners,
multiple patients matching a fuzzy name search — so search tools return
ambiguous results and grounding has something real to guard. Hand-typing
that by hand would drift out of sync with the schema; a fixed-seed RNG makes
the dataset regenerate identically every run.

What calls it: `python -m app.seed.seed_data` (manual/dev), and
`tests/integration/conftest.py` (fixture that seeds a test database before
each integration test module).

Fails: wipes and re-seeds idempotently — safe to run against an already-seeded
database (it deletes hospital-domain rows first, in FK-safe order, but never
touches agent_sessions/conversation_messages/preferences/agent_events, which
belong to a different lifecycle).
"""

import asyncio
import random
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.hospital import (
    Appointment,
    AppointmentStatus,
    Department,
    Patient,
    Room,
    Scanner,
    ScannerStatus,
    ScannerType,
    Staff,
)
from app.db.session import async_session_factory, engine

SEED = 20260917  # fixed so every regeneration is identical

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Daniel", "Nancy", "Matthew", "Lisa",
    "Anthony", "Betty", "Priya", "Wei", "Fatima", "Carlos",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
]

DEPARTMENTS = [
    ("DEPT-RAD", "Radiology"),
    ("DEPT-CARD", "Cardiology"),
    ("DEPT-ORTH", "Orthopedics"),
    ("DEPT-ER", "Emergency"),
]

STAFF_ROLES = ["Radiologist", "Technologist", "Nurse", "Physician"]


async def seed(session: AsyncSession) -> dict[str, int]:
    rng = random.Random(SEED)

    # Wipe hospital-domain tables only, in FK-safe order. Agent-domain tables
    # (sessions/conversation/preferences/events) are a separate lifecycle and
    # are never touched here.
    await session.execute(delete(Appointment))
    await session.execute(delete(Scanner))
    await session.execute(delete(Room))
    await session.execute(delete(Staff))
    await session.execute(delete(Patient))
    await session.execute(delete(Department))
    await session.flush()

    departments = [Department(code=code, name=name) for code, name in DEPARTMENTS]
    session.add_all(departments)
    await session.flush()
    dept_by_code = {d.code: d for d in departments}

    staff: list[Staff] = []
    staff_counter = 1
    for dept in departments:
        for _ in range(3):
            role = rng.choice(STAFF_ROLES)
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            staff.append(
                Staff(code=f"STF-{staff_counter}", name=name, role=role, department_id=dept.id)
            )
            staff_counter += 1
    session.add_all(staff)
    await session.flush()

    # Rooms + scanners concentrated in Radiology, since scanners are the
    # entity multi-tool requests revolve around.
    rooms: list[Room] = []
    room_counter = 1
    for dept in departments:
        n_rooms = 4 if dept.code == "DEPT-RAD" else 1
        for _ in range(n_rooms):
            rooms.append(
                Room(code=f"RM-{room_counter}", name=f"Room {room_counter}", department_id=dept.id)
            )
            room_counter += 1
    session.add_all(rooms)
    await session.flush()

    rad_rooms = [r for r in rooms if r.department_id == dept_by_code["DEPT-RAD"].id]

    scanner_plan = [
        (ScannerType.MRI, ScannerStatus.AVAILABLE),
        (ScannerType.MRI, ScannerStatus.AVAILABLE),
        (ScannerType.MRI, ScannerStatus.IN_USE),
        (ScannerType.MRI, ScannerStatus.MAINTENANCE),
        (ScannerType.CT, ScannerStatus.AVAILABLE),
        (ScannerType.CT, ScannerStatus.AVAILABLE),
        (ScannerType.CT, ScannerStatus.IN_USE),
        (ScannerType.XRAY, ScannerStatus.AVAILABLE),
    ]
    scanners: list[Scanner] = []
    for i, (stype, status) in enumerate(scanner_plan, start=1):
        room = rad_rooms[(i - 1) % len(rad_rooms)]
        scanners.append(
            Scanner(
                code=f"SCN-{i}",
                name=f"{stype.value} Scanner {i}",
                type=stype,
                status=status,
                room_id=room.id,
            )
        )
    session.add_all(scanners)
    await session.flush()

    patients: list[Patient] = []
    used_names: set[str] = set()
    n_patients = 30
    while len(patients) < n_patients:
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        name = f"{first} {last}"
        if name in used_names:
            continue
        used_names.add(name)
        idx = len(patients) + 1
        dob = date(1940, 1, 1) + timedelta(days=rng.randint(0, 30000))
        patients.append(
            Patient(
                code=f"PT-{1000 + idx}",
                mrn=f"MRN{100000 + idx}",
                name=name,
                date_of_birth=dob,
            )
        )
    session.add_all(patients)
    await session.flush()

    rad_dept = dept_by_code["DEPT-RAD"]
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    mri_scanners = [s for s in scanners if s.type == ScannerType.MRI]
    ct_scanners = [s for s in scanners if s.type == ScannerType.CT]
    xray_scanners = [s for s in scanners if s.type == ScannerType.XRAY]

    appointments: list[Appointment] = []
    apt_counter = 1

    def add_appointment(
        patient: Patient,
        appt_type: ScannerType,
        scanner: Scanner | None,
        start: datetime,
        status: AppointmentStatus,
    ) -> None:
        nonlocal apt_counter
        staff_member = rng.choice([s for s in staff if s.department_id == rad_dept.id])
        appointments.append(
            Appointment(
                code=f"APT-{2000 + apt_counter}",
                patient_id=patient.id,
                department_id=rad_dept.id,
                scanner_id=scanner.id if scanner else None,
                staff_id=staff_member.id,
                appointment_type=appt_type,
                scheduled_start=start,
                scheduled_end=start + timedelta(minutes=45),
                status=status,
            )
        )
        apt_counter += 1

    # Several DELAYED MRI appointments today, deliberately more than one
    # compatible AVAILABLE MRI scanner exists for — this is the scenario the
    # spec's illustrative "find the first delayed CT/MRI appointment and move
    # it" requests exercise.
    for i in range(4):
        add_appointment(
            patients[i],
            ScannerType.MRI,
            rng.choice(mri_scanners),
            now - timedelta(hours=rng.randint(1, 4)),
            AppointmentStatus.DELAYED,
        )
    for i in range(4, 7):
        add_appointment(
            patients[i],
            ScannerType.CT,
            rng.choice(ct_scanners),
            now - timedelta(hours=rng.randint(1, 3)),
            AppointmentStatus.DELAYED,
        )

    # A mix of upcoming SCHEDULED appointments across types.
    for i in range(7, 20):
        appt_type = rng.choice([ScannerType.MRI, ScannerType.CT, ScannerType.XRAY])
        scanner_pool = {
            ScannerType.MRI: mri_scanners,
            ScannerType.CT: ct_scanners,
            ScannerType.XRAY: xray_scanners,
        }[appt_type]
        add_appointment(
            patients[i],
            appt_type,
            rng.choice(scanner_pool),
            now + timedelta(hours=rng.randint(1, 72)),
            AppointmentStatus.SCHEDULED,
        )

    # A handful of COMPLETED/CANCELLED in the past, for realistic history.
    for i in range(20, 26):
        appt_type = rng.choice([ScannerType.MRI, ScannerType.CT, ScannerType.XRAY])
        scanner_pool = {
            ScannerType.MRI: mri_scanners,
            ScannerType.CT: ct_scanners,
            ScannerType.XRAY: xray_scanners,
        }[appt_type]
        status = rng.choice([AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED])
        add_appointment(
            patients[i],
            appt_type,
            rng.choice(scanner_pool),
            now - timedelta(days=rng.randint(1, 14)),
            status,
        )

    # Give a handful of patients more than one appointment, so "show his
    # appointments" returns a list, not a single row.
    for i in range(3):
        add_appointment(
            patients[i],
            ScannerType.CT,
            rng.choice(ct_scanners),
            now + timedelta(hours=rng.randint(10, 48)),
            AppointmentStatus.SCHEDULED,
        )

    session.add_all(appointments)
    await session.flush()

    return {
        "departments": len(departments),
        "staff": len(staff),
        "rooms": len(rooms),
        "scanners": len(scanners),
        "patients": len(patients),
        "appointments": len(appointments),
    }


async def main() -> None:
    async with async_session_factory() as session:
        async with session.begin():
            counts = await seed(session)
    print("Seeded:", counts)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
