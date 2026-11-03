from contextlib import contextmanager
import json
from threading import Event
from types import SimpleNamespace

from servers.simulation.mail import MailuGateway
from servers.simulation.runtime import MailWorkers


def test_native_employee_mail_activity_refreshes_only_running_inbox_and_reports_failures(tmp_path):
    actor = {"actor_id": "sarah", "email": "sarah@example.test", "kind": "employee"}
    profile = tmp_path / "company-profile.json"
    profile.write_text(json.dumps({"employees": [{"id": "sarah-johnson", "email": actor["email"]}]}))

    for desktop_running, browser_fails in ((True, False), (False, False), (True, True)):
        arrived = Event()
        watching = Event()
        existing_checked = Event()
        browser_refreshed = Event()
        browser_calls = []

        class Manager:
            company_profile_path = profile

            @contextmanager
            def employee_action_lock(self, _employee_id):
                yield

            def existing_session(self, employee_id):
                assert employee_id == "sarah-johnson"
                existing_checked.set()
                return ({"cdp_endpoint": "ws://127.0.0.1:9223/devtools/browser/active"}
                        if desktop_running else None)

            def ensure_session(self, _employee_id):
                raise AssertionError("mail activity must never start a stopped desktop")

        class Browser:
            def refresh_inbox(self, *args, **kwargs):
                browser_calls.append((args, kwargs))
                browser_refreshed.set()
                if browser_fails:
                    raise RuntimeError("synthetic browser failure")

        gateway = MailuGateway(
            {"actors": [actor], "webmail_url": "https://mail.example.test/webmail/"},
            {actor["email"]: {"username": "sarah-login", "password": "private"}},
            employee_desktops=Manager(), webmail_sender=Browser(),
        )
        delivered = False

        def wait_for_activity(_actor, timeout=30):
            nonlocal delivered
            watching.set()
            if not delivered:
                arrived.wait()
                delivered = True
                return True
            workers.stopped.wait()
            return False

        gateway.wait_for_activity = wait_for_activity
        engine = SimpleNamespace(
            mail=gateway,
            store=SimpleNamespace(list_active_runs=lambda: []),
        )
        workers = MailWorkers(engine, actors=[actor])
        workers.start()
        assert watching.wait(timeout=2)
        arrived.set()
        assert existing_checked.wait(timeout=2)
        workers._employee_inbox_queues["sarah"].join()

        assert len(browser_calls) == (1 if desktop_running else 0)
        if desktop_running and browser_fails:
            assert browser_refreshed.is_set()
            assert workers.errors["employee_inbox:sarah"] == "RuntimeError"
            assert workers.health_components()["employee_inboxes"] == "failed"
        elif desktop_running:
            assert browser_refreshed.is_set()
            assert "employee_inbox:sarah" not in workers.errors
        else:
            assert "employee_inbox:sarah" not in workers.errors
            assert "employee_inboxes" not in workers.health_components()

        workers.stop()


def test_one_blocked_employee_inbox_does_not_block_another_and_activity_coalesces():
    sarah_started = Event()
    release_sarah = Event()
    li_refreshed = Event()
    sarah_refreshes = []

    class Mail:
        def refresh_employee_inbox_if_running(self, actor):
            if actor["actor_id"] == "sarah":
                sarah_refreshes.append(True)
                sarah_started.set()
                release_sarah.wait()
            else:
                li_refreshed.set()
            return True

    workers = MailWorkers(SimpleNamespace(mail=Mail(), store=None), actors=[])
    sarah = {"actor_id": "sarah", "kind": "employee"}
    workers._enqueue_employee_inbox(sarah)
    assert sarah_started.wait(timeout=2)
    for _ in range(4):
        workers._enqueue_employee_inbox(sarah)
    workers._enqueue_employee_inbox({"actor_id": "li-ming", "kind": "employee"})
    assert li_refreshed.wait(timeout=2)
    workers._employee_inbox_queues["li-ming"].join()

    assert workers._employee_inbox_queues["sarah"].maxsize == 1
    assert workers.employee_inbox_coalesced_events["sarah"] == 3
    release_sarah.set()
    workers._employee_inbox_queues["sarah"].join()
    assert len(sarah_refreshes) == 2
    workers.stop()


def test_stop_racing_mail_activity_does_not_start_visual_worker():
    actor = {"actor_id": "sarah", "kind": "employee"}
    started = Event()
    release = Event()

    class Mail:
        def refresh_employee_inbox_if_running(self, _actor):
            raise AssertionError("stopped watcher must not start a browser action")

        def wait_for_activity(self, _actor, timeout=30):
            started.set()
            release.wait()
            return True

    workers = MailWorkers(SimpleNamespace(mail=Mail(), store=SimpleNamespace(list_active_runs=lambda: [])),
                          actors=[actor])
    workers.start()
    assert started.wait(timeout=2)
    workers.stop()
    release.set()
    watcher = next(thread for thread in workers.threads if thread.name == "simulation-mail-sarah")
    watcher.join(timeout=2)
    assert not watcher.is_alive()
    assert workers._employee_inbox_queues == {}
