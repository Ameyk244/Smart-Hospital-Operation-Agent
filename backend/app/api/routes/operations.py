"""Read-only hospital-operations endpoints for the frontend's main
operations view (departments, scanners, appointments, patients).

Why it exists: the UI needs to browse hospital data independently of the
chat/command flow — this is a third caller of the exact same `Command`
vocabulary the deterministic parser and the agent's tools use (see
docs/ARCHITECTURE.md §2), not a fourth reimplementation. Every route here
is a thin `CommandRunner.execute(Command(...))` call.

What calls it: the frontend's operations view.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.execution.commands import Command, CommandRunner

router = APIRouter(prefix="/api/operations", tags=["operations"])


async def _run(db: AsyncSession, name: str, args: dict) -> object:
    result = await CommandRunner(db).execute(Command(name, args))
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)
    return result.data


@router.get("/departments")
async def list_departments(db: AsyncSession = Depends(get_db)) -> object:
    return await _run(db, "list_departments", {})


@router.get("/scanners")
async def list_scanners(
    type: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> object:
    return await _run(db, "list_scanners", {"type": type, "status": status})


@router.get("/appointments")
async def search_appointments(
    status: str | None = None,
    appointment_type: str | None = None,
    patient_code: str | None = None,
    department_code: str | None = None,
    scanner_code: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> object:
    return await _run(
        db,
        "search_appointments",
        {
            "status": status,
            "appointment_type": appointment_type,
            "patient_code": patient_code,
            "department_code": department_code,
            "scanner_code": scanner_code,
            "limit": limit,
        },
    )


@router.get("/patients")
async def search_patients(query: str, db: AsyncSession = Depends(get_db)) -> object:
    return await _run(db, "search_patients", {"query": query})
