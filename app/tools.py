import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .database import TicketRepository
from .schemas import Ticket, TicketChannel


class ToolError(Exception):
    """Base error for a safe, expected tool failure."""


class NotFoundError(ToolError):
    pass


class ToolPermissionError(ToolError):
    pass


class ApprovalRequired(ToolError):
    pass


class DiagnosticRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=128)
    check: str = Field(min_length=1, max_length=64)


class RefundRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=128)
    amount: float = Field(gt=0, le=100_000)


class ToolRegistry:
    ALLOWLISTS: ClassVar[dict[str, set[str]]] = {
        "triage": {"get_customer_context"},
        "billing": {"get_invoice", "get_subscription", "explain_charge"},
        "technical": {"search_known_issues", "get_system_status", "run_diagnostic", "create_bug_ticket"},
        "refunds": {"get_order", "check_refund_eligibility", "issue_refund"},
        "account-security": {"verify_identity", "initiate_password_reset", "flag_account_for_review"},
    }

    def __init__(self, repository: TicketRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.tools: dict[str, Callable[..., Any]] = {
            "get_customer_context": self.get_customer_context,
            "get_invoice": self.get_invoice,
            "get_subscription": self.get_subscription,
            "explain_charge": self.explain_charge,
            "search_known_issues": self.search_known_issues,
            "get_system_status": self.get_system_status,
            "run_diagnostic": self.run_diagnostic,
            "create_bug_ticket": self.create_bug_ticket,
            "get_order": self.get_order,
            "check_refund_eligibility": self.check_refund_eligibility,
            "issue_refund": self.issue_refund,
            "verify_identity": self.verify_identity,
            "initiate_password_reset": self.initiate_password_reset,
            "flag_account_for_review": self.flag_account_for_review,
        }

    def call(self, agent_name: str, tool_name: str, **arguments: Any) -> Any:
        if tool_name not in self.ALLOWLISTS.get(agent_name, set()):
            raise ToolPermissionError(f"{tool_name} is not allowed for {agent_name}")
        try:
            return self.tools[tool_name](**arguments)
        except KeyError as error:
            raise ToolError(f"Unknown tool: {tool_name}") from error

    def _query_one(self, query: str, parameters: tuple[Any, ...]) -> dict[str, Any]:
        with self.repository._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        if not row:
            raise NotFoundError("Requested record was not found")
        return dict(row)

    def get_customer_context(self, customer_id: str) -> dict[str, Any]:
        customer = self._query_one("SELECT * FROM customers WHERE id = ?", (customer_id,))
        with self.repository._connect() as connection:
            orders = [dict(row) for row in connection.execute(
                "SELECT id, amount, status FROM orders WHERE customer_id = ?", (customer_id,)
            )]
        return {"customer_id": customer_id, "plan": customer["plan"], "recent_orders": orders}

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._query_one("SELECT * FROM invoices WHERE id = ?", (invoice_id,))

    def get_subscription(self, customer_id: str) -> dict[str, Any]:
        return self._query_one("SELECT * FROM subscriptions WHERE customer_id = ?", (customer_id,))

    def explain_charge(self, charge_id: str) -> dict[str, Any]:
        invoice = self.get_invoice(charge_id)
        return {"charge_id": charge_id, "amount": invoice["amount"], "explanation": "Subscription charge"}

    def search_known_issues(self, query: str) -> list[dict[str, Any]]:
        if not query or len(query) > 500:
            raise ToolError("Issue search query must contain 1-500 characters")
        with self.repository._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM known_issues WHERE title LIKE ?", (f"%{query}%",)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_system_status(self) -> dict[str, str]:
        return {"status": "operational", "source": "local-status-fixture"}

    def run_diagnostic(self, account_id: str, check: str) -> dict[str, str]:
        request = DiagnosticRequest(account_id=account_id, check=check)
        if request.check not in {"connectivity", "login", "billing"}:
            raise ToolError("Unsupported diagnostic check")
        return {"account_id": request.account_id, "check": request.check, "status": "passed"}

    def create_bug_ticket(self, details: dict[str, Any]) -> dict[str, Any]:
        subject = str(details.get("subject", "")).strip()
        body = str(details.get("body", "")).strip()
        customer_id = str(details.get("customer_id", "")).strip()
        if not customer_id or not subject or not body or len(subject) > 500 or len(body) > 20_000:
            raise ToolError("Bug details are invalid")
        ticket = Ticket(
            customer_id=customer_id,
            channel=TicketChannel.CHAT,
            subject=subject,
            body=body,
            external_id=str(details.get("external_id")) if details.get("external_id") else None,
        )
        try:
            saved = self.repository.save_ticket(ticket)
        except sqlite3.IntegrityError as error:
            raise ToolError("Bug ticket already exists") from error
        self.repository.record_audit_event(saved.id, "bug_ticket_created", {"source": "technical-agent"})
        return {"ticket_id": str(saved.id), "status": saved.status.value}

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._query_one("SELECT * FROM orders WHERE id = ?", (order_id,))

    def check_refund_eligibility(self, order_id: str) -> dict[str, Any]:
        order = self.get_order(order_id)
        return {"order_id": order_id, "eligible": order["status"] == "paid", "amount": order["amount"]}

    def issue_refund(self, order_id: str, amount: float) -> dict[str, Any]:
        request = RefundRequest(order_id=order_id, amount=amount)
        order = self.get_order(request.order_id)
        if request.amount > float(order["amount"]):
            raise ToolError("Refund amount exceeds order amount")
        if request.amount > self.settings.refund_approval_threshold:
            raise ApprovalRequired("Refund exceeds the configured approval threshold")
        with self.repository._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM refunds WHERE customer_id = ? AND created_at >= date('now')",
                (self._query_one("SELECT customer_id FROM orders WHERE id = ?", (order_id,))["customer_id"],),
            ).fetchone()[0]
            if count >= 3:
                raise ToolError("Daily refund limit reached")
            refund_id = f"refund-{datetime.now(UTC).timestamp()}"
            connection.execute(
                "INSERT INTO refunds (id, order_id, customer_id, amount, created_at) VALUES (?, ?, ?, ?, ?)",
                (refund_id, order_id, order["customer_id"], request.amount, datetime.now(UTC).isoformat()),
            )
        return {"refund_id": refund_id, "order_id": order_id, "amount": request.amount, "status": "issued"}

    def verify_identity(self, customer_id: str, verification_code: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Z0-9]{6}", verification_code):
            raise ToolError("Invalid verification code format")
        self._query_one("SELECT id FROM customers WHERE id = ?", (customer_id,))
        return {"customer_id": customer_id, "verified": verification_code == "ABC123"}

    def initiate_password_reset(self, customer_id: str) -> dict[str, str]:
        self._query_one("SELECT id FROM customers WHERE id = ?", (customer_id,))
        return {"customer_id": customer_id, "status": "reset_link_sent"}

    def flag_account_for_review(self, customer_id: str, reason: str) -> dict[str, str]:
        self._query_one("SELECT id FROM customers WHERE id = ?", (customer_id,))
        if not reason or len(reason) > 500:
            raise ToolError("Review reason must contain 1-500 characters")
        return {"customer_id": customer_id, "status": "flagged", "reason": reason}
