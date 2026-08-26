from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app, ticket_repository

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
