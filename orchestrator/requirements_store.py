"""Persistent requirements catalog and lifecycle state for Governance."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

import yaml
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class AuthoringStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    ARCHIVED = "archived"


class ExecutionStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    IMPLEMENTED = "implemented"
    STOPPED = "stopped"


class AnalysisStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    READY = "ready"
    FAILED = "failed"


class RequirementNotFound(LookupError):
    """Raised when a public requirement ID does not exist."""


class RequirementConflict(RuntimeError):
    """Raised when a lifecycle or execution transition is not currently legal."""


@dataclass(frozen=True)
class RequirementRecord:
    id: int
    requirement_type: str
    title: str
    intent: str
    acceptance: list[str]
    constraints: list[str]
    possible_interpretations: list[str]
    authoring_status: AuthoringStatus
    execution_status: ExecutionStatus
    analysis_status: AnalysisStatus
    analysis: dict[str, Any] | None
    analysis_error: str | None
    current_run_id: str | None
    created_at: datetime
    updated_at: datetime
    analyzed_at: datetime | None
    implemented_at: datetime | None
    revision: int

    @property
    def requirement_id(self) -> str:
        return f"REQ-{self.id:03d}"

    def to_requirement_dict(self) -> dict[str, Any]:
        return {
            "id": self.requirement_id,
            "type": self.requirement_type,
            "title": self.title,
            "intent": self.intent,
            "acceptance": list(self.acceptance),
            "constraints": list(self.constraints),
            "possible_interpretations": list(self.possible_interpretations),
            "status": self.authoring_status.value,
        }


class RequirementsRepositoryProtocol(Protocol):
    """Typed persistence boundary shared by UI, planning, and execution."""

    def create(
        self,
        *,
        requirement_type: str,
        title: str,
        intent: str,
        acceptance: list[str] | None = None,
        constraints: list[str] | None = None,
        possible_interpretations: list[str] | None = None,
    ) -> RequirementRecord: ...

    def list_requirements(self) -> list[RequirementRecord]: ...

    def get(self, requirement_id: str) -> RequirementRecord: ...

    def transition_authoring(
        self,
        requirement_id: str,
        target: AuthoringStatus,
    ) -> RequirementRecord: ...

    def record_analysis(
        self,
        requirement_id: str,
        *,
        analysis: dict[str, Any] | None = None,
        error: str | None = None,
        expected_revision: int | None = None,
        expected_run_id: str | None = None,
    ) -> RequirementRecord: ...

    def mark_run_started(
        self,
        requirement_id: str,
        run_id: str,
        *,
        force: bool = False,
    ) -> RequirementRecord: ...

    def mark_replan_started(
        self,
        requirement_id: str,
        run_id: str,
    ) -> RequirementRecord: ...

    def sync_execution(
        self,
        requirement_id: str,
        run_id: str,
        status: ExecutionStatus,
    ) -> RequirementRecord: ...


class Base(DeclarativeBase):
    pass


class RequirementRow(Base):
    __tablename__ = "requirements"
    __table_args__ = (
        CheckConstraint(
            "authoring_status IN ('draft', 'ready', 'archived')",
            name="ck_requirements_authoring_status",
        ),
        CheckConstraint(
            "execution_status IN "
            "('not_started', 'in_progress', 'awaiting_approval', 'implemented', 'stopped')",
            name="ck_requirements_execution_status",
        ),
        CheckConstraint(
            "analysis_status IN ('not_requested', 'ready', 'failed')",
            name="ck_requirements_analysis_status",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requirement_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    constraints: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    possible_interpretations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    authoring_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AuthoringStatus.DRAFT.value
    )
    execution_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ExecutionStatus.NOT_STARTED.value
    )
    analysis_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=AnalysisStatus.NOT_REQUESTED.value
    )
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    analysis_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    implemented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


_INITIALIZE_LOCK = Lock()
_REQUIREMENT_ID = re.compile(r"^REQ-(\d+)$")
_BUSY_EXECUTION = {ExecutionStatus.IN_PROGRESS, ExecutionStatus.AWAITING_APPROVAL}
_VALID_REQUIREMENT_TYPES = {"greenfield", "brownfield", "ambiguous"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _clean_items(items: list[str] | None) -> list[str]:
    return [str(item).strip() for item in (items or []) if str(item).strip()]


class RequirementsRepository:
    """Owns the canonical SQLite-backed requirements catalog."""

    def __init__(self, workspace_dir: str | Path, *, seed_dir: str | Path | None = None):
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace_dir / "governance.db"
        self.engine = create_engine(
            f"sqlite:///{self.db_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(self.engine, "connect", self._configure_connection)
        with _INITIALIZE_LOCK:
            Base.metadata.create_all(self.engine)
            self._ensure_revision_column()
            if seed_dir is not None:
                self._seed(Path(seed_dir))

    def _ensure_revision_column(self) -> None:
        """Upgrade databases created by the pre-revision prototype in place."""
        columns = {column["name"] for column in inspect(self.engine).get_columns("requirements")}
        if "revision" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE requirements "
                        "ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                    )
                )

    @staticmethod
    def _configure_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @staticmethod
    def _numeric_id(requirement_id: str) -> int:
        match = _REQUIREMENT_ID.fullmatch(requirement_id)
        if not match:
            raise RequirementNotFound(requirement_id)
        return int(match.group(1))

    @staticmethod
    def _record(row: RequirementRow) -> RequirementRecord:
        return RequirementRecord(
            id=row.id,
            requirement_type=row.requirement_type,
            title=row.title,
            intent=row.intent,
            acceptance=list(row.acceptance or []),
            constraints=list(row.constraints or []),
            possible_interpretations=list(row.possible_interpretations or []),
            authoring_status=AuthoringStatus(row.authoring_status),
            execution_status=ExecutionStatus(row.execution_status),
            analysis_status=AnalysisStatus(row.analysis_status),
            analysis=dict(row.analysis) if row.analysis is not None else None,
            analysis_error=row.analysis_error,
            current_run_id=row.current_run_id,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            analyzed_at=_as_utc(row.analyzed_at),
            implemented_at=_as_utc(row.implemented_at),
            revision=row.revision,
        )

    def _get_row(self, session: Session, requirement_id: str) -> RequirementRow:
        row = session.get(RequirementRow, self._numeric_id(requirement_id))
        if row is None:
            raise RequirementNotFound(requirement_id)
        return row

    def _seed(self, seed_dir: Path) -> None:
        now = _utcnow()
        with Session(self.engine) as session, session.begin():
            for path in sorted(seed_dir.glob("REQ-*.yaml")):
                requirement = yaml.safe_load(path.read_text()) or {}
                requirement_id = str(requirement.get("id", ""))
                try:
                    numeric_id = self._numeric_id(requirement_id)
                except RequirementNotFound:
                    continue
                if requirement_id == "REQ-001":
                    authoring = AuthoringStatus.READY
                    execution = ExecutionStatus.IMPLEMENTED
                    implemented_at = now
                elif requirement_id == "REQ-002":
                    authoring = AuthoringStatus.READY
                    execution = ExecutionStatus.NOT_STARTED
                    implemented_at = None
                elif requirement_id == "REQ-003":
                    authoring = AuthoringStatus.DRAFT
                    execution = ExecutionStatus.NOT_STARTED
                    implemented_at = None
                else:
                    authoring = AuthoringStatus.DRAFT
                    execution = ExecutionStatus.NOT_STARTED
                    implemented_at = None
                statement = sqlite_insert(RequirementRow).values(
                    id=numeric_id,
                    requirement_type=requirement.get("type", "greenfield"),
                    title=requirement.get("title") or requirement_id,
                    intent=requirement.get("intent") or "",
                    acceptance=_clean_items(requirement.get("acceptance")),
                    constraints=_clean_items(requirement.get("constraints")),
                    possible_interpretations=_clean_items(
                        requirement.get("possible_interpretations")
                    ),
                    authoring_status=authoring.value,
                    execution_status=execution.value,
                    analysis_status=AnalysisStatus.NOT_REQUESTED.value,
                    created_at=now,
                    updated_at=now,
                    implemented_at=implemented_at,
                ).on_conflict_do_nothing(index_elements=["id"])
                session.execute(statement)

    def list_requirements(self) -> list[RequirementRecord]:
        with Session(self.engine) as session:
            rows = session.query(RequirementRow).order_by(RequirementRow.id).all()
            return [self._record(row) for row in rows]

    def get(self, requirement_id: str) -> RequirementRecord:
        with Session(self.engine) as session:
            return self._record(self._get_row(session, requirement_id))

    def create(
        self,
        *,
        requirement_type: str,
        title: str,
        intent: str,
        acceptance: list[str] | None = None,
        constraints: list[str] | None = None,
        possible_interpretations: list[str] | None = None,
    ) -> RequirementRecord:
        requirement_type = requirement_type.strip().lower()
        title = title.strip()
        intent = intent.strip()
        if requirement_type not in _VALID_REQUIREMENT_TYPES:
            raise ValueError("Unsupported requirement type")
        if not title or not intent:
            raise ValueError("Title and intent are required")
        now = _utcnow()
        with Session(self.engine) as session, session.begin():
            row = RequirementRow(
                requirement_type=requirement_type,
                title=title,
                intent=intent,
                acceptance=_clean_items(acceptance),
                constraints=_clean_items(constraints),
                possible_interpretations=_clean_items(possible_interpretations),
                authoring_status=AuthoringStatus.DRAFT.value,
                execution_status=ExecutionStatus.NOT_STARTED.value,
                analysis_status=AnalysisStatus.NOT_REQUESTED.value,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            record = self._record(row)
        return record

    def transition_authoring(
        self,
        requirement_id: str,
        target: AuthoringStatus,
    ) -> RequirementRecord:
        target = AuthoringStatus(target)
        with Session(self.engine) as session, session.begin():
            row = self._get_row(session, requirement_id)
            current = AuthoringStatus(row.authoring_status)
            execution = ExecutionStatus(row.execution_status)
            if execution in _BUSY_EXECUTION:
                raise RequirementConflict("A running requirement cannot change lifecycle status")
            legal = {
                AuthoringStatus.DRAFT: {AuthoringStatus.READY, AuthoringStatus.ARCHIVED},
                AuthoringStatus.READY: {AuthoringStatus.ARCHIVED},
                AuthoringStatus.ARCHIVED: {AuthoringStatus.DRAFT},
            }
            if target not in legal[current]:
                raise RequirementConflict(f"Illegal authoring transition {current} -> {target}")
            row.authoring_status = target.value
            row.updated_at = _utcnow()
            row.revision += 1
            session.flush()
            return self._record(row)

    def record_analysis(
        self,
        requirement_id: str,
        *,
        analysis: dict[str, Any] | None = None,
        error: str | None = None,
        expected_revision: int | None = None,
        expected_run_id: str | None = None,
    ) -> RequirementRecord:
        if (analysis is None) == (error is None):
            raise ValueError("Provide exactly one of analysis or error")
        now = _utcnow()
        with Session(self.engine) as session, session.begin():
            row = self._get_row(session, requirement_id)
            execution = ExecutionStatus(row.execution_status)
            if execution in _BUSY_EXECUTION:
                if (
                    execution != ExecutionStatus.IN_PROGRESS
                    or expected_run_id is None
                    or row.current_run_id != expected_run_id
                ):
                    raise RequirementConflict(
                        "Analysis cannot change while a requirement has an active run"
                    )
            elif expected_run_id is not None:
                raise RequirementConflict("The analysis run is no longer current")
            if expected_revision is not None and row.revision != expected_revision:
                raise RequirementConflict("Requirement changed while analysis was running")
            row.analysis = analysis
            row.analysis_error = error
            row.analysis_status = (
                AnalysisStatus.READY.value if analysis is not None else AnalysisStatus.FAILED.value
            )
            row.analyzed_at = now
            row.updated_at = now
            row.revision += 1
            session.flush()
            return self._record(row)

    def mark_replan_started(
        self,
        requirement_id: str,
        run_id: str,
    ) -> RequirementRecord:
        with Session(self.engine) as session, session.begin():
            row = self._get_row(session, requirement_id)
            if AuthoringStatus(row.authoring_status) == AuthoringStatus.ARCHIVED:
                raise RequirementConflict("Archived requirements cannot be re-planned")
            if row.current_run_id != run_id:
                raise RequirementConflict("Run is no longer current for this requirement")
            if ExecutionStatus(row.execution_status) not in _BUSY_EXECUTION:
                raise RequirementConflict("Only an active run can be re-planned")
            row.execution_status = ExecutionStatus.IN_PROGRESS.value
            row.updated_at = _utcnow()
            row.revision += 1
            session.flush()
            return self._record(row)

    def mark_run_started(
        self,
        requirement_id: str,
        run_id: str,
        *,
        force: bool = False,
    ) -> RequirementRecord:
        with Session(self.engine) as session, session.begin():
            row = self._get_row(session, requirement_id)
            authoring = AuthoringStatus(row.authoring_status)
            execution = ExecutionStatus(row.execution_status)
            if authoring == AuthoringStatus.ARCHIVED:
                raise RequirementConflict("Archived requirements cannot run")
            if execution in _BUSY_EXECUTION:
                raise RequirementConflict("Requirement already has an active run")
            normally_runnable = (
                authoring == AuthoringStatus.READY
                and execution in {ExecutionStatus.NOT_STARTED, ExecutionStatus.STOPPED}
            )
            if not force and not normally_runnable:
                raise RequirementConflict("Requirement is not ready to run")
            row.execution_status = ExecutionStatus.IN_PROGRESS.value
            row.current_run_id = run_id
            row.updated_at = _utcnow()
            row.revision += 1
            session.flush()
            return self._record(row)

    def sync_execution(
        self,
        requirement_id: str,
        run_id: str,
        status: ExecutionStatus,
    ) -> RequirementRecord:
        status = ExecutionStatus(status)
        now = _utcnow()
        with Session(self.engine) as session, session.begin():
            row = self._get_row(session, requirement_id)
            if row.current_run_id != run_id:
                raise RequirementConflict("Run is no longer current for this requirement")
            row.execution_status = status.value
            row.updated_at = now
            row.revision += 1
            if status == ExecutionStatus.IMPLEMENTED:
                row.implemented_at = now
            session.flush()
            return self._record(row)
