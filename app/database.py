import json
import sqlite3
from datetime import UTC, datetime
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
                    proposed_action TEXT NOT NULL,
                    risk_reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reviewer_id TEXT,
                    schema_version INTEGER NOT NULL
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

    def save_approval_request(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO approval_requests
                (id, agent_run_id, proposed_action, risk_reason, status, reviewer_id, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(request.id), str(request.agent_run_id), request.proposed_action,
                 request.risk_reason, request.status.value, request.reviewer_id, request.schema_version),
            )
        return request

    def update_approval_request(
        self, request_id: UUID, status: ApprovalStatus, reviewer_id: str
    ) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE approval_requests SET status = ?, reviewer_id = ? WHERE id = ?",
                (status.value, reviewer_id, str(request_id)),
            )
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
