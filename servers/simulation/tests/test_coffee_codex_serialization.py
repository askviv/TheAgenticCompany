from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from servers.simulation.codex_model import CodexCLIModel, CodexCLISettings
from servers.simulation.coffeebench_background import _IdempotentCoffeeModel, _ModelResponseStore
from servers.simulation.coffeebench_supplier import CodexCoffeeModel
from servers.simulation.background_worker import BackgroundOperationUncertain, BackgroundWorker
from servers.simulation.integration_contracts import BackgroundAdvanceRequest
from coffeebench.models.types import ToolCall, ToolSpec
from servers.simulation.coffeebench_background import _jsonable


def test_native_coffee_toolcall_transcript_is_serialized_before_codex_cli_boundary(tmp_path, monkeypatch):
    native_messages = [
        {"role": "developer", "content": "Use the registered native tool."},
        {"role": "user", "content": "Submit the confirmed intake fields."},
        {
            "role": "assistant", "content": "", "thinking": "",
            "tool_calls": [ToolCall(id="prior-call", name="read_record", input={"record_id": "R-1"})],
            "_raw": None, "timestamp": "20261030_093000",
        },
        {"role": "tool", "tool_call_id": "prior-call", "name": "read_record", "content": '{"ok":true}'},
    ]
    native_tools = [ToolSpec(
        name="submit_fields", description="Submit the actual source record.",
        input_schema={"type": "object", "properties": {"fields": {"type": "object"}}},
    )]
    cli_settings = CodexCLISettings(
        model="gpt-6-luna", codex_home=tmp_path / "codex-home", timeout_seconds=5,
    )
    cli = CodexCLIModel(cli_settings)
    wrapper = _IdempotentCoffeeModel(
        "roaster_B", CodexCoffeeModel(cli, model_id="gpt-6-luna"),
        _ModelResponseStore(tmp_path / "model-turns.sqlite"),
    )
    captured = []

    def fake_subprocess_run(command, *, input, **_kwargs):
        prompt_json = input.split("\n", 1)[1]
        request = json.loads(prompt_json)
        captured.append(request)
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({
            "content": None,
            "tool_calls": [{
                "id": "next-call", "name": "submit_fields",
                "arguments_json": '{"fields":{"customer_name":"Ada"}}',
            }],
        }), encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 3}}),
            stderr="",
        )

    monkeypatch.setattr("servers.simulation.codex_model.subprocess.run", fake_subprocess_run)

    first = wrapper.query(native_messages, native_tools)
    second = wrapper.query(native_messages, native_tools)

    assert len(captured) == 1  # exact provider-turn cache replays the confirmed response
    transcript = captured[0]["messages"]
    assert transcript[2]["tool_calls"] == [{
        "id": "prior-call", "name": "read_record", "input": {"record_id": "R-1"},
    }]
    assert "ToolCall(" not in json.dumps(transcript)
    assert captured[0]["tools"] == [{
        "type": "function",
        "function": {
            "name": "submit_fields", "description": "Submit the actual source record.",
            "parameters": {"type": "object", "properties": {"fields": {"type": "object"}}},
        },
    }]
    assert first.tool_calls[0].input == {"fields": {"customer_name": "Ada"}}
    assert second.tool_calls[0].input == first.tool_calls[0].input


def test_unknown_coffee_turn_value_is_rejected_instead_of_stringified():
    class UnknownNativeValue:
        pass

    with pytest.raises(TypeError, match="unsupported CoffeeBench JSON value type: UnknownNativeValue"):
        _jsonable(UnknownNativeValue())


def test_model_turn_submission_phase_allows_only_proven_unsubmitted_replay(tmp_path):
    store = _ModelResponseStore(tmp_path / "phases.sqlite")
    operation_id = "advance:test-operation"
    created_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    assert store.can_replay_operation(operation_id, created_at)

    assert store.load_or_claim("turn-prepared", "hash-prepared", operation_id) == (None, "turn-prepared")
    assert store.can_replay_operation(operation_id, created_at)
    # The same operation can safely repeat the prepared request without
    # resetting its write-ahead claim.
    assert store.load_or_claim("turn-prepared", "hash-prepared", operation_id) == (None, "turn-prepared")
    store.mark_submitted("turn-prepared", "hash-prepared")
    assert not store.can_replay_operation(operation_id, created_at)


def test_recovery_inference_uses_a_new_claim_without_resetting_ambiguous_turn(tmp_path):
    store = _ModelResponseStore(tmp_path / "recovery-turns.sqlite")
    original_id = "old-operation"
    recovery_id = "recovery-operation"
    _, original_turn_id = store.load_or_claim("same-native-turn", "same-request-hash", original_id)
    store.mark_submitted(original_turn_id, "same-request-hash")

    _, recovery_turn_id = store.load_or_claim(
        "same-native-turn", "same-request-hash", recovery_id, recovery_attempt=True,
    )
    assert recovery_turn_id != original_turn_id
    store.complete(recovery_turn_id, "same-request-hash", {
        "content": "bounded result", "thinking": "", "tool_calls": [],
        "stop_reason": "end_turn", "cost": 0,
    })
    recovered, replay_turn_id = store.load_or_claim(
        "same-native-turn", "same-request-hash", recovery_id, recovery_attempt=True,
    )
    assert replay_turn_id == recovery_turn_id
    assert recovered["content"] == "bounded result"
    # The original uncertain submission and its phase remain preserved.
    with sqlite3.connect(store.path) as connection:
        original = connection.execute(
            "SELECT status,submission_phase,operation_id FROM coffee_model_turns WHERE turn_id=?",
            (original_turn_id,),
        ).fetchone()
    assert original == ("started", "submitted", original_id)


def test_legacy_started_model_turns_migrate_to_unknown_without_reset(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE coffee_model_turns (turn_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, response_json TEXT)"
        )
        connection.execute(
            "INSERT INTO coffee_model_turns VALUES('legacy-turn','old-hash','started',NULL)"
        )
    store = _ModelResponseStore(path)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT status,operation_id,submission_phase FROM coffee_model_turns WHERE turn_id='legacy-turn'"
        ).fetchone()
        schema_at = connection.execute(
            "SELECT value FROM coffee_model_store_meta WHERE key='submission_phase_schema_at'"
        ).fetchone()[0]
    assert row == ("started", None, "unknown")
    assert not store.can_replay_operation(
        "legacy-operation", (datetime.fromisoformat(schema_at) - timedelta(seconds=1)).isoformat(),
    )


def test_background_operation_replays_only_when_linked_turns_are_prepared(tmp_path, monkeypatch):
    worker = BackgroundWorker(
        tmp_path / "worker.sqlite", checkpoint_directory=tmp_path / "checkpoints",
        coffee_model_factory=lambda _actor_id, _model_id: None,
        configured_model_id="gpt-6-luna",
    )
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    base_snapshot = {
        "seed": 7, "started_at": start.isoformat(), "cursor": start.isoformat(),
        "status": "active", "company_business_ids": ["roaster_A"],
        "binding": {"model_id": "gpt-6-luna"}, "checkpoint": {},
    }
    init_request = {"provider_id": "coffeebench", "operation_id": "initialize"}
    worker.store.claim_background_operation("coffeebench", "initialize", init_request)
    worker.store.complete_background_operation(
        "coffeebench", "initialize", init_request, upstream_commit="9e2f3950b5812c8753de81ede9c68babc024b5c8",
        seed=7, started_at=start, cursor=start, snapshot=base_snapshot,
        result={"provider_id": "coffeebench", "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
                "cursor": start.isoformat()}, intents=[],
    )

    until = start + timedelta(minutes=30)
    safe_request = BackgroundAdvanceRequest(
        provider_id="coffeebench", operation_id="safe-retry", until=until,
        binding={"company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna"},
    )
    safe_body = safe_request.model_dump(mode="json")
    worker.store.claim_background_operation("coffeebench", safe_request.operation_id, safe_body)
    worker.coffee_model_responses.load_or_claim("prepared-turn", "prepared-hash", safe_request.operation_id)

    unsafe_request = BackgroundAdvanceRequest(
        provider_id="coffeebench", operation_id="unsafe-retry", until=until,
        binding={"company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna"},
    )
    unsafe_body = unsafe_request.model_dump(mode="json")
    worker.store.claim_background_operation("coffeebench", unsafe_request.operation_id, unsafe_body)
    worker.coffee_model_responses.load_or_claim("submitted-turn", "submitted-hash", unsafe_request.operation_id)
    worker.coffee_model_responses.mark_submitted("submitted-turn", "submitted-hash")

    called = []

    async def fake_advance(snapshot, target, _binding):
        called.append(target)
        updated = {**snapshot, "cursor": target.isoformat()}
        return {"snapshot": updated, "events": [], "outcomes": [], "erp_intents": []}

    monkeypatch.setattr("servers.simulation.background_worker.advance_coffee", fake_advance)

    import asyncio

    result = asyncio.run(worker.advance(safe_request))
    assert result.cursor == until
    assert called == [until]

    with __import__("pytest").raises(BackgroundOperationUncertain):
        asyncio.run(worker.advance(unsafe_request))
    assert called == [until]
