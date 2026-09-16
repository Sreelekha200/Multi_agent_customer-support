from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TicketChannel(StrEnum):
    EMAIL = "email"
    CHAT = "chat"
    WEB_FORM = "web_form"


class TicketStatus(StrEnum):
    NEW = "new"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class TicketCategory(StrEnum):
    BILLING = "billing"
    TECHNICAL = "technical"
    REFUND = "refund"
    ACCOUNT_SECURITY = "account-security"
    OTHER = "other"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ToolCall(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    args: dict[str, object] = Field(default_factory=dict)
    result: object | None = None
    latency_ms: int = Field(ge=0)


class TicketCreate(BaseModel):
    customer_id: str = Field(min_length=1, max_length=128)
    channel: TicketChannel
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20_000)
    external_id: str | None = Field(default=None, max_length=256)


class Ticket(TicketCreate):
    id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    status: TicketStatus = TicketStatus.NEW
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = Field(default=1, ge=1)


class CustomerContext(BaseModel):
    customer_id: str = Field(min_length=1, max_length=128)
    plan: str | None = None
    recent_orders: list[dict[str, object]] = Field(default_factory=list)
    past_tickets: list[dict[str, object]] = Field(default_factory=list)


class ProposedAction(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, object] = Field(default_factory=dict)
    requires_approval: bool = False


class ResponseDraft(BaseModel):
    customer_message: str = Field(min_length=1, max_length=20_000)
    internal_notes: str | None = Field(default=None, max_length=20_000)
    proposed_action: ProposedAction | None = None
    escalation_reason: str | None = Field(default=None, max_length=2_000)


class TriageResult(BaseModel):
    category: TicketCategory
    urgency: int = Field(ge=1, le=5)
    sentiment: str = Field(min_length=1, max_length=32)
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1, max_length=2_000)


class AgentRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    ticket_id: UUID
    agent_name: str = Field(min_length=1, max_length=128)
    trace_id: UUID
    tool_calls: list[ToolCall] = Field(default_factory=list)
    output: object | None = None
    guardrail_flags: list[str] = Field(default_factory=list)
    approved_by: str | None = None
    schema_version: int = Field(default=1, ge=1)


class ApprovalRequest(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    agent_run_id: UUID
    customer_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=128)
    proposed_action: str = Field(min_length=1, max_length=2_000)
    risk_reason: str = Field(min_length=1, max_length=2_000)
    status: ApprovalStatus = ApprovalStatus.PENDING
    reviewer_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(hours=24))
    schema_version: int = Field(default=1, ge=1)
