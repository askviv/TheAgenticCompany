"""Employee-facing external-provider portal, separate from the operator BFF."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
from pathlib import Path
import secrets
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware

from servers.simulation.provider_contracts import (
    ProviderPortalCaseRegistration, ProviderRequest, ProviderResponse,
    ProviderCasePrepareRequest, ProviderCasePrepareResponse,
    CompanyProviderPortalAction, ProviderBankActionReceipt, ProviderPortalFormSubmission,
    ProviderEvidenceReference,
)
from servers.simulation.provider_portal_store import ProviderPortalStore
from servers.simulation.store import SQLiteStore, StoreConflict

LOG = logging.getLogger(__name__)


class MailAuthenticationRejected(Exception):
    """The existing Mailu account rejected the supplied credentials."""


class PortalMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    client_message_id: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=20000)


class RegisteredProviderEmail(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider_actor_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=3, max_length=998)
    sender_actor_id: str = Field(min_length=1, max_length=200)
    recipient_actor_id: str = Field(min_length=1, max_length=200)
    in_reply_to: str | None = Field(default=None, max_length=998)
    references: list[str] = Field(default_factory=list, max_length=20)
    subject: str = Field(default="", max_length=998)
    body: str = Field(min_length=1, max_length=20000)
    received_at: str = Field(min_length=1, max_length=64)
    mailbox_uid: int = Field(gt=0)
    uidvalidity: int = Field(gt=0)


def _read_private_key(path: Path) -> str:
    mode = path.stat().st_mode
    if mode & 0o077:
        raise PermissionError("Provider portal secret files must be private to their owner")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise ValueError("Provider portal secret is not configured")
    return value


def _log_failure(operation: str, error: Exception, *, case_id: str | None = None,
                 message_id: str | None = None) -> None:
    import traceback

    cause_types = []
    cause = error.__cause__ or error.__context__
    while cause is not None:
        cause_types.append(type(cause).__name__)
        cause = cause.__cause__ or cause.__context__
    frames = [(frame.filename, frame.lineno, frame.name)
              for frame in traceback.extract_tb(error.__traceback__)]
    LOG.error(
        "company provider portal operation failed operation=%s exception_type=%s causes=%s case_ref=%s message_ref=%s frames=%s",
        operation, type(error).__name__, cause_types,
        hashlib.sha256(case_id.encode()).hexdigest()[:16] if case_id else None,
        hashlib.sha256(message_id.encode()).hexdigest()[:16] if message_id else None,
        frames,
    )


def authenticate_mailu(
    email: str, password: str, *, host: str, port: int, tls_ca_file: str | None = None,
) -> None:
    """Authenticate against the existing Mailu IMAP account without retaining credentials."""
    from imapclient import IMAPClient
    from servers.company.scenarios import _ssl_context

    client = IMAPClient(host, port=port, ssl=True, ssl_context=_ssl_context(tls_ca_file), timeout=15)
    from imapclient.exceptions import LoginError

    login_error = None
    try:
        client.login(email, password)
    except BaseException as exc:
        login_error = exc
    logout_error = None
    try:
        client.logout()
    except BaseException as exc:
        logout_error = exc
    if login_error is not None:
        if logout_error is not None:
            login_error.add_note("Mailu IMAP logout also failed: " + type(logout_error).__name__)
        if isinstance(login_error, LoginError):
            raise MailAuthenticationRejected from login_error
        raise login_error.with_traceback(login_error.__traceback__)
    if logout_error is not None:
        raise logout_error.with_traceback(logout_error.__traceback__)


class NativeProviderHTTPClient:
    def __init__(self, base_url: str, bearer_token: str, *, timeout: float = 180, session=None):
        import requests

        if not base_url or not bearer_token or timeout <= 0:
            raise ValueError("native τ provider worker connection is incomplete")
        self.base_url = base_url.rstrip("/")
        self.token = bearer_token
        self.timeout = timeout
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def handle(self, request: ProviderRequest) -> ProviderResponse:
        return self._post("/v1/external-provider/message", request, ProviderResponse)

    def prepare_case(self, request: ProviderCasePrepareRequest) -> ProviderCasePrepareResponse:
        return self._post("/v1/external-provider/prepare", request, ProviderCasePrepareResponse)

    def apply_portal_action(self, request: CompanyProviderPortalAction) -> ProviderBankActionReceipt:
        return self._post("/v1/external-provider/portal-action", request, ProviderBankActionReceipt)

    def _post(self, path: str, request: Any, response_type):
        try:
            response = self.session.post(
                self.base_url + path,
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + self.token},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response_type.model_validate(response.json())
        except Exception as exc:
            # Do not leak provider request/response bodies or bearer credentials.
            raise RuntimeError("native external-provider worker request failed") from exc


def create_app(
    *,
    store: SQLiteStore,
    profile: dict[str, Any],
    mail_config: dict[str, Any],
    mail_credentials: dict[str, dict[str, str]],
    session_secret: str,
    integration_secret: str,
    provider_worker: Any,
    mail_gateway: Any,
    provider_mail_actors: dict[str, str],
    portal_public_url: str,
    mail_authenticator: Callable[..., None] = authenticate_mailu,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> FastAPI:
    if len(session_secret) < 32 or len(integration_secret) < 32:
        raise ValueError("provider portal secrets must contain at least 32 characters")
    profile_emails: set[str] = set()
    staff: dict[str, dict[str, str]] = {}
    actor_by_email = {str(row["email"]).casefold(): row for row in mail_config["actors"]}
    credential_emails = {key.casefold() for key in mail_credentials}
    for employee in profile.get("employees", []):
        if not isinstance(employee, dict) or not isinstance(employee.get("email"), str):
            continue
        email = employee["email"]
        normalized = email.casefold()
        profile_emails.add(normalized)
        actor = actor_by_email.get(normalized)
        if actor is not None and normalized in credential_emails and actor.get("actor_id") != "viv":
            staff[normalized] = {
                "actor_id": str(actor["actor_id"]), "email": str(actor["email"]),
                "display_name": str(employee.get("displayName") or employee.get("id") or email),
            }
    mail_actor_by_id = {row["actor_id"]: row for row in mail_config["actors"]}
    assistant_email = profile.get("company", {}).get("assistantEmail")
    if not isinstance(assistant_email, str):
        raise ValueError("company assistant mailbox is not configured")
    viv_actor = actor_by_email.get(assistant_email.casefold())
    if viv_actor is None or viv_actor.get("actor_id") != "viv":
        raise ValueError("configured company assistant email does not match the managed Viv mailbox")
    required_providers = {"tau2-airline", "tau2-telecom", "tau2-banking"}
    if set(provider_mail_actors) != required_providers:
        raise ValueError("provider mail actor mappings must cover all configured τ service providers")
    for provider, actor_id in provider_mail_actors.items():
        if actor_id not in mail_actor_by_id or actor_id == "viv":
            raise ValueError("provider mail actor must be an existing non-Viv Mailu account")
    split_url = urlsplit(portal_public_url)
    if split_url.scheme not in {"http", "https"} or not split_url.netloc:
        raise ValueError("provider portal public URL must be an absolute HTTP(S) URL")

    portal_store = ProviderPortalStore(store)
    app = FastAPI(title="Company Provider Portal", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="company_provider_session",
        same_site="lax",
        https_only=split_url.scheme == "https",
        max_age=60 * 60 * 8,
    )
    app.state.portal_store = portal_store
    app.state.cookie_secure = split_url.scheme == "https"
    app.state.session_secret = session_secret

    def session_record(request: Request) -> dict[str, str] | None:
        token = request.session.get("session_token")
        if not isinstance(token, str) or not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return portal_store.session(token_hash, now().astimezone(timezone.utc).isoformat())

    def csrf(request: Request) -> None:
        saved = session_record(request)
        expected = saved.get("csrf_token") if saved else None
        supplied = request.headers.get("X-CSRF-Token")
        if not isinstance(expected, str) or not isinstance(supplied, str) or not hmac.compare_digest(
            expected.encode(), supplied.encode()
        ):
            raise HTTPException(status_code=403, detail="CSRF validation failed")

    def current_actor(request: Request) -> dict[str, str]:
        saved = session_record(request)
        actor = next((value for value in staff.values() if value["actor_id"] == saved["actor_id"]), None) if saved else None
        if actor is None:
            raise HTTPException(status_code=401, detail="Employee login required")
        return actor

    def deliver_company_request(case: dict[str, Any]) -> None:
        sender = staff.get(case["responsible_email"].casefold())
        sender_actor = mail_actor_by_id.get(case["responsible_actor_id"])
        if sender is None or sender["actor_id"] != case["responsible_actor_id"] or sender_actor is None:
            raise RuntimeError("provider case employee does not match the managed company mailbox directory")
        issue_digest = hashlib.sha256(case["case_id"].encode("utf-8")).hexdigest()
        message_id = f"<company-provider-case-{issue_digest}@theagentcompany.local>"
        subject = "Please handle this provider request: " + case["subject"]
        run_id = "company-provider-case:" + case["case_id"]
        provider_actor_id = provider_mail_actors[case["provider_id"]]
        current = now().astimezone(timezone.utc).isoformat()
        saved = portal_store.claim_notification(case["case_id"], message_id, subject, current)
        if saved["status"] == "sent":
            return
        if saved["status"] in {"uncertain", "submitting"}:
            receipt = mail_gateway.find_receipt(viv_actor, message_id, subject, run_id, 0)
            if receipt is None:
                raise RuntimeError("provider case notification delivery is awaiting mailbox reconciliation")
            portal_store.complete_notification(case["case_id"], receipt, now().astimezone(timezone.utc).isoformat())
            return
        body = (
            case["business_description"] + "\n\nRequested outcome: "
            + case["requested_outcome"]
            + portal_store._private_context_text(case.get("private_factual_context", []))
            + "\n\nOpen this case in the Company Provider Portal: "
            + portal_public_url.rstrip("/") + "/?case=" + quote(case["case_id"], safe="")
        )
        try:
            receipt = mail_gateway.send(
                sender_actor, viv_actor, subject, body, message_id, run_id, 0,
                reply_to_actor_id=provider_actor_id,
                on_submission_started=lambda: portal_store.notification_submission_started(
                    case["case_id"], now().astimezone(timezone.utc).isoformat()
                ),
            )
            portal_store.complete_notification(case["case_id"], receipt, now().astimezone(timezone.utc).isoformat())
        except Exception as exc:
            if portal_store.notification(case["case_id"])["status"] == "submitting":
                portal_store.mark_notification_uncertain(
                    case["case_id"], exc, now().astimezone(timezone.utc).isoformat()
                )
            raise

    def process_registered_email(reply: RegisteredProviderEmail | dict[str, Any]) -> dict[str, Any]:
        """Process one trusted, exact-thread Viv reply with durable message idempotency."""
        reply = RegisteredProviderEmail.model_validate(reply)
        roots = [reply.in_reply_to, *reply.references]
        cases = [portal_store.case_for_notification_message_id(root) for root in roots if root]
        matched = {case["case_id"]: case for case in cases if case is not None}
        if len(matched) != 1:
            raise ValueError("registered email does not resolve to one active provider case thread")
        case = next(iter(matched.values()))
        provider_actor_id = provider_mail_actors[case["provider_id"]]
        if (reply.provider_actor_id != provider_actor_id or reply.recipient_actor_id != provider_actor_id
                or reply.sender_actor_id not in {viv_actor["actor_id"], case["responsible_actor_id"]}):
            raise PermissionError("registered provider email sender or recipient does not match the managed thread")
        root_id = portal_store.notification(case["case_id"])["message_id"]
        if root_id not in roots:
            raise ValueError("registered email is not in the exact provider request thread")
        reply_recipient = mail_actor_by_id[reply.sender_actor_id]
        request_facts = reply.model_dump(mode="json")
        request_hash = hashlib.sha256(json.dumps(
            request_facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        now_value = now().astimezone(timezone.utc).isoformat()
        mail_status, saved_mail = portal_store.claim_registered_email(
            provider_actor_id=provider_actor_id, message_id=reply.message_id,
            case_id=case["case_id"], uidvalidity=reply.uidvalidity, mailbox_uid=reply.mailbox_uid,
            request_hash=request_hash, created_at=now_value,
        )
        if mail_status == "processed":
            result = json.loads(saved_mail["response_json"])
            portal_store.advance_mail_cursor(provider_actor_id, case["case_id"], reply.uidvalidity,
                                              reply.mailbox_uid, reply.message_id, now_value)
            return result
        client_message_id = hashlib.sha256(reply.message_id.encode("utf-8")).hexdigest()
        operation_id = hashlib.sha256(
            (case["conversation_id"] + "\\0" + reply.message_id).encode("utf-8")
        ).hexdigest()
        reply_message_id = f"<{hashlib.sha256((reply.message_id + ':reply').encode()).hexdigest()}@provider-portal.company>"
        status, saved = portal_store.claim_message(
            case["case_id"], client_message_id, operation_id, reply.message_id,
            reply.body, reply.received_at,
        )
        portal_store.record_inbound_received(case["case_id"], client_message_id, {
            "message_id": reply.message_id, "in_reply_to": reply.in_reply_to,
            "uidvalidity": reply.uidvalidity, "mailbox_uid": reply.mailbox_uid,
            "sender_actor_id": reply.sender_actor_id, "recipient_actor_id": reply.recipient_actor_id,
            "received_at": reply.received_at,
        })
        run_id = "company-provider:" + case["case_id"]
        subject = "Re: " + (reply.subject or case["subject"])
        response = None
        if status == "sent":
            result = {"status": "processed", "case_id": case["case_id"],
                      "message_id": reply.message_id, "reply_message_id": saved["reply_message_id"]}
            portal_store.complete_registered_email(provider_actor_id, reply.message_id, result)
            portal_store.advance_mail_cursor(provider_actor_id, case["case_id"], reply.uidvalidity,
                                              reply.mailbox_uid, reply.message_id, now_value)
            return result
        if status in {"reply_uncertain", "reply_submitting"}:
            receipt = mail_gateway.find_receipt(reply_recipient, reply_message_id, subject, run_id, 0)
            if receipt is None:
                raise RuntimeError("provider email reply delivery is awaiting mailbox reconciliation")
            portal_store.record_reply_receipt(case["case_id"], client_message_id, receipt)
            result = {"status": "processed", "case_id": case["case_id"],
                      "message_id": reply.message_id, "reply_message_id": reply_message_id}
            portal_store.complete_registered_email(provider_actor_id, reply.message_id, result)
            portal_store.advance_mail_cursor(provider_actor_id, case["case_id"], reply.uidvalidity,
                                              reply.mailbox_uid, reply.message_id, now_value)
            return result
        if status == "provider_uncertain":
            portal_store.resume_native_request(case["case_id"], client_message_id)
            status = "outbound_sent"
        if status == "provider_completed" and saved and saved.get("native_response_json"):
            response = ProviderResponse.model_validate_json(saved["native_response_json"])
        else:
            native_request = ProviderRequest(
                provider_id=case["provider_id"], operation_id=operation_id,
                conversation_id=case["conversation_id"], task_id=case["task_id"],
                company_actor_id=case["responsible_actor_id"],
                company_account_reference=case["company_account_reference"],
                company_issue_reference=case["issue_reference"], message_id=reply.message_id,
                in_reply_to=root_id, subject=reply.subject, body=reply.body,
            )
            response = provider_worker.handle(native_request)
            if response.status != "completed" or not response.body:
                raise RuntimeError("native provider email response is uncertain")
            if status != "provider_completed":
                portal_store.complete_provider(case["case_id"], client_message_id, {
                    "body": response.body, "reply_message_id": reply_message_id,
                    **response.model_dump(mode="json"),
                })
        provider_actor = mail_actor_by_id[provider_actor_id]
        try:
            receipt = mail_gateway.send(
                provider_actor, reply_recipient, subject, response.body, reply_message_id,
                run_id, 0, in_reply_to=reply.message_id, automated=True,
                reply_to_actor_id=provider_actor_id,
                on_submission_started=lambda: portal_store.mark_submission_started(
                    case["case_id"], client_message_id, "reply_submitting"
                ),
            )
            portal_store.complete_delivery(case["case_id"], client_message_id, receipt)
        except Exception as exc:
            _log_failure("registered_provider_email_reply", exc, case_id=case["case_id"],
                         message_id=reply.message_id)
            latest = portal_store.message(case["case_id"], client_message_id)
            if latest and latest["status"] == "reply_submitting":
                portal_store.mark_uncertain(case["case_id"], client_message_id, exc, "reply_submitting")
            raise
        result = {"status": "processed", "case_id": case["case_id"],
                  "message_id": reply.message_id, "reply_message_id": reply_message_id}
        portal_store.complete_registered_email(provider_actor_id, reply.message_id, result)
        portal_store.advance_mail_cursor(provider_actor_id, case["case_id"], reply.uidvalidity,
                                          reply.mailbox_uid, reply.message_id, now_value)
        return result

    app.state.process_registered_email = process_registered_email

    @app.get("/", response_class=HTMLResponse)
    def portal_shell():
        return HTMLResponse((Path(__file__).with_name("provider_portal.html")).read_text(encoding="utf-8"))

    @app.get("/api/session")
    def session(request: Request):
        saved = session_record(request)
        actor = next((value for value in staff.values() if value["actor_id"] == saved["actor_id"]), None) if saved else None
        return {"authenticated": actor is not None,
                "employee": {"display_name": actor["display_name"], "email": actor["email"]} if actor else None,
                "csrf_token": saved["csrf_token"] if actor and saved else None}

    @app.post("/api/login")
    async def login(request: Request):
        try:
            value = await request.json()
            email = str(value.get("email", "")).strip()
            password = value.get("password")
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Email and password are required") from None
        actor = staff.get(email.casefold())
        if email.casefold() in profile_emails and actor is None:
            raise HTTPException(status_code=503, detail="This employee mailbox is not configured for the provider portal")
        if actor is None or not isinstance(password, str) or not password:
            raise HTTPException(status_code=401, detail="Company employee login failed")
        try:
            mail_authenticator(
                actor["email"], password,
                host=mail_config.get("mailu_imap_host", "mail.company.example.test"),
                port=int(mail_config.get("mailu_imap_port", 2993)),
                tls_ca_file=mail_config.get("tls_ca_file"),
            )
        except MailAuthenticationRejected as exc:
            raise HTTPException(status_code=401, detail="Company employee login failed") from exc
        except Exception as exc:
            _log_failure("mailbox_authentication", exc)
            raise HTTPException(status_code=503, detail="Company mailbox authentication is unavailable") from exc
        token, csrf_token = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        current_time = now().astimezone(timezone.utc)
        expiry = current_time.timestamp() + 8 * 60 * 60
        portal_store.create_session(
            hashlib.sha256(token.encode("utf-8")).hexdigest(), actor["actor_id"],
            csrf_token, datetime.fromtimestamp(expiry, timezone.utc).isoformat(), current_time.isoformat(),
        )
        request.session.clear()
        request.session["session_token"] = token
        return {"authenticated": True, "employee": {"display_name": actor["display_name"], "email": actor["email"]},
                "csrf_token": csrf_token}

    @app.post("/api/logout")
    def logout(request: Request):
        current_actor(request)
        csrf(request)
        token = request.session.get("session_token")
        if isinstance(token, str):
            portal_store.delete_session(hashlib.sha256(token.encode("utf-8")).hexdigest())
        request.session.clear()
        return {"authenticated": False}

    @app.get("/api/cases")
    def list_cases(request: Request):
        actor = current_actor(request)
        return {"cases": portal_store.cases_for_actor(actor["actor_id"])}

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str, request: Request):
        actor = current_actor(request)
        case = portal_store.case(case_id)
        if case is None or case["responsible_actor_id"] != actor["actor_id"]:
            raise HTTPException(status_code=404, detail="Provider case was not found")
        public = {key: case[key] for key in (
            "case_id", "provider_id", "subject", "business_description", "requested_outcome", "created_at"
        )}
        public["private_factual_context"] = case.get("private_factual_context", [])
        public["messages"] = portal_store.messages(case_id)
        setup = provider_worker.prepare_case(ProviderCasePrepareRequest(
            provider_id=case["provider_id"], task_id=case["task_id"],
            conversation_id=case["conversation_id"], company_actor_id=case["responsible_actor_id"],
            company_account_reference=case["company_account_reference"],
            company_issue_reference=case["issue_reference"],
        ))
        public["account"] = setup.account.model_dump(mode="json") if setup.account else None
        public["forms"] = [form.model_dump(mode="json") for form in setup.forms]
        return public

    @app.post("/api/cases/{case_id}/messages")
    def post_message(case_id: str, message: PortalMessage, request: Request):
        with portal_store.locked_case(case_id):
            return _post_message_locked(case_id, message, request)

    @app.post("/api/cases/{case_id}/actions")
    def post_customer_action(case_id: str, submission: ProviderPortalFormSubmission, request: Request):
        with portal_store.locked_case(case_id):
            actor = current_actor(request)
            csrf(request)
            case = portal_store.case(case_id)
            if case is None or case["responsible_actor_id"] != actor["actor_id"]:
                raise HTTPException(status_code=404, detail="Provider case was not found")
            setup = provider_worker.prepare_case(ProviderCasePrepareRequest(
                provider_id=case["provider_id"], task_id=case["task_id"],
                conversation_id=case["conversation_id"], company_actor_id=actor["actor_id"],
                company_account_reference=case["company_account_reference"],
                company_issue_reference=case["issue_reference"],
            ))
            if setup.status == "conflict" or setup.initialization_status == "conflict":
                raise HTTPException(status_code=409, detail="The linked native provider account has conflicting setup data")
            form = next((item for item in setup.forms if item.source_tool_name == submission.form_id), None)
            if form is None:
                raise HTTPException(status_code=422, detail="This customer form is not available for the linked provider case")
            schema = form.argument_schema
            properties = schema.get("properties", {})
            if not isinstance(properties, dict) or set(submission.arguments) - set(properties):
                raise HTTPException(status_code=422, detail="Form fields do not match the linked provider form")
            required = set(schema.get("required", []))
            if required - set(submission.arguments):
                raise HTTPException(status_code=422, detail="Complete the required provider form fields")
            user_id = (
                setup.account.source_id
                if setup.account is not None and setup.account.entity_type in {"BankingCustomer", "TelecomCustomer"}
                else None
            )
            arguments = dict(submission.arguments)
            if form.native_user_tool == "call_discoverable_user_tool":
                arguments = {
                    "discoverable_tool_name": form.source_tool_name,
                    "arguments": json.dumps(arguments, sort_keys=True),
                }
            operation_id = hashlib.sha256(
                (case_id + "\0" + submission.client_action_id).encode("utf-8")
            ).hexdigest()
            try:
                receipt = provider_worker.apply_portal_action(CompanyProviderPortalAction(
                    provider_id=case["provider_id"], operation_id=operation_id,
                    conversation_id=case["conversation_id"], company_actor_id=actor["actor_id"],
                    company_account_reference=case["company_account_reference"],
                    company_issue_reference=case["issue_reference"], mapped_provider_user_id=user_id,
                    native_user_tool=form.native_user_tool, arguments=arguments,
                    portal_event="portal_action_confirmed",
                    evidence_references=[ProviderEvidenceReference(
                        source="company_provider_portal",
                        reference=f"provider-portal-action:{case_id}:{submission.client_action_id}",
                    )], correlation_id=operation_id,
                ))
            except Exception as exc:
                _log_failure("provider_customer_portal_action", exc, case_id=case_id,
                             message_id=submission.client_action_id)
                raise HTTPException(status_code=503, detail="Provider action status needs reconciliation") from exc
            return {"status": receipt.status, "form_id": submission.form_id}

    def _post_message_locked(case_id: str, message: PortalMessage, request: Request):
        actor = current_actor(request)
        csrf(request)
        case = portal_store.case(case_id)
        if case is None or case["responsible_actor_id"] != actor["actor_id"]:
            raise HTTPException(status_code=404, detail="Provider case was not found")
        provider_actor_id = provider_mail_actors.get(case["provider_id"])
        provider_actor = mail_actor_by_id.get(provider_actor_id)
        employee_actor = mail_actor_by_id.get(actor["actor_id"])
        if provider_actor is None or employee_actor is None:
            raise HTTPException(status_code=503, detail="Provider email routing is not configured")
        now_value = now().astimezone(timezone.utc).isoformat()
        message_id = f"<{hashlib.sha256((case_id + ':' + message.client_message_id).encode()).hexdigest()}@provider-portal.company>"
        operation_id = hashlib.sha256((case["conversation_id"] + "\0" + message.client_message_id).encode()).hexdigest()
        reply_message_id = f"<{hashlib.sha256((message_id + ':reply').encode()).hexdigest()}@provider-portal.company>"
        previous_replies = [row["reply_message_id"] for row in portal_store.messages(case_id)
                            if row["reply_message_id"] and row["client_message_id"] != message.client_message_id]
        previous_reply_id = previous_replies[-1] if previous_replies else None
        try:
            status, saved = portal_store.claim_message(
                case_id, message.client_message_id, operation_id, message_id, message.body, now_value
            )
        except (ValueError, KeyError, StoreConflict) as exc:
            raise HTTPException(status_code=409, detail="Provider message conflicts with recorded case state") from exc
        if status == "sent":
            return {"status": "sent", "message_id": message_id, "reply_message_id": saved["reply_message_id"]}
        subject = "Provider case " + case["issue_reference"] + ": " + case["subject"]
        run_id = "company-provider:" + case_id
        if status in {"outbound_uncertain", "outbound_submitting"}:
            receipt = mail_gateway.find_receipt(
                provider_actor, message_id, subject, run_id, 0,
            )
            if receipt is None:
                raise HTTPException(status_code=409, detail="Provider email delivery is still being reconciled")
            portal_store.record_reconciled_outbound(case_id, message.client_message_id, receipt)
            status = "outbound_sent"
            saved = portal_store.message(case_id, message.client_message_id)
        elif status in {"reply_uncertain", "reply_submitting"}:
            receipt = mail_gateway.find_receipt(
                employee_actor, reply_message_id, "Re: " + subject, run_id, 0,
            )
            if receipt is not None:
                portal_store.record_reply_receipt(case_id, message.client_message_id, receipt)
                return {"status": "sent", "message_id": message_id, "reply_message_id": reply_message_id}
            raise HTTPException(status_code=409, detail="Provider interaction is still being reconciled")
        elif status == "provider_uncertain":
            # Native provider operation IDs are durable and idempotent. Replaying
            # the same exact request reconciles a committed response or reports
            # that the worker still cannot confirm its prior operation.
            portal_store.resume_native_request(case_id, message.client_message_id)
            status = "outbound_sent"

        if status == "started":
            try:
                receipt = mail_gateway.send(
                    employee_actor, provider_actor, subject, message.body, message_id,
                    run_id, 0,
                    in_reply_to=previous_reply_id,
                    on_submission_started=lambda: portal_store.mark_submission_started(
                        case_id, message.client_message_id, "outbound_submitting"
                    ),
                )
                portal_store.record_outbound_receipt(case_id, message.client_message_id, receipt)
                status = "outbound_sent"
                saved = portal_store.message(case_id, message.client_message_id)
            except Exception as exc:
                _log_failure("provider_message_email_submission", exc, case_id=case_id,
                             message_id=message.client_message_id)
                latest = portal_store.message(case_id, message.client_message_id)
                if latest["status"] == "outbound_submitting":
                    portal_store.mark_uncertain(
                        case_id, message.client_message_id, exc, "outbound_submitting"
                    )
                raise HTTPException(status_code=503, detail="Provider email submission needs reconciliation") from exc

        native_request = ProviderRequest(
            provider_id=case["provider_id"], operation_id=operation_id,
            conversation_id=case["conversation_id"], task_id=case["task_id"],
            company_actor_id=actor["actor_id"], company_account_reference=case["company_account_reference"],
            company_issue_reference=case["issue_reference"], message_id=message_id,
            in_reply_to=previous_reply_id, subject=subject, body=message.body,
        )
        try:
            if status == "provider_completed" and saved and saved.get("reply_body"):
                response = ProviderResponse.model_validate_json(saved["native_response_json"])
            else:
                response = provider_worker.handle(native_request)
            if response.status != "completed" or not response.body:
                portal_store.mark_uncertain(case_id, message.client_message_id,
                                            RuntimeError("provider response is uncertain"), "provider")
                raise HTTPException(status_code=202, detail="Provider response is still being reconciled")
            if status != "provider_completed":
                # Persist the native result before attempting email; retries do not
                # re-run native provider actions after a completed response.
                portal_store.complete_provider(case_id, message.client_message_id, {
                    "body": response.body, "reply_message_id": reply_message_id,
                    **response.model_dump(mode="json"),
                })
            receipt = mail_gateway.send(
                provider_actor, employee_actor,
                "Re: " + subject, response.body, reply_message_id,
                run_id, 0, in_reply_to=message_id, automated=True,
                on_submission_started=lambda: portal_store.mark_submission_started(
                    case_id, message.client_message_id, "reply_submitting"
                ),
            )
            portal_store.complete_delivery(case_id, message.client_message_id, receipt)
            return {"status": "sent", "message_id": message_id, "reply_message_id": reply_message_id}
        except HTTPException:
            raise
        except Exception as exc:
            _log_failure("provider_interaction", exc, case_id=case_id,
                         message_id=message.client_message_id)
            latest = portal_store.message(case_id, message.client_message_id)
            stage = "reply_submitting" if latest["status"] == "reply_submitting" else "provider"
            if latest["status"] in {"reply_submitting", "outbound_sent"}:
                portal_store.mark_uncertain(case_id, message.client_message_id, exc, stage)
            raise HTTPException(status_code=503, detail="Provider interaction needs delivery reconciliation") from exc

    @app.post("/internal/cases")
    async def register_case(
        registration: ProviderPortalCaseRegistration,
        authorization: str | None = Header(default=None),
    ):
        if not isinstance(authorization, str) or not hmac.compare_digest(
            authorization.encode("utf-8"), ("Bearer " + integration_secret).encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="Internal integration authentication failed")
        employee = staff.get(registration.responsible_email.casefold())
        if employee is None or employee["actor_id"] != registration.responsible_actor_id:
            raise HTTPException(status_code=422, detail="Provider case employee identity is not a managed company mailbox")
        if registration.provider_id not in provider_mail_actors:
            raise HTTPException(status_code=503, detail="Provider email routing is not configured")
        try:
            prepared = provider_worker.prepare_case(ProviderCasePrepareRequest(
                provider_id=registration.provider_id, task_id=registration.task_id,
                conversation_id=registration.conversation_id,
                company_actor_id=registration.responsible_actor_id,
                company_account_reference=registration.company_account_reference,
                company_issue_reference=registration.issue_reference,
            ))
            if prepared.status == "conflict" or prepared.initialization_status == "conflict":
                raise HTTPException(status_code=409, detail="Native provider setup conflicts with existing provider state")
            registration = ProviderPortalCaseRegistration.model_validate({
                **registration.model_dump(mode="json"),
                "subject": prepared.subject,
                "business_description": prepared.business_description,
                "requested_outcome": prepared.requested_outcome,
                "private_factual_context": [fact.model_dump(mode="json")
                                            for fact in prepared.private_factual_context],
            })
            with portal_store.locked_case(registration.case_id):
                row = portal_store.register_case(registration.model_dump(mode="json"))
                deliver_company_request(row)
        except HTTPException:
            raise
        except (ValueError, KeyError, StoreConflict) as exc:
            raise HTTPException(status_code=409, detail="Provider case registration conflicts with existing state") from exc
        except Exception as exc:
            _log_failure("case_registration_or_notification", exc, case_id=registration.case_id)
            raise HTTPException(status_code=503, detail="Provider case was recorded but employee email delivery needs reconciliation") from exc
        return {"case_id": row["case_id"], "status": "registered"}

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "company_provider_portal"}

    @app.get("/internal/registered-mail-cases")
    def registered_mail_cases(authorization: str | None = Header(default=None)):
        if not isinstance(authorization, str) or not hmac.compare_digest(
            authorization.encode("utf-8"), ("Bearer " + integration_secret).encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="Internal integration authentication failed")
        rows = []
        for case in portal_store.registered_email_cases():
            provider_actor_id = provider_mail_actors[case["provider_id"]]
            cursor = portal_store.mail_cursor(provider_actor_id, case["case_id"])
            rows.append({
                "case_id": case["case_id"], "provider_id": case["provider_id"],
                "provider_actor_id": provider_actor_id,
                "responsible_actor_id": case["responsible_actor_id"],
                "root_message_id": case["message_id"],
                "after_uid": cursor["after_uid"] if cursor else 0,
                "uidvalidity": str(cursor["uidvalidity"]) if cursor else None,
            })
        return {"cases": rows}

    @app.post("/internal/observe-mail")
    def observe_registered_mail(
        reply: RegisteredProviderEmail,
        authorization: str | None = Header(default=None),
    ):
        if not isinstance(authorization, str) or not hmac.compare_digest(
            authorization.encode("utf-8"), ("Bearer " + integration_secret).encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="Internal integration authentication failed")
        try:
            return process_registered_email(reply)
        except (PermissionError, ValueError, StoreConflict) as exc:
            raise HTTPException(status_code=409, detail="Registered provider email identity conflicts") from exc
        except Exception as exc:
            _log_failure("registered_provider_email", exc, message_id=reply.message_id)
            raise HTTPException(status_code=503, detail="Registered provider email needs reconciliation") from exc

    return app
