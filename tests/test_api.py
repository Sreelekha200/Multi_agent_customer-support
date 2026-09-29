from uuid import uuid4

from fastapi.testclient import TestClient

from app.guardrails import redact_pii, validate_response_policy
from app.main import app, ticket_repository
from app.orchestration import AgentOrchestrator
from app.schemas import ApprovalRequest, ApprovalStatus, Ticket, TicketCategory, TriageResult
from app.tools import ApprovalRequired, NotFoundError, ToolError, ToolPermissionError, ToolRegistry

client = TestClient(app)
INGESTION_HEADERS = {"X-Ingestion-Key": "dev-ingestion-key"}


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_and_metrics_endpoints() -> None:
    ready = client.get("/ready")
    metrics = client.get("/metrics")

    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert metrics.status_code == 200
    assert "metrics" in metrics.json()


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
        Ticket(
            customer_id="customer-123",
            channel="chat",
            subject="Plan question",
            body="What plan am I on?",
        )
    )

    result = AgentOrchestrator(ticket_repository).process(ticket.id)

    assert result["status"] == "resolved"
    assert result["agent"] == "billing"
    with ticket_repository._connect() as connection:
        run = connection.execute(
            "SELECT agent_name FROM agent_runs WHERE ticket_id = ? ORDER BY rowid DESC LIMIT 1",
            (str(ticket.id),),
        ).fetchone()
        calls = connection.execute(
            "SELECT name FROM tool_calls WHERE agent_run_id = (SELECT id FROM agent_runs WHERE ticket_id = ? AND agent_name = 'billing' ORDER BY rowid DESC LIMIT 1)",
            (str(ticket.id),),
        ).fetchall()
    assert run["agent_name"] == "billing"
    assert [call["name"] for call in calls] == ["get_subscription"]


def test_orchestrator_escalates_low_confidence_without_specialist() -> None:
    ticket = ticket_repository.save_ticket(
        Ticket(
            customer_id="customer-123", channel="email", subject="Hello", body="I have a question"
        )
    )
    triage = lambda _ticket, _context: TriageResult(
        category=TicketCategory.BILLING,
        urgency=1,
        sentiment="neutral",
        confidence=0.2,
        summary="ambiguous",
    )

    result = AgentOrchestrator(ticket_repository, triage=triage).process(ticket.id)

    assert result["status"] == "escalated"
    assert "confidence" in result["reason"]


def test_guardrails_redact_pii_and_block_unsafe_response_language() -> None:
    redacted, flags = redact_pii("Email jane.smith@example.com or call 555-123-4567")

    assert "jane.smith@example.com" not in redacted
    assert "555-123-4567" not in redacted
    assert flags

    violations = validate_response_policy("I can guarantee a full refund and give legal advice")
    assert violations


def test_account_actions_are_hard_denied_and_rate_limited() -> None:
    registry = ToolRegistry(ticket_repository)

    try:
        registry.call("account-security", "disable_2fa", customer_id="customer-123")
    except ToolPermissionError as error:
        assert "Human review required" in str(error)
    else:
        raise AssertionError("dangerous account actions must be blocked")

    assert registry.check_rate_limit("customer-123", "refund") == False

    registry.record_rate_limit_event("customer-123", "refund")
    registry.record_rate_limit_event("customer-123", "refund")
    registry.record_rate_limit_event("customer-123", "refund")
    assert registry.check_rate_limit("customer-123", "refund") == True


def test_approval_requests_support_review_and_expiration() -> None:
    registry = ToolRegistry(ticket_repository)
    approval = registry.create_approval_request(
        "customer-123",
        "11111111-1111-4111-8111-111111111111",
        "issue_refund",
        "Large refund requires review",
    )

    assert approval.status == ApprovalStatus.PENDING
    registry.approve_approval_request(approval.id, "reviewer-1")
    assert registry.get_approval_request(approval.id).status == ApprovalStatus.APPROVED

    expired = registry.create_approval_request(
        "customer-123",
        "11111111-1111-4111-8111-111111111112",
        "change_email",
        "Sensitive customer change",
    )
    expired.created_at = expired.created_at.replace(year=expired.created_at.year - 1)
    assert registry.is_approval_expired(expired) is True


def test_phase_six_escalation_returns_final_response_contract_and_queue_record() -> None:
    ticket = ticket_repository.save_ticket(
        Ticket(customer_id="customer-123", channel="email", subject="Hello", body="I need help")
    )

    result = AgentOrchestrator(
        ticket_repository,
        triage=lambda _ticket, _context: TriageResult(
            category=TicketCategory.BILLING,
            urgency=1,
            sentiment="neutral",
            confidence=0.2,
            summary="ambiguous request",
        ),
    ).process(ticket.id)

    assert result["status"] == "escalated"
    assert "final_response" in result
    assert result["final_response"]["status"] == "escalated"
    assert result["final_response"]["escalation_reason"]
    queue = ticket_repository.get_escalation_queue(ticket.id)
    assert queue and queue[0]["status"] == "pending"


def test_phase_six_retry_marks_ticket_in_progress_and_dead_letter_is_preserved() -> None:
    ticket = ticket_repository.save_ticket(
        Ticket(
            customer_id="customer-123",
            channel="chat",
            subject="Retry me",
            body="Please retry this ticket",
        )
    )

    ticket_repository.record_dead_letter(ticket.id, "temporary tool outage")
    ticket_repository.retry_ticket(ticket.id)

    stored = ticket_repository.get_ticket(ticket.id)
    assert stored["status"] == "in_progress"
    assert ticket_repository.get_dead_letter(ticket.id) is not None


def test_phase_seven_dashboard_lists_approvals_and_escalations() -> None:
    ticket = ticket_repository.save_ticket(
        Ticket(customer_id="customer-123", channel="web_form", subject="Refund", body="I need help")
    )
    request = ticket_repository.save_approval_request(
        ApprovalRequest(
            id=__import__("uuid").UUID("11111111-1111-4111-8111-111111111113"),
            agent_run_id=__import__("uuid").UUID("11111111-1111-4111-8111-111111111114"),
            customer_id="customer-123",
            action="issue_refund",
            proposed_action="Issue refund for order-100",
            risk_reason="Refund exceeds threshold",
            status=ApprovalStatus.PENDING,
        )
    )
    ticket_repository.record_escalation(
        ticket.id, request.agent_run_id, "Needs human review", {"status": "escalated"}
    )

    response = client.get("/dashboard", headers={"X-Reviewer-Id": "support-lead"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["pending_approvals"]
    assert payload["escalated_tickets"]
    assert payload["pending_approvals"][0]["customer_id"] == "customer-123"


def test_phase_seven_approve_reject_are_permissioned_and_idempotent() -> None:
    approval = ticket_repository.save_approval_request(
        ApprovalRequest(
            id=__import__("uuid").UUID("11111111-1111-4111-8111-111111111115"),
            agent_run_id=__import__("uuid").UUID("11111111-1111-4111-8111-111111111116"),
            customer_id="customer-123",
            action="issue_refund",
            proposed_action="Issue refund for order-100",
            risk_reason="Large amount",
            status=ApprovalStatus.PENDING,
        )
    )
    denied = client.post(
        f"/approvals/{approval.id}/approve",
        json={"reviewer_id": "random-user", "confirm": True},
        headers={"X-Reviewer-Id": "random-user"},
    )
    assert denied.status_code == 403

    approved = client.post(
        f"/approvals/{approval.id}/approve",
        json={"reviewer_id": "support-lead", "confirm": True},
        headers={"X-Reviewer-Id": "support-lead"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    duplicate = client.post(
        f"/approvals/{approval.id}/approve",
        json={"reviewer_id": "support-lead", "confirm": True},
        headers={"X-Reviewer-Id": "support-lead"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "approved"

    rejected = client.post(
        f"/approvals/{approval.id}/reject",
        json={"reviewer_id": "support-lead"},
        headers={"X-Reviewer-Id": "support-lead"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def test_phase_seven_stale_requests_cannot_be_approved() -> None:
    approval = ticket_repository.save_approval_request(
        ApprovalRequest(
            id=__import__("uuid").UUID("11111111-1111-4111-8111-111111111117"),
            agent_run_id=__import__("uuid").UUID("11111111-1111-4111-8111-111111111118"),
            customer_id="customer-456",
            action="change_email",
            proposed_action="Change email address",
            risk_reason="Sensitive change",
            status=ApprovalStatus.PENDING,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            - __import__("datetime").timedelta(days=3),
        )
    )
    response = client.post(
        f"/approvals/{approval.id}/approve",
        json={"reviewer_id": "support-lead", "confirm": True},
        headers={"X-Reviewer-Id": "support-lead"},
    )
    assert response.status_code == 409
    assert "expired" in response.json()["detail"].lower()
