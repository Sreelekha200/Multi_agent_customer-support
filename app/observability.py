import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from .config import Settings, get_settings
from .guardrails import redact_pii

logger = logging.getLogger("support.observability")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


class _NoopSpan:
    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class Observability:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.metrics: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "value": 0.0, "tags": {}}
        )
        self.tracer: Any | None = None
        self._enable_tracing = False
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

            if self.settings.otel_exporter_otlp_endpoint:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

                exporter: Any = OTLPSpanExporter(
                    endpoint=self.settings.otel_exporter_otlp_endpoint, insecure=True
                )
            else:
                exporter = ConsoleSpanExporter()

            resource = Resource.create({"service.name": self.settings.otel_service_name})
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            self.tracer = trace.get_tracer(self.settings.otel_service_name)
            self._enable_tracing = True
        except (ImportError, RuntimeError):
            self.tracer = None
            self._enable_tracing = False

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact_pii(value)[0]
        if isinstance(value, dict):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def log_event(self, event_type: str, **context: Any) -> dict[str, Any]:
        payload = {"timestamp": datetime.now(UTC).isoformat(), "event_type": event_type}
        for key, value in context.items():
            payload[key] = self.redact(value)
        logger.info(json.dumps(payload, default=str, separators=(",", ":")))
        return payload

    def record_metric(self, metric_name: str, value: float = 1.0, **tags: Any) -> dict[str, Any]:
        entry = self.metrics[metric_name]
        entry["count"] += 1
        entry["value"] += float(value)
        for key, item in tags.items():
            entry["tags"][str(key)] = self.redact(item)
        return dict(entry)

    def metrics_snapshot(self) -> dict[str, Any]:
        return {
            name: {"count": info["count"], "value": info["value"], "tags": dict(info["tags"])}
            for name, info in sorted(self.metrics.items())
        }

    def trace_span(self, name: str, **attributes: Any):
        if self.tracer is None:
            return _NoopSpan()
        return self.tracer.start_as_current_span(
            name,
            attributes={str(key): str(value) for key, value in self.redact(attributes).items()},
        )

    def trace_tool_call(
        self,
        ticket_id: str | None = None,
        trace_id: str | None = None,
        agent_name: str | None = None,
        tool_name: str | None = None,
        latency_ms: int | None = None,
        outcome: str | None = None,
    ) -> None:
        self.log_event(
            "tool_call",
            ticket_id=ticket_id,
            trace_id=trace_id,
            agent_name=agent_name,
            tool_name=tool_name,
            latency_ms=latency_ms,
            outcome=outcome,
        )
        self.record_metric(
            "tool.calls",
            1.0,
            tool=tool_name or "unknown",
            agent=agent_name or "unknown",
            outcome=outcome or "unknown",
        )

    def trace_guardrail(
        self,
        ticket_id: str | None = None,
        trace_id: str | None = None,
        guardrail_name: str | None = None,
        decision: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.log_event(
            "guardrail",
            ticket_id=ticket_id,
            trace_id=trace_id,
            guardrail_name=guardrail_name,
            decision=decision,
            reason=reason,
        )
        self.record_metric(
            "guardrail.events",
            1.0,
            guardrail=guardrail_name or "unknown",
            decision=decision or "unknown",
        )

    def trace_approval(
        self,
        ticket_id: str | None = None,
        approval_id: str | None = None,
        reviewer_id: str | None = None,
        status: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        self.log_event(
            "approval_review",
            ticket_id=ticket_id,
            approval_id=approval_id,
            reviewer_id=reviewer_id,
            status=status,
            latency_ms=latency_ms,
        )
        self.record_metric("approval.events", 1.0, status=status or "unknown")
