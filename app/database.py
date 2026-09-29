import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from .schemas import AgentRun, ApprovalRequest, ApprovalStatus, Ticket, TicketStatus


class TicketRepository:
    def __init__(self, database_url: str) -> None:
        self.database_path = self._sqlite_path(database_url)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _sqlite_path(database_url: str) -> Path:
        prefix = "sqlite:///"
        if not database_url.startswith(prefix):
            raise ValueError("Only sqlite database URLs are supported initially")
        return Path(database_url.removeprefix(prefix)).resolve()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    external_id TEXT UNIQUE,
                    trace_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS guardrail_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    guardrail_name TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rate_limit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    output TEXT,
                    guardrail_flags TEXT NOT NULL,
                    approved_by TEXT,
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    args TEXT NOT NULL,
                    result TEXT,
                    latency_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_requests (
                    id TEXT PRIMARY KEY,
                    agent_run_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    proposed_action TEXT NOT NULL,
                    risk_reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reviewer_id TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS escalation_queue (
                    id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    agent_run_id TEXT,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    dead_letter TEXT,
                    payload TEXT
                );
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    plan TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS known_issues (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS security_events (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refunds (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    amount REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT OR IGNORE INTO customers (id, plan) VALUES (?, ?)",
                [("customer-123", "pro"), ("customer-456", "starter")],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO orders (id, customer_id, amount, status) VALUES (?, ?, ?, ?)",
                [("order-100", "customer-123", 29.0, "paid"), ("order-101", "customer-456", 79.0, "paid")],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO subscriptions (id, customer_id, plan, status) VALUES (?, ?, ?, ?)",
                [("sub-100", "customer-123", "pro", "active"), ("sub-101", "customer-456", "starter", "active")],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO invoices (id, customer_id, amount, status) VALUES (?, ?, ?, ?)",
                [("inv-100", "customer-123", 29.0, "open"), ("inv-101", "customer-456", 79.0, "paid")],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO known_issues (id, title, status) VALUES (?, ?, ?)",
                [("issue-1", "Invoice page timeout", "investigating")],
            )
            connection.execute(
                "INSERT OR IGNORE INTO security_events (id, customer_id, event_type, status) VALUES (?, ?, ?, ?)",
                ("security-100", "customer-123", "new-login", "reviewed"),
            )

            approval_columns = {row["name"] for row in connection.execute("PRAGMA table_info('approval_requests')").fetchall()}
            for column_name, column_type in {
                "customer_id": "TEXT",
                "action": "TEXT",
                "created_at": "TEXT",
                "expires_at": "TEXT",
            }.items():
                if column_name not in approval_columns:
                    connection.execute(f"ALTER TABLE approval_requests ADD COLUMN {column_name} {column_type}")

            rate_columns = {row["name"] for row in connection.execute("PRAGMA table_info('rate_limit_events')").fetchall()}
            for column_name, column_type in {
                "customer_id": "TEXT",
                "action": "TEXT",
                "created_at": "TEXT",
            }.items():
                if column_name not in rate_columns:
                    connection.execute(f"ALTER TABLE rate_limit_events ADD COLUMN {column_name} {column_type}")

    def save_ticket(self, ticket: Ticket) -> Ticket:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tickets
                    (id, customer_id, channel, subject, body, external_id,
                     trace_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(ticket.id),
                    ticket.customer_id,
                    ticket.channel.value,
                    ticket.subject,
                    ticket.body,
                    ticket.external_id,
                    str(ticket.trace_id),
                    ticket.status.value,
                    ticket.created_at.isoformat(),
                ),
            )
        return ticket

    def update_ticket_status(self, ticket_id: UUID, status: TicketStatus) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE tickets SET status = ? WHERE id = ?", (status.value, str(ticket_id))
            )
        return result.rowcount == 1

    def get_ticket(self, ticket_id: UUID) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (str(ticket_id),)
            ).fetchone()
        return dict(row) if row else None

    def get_ticket_by_external_id(self, external_id: str) -> Ticket | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE external_id = ?", (external_id,)
            ).fetchone()
        if not row:
            return None
        return Ticket.model_validate(
            {**dict(row), "id": row["id"], "trace_id": row["trace_id"]}
        )

    def save_agent_run(self, run: AgentRun) -> AgentRun:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_runs
                (id, ticket_id, agent_name, trace_id, output, guardrail_flags, approved_by, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(run.id), str(run.ticket_id), run.agent_name, str(run.trace_id),
                 json.dumps(run.output), json.dumps(run.guardrail_flags), run.approved_by,
                 run.schema_version),
            )
            connection.executemany(
                "INSERT INTO tool_calls (agent_run_id, name, args, result, latency_ms) VALUES (?, ?, ?, ?, ?)",
                [(str(run.id), call.name, json.dumps(call.args), json.dumps(call.result), call.latency_ms)
                 for call in run.tool_calls],
            )
        return run

    def record_guardrail_event(self, ticket_id: UUID, guardrail_name: str, decision: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO guardrail_events (ticket_id, guardrail_name, decision, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(ticket_id), guardrail_name, decision, reason, datetime.now(UTC).isoformat()),
            )

    def save_approval_request(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO approval_requests
                (id, agent_run_id, customer_id, action, proposed_action, risk_reason, status, reviewer_id, created_at, expires_at, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(request.id), str(request.agent_run_id), request.customer_id, request.action,
                 request.proposed_action, request.risk_reason, request.status.value, request.reviewer_id,
                 request.created_at.isoformat(), request.expires_at.isoformat(), request.schema_version),
            )
        ticket_row = connection.execute("SELECT ticket_id FROM agent_runs WHERE id = ?", (str(request.agent_run_id),)).fetchone()
        if ticket_row:
            self.record_audit_event(UUID(ticket_row["ticket_id"]), "approval_requested", {
                "approval_id": str(request.id),
                "action": request.action,
                "status": request.status.value,
                "risk_reason": request.risk_reason,
            })
        return request

    def save_escalation(self, ticket_id: UUID, agent_run_id: UUID | None, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from uuid import uuid4
        escalation_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO escalation_queue (id, ticket_id, agent_run_id, reason, status, created_at, updated_at, dead_letter, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (escalation_id, str(ticket_id), str(agent_run_id) if agent_run_id else None, reason, "pending", now, now, None, json.dumps(payload or {})),
            )
        self.record_audit_event(ticket_id, "escalation_queued", {"escalation_id": escalation_id, "reason": reason})
        return {"id": escalation_id, "status": "pending", "reason": reason}

    def record_escalation(self, ticket_id: UUID, agent_run_id: UUID | None, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.save_escalation(ticket_id, agent_run_id, reason, payload)

    def get_escalation_queue(self, ticket_id: UUID | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if ticket_id is None:
                rows = connection.execute("SELECT * FROM escalation_queue ORDER BY created_at DESC").fetchall()
            else:
                rows = connection.execute("SELECT * FROM escalation_queue WHERE ticket_id = ? ORDER BY created_at DESC", (str(ticket_id),)).fetchall()
        return [dict(row) for row in rows]

    def record_dead_letter(self, ticket_id: UUID, reason: str) -> dict[str, Any]:
        from uuid import uuid4
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM escalation_queue WHERE ticket_id = ? AND dead_letter IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                (str(ticket_id),),
            ).fetchone()
            if existing:
                return dict(existing)
            escalation_id = str(uuid4())
            connection.execute(
                "INSERT INTO escalation_queue (id, ticket_id, agent_run_id, reason, status, created_at, updated_at, dead_letter, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (escalation_id, str(ticket_id), None, reason, "dead_letter", now, now, reason, json.dumps({"reason": reason, "kind": "dead_letter"})),
            )
        self.record_audit_event(ticket_id, "dead_letter", {"reason": reason, "escalation_id": escalation_id})
        return {"id": escalation_id, "status": "dead_letter", "reason": reason}

    def get_dead_letter(self, ticket_id: UUID) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM escalation_queue WHERE ticket_id = ? AND status = 'dead_letter' ORDER BY created_at DESC LIMIT 1",
                (str(ticket_id),),
            ).fetchone()
        return dict(row) if row else None

    def retry_ticket(self, ticket_id: UUID, reason: str | None = None) -> dict[str, Any]:
        self.update_ticket_status(ticket_id, TicketStatus.IN_PROGRESS)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE escalation_queue SET status = 'retried', updated_at = ?, dead_letter = ?, payload = COALESCE(payload, '{}') WHERE ticket_id = ? AND status = 'pending'",
                (now, reason or "retry_scheduled", str(ticket_id)),
            )
            retry_row = connection.execute(
                "SELECT * FROM escalation_queue WHERE ticket_id = ? ORDER BY created_at DESC LIMIT 1",
                (str(ticket_id),),
            ).fetchone()
        self.record_audit_event(ticket_id, "retry_scheduled", {"reason": reason or "retry_scheduled"})
        return dict(retry_row) if retry_row else {"status": "in_progress"}

    def get_approval_request(self, request_id: UUID) -> ApprovalRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (str(request_id),)
            ).fetchone()
        if not row:
            return None
        return ApprovalRequest.model_validate({**dict(row), "id": row["id"], "agent_run_id": row["agent_run_id"]})

    def get_approval_request_obj(self, request_id: str | UUID) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (str(request_id),)
            ).fetchone()
        return dict(row) if row else None

    def get_approval_requests_by_status(self, status: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM approval_requests WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        return [dict(row) for row in rows]

    def is_approval_expired_by_id(self, request_id: str | UUID) -> bool:
        request = self.get_approval_request_obj(request_id)
        if request is None:
            return True
        expires_at = datetime.fromisoformat(request["expires_at"])
        created_at = datetime.fromisoformat(request["created_at"])
        return datetime.now(UTC) > expires_at or datetime.now(UTC) > created_at + timedelta(days=1)

    def update_approval_request_status(self, request_id: str | UUID, status: ApprovalStatus, reviewer_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "UPDATE approval_requests SET status = ?, reviewer_id = ? WHERE id = ?",
                (status.value, reviewer_id, str(request_id)),
            )
        row = connection.execute("SELECT * FROM approval_requests WHERE id = ?", (str(request_id),)).fetchone()
        return dict(row)

    def update_approval_request(
        self, request_id: UUID, status: ApprovalStatus, reviewer_id: str
    ) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE approval_requests SET status = ?, reviewer_id = ? WHERE id = ?",
                (status.value, reviewer_id, str(request_id)),
            )
        if result.rowcount == 1:
            request = self.get_approval_request(request_id)
            if request:
                ticket_row = connection.execute("SELECT ticket_id FROM agent_runs WHERE id = ?", (str(request.agent_run_id),)).fetchone()
                if ticket_row:
                    self.record_audit_event(UUID(ticket_row["ticket_id"]), "approval_updated", {
                        "approval_id": str(request_id),
                        "status": status.value,
                        "reviewer_id": reviewer_id,
                    })
        return result.rowcount == 1

    def record_audit_event(self, ticket_id: UUID, event_type: str, details: dict[str, object]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_events (ticket_id, event_type, details, created_at) VALUES (?, ?, ?, ?)",
                (str(ticket_id), event_type, json.dumps(details), datetime.now(UTC).isoformat()),
            )

    def get_audit_events(self, ticket_id: UUID) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE ticket_id = ? ORDER BY id", (str(ticket_id),)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_rate_limit_event(self, customer_id: str, action: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO rate_limit_events (customer_id, action, created_at) VALUES (?, ?, ?)",
                (customer_id, action, datetime.now(UTC).isoformat()),
            )

    def get_guardrail_events(self, ticket_id: UUID) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM guardrail_events WHERE ticket_id = ? ORDER BY id", (str(ticket_id),)
            ).fetchall()
        return [dict(row) for row in rows]
