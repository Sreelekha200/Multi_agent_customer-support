from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app, ticket_repository
from app.orchestration import AgentOrchestrator
from app.schemas import Ticket, TicketCategory, TriageResult
from app.tools import ApprovalRequired, NotFoundError, ToolError, ToolPermissionError, ToolRegistry

client = TestClient(app)
INGESTION_HEADERS = {"X-Ingestion-Key": "dev-ingestion-key"}


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ticket_ingestion_normalizes_ticket() -> None:
    response = client.post(
        "/tickets",
        json={
            "customer_id": "customer-123",
            "channel": "web_form",
            "subject": "Cannot access my invoice",
            "body": "Please help me find my latest invoice.",
        },
        headers=INGESTION_HEADERS,
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["customer_id"] == "customer-123"
    assert payload["status"] == "new"
    assert payload["trace_id"]


def test_ticket_ingestion_persists_ticket() -> None:
    response = client.post(
        "/tickets",
        json={
            "customer_id": "customer-456",
            "channel": "email",
            "subject": "Question about my plan",
            "body": "What is included in my current plan?",
        },
        headers=INGESTION_HEADERS,
    )

    ticket_id = response.json()["id"]
    stored_ticket = ticket_repository.get_ticket(ticket_id)

    assert stored_ticket is not None
    assert stored_ticket["customer_id"] == "customer-456"
    assert stored_ticket["status"] == "new"


def test_ticket_ingestion_supports_all_channels_and_is_idempotent() -> None:
    payload = {
        "customer_id": "customer-789",
        "channel": "chat",
        "subject": "The app is unavailable",
        "body": "I cannot sign in from the mobile app.",
        "external_id": f"chat-event-{uuid4()}",
    }

    first_response = client.post("/tickets", json=payload, headers=INGESTION_HEADERS)
    second_response = client.post("/tickets", json=payload, headers=INGESTION_HEADERS)

    assert first_response.status_code == 201
    assert second_response.status_code == 200
    assert second_response.json()["id"] == first_response.json()["id"]


def test_ticket_ingestion_rejects_missing_or_invalid_authentication() -> None:
    payload = {
        "customer_id": "customer-999",
        "channel": "email",
        "subject": "Help",
        "body": "I need assistance.",
    }

    missing_key_response = client.post("/tickets", json=payload)
    invalid_key_response = client.post(
        "/tickets", json=payload, headers={"X-Ingestion-Key": "wrong-key"}
    )

    assert missing_key_response.status_code == 401
    assert invalid_key_response.status_code == 401


def test_ticket_ingestion_rejects_malformed_payload() -> None:
    response = client.post(
        "/tickets",
        json={"customer_id": "customer-123", "channel": "unknown"},
        headers=INGESTION_HEADERS,
    )

    assert response.status_code == 422


def test_scoped_tools_read_seeded_customer_data() -> None:
    registry = ToolRegistry(ticket_repository)

    context = registry.call("triage", "get_customer_context", customer_id="customer-123")
    order = registry.call("refunds", "get_order", order_id="order-100")

    assert context["plan"] == "pro"
    assert order["amount"] == 29.0


def test_tool_allowlist_rejects_cross_agent_access() -> None:
    registry = ToolRegistry(ticket_repository)

    try:
        registry.call("billing", "issue_refund", order_id="order-100", amount=10)
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("billing agent must not access refund tools")


def test_refund_threshold_requires_approval_and_safe_refund_is_recorded() -> None:
    registry = ToolRegistry(ticket_repository)

    try:
        registry.call("refunds", "issue_refund", order_id="order-101", amount=79)
    except ApprovalRequired:
        pass
    else:
        raise AssertionError("above-threshold refund must require approval")

    result = registry.call("refunds", "issue_refund", order_id="order-100", amount=10)
    assert result["status"] == "issued"


def test_tools_return_safe_failures_for_unknown_records_and_diagnostics() -> None:
    registry = ToolRegistry(ticket_repository)

    try:
        registry.call("billing", "get_invoice", invoice_id="missing")
    except NotFoundError:
        pass
    else:
        raise AssertionError("missing records must fail safely")

    try:
        registry.call("technical", "run_diagnostic", account_id="customer-123", check="shell")
    except ToolError as error:
        assert str(error) == "Unsupported diagnostic check"
    else:
        raise AssertionError("unsupported diagnostics must be rejected")


def test_orchestrator_routes_billing_and_records_tool_call() -> None:
    ticket = ticket_repository.save_ticket(
        Ticket(customer_id="customer-123", channel="chat", subject="Plan question", body="What plan am I on?")
    )

    result = AgentOrchestrator(ticket_repository).process(ticket.id)

    assert result["status"] == "resolved"
    assert result["agent"] == "billing"
    with ticket_repository._connect() as connection:
        run = connection.execute(
            "SELECT agent_name FROM agent_runs WHERE ticket_id = ? ORDER BY rowid DESC LIMIT 1", (str(ticket.id),)
        ).fetchone()
        calls = connection.execute(
            "SELECT name FROM tool_calls WHERE agent_run_id = (SELECT id FROM agent_runs WHERE ticket_id = ? AND agent_name = 'billing' ORDER BY rowid DESC LIMIT 1)",
            (str(ticket.id),),
        ).fetchall()
    assert run["agent_name"] == "billing"
    assert [call["name"] for call in calls] == ["get_subscription"]


def test_orchestrator_escalates_low_confidence_without_specialist() -> None:
    ticket = ticket_repository.save_ticket(
        Ticket(customer_id="customer-123", channel="email", subject="Hello", body="I have a question")
    )
    triage = lambda _ticket, _context: TriageResult(category=TicketCategory.BILLING, urgency=1,
                                                    sentiment="neutral", confidence=0.2, summary="ambiguous")

    result = AgentOrchestrator(ticket_repository, triage=triage).process(ticket.id)

    assert result["status"] == "escalated"
    assert "confidence" in result["reason"]
