from fastapi import FastAPI, Header, HTTPException, Response, status

from .config import get_settings
from .database import TicketRepository
from .orchestration import AgentOrchestrator, OrchestrationError
from .schemas import Ticket, TicketCreate

settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0")
ticket_repository = TicketRepository(settings.database_url)
orchestrator = AgentOrchestrator(ticket_repository, settings)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}


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
            return existing_ticket

    normalized_ticket = Ticket.model_validate(ticket.model_dump())
    return ticket_repository.save_ticket(normalized_ticket)


@app.post("/tickets/{ticket_id}/process")
def process_ticket(ticket_id: str) -> dict[str, object]:
    try:
        from uuid import UUID
        return orchestrator.process(UUID(ticket_id))
    except (ValueError, OrchestrationError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
