"""Pinned τ-bench retail catalogue and native evaluator bridge.

This module is used inside the isolated τ runtime. It keeps source task JSON
unchanged and projects an ERP-observed retail database into τ's native model
before calling τ's own environment evaluator.
"""

from __future__ import annotations

import copy
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence


UPSTREAM_COMMIT = "7aad81c37a3a78b4ed3e4866769f851400c64415"
DOMAIN = "retail"
RUNTIME_DATA_DIR = Path(__file__).parent / "tau2_actor_runtime" / "data"
RETAIL_DATA_DIR = RUNTIME_DATA_DIR / "tau2" / "domains" / DOMAIN
TASKS_PATH = RETAIL_DATA_DIR / "tasks.json"
DB_PATH = RETAIL_DATA_DIR / "db.json"
POLICY_PATH = RETAIL_DATA_DIR / "policy.md"
NL_ASSERTION_MODEL = "tac_codex/codex-cli"
_NL_ASSERTION_EVALUATION_LOCK = threading.Lock()


@contextmanager
def codex_nl_assertion_context():
    """Route τ's native NL assertion judge through the configured Codex provider.

    τ keeps these defaults in module globals. The lock and restoration ensure
    concurrent native evaluations cannot leak the scoped provider setting.
    """
    from tau2.evaluator import evaluator_nl_assertions

    with _NL_ASSERTION_EVALUATION_LOCK:
        original_model = evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS
        original_args = evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS
        evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS = NL_ASSERTION_MODEL
        evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS = {}
        try:
            yield
        finally:
            evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS = original_model
            evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS = original_args


def validate_nl_assertion_coverage(task: Any, reward_info: Any) -> None:
    """Reject native NL judge responses that omit or duplicate criterion results."""
    criteria = task.evaluation_criteria
    required = list(criteria.nl_assertions or []) if criteria is not None else []
    if not required:
        return
    checks = getattr(reward_info, "nl_assertions", None)
    observed = [getattr(check, "nl_assertion", None) for check in checks or ()]
    if Counter(observed) != Counter(required):
        raise RuntimeError("τ native NL assertion evaluator returned incomplete criterion coverage")

SOURCE_SHA256 = {
    "db.json": "413a65160adbdb5fde0ffc0015c49b6d70250b10c18128de169b597af7766765",
    "tasks.json": "8e03ebce7901bd6218e7a7dc3105faa9324091a68058f7fe61c65262868812e8",
    "policy.md": "2c9652afbce57d6e087768d37cda64d31c53d50b3e3225cfdb791bac66466467",
    "split_tasks.json": "ed0580ec52575b63fbf76568af42490da6ee7783ecb4aa81af46961291358f20",
}


class RetailTaskNotFound(LookupError):
    """The requested task ID is absent from the pinned upstream task set."""


def _ensure_upstream_data_root() -> None:
    """Point τ's standard data loader at the byte-pinned bundled data."""
    current = os.environ.get("TAU2_DATA_DIR")
    if current is not None and Path(current).resolve() != RUNTIME_DATA_DIR.resolve():
        raise ValueError("TAU2_DATA_DIR does not point at the pinned retail runtime data")
    os.environ["TAU2_DATA_DIR"] = str(RUNTIME_DATA_DIR)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_tasks() -> list[dict[str, Any]]:
    verify_vendored_data()
    tasks = _read_json(TASKS_PATH)
    if not isinstance(tasks, list):
        raise ValueError("Pinned τ retail tasks.json must contain a task list")
    return tasks


def load_tasks() -> list[Any]:
    """Load all 114 retail tasks through τ's own registered domain loader."""
    _ensure_upstream_data_root()
    verify_vendored_data()
    from tau2.domains.retail.environment import get_tasks

    tasks = get_tasks(task_split_name=None)
    if len(tasks) != 114:
        raise ValueError(f"Pinned τ retail catalogue expected 114 tasks, got {len(tasks)}")
    return tasks


def get_task(task_id: str) -> Any:
    """Return a validated upstream Task with its original source identifier."""
    for task in load_tasks():
        if task.id == task_id:
            return task
    raise RetailTaskNotFound(task_id)


def catalog_metadata() -> dict[str, Any]:
    """Return source-authored titles/summaries, never gold criteria or task state."""
    tasks = load_tasks()
    source_tasks = {task["id"]: task for task in _source_tasks()}
    metadata = []
    for task in tasks:
        source = source_tasks[task.id]
        description = source.get("description") or {}
        instructions = (source.get("user_scenario") or {}).get("instructions") or {}
        summary = instructions.get("reason_for_call") if isinstance(instructions, dict) else None
        if not isinstance(summary, str):
            summary = str(instructions) if instructions else ""
        title = description.get("purpose") or f"Retail task {task.id}"
        metadata.append({"task_id": task.id, "title": title, "summary": summary})
    return {
        "schema_version": 1,
        "domain": DOMAIN,
        "upstream_commit": UPSTREAM_COMMIT,
        "task_count": len(metadata),
        "tasks": metadata,
    }


def _linked_database(
    task: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Return a task-linked subset without changing any upstream record IDs."""
    source_db = _read_json(DB_PATH)
    linked: dict[str, set[str]] = {
        "products": set(), "users": set(), "orders": set(), "items": set()
    }
    integrity_issues: list[dict[str, Any]] = []
    criteria = task.get("evaluation_criteria") or {}
    actions = criteria.get("actions") or []

    # Seed the closure from original reference actions and their exact arguments.
    direct_fields = {
        "user_id": "users", "order_id": "orders", "product_id": "products"
    }
    for action in actions:
        args = action.get("arguments") or {}
        for field, entity in direct_fields.items():
            if isinstance(args.get(field), str):
                linked[entity].add(args[field])
        for field in ("item_id", "item_ids", "new_item_ids"):
            value = args.get(field)
            if isinstance(value, str):
                linked["items"].add(value)
            elif isinstance(value, list):
                linked["items"].update(item for item in value if isinstance(item, str))
        if action.get("name") == "find_user_id_by_name_zip":
            first = args.get("first_name")
            last = args.get("last_name")
            zip_code = args.get("zip")
            matches = []
            for user_id, user in source_db["users"].items():
                if (user["name"]["first_name"] == first
                        and user["name"]["last_name"] == last
                        and user["address"]["zip"] == zip_code):
                    matches.append(user_id)
            if len(matches) != 1:
                integrity_issues.append({
                    "action_id": action.get("action_id"),
                    "reason": "source_user_lookup_not_unique",
                    "match_count": len(matches),
                })
            else:
                linked["users"].add(matches[0])

    # Include identified user context and the order/product graph needed to
    # preserve the original IDs and relationships consumed by τ retail tools.
    order_ids = set(linked["orders"])
    for order_id in tuple(order_ids):
        order = source_db["orders"].get(order_id)
        if order:
            linked["users"].add(order["user_id"])
            linked["items"].update(item["item_id"] for item in order["items"])
    for user_id in tuple(linked["users"]):
        user = source_db["users"].get(user_id)
        if user:
            linked["orders"].update(user["orders"])
    for order_id in tuple(linked["orders"]):
        order = source_db["orders"].get(order_id)
        if order:
            linked["users"].add(order["user_id"])
            linked["items"].update(item["item_id"] for item in order["items"])
            linked["products"].update(item["product_id"] for item in order["items"])
    for product_id, product in source_db["products"].items():
        if linked["items"].intersection(product["variants"]):
            linked["products"].add(product_id)

    for entity in ("products", "users", "orders"):
        missing = linked[entity] - source_db[entity].keys()
        if missing:
            integrity_issues.append({
                "entity": entity,
                "reason": "source_record_missing",
                "source_ids": sorted(missing),
            })
            linked[entity].difference_update(missing)
    known_items = {
        item_id
        for product in source_db["products"].values()
        for item_id in product["variants"]
    }
    if missing_items := linked["items"] - known_items:
        integrity_issues.append({
            "entity": "items",
            "reason": "source_record_missing",
            "source_ids": sorted(missing_items),
        })

    return ({
        entity: {source_id: source_db[entity][source_id]
                 for source_id in sorted(linked[entity])
                 if source_id in source_db[entity]}
        for entity in ("products", "users", "orders")
    }, integrity_issues)


def _id_mapping(original_db: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for entity, records in original_db.items():
        result[entity] = {
            source_id: f"tau2:retail:{entity[:-1] if entity.endswith('s') else entity}:{source_id}"
            for source_id in records
        }
    item_ids = {
        item_id
        for product in original_db["products"].values()
        for item_id in product["variants"]
    }
    result["items"] = {item_id: f"tau2:retail:item:{item_id}" for item_id in sorted(item_ids)}
    return result


def task_manifest(task_id: str) -> dict[str, Any]:
    """Return the original task and a task-scoped source-ID-preserving DB view."""
    task = next((item for item in _source_tasks() if item.get("id") == task_id), None)
    if task is None:
        raise RetailTaskNotFound(task_id)
    original_db, integrity_issues = _linked_database(task)
    return {
        "schema_version": 1,
        "upstream": {
            "project": "sierra-research/tau2-bench",
            "commit": UPSTREAM_COMMIT,
            "domain": DOMAIN,
        },
        "task_id": task_id,
        "task": copy.deepcopy(task),
        "source_database": {
            "path": "servers/simulation/tau2_actor_runtime/data/tau2/domains/retail/db.json",
            "sha256": SOURCE_SHA256["db.json"],
        },
        "original_db": original_db,
        "id_mapping": _id_mapping(original_db),
        "source_integrity": {
            "status": "failed" if integrity_issues else "valid",
            "issues": integrity_issues,
        },
    }


def load_observed_db(observation: Mapping[str, Any]) -> Any:
    """Overlay projected ERP records onto the pinned baseline and validate.

    The observation contract is partial native `products`/`users`/`orders`
    mappings keyed by original τ IDs. A supplied record must be a complete τ
    record; omitted entities retain their source baseline. No ERP field is
    inferred or silently defaulted, and unknown/deleted IDs are rejected.
    """
    _ensure_upstream_data_root()
    verify_vendored_data()
    from tau2.domains.retail.data_model import RetailDB

    if set(observation) - {"products", "users", "orders"}:
        raise ValueError("ERP observation may contain only products, users, and orders")
    merged = _read_json(DB_PATH)
    identity_field = {"products": "product_id", "users": "user_id", "orders": "order_id"}
    for entity, records in observation.items():
        if not isinstance(records, Mapping):
            raise ValueError(f"ERP observation {entity} must be an ID-keyed object")
        for source_id, record in records.items():
            if source_id not in merged[entity]:
                raise ValueError(f"ERP observation contains unknown τ {entity[:-1]} ID")
            if not isinstance(record, Mapping) or record.get(identity_field[entity]) != source_id:
                raise ValueError(f"ERP observation {entity[:-1]} record identity does not match its τ source ID")
            merged[entity][source_id] = dict(record)
    return RetailDB.model_validate(merged)


def evaluate_observed_state(
    task_id: str,
    observation: Mapping[str, Any],
    trajectory: Sequence[Any] | None = None,
    *,
    evaluate_nl_assertions: bool = False,
    unmapped: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Evaluate a projected ERP state with τ's native retail evaluators.

    DB and environment assertions use τ's actual `EnvironmentEvaluator`.
    ACTION and COMMUNICATE are evaluated only when the caller supplies a
    translated upstream Message trajectory. NL assertions use τ's native
    evaluator when requested; its model name is scoped to the existing Codex
    CLI LiteLLM provider in this isolated worker.
    """
    _ensure_upstream_data_root()
    from tau2.data_model.semantic_communication import CommunicateScorer, CommunicateScoring
    from tau2.data_model.tasks import RewardType
    from tau2.domains.retail.environment import get_environment
    from tau2.evaluator.evaluator_action import ActionEvaluator
    from tau2.evaluator.evaluator_communicate import CommunicateEvaluator
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator
    from tau2.environment.toolkit import ToolType, get_tool_types

    task = get_task(task_id)
    observed_db = load_observed_db(observation)
    manifest = task_manifest(task_id)
    native_tool_types = get_tool_types(get_environment().tools)
    write_targets: set[tuple[str, str]] = set()
    for action in task.evaluation_criteria.actions if task.evaluation_criteria else []:
        if native_tool_types.get(action.name) != ToolType.WRITE:
            continue
        arguments = action.arguments
        if isinstance(arguments.get("user_id"), str):
            write_targets.add(("users", arguments["user_id"]))
        if isinstance(arguments.get("order_id"), str):
            write_targets.add(("orders", arguments["order_id"]))
    safe_unmapped = []
    for issue in unmapped:
        if not isinstance(issue, Mapping):
            raise ValueError("unmapped ERP state entries must be objects")
        safe_unmapped.append({
            key: str(issue[key])[:300]
            for key in ("entity", "source_id", "field", "reason")
            if issue.get(key) is not None
        })
    criteria_unmapped = [issue for issue in safe_unmapped
                         if (issue.get("entity"), issue.get("source_id")) in write_targets]
    criteria_ambiguities = [issue for issue in criteria_unmapped
                            if issue.get("reason") == "shared_record_state_ambiguous"]

    def environment_constructor(**kwargs: Any) -> Any:
        # EnvironmentEvaluator passes `solo_mode` only to the predicted
        # environment. Its gold environment must start from the pinned source
        # database so the ERP projection is compared to the reference result.
        if "solo_mode" in kwargs:
            return get_environment(db=observed_db.model_copy(deep=True))
        return get_environment()

    # τ's EnvironmentEvaluator currently logs and swallows failures while
    # replaying reference actions. Detect them explicitly so a failed golden
    # trajectory cannot turn into a misleading DB match against an unchanged
    # baseline.
    reference_environment = get_environment()
    failed_reference_actions: list[dict[str, str]] = []
    for action in task.evaluation_criteria.actions if task.evaluation_criteria else []:
        try:
            reference_environment.make_tool_call(
                tool_name=action.name,
                requestor=action.requestor,
                **action.arguments,
            )
        except Exception as exc:
            failed_reference_actions.append({
                "action_id": action.action_id or action.name,
                "error_type": type(exc).__name__,
            })

    environment_result = EnvironmentEvaluator.calculate_reward(
        environment_constructor=environment_constructor,
        task=task,
        full_trajectory=[],
    )
    reward_basis = task.evaluation_criteria.reward_basis if task.evaluation_criteria else []
    components: dict[str, dict[str, Any]] = {}
    evaluator_results: dict[str, Any] = {
        "environment": environment_result.model_dump(mode="json"),
    }
    for component in reward_basis:
        name = component.value
        if component in (RewardType.DB, RewardType.ENV_ASSERTION):
            value = environment_result.reward_breakdown.get(component)
            components[name] = {
                "status": (
                    "inconclusive"
                    if component == RewardType.DB and criteria_ambiguities
                    else "unevaluated"
                    if component == RewardType.DB and (failed_reference_actions or criteria_unmapped)
                    else "evaluated"
                ),
                "reward": (
                    None
                    if component == RewardType.DB and (failed_reference_actions or criteria_unmapped)
                    else value
                ),
            }
        elif component in (RewardType.ACTION, RewardType.COMMUNICATE) and trajectory is not None:
            if component == RewardType.ACTION:
                result = ActionEvaluator.calculate_reward(task, list(trajectory))
            else:
                result = CommunicateEvaluator.calculate_reward(
                    task,
                    list(trajectory),
                    scoring=CommunicateScoring(scorer=CommunicateScorer.SUBSTRING),
                )
            components[name] = {"status": "evaluated", "reward": result.reward}
            evaluator_results[name.lower()] = result.model_dump(mode="json")
        elif component == RewardType.NL_ASSERTION and evaluate_nl_assertions and trajectory is not None:
            from tau2.evaluator.evaluator_nl_assertions import NLAssertionsEvaluator

            # The worker's configured CodexCLIModel pins reasoning effort;
            # τ's default reasoning_effort LiteLLM argument is unsupported.
            with codex_nl_assertion_context():
                result = NLAssertionsEvaluator.calculate_reward(task, list(trajectory))
            validate_nl_assertion_coverage(task, result)
            components[name] = {"status": "evaluated", "reward": result.reward}
            evaluator_results[name.lower()] = result.model_dump(mode="json")
        else:
            components[name] = {"status": "unevaluated", "reward": None}

    unsupported = [name for name, component in components.items()
                   if component["status"] != "evaluated"]
    inconclusive = any(component["status"] == "inconclusive" for component in components.values())
    reward = None if unsupported else 1.0
    if reward is not None:
        for component in components.values():
            reward *= float(component["reward"])
    return {
        "task_id": task_id,
        "upstream_commit": UPSTREAM_COMMIT,
        "reward_basis": [component.value for component in reward_basis],
        "components": components,
        "reward": reward,
        "reward_status": "inconclusive" if inconclusive else "partial" if unsupported else "complete",
        "unevaluated_components": unsupported,
        "source_integrity": manifest["source_integrity"],
        "criteria_mapping": {
            "db_status": "inconclusive" if criteria_ambiguities else (
                "unevaluated" if criteria_unmapped else "native_projection_complete"
            ),
            "unmapped_criteria_state": criteria_unmapped,
            "shared_record_ambiguities": criteria_ambiguities,
        },
        "unmapped": safe_unmapped,
        "reference_replay": {
            "status": "failed" if failed_reference_actions else "completed",
            "failed_actions": failed_reference_actions,
        },
        "evaluator_results": evaluator_results,
    }


def verify_vendored_data() -> dict[str, str]:
    """Verify byte-for-byte source integrity for all vendored retail inputs."""
    actual = {name: _sha256(RETAIL_DATA_DIR / name) for name in SOURCE_SHA256}
    mismatches = {
        name: {"expected": expected, "actual": actual[name]}
        for name, expected in SOURCE_SHA256.items()
        if actual[name] != expected
    }
    if mismatches:
        raise ValueError(f"τ retail source checksum mismatch: {mismatches}")
    return actual
