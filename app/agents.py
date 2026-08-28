import json
from typing import Any

from agents import Agent, function_tool

from .schemas import ResponseDraft, TriageResult
from .tools import ToolRegistry


def build_agents(registry: ToolRegistry) -> dict[str, Agent[Any]]:
    """Build SDK agents with tools limited to each specialist's allowlist."""

    @function_tool
    def get_customer_context(customer_id: str) -> dict[str, Any]:
        return registry.call("triage", "get_customer_context", customer_id=customer_id)

    @function_tool
    def get_subscription(customer_id: str) -> dict[str, Any]:
        return registry.call("billing", "get_subscription", customer_id=customer_id)

    @function_tool
    def search_known_issues(query: str) -> list[dict[str, Any]]:
        return registry.call("technical", "search_known_issues", query=query)

    @function_tool
    def get_system_status() -> dict[str, str]:
        return registry.call("technical", "get_system_status")

    @function_tool
    def run_diagnostic(account_id: str, check: str) -> dict[str, str]:
        return registry.call("technical", "run_diagnostic", account_id=account_id, check=check)

    @function_tool
    def create_bug_ticket(details_json: str) -> dict[str, Any]:
        return registry.call("technical", "create_bug_ticket", details=json.loads(details_json))

    @function_tool
    def check_refund_eligibility(order_id: str) -> dict[str, Any]:
        return registry.call("refunds", "check_refund_eligibility", order_id=order_id)

    @function_tool
    def verify_identity(customer_id: str, verification_code: str) -> dict[str, Any]:
        return registry.call("account-security", "verify_identity", customer_id=customer_id,
                             verification_code=verification_code)

    @function_tool
    def initiate_password_reset(customer_id: str) -> dict[str, str]:
        return registry.call("account-security", "initiate_password_reset", customer_id=customer_id)

    @function_tool
    def flag_account_for_review(customer_id: str, reason: str) -> dict[str, str]:
        return registry.call("account-security", "flag_account_for_review", customer_id=customer_id,
                             reason=reason)

    billing_agent = Agent(
        name="Billing specialist",
        instructions="Answer invoice, subscription, plan, and charge questions using read-only tools.",
        tools=[get_subscription],
        output_type=ResponseDraft,
    )
    technical_agent = Agent(
        name="Technical specialist",
        instructions="Diagnose technical issues with bounded, read-only checks and create bug tickets only when useful.",
        tools=[search_known_issues, get_system_status, run_diagnostic, create_bug_ticket],
        output_type=ResponseDraft,
    )
    refunds_agent = Agent(
        name="Refund specialist",
        instructions="Check refund eligibility and prepare a response. Never claim a refund was issued.",
        tools=[check_refund_eligibility],
        output_type=ResponseDraft,
    )
    account_security_agent = Agent(
        name="Account security specialist",
        instructions="Verify identity and initiate safe account support. Escalate account changes to a human.",
        tools=[verify_identity, initiate_password_reset, flag_account_for_review],
        output_type=ResponseDraft,
    )
    triage_agent = Agent(
        name="Triage router",
        instructions="Classify the ticket, assess urgency and sentiment, summarize it, and choose a specialist.",
        tools=[get_customer_context],
        handoffs=[billing_agent, technical_agent, refunds_agent, account_security_agent],
        output_type=TriageResult,
    )
    return {
        "triage": triage_agent,
        "billing": billing_agent,
        "technical": technical_agent,
        "refunds": refunds_agent,
        "account-security": account_security_agent,
    }