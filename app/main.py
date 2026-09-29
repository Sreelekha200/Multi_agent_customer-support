import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from .config import get_settings
from .database import TicketRepository
from .observability import Observability
from .orchestration import AgentOrchestrator, OrchestrationError
from .schemas import ApprovalStatus, Ticket, TicketCreate

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    observability.log_event("service_startup", environment=settings.app_env, service_name=settings.otel_service_name)
    yield
    observability.log_event("service_shutdown", environment=settings.app_env)


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
ticket_repository = TicketRepository(settings.database_url)
observability = Observability(settings)
orchestrator = AgentOrchestrator(ticket_repository, settings)
startup_time = time.monotonic()


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    latency_ms = int((time.perf_counter() - started) * 1000)
    observability.record_metric(
        "http.requests",
        1.0,
        method=request.method,
        path=request.url.path,
        status=str(response.status_code),
    )
    observability.log_event(
        "http_request",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        latency_ms=latency_ms,
    )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    observability.log_event("health_check", status="ok", environment=settings.app_env)
    return {"status": "ok", "environment": settings.app_env}


@app.get("/ready")
def readiness() -> dict[str, object]:
    return {"status": "ready", "database": "ok", "uptime_seconds": round(time.monotonic() - startup_time, 3), "metrics": observability.metrics_snapshot()}


@app.get("/metrics")
def metrics() -> dict[str, object]:
    return {"status": "ok", "uptime_seconds": round(time.monotonic() - startup_time, 3), "metrics": observability.metrics_snapshot()}


@app.post("/tickets", response_model=Ticket, status_code=status.HTTP_201_CREATED)
def create_ticket(
    ticket: TicketCreate,
    response: Response,
    x_ingestion_key: str | None = Header(default=None),
) -> Ticket:
    if x_ingestion_key != settings.ingestion_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid ingestion key")

    if ticket.external_id:
        existing_ticket = ticket_repository.get_ticket_by_external_id(ticket.external_id)
        if existing_ticket:
            response.status_code = status.HTTP_200_OK
            observability.log_event("ingestion_duplicate", customer_id=ticket.customer_id, external_id=ticket.external_id)
            return existing_ticket

    normalized_ticket = Ticket.model_validate(ticket.model_dump())
    saved = ticket_repository.save_ticket(normalized_ticket)
    observability.log_event("ticket_created", ticket_id=str(saved.id), trace_id=str(saved.trace_id), customer_id=saved.customer_id)
    return saved


@app.post("/tickets/{ticket_id}/process")
def process_ticket(ticket_id: str) -> dict[str, object]:
    try:
        from uuid import UUID
        result = orchestrator.process(UUID(ticket_id))
        observability.log_event("ticket_processed", ticket_id=ticket_id, status=result.get("status"), agent=result.get("agent"))
        observability.record_metric("tickets.processed", 1.0, status=str(result.get("status")))
        return result
    except (ValueError, OrchestrationError) as error:
        observability.log_event("ticket_processing_error", ticket_id=ticket_id, error=str(error))
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get("/tickets/{ticket_id}/queue")
def get_ticket_queue(ticket_id: str) -> list[dict[str, object]]:
    try:
        from uuid import UUID
        return ticket_repository.get_escalation_queue(UUID(ticket_id))
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post("/tickets/{ticket_id}/retry")
def retry_ticket(ticket_id: str) -> dict[str, object]:
    try:
        from uuid import UUID
        return ticket_repository.retry_ticket(UUID(ticket_id), "manual_retry")
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


def _require_reviewer(reviewer_id: str | None, allowed: set[str] | None = None) -> None:
    if not reviewer_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Reviewer identity is required")
    if allowed is not None and reviewer_id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reviewer is not authorized")


@app.get("/dashboard")
def dashboard(x_reviewer_id: str | None = Header(default=None, alias="X-Reviewer-Id")) -> dict[str, object]:
    _require_reviewer(x_reviewer_id, {"support-lead", "support-manager", "reviewer-1"})
    pending = [
        {
            "id": str(item["id"]),
            "customer_id": item["customer_id"],
            "action": item["action"],
            "proposed_action": item["proposed_action"],
            "risk_reason": item["risk_reason"],
            "status": item["status"],
        }
        for item in ticket_repository.get_approval_requests_by_status("pending")
    ]
    escalated = [
        {
            "ticket_id": str(item["ticket_id"]),
            "reason": item["reason"],
            "status": item["status"],
        }
        for item in ticket_repository.get_escalation_queue()
        if item.get("status") in {"pending", "dead_letter"}
    ]
    return {"pending_approvals": pending, "escalated_tickets": escalated}


@app.post("/approvals/{approval_id}/approve")
def approve_approval(approval_id: str, payload: dict[str, object], x_reviewer_id: str | None = Header(default=None, alias="X-Reviewer-Id")) -> dict[str, object]:
    _require_reviewer(x_reviewer_id, {"support-lead", "support-manager", "reviewer-1"})
    reviewer_id = str(payload.get("reviewer_id") or x_reviewer_id)
    if payload.get("confirm") is not True:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Confirmation required")

    request = ticket_repository.get_approval_request_obj(approval_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")
    if request["status"] == ApprovalStatus.APPROVED.value or request["status"] == ApprovalStatus.REJECTED.value:
        return {"id": approval_id, "status": request["status"], "reviewer_id": request["reviewer_id"]}
    if ticket_repository.is_approval_expired_by_id(approval_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approval request expired")

    updated = ticket_repository.update_approval_request_status(approval_id, ApprovalStatus.APPROVED, reviewer_id)
    return {"id": approval_id, "status": updated["status"], "reviewer_id": reviewer_id}


@app.post("/approvals/{approval_id}/reject")
def reject_approval(approval_id: str, payload: dict[str, object], x_reviewer_id: str | None = Header(default=None, alias="X-Reviewer-Id")) -> dict[str, object]:
    _require_reviewer(x_reviewer_id, {"support-lead", "support-manager", "reviewer-1"})
    reviewer_id = str(payload.get("reviewer_id") or x_reviewer_id)

    request = ticket_repository.get_approval_request_obj(approval_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")
    if request["status"] == ApprovalStatus.REJECTED.value:
        return {"id": approval_id, "status": ApprovalStatus.REJECTED.value, "reviewer_id": request["reviewer_id"]}
    if ticket_repository.is_approval_expired_by_id(approval_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approval request expired")

    updated = ticket_repository.update_approval_request_status(approval_id, ApprovalStatus.REJECTED, reviewer_id)
    return {"id": approval_id, "status": updated["status"], "reviewer_id": reviewer_id}
