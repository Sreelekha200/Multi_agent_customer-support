import re
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from .config import Settings, get_settings
from .database import TicketRepository
from .guardrails import redact_pii, validate_response_policy
from .schemas import (
    AgentRun,
    ResponseDraft,
    Ticket,
    TicketCategory,
    TicketStatus,
    ToolCall,
    TriageResult,
)
from .tools import ApprovalRequired, ToolError, ToolRegistry


class OrchestrationError(Exception):
    pass


class BudgetExceeded(OrchestrationError):
    pass


class AgentOrchestrator:
    def __init__(self, repository: TicketRepository, settings: Settings | None = None,
                 triage: Callable[[Ticket, dict[str, Any]], TriageResult] | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.tools = ToolRegistry(repository, self.settings)
        self.triage = triage or self._classify

    def process(self, ticket_id: UUID) -> dict[str, Any]:
        stored = self.repository.get_ticket(ticket_id)
        if not stored:
            raise OrchestrationError("Ticket was not found")
        ticket = Ticket.model_validate(stored)
        started = time.monotonic()
        tool_calls = 0
        active_calls: list[ToolCall] = []

        def call(agent: str, name: str, **arguments: Any) -> Any:
            nonlocal tool_calls
            if tool_calls >= self.settings.max_tool_calls or time.monotonic() - started > self.settings.max_execution_seconds:
                raise BudgetExceeded("Execution budget exhausted")
            tool_calls += 1
            call_started = time.monotonic()
            try:
                result = self.tools.call(agent, name, **arguments)
                if not isinstance(result, (dict, list, str, int, float, bool, type(None))):
                    raise ToolError("Tool returned an unsupported result")
                active_calls.append(ToolCall(name=name, args=arguments, result=result,
                                             latency_ms=int((time.monotonic() - call_started) * 1000)))
                return result
            except Exception as error:
                active_calls.append(ToolCall(name=name, args=arguments, result={"error": str(error)},
                                             latency_ms=int((time.monotonic() - call_started) * 1000)))
                raise

        context: dict[str, Any] = {}
        triage_flags: list[str] = []
        sanitized_subject, subject_flags = redact_pii(ticket.subject)
        sanitized_body, body_flags = redact_pii(ticket.body)
        if subject_flags or body_flags:
            triage_flags.extend(subject_flags + body_flags)
            ticket.subject = sanitized_subject
            ticket.body = sanitized_body
        try:
            context = call("triage", "get_customer_context", customer_id=ticket.customer_id)
            triage = self.triage(ticket, context)
        except (BudgetExceeded, ToolError, ValueError):
            triage_flags.append("triage_error")
            triage = TriageResult(category=TicketCategory.OTHER, urgency=5, sentiment="unknown",
                                  confidence=0, summary="Triage could not be completed")
        triage_run = AgentRun(ticket_id=ticket.id, agent_name="triage", trace_id=ticket.trace_id,
                              output=triage.model_dump(mode="json"), guardrail_flags=triage_flags,
                              tool_calls=active_calls)
        self.repository.save_agent_run(triage_run)
        self.repository.update_ticket_status(ticket.id, TicketStatus.TRIAGED)
        if triage.confidence < self.settings.triage_confidence_floor:
            return self._escalate(ticket, triage_run, "Triage confidence is below the routing floor")

        agent = {TicketCategory.BILLING: "billing", TicketCategory.TECHNICAL: "technical",
                 TicketCategory.REFUND: "refunds", TicketCategory.ACCOUNT_SECURITY: "account-security"}.get(triage.category)
        if not agent:
            return self._escalate(ticket, triage_run, "No specialist is available for this request")
        active_calls = []
        response, flags = self._run_specialist(agent, ticket, call)
        omissions = validate_response_policy(response.customer_message)
        if omissions:
            flags.extend(omissions)
            response.customer_message = "We need to review this response with a human support specialist."
            response.escalation_reason = response.escalation_reason or "Response policy violation"
        redacted_message, message_flags = redact_pii(response.customer_message)
        response.customer_message = redacted_message
        flags.extend(message_flags)
        run = AgentRun(ticket_id=ticket.id, agent_name=agent, trace_id=ticket.trace_id,
                       output=response.model_dump(mode="json"), guardrail_flags=sorted(set(flags)),
                       tool_calls=active_calls)
        self.repository.save_agent_run(run)
        if response.escalation_reason or flags:
            return self._escalate(ticket, run, response.escalation_reason or "Specialist execution failed")
        self.repository.update_ticket_status(ticket.id, TicketStatus.RESOLVED)
        return {"status": TicketStatus.RESOLVED.value, "agent": agent, "response": response.model_dump(mode="json")}

    def _classify(self, ticket: Ticket, _: dict[str, Any]) -> TriageResult:
        text = f"{ticket.subject} {ticket.body}".lower()
        categories = {TicketCategory.ACCOUNT_SECURITY: ("password", "login", "suspicious", "2fa", "security"),
                      TicketCategory.REFUND: ("refund", "credit", "money back"),
                      TicketCategory.BILLING: ("invoice", "charge", "subscription", "plan", "billing"),
                      TicketCategory.TECHNICAL: ("bug", "error", "broken", "not working", "timeout", "unable")}
        for category, words in categories.items():
            if any(word in text for word in words):
                return TriageResult(category=category, urgency=4 if "urgent" in text else 3,
                                    sentiment="negative", confidence=0.95, summary=ticket.subject)
        return TriageResult(category=TicketCategory.OTHER, urgency=2, sentiment="neutral", confidence=0.35,
                            summary=ticket.subject)

    def _run_specialist(self, agent: str, ticket: Ticket, call: Callable[..., Any]) -> tuple[ResponseDraft, list[str]]:
        flags: list[str] = []
        try:
            if agent == "billing":
                result = call(agent, "get_subscription", customer_id=ticket.customer_id)
                return ResponseDraft(customer_message=f"Your current plan is {result['plan']} ({result['status']})."), flags
            if agent == "technical":
                result = call(agent, "get_system_status")
                return ResponseDraft(customer_message=f"System status is {result['status']}. We will investigate your issue."), flags
            if agent == "refunds":
                match = re.search(r"order[- ]?(\d+)", f"{ticket.subject} {ticket.body}", re.IGNORECASE)
                if not match:
                    return ResponseDraft(customer_message="A refund specialist needs the order ID.", escalation_reason="Order ID is missing"), flags
                eligibility = call(agent, "check_refund_eligibility", order_id=f"order-{match.group(1)}")
                if not eligibility["eligible"]:
                    return ResponseDraft(customer_message="This order is not eligible for a refund."), flags
                if eligibility.get("amount", 0) > self.settings.refund_approval_threshold:
                    flags.append("refund_above_threshold")
                    return ResponseDraft(customer_message="Your refund request is above the automatic approval limit and requires human review.", escalation_reason="Refund requires approval"), flags
                return ResponseDraft(customer_message="Your refund request is ready for review.", escalation_reason="Refund requires approval"), flags
            return ResponseDraft(customer_message="An account specialist will review this request.", escalation_reason="Account security action requires human review"), flags
        except BudgetExceeded:
            flags.append("budget_exhausted")
        except ToolError as error:
            flags.append("tool_failure")
            if isinstance(error, ApprovalRequired):
                flags.append("approval_required")
        return ResponseDraft(customer_message="We could not complete this request automatically.", escalation_reason="A support tool failed"), flags

    def _escalate(self, ticket: Ticket, run: AgentRun, reason: str) -> dict[str, Any]:
        self.repository.update_ticket_status(ticket.id, TicketStatus.ESCALATED)
        self.repository.record_audit_event(ticket.id, "escalated", {"reason": reason, "agent_run_id": str(run.id)})
        return {"status": TicketStatus.ESCALATED.value, "reason": reason, "agent_run_id": str(run.id)}