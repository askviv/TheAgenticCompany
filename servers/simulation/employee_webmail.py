"""Send employee-authored messages through their persistent Roundcube browser."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit


class EmployeeWebmailError(RuntimeError):
    """The employee browser could not safely complete the native webmail action."""


_TYPING_DELAY_MS = 150


class EmployeeWebmailSender:
    """Use a managed employee Chrome profile and Roundcube's normal compose UI.

    Roundcube's TAC plugin supplies deterministic headers through its compose
    and pre-send hooks. This class does not call SMTP or infer delivery; the
    caller must reconcile the resulting native mailbox receipt.
    """

    def __init__(
        self,
        employee_desktops: Any,
        webmail_url: str,
        *,
        playwright_factory: Callable[[], Any] | None = None,
    ):
        self.employee_desktops = employee_desktops
        self.webmail_url = webmail_url
        self.playwright_factory = playwright_factory

    def send(
        self,
        employee_id: str,
        sender_email: str,
        sender_username: str,
        sender_password: str,
        recipient_email: str,
        subject: str,
        body: str,
        message_id: str,
        run_id: str,
        generation: int,
        *,
        in_reply_to: str | None,
        reply_message_url: str | None,
        verify_reply_source: Callable[[], None] | None,
        reply_to: str | None,
        automated: bool,
        on_submission_started: Callable[[], None],
    ) -> None:
        with self.employee_desktops.employee_activity(employee_id) as session:
            endpoint = session.get("cdp_endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise EmployeeWebmailError("employee desktop did not provide a CDP endpoint")
            self._send_in_browser(
                endpoint,
                sender_email,
                sender_username,
                sender_password,
                recipient_email,
                subject,
                body,
                message_id,
                run_id,
                generation,
                in_reply_to=in_reply_to,
                reply_message_url=reply_message_url,
                verify_reply_source=verify_reply_source,
                reply_to=reply_to,
                automated=automated,
                on_submission_started=on_submission_started,
            )

    def open_inbox(
        self,
        employee_id: str,
        email: str,
        username: str,
        password: str,
        *,
        session: dict[str, Any] | None = None,
    ) -> None:
        """Open the authenticated inbox and leave it visible without reading a message."""
        with self.employee_desktops.employee_action_lock(employee_id):
            active_session = session or self.employee_desktops.ensure_session(employee_id)
            self._show_inbox(active_session, email, username, password, "tac-employee-mail")

    def open_message(
        self,
        employee_id: str,
        email: str,
        username: str,
        password: str,
        message_url: str,
    ) -> None:
        """Open one exact native inbox message for a committed employee response."""
        with self.employee_desktops.employee_activity(employee_id) as session:
            endpoint = session.get("cdp_endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise EmployeeWebmailError("employee desktop did not provide a CDP endpoint")
            self._show_message(endpoint, email, username, password, message_url)

    def _show_message(
        self,
        endpoint: str,
        email: str,
        username: str,
        password: str,
        message_url: str,
    ) -> None:
        self._validate_same_origin(message_url)
        playwright = self._start_playwright()
        failure: BaseException | None = None
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            if len(browser.contexts) != 1:
                raise EmployeeWebmailError("employee desktop has an unexpected browser context count")
            page = self._automation_page(browser.contexts[0], "tac-employee-mail")
            page.evaluate("window.name = 'tac-employee-mail'")
            page.set_default_timeout(0)
            page.set_default_navigation_timeout(0)
            page.goto(self.webmail_url, wait_until="domcontentloaded", timeout=0)
            self._sign_in_if_needed(page, username, password)
            self._validate_webmail_origin(page)
            self._validate_account(page, email)
            page.goto(message_url, wait_until="domcontentloaded", timeout=0)
            page.locator("#messagebody").wait_for(state="visible")
            page.bring_to_front()
        except BaseException as error:
            failure = error
            if isinstance(error, EmployeeWebmailError):
                raise
            raise EmployeeWebmailError("employee's exact inbox message could not be opened") from error
        finally:
            try:
                playwright.stop()
            except BaseException as cleanup_error:
                if failure is not None:
                    failure.add_note(f"Playwright disconnect failed: {type(cleanup_error).__name__}")
                else:
                    raise EmployeeWebmailError("Playwright browser connection could not be closed") from cleanup_error

    def refresh_inbox(
        self,
        employee_id: str,
        email: str,
        username: str,
        password: str,
        *,
        session: dict[str, Any],
    ) -> None:
        """Visually refresh a running employee inbox after native IMAP activity."""
        with self.employee_desktops.employee_action_lock(employee_id):
            self._show_inbox(session, email, username, password, "tac-employee-inbox")

    def _show_inbox(
        self,
        session: dict[str, Any],
        email: str,
        username: str,
        password: str,
        page_name: str,
    ) -> None:
        endpoint = session.get("cdp_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise EmployeeWebmailError("employee desktop did not provide a CDP endpoint")
        playwright = self._start_playwright()
        failure: BaseException | None = None
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            if len(browser.contexts) != 1:
                raise EmployeeWebmailError("employee desktop has an unexpected browser context count")
            page = self._automation_page(browser.contexts[0], page_name)
            page.evaluate(f"window.name = '{page_name}'")
            page.set_default_timeout(0)
            page.set_default_navigation_timeout(0)
            page.goto(self.webmail_url, wait_until="domcontentloaded", timeout=0)
            self._sign_in_if_needed(page, username, password)
            self._validate_webmail_origin(page)
            self._validate_account(page, email)
            page.bring_to_front()
        except BaseException as error:
            failure = error
            if isinstance(error, EmployeeWebmailError):
                raise
            raise EmployeeWebmailError("employee webmail inbox could not be opened") from error
        finally:
            try:
                playwright.stop()
            except BaseException as cleanup_error:
                if failure is not None:
                    failure.add_note(f"Playwright disconnect failed: {type(cleanup_error).__name__}")
                else:
                    raise EmployeeWebmailError("Playwright browser connection could not be closed") from cleanup_error

    def _send_in_browser(
        self,
        endpoint: str,
        sender_email: str,
        sender_username: str,
        sender_password: str,
        recipient_email: str,
        subject: str,
        body: str,
        message_id: str,
        run_id: str,
        generation: int,
        *,
        in_reply_to: str | None,
        reply_message_url: str | None,
        verify_reply_source: Callable[[], None] | None,
        reply_to: str | None,
        automated: bool,
        on_submission_started: Callable[[], None],
    ) -> None:
        playwright = self._start_playwright()
        page = None
        attempted = False
        failure: BaseException | None = None
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            if len(browser.contexts) != 1:
                raise EmployeeWebmailError("employee desktop has an unexpected browser context count")
            context = browser.contexts[0]
            page = self._automation_page(context)
            page.evaluate("window.name = 'tac-employee-mail'")
            page.set_default_timeout(0)
            page.set_default_navigation_timeout(0)

            page.goto(self.webmail_url, wait_until="domcontentloaded", timeout=0)
            self._sign_in_if_needed(page, sender_username, sender_password)
            self._validate_webmail_origin(page)
            self._validate_account(page, sender_email)

            if in_reply_to is not None:
                if not reply_message_url or verify_reply_source is None:
                    raise EmployeeWebmailError(
                        "employee reply has no exact native inbox message reference"
                    )
                self._validate_same_origin(reply_message_url)
                page.goto(reply_message_url, wait_until="domcontentloaded", timeout=0)
                page.locator("#messagebody").wait_for(state="visible")
                verify_reply_source()
                reply_query = parse_qs(urlsplit(reply_message_url).query)
                source_uids = reply_query.get("_uid", [])
                if len(source_uids) != 1 or not source_uids[0].isdigit():
                    raise EmployeeWebmailError("employee reply reference has no exact native inbox UID")
                compose_request_modified = False
                correlation = self._correlation_values(
                    message_id, run_id, generation, in_reply_to,
                    reply_to, automated,
                )

                def preserve_native_reply(route):
                    nonlocal compose_request_modified
                    request_parts = urlsplit(route.request.url)
                    query = dict(parse_qsl(request_parts.query, keep_blank_values=True))
                    if (
                        request_parts.scheme == urlsplit(self.webmail_url).scheme
                        and request_parts.netloc.casefold() == urlsplit(self.webmail_url).netloc.casefold()
                        and query.get("_task") == "mail"
                        and query.get("_action") == "compose"
                        and query.get("_reply_uid") == source_uids[0]
                        and query.get("_mbox") == "INBOX"
                    ):
                        query.update(correlation)
                        correlated_url = urlunsplit((
                            request_parts.scheme,
                            request_parts.netloc,
                            request_parts.path,
                            urlencode(query),
                            request_parts.fragment,
                        ))
                        route.continue_(url=correlated_url)
                        compose_request_modified = True
                    else:
                        route.continue_()

                page.route("**/*", preserve_native_reply)
                reply_link = page.get_by_role("link", name="Reply", exact=True)
                try:
                    if reply_link.count() != 1:
                        raise EmployeeWebmailError("Roundcube message view did not expose one native Reply action")
                    reply_link.click()
                finally:
                    page.unroute("**/*", preserve_native_reply)
                if not compose_request_modified:
                    raise EmployeeWebmailError(
                        "Roundcube native Reply request did not preserve its exact UID and TAC context"
                    )
                page.locator("#form").wait_for(state="visible")
                page.wait_for_function(
                    "document.querySelector('#form')?.dataset.tacCorrelationReady === 'true'",
                    timeout=0,
                )
            else:
                compose_url = self._compose_url(
                    message_id, run_id, generation, in_reply_to, reply_to, automated
                )
                page.goto(compose_url, wait_until="domcontentloaded", timeout=0)
            form = page.locator("#form")
            form.wait_for(state="visible")
            page.wait_for_function(
                "document.querySelector('#form')?.dataset.tacCorrelationReady === 'true'",
                timeout=0,
            )
            self._verify_plugin_context(form, message_id, run_id, generation,
                                        in_reply_to, reply_to, automated)

            self._type_text(form.locator("#_to"), recipient_email)
            self._type_text(form.locator("#compose-subject"), subject)
            self._verify_sender(form, sender_email)
            body_field = form.locator("#composebody")
            if body_field.is_visible():
                self._type_text(body_field, body)
            else:
                editor_body = page.frame_locator("iframe#composebody_ifr").locator("body")
                editor_body.wait_for(state="visible")
                self._type_text(editor_body, body)

            # Elastic renders its native Send action immediately after the
            # compose form, so it is a page-level accessible control.
            send_button = page.get_by_role("button", name="Send", exact=True)
            if send_button.count() != 1:
                raise EmployeeWebmailError("Roundcube compose form did not expose one Send button")

            # The durable outbox boundary is committed immediately before the
            # browser's native Send action. Any later exception is uncertain.
            on_submission_started()
            attempted = True
            send_button.click(timeout=0)
        except BaseException as error:
            failure = error
            if page is not None and not attempted:
                try:
                    page.close()
                except BaseException as cleanup_error:
                    error.add_note(f"unsent compose cleanup failed: {type(cleanup_error).__name__}")
            if isinstance(error, EmployeeWebmailError):
                raise
            raise EmployeeWebmailError(
                "employee webmail browser action failed; submission state follows the durable outbox boundary"
            ) from error
        finally:
            try:
                playwright.stop()
            except BaseException as cleanup_error:
                if failure is not None:
                    failure.add_note(f"Playwright disconnect failed: {type(cleanup_error).__name__}")
                else:
                    raise EmployeeWebmailError("Playwright browser connection could not be closed") from cleanup_error

    def _automation_page(self, context: Any, page_name: str = "tac-employee-mail") -> Any:
        for existing in context.pages:
            if not existing.is_closed() and existing.evaluate("window.name") == page_name:
                return existing
        return context.new_page()

    @staticmethod
    def _type_text(locator: Any, text: str) -> None:
        """Type into a native compose field with visible, bounded key cadence."""
        locator.fill("")
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if line.endswith("\r"):
                line = line[:-1]
            if line:
                locator.press_sequentially(
                    line, delay=_TYPING_DELAY_MS, timeout=0
                )
            if index < len(lines) - 1:
                locator.press("Enter", timeout=0)

    def _start_playwright(self):
        playwright_factory = self.playwright_factory
        if playwright_factory is None:
            from playwright.sync_api import sync_playwright

            playwright_factory = sync_playwright
        return playwright_factory().start()

    def _sign_in_if_needed(self, page: Any, username: str, password: str) -> None:
        if "/sso/login" not in urlsplit(page.url).path:
            return
        page.get_by_label("E-mail", exact=True).fill(username)
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.wait_for_function(
            """() => {
              if (!location.pathname.includes('/sso/login')) return true;
              const errors = document.querySelectorAll(
                '[role="alert"], [aria-live="assertive"], .alert-danger, .error, .error-message, .invalid-feedback'
              );
              return Array.from(errors).some((node) => node.textContent.trim().length > 0);
            }""",
            timeout=0,
        )
        if "/sso/login" in urlsplit(page.url).path:
            raise EmployeeWebmailError("employee webmail sign-in was rejected")

    def _validate_webmail_origin(self, page: Any) -> None:
        expected = urlsplit(self.webmail_url)
        actual = urlsplit(page.url)
        if actual.scheme != expected.scheme or actual.netloc.casefold() != expected.netloc.casefold():
            raise EmployeeWebmailError("employee browser left the configured webmail origin")
        if actual.path.startswith("/sso/login"):
            raise EmployeeWebmailError("employee webmail sign-in did not complete")

    def _validate_same_origin(self, url: str) -> None:
        expected = urlsplit(self.webmail_url)
        actual = urlsplit(url)
        if actual.scheme != expected.scheme or actual.netloc.casefold() != expected.netloc.casefold():
            raise EmployeeWebmailError("employee reply reference is outside the configured webmail origin")

    def _validate_account(self, page: Any, expected_email: str) -> None:
        actual_email = page.evaluate("rcmail.env.tac_logged_in_email || null")
        if not isinstance(actual_email, str) or actual_email.casefold() != expected_email.casefold():
            raise EmployeeWebmailError("Roundcube authenticated mailbox does not match the employee identity")

    def _compose_url(
        self,
        message_id: str,
        run_id: str,
        generation: int,
        in_reply_to: str | None,
        reply_to: str | None,
        automated: bool,
    ) -> str:
        parts = urlsplit(self.webmail_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update({
            "_task": "mail",
            "_action": "compose",
            "_tac_message_id": message_id,
            "_tac_run_id": run_id,
            "_tac_generation": str(generation),
            "_tac_in_reply_to": in_reply_to or "",
            "_tac_reply_to": reply_to or "",
            "_tac_automated": "1" if automated else "0",
        })
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _verify_plugin_context(
        self,
        form: Any,
        message_id: str,
        run_id: str,
        generation: int,
        in_reply_to: str | None,
        reply_to: str | None,
        automated: bool,
    ) -> None:
        expected = {
            "_tac_message_id": message_id,
            "_tac_run_id": run_id,
            "_tac_generation": str(generation),
            "_tac_in_reply_to": in_reply_to or "",
            "_tac_reply_to": reply_to or "",
            "_tac_automated": "1" if automated else "0",
        }
        for name, value in expected.items():
            field = form.locator(f'input[name="{name}"]')
            if field.count() != 1 or field.input_value() != value:
                raise EmployeeWebmailError("Roundcube TAC correlation plugin is absent or incomplete")

    @staticmethod
    def _correlation_values(
        message_id: str,
        run_id: str,
        generation: int,
        in_reply_to: str | None,
        reply_to: str | None,
        automated: bool,
    ) -> dict[str, str]:
        return {
            "_tac_message_id": message_id,
            "_tac_run_id": run_id,
            "_tac_generation": str(generation),
            "_tac_in_reply_to": in_reply_to or "",
            "_tac_reply_to": reply_to or "",
            "_tac_automated": "1" if automated else "0",
        }

    def _verify_sender(self, form: Any, sender_email: str) -> None:
        field = form.locator("#_from")
        if field.count() != 1:
            raise EmployeeWebmailError("Roundcube compose form has no sender identity field")
        displayed = field.evaluate(
            "element => element.tagName === 'SELECT' "
            "? element.selectedOptions[0]?.textContent || '' "
            ": element.value || ''"
        )
        if not isinstance(displayed, str) or sender_email.casefold() not in displayed.casefold():
            raise EmployeeWebmailError("Roundcube sender identity does not match the configured employee")
