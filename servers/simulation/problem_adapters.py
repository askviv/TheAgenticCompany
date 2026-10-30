"""Problem adapters over shared company infrastructure and isolated evaluators."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servers.simulation.actor_client import ActorTurnClient, ActorWorkerError
from servers.simulation.actor_protocol import (
    Tau2RetailSeedRequest,
)


class ProblemOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    commit: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PreparedEmail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1)
    message_id: str = Field(min_length=3, max_length=998)


class PreparedProblem(BaseModel):
    """Validated private problem snapshot for one company run generation."""

    model_config = ConfigDict(extra="forbid")

    problem_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    origin: ProblemOrigin
    snapshot: dict[str, Any]
    customer_instructions: dict[str, Any]
    customer_actor_id: str = Field(min_length=1)
    support_actor_id: str = Field(min_length=1)
    customer_user_state: dict[str, Any]
    initial_email: PreparedEmail
    import_receipt: dict[str, Any]

    @model_validator(mode="after")
    def validate_private_snapshot_bindings(self):
        task = self.snapshot.get("task")
        if (self.snapshot.get("task_id") != self.task_id
                or not isinstance(task, dict) or task.get("id") != self.task_id):
            raise ValueError("prepared problem snapshot must contain its original task")
        source_database = self.snapshot.get("source_database")
        upstream = self.snapshot.get("upstream")
        if not isinstance(source_database, dict):
            raise ValueError("prepared problem snapshot must include its pinned source database")
        if not isinstance(upstream, dict):
            raise ValueError("prepared problem snapshot must include upstream provenance")
        if not isinstance(self.snapshot.get("original_db"), dict):
            raise ValueError("prepared problem snapshot must include its source database slice")
        id_mapping = self.snapshot.get("id_mapping")
        if not isinstance(id_mapping, dict) or not {"products", "users", "orders", "items"}.issubset(id_mapping):
            raise ValueError("prepared problem snapshot must include the source ID mapping")
        source_integrity = self.snapshot.get("source_integrity")
        if (not isinstance(source_integrity, dict)
                or source_integrity.get("status") not in {"valid", "failed"}
                or not isinstance(source_integrity.get("issues"), list)):
            raise ValueError("prepared problem snapshot must include source integrity provenance")
        if not isinstance(self.customer_instructions, dict):
            raise ValueError("customer instructions must be a mapping")
        if not isinstance(task.get("user_scenario"), dict):
            raise ValueError("original task must include a customer scenario")
        if self.customer_instructions != task["user_scenario"]:
            raise ValueError("customer instructions must match the original task scenario")
        source_sha = source_database.get("sha256")
        if (self.origin.task_id != self.task_id or source_sha != self.origin.database_sha256
                or self.origin.commit != upstream.get("commit")
                or self.origin.source != upstream.get("project")
                or self.origin.domain != upstream.get("domain")):
            raise ValueError("prepared problem origin must match the pinned source snapshot")
        if self.import_receipt.get("task_id") != self.task_id or self.import_receipt.get("experiment_id") != self.run_id:
            raise ValueError("native import receipt must bind this task to this run")
        if not isinstance(self.customer_user_state, dict):
            raise ValueError("customer user state must be a mapping")
        return self

    def customer_private_state(self) -> dict[str, Any]:
        """Return the existing private actor-state shape used by τ customer turns."""
        return {
            "problem_id": self.problem_id,
            "task_id": self.task_id,
            "tau2_task": self.snapshot["task"],
            "tau2_user_state": self.customer_user_state,
            "support_actor_id": self.support_actor_id,
        }


@runtime_checkable
class ProblemAdapter(Protocol):
    """Adapter for a benchmark problem using the company's existing systems."""

    scenario_id: str

    def prepare(
        self,
        *,
        run_id: str,
        generation: int,
        task_id: str,
        projection_as_of: datetime,
        customer_actor_id: str,
        support_actor_id: str,
    ) -> PreparedProblem: ...

    def evaluate(
        self,
        *,
        prepared: PreparedProblem,
        trajectory: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]: ...

    def public_catalog(self) -> dict[str, Any]: ...


class TauRetailProblemAdapter:
    """Prepare τ's original retail Task in shared ERPNext and the email gateway."""

    scenario_id = "TAU_RETAIL"
    problem_id = "tau2:retail"

    def __init__(self, actor_client: ActorTurnClient, erp_retail_admin: Any):
        self.actor_client = actor_client
        self.erp_retail_admin = erp_retail_admin

    def public_catalog(self) -> dict[str, Any]:
        return self.actor_client.retail_catalog()

    def prepare(
        self,
        *,
        run_id: str,
        generation: int,
        task_id: str,
        projection_as_of: datetime,
        customer_actor_id: str,
        support_actor_id: str,
    ) -> PreparedProblem:
        manifest = self.actor_client.retail_manifest(task_id)
        origin_data = manifest.get("upstream")
        database = manifest.get("source_database")
        task = manifest.get("task")
        if (not isinstance(origin_data, dict) or not isinstance(database, dict)
                or not isinstance(task, dict) or task.get("id") != task_id):
            raise ValueError("τ retail worker returned an incomplete task manifest")
        receipt = self.erp_retail_admin.seed_task(
            experiment_id=run_id,
            task_id=task_id,
            manifest=manifest,
            projection_as_of=projection_as_of.date().isoformat(),
        )
        if receipt.get("experiment_id") != run_id or receipt.get("task_id") != task_id:
            raise ValueError("ERPNext retail importer returned a different task binding")

        native_start = self.erp_retail_admin.observe_task(manifest, run_id)
        if (not isinstance(native_start, dict) or native_start.get("experiment_id") != run_id
                or native_start.get("task_id") != task_id or not isinstance(native_start.get("observation"), dict)):
            raise ValueError("ERPNext retail observer returned an invalid native starting projection")
        feedback_state = self.actor_client.initialize_retail_feedback(
            task_id, run_id, native_start["observation"],
        )
        if feedback_state.get("task_id") != task_id or feedback_state.get("experiment_id") != run_id:
            raise ValueError("τ retail native feedback worker returned a different task binding")

        seed_request = Tau2RetailSeedRequest(
            turn_id=f"{run_id}:{generation}:retail-seed:{task_id}",
            run_id=run_id,
            generation=generation,
            actor_id=customer_actor_id,
            task_id=task_id,
            message_id=f"<{run_id}.{generation}.retail-seed@tau-retail.local>",
        )
        seed = self.actor_client.retail_seed_email(seed_request)
        if seed.status != "completed" or not seed.body or seed.state is None:
            raise ActorWorkerError("customer", 503, seed.error_type or "retail_seed_incomplete")

        source_database_sha = database.get("sha256")
        origin = ProblemOrigin(
            source=str(origin_data.get("project", "")),
            domain=str(origin_data.get("domain", "")),
            commit=str(origin_data.get("commit", "")),
            task_id=task_id,
            database_sha256=source_database_sha,
        )
        return PreparedProblem(
            problem_id=self.problem_id,
            task_id=task_id,
            run_id=run_id,
            generation=generation,
            origin=origin,
            snapshot=manifest,
            customer_instructions=task.get("user_scenario", {}),
            customer_actor_id=customer_actor_id,
            support_actor_id=support_actor_id,
            customer_user_state=seed.state,
            initial_email=PreparedEmail(
                subject="Customer support request",
                body=seed.body,
                message_id=seed_request.message_id,
            ),
            import_receipt=receipt,
        )

    def evaluate(
        self,
        *,
        prepared: PreparedProblem,
        trajectory: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        captured = self.capture_evaluation_evidence(prepared=prepared)
        return self.evaluate_captured(prepared=prepared, trajectory=trajectory, captured=captured)

    def capture_evaluation_evidence(self, *, prepared: PreparedProblem) -> dict[str, Any]:
        """Freeze native ERP state and its version evidence before model judging."""
        native = self.erp_retail_admin.capture_evaluation_evidence(
            prepared.snapshot, prepared.run_id, prepared.import_receipt,
        )
        if (not isinstance(native, dict)
                or native.get("experiment_id") != prepared.run_id
                or native.get("task_id") != prepared.task_id
                or not isinstance(native.get("observation"), dict)
                or not isinstance(native.get("unmapped"), list)
                or not isinstance(native.get("source_gaps", []), list)
                or not isinstance(native.get("native_evidence"), dict)
                or not isinstance(native.get("version_manifest"), dict)):
            raise ValueError("ERPNext retail evidence capture returned an invalid task snapshot")
        return {
            "task_id": prepared.task_id,
            "experiment_id": prepared.run_id,
            "observation": native["observation"],
            "unmapped": native["unmapped"],
            "source_gaps": native.get("source_gaps", []),
            "native_evidence": native["native_evidence"],
            "version_manifest": native["version_manifest"],
        }

    def evaluate_captured(
        self,
        *,
        prepared: PreparedProblem,
        trajectory: Sequence[Mapping[str, Any]],
        captured: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Score only the immutable ERP projection supplied by the caller."""
        if (captured.get("experiment_id") != prepared.run_id
                or captured.get("task_id") != prepared.task_id
                or not isinstance(captured.get("observation"), dict)
                or not isinstance(captured.get("unmapped"), list)
                or not isinstance(captured.get("source_gaps", []), list)):
            raise ValueError("captured τ retail evaluation evidence is not bound to this task and run")
        evaluation = self.actor_client.evaluate_retail_feedback(
            prepared.task_id, prepared.run_id, captured["observation"], captured["unmapped"],
            [dict(message) for message in trajectory],
        )
        return {
            "problem_id": prepared.problem_id,
            "task_id": prepared.task_id,
            "origin": prepared.origin.model_dump(mode="json"),
            "source_gaps": captured.get("source_gaps", []),
            "evaluation": evaluation,
        }
