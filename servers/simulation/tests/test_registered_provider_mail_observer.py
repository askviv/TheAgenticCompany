import json

from servers.simulation.company_feedback_runtime import CompanyFeedbackRuntime
from servers.simulation.store import SQLiteStore


def test_registered_provider_mail_failure_is_durable_and_cleared_after_recovery(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    with store.transaction() as db:
        db.execute(
            "CREATE TABLE company_feedback_runtime_reports(provider TEXT PRIMARY KEY,result_json TEXT NOT NULL)"
        )
    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = store

    def unavailable():
        raise ConnectionError("private detail")

    runtime.provider_mail_observer = unavailable

    runtime._observe_registered_provider_mail()

    with store.transaction() as db:
        failure = db.execute(
            "SELECT result_json FROM company_feedback_runtime_reports WHERE provider='component_provider_mail'"
        ).fetchone()
    assert json.loads(failure["result_json"]) == {
        "status": "failed",
        "error_type": "ConnectionError",
    }

    runtime.provider_mail_observer = lambda: {"status": "ok", "processed": 0}
    runtime._observe_registered_provider_mail()
    with store.transaction() as db:
        assert db.execute(
            "SELECT 1 FROM company_feedback_runtime_reports WHERE provider='component_provider_mail'"
        ).fetchone() is None
