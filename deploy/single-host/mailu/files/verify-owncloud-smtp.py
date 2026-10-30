#!/usr/bin/env python3
"""Send an ownCloud Mailer test and confirm delivery by employee IMAP UID."""

from __future__ import annotations

import email
import email.policy
import imaplib
import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path


ROOT = Path("/data/theagentcompany/mailu")
SMTP_HOST = "mail.company.example.test"
EMPLOYEE_ID = "sarah-johnson"


def run(args: list[str], input_text: str | None = None) -> str:
    result = subprocess.run(args, input=input_text, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode})")
    return result.stdout


def main() -> None:
    global CURRENT_PHASE
    CURRENT_PHASE = "read-credentials"
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", help="reuse an existing verification subject without sending again")
    args = parser.parse_args()
    credentials = json.loads((ROOT / "employee-credentials.json").read_text(encoding="utf-8"))
    employee = credentials["employees"][EMPLOYEE_ID]["mail"]
    identity = next(item for item in json.loads((ROOT / "company-identities.json").read_text(encoding="utf-8"))["employees"]
                    if item["id"] == EMPLOYEE_ID)
    if employee["address"] != identity["email"]:
        raise RuntimeError("Employee address does not match the company roster")
    subject = args.subject or "TheAgentCompany ownCloud SMTP verification " + str(uuid.uuid4())
    def find_delivery() -> dict[str, str] | None:
        global CURRENT_PHASE
        CURRENT_PHASE = "connect-imap"
        with imaplib.IMAP4_SSL(SMTP_HOST, 2993) as client:
            client.login(employee["address"], employee["password"])
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("Employee INBOX could not be selected")
            status, data = client.uid("search", None, "SUBJECT", chr(34) + subject + chr(34))
            if status != "OK":
                raise RuntimeError("IMAP search failed")
            for uid in data[0].split():
                status, fetched = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID FROM TO)])")
                if status != "OK":
                    raise RuntimeError("IMAP message fetch failed")
                raw = next((part[1] for part in fetched if isinstance(part, tuple)), b"")
                message = email.message_from_bytes(raw, policy=email.policy.default)
                if (str(message.get("Subject", "")) == subject
                        and "postmaster@example.com" in str(message.get("From", ""))
                        and employee["address"] in str(message.get("To", ""))):
                    return {"uid": uid.decode("ascii"), "message_id": str(message.get("Message-ID") or "")}
        return None

    existing = find_delivery()
    sent = False
    if existing is None:
        CURRENT_PHASE = "send-owncloud-mailer"
        php = r'''require '/var/www/html/lib/base.php';
$request = json_decode(stream_get_contents(STDIN), true);
$mailer = \OC::$server->getMailer();
$message = $mailer->createMessage();
$message->setTo([$request['to'] => $request['name']]);
$message->setFrom([$request['from'] => 'TheAgentCompany ownCloud']);
$message->setSubject($request['subject']);
$message->setPlainBody('This is a private ownCloud SMTP delivery verification.');
$mailer->send($message);
echo 'sent';'''
        output = run(["docker", "exec", "-i", "--user", "www-data", "owncloud", "php", "-r", php],
                     json.dumps({"to": employee["address"], "name": identity["displayName"],
                                 "from": "postmaster@example.com", "subject": subject}))
        if output.strip() != "sent":
            raise RuntimeError("ownCloud Mailer did not confirm the message send")
        sent = True
        CURRENT_PHASE = "confirm-imap-delivery"
        matching = find_delivery()
        if matching is None:
            raise RuntimeError("The ownCloud test email was not found in the employee INBOX")
    else:
        matching = existing

    print(json.dumps({"status": "delivered", "application": "ownCloud Mailer",
                      "employee_id": EMPLOYEE_ID, "recipient": employee["address"],
                      "imap_folder": "INBOX", "imap_uid": matching["uid"],
                      "message_id": matching["message_id"], "subject": subject,
                      "sent_by_this_run": sent}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "phase": globals().get("CURRENT_PHASE", "startup"),
                          "error_type": type(error).__name__,
                          "detail": str(error) if type(error) is RuntimeError else "operation failed"}),
              file=sys.stderr)
        raise SystemExit(1)
