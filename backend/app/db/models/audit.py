"""The audit log for hospital-data mutations (observability Phase 6).

Why it exists: observability logs and the agent trace are operational. They
answer "is the system healthy, and why was this request slow", they're
redacted, and they're allowed to be sampled, dropped, or rotated. An audit
record answers a different question: "who changed this hospital record,
when, and from what to what". It must be complete and must not be edited
after the fact. So it is its own table:

- **Append-only, enforced by the database.** A trigger
  (`AUDIT_LOG_IMMUTABLE_DDL`) rejects every UPDATE and DELETE. It's
  installed both by the Alembic migration and, for `create_all` (the test
  database), by the DDL listener below.
- **Written in the same transaction as the change.** `reassign_scanner`
  writes it before `CommandRunner` commits, so there is never a change
  without its record or a record without its change.
- **No foreign key to `agent_sessions`.** An audit record outlives the
  session that caused it.
- **No patient data.** Before/after hold only the fields the mutation
  changes: scanner code, start/end times, status.

"Who" is the agent session id (`actor`). This app has no user
authentication, so the session is the most specific actor that exists;
`request_id`/`trace_id` tie the record to its logs and trace.

What calls it: `app/db/repositories/audit_repository.py`, from
`app/execution/commands/appointment_commands.py` (`reassign_scanner`).
"""

from datetime import datetime

from sqlalchemy import DDL, JSON, DateTime, Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class AuditRecord(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    before_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    after_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


# One SQL definition, shared by the migration and the create_all listener.
# Separate statements (asyncpg runs one per call), and no `%` (SQLAlchemy's
# DDL treats it as a format character).
AUDIT_LOG_IMMUTABLE_DDL = (
    """
    CREATE OR REPLACE FUNCTION audit_log_reject_change() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION USING MESSAGE = 'audit_log is append-only: ' || TG_OP || ' is not allowed';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER audit_log_immutable
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change()
    """,
)

AUDIT_LOG_IMMUTABLE_DROP_DDL = (
    "DROP TRIGGER IF EXISTS audit_log_immutable ON audit_log",
    "DROP FUNCTION IF EXISTS audit_log_reject_change()",
)

for _statement in AUDIT_LOG_IMMUTABLE_DDL:
    event.listen(
        AuditRecord.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="postgresql"),
    )
