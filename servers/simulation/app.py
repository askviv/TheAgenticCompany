"""Authenticated HTTP boundary for the enterprise simulation engine."""

from __future__ import annotations

import hmac
import hashlib
import base64
import logging
import traceback
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from servers.simulation.erpnext import ERPNextConflict, ERPNextError, ERPNextNotFound
from servers.simulation.engine import ActorTurnUncertain, EngineConflict, MailDeliveryUncertain
from servers.simulation.models import (
    AdvanceClock,
    HumanIntervention,
    ApprovalDecision,
    CanonicalEvent,
    ERPAction,
    GoodsReceiptActionParameters,
    PauseClock,
    ShippingCommitActionParameters,
    SendMessage,
    StartScenario,
    ModelUsageRecord,
    PauseCompany,
    StartCompany,
    UpdateCase,
)
from servers.simulation.store import IdempotencyConflict, StoreConflict, StoreNotFound
from servers.simulation.provider_experiments import ProviderExperimentRequest

logger = logging.getLogger(__name__)
Role = Literal["controller", "evaluator", "approver", "agent"]
ERP_READ_OPERATIONS = frozenset({
    "get_purchase_order", "get_purchase_receipt", "get_purchase_invoice", "get_supplier",
    "get_sales_order", "get_customer", "get_stock_balance", "read_payment_status", "assess_invoice",
})
ERP_ACTION_OPERATIONS = frozenset({"create_goods_receipt", "commit_expedited_shipping"})


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str = Field(min_length=1)
    role: Role


class BackgroundControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    enabled: StrictBool


class CoffeeBackgroundRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    previous_operation_id: str = Field(min_length=1, max_length=240)


def create_app(engine: Any, principals: dict[str, Principal | dict[str, str]]) -> FastAPI:
    """Create the API with an explicit configured bearer-token principal map.

    There are no default tokens or environment-derived privileged identities.
    The caller owns token configuration and secret storage.
    """
    if not principals:
        raise ValueError("an explicit principal map is required")
    configured: list[tuple[str, Principal]] = []
    for token, value in principals.items():
        if not token or not token.isascii():
            raise ValueError("principal tokens must be non-empty ASCII strings")
        configured.append((token, Principal.model_validate(value)))

    app = FastAPI(title="Enterprise Simulation API", version="1.0")
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def correlation_context(request: Request, call_next):
        request.state.correlation_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        return response

    def authenticate(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Principal:
        if credentials is None or credentials.scheme.casefold() != "bearer":
            raise HTTPException(status_code=401, detail="Bearer authentication required", headers={"WWW-Authenticate": "Bearer"})
        for token, principal in configured:
            if hmac.compare_digest(token.encode("utf-8"), credentials.credentials.encode("utf-8")):
                return principal
        raise HTTPException(status_code=401, detail="Invalid bearer credentials", headers={"WWW-Authenticate": "Bearer"})

    def idempotency_key(value: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
        if value is None or not value.strip():
            raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        return value.strip()

    def require_role(principal: Principal, *roles: str) -> None:
        if principal.role not in roles:
            raise HTTPException(status_code=403, detail="Operation is not permitted for this principal")

    def load_run(run_id: UUID) -> dict[str, Any]:
        try:
            run = engine.store.get_run(run_id)
        except StoreNotFound as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        manifest = run.get("manifest")
        if isinstance(manifest, dict) and manifest.get("source_kind") in {
            "company_background", "company_provider",
        }:
            # Provider source subjects use hidden run rows only to share the
            # durable evaluation ledger. They are not scenario runs and must
            # never enter ordinary run controls or evaluation routes.
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    def company_payload() -> dict[str, Any]:
        company = engine.company_state()
        if company is None:
            provider_service = getattr(engine, "provider_experiment_service", None)
            provider_experiments, provider_activity = (
                provider_service.company_dashboard() if provider_service is not None else ([], [])
            )
            return {"company_id": "company", "status": "not_started", "simulated_time": None,
                    "paused": False, "erp_site_id": None,
                    "experiments": provider_experiments, "activity": provider_activity}
        experiments = []
        for record in engine.store.company_experiments("company"):
            run = engine.store.get_run(record["experiment_id"])
            prepared = run.get("manifest", {}).get("prepared_problem", {})
            experiments.append({**record, **({"task_id": prepared["task_id"]}
                                               if run["scenario_id"] == "TAU_RETAIL"
                                               and isinstance(prepared, dict) and prepared.get("task_id") else {})})
        activity = []
        public_event_descriptions = {
            "SHIPMENT_DELAYED": "A shipment delay was recorded.",
            "DELIVERY_ARRIVED": "A delivery arrival was recorded.",
            "EMPLOYEE_RESPONSE_READY": "An employee response became available.",
            "PRODUCTION_DEADLINE": "A production deadline was reached.",
            "CUSTOMER_REQUIREMENTS_CHANGED": "A customer requirement changed.",
            "SUPPLIER_PRICING_CHANGED": "A supplier pricing update was recorded.",
            "PAYMENT_POSTED": "A payment event was recorded.",
            "SUPPLIER_MARKET_DEMAND": "A supplier market demand event was recorded.",
            "SUPPLIER_STOCK_REPLENISHED": "Supplier stock replenishment was recorded.",
            "SUPPLIER_SUPPLY_DISRUPTED": "A supplier supply disruption was recorded.",
            "SUPPLIER_DELIVERY_OUTCOME": "A supplier delivery outcome was recorded.",
        }
        for experiment in experiments:
            run_id = experiment["experiment_id"]
            run = engine.store.get_run(run_id)
            for event in engine.store.list_events(run_id, run["generation"], status="completed"):
                description = public_event_descriptions.get(event["event_type"])
                if description:
                    activity.append({"id": f"{run_id}:{event['event_id']}", "timestamp": event["simulated_time"],
                                     "event": event["event_type"], "description": description,
                                     "experiment_id": run_id, "scenario_id": run["scenario_id"]})
            for item in engine.store.list_outbox(run_id, run["generation"], status="completed"):
                message = item["private_payload"]
                activity.append({"id": message.get("message_id", item["outbox_id"]),
                    "timestamp": message.get("simulated_time") or item["created_at"], "event": "MESSAGE_SENT",
                    "description": f"Email sent: {message.get('subject', '')}", "type": "email",
                    "experiment_id": run_id, "scenario_id": run["scenario_id"],
                    "actor": message.get("from_actor_id"), "direction": "sent",
                    "title": message.get("subject", ""), "summary": message.get("subject", ""),
                    "content": message.get("body", "")})
            for message in engine.store.list_private_messages(run_id, run["generation"]):
                subject = str(message.get("subject", ""))
                activity.append({"id": message["message_id"],
                    "timestamp": message.get("simulated_time"), "event": "MESSAGE_RECEIVED",
                    "description": f"Email received: {subject}", "type": "email", "experiment_id": run_id,
                    "scenario_id": run["scenario_id"], "actor": message.get("from_actor_id"),
                    "direction": "received", "title": subject, "summary": subject,
                    "content": message.get("body", "")})
        # Provider cases share company visibility but intentionally use hidden
        # evaluation subjects. Aggregate them only after ordinary runs have
        # been read so they never enter the scenario/event pump above.
        provider_service = getattr(engine, "provider_experiment_service", None)
        provider_experiments, provider_activity = (
            provider_service.company_dashboard() if provider_service is not None else ([], [])
        )
        experiments.extend(provider_experiments)
        activity.extend(provider_activity)
        background_status = None
        background_reader = getattr(engine, "company_background_status", None)
        if callable(background_reader):
            background_status = background_reader()
        background_activity = getattr(engine, "company_background_activity", None)
        if callable(background_activity):
            activity.extend(background_activity())
        issue_activity = getattr(engine, "company_issue_activity", None)
        if callable(issue_activity):
            activity.extend(issue_activity())
        feedback_activity = getattr(engine, "company_feedback_activity", None)
        if callable(feedback_activity):
            activity.extend(feedback_activity())
        activity.sort(key=lambda item: (str(item.get("timestamp", "")), str(item["id"])))
        return {"company_id": "company", "background": background_status, "status": company["status"],
                "simulated_time": company["simulated_time"], "paused": company["paused"],
                "erp_site_id": company["erp_binding"]["site_name"],
                "experiments": experiments, "activity": activity}

    @app.post("/company/evidence/native")
    async def observe_company_evidence(request: Request):
        from .feedback_contracts import NativeDocumentEvent
        from starlette.concurrency import run_in_threadpool
        feedback = getattr(engine, "company_feedback", None)
        if feedback is None:
            raise HTTPException(status_code=503, detail="Company evidence observer is unavailable")
        body = await request.body()
        signature = request.headers.get("X-Frappe-Webhook-Signature", "")
        expected = base64.b64encode(hmac.new(feedback.secret.encode(), body, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="Invalid native evidence signature")
        try:
            event = NativeDocumentEvent.model_validate_json(body)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail="Invalid native evidence envelope") from error
        results = await run_in_threadpool(feedback.accept, event)
        return {"evidence": results}

    @app.get("/company/feedback")
    def get_company_feedback(experiment_id: str | None = None, principal: Principal = Depends(authenticate)):
        require_role(principal, "controller", "evaluator")
        feedback = getattr(engine, "company_feedback", None)
        if feedback is None:
            raise HTTPException(status_code=503, detail="Company evidence observer is unavailable")
        return feedback.report(experiment_id)

    def background_evaluation_service(provider_id: str):
        if provider_id not in {"coffeebench", "ecommercebench"}:
            raise HTTPException(status_code=404, detail="Company evaluation provider not found")
        service = getattr(engine, "background_evaluation_service", None)
        if service is None or provider_id not in getattr(service, "providers", {}):
            raise HTTPException(status_code=503, detail="Company evaluation provider is unavailable")
        return service

    @app.post("/company/evaluations/{provider_id}")
    def start_company_evaluation(
        provider_id: str,
        request: Request,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller", "evaluator")
        service = background_evaluation_service(provider_id)
        evaluation = service.evaluate(
            provider_id, principal.actor_id, key,
            correlation_id=request.state.correlation_id,
        )
        return {"evaluation": evaluation}

    @app.get("/company/evaluations/{provider_id}")
    def list_company_evaluations(
        provider_id: str,
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller", "evaluator")
        service = background_evaluation_service(provider_id)
        return {"evaluations": service.list(provider_id), "scope": "company_global"}

    @app.get("/company/evaluations/{provider_id}/latest")
    def latest_company_evaluation(
        provider_id: str,
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller", "evaluator")
        service = background_evaluation_service(provider_id)
        # This is the newest saved evaluation across source initializations;
        # it does not claim that the saved source is today's active world.
        return {"evaluation": service.latest(provider_id), "selection": "latest_saved"}

    @app.get("/company/evaluations/{provider_id}/{evaluation_id}")
    def get_company_evaluation(
        provider_id: str,
        evaluation_id: str,
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller", "evaluator")
        service = background_evaluation_service(provider_id)
        try:
            evaluation = service.get(evaluation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Company evaluation not found") from exc
        if evaluation.get("provider_id") != provider_id:
            raise HTTPException(status_code=404, detail="Company evaluation not found")
        return {"evaluation": evaluation}

    @app.get("/company")
    def get_company(principal: Principal = Depends(authenticate)):
        require_role(principal, "controller")
        return company_payload()

    @app.post("/company/background/{provider_id}/control", status_code=202)
    def control_company_background(
        provider_id: Literal["coffeebench", "ecommercebench"],
        model: BackgroundControlRequest,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        control = getattr(engine, "company_background_control", None)
        if not callable(control):
            raise HTTPException(status_code=503, detail="Company background controls are unavailable")
        return control(model.enabled, key, provider_id=provider_id)

    @app.post("/company/background/coffeebench/recover")
    def recover_company_coffeebench(
        model: CoffeeBackgroundRecoveryRequest,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        recover = getattr(engine, "company_background_recover", None)
        if not callable(recover):
            raise HTTPException(status_code=503, detail="Company background recovery is unavailable")
        from .background_runtime import BackgroundWorkerError, LegacyEcommerceHandoffIntegrityError
        try:
            outcome = recover(model.previous_operation_id, key)
            if outcome.get("status") == "uncertain":
                return JSONResponse(status_code=202, content={
                    "status": "uncertain", "operation_id": outcome["operation_id"],
                    "code": outcome.get("code", "coffee_recovery_uncertain"),
                })
            if outcome.get("status") == "result_pending":
                return JSONResponse(status_code=202, content={
                    "status": "result_pending", "operation_id": outcome["operation_id"],
                })
            return outcome
        except BackgroundWorkerError as exc:
            if exc.status_code == 202:
                return JSONResponse(status_code=202, content={
                    "status": "uncertain", "code": exc.code,
                })
            status_code = exc.status_code if exc.status_code in {404, 409, 422, 503} else 503
            raise HTTPException(status_code=status_code, detail=exc.code) from exc
        except StoreNotFound as exc:
            raise HTTPException(status_code=404, detail="CoffeeBench pending operation not found") from exc
        except LegacyEcommerceHandoffIntegrityError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except (StoreConflict, IdempotencyConflict) as exc:
            raise HTTPException(status_code=409, detail="CoffeeBench recovery conflicts with current state") from exc

    @app.get("/company/problem-catalog")
    def get_problem_catalog(principal: Principal = Depends(authenticate)):
        require_role(principal, "controller")
        adapters = getattr(engine, "problem_adapters", {})
        adapter = adapters.get("TAU_RETAIL")
        if adapter is None:
            raise HTTPException(status_code=503, detail="Problem catalogue is not configured")
        return adapter.public_catalog()

    def provider_experiment_service():
        service = getattr(engine, "provider_experiment_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Company provider experiments are unavailable")
        return service

    @app.get("/company/provider-experiments/catalog")
    def get_company_provider_experiment_catalog(principal: Principal = Depends(authenticate)):
        require_role(principal, "controller")
        try:
            return provider_experiment_service().catalog()
        except HTTPException:
            raise
        except Exception as exc:
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(exc.__traceback__)]
            logger.error("provider experiment catalogue failed exception_type=%s stack=%s",
                         type(exc).__name__, frames)
            raise HTTPException(status_code=503, detail="Company provider catalogue is unavailable") from exc

    @app.post("/company/provider-experiments")
    def start_company_provider_experiment(
        model: ProviderExperimentRequest,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        try:
            return provider_experiment_service().launch(
                model.model_dump(mode="json"), principal.actor_id, key,
            )
        except HTTPException:
            raise
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail="Invalid provider experiment request") from exc
        except (IdempotencyConflict, StoreConflict) as exc:
            raise HTTPException(status_code=409, detail="Idempotency key conflicts with another request") from exc

    @app.get("/company/provider-experiments")
    def list_company_provider_experiments(principal: Principal = Depends(authenticate)):
        require_role(principal, "controller")
        return {"experiments": provider_experiment_service().list()}

    @app.get("/company/provider-experiments/{experiment_id}")
    def get_company_provider_experiment(experiment_id: str, principal: Principal = Depends(authenticate)):
        require_role(principal, "controller")
        try:
            return {"experiment": provider_experiment_service().get(experiment_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Company provider experiment not found") from exc

    def provider_experiment_for_evaluation(experiment_id: str):
        service = provider_experiment_service()
        row = service.store.get(experiment_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Company provider experiment not found")
        if row["status"] != "registered":
            raise HTTPException(status_code=409, detail="Provider case is not ready for evaluation")
        evaluation_service = getattr(engine, "evaluation_service", None)
        if evaluation_service is None:
            raise HTTPException(status_code=503, detail="Evaluation service is unavailable")
        return evaluation_service

    @app.post("/company/provider-experiments/{experiment_id}/evaluations")
    def evaluate_company_provider_experiment(
        experiment_id: str,
        request: Request,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller", "evaluator")
        service = provider_experiment_for_evaluation(experiment_id)
        result = service.evaluate(experiment_id, principal.actor_id, key,
                                  correlation_id=request.state.correlation_id)
        return {"evaluation": result.record.public_dict()}

    @app.get("/company/provider-experiments/{experiment_id}/evaluations")
    def list_company_provider_evaluations(experiment_id: str, principal: Principal = Depends(authenticate)):
        require_role(principal, "controller", "evaluator")
        service = provider_experiment_for_evaluation(experiment_id)
        return {"evaluations": [record.public_dict() for record in service.list(experiment_id)]}

    @app.get("/company/provider-experiments/{experiment_id}/evaluations/latest")
    def latest_company_provider_evaluation(experiment_id: str, principal: Principal = Depends(authenticate)):
        require_role(principal, "controller", "evaluator")
        service = provider_experiment_for_evaluation(experiment_id)
        record = service.latest(experiment_id)
        return {"evaluation": record.public_dict() if record else None}

    @app.get("/company/provider-experiments/{experiment_id}/evaluations/{evaluation_id}")
    def get_company_provider_evaluation(
        experiment_id: str,
        evaluation_id: str,
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller", "evaluator")
        service = provider_experiment_for_evaluation(experiment_id)
        try:
            record = service.get_record(evaluation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Provider evaluation not found") from exc
        if record.run_id != experiment_id:
            raise HTTPException(status_code=404, detail="Provider evaluation not found")
        return {"evaluation": record.public_dict()}

    @app.post("/company/start")
    def start_company(_body: StartCompany, key: str = Depends(idempotency_key),
                      principal: Principal = Depends(authenticate)):
        require_role(principal, "controller")
        binding = getattr(engine, "company_binding", None)
        if binding is None:
            raise HTTPException(status_code=503, detail="Company ERP binding is not configured")
        current = engine.company_state()
        if current is None:
            engine.start_company(binding, key)
        elif current["paused"]:
            engine.pause_company(False, key)
        else:
            engine.start_company(binding, key)
        return company_payload()

    @app.post("/company/pause")
    def pause_company(model: PauseCompany, key: str = Depends(idempotency_key),
                      principal: Principal = Depends(authenticate)):
        require_role(principal, "controller")
        engine.pause_company(model.paused, key)
        return company_payload()

    @app.post("/company/experiments")
    def start_company_experiment(model: StartScenario, key: str = Depends(idempotency_key),
                                 principal: Principal = Depends(authenticate)):
        require_role(principal, "controller")
        try:
            run = engine.start_company_experiment(model, key, principal.actor_id)
        except EngineConflict as exc:
            raise HTTPException(status_code=409, detail="Experiment conflicts with company state") from exc
        response = {"experiment_id": run["run_id"], "scenario_id": run["scenario_id"],
                    "generation": run["generation"], "status": run["status"],
                    "simulated_time": run["simulated_time"], "paused": run["paused"],
                    "erp_site_id": run["erp_binding"]["site_name"]}
        if model.task_id is not None:
            response["task_id"] = model.task_id
        return response

    def authorize_run(
        run_id: UUID,
        principal: Principal,
        *,
        agent_allowed: bool = False,
        privileged_roles: tuple[str, ...] = ("controller",),
    ) -> dict[str, Any]:
        run = load_run(run_id)
        if principal.role == "agent":
            if not agent_allowed:
                raise HTTPException(status_code=403, detail="Operation is not permitted for this principal")
            if run["agent_id"] != principal.actor_id:
                # Do not confirm another agent's run exists.
                raise HTTPException(status_code=404, detail="Run not found")
        elif principal.role not in privileged_roles:
            raise HTTPException(status_code=403, detail="Operation is not permitted for this principal")
        return run

    def log_operational_failure(request: Request, exc: Exception) -> str:
        correlation_id = getattr(request.state, "correlation_id", "unavailable")
        cause_types: list[str] = []
        cause = exc.__cause__ or exc.__context__
        seen: set[int] = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            cause_types.append(type(cause).__name__)
            cause = cause.__cause__ or cause.__context__
        logger.error(
            "simulation operation failed correlation_id=%s exception_type=%s cause_types=%s frames=%s",
            correlation_id,
            type(exc).__name__,
            ",".join(cause_types),
            ";".join(
                f"{frame.filename}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(exc.__traceback__)
            ),
        )
        return correlation_id

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        # Return locations/types only: Pydantic's default response echoes the
        # rejected input, which may contain private case or message content.
        errors = [{"loc": list(error.get("loc", ())), "type": error.get("type", "invalid")}
                  for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(StoreNotFound)
    async def not_found_handler(_request: Request, _exc: StoreNotFound):
        return JSONResponse(status_code=404, content={"detail": "Record not found"})

    @app.exception_handler(StoreConflict)
    @app.exception_handler(IdempotencyConflict)
    async def conflict_handler(_request: Request, _exc: Exception):
        return JSONResponse(status_code=409, content={"detail": "Operation conflicts with persisted state"})

    @app.exception_handler(EngineConflict)
    async def engine_conflict_handler(_request: Request, _exc: EngineConflict):
        return JSONResponse(status_code=409, content={"detail": "Simulation operation conflicts with current state"})

    @app.exception_handler(ERPNextNotFound)
    async def erp_not_found_handler(_request: Request, _exc: ERPNextNotFound):
        return JSONResponse(status_code=404, content={"detail": "ERP record not found"})

    @app.exception_handler(ERPNextConflict)
    async def erp_conflict_handler(_request: Request, _exc: ERPNextConflict):
        return JSONResponse(status_code=409, content={"detail": "ERP operation conflicts with current state"})

    @app.exception_handler(ERPNextError)
    async def erp_error_handler(request: Request, exc: ERPNextError):
        correlation_id = log_operational_failure(request, exc)
        return JSONResponse(
            status_code=502,
            content={"detail": "ERP operation failed", "correlation_id": correlation_id},
        )

    @app.exception_handler(MailDeliveryUncertain)
    async def mail_delivery_uncertain_handler(request: Request, exc: MailDeliveryUncertain):
        correlation_id = log_operational_failure(request, exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Message delivery is uncertain; reconcile the mailbox receipt before retrying",
                "correlation_id": correlation_id,
            },
        )

    @app.exception_handler(ActorTurnUncertain)
    async def actor_turn_uncertain_handler(request: Request, exc: ActorTurnUncertain):
        correlation_id = log_operational_failure(request, exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "External actor turn is pending reconciliation; retry the owning operation",
                "correlation_id": correlation_id,
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        correlation_id = log_operational_failure(request, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal simulation error", "correlation_id": correlation_id},
        )

    @app.post("/scenarios/start")
    def start_scenario(
        model: StartScenario,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        return engine.start(model, key, principal.actor_id)

    @app.post("/events")
    def enqueue_event(
        model: CanonicalEvent,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        load_run(model.run_id)
        return engine.enqueue_event(model, key)

    @app.post("/clock/advance")
    def advance_clock(
        model: AdvanceClock,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        load_run(model.run_id)
        return engine.advance(model, key)

    @app.post("/clock/pause")
    def pause_clock(
        model: PauseClock,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        load_run(model.run_id)
        return engine.pause(model, key)

    @app.get("/events")
    def list_pending_events(
        run_id: UUID = Query(...), principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        load_run(run_id)
        return engine.list_pending(run_id)

    @app.get("/agents/{actor_id}/inbox")
    def inbox(
        actor_id: str, run_id: UUID = Query(...), principal: Principal = Depends(authenticate),
    ):
        if principal.role not in ("controller", "agent"):
            raise HTTPException(status_code=403, detail="Operation is not permitted for this principal")
        authorize_run(run_id, principal, agent_allowed=True, privileged_roles=("controller",))
        if principal.role == "agent" and actor_id != principal.actor_id:
            raise HTTPException(status_code=404, detail="Inbox not found")
        return engine.inbox(run_id, actor_id)

    @app.get("/cases/{case_id}")
    def case(
        case_id: str, run_id: UUID = Query(...), principal: Principal = Depends(authenticate),
    ):
        authorize_run(run_id, principal, agent_allowed=True, privileged_roles=("controller", "evaluator"))
        return engine.case(run_id, case_id)

    @app.patch("/cases/{case_id}")
    def update_case(
        case_id: str,
        model: UpdateCase,
        run_id: UUID = Query(...),
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        authorize_run(run_id, principal, agent_allowed=True, privileged_roles=("controller",))
        return engine.update_case(run_id, case_id, model, key, principal.actor_id)

    @app.post("/messages")
    def send_message(
        model: SendMessage,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        authorize_run(model.run_id, principal, agent_allowed=True, privileged_roles=("controller",))
        return engine.send_message(model, key, principal.actor_id)

    @app.get("/runs/{run_id}/erp/{operation}/{entity_id}")
    def erp_read(
        run_id: UUID, operation: str, entity_id: str, principal: Principal = Depends(authenticate),
    ):
        if operation not in ERP_READ_OPERATIONS:
            raise HTTPException(status_code=404, detail="ERP operation not found")
        authorize_run(
            run_id,
            principal,
            agent_allowed=operation != "assess_invoice",
            privileged_roles=("controller", "evaluator"),
        )
        return engine.erp_read(run_id, operation, entity_id, principal.actor_id)

    @app.post("/runs/{run_id}/erp/actions/{operation}")
    def erp_action(
        run_id: UUID,
        operation: str,
        model: ERPAction,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        if operation not in ERP_ACTION_OPERATIONS:
            raise HTTPException(status_code=404, detail="ERP action not found")
        authorize_run(run_id, principal, agent_allowed=True, privileged_roles=("controller",))
        if model.run_id != run_id:
            raise HTTPException(status_code=409, detail="ERP action run does not match the requested run")
        parameter_model = (GoodsReceiptActionParameters if operation == "create_goods_receipt"
                           else ShippingCommitActionParameters)
        try:
            parameter_model.model_validate(model.parameters)
        except ValidationError as error:
            errors = error.errors()
            for item in errors:
                item["loc"] = ("body", "parameters", *item.get("loc", ()))
            raise RequestValidationError(errors) from error
        return engine.erp_action(model, operation, key, principal.actor_id)

    @app.post("/runs/{run_id}/invoice-review/{invoice_id}")
    def request_invoice_review(
        run_id: UUID,
        invoice_id: str,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        authorize_run(run_id, principal, agent_allowed=True, privileged_roles=("controller",))
        return engine.request_review(run_id, invoice_id, key, principal.actor_id)

    @app.post("/approvals")
    def approve(
        model: ApprovalDecision,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "approver", "controller")
        load_run(model.run_id)
        return engine.approve(model, key, principal.actor_id)

    @app.post("/scenarios/{run_id}/reset")
    def reset_scenario(
        run_id: UUID,
        key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        load_run(run_id)
        return engine.reset(run_id, key, principal.actor_id)

    @app.post("/scenarios/{run_id}/evaluate")
    def evaluate_scenario(run_id: UUID, request: Request, key: str = Depends(idempotency_key),
                          generation: int | None = Query(default=None, ge=0),
                          principal: Principal = Depends(authenticate)):
        require_role(principal, "controller", "evaluator")
        load_run(run_id)
        service = getattr(engine, "evaluation_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Evaluation service is unavailable")
        result = service.evaluate(str(run_id), principal.actor_id, key,
                                  requested_generation=generation,
                                  correlation_id=request.state.correlation_id)
        return {"evaluation": result.record.public_dict()}

    @app.post("/runs/{run_id}/evaluations")
    def start_evaluation(run_id: UUID, request: Request, key: str = Depends(idempotency_key),
                         generation: int | None = Query(default=None, ge=0),
                         principal: Principal = Depends(authenticate)):
        require_role(principal, "controller", "evaluator")
        load_run(run_id)
        service = getattr(engine, "evaluation_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Evaluation service is unavailable")
        result = service.evaluate(str(run_id), principal.actor_id, key,
                                  requested_generation=generation,
                                  correlation_id=request.state.correlation_id)
        return {"evaluation": result.record.public_dict()}

    @app.get("/runs/{run_id}/evaluations")
    def list_evaluations(run_id: UUID, generation: int | None = Query(default=None, ge=0),
                         principal: Principal = Depends(authenticate)):
        require_role(principal, "controller", "evaluator")
        load_run(run_id)
        selected = generation
        service = getattr(engine, "evaluation_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Evaluation service is unavailable")
        return {"evaluations": [record.public_dict()
                                for record in service.list(str(run_id), generation=generation)]}

    @app.get("/runs/{run_id}/evaluations/latest")
    def latest_evaluation(run_id: UUID, generation: int | None = Query(default=None, ge=0),
                          principal: Principal = Depends(authenticate)):
        require_role(principal, "controller", "evaluator")
        run = load_run(run_id)
        selected = run["generation"] if generation is None else generation
        service = getattr(engine, "evaluation_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Evaluation service is unavailable")
        record = service.latest(str(run_id), generation=selected)
        return {"evaluation": record.public_dict() if record is not None else None}

    @app.get("/evaluations/{evaluation_id}")
    def get_evaluation(evaluation_id: str, principal: Principal = Depends(authenticate)):
        require_role(principal, "controller", "evaluator")
        service = getattr(engine, "evaluation_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Evaluation service is unavailable")
        try:
            record = service.get_record(evaluation_id)
        except StoreNotFound as exc:
            raise HTTPException(status_code=404, detail="Evaluation not found") from exc
        authorize_run(UUID(record.run_id), principal, privileged_roles=("controller", "evaluator"))
        return {"evaluation": record.public_dict()}

    @app.get("/scenarios/{run_id}/trajectory")
    def private_trajectory(run_id: UUID, principal: Principal = Depends(authenticate)):
        require_role(principal, "evaluator")
        load_run(run_id)
        return engine.trajectory(run_id)

    @app.post("/scenarios/{run_id}/human-interventions")
    def record_human_intervention(
        run_id: UUID, model: HumanIntervention,
        key: str = Depends(idempotency_key), principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        load_run(run_id)
        return engine.record_human_intervention(run_id, model, key, principal.actor_id)

    @app.post("/scenarios/{run_id}/model-usage")
    def record_model_usage(
        run_id: UUID, model: ModelUsageRecord,
        key: str = Depends(idempotency_key), principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        load_run(run_id)
        return engine.record_model_usage(run_id, model, key, principal.actor_id)

    @app.post("/mail/pump")
    def pump_mail(
        run_id: UUID = Query(...),
        _key: str = Depends(idempotency_key),
        principal: Principal = Depends(authenticate),
    ):
        require_role(principal, "controller")
        load_run(run_id)
        # The durable outbox/attempt marker supplies per-message idempotency;
        # this endpoint still requires an idempotency key for request tracking.
        return engine.pump(run_id)

    return app
