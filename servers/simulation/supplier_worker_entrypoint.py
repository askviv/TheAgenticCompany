"""Environment-backed factory for the isolated CoffeeBench worker."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from servers.simulation.codex_model import CodexCLISettings
from servers.simulation.codex_remote import CodexRemoteSettings
from servers.simulation.background_worker import BackgroundWorker, attach_background_routes
from servers.simulation.coffeebench_supplier import CodexCoffeeModel
from servers.simulation.supplier_worker import (
    CoffeeSupplierWorker,
    codex_supplier_model_factory,
    create_supplier_worker_app,
)


class SupplierWorkerSettings(BaseModel):
    """Validated process configuration; bearer credentials stay redacted."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    database_path: Path
    bearer_secret: SecretStr = Field(min_length=32, max_length=256)
    codex_home: Path | None = None
    codex_model: str = Field(min_length=1, max_length=240)
    codex_executable: str = Field(default="codex", min_length=1, max_length=500)
    codex_timeout_seconds: float | None = Field(default=None, gt=0)
    codex_inference_url: str | None = None
    codex_inference_token: SecretStr | None = Field(default=None, min_length=1)

    @classmethod
    def from_environment(cls) -> "SupplierWorkerSettings":
        required = {
            "database_path": "ACTOR_WORKER_DATABASE_PATH",
            "bearer_secret": "ACTOR_WORKER_BEARER_SECRET",
            "codex_model": "CODEX_MODEL",
        }
        missing = [environment_name for field, environment_name in required.items()
                   if not os.environ.get(environment_name)]
        if missing:
            raise RuntimeError(
                "supplier worker is missing required environment configuration: "
                + ", ".join(missing)
            )
        remote_url = os.getenv("CODEX_INFERENCE_URL")
        remote_token = os.getenv("CODEX_INFERENCE_BEARER_TOKEN")
        if bool(remote_url) != bool(remote_token):
            raise RuntimeError("remote Codex inference requires both CODEX_INFERENCE_URL and CODEX_INFERENCE_BEARER_TOKEN")
        if not remote_url and not os.environ.get("CODEX_HOME"):
            raise RuntimeError("supplier worker requires CODEX_HOME when remote Codex inference is not configured")
        return cls(
            database_path=os.environ[required["database_path"]],
            bearer_secret=os.environ[required["bearer_secret"]],
            codex_home=os.environ.get("CODEX_HOME"),
            codex_model=os.environ[required["codex_model"]],
            codex_executable=os.environ.get("CODEX_EXECUTABLE", "codex"),
            codex_timeout_seconds=os.environ.get("CODEX_TIMEOUT_SECONDS"),
            codex_inference_url=remote_url,
            codex_inference_token=remote_token,
        )


def create_app(settings: SupplierWorkerSettings | None = None):
    """Construct the worker API from validated private process settings."""
    settings = settings or SupplierWorkerSettings.from_environment()
    model_settings = (
        CodexRemoteSettings(
            url=settings.codex_inference_url,
            bearer_token=settings.codex_inference_token,
            model=settings.codex_model,
        ) if settings.codex_inference_url else CodexCLISettings(
            executable=settings.codex_executable,
            model=settings.codex_model,
            codex_home=settings.codex_home,
            timeout_seconds=settings.codex_timeout_seconds,
        )
    )
    worker = CoffeeSupplierWorker(
        str(settings.database_path),
        model_factory=codex_supplier_model_factory(model_settings),
    )
    def background_model_factory(actor_id: str, model_id: str):
        del actor_id
        if model_id != model_settings.model:
            raise ValueError("CoffeeBench background model binding differs from worker configuration")
        from servers.simulation.codex_remote import create_codex_transport

        return CodexCoffeeModel(create_codex_transport(model_settings), model_id=model_id)

    background_worker = BackgroundWorker(
        str(settings.database_path),
        checkpoint_directory=settings.database_path.parent / "background-checkpoints",
        coffee_model_factory=background_model_factory,
        configured_model_id=model_settings.model,
    )
    app = create_supplier_worker_app(
        worker, bearer_secret=settings.bearer_secret.get_secret_value()
    )
    attach_background_routes(
        app, background_worker, bearer_secret=settings.bearer_secret.get_secret_value(),
    )
    return app


app = create_app()
