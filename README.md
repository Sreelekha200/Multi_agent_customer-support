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

The initial vertical slice exposes `GET /health` and `POST /tickets`.
