Multi-Agent Customer Support Triage System
Architecture & Build Plan (OpenAI Agents SDK)
1. Goal & Scope

Build a system that ingests support tickets (email/chat/web form), routes them to the right specialist agent, resolves what it safely can, and escalates the rest to a human — with full observability and human-in-the-loop guardrails for anything risky (money, account changes, PII).

What this project needs to prove, at SDE2 level:

You can design multi-agent orchestration, not just prompt one agent
You handle failure modes (bad tool calls, hallucinated data, infinite loops)
You have guardrails and approval gates for irreversible actions
You have observability: every decision is traceable and debuggable
The system degrades gracefully instead of failing silently
2. High-Level Architecture
                    ┌─────────────────┐
   Ticket in ──────▶│  Ingestion API   │  (webhook: email/chat/form)
                    └────────┬─────────┘
                             │ normalize → Ticket object
                             ▼
                    ┌─────────────────┐
                    │  Triage Agent    │  classify + route
                    │  (Router)        │
                    └────────┬─────────┘
              ┌──────────────┼──────────────┬───────────────┐
              ▼              ▼              ▼               ▼
       ┌───────────┐  ┌────────────┐ ┌────────────┐ ┌──────────────┐
       │  Billing   │  │ Technical  │ │  Refunds   │ │  Account/    │
       │  Agent     │  │  Agent     │ │  Agent     │ │  Security    │
       └─────┬──────┘  └─────┬──────┘ └─────┬──────┘ └──────┬───────┘
             │               │              │               │
             ▼               ▼              ▼               ▼
        scoped tools    scoped tools   scoped tools    scoped tools
        (read-only)     (read-only +   (write, gated   (write, gated
                         diagnostics)   by approval)     by approval)
              \______________|______________|______________/
                             ▼
                    ┌─────────────────┐
                    │ Guardrail Layer  │  input/output validation,
                    │ (pre + post)     │  PII redaction, $ thresholds
                    └────────┬─────────┘
                             ▼
                    ┌─────────────────┐
                    │ Response / or    │  auto-reply, or
                    │ Escalation queue │  human approval / handoff
                    └────────┬─────────┘
                             ▼
                    ┌─────────────────┐
                    │ Tracing/Eval     │  every run logged, scored,
                    │ store            │  reviewable
                    └─────────────────┘
3. Agents & Responsibilities
3.1 Triage Agent (Router)
Input: raw ticket (subject, body, customer_id, channel, prior ticket history)
Job: classify intent (billing / technical / refund / account-security / other), estimate urgency/sentiment, and hand off
Tools: get_customer_context(customer_id) — read-only lookup of plan, recent orders, past tickets
Output: structured object {category, urgency, confidence, summary} — if confidence < threshold, route straight to human queue instead of guessing
3.2 Billing Agent
Scope: invoice questions, plan explanations, proration questions
Tools: get_invoice(id), get_subscription(customer_id), explain_charge(charge_id) — all read-only
No write tools. Cannot issue credits or change plans itself.
3.3 Technical Agent
Scope: bug reports, "it's not working" tickets
Tools: search_known_issues(query), get_system_status(), run_diagnostic(account_id) (read-only diagnostic, sandboxed), create_bug_ticket(details) (write, but low-risk/reversible)
3.4 Refunds Agent
Scope: refund/credit requests
Tools: get_order(order_id), check_refund_eligibility(order_id), issue_refund(order_id, amount) — gated
Guardrail: any issue_refund call above a configurable threshold (e.g. $50) is intercepted and routed to a human-approval step before execution. Below threshold, still logged and rate-limited per customer/day.
3.5 Account/Security Agent
Scope: password resets, suspicious activity, account changes
Tools: verify_identity(...), initiate_password_reset(...), flag_account_for_review(...)
Guardrail: never allowed to disable 2FA, change email, or unlock an account without a human-approval step — no exceptions, no threshold. This is a hard-coded deny, not a prompted instruction.
3.6 Escalation / Human Handoff

Not an LLM agent — a queue. Any agent can hand off here explicitly (escalate_to_human(reason)), and the guardrail layer can force a handoff regardless of what the agent decided.

4. Guardrail Layer (this is the part that makes it SDE2, not tutorial)

Split into input guardrails and output guardrails, run outside the agent's own reasoning so a hallucinating agent can't reason its way past them:

Guardrail	Type	Behavior
PII detection	input + output	Redact/flag SSNs, full card numbers before they hit a prompt or a reply
$ threshold check	output	Any refund/credit tool call above limit → hold for approval, don't execute
Hard-deny actions	output	Account deletion, 2FA disable, email change → always human-only
Rate limiting	output	Max N refunds / account changes per customer per day, regardless of agent's individual reasoning
Confidence floor	routing	Triage confidence below threshold → human queue, not best-guess routing
Loop/budget guard	orchestration	Max tool calls / max turns per ticket before forced escalation (stops infinite tool-call loops)
Tone/policy check	output	Response reviewed against a short policy rubric before sending (no promises the company can't keep, no legal advice)

Implement these as the SDK's guardrails construct (input_guardrail / output_guardrail hooks) rather than as extra prompt instructions — instructions are advisory, guardrails should be enforced in code.

5. Data & Control Flow (sequence)
Ticket arrives → normalized into a Ticket schema, stored, assigned trace_id
Triage Agent runs → outputs category + confidence
If confidence low → straight to human queue (skip specialist agents entirely)
Else → handoff to specialist agent with the ticket + customer context
Specialist agent reasons, calls scoped tools, drafts a response or a proposed action
Output guardrails run on the proposed action/response
If clean and within thresholds → auto-send response / execute action
If it trips a guardrail → goes to human-approval queue with the agent's reasoning attached (so the human isn't starting from scratch)
Every step (tool calls, arguments, intermediate reasoning, guardrail decisions, timing) is written to the trace store
6. Data Model (minimal)
Ticket {
  id, customer_id, channel, subject, body,
  created_at, status: [new, triaged, in_progress, escalated, resolved]
}

AgentRun {
  id, ticket_id, agent_name, trace_id,
  tool_calls: [ {name, args, result, latency_ms} ],
  output, guardrail_flags: [...], approved_by (nullable)
}

ApprovalRequest {
  id, agent_run_id, proposed_action, risk_reason,
  status: [pending, approved, rejected], reviewer_id
}
7. Tech Stack Suggestion
Orchestration: OpenAI Agents SDK (Python) — agents, handoffs, guardrails, sessions
Tools: plain functions for internal APIs (mock a billing/order DB with Postgres + seed data if you don't have a real backend)
Sandbox use: if the technical agent runs diagnostics, use the SDK's native sandbox execution rather than shelling out yourself
Tracing: SDK's built-in tracing exported to an OTel-compatible backend (Logfire, or self-hosted Grafana Tempo) — this is your debugging and demo surface
Human approval UI: a small internal dashboard (even a simple Next.js or Streamlit page) showing pending approvals with the agent's reasoning trace attached
Eval set: 30–50 hand-written sample tickets with expected category + expected action, used as a regression suite you re-run whenever you change a prompt or tool

## 8. Build To-Do List

### Phase 0: Project foundation

- [x] Decide the initial project layout: `app/`, `tests/`, `dashboard/`, `scripts/`, and `docs/`.
- [x] Create a Python environment and pin dependencies, including the OpenAI Agents SDK, web framework, database client/ORM, validation library, test runner, and tracing dependencies.
- [x] Add configuration management for API keys, database URLs, model name, confidence floor, refund threshold, rate limits, and tool/turn budgets.
- [x] Add `.env.example`, `.gitignore`, logging defaults, and a README with local setup and run commands.
- [x] Define a local development strategy using SQLite for ticket storage and a mocked customer, billing, order, and account data layer.

### Phase 1: Domain schemas and persistence

 [x] Implement validated `Ticket`, `AgentRun`, `ApprovalRequest`, customer context, proposed action, and response schemas.
 [x] Define status and category enums, timestamps, IDs, trace IDs, and schema version fields.
 [x] Create database tables/migrations for tickets, agent runs, tool calls, approval requests, guardrail events, and audit events.
 [x] Seed realistic mock data for customers, subscriptions, invoices, orders, known issues, and account security events.
 [x] Add repository functions for creating/updating tickets, recording runs and tool calls, managing approvals, and querying audit history.
 [x] Add unit/API tests for schema validation, serialization, persistence, authentication, and repository-backed ingestion.

### Phase 2: Ingestion and ticket lifecycle

 [x] Build the ingestion API for email, chat, and web-form payloads.
 [x] Normalize all channels into the canonical `Ticket` schema.
 [x] Detect duplicate or replayed webhook deliveries and make ingestion idempotent.
 [x] Store each accepted ticket and assign a trace ID before agent execution.
 [x] Add input size limits, malformed-payload handling, authentication for webhooks, and safe error responses.
 [x] Add API tests for each channel, validation failures, idempotency, and persistence.

### Phase 3: Scoped business tools

- [ ] Implement read-only customer context, invoice, subscription, charge, order, refund eligibility, known-issue, and system-status tools.
- [ ] Implement the sandboxed diagnostic tool with strict input validation, timeouts, resource limits, and redacted output.
- [ ] Implement low-risk `create_bug_ticket` with validation, idempotency, and audit logging.
- [ ] Implement `issue_refund` as a policy-aware operation that cannot bypass approval or rate-limit checks.
- [ ] Implement identity verification, password-reset initiation, and account-review flagging.
- [ ] Add explicit tool allowlists for each specialist agent and tests proving unauthorized tools are rejected.
- [ ] Add failure behavior for unavailable services, invalid IDs, timeouts, malformed results, and partial writes.

### Phase 4: Agent orchestration

- [ ] Configure the triage agent to return structured category, urgency, sentiment, summary, and confidence output.
- [ ] Configure billing, technical, refunds, and account/security agents with narrow instructions and their scoped tool sets.
- [ ] Implement handoffs from triage to specialists while preserving ticket data, customer context, and trace ID.
- [ ] Route low-confidence triage results directly to the human queue and skip specialist execution.
- [ ] Implement explicit `escalate_to_human` behavior from any agent.
- [ ] Add loop and budget controls for maximum turns, tool calls, elapsed time, and retry attempts.
- [ ] Define deterministic behavior for model errors, invalid structured output, unavailable tools, and failed handoffs.
- [ ] Add orchestration tests covering each category, low confidence, explicit escalation, tool failure, and budget exhaustion.

### Phase 5: Guardrails and approvals

- [ ] Implement input PII detection and redaction before ticket content reaches an agent prompt.
- [ ] Implement output PII detection and redaction before a response is sent or stored as customer-visible content.
- [ ] Implement the confidence-floor routing guardrail in code, outside agent instructions.
- [ ] Implement the refund threshold guardrail; hold above-threshold refunds before the refund tool executes.
- [ ] Implement hard-coded human-only denies for account deletion, 2FA disablement, email changes, and account unlocks.
- [ ] Implement per-customer/day rate limiting for refunds and account changes across all agents.
- [ ] Implement the response tone/policy guardrail for unsupported promises, legal advice, and unsafe wording.
- [ ] Persist every guardrail decision, reason, redaction, and approval state in the audit trail.
- [ ] Build approval request creation, approval, rejection, expiration, and replay-safe execution flows.
- [ ] Add adversarial tests for PII leakage, threshold bypasses, prompt injection, unauthorized actions, rate-limit bypasses, and infinite loops.

### Phase 6: Response and human handoff

- [ ] Define the final response contract, including customer reply, internal notes, proposed action, escalation reason, and status.
- [ ] Implement auto-send for clean responses and safe actions only after post-output guardrails pass.
- [ ] Implement the escalation queue for low confidence, explicit handoff, guardrail violations, failures, and budget exhaustion.
- [ ] Attach the agent run, tool calls, guardrail flags, and reasoning summary to each escalation or approval request.
- [ ] Add retry and dead-letter handling so tickets never fail silently.
- [ ] Add lifecycle tests for resolved, escalated, in-progress, rejected, and retried tickets.

### Phase 7: Approval dashboard

- [ ] Build a small internal dashboard showing pending approval requests and escalated tickets.
- [ ] Show ticket context, proposed action, risk reason, guardrail flags, tool-call summary, and trace link.
- [ ] Add approve, reject, assign, and resolve actions with reviewer identity and confirmation for irreversible operations.
- [ ] Enforce authorization so only permitted reviewers can approve actions.
- [ ] Make approval actions idempotent and update the API, database, and queue consistently.
- [ ] Add dashboard/API tests for permissions, stale requests, duplicate clicks, rejection, and successful approval.

### Phase 8: Observability and operations

- [ ] Export SDK traces to an OTel-compatible backend and configure local development tracing.
- [ ] Add structured logs with ticket ID, agent run ID, trace ID, tool name, latency, outcome, and guardrail decision.
- [ ] Ensure sensitive values are redacted from logs, traces, dashboard output, and error messages.
- [ ] Add metrics for ingestion failures, routing confidence, escalations, approval latency, tool errors, loop stops, and auto-resolution rate.
- [ ] Add health/readiness endpoints and graceful shutdown behavior.
- [ ] Document operational limits, retention, failure recovery, and how to inspect a trace for one ticket.

### Phase 9: Regression evaluation and delivery

- [ ] Create 30–50 representative tickets across all categories, channels, urgency levels, ambiguous requests, and adversarial cases.
- [ ] Record expected category, confidence outcome, specialist, tool usage, action, escalation status, and response policy result for each sample.
- [ ] Build a repeatable evaluation command that runs the dataset and reports failures by requirement.
- [ ] Add tests for guardrail invariants that must never regress, especially hard denies and PII handling.
- [ ] Add contract tests for ingestion, approval, and tool APIs.
- [ ] Add CI checks for formatting, linting, type checking, unit tests, integration tests, and evaluation thresholds.
- [ ] Add Docker/local orchestration for the API, database, tracing backend, and dashboard.
- [ ] Run an end-to-end demo covering ingestion, triage, specialist tool use, approval, escalation, response, and trace inspection.
- [ ] Document known limitations, model assumptions, cost controls, security boundaries, and next production steps.

### Definition of done

- [ ] A ticket can be ingested, normalized, persisted, triaged, routed, resolved or escalated, and traced end to end.
- [ ] No specialist can call a tool outside its allowlist.
- [ ] PII is redacted before prompts and customer-visible output.
- [ ] Refund thresholds, rate limits, and hard-deny account actions are enforced in code and covered by tests.
- [ ] Human reviewers can approve or reject risky actions without duplicate execution.
- [ ] Model, tool, guardrail, and infrastructure failures produce a visible escalation or retry outcome.
- [ ] The regression suite passes and every ticket decision is explainable from stored audit data and traces.