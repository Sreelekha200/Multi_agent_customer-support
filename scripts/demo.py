"""Run the documented end-to-end support workflow against the local API."""

import json
import sys
from typing import Any

import httpx

BASE_URL = "http://127.0.0.1:8000"
HEADERS = {"X-Ingestion-Key": "dev-ingestion-key"}


def show(label: str, response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    print(f"{label}: {json.dumps(payload, indent=2)}")
    return payload


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        show("health", client.get("/health"))
        ticket = show(
            "ingest",
            client.post(
                "/tickets",
                headers=HEADERS,
                json={
                    "customer_id": "customer-456",
                    "channel": "email",
                    "subject": "Refund order-101",
                    "body": "Please review my refund request for order-101.",
                },
            ),
        )
        processed = show("process", client.post(f"/tickets/{ticket['id']}/process"))
        show("queue", client.get(f"/tickets/{ticket['id']}/queue"))
        show("metrics", client.get("/metrics"))
        print(f"Trace ID: {ticket['trace_id']}")
        print(f"Final status: {processed['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
