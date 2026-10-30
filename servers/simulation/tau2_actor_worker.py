"""Isolated FastAPI worker running the pinned τ-bench UserSimulator."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from servers.simulation.actor_protocol import (
    ActorEvaluateRequest, ActorEvaluateResponse, ActorTurnRequest, ActorTurnResponse,
    ActorUsage, Tau2RetailEvaluateRequest, Tau2RetailSeedRequest,
    Tau2RetailFeedbackActionRequest, Tau2RetailFeedbackInitializeRequest,
    Tau2RetailFeedbackReconcileRequest, Tau2RetailFeedbackEvaluateRequest,
    bearer_token_matches,
)
from servers.simulation.actor_worker_store import ActorTurnStore, canonical_request_hash
from servers.simulation.provider_contracts import (
    ProviderRequest, ProviderResponse, ProviderTaskCatalog,
    ProviderCasePrepareRequest, ProviderCasePrepareResponse,
    ProviderEvaluationSnapshotRequest, ProviderEvaluationSnapshotReceipt,
    ProviderEvaluationSnapshotCheck, ProviderEvaluationSnapshotVerification,
    ProviderEvaluationResult, CompanyProviderPortalAction, ProviderBankActionReceipt,
)


class Tau2CustomerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    model: str | None = Field(default=None, min_length=1, max_length=240)
    codex_executable: str | None = Field(default=None, min_length=1, max_length=500)
    codex_home: Path | None = None
    codex_timeout_seconds: float | None = Field(default=None, gt=0)
    bearer_token: SecretStr | None = Field(default=None, min_length=32, max_length=256)

    @classmethod
    def from_environment(cls) -> "Tau2CustomerSettings":
        return cls(
            model=os.getenv("TAU2_CUSTOMER_MODEL"),
            codex_executable=os.getenv("TAU2_CODEX_EXECUTABLE"),
            codex_home=os.getenv("TAU2_CODEX_HOME"),
            codex_timeout_seconds=(
                float(os.environ["TAU2_CODEX_TIMEOUT_SECONDS"])
                if os.getenv("TAU2_CODEX_TIMEOUT_SECONDS") else None
            ),
            bearer_token=os.getenv("TAU2_ACTOR_BEARER_TOKEN"),
        )


def _private_state_json(state: Any) -> dict[str, Any]:
    # τ's raw_data can contain provider response objects. The conversational
    # state is preserved, but raw provider payloads are intentionally omitted.
    result = state.model_dump(mode="json")
    for message in result.get("system_messages", []):
        message.pop("raw_data", None)
    for message in result.get("messages", []):
        message.pop("raw_data", None)
    return result


def _usage(message: Any, model: str) -> dict[str, Any] | None:
    usage = message.usage if isinstance(message.usage, dict) else {}
    result = {
        "provider": "codex-cli",
        "model_id": model,
        "model_version": None,
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
        # LiteLLM's custom-provider fallback reports an unpriced model as
        # 0.0. A Codex subscription has no per-turn currency price available,
        # so the worker deliberately does not propagate that synthetic zero.
        "cost": None,
        "currency": None,
    }
    if all(result[key] is None for key in ("input_tokens", "output_tokens", "cost")):
        return None
    return ActorUsage.model_validate(result).model_dump(mode="json")


def create_app(
    settings: Tau2CustomerSettings,
    database_path: str | Path,
    *,
    codex_model=None,
) -> FastAPI:
    """Create worker app using actual τ calls and the configured Codex CLI model."""
    from loguru import logger

    # τ's published English scaffold is source data, not included by its
    # installed wheel. Use the exact pinned upstream pack shipped alongside
    # the worker unless an operator explicitly mounts another τ data root.
    bundled_data = Path(__file__).parent / "tau2_actor_runtime" / "data"
    os.environ.setdefault("TAU2_DATA_DIR", str(bundled_data))

    # Upstream debug logs include full message text. Keep all actor content out
    # of process logs even when the worker is launched with debug log levels.
    logger.remove()
    database = ActorTurnStore(database_path)
    from servers.simulation.provider_store import NativeProviderStore
    from servers.simulation.tau2_external_provider import Tau2ExternalProvider
    native_provider = Tau2ExternalProvider(NativeProviderStore(database_path))
    app = FastAPI(title="τ2 customer actor worker", version="1")
    app.state.settings = settings
    if codex_model is None and settings.model and settings.codex_executable and settings.codex_home and settings.codex_timeout_seconds:
        from servers.simulation.codex_model import CodexCLIModel, CodexCLISettings

        codex_model = CodexCLIModel(CodexCLISettings(
            executable=settings.codex_executable,
            model=settings.model,
            codex_home=settings.codex_home,
            timeout_seconds=settings.codex_timeout_seconds,
        ))
    if codex_model is not None:
        from servers.simulation.tau2_codex_provider import register_codex_provider

        register_codex_provider(codex_model)
    app.state.codex_provider_registered = codex_model is not None
    app.state.native_provider = native_provider

    @app.get("/health")
    def health():
        configured = bool(
            settings.model and settings.codex_executable and settings.codex_home
            and settings.codex_timeout_seconds and settings.bearer_token
            and settings.codex_home.is_dir()
        )
        return {
            "status": "ok",
            "actor_type": "customer",
            "provider_configured": configured,
            "model_id": settings.model,
        }

    def require_worker_auth(authorization: str | None) -> None:
        if settings.bearer_token is None:
            raise HTTPException(status_code=503, detail="actor worker authentication is not configured")
        if not bearer_token_matches(authorization, settings.bearer_token.get_secret_value()):
            raise HTTPException(status_code=401, detail="actor worker authentication failed")

    @app.post("/v1/external-provider/message", response_model=ProviderResponse)
    def external_provider_message(
        request: ProviderRequest,
        authorization: str | None = Header(default=None),
    ):
        """Handle one employee message through the pinned native provider agent."""
        require_worker_auth(authorization)
        if not app.state.codex_provider_registered:
            raise HTTPException(status_code=503, detail="Codex provider is not configured")
        try:
            return app.state.native_provider.handle(request)
        except Exception as exc:
            from servers.simulation.tau2_external_provider import ExternalProviderError, ProviderTaskNotFound
            if isinstance(exc, ProviderTaskNotFound):
                raise HTTPException(status_code=404, detail="native provider task was not found") from exc
            if isinstance(exc, ExternalProviderError):
                raise HTTPException(status_code=409, detail="native provider conversation conflicts with existing state") from exc
            import logging
            import traceback

            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).error(
                "native external provider message failed provider_id=%s operation_id=%s exception_type=%s frames=%s",
                request.provider_id, request.operation_id, type(exc).__name__, frames,
            )
            raise HTTPException(status_code=500, detail="native provider message failed") from exc

    @app.post("/v1/external-provider/prepare", response_model=ProviderCasePrepareResponse)
    def external_provider_prepare(
        request: ProviderCasePrepareRequest,
        authorization: str | None = Header(default=None),
    ):
        """Initialize a native τ task and read its account/forms without model generation."""
        require_worker_auth(authorization)
        try:
            return app.state.native_provider.prepare_case(request)
        except Exception as exc:
            from servers.simulation.tau2_external_provider import ExternalProviderError, ProviderTaskNotFound
            if isinstance(exc, ProviderTaskNotFound):
                raise HTTPException(status_code=404, detail="native provider task was not found") from exc
            if isinstance(exc, ExternalProviderError):
                raise HTTPException(status_code=409, detail="native provider case preparation conflicts with existing state") from exc
            import logging
            import traceback

            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).error(
                "native provider preparation failed provider_id=%s task_id=%s exception_type=%s frames=%s",
                request.provider_id, request.task_id, type(exc).__name__, frames,
            )
            raise HTTPException(status_code=500, detail="native provider preparation failed") from exc

    @app.get("/v1/external-provider/conversations/{conversation_id}/evaluation-snapshot",
             response_model=ProviderEvaluationSnapshotReceipt)
    def external_provider_evaluation_snapshot(
        conversation_id: str,
        provider_id: str = Query(min_length=1, max_length=80),
        authorization: str | None = Header(default=None),
    ):
        require_worker_auth(authorization)
        request = ProviderEvaluationSnapshotRequest(provider_id=provider_id, conversation_id=conversation_id)
        try:
            return app.state.native_provider.capture_evaluation_snapshot(
                request.provider_id, request.conversation_id,
            )
        except Exception as exc:
            from servers.simulation.tau2_external_provider import ProviderTaskNotFound
            if isinstance(exc, ProviderTaskNotFound):
                raise HTTPException(status_code=404, detail="native provider conversation was not found") from exc
            import logging
            logging.getLogger(__name__).exception(
                "native provider evaluation capture failed provider_id=%s conversation_id=%s",
                request.provider_id, request.conversation_id,
            )
            raise HTTPException(status_code=500, detail="native provider evaluation capture failed") from exc

    @app.post("/v1/external-provider/evaluate-captured", response_model=ProviderEvaluationResult)
    def external_provider_evaluate_captured(
        request: ProviderEvaluationSnapshotCheck,
        authorization: str | None = Header(default=None),
    ):
        require_worker_auth(authorization)
        try:
            from tau2.data_model.tasks import RewardType
            snapshot_row = app.state.native_provider.store.evaluation_snapshot(request.capture_ref)
            if snapshot_row is None:
                raise HTTPException(status_code=404, detail="native provider evaluation capture was not found")
            captured = snapshot_row["snapshot"]
            task = app.state.native_provider._load_task(captured["provider_id"], captured["task_id"])
            criteria = task.evaluation_criteria
            needs_model_judge = bool(
                criteria and (
                    (RewardType.NL_ASSERTION in criteria.reward_basis and criteria.nl_assertions)
                    or (RewardType.COMMUNICATE in criteria.reward_basis and criteria.communicate_info)
                )
            )
            if needs_model_judge and not app.state.codex_provider_registered:
                raise HTTPException(
                    status_code=503,
                    detail="native provider model-based evaluator requires the configured Codex provider",
                )
            return app.state.native_provider.evaluate_captured(request)
        except HTTPException:
            raise
        except Exception as exc:
            from servers.simulation.tau2_external_provider import ExternalProviderError, ProviderTaskNotFound
            if isinstance(exc, ProviderTaskNotFound):
                raise HTTPException(status_code=404, detail="native provider task was not found") from exc
            if isinstance(exc, ExternalProviderError):
                raise HTTPException(status_code=409, detail="native provider evaluation capture is invalid") from exc
            import logging
            logging.getLogger(__name__).exception("native provider captured evaluation failed")
            raise HTTPException(status_code=500, detail="native provider captured evaluation failed") from exc

    @app.post("/v1/external-provider/verify-evaluation-snapshot",
              response_model=ProviderEvaluationSnapshotVerification)
    def external_provider_verify_evaluation_snapshot(
        request: ProviderEvaluationSnapshotCheck,
        authorization: str | None = Header(default=None),
    ):
        require_worker_auth(authorization)
        return app.state.native_provider.verify_evaluation_snapshot(request)

    @app.post("/v1/external-provider/portal-action", response_model=ProviderBankActionReceipt)
    def external_provider_portal_action(
        request: CompanyProviderPortalAction,
        authorization: str | None = Header(default=None),
    ):
        require_worker_auth(authorization)
        try:
            return app.state.native_provider.apply_company_portal_action(request)
        except Exception as exc:
            from servers.simulation.tau2_external_provider import ExternalProviderError, ProviderTaskNotFound
            if isinstance(exc, ProviderTaskNotFound):
                raise HTTPException(status_code=404, detail="native provider case was not found") from exc
            if isinstance(exc, ExternalProviderError):
                raise HTTPException(status_code=409, detail="native provider customer action conflicts with case state") from exc
            import logging
            logging.getLogger(__name__).exception(
                "native provider customer action failed provider_id=%s operation_id=%s",
                request.provider_id, request.operation_id,
            )
            raise HTTPException(status_code=500, detail="native provider customer action failed") from exc

    @app.get("/v1/external-provider/catalog", response_model=ProviderTaskCatalog)
    def external_provider_catalog(authorization: str | None = Header(default=None)):
        """Return unchanged task IDs and public descriptions without private task data."""
        require_worker_auth(authorization)
        try:
            return app.state.native_provider.public_task_catalog()
        except Exception as exc:
            import logging
            import traceback

            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).error(
                "native external provider catalogue failed exception_type=%s frames=%s",
                type(exc).__name__, frames,
            )
            raise HTTPException(status_code=500, detail="native provider catalogue failed") from exc

    @app.post("/v1/turn", response_model=ActorTurnResponse)
    def turn(request: ActorTurnRequest, response: Response, authorization: str | None = Header(default=None)):
        require_worker_auth(authorization)
        if request.actor_type != "customer":
            raise HTTPException(status_code=422, detail="actor_type is not supported by this worker")
        if not (settings.model and settings.codex_executable and settings.codex_home
                and settings.codex_timeout_seconds and settings.codex_home.is_dir()):
            raise HTTPException(status_code=503, detail="Codex CLI model environment is not configured")

        from tau2.data_model.tasks import Task

        try:
            task = Task.model_validate(request.scenario)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="scenario is not a valid τ-bench Task") from exc

        request_hash = canonical_request_hash(request.model_dump(mode="json"))
        state, saved = database.claim(request.turn_id, request_hash)
        if state == "conflict":
            raise HTTPException(status_code=409, detail="turn_id was used with a different request")
        if state == "completed":
            return saved
        if state == "started":
            response.status_code = 202
            return ActorTurnResponse(
                turn_id=request.turn_id, status="started_uncertain", error_type="generation_started"
            )

        try:
            from tau2.data_model.message import AssistantMessage
            from tau2.user.user_simulator import UserSimulator
            from tau2.user.user_simulator_base import UserState

            simulator = UserSimulator(
                # Use a private LiteLLM routing alias. If the configured model
                # name is itself recognized as an OpenAI model, LiteLLM routes
                # it through its OpenAI branch before custom handlers.
                llm="tac_codex/codex-cli",
                instructions=str(task.user_scenario.instructions),
                tools=[],
                llm_args={"num_retries": 0},
            )
            if request.state is None:
                history = task.initial_state.message_history if task.initial_state else []
                tau_state = simulator.get_init_state(history)
            else:
                tau_state = UserState.model_validate(request.state)
            inbound_text = request.inbound_message.body
            if request.inbound_message.subject:
                inbound_text = f"Subject: {request.inbound_message.subject}\n\n{inbound_text}"
            message = AssistantMessage(role="assistant", content=inbound_text)
            user_message, tau_state = simulator.generate_next_message(message, tau_state)
            if not user_message.content or not user_message.content.strip():
                raise ValueError("τ UserSimulator returned no customer message content")
            # LiteLLM currently defaults unknown custom-provider prices to
            # 0.0; this CLI account is subscription-backed, not token-priced.
            user_message.cost = None
            result = ActorTurnResponse(
                turn_id=request.turn_id,
                status="completed",
                body=user_message.content,
                state=_private_state_json(tau_state),
                usage=_usage(user_message, settings.model),
            ).model_dump(mode="json")
            database.complete(request.turn_id, request_hash, result)
            return result
        except HTTPException:
            raise
        except Exception as exc:
            # The durable started marker makes retries observational only; a
            # timeout after a provider side effect must never trigger a second
            # model charge. Keep details in process memory/cause chains only.
            import logging
            import traceback

            cause_types = []
            cause = exc.__cause__ or exc.__context__
            while cause is not None:
                cause_types.append(type(cause).__name__)
                cause = cause.__cause__ or cause.__context__
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).error(
                "tau2 customer turn uncertain turn_id=%s exception_type=%s causes=%s frames=%s",
                request.turn_id, type(exc).__name__, cause_types, frames,
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "customer_generation_uncertain", "error_type": type(exc).__name__},
            ) from exc

    @app.get("/v1/retail/catalog")
    def retail_catalog(authorization: str | None = Header(default=None)):
        """Return public task identifiers and provenance only."""
        require_worker_auth(authorization)
        from servers.simulation.tau2_retail import catalog_metadata

        return catalog_metadata()

    @app.get("/v1/retail/tasks/{task_id}/manifest")
    def retail_manifest(task_id: str, authorization: str | None = Header(default=None)):
        """Return task content, gold criteria, and linked source data privately."""
        require_worker_auth(authorization)
        from servers.simulation.tau2_retail import RetailTaskNotFound, task_manifest

        try:
            return task_manifest(task_id)
        except RetailTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="τ retail task was not found") from exc

    @app.post("/v1/retail/seed-email", response_model=ActorTurnResponse)
    def retail_seed_email(
        request: Tau2RetailSeedRequest,
        response: Response,
        authorization: str | None = Header(default=None),
    ):
        """Generate the opening customer email from the pinned Task scenario."""
        require_worker_auth(authorization)
        from servers.simulation.actor_protocol import InboundMessage
        from servers.simulation.tau2_retail import RetailTaskNotFound, get_task

        try:
            task = get_task(request.task_id)
        except RetailTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="τ retail task was not found") from exc
        seeded_turn = ActorTurnRequest(
            turn_id=request.turn_id,
            run_id=request.run_id,
            generation=request.generation,
            actor_id=request.actor_id,
            actor_type="customer",
            scenario=task.model_dump(mode="json"),
            state=None,
            inbound_message=InboundMessage(
                message_id=request.message_id,
                subject="",
                body=(
                    "Write the first email you would send to customer support about the issue "
                    "in your scenario. Begin as the customer and include the relevant details "
                    "and request from your instructions."
                ),
            ),
        )
        # Reuse /v1/turn so the same authenticated UserSimulator, durable turn
        # idempotency, Codex provider, and private state handling apply.
        return turn(seeded_turn, response, authorization)

    @app.post("/v1/retail/evaluate")
    def retail_evaluate(
        request: Tau2RetailEvaluateRequest,
        authorization: str | None = Header(default=None),
    ):
        """Score ERP-projected state and the real email transcript with τ evaluators."""
        require_worker_auth(authorization)
        try:
            from pydantic import TypeAdapter
            from tau2.data_model.message import Message
            from tau2.data_model.tasks import RewardType
            from servers.simulation.tau2_retail import RetailTaskNotFound, evaluate_observed_state, get_task

            task = get_task(request.task_id)
            criteria = task.evaluation_criteria
            needs_nl_judge = bool(
                criteria
                and RewardType.NL_ASSERTION in criteria.reward_basis
                and criteria.nl_assertions
            )
            if needs_nl_judge and not app.state.codex_provider_registered:
                raise HTTPException(
                    status_code=503,
                    detail="τ retail NL assertion evaluator requires the configured Codex provider",
                )
            trajectory = TypeAdapter(list[Message]).validate_python(request.trajectory)
            return evaluate_observed_state(
                request.task_id,
                request.observed_db,
                trajectory,
                evaluate_nl_assertions=True,
                unmapped=request.unmapped,
            )
        except RetailTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="τ retail task was not found") from exc
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="τ retail evaluation input is invalid") from exc
        except HTTPException:
            raise
        except Exception as exc:
            import logging
            import traceback

            cause_types = []
            cause = exc.__cause__ or exc.__context__
            while cause is not None:
                cause_types.append(type(cause).__name__)
                cause = cause.__cause__ or cause.__context__
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).error(
                "tau2 retail evaluation failed run_id=%s actor_id=%s exception_type=%s causes=%s frames=%s",
                request.run_id, request.actor_id, type(exc).__name__, cause_types, frames,
            )
            raise HTTPException(status_code=500, detail="τ retail evaluator failed") from exc

    @app.post("/v1/retail/feedback/initialize")
    def retail_feedback_initialize(
        request: Tau2RetailFeedbackInitializeRequest,
        authorization: str | None = Header(default=None),
    ):
        """Checkpoint the exact native ERP projection for this task execution."""
        require_worker_auth(authorization)
        try:
            from servers.simulation.tau2_retail import RetailTaskNotFound, get_task
            from servers.simulation.tau2_retail_feedback import initialize_native_state

            get_task(request.task_id)
            return initialize_native_state(
                database, request.task_id, request.experiment_id, request.observed_db,
            )
        except RetailTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="τ retail task was not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="τ retail native state conflicts with its ERP projection") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="τ retail native state initialization failed") from exc

    @app.post("/v1/retail/feedback/apply")
    def retail_feedback_apply(
        request: Tau2RetailFeedbackActionRequest,
        authorization: str | None = Header(default=None),
    ):
        """Apply mapped company evidence through τ RetailTools and persist atomically."""
        require_worker_auth(authorization)
        try:
            from servers.simulation.tau2_retail import RetailTaskNotFound, _ensure_upstream_data_root, get_task
            from servers.simulation.tau2_retail_feedback import apply_persisted_native

            get_task(request.task_id)
            _ensure_upstream_data_root()
            result = apply_persisted_native(
                database, request.task_id, request.experiment_id, request.action,
            )
            return result.model_dump(mode="json")
        except RetailTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="τ retail task was not found") from exc
        except LookupError as exc:
            raise HTTPException(status_code=409, detail="τ retail native state is not initialized") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="τ retail native action conflicts with its mapped source state") from exc
        except Exception as exc:
            import logging
            import traceback

            frames = [(frame.filename, frame.lineno, frame.name) for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).error(
                "tau2 retail native apply failed task_id=%s experiment_id=%s exception_type=%s frames=%s",
                request.task_id, request.experiment_id, type(exc).__name__, frames,
            )
            raise HTTPException(status_code=500, detail="τ retail native action failed") from exc

    @app.post("/v1/retail/feedback/reconcile")
    def retail_feedback_reconcile(
        request: Tau2RetailFeedbackReconcileRequest,
        authorization: str | None = Header(default=None),
    ):
        """Return only a previously committed native action receipt."""
        require_worker_auth(authorization)
        from servers.simulation.tau2_retail_feedback import reconcile_persisted_native

        try:
            result = reconcile_persisted_native(
                database, request.task_id, request.experiment_id, request.action,
            )
            if result is None:
                raise HTTPException(status_code=404, detail="τ retail action receipt was not found")
            return result.model_dump(mode="json")
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="τ retail action ID conflicts with its recorded evidence") from exc

    @app.post("/v1/retail/feedback/evaluate")
    def retail_feedback_evaluate(
        request: Tau2RetailFeedbackEvaluateRequest,
        authorization: str | None = Header(default=None),
    ):
        """Evaluate current ERP-observed state against the original τ criteria."""
        require_worker_auth(authorization)
        try:
            from pydantic import TypeAdapter
            from tau2.data_model.message import Message
            from tau2.data_model.tasks import RewardType
            from servers.simulation.tau2_retail import RetailTaskNotFound, evaluate_observed_state, get_task

            task = get_task(request.task_id)
            criteria = task.evaluation_criteria
            needs_nl_judge = bool(
                criteria and RewardType.NL_ASSERTION in criteria.reward_basis
                and criteria.nl_assertions
            )
            if needs_nl_judge and not app.state.codex_provider_registered:
                raise HTTPException(
                    status_code=503,
                    detail="τ retail NL assertion evaluator requires the configured Codex provider",
                )
            trajectory = TypeAdapter(list[Message]).validate_python(request.trajectory)
            result = evaluate_observed_state(
                request.task_id, request.observed_db, trajectory,
                evaluate_nl_assertions=True, unmapped=request.unmapped,
            )
            result["experiment_id"] = request.experiment_id
            return result
        except RetailTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="τ retail task was not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="τ retail evaluation input is invalid") from exc
        except HTTPException:
            raise
        except Exception as exc:
            import logging
            import traceback

            frames = [(frame.filename, frame.lineno, frame.name) for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).error(
                "tau2 retail feedback evaluation failed task_id=%s experiment_id=%s exception_type=%s frames=%s",
                request.task_id, request.experiment_id, type(exc).__name__, frames,
            )
            raise HTTPException(status_code=500, detail="τ retail native evaluation failed") from exc

    @app.post("/v1/evaluate", response_model=ActorEvaluateResponse)
    def evaluate(request: ActorEvaluateRequest, authorization: str | None = Header(default=None)):
        require_worker_auth(authorization)
        try:
            from pydantic import TypeAdapter
            from tau2.data_model.message import Message
            from tau2.data_model.semantic_communication import CommunicateScorer, CommunicateScoring
            from tau2.data_model.tasks import Task
            from tau2.evaluator.evaluator_action import ActionEvaluator
            from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

            task = Task.model_validate(request.task)
            trajectory = TypeAdapter(list[Message]).validate_python(request.trajectory)
            criteria = task.evaluation_criteria
            if criteria is None or not criteria.actions or not criteria.communicate_info:
                raise HTTPException(status_code=422, detail="τ-bench Task lacks required evaluation criteria")
            action = ActionEvaluator.calculate_reward(task, trajectory)
            communication = CommunicateEvaluator.calculate_reward(
                task, trajectory, scoring=CommunicateScoring(scorer=CommunicateScorer.SUBSTRING),
            )
            basis = set(criteria.reward_basis)
            reward_components = []
            if "ACTION" in basis:
                reward_components.append(action.reward)
            if "COMMUNICATE" in basis:
                reward_components.append(communication.reward)
            reward = 1.0
            for component in reward_components:
                reward *= component
            return ActorEvaluateResponse(
                evaluator="tau2-action+substring-communicate",
                action_reward=action.reward,
                communicate_reward=communication.reward,
                reward=reward,
                action_checks=[check.model_dump(mode="json") for check in action.action_checks or []],
                communicate_checks=[check.model_dump(mode="json") for check in communication.communicate_checks or []],
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="τ-bench evaluation input is invalid") from exc
        except HTTPException:
            raise
        except Exception as exc:
            import logging
            import traceback

            cause_types = []
            cause = exc.__cause__ or exc.__context__
            while cause is not None:
                cause_types.append(type(cause).__name__)
                cause = cause.__cause__ or cause.__context__
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).error(
                "tau2 evaluation failed run_id=%s actor_id=%s exception_type=%s causes=%s frames=%s",
                request.run_id, request.actor_id, type(exc).__name__, cause_types, frames,
            )
            raise HTTPException(status_code=500, detail="τ-bench evaluator failed") from exc

    return app


def app_from_environment() -> FastAPI:
    settings = Tau2CustomerSettings.from_environment()
    database_path = os.getenv("TAU2_CUSTOMER_JOB_DB", "/var/lib/tac-tau2-customer/turns.sqlite")
    return create_app(settings, database_path)
