"""Private CoffeeBench supplier worker for the shared actor-turn protocol."""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from servers.simulation.actor_protocol import (
    ActorTurnRequest, ActorTurnResponse, ActorUsage, SupplierStatePatch,
    SupplierKernelStatePatch, bearer_token_matches,
)
from servers.simulation.actor_worker_store import ActorTurnStore, canonical_request_hash
from servers.simulation.coffeebench_supplier import CoffeeSupplierAgent
from servers.simulation.coffeebench_supplier import CodexCoffeeModel


logger = logging.getLogger(__name__)
_UPSTREAM_COFFEE_COMMIT = "9e2f3950b5812c8753de81ede9c68babc024b5c8"
_EXECUTION_LOCK = threading.Lock()


def _safe_diagnostics(exc: BaseException) -> dict[str, Any]:
    chain: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 8:
        chain.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    frames = []
    tb = exc.__traceback__
    while tb is not None and len(frames) < 24:
        code = tb.tb_frame.f_code
        frames.append({"file": Path(code.co_filename).name, "function": code.co_name, "line": tb.tb_lineno})
        tb = tb.tb_next
    return {"exception_types": chain, "frames": frames}


class CoffeeTranscriptStore:
    """Append private Coffee Agent transcripts in the common actor-worker DB."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self._initialize()
        if os.name == "posix":
            self.path.chmod(0o600)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS coffee_supplier_transcripts (
                    run_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    upstream_commit TEXT NOT NULL,
                    updated_turn_id TEXT NOT NULL,
                    PRIMARY KEY(run_id,generation,actor_id)
                )"""
            )

    def load(self, run_id: str, generation: int, actor_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT messages_json,model_id,upstream_commit,updated_turn_id "
                "FROM coffee_supplier_transcripts WHERE run_id=? AND generation=? AND actor_id=?",
                (run_id, generation, actor_id),
            ).fetchone()
        if row is None:
            return None
        return {"messages": json.loads(row["messages_json"]), "model_id": row["model_id"],
                "upstream_commit": row["upstream_commit"], "updated_turn_id": row["updated_turn_id"]}

    def save(
        self, run_id: str, generation: int, actor_id: str, *, messages: list[dict[str, Any]],
        model_id: str, turn_id: str,
    ) -> None:
        encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO coffee_supplier_transcripts "
                "(run_id,generation,actor_id,messages_json,model_id,upstream_commit,updated_turn_id) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,generation,actor_id) DO UPDATE SET "
                "messages_json=excluded.messages_json,model_id=excluded.model_id, "
                "upstream_commit=excluded.upstream_commit,updated_turn_id=excluded.updated_turn_id",
                (run_id, generation, actor_id, encoded, model_id, _UPSTREAM_COFFEE_COMMIT, turn_id),
            )
            connection.commit()


class CoffeeSupplierWorker:
    def __init__(
        self,
        database_path: str | Path,
        *,
        model_factory: Callable[[ActorTurnRequest], Any],
        max_agent_steps: int = 12,
    ):
        self.turns = ActorTurnStore(database_path)
        self.transcripts = CoffeeTranscriptStore(database_path)
        self.model_factory = model_factory
        self.max_agent_steps = max_agent_steps

    def process(self, request: ActorTurnRequest) -> ActorTurnResponse:
        if request.actor_type != "supplier":
            raise WorkerTurnRejected("unsupported_actor_type")
        if not request.run_id or request.generation < 0:
            raise WorkerTurnRejected("invalid_actor_scope")
        private_state = request.scenario.get("supplier_private_state")
        if not isinstance(private_state, dict):
            raise WorkerTurnRejected("supplier_private_state_missing")
        body = request.model_dump(mode="json")
        request_hash = canonical_request_hash(body)
        claimed, cached = self.turns.claim(request.turn_id, request_hash)
        if claimed == "conflict":
            raise WorkerTurnConflict
        if claimed == "completed":
            return ActorTurnResponse.model_validate(cached)
        if claimed == "started":
            return ActorTurnResponse(turn_id=request.turn_id, status="started_uncertain")

        try:
            model = self.model_factory(request)
            model_id = getattr(model, "model", None)
            if not isinstance(model_id, str) or not model_id.strip():
                raise WorkerTurnFailed("supplier_model_identifier_missing")
            prior = self.transcripts.load(request.run_id, request.generation, request.actor_id)
            messages = prior["messages"] if prior else None
            # Coffee's upstream Agent writes its tool registration to stdout.
            # The lock keeps this scoped redirection from hiding other worker logs.
            with _EXECUTION_LOCK, redirect_stdout(io.StringIO()):
                outcome = CoffeeSupplierAgent(model, max_steps=self.max_agent_steps).respond(
                    request.inbound_message.body,
                    private_state,
                    {**request.visible_context, "subject": request.inbound_message.subject},
                    messages=messages,
                )
            tool_errors = outcome.pop("_tool_errors", [])
            if tool_errors:
                logger.warning("supplier agent recovered from tool call failures", extra={
                    "turn_id": request.turn_id,
                    "error_count": len(tool_errors),
                    "diagnostics": [_safe_diagnostics(error) for error in tool_errors],
                })
            self.transcripts.save(
                request.run_id, request.generation, request.actor_id,
                messages=outcome["messages"], model_id=model_id, turn_id=request.turn_id,
            )
            patch = {}
            if isinstance(private_state.get("kernel_state"), dict):
                patch["kernel_state"] = deepcopy(private_state["kernel_state"])
            conversation_key = hashlib.sha256(
                f"{request.run_id}:{request.generation}:{request.actor_id}".encode("utf-8")
            ).hexdigest()
            usage = model.get_usage_stats() if callable(getattr(model, "get_usage_stats", None)) else {}
            result = ActorTurnResponse(
                turn_id=request.turn_id,
                status="completed",
                body=outcome["body"],
                conversation_pointer=f"coffee-agent:{_UPSTREAM_COFFEE_COMMIT}:{conversation_key}",
                supplier_private_state_patch=SupplierStatePatch(
                    kernel_state=SupplierKernelStatePatch(turns=patch.get("kernel_state", {}).get("turns", []))
                ),
                quote=outcome.get("quote"),
                disclosed_facts=outcome.get("disclosed_facts", []),
                required_actions=outcome.get("required_actions", []),
                usage=ActorUsage(
                    provider="codex_cli",
                    model_id=model_id,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                ),
            )
            self.turns.complete(request.turn_id, request_hash, result.model_dump(mode="json"))
            return result
        except WorkerTurnFailed:
            raise
        except Exception as exc:
            details = _safe_diagnostics(exc)
            logger.error("supplier actor turn failed", extra={
                "turn_id": request.turn_id,
                "error_type": type(exc).__name__, "diagnostics": details,
            })
            raise WorkerTurnFailed(type(exc).__name__) from exc


class WorkerTurnConflict(Exception):
    pass


class WorkerTurnRejected(Exception):
    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__(error_type)


class WorkerTurnFailed(Exception):
    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__(error_type)


def codex_supplier_model_factory(settings: Any) -> Callable[[ActorTurnRequest], CodexCoffeeModel]:
    """Create the Coffee Model bridge over the shared Codex CLI transport."""
    from servers.simulation.codex_model import CodexCLIModel

    def create(_: ActorTurnRequest) -> CodexCoffeeModel:
        return CodexCoffeeModel(CodexCLIModel(settings), model_id=settings.model)

    return create


def create_supplier_worker_app(worker: CoffeeSupplierWorker, *, bearer_secret: str) -> FastAPI:
    if not bearer_secret:
        raise ValueError("supplier worker bearer secret is required")
    app = FastAPI(title="CoffeeBench supplier actor worker", docs_url=None, redoc_url=None, openapi_url=None)
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        authorization = None if credentials is None else f"{credentials.scheme} {credentials.credentials}"
        if not bearer_token_matches(authorization, bearer_secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/turn", response_model=ActorTurnResponse, dependencies=[Depends(authenticate)])
    def turn(request: ActorTurnRequest, response: Response):
        try:
            result = worker.process(request)
        except WorkerTurnConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="turn_id_payload_conflict") from exc
        except WorkerTurnRejected as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail={"error_type": exc.error_type}) from exc
        except WorkerTurnFailed as exc:
            # Provider and private-record data remain in the operator log only.
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail={"error_type": exc.error_type}) from exc
        if result.status == "started_uncertain":
            response.status_code = status.HTTP_202_ACCEPTED
        return result

    return app
