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

The API exposes `GET /health`, `POST /tickets`, and `POST /tickets/{ticket_id}/process`.
Processing performs deterministic triage, routes to a scoped specialist, persists agent runs and tool calls, and escalates low-confidence or failed work.
