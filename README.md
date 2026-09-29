# Multi-Agent Customer Support

A customer support triage service built around a router agent, scoped specialist agents, code-enforced guardrails, and human approval.

## Local setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -e ".[dev]"
py -m pytest
```

Start the API with:

```powershell
uvicorn app.main:app --reload
```

The API exposes `GET /health`, `GET /ready`, `GET /metrics`, `POST /tickets`, and `POST /tickets/{ticket_id}/process`.
Processing performs deterministic triage, routes to a scoped specialist, persists agent runs and tool calls, and escalates low-confidence or failed work.

Operational tracing and metrics are emitted in structured JSON logs, and the service records startup, shutdown, request, guardrail, tool, and approval events without writing raw PII into the logs.

## Evaluation and delivery

Run the regression suite and the 30-case evaluation set locally:

```powershell
pytest -q
python scripts/run_evaluation.py --min-pass-rate 1.0
```

The evaluation covers all ticket categories, channels, ambiguous requests, urgency, PII, and adversarial wording. `docker compose up --build` starts the API with SQLite persistence and a local Jaeger tracing backend; trace inspection is available at `http://localhost:16686`.

The deterministic local router is the reference implementation for evaluation. Production deployment still requires a managed database, authenticated reviewer identity, durable log/trace retention, secret management, and a configured model provider. See [docs/operations.md](docs/operations.md) for recovery limits and trace inspection guidance.
