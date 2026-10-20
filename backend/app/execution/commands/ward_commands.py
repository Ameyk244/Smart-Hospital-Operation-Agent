"""Ward and bed commands: two reads and the bed writes.

Why it exists: backs "list wards" / "list beds" in the deterministic grammar,
the agent's `search_beds`, and its `assign_bed` / `release_bed` tools, which
build the same `Command` from grounded arguments (see
`app/agent/tools/action_tools.py`).

Business rules enforced here, not by any caller:
- `assign_bed`: the bed must be AVAILABLE, the patient must not already hold a
  bed, and must have a WAITING queue entry. The bed becomes OCCUPIED and the
  entry ADMITTED.
- `release_bed`: the bed must be OCCUPIED. It becomes CLEANING and the
  patient's ADMITTED entry becomes DISCHARGED.
A violation raises `CommandError`; unknown codes raise `ValueError`.

Both writes are audited in the same transaction. Before/after carry only
codes, statuses and timestamps, never names.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models.hospital import Bed, BedStatus, QueueEntry
from app.db.repositories.audit_repository import AuditRepository
from app.db.repositories.patient_repository import PatientRepository
from app.db.repositories.queue_repository import QueueRepository
from app.db.repositories.ward_repository import BedRepository, WardRepository
from app.execution.commands.base import CommandError, register
from app.execution.commands.coercion import coerce_bed_status
from app.schemas.hospital import BedOut, WardOut


@register("list_wards")
async def list_wards(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    wards = await WardRepository(session).list_wards()
    return [WardOut.model_validate(w).model_dump(mode="json") for w in wards]


@register("list_beds")
async def list_beds(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    beds = await BedRepository(session).search(
        status=coerce_bed_status(args.get("status")), ward_code=args.get("ward_code")
    )
    return [BedOut.model_validate(b).model_dump(mode="json") for b in beds]


def _audit_state(bed: Bed, entry: QueueEntry) -> dict[str, Any]:
    return {
        "ward_code": bed.ward.code,
        "bed_status": bed.status.value,
        "patient_code": bed.patient.code if bed.patient else None,
        "occupied_since": bed.occupied_since.isoformat() if bed.occupied_since else None,
        "queue_entry_code": entry.code,
        "queue_status": entry.status.value,
    }


@register("assign_bed", audited=True)
async def assign_bed(session: AsyncSession, args: dict[str, Any], *, actor: str) -> dict:
    patient_code = args.get("patient_code")
    bed_code = args.get("bed_code")
    if not patient_code or not bed_code:
        raise CommandError("patient_code and bed_code are both required", category="invalid_argument")

    patient = await PatientRepository(session).get_by_code(patient_code)
    if patient is None:
        raise ValueError(f"No patient with code {patient_code!r}")
    bed_repo = BedRepository(session)
    bed = await bed_repo.get_by_code(bed_code, for_update=True)
    if bed is None:
        raise ValueError(f"No bed with code {bed_code!r}")

    if bed.status != BedStatus.AVAILABLE:
        raise CommandError(
            f"Bed {bed_code} is not available (status: {bed.status.value})",
            category="bed_unavailable",
        )
    if await bed_repo.occupied_by(patient) is not None:
        raise CommandError(f"Patient {patient_code} already has a bed", category="already_bedded")
    entry = await QueueRepository(session).get_waiting(patient, for_update=True)
    if entry is None:
        raise CommandError(
            f"Patient {patient_code} has no waiting queue entry", category="no_waiting_entry"
        )

    before = _audit_state(bed, entry)
    await bed_repo.admit(bed, patient, entry, occupied_since=utcnow())
    await AuditRepository(session).record(
        actor=actor,
        action="assign_bed",
        entity_type="bed",
        entity_code=bed_code,
        before=before,
        after=_audit_state(bed, entry),
    )
    return BedOut.model_validate(bed).model_dump(mode="json")


@register("release_bed", audited=True)
async def release_bed(session: AsyncSession, args: dict[str, Any], *, actor: str) -> dict:
    bed_code = args.get("bed_code")
    if not bed_code:
        raise CommandError("bed_code is required", category="invalid_argument")

    bed_repo = BedRepository(session)
    bed = await bed_repo.get_by_code(bed_code, for_update=True)
    if bed is None:
        raise ValueError(f"No bed with code {bed_code!r}")
    if bed.status != BedStatus.OCCUPIED:
        raise CommandError(
            f"Bed {bed_code} is not occupied (status: {bed.status.value})",
            category="bed_not_occupied",
        )

    entry = await QueueRepository(session).get_admitted_in(bed)
    if entry is None:
        raise CommandError(
            f"Bed {bed_code} has no admitted queue entry", category="bed_not_occupied"
        )
    before = _audit_state(bed, entry)
    await bed_repo.release(bed, entry, discharged_at=utcnow())
    await AuditRepository(session).record(
        actor=actor,
        action="release_bed",
        entity_type="bed",
        entity_code=bed_code,
        before=before,
        after=_audit_state(bed, entry),
    )
    return BedOut.model_validate(bed).model_dump(mode="json")
