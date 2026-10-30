"""Company issue wiring and native systemd timer entry point. No benchmark runs."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import traceback

from .erpnext import ERPNextSiteAdmin
from .integration_contracts import resolve_company_issue_route
from .issue_handoffs import IssueHandoffService, IssueHandoffStore, SystemdIssueScheduler
from .mail import MailuGateway
from .store import SQLiteStore

LOG = logging.getLogger(__name__)


class CompanyIssueWorkflow:
    def __init__(self, config, *, mail=None, store=None, scheduler=None, native=None):
        self.config = config
        self.store = store or SQLiteStore(config.state_root / "state.sqlite")
        self.native = native or ERPNextSiteAdmin(config.erp_compose, config.erp_credentials_root.parent)
        profile = json.loads(config.company_profile_file.read_text())
        route, employee = resolve_company_issue_route(profile)
        actor = next((actor for actor in config.mail["actors"] if actor["actor_id"] == route.actor_id), None)
        if actor is None or actor["email"].casefold() != employee["email"].casefold():
            raise ValueError("Company issue route does not match its authenticated employee mailbox actor")
        if mail is None:
            from .runtime import private_json
            mail = MailuGateway(config.mail, private_json(config.mail_credentials_file))
        scheduler = scheduler or SystemdIssueScheduler(
            config.issue_config_path, executable=sys.executable,
            source_root=Path(__file__).resolve().parents[2],
            state_root=config.state_root,
        )
        self.service = IssueHandoffService(IssueHandoffStore(self.store), mail, scheduler,
                                          actors=config.mail["actors"], erp_public_url=config.erp_public_url)
        self.error: str | None = None

    def _consume(self, result) -> None:
        self.service.register(result["notifications"])
        native_errors = result["errors"]
        errors = native_errors + self.service.process_pending()
        self.error = "native_issue_notification_failed" if errors else None
        if native_errors:
            LOG.error("company issue notification failures=%s", native_errors)

    def reconcile(self) -> None:
        binding = self.config.company_binding
        if binding is None:
            return
        documents = binding["documents"]
        company = documents["company"]
        if isinstance(company, dict):
            company = company["name"]
        self._consume(self.native.reconcile_company_issues(
            self.store.list_company_source_events(), {**binding, "company_id": "company", "erp_company": company},
        ))

    def maintenance(self) -> None:
        # Replay source-to-Issue mapping as well as mail receipts. A crash after
        # native commit but before host registration must not orphan the issue.
        self.reconcile()
        from .company_feedback_runtime import CompanyFeedbackRuntime
        from .runtime import private_json
        CompanyFeedbackRuntime(self.config, self.store, private_json(self.config.principals_file)).maintenance()

    def observe_mail(self) -> None:
        if self.service.process_pending():
            self.error = "tracked_issue_notification_processing_failed"

    def report_failure(self, error: Exception) -> None:
        self.error = type(error).__name__
        diagnostics=[]
        seen=set()
        def collect(failure):
            if id(failure) in seen:return
            seen.add(id(failure))
            diagnostics.append({"exception_type":type(failure).__name__,
                "stack":[(frame.filename,frame.lineno,frame.name)
                         for frame in traceback.extract_tb(failure.__traceback__)]})
            for member in getattr(failure,"exceptions",()):collect(member)
            cause=failure.__cause__ or failure.__context__
            if cause is not None:collect(cause)
        collect(error)
        LOG.error("company maintenance failed diagnostics=%s",diagnostics)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--issue-key")
    parser.add_argument("--maintenance", action="store_true")
    args = parser.parse_args()
    from .runtime import RuntimeConfig, private_json
    config = RuntimeConfig.model_validate(private_json(args.config))
    config.issue_config_path = args.config
    workflow = CompanyIssueWorkflow(config)
    try:
        if args.issue_key:
            workflow.service.process(args.issue_key)
        elif args.maintenance:
            workflow.maintenance()
        else:
            workflow.reconcile()
        if workflow.error:
            raise SystemExit(1)
    except Exception as error:
        workflow.report_failure(error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
