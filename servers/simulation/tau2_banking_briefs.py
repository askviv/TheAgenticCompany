"""Verified curated Banking task briefs from pinned source USER requests."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .provider_contracts import ProviderSourceFact, ProviderTaskCatalogEntry


SOURCE_COMMIT = "7aad81c37a3a78b4ed3e4866769f851400c64415"
BRIEF_SHA256 = "77f2b17932611ab22ab9ece36b3496de3935b7de8462e81a906432b1ddec8f7b"
SOURCE_POINTER = "/user_scenario/instructions"
BRIEF_PATH = Path(__file__).with_name("tau2_actor_runtime") / "provider_task_briefs/tau2-banking.json"
TASKS_PATH = Path(__file__).with_name("tau2_actor_runtime") / "data/tau2/domains/banking_knowledge/tasks.json"


@lru_cache(maxsize=1)
def _verified_briefs() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    briefs = json.loads(BRIEF_PATH.read_text(encoding="utf-8"))
    if hashlib.sha256(BRIEF_PATH.read_bytes()).hexdigest() != BRIEF_SHA256:
        raise ValueError("curated Banking brief asset integrity check failed")
    source_bytes = TASKS_PATH.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    upstream = briefs.get("upstream", {})
    if (upstream.get("commit") != SOURCE_COMMIT
            or upstream.get("task_source_sha256") != source_hash
            or upstream.get("task_count") != 97):
        raise ValueError("curated Banking briefs do not match the pinned native task source")
    source_doc = json.loads(source_bytes)
    task_rows = source_doc if isinstance(source_doc, list) else source_doc["tasks"]
    source_by_id = {str(row["id"]): row for row in task_rows}
    brief_by_id = {str(row["task_id"]): row for row in briefs.get("tasks", [])}
    if set(source_by_id) != set(brief_by_id) or len(brief_by_id) != len(briefs.get("tasks", [])):
        raise ValueError("curated Banking brief inventory differs from the native task inventory")
    for task_id, row in brief_by_id.items():
        source_path = TASKS_PATH.parent / "tasks" / f"{task_id}.json"
        if not source_path.is_file():
            raise ValueError(f"curated Banking brief source file is missing for {task_id}")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != row.get("source_task_sha256"):
            raise ValueError(f"curated Banking brief source hash mismatch for {task_id}")
        if row.get("source_task_json_pointer") != SOURCE_POINTER:
            raise ValueError("curated Banking brief references an unsupported source field")
        instructions = source_by_id[task_id]["user_scenario"]["instructions"]
        review_span = row.get("review_source_request", {})
        if (review_span.get("json_pointer") != SOURCE_POINTER
                or review_span.get("source_task_sha256") != row.get("source_task_sha256")):
            raise ValueError(f"curated Banking review span is not tied to its source task for {task_id}")
        review_start, review_end = review_span.get("start_char"), review_span.get("end_char")
        if (not isinstance(review_start, int) or not isinstance(review_end, int)
                or review_start < 0 or review_end <= review_start or review_end > len(instructions)
                or instructions[review_start:review_end] != review_span.get("excerpt")):
            raise ValueError(f"curated Banking review request span is invalid for {task_id}")
        for fact in row.get("private_factual_context", []):
            start, end = fact.get("start_char"), fact.get("end_char")
            if (not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start
                    or end > len(instructions) or instructions[start:end] == ""):
                raise ValueError(f"curated Banking fact span is invalid for {task_id}")
    return briefs, source_by_id


def catalog_entry(task_id: str) -> ProviderTaskCatalogEntry:
    briefs, _ = _verified_briefs()
    row = next((item for item in briefs["tasks"] if item["task_id"] == task_id), None)
    if row is None:
        raise KeyError(task_id)
    public = row["public"]
    return ProviderTaskCatalogEntry(
        provider_id="tau2-banking", task_id=task_id,
        description=public["summary"], title=public["title"],
        requested_outcome=public["requested_outcome"], role_fit=public["role_fit"],
        role_fit_note=public["role_fit_note"],
        financial_context_status=public["financial_context_status"],
    )


def private_factual_context(task_id: str) -> list[ProviderSourceFact]:
    """Return exact source spans to the assigned company delegate only."""
    briefs, source_by_id = _verified_briefs()
    row = next((item for item in briefs["tasks"] if item["task_id"] == task_id), None)
    if row is None:
        raise KeyError(task_id)
    instructions = source_by_id[task_id]["user_scenario"]["instructions"]
    return [ProviderSourceFact(
        label=fact["label"], text=instructions[fact["start_char"]:fact["end_char"]],
        source_task_sha256=row["source_task_sha256"], source_json_pointer=SOURCE_POINTER,
        start_char=fact["start_char"], end_char=fact["end_char"],
    ) for fact in row.get("private_factual_context", [])]
