"""Idempotent evaluation runner with immutable evidence and score history."""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime
import logging
import math
import re
import traceback
from typing import Any, Callable

from .evaluation_store import CriterionResult, EvaluationIdentity, EvaluationRecord, EvaluationResponse, EvaluationStore
from .store import SQLiteStore

LOG = logging.getLogger(__name__)


Snapshotter = Callable[[str, int], Any]
Evaluator = Callable[[str, Any], dict[str, Any]]
VersionVerifier = Callable[[str, Any], bool]


_SAFE_STATUS = frozenset({"complete", "partial", "inconclusive", "incomplete", "completed",
                          "failed", "unevaluated", "not_started", "not_applicable", "evaluated"})


def _log_failure(evaluation_id: str, correlation_id: str | None, error: BaseException) -> None:
    frames = [(frame.filename, frame.lineno, frame.name) for frame in traceback.extract_tb(error.__traceback__)]
    LOG.error("evaluation failed evaluation_id=%s correlation_id=%s exception_type=%s stack=%s",
              evaluation_id, correlation_id or "unavailable", type(error).__name__, frames)


def _native_results(result: dict[str, Any]):
    """Read the actual CompanyFeedback.report provider result contract."""
    feedback = result.get("company_feedback")
    if not isinstance(feedback, dict):
        return
    rows = feedback.get("native_benchmark_result", [])
    if not isinstance(rows, list):
        raise TypeError("Native benchmark results must be a list")
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("Native benchmark result must be an object")
        benchmark = row.get("benchmark")
        if benchmark not in {"coffeebench", "ecommercebench"}:
            continue
        native = row.get("result")
        if not isinstance(native, dict):
            raise TypeError("Native benchmark result payload must be an object")
        yield benchmark, native


def _scalars(result: dict[str, Any]) -> dict[str, bool | int | float | str | None]:
    """Extract native-scale values by known field names; never persist prose."""
    components: dict[str, bool | int | float | str | None] = {}

    def add(name: str, value: Any, scale: str) -> None:
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("Native evaluator scalar must be finite")
            value = str(value)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Native evaluator scalar must be finite")
        if value is None or type(value) in (bool, int, float, str):
            components[name] = value

    checks = result.get("checks")
    if isinstance(checks, dict):
        for name, value in sorted(checks.items()):
            if type(value) is bool and isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", name):
                add(f"checks.{name}", value, "boolean")

    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        categories = metrics.get("categories")
        if isinstance(categories, dict):
            for category, fields in sorted(categories.items()):
                if (not isinstance(fields, dict) or not isinstance(category, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", category)):
                    continue
                for field, scale in (("passed", "count"), ("total", "count"),
                                     ("complete", "boolean"), ("status", "status")):
                    value = fields.get(field)
                    if (field == "passed" or field == "total") and type(value) is int:
                        add(f"metrics.categories.{category}.{field}", value, scale)
                    elif field == "complete" and type(value) is bool:
                        add(f"metrics.categories.{category}.{field}", value, scale)
                    elif field == "status" and value in _SAFE_STATUS:
                        add(f"metrics.categories.{category}.{field}", value, scale)
        for field, scale in (
            ("elapsed_simulated_seconds", "seconds"),
        ):
            value = metrics.get(field)
            if type(value) in (int, float):
                add(f"metrics.{field}", value, scale)
        tool_calls = metrics.get("tool_calls")
        if isinstance(tool_calls, dict):
            count = tool_calls.get("count")
            if type(count) is int:
                add("metrics.tool_calls.count", count, "count")
            by_type = tool_calls.get("by_type")
            if isinstance(by_type, dict):
                for name, value in sorted(by_type.items()):
                    if type(value) is int and isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", name):
                        add(f"metrics.tool_calls.by_type.{name}", value, "count")
        intervention = metrics.get("human_intervention")
        if isinstance(intervention, dict):
            for field, scale in (("count", "count"), ("effort_seconds", "seconds"),
                                 ("effort_measured_subtotal_seconds", "seconds")):
                value = intervention.get(field)
                if type(value) is int:
                    add(f"metrics.human_intervention.{field}", value, scale)

    problem = result.get("problem_evaluation")
    if isinstance(problem, dict):
        native = problem.get("evaluation")
        if isinstance(native, dict):
            if native.get("reward_status") in _SAFE_STATUS:
                add("problem_evaluation.evaluation.reward_status", native["reward_status"], "status")
            add("problem_evaluation.evaluation.reward", native.get("reward"), "native_reward_0_to_1")
            basis = native.get("components")
            if isinstance(basis, dict):
                for component, values in sorted(basis.items()):
                    if (not isinstance(values, dict) or not isinstance(component, str)
                            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", component)):
                        continue
                    if values.get("status") in _SAFE_STATUS:
                        add(f"problem_evaluation.evaluation.components.{component}.status",
                            values["status"], "status")
                    add(f"problem_evaluation.evaluation.components.{component}.reward",
                        values.get("reward"), "native_reward_0_to_1")

    tau = _tau_reward_info(result)
    if tau is not None:
        commit, reward_info = tau
        prefix = f"tau2.{commit}"
        reward = reward_info.get("reward")
        add(f"{prefix}.reward", reward, "native_reward")
        basis = reward_info.get("reward_basis")
        if isinstance(basis, list):
            for index, component in enumerate(basis):
                if component in _TAU_REWARD_TYPES:
                    add(f"{prefix}.reward_basis.{index}", component, "categorical")
        breakdown = reward_info.get("reward_breakdown")
        if isinstance(breakdown, dict):
            for component, value in sorted(breakdown.items()):
                if component in _TAU_REWARD_TYPES:
                    add(f"{prefix}.reward_breakdown.{component}", value, "native_reward")
        db_check = reward_info.get("db_check")
        if isinstance(db_check, dict):
            add(f"{prefix}.db_check.db_match", db_check.get("db_match"), "boolean")
            add(f"{prefix}.db_check.db_reward", db_check.get("db_reward"), "native_reward")
        for field, value_field, bool_field in (
            ("action_checks", "action_reward", "action_match"),
            ("env_assertions", "reward", "met"),
            ("nl_assertions", None, "met"),
            ("communicate_checks", None, "met"),
        ):
            rows = reward_info.get(field)
            if not isinstance(rows, list):
                continue
            for index, check in enumerate(rows):
                if not isinstance(check, dict):
                    continue
                item_prefix = f"{prefix}.{field}.{index}"
                add(f"{item_prefix}.{bool_field}", check.get(bool_field), "boolean")
                if value_field is not None:
                    add(f"{item_prefix}.{value_field}", check.get(value_field), "native_reward")
                if field == "action_checks":
                    tool_type = check.get("tool_type")
                    if tool_type in {"read", "write", "think", "generic"}:
                        add(f"{item_prefix}.tool_type", tool_type, "categorical")

    customer = result.get("customer_domain_evaluation")
    if isinstance(customer, dict):
        for field in ("action_reward", "communicate_reward", "reward"):
            value = customer.get(field)
            if type(value) in (int, float):
                add(f"customer_domain_evaluation.{field}", value, "native_reward_0_to_1")

    for benchmark, native in _native_results(result):
        prefix = f"company_feedback.native_benchmark_result.{benchmark}"
        if benchmark == "coffeebench":
            for item in native.get("native_evaluations", []):
                if not isinstance(item, dict):
                    raise TypeError("CoffeeBench native evaluation row must be an object")
                actor = item["actor_id"]
                if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", actor):
                    raise ValueError("CoffeeBench native evaluation actor is invalid")
                if item["status"] not in {"final", "incomplete"}:
                    raise ValueError("CoffeeBench native evaluation status is invalid")
                add(f"{prefix}.{actor}.status", item["status"], "categorical")
                # This is the pinned native score name, including its dots.
                # An unfinished horizon keeps None; it is never coerced to zero.
                if item["score_metric"] != "audit.annual.true_net_income":
                    raise ValueError("CoffeeBench returned an unknown canonical score")
                score = item["score"]
                if score is not None and type(score) not in (int, float, Decimal):
                    raise TypeError("CoffeeBench native score must be numeric or null")
                add(f"{prefix}.{actor}.audit.annual.true_net_income", score, "native_metric")
        else:
            analysis = native.get("native_analysis")
            if not isinstance(analysis, dict):
                raise TypeError("E-CommerceBench native analysis must be an object")
            # Only the original evaluator's metric panels are public. Its
            # numeric values and nulls retain their original units and scales;
            # no transcript, supplier prose or raw result is copied here.
            panels = {"profitability", "negotiation_quality", "fraud_identification",
                      "supplier_engagement", "return_management", "fulfilment_quality"}
            def numeric_panel(path, value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_%+.-]{1,120}", key):
                            raise ValueError("Native analysis metric name is invalid")
                        numeric_panel(f"{path}.{key}", child)
                elif value is None or type(value) in (bool, int, float, Decimal):
                    add(path, value, "native_metric")
            for panel in sorted(panels & analysis.keys()):
                numeric_panel(f"{prefix}.{panel}", analysis[panel])
    return components


def _record_status(result: dict[str, Any], version_matches: bool) -> str:
    if not version_matches:
        return "incomplete"
    problem = result.get("problem_evaluation")
    native = problem.get("evaluation") if isinstance(problem, dict) else None
    if isinstance(native, dict) and native.get("reward_status") in ("partial", "inconclusive"):
        return "incomplete"
    if _mapping_gaps(result):
        return "incomplete"
    feedback = result.get("company_feedback")
    if isinstance(feedback, dict):
        if feedback.get("attribution") == "inconclusive":
            return "incomplete"
        for benchmark, native in _native_results(result):
            if benchmark == "ecommercebench" and native.get("native_status") != "finished":
                return "incomplete"
            if benchmark == "coffeebench" and any(
                    item.get("status") != "final" for item in native.get("native_evaluations", [])):
                return "incomplete"
    if isinstance(result.get("agent_performance"), str) and result["agent_performance"] in ("unevaluated", "inconclusive"):
        return "incomplete"
    return "completed"


def _code_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item for item in value if isinstance(item, str)
                   and re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", item)})


def _mapping_gaps(result: dict[str, Any]) -> list[str]:
    gaps = set(_code_list(result.get("mapping_gaps")))
    problem = result.get("problem_evaluation")
    if isinstance(problem, dict):
        if problem.get("source_gaps"):
            gaps.add("native_source_gaps")
        native = problem.get("evaluation")
        if isinstance(native, dict):
            integrity = native.get("source_integrity")
            if isinstance(integrity, dict) and integrity.get("status") != "valid":
                gaps.add("source_integrity_incomplete")
            if native.get("unmapped"):
                gaps.add("native_projection_unmapped")
            criteria = native.get("criteria_mapping")
            if isinstance(criteria, dict):
                status = criteria.get("db_status")
                if status in {"unevaluated", "inconclusive"}:
                    gaps.add("native_criteria_mapping_" + status)
                if criteria.get("unmapped_criteria_state"):
                    gaps.add("native_criteria_unmapped")
                if criteria.get("shared_record_ambiguities"):
                    gaps.add("native_criteria_ambiguous")
    feedback = result.get("company_feedback")
    if isinstance(feedback, dict):
        coverage = feedback.get("coverage")
        if isinstance(coverage, dict):
            if type(coverage.get("unmapped")) is int and coverage["unmapped"] > 0:
                gaps.add("company_feedback_unmapped_records")
            if type(coverage.get("inconclusive")) is int and coverage["inconclusive"] > 0:
                gaps.add("company_feedback_inconclusive_records")
    return sorted(gaps)


def _native_scale(scores: dict[str, Any]) -> dict[str, str]:
    result = {}
    for key in scores:
        if key.startswith("checks."):
            scale = "boolean"
        elif key.startswith("tau2.") and "reward" in key:
            scale = "native_tau_reward"
        elif key.endswith((".db_match", ".action_match", ".met")):
            scale = "boolean"
        elif ".reward_basis." in key or key.endswith(".tool_type"):
            scale = "categorical"
        elif key.endswith("reward_status") or key.endswith(".status"):
            scale = "categorical"
        elif key.endswith("elapsed_simulated_seconds") or key.endswith("effort_seconds"):
            scale = "seconds"
        elif "reward" in key:
            scale = "native_reward_0_to_1"
        elif "count" in key or key.endswith(".passed") or key.endswith(".total"):
            scale = "count"
        else:
            scale = "native_metric"
        result[key] = scale
    return result


def _criterion_results(result: dict[str, Any], version_matches: bool) -> list[CriterionResult]:
    if not version_matches:
        return []
    criteria: list[CriterionResult] = []
    checks = result.get("checks")
    if isinstance(checks, dict):
        for name, passed in sorted(checks.items()):
            if type(passed) is bool and isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", name):
                criteria.append(CriterionResult(criterion_id=f"check.{name}", name=name, passed=passed,
                                                status="complete"))
    problem = result.get("problem_evaluation")
    native = problem.get("evaluation") if isinstance(problem, dict) else None
    components = native.get("components") if isinstance(native, dict) else None
    if isinstance(components, dict):
        for name, component in sorted(components.items()):
            if (not isinstance(component, dict) or not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", name)):
                continue
            status = component.get("status") if component.get("status") in _SAFE_STATUS else None
            reward = component.get("reward")
            if status is not None or type(reward) in (int, float):
                criteria.append(CriterionResult(
                    criterion_id=f"tau2.{name}", name=name, status=status,
                    score=reward if type(reward) in (int, float) else None,
                ))
    tau = _tau_reward_info(result)
    if tau is not None:
        commit, reward_info = tau
        prefix = f"tau2:{commit}"
        overall = reward_info.get("reward")
        if type(overall) in (int, float):
            criteria.append(CriterionResult(
                criterion_id=f"{prefix}:reward", name="reward", status="complete", score=overall,
            ))
        breakdown = reward_info.get("reward_breakdown")
        if isinstance(breakdown, dict):
            for component, score in sorted(breakdown.items()):
                if component in _TAU_REWARD_TYPES and type(score) in (int, float):
                    criteria.append(CriterionResult(
                        criterion_id=f"{prefix}:reward_breakdown:{component}",
                        name=f"reward_breakdown.{component}", status="complete", score=score,
                    ))

        def append_check(field: str, rows: Any, bool_field: str,
                         score_field: str | None) -> None:
            if not isinstance(rows, list):
                return
            for index, check in enumerate(rows):
                if not isinstance(check, dict):
                    continue
                passed = check.get(bool_field)
                score = check.get(score_field) if score_field else None
                if type(passed) is not bool and type(score) not in (int, float):
                    continue
                criteria.append(CriterionResult(
                    criterion_id=f"{prefix}:{field}:{index}",
                    name=f"{field}.{index}", status="complete",
                    score=score if type(score) in (int, float) else None,
                    passed=passed if type(passed) is bool else None,
                ))

        db_check = reward_info.get("db_check")
        if isinstance(db_check, dict):
            matched = db_check.get("db_match")
            score = db_check.get("db_reward")
            criteria.append(CriterionResult(
                criterion_id=f"{prefix}:db_check", name="db_check", status="complete",
                score=score if type(score) in (int, float) else None,
                passed=matched if type(matched) is bool else None,
            ))
        for field, bool_field, score_field in (
            ("action_checks", "action_match", "action_reward"),
            ("env_assertions", "met", "reward"),
            ("nl_assertions", "met", None),
            ("communicate_checks", "met", None),
        ):
            append_check(field, reward_info.get(field), bool_field, score_field)
    return criteria


_TAU_REWARD_TYPES = frozenset({"DB", "ENV_ASSERTION", "NL_ASSERTION", "ACTION", "COMMUNICATE"})


def _tau_reward_info(result: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Return pinned τ RewardInfo only for a company-adapted provider case."""
    source = result.get("native_provider_evaluation")
    if not isinstance(source, dict):
        return None
    commit = source.get("upstream_commit")
    problem = result.get("problem_evaluation")
    evaluation = problem.get("evaluation") if isinstance(problem, dict) else None
    if (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not isinstance(evaluation, dict)):
        raise ValueError("pinned native τ reward projection is incomplete")
    return commit, evaluation


def _public_summary(result: dict[str, Any], scores: dict[str, Any],
                    verification_error: str | None, version_matches: bool) -> dict[str, Any]:
    mapping_gaps = _mapping_gaps(result)
    warnings = _code_list(result.get("warnings"))
    if not version_matches:
        warnings = sorted(set(warnings) | {"evidence_version_mismatch"})
    failure = None
    if verification_error:
        failure = {"code": "evidence_version_verification_failed", "exception_type": verification_error}
    elif not version_matches:
        failure = {"code": "evidence_version_mismatch", "exception_type": "EvidenceVersionMismatch"}
    summary = {"warnings": warnings, "mapping_gaps": mapping_gaps,
              "native_scale": _native_scale(scores), "failure": failure}
    if "company_background_source" in result:
        summary["native_source"] = _company_background_source_summary(result["company_background_source"])
    return summary


def _company_background_source_summary(value: Any) -> dict[str, Any]:
    """Retain only pinned source identity fields needed by company score history."""
    if not isinstance(value, dict):
        raise TypeError("company background source metadata must be an object")
    expected = {
        "company_id", "provider_id", "scope", "attribution", "upstream_commit", "subject_sha256",
        "initialization_request_sha256", "identity_fingerprint", "seed", "started_at",
        "native_state_ref", "native_state_hash", "native_company_time", "money_metric_units",
    }
    if set(value) != expected:
        raise ValueError("company background source metadata has an unexpected shape")
    if (value["company_id"] != "company"
            or value["provider_id"] not in {"coffeebench", "ecommercebench"}
            or value["scope"] != "company_global"
            or value["attribution"] != "company_global_not_experiment"):
        raise ValueError("company background source metadata has an invalid scope")
    for key in ("upstream_commit",):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{40}", value[key]):
            raise ValueError("company background source commit is invalid")
    for key in ("subject_sha256", "initialization_request_sha256", "identity_fingerprint", "native_state_hash"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise ValueError("company background source digest is invalid")
    if type(value["seed"]) is not int or value["seed"] < 0:
        raise ValueError("company background source seed is invalid")
    if not isinstance(value["native_state_ref"], str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,200}", value["native_state_ref"]):
        raise ValueError("company background native state reference is invalid")
    for key in ("started_at", "native_company_time"):
        parsed = datetime.fromisoformat(str(value[key]).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("company background native times must be timezone-aware")
    expected_units = ({"audit.annual.true_net_income": "USD"}
                      if value["provider_id"] == "coffeebench"
                      else {"profitability": "GBP"})
    if value["money_metric_units"] != expected_units:
        raise ValueError("company background financial metric units are invalid")
    return {key: value[key] for key in sorted(expected)}


class EvaluationService:
    """Run one evaluator against one caller-reserved evidence snapshot.

    The caller supplies a native input snapshotter and verifier, so this layer
    stays independent of ERPNext, τ, and CoffeeBench state models.
    """

    def __init__(
        self,
        store: SQLiteStore,
        snapshot_directory,
        *,
        snapshotter: Snapshotter,
        evaluator: Evaluator,
        version_verifier: VersionVerifier,
        identity_resolver: Callable[[str, int, Any], dict[str, Any]],
        guard_factory: Callable[[str], Any],
    ):
        self.store = store
        self.records = EvaluationStore(store, snapshot_directory)
        self.snapshotter = snapshotter
        self.evaluator = evaluator
        self.version_verifier = version_verifier
        self.identity_resolver = identity_resolver
        self.guard_factory = guard_factory

    def evaluate(
        self, run_id: str, caller_actor_id: str, idempotency_key: str,
        *, requested_generation: int | None = None, correlation_id: str | None = None,
        evaluation_id: str | None = None,
    ) -> EvaluationResponse:
        record, _created = self.records.reserve(
            run_id, caller_actor_id, idempotency_key,
            requested_generation=requested_generation, correlation_id=correlation_id,
            evaluation_id=evaluation_id,
        )
        existing = self._response_if_terminal(record)
        if existing is not None:
            return existing

        with self.guard_factory(str(run_id)):
            record = self.records.get_record(record.evaluation_id)
            existing = self._response_if_terminal(record)
            if existing is not None:
                return existing
            if not any(event.event in {"identity_resolved", "snapshot_captured"}
                       for event in self.records.events(record.evaluation_id)):
                try:
                    initial_identity = EvaluationIdentity.model_validate(
                        self.identity_resolver(str(run_id), record.generation, None)
                    ).model_dump(mode="json")
                    self.records.append_event(record.evaluation_id, "identity_resolved", {
                        "identity": initial_identity,
                    })
                    record = self.records.get_record(record.evaluation_id)
                except Exception as exc:
                    _log_failure(record.evaluation_id, correlation_id, exc)
                    completed = self.records.complete(
                        record.evaluation_id, status="failed", snapshot_ref=None,
                        snapshot_sha256=None, result_ref=None, result_sha256=None,
                        scores={}, criterion_results=[], error_type=type(exc).__name__,
                    )
                    return EvaluationResponse(record=completed)
            events = self.records.events(record.evaluation_id)
            snapshot_event = next((event for event in events if event.event == "snapshot_captured"), None)
            if snapshot_event is None:
                try:
                    recovered = self.records.recover_artifact(record.evaluation_id, "snapshot")
                    if recovered is None:
                        snapshot = self.snapshotter(str(run_id), record.generation)
                        snapshot_ref, snapshot_hash = self.records.write_artifact(
                            record.evaluation_id, "snapshot", snapshot,
                        )
                    else:
                        snapshot_ref, snapshot_hash, snapshot = recovered
                    identity = EvaluationIdentity.model_validate(
                        self.identity_resolver(str(run_id), record.generation, snapshot)
                    ).model_dump(mode="json")
                    self.records.append_event(record.evaluation_id, "snapshot_captured", {
                        "snapshot_ref": snapshot_ref, "snapshot_sha256": snapshot_hash,
                        "identity": identity,
                    })
                except Exception as exc:
                    _log_failure(record.evaluation_id, correlation_id, exc)
                    completed = self.records.complete(
                        record.evaluation_id, status="failed", snapshot_ref=None,
                        snapshot_sha256=None, result_ref=None, result_sha256=None,
                        scores={}, criterion_results=[], error_type=type(exc).__name__,
                    )
                    return EvaluationResponse(record=completed)
            else:
                snapshot_ref = snapshot_event.metadata["snapshot_ref"]
                snapshot_hash = snapshot_event.metadata["snapshot_sha256"]
            try:
                snapshot = self.records.read_artifact(snapshot_ref, snapshot_hash)
            except Exception as exc:
                _log_failure(record.evaluation_id, correlation_id, exc)
                completed = self.records.complete(
                    record.evaluation_id, status="failed", snapshot_ref=snapshot_ref,
                    snapshot_sha256=snapshot_hash, result_ref=None, result_sha256=None,
                    scores={}, criterion_results=[], error_type=type(exc).__name__,
                )
                return EvaluationResponse(record=completed)
            events = self.records.events(record.evaluation_id)
            result_stage = next((event for event in events if event.event == "evaluator_result_staged"), None)
            if result_stage is not None:
                try:
                    staged = self.records.read_artifact(
                        result_stage.metadata["result_ref"], result_stage.metadata["result_sha256"],
                    )
                except Exception as exc:
                    _log_failure(record.evaluation_id, correlation_id, exc)
                    completed = self.records.complete(
                        record.evaluation_id, status="failed", snapshot_ref=snapshot_ref,
                        snapshot_sha256=snapshot_hash,
                        result_ref=result_stage.metadata.get("result_ref"),
                        result_sha256=result_stage.metadata.get("result_sha256"),
                        scores={}, criterion_results=[], error_type=type(exc).__name__,
                    )
                    return EvaluationResponse(record=completed)
                return self._persist_staged(record, snapshot_ref, snapshot_hash, staged)
            if any(event.event == "evaluator_started" for event in events):
                recovered_result = self.records.recover_artifact(record.evaluation_id, "result")
                if recovered_result is not None:
                    result_ref, result_hash, staged = recovered_result
                    self.records.append_event(record.evaluation_id, "evaluator_result_staged", {
                        "result_ref": result_ref, "result_sha256": result_hash,
                        "status": staged["status"],
                    })
                    return self._persist_staged(record, snapshot_ref, snapshot_hash, staged)
                self.records.append_event(record.evaluation_id, "uncertain", {"reason_code": "evaluator_interrupted"})
                completed = self.records.complete(
                    record.evaluation_id, status="incomplete", snapshot_ref=snapshot_ref,
                    snapshot_sha256=snapshot_hash, result_ref=None, result_sha256=None,
                    scores={}, criterion_results=[], error_type="EvaluationInterrupted",
                )
                return EvaluationResponse(record=completed)

            self.records.append_event(record.evaluation_id, "evaluator_started", {
                "benchmark_version": record.benchmark_version,
            })
            try:
                result = self.evaluator(str(run_id), snapshot)
                if not isinstance(result, dict):
                    raise TypeError("evaluation callback must return an object")
            except Exception as exc:
                _log_failure(record.evaluation_id, correlation_id, exc)
                self.records.append_event(record.evaluation_id, "uncertain", {
                    "reason_code": "evaluator_failed_after_start",
                    "error_type": type(exc).__name__,
                })
                completed = self.records.complete(
                    record.evaluation_id, status="failed", snapshot_ref=snapshot_ref,
                    snapshot_sha256=snapshot_hash, result_ref=None, result_sha256=None,
                    scores={}, criterion_results=[], error_type=type(exc).__name__,
                )
                return EvaluationResponse(record=completed)

            verification_error = None
            try:
                version_matches = bool(self.version_verifier(str(run_id), snapshot))
            except Exception as exc:
                version_matches = False
                verification_error = type(exc).__name__
                _log_failure(record.evaluation_id, correlation_id, exc)
                self.records.append_event(record.evaluation_id, "uncertain", {
                    "reason_code": "evidence_version_verification_failed",
                    "error_type": verification_error,
                })

            projection_error = None
            try:
                status = _record_status(result, version_matches)
                scores = _scalars(result) if version_matches else {}
                criteria = _criterion_results(result, version_matches)
                public_summary = _public_summary(result, scores, verification_error, version_matches)
            except Exception as exc:
                _log_failure(record.evaluation_id, correlation_id, exc)
                status, scores, criteria = "failed", {}, []
                projection_error = type(exc).__name__
                public_summary = {"warnings": [], "mapping_gaps": [], "native_scale": {},
                                  "failure": {"code": "native_evaluation_contract_failed",
                                              "exception_type": projection_error}}
            outcome_error = projection_error or verification_error or (None if version_matches else "EvidenceVersionMismatch")
            staged_payload = {
                "result": result,
                "status": status,
                "scores": scores,
                "criterion_results": [item.model_dump(mode="json") for item in criteria],
                "error_type": outcome_error,
                "public_summary": public_summary,
            }
            result_ref, result_hash = self.records.write_artifact(
                record.evaluation_id, "result", staged_payload,
            )
            self.records.append_event(record.evaluation_id, "evaluator_result_staged", {
                "result_ref": result_ref, "result_sha256": result_hash,
                "status": status,
            })
            completed = self.records.complete(
                record.evaluation_id, status=status, snapshot_ref=snapshot_ref,
                snapshot_sha256=snapshot_hash, result_ref=result_ref,
                result_sha256=result_hash, scores=scores, criterion_results=criteria,
                error_type=outcome_error,
                public_summary=staged_payload["public_summary"],
            )
            return EvaluationResponse(record=completed)

    def list(self, run_id: str, *, generation: int | None = None) -> list[EvaluationRecord]:
        return self.records.list_records(run_id, generation=generation)

    def latest(self, run_id: str, *, generation: int | None = None) -> EvaluationRecord | None:
        return self.records.latest_record(run_id, generation=generation)

    def get_record(self, evaluation_id: str) -> EvaluationRecord:
        return self.records.get_record(evaluation_id)

    def reconcile_interrupted(self, try_guard_factory: Callable[[str], Any]) -> int:
        """Close evaluator attempts abandoned by a prior process without replaying them."""
        reconciled = 0
        for record in self.records.unfinished_records():
            with try_guard_factory(record.run_id) as acquired:
                if not acquired:
                    continue
                if self.records.get_record(record.evaluation_id).completed_at is not None:
                    continue
                events = self.records.events(record.evaluation_id)
                if not any(event.event == "evaluator_started" for event in events):
                    continue
                snapshot_event = next((event for event in events if event.event == "snapshot_captured"), None)
                staged_event = next((event for event in events if event.event == "evaluator_result_staged"), None)
                snapshot_ref = snapshot_event.metadata.get("snapshot_ref") if snapshot_event else None
                snapshot_hash = snapshot_event.metadata.get("snapshot_sha256") if snapshot_event else None
                recovered = None
                if staged_event is not None:
                    try:
                        recovered = self.records.read_artifact(
                            staged_event.metadata["result_ref"], staged_event.metadata["result_sha256"],
                        )
                    except Exception as exc:
                        _log_failure(record.evaluation_id, record.correlation_id, exc)
                else:
                    recovered_result = self.records.recover_artifact(record.evaluation_id, "result")
                    if recovered_result is not None:
                        result_ref, result_hash, recovered = recovered_result
                        self.records.append_event(record.evaluation_id, "evaluator_result_staged", {
                            "result_ref": result_ref, "result_sha256": result_hash,
                            "status": recovered["status"],
                        })
                if recovered is not None and snapshot_ref and snapshot_hash:
                    self._persist_staged(record, snapshot_ref, snapshot_hash, recovered)
                    reconciled += 1
                    continue
                if not any(event.event == "uncertain" for event in events):
                    self.records.append_event(record.evaluation_id, "uncertain", {
                        "reason_code": "evaluator_interrupted_by_process_restart",
                    })
                self.records.complete(
                    record.evaluation_id, status="incomplete",
                    snapshot_ref=snapshot_ref, snapshot_sha256=snapshot_hash,
                    result_ref=None, result_sha256=None, scores={}, criterion_results=[],
                    error_type="EvaluationInterrupted",
                )
                reconciled += 1
        return reconciled

    def _persist_staged(
        self, record: EvaluationRecord, snapshot_ref: str, snapshot_hash: str, staged: dict[str, Any],
    ) -> EvaluationResponse:
        staged_event = next(event for event in self.records.events(record.evaluation_id)
                            if event.event == "evaluator_result_staged")
        result_ref = staged_event.metadata["result_ref"]
        # The staged hash is already verified by read_artifact; preserve exact
        # result bytes and record only the allowlisted native score components.
        result_hash = staged_event.metadata["result_sha256"]
        criteria = [CriterionResult.model_validate(item) for item in staged.get("criterion_results", [])]
        completed = self.records.complete(
            record.evaluation_id, status=staged["status"], snapshot_ref=snapshot_ref,
            snapshot_sha256=snapshot_hash, result_ref=result_ref, result_sha256=result_hash,
            scores=staged.get("scores", {}), criterion_results=criteria,
            error_type=staged.get("error_type"),
            public_summary=staged.get("public_summary", {}),
        )
        return EvaluationResponse(record=completed)

    def _response_if_terminal(self, record: EvaluationRecord) -> EvaluationResponse | None:
        if record.completed_at is None:
            return None
        return EvaluationResponse(record=record)

