"""Run the deterministic ticket evaluation set and report requirement failures."""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import TicketRepository
from app.orchestration import AgentOrchestrator
from app.schemas import Ticket


def evaluate_case(
    case: dict[str, Any], repository: TicketRepository, settings: Settings
) -> list[str]:
    ticket = repository.save_ticket(
        Ticket.model_validate(
            {
                key: value
                for key, value in case.items()
                if key in {"customer_id", "channel", "subject", "body"}
            }
        )
    )
    result = AgentOrchestrator(repository, settings).process(ticket.id)
    failures: list[str] = []
    expected_category = case["expected_category"]
    with repository._connect() as connection:
        run_rows = connection.execute(
            "SELECT agent_name, output FROM agent_runs WHERE ticket_id = ? ORDER BY rowid",
            (str(ticket.id),),
        ).fetchall()
    triage_output = json.loads(run_rows[0]["output"]) if run_rows and run_rows[0]["output"] else {}
    actual_category = triage_output.get("category")
    if actual_category != expected_category:
        failures.append(f"category expected {expected_category} got {actual_category}")
    specialist_agents = [row["agent_name"] for row in run_rows if row["agent_name"] != "triage"]
    actual_agent = specialist_agents[0] if specialist_agents else None
    if result["status"] != case["expected_status"]:
        failures.append(f"status expected {case['expected_status']} got {result['status']}")
    if actual_agent != case["expected_agent"]:
        failures.append(f"agent expected {case['expected_agent']} got {actual_agent}")
    final_response = result.get("final_response", {})
    if any(
        token in str(final_response).lower() for token in ("guarantee", "legal advice", "[redacted")
    ):
        failures.append("unsafe or unredacted final response")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("evals/tickets.json"))
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    args = parser.parse_args()
    cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    passed = 0
    failures: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        settings = Settings(database_url=f"sqlite:///{Path(directory) / 'evaluation.db'}")
        repository = TicketRepository(settings.database_url)
        for case in cases:
            case_failures = evaluate_case(case, repository, settings)
            if case_failures:
                failures.append({"id": case["id"], "failures": case_failures})
            else:
                passed += 1
    pass_rate = passed / len(cases) if cases else 0.0
    print(
        json.dumps(
            {
                "total": len(cases),
                "passed": passed,
                "failed": len(failures),
                "pass_rate": pass_rate,
                "failures": failures,
            },
            indent=2,
        )
    )
    return 0 if pass_rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
