# Operations and observability

## Operational limits

- Tool budget: each ticket is capped by `max_tool_calls` and `max_execution_seconds` from the app configuration.
- Triage guardrail: confidence below the configured floor routes directly to human review.
- Approval expiry: all approval requests expire after 24 hours or the configured expiry window, whichever comes first.
- Rate limiting: refunds and sensitive account actions are limited to 3 events per customer within a 24-hour window.

## Retention and auditing

- Ticket, agent-run, escalation, and approval data remain in the local SQLite store for debugging and replay.
- Audit events and guardrail records are retained alongside the original ticket so a human reviewer can explain why an action was taken or blocked.
- Logs are intentionally structured and redacted so they remain useful without exposing raw customer data.

## Failure recovery

- A failed tool call records the error in the agent run and escalates the ticket when the automation budget is exhausted.
- Dead-letter entries are preserved in the escalation queue so the system never silently drops a ticket.
- The retry endpoint moves a ticket back to `in_progress` while keeping the original dead-letter record for audit purposes.

## Inspecting a trace for one ticket

1. Create or locate the ticket ID from the ingestion response or dashboard.
2. Query the ticket record and related agent runs from the local database.
3. Review the audit and guardrail events for the same ticket to understand triage, tool usage, and any approval or escalation decisions.
4. Inspect the structured logs emitted by the API; they include the ticket ID, trace ID, tool name, latency, and status in JSON format.
5. Use the health and readiness endpoints to confirm the service is running before retrying a failed ticket.

## Local tracing

- The app attempts to initialize an OTel tracer when the optional tracing dependency is available.
- Even without the full exporter, the service emits redacted structured logs and metrics that are suitable for local debugging and operational review.

## Production boundaries

- The local router is deterministic and does not provide production model quality or model-cost accounting.
- SQLite and the local reviewer header are development conveniences; production needs a managed database and authenticated reviewer sessions.
- Configure secrets through the deployment environment, never through source control or ticket content.
- Keep trace and audit retention aligned with the organization's privacy policy and delete records on the approved retention schedule.
