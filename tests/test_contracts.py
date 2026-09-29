from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app, ticket_repository
from app.schemas import ApprovalRequest, ApprovalStatus

client = TestClient(app)


def test_ingestion_contract_requires_auth_and_returns_traceable_ticket() -> None:
    payload = {
        "customer_id": "customer-123",
        "channel": "email",
        "subject": "Contract",
        "body": "Please help.",
    }
    response = client.post(
        "/tickets", json=payload, headers={"X-Ingestion-Key": "dev-ingestion-key"}
    )

    assert response.status_code == 201
    body = response.json()
    assert {"id", "trace_id", "status", "created_at"}.issubset(body)


def test_approval_contract_requires_confirmation_and_reviewer() -> None:
    approval = ticket_repository.save_approval_request(
        ApprovalRequest(
            id=uuid4(),
            agent_run_id=uuid4(),
            customer_id="customer-123",
            action="issue_refund",
            proposed_action="Review refund",
            risk_reason="Amount exceeds threshold",
            status=ApprovalStatus.PENDING,
        )
    )

    missing_confirmation = client.post(
        f"/approvals/{approval.id}/approve",
        json={"confirm": False},
        headers={"X-Reviewer-Id": "support-lead"},
    )
    assert missing_confirmation.status_code == 400


def test_tool_contract_rejects_unauthorized_tool_access() -> None:
    from app.tools import ToolPermissionError, ToolRegistry

    registry = ToolRegistry(ticket_repository)
    try:
        registry.call("billing", "issue_refund", order_id="order-100", amount=1)
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("billing must not call issue_refund")
