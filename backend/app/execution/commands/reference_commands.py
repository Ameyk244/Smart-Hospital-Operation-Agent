"""Read-only commands over reference data (departments, scanners).

Why it exists: backs both the deterministic "list departments"/"list
scanners" grammar and the agent's `execute_command`/
`get_scanner_availability` tools — one execution function per operation,
per docs/ARCHITECTURE.md §2.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.catalog_repository import CatalogRepository
from app.db.repositories.scanner_repository import ScannerRepository
from app.execution.commands.base import CommandError, register
from app.execution.commands.coercion import coerce_scanner_status, coerce_scanner_type
from app.schemas.hospital import DepartmentOut, ScannerOut


@register("list_departments")
async def list_departments(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    departments = await CatalogRepository(session).list_departments()
    return [DepartmentOut.model_validate(d).model_dump(mode="json") for d in departments]


@register("list_scanners")
async def list_scanners(session: AsyncSession, args: dict[str, Any]) -> list[dict]:
    scanner_type = coerce_scanner_type(args.get("type"))
    status = coerce_scanner_status(args.get("status"))
    department_code = args.get("department_code")

    scanners = await ScannerRepository(session).search(
        scanner_type=scanner_type, status=status, department_code=department_code
    )
    return [ScannerOut.model_validate(s).model_dump(mode="json") for s in scanners]


@register("get_scanner_availability")
async def get_scanner_availability(session: AsyncSession, args: dict[str, Any]) -> dict:
    code = args.get("scanner_code")
    if not code:
        raise CommandError("scanner_code is required", category="invalid_argument")
    scanner = await ScannerRepository(session).get_by_code(code)
    if scanner is None:
        raise ValueError(f"No scanner with code {code!r}")
    return ScannerOut.model_validate(scanner).model_dump(mode="json")
