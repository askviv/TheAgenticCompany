# Company scenario infrastructure

This CLI supports future scenario definitions. It does not change the experiment
dashboard or existing benchmark tasks, and contains no business experiment catalogue.

Install `requirements.txt` in a dedicated virtual environment. On the host the
prepared interpreter is `/data/theagentcompany/company-tools-venv/bin/python`.
Run from `/data/theagentcompany/enterprise/source`:

```sh
/data/theagentcompany/company-tools-venv/bin/python -m servers.company.scenarios run \
  --fixture /private/path/scenario.json --credentials /private/path/credentials.json \
  --database /data/theagentcompany/scenarios/runs.sqlite
/data/theagentcompany/company-tools-venv/bin/python -m servers.company.scenarios inspect \
  --database /data/theagentcompany/scenarios/runs.sqlite
```

## Input contract

Fixtures are JSON files kept separately from operational logs. The required
top-level fields are `scenario_id`, `fixture_version`, `environment_version`,
`message_id` (a stable seed, not the eventual run Message-ID), `from_address`,
`subject`, `body`, and `employee_mailbox`.

For company scenarios, set `employee_id` to the exact central employee name
(for example, `sarah-johnson`) and configure `directory` with `mode: midpoint`,
`base_url` and `credentials_ref`. the host's private directory base URL is
`http://127.0.0.1:18121/midpoint`. The runner reads midPoint's native user API
and resolves the employee's mailbox address and immutable chat ID. The fixture
does not need to repeat those two fields. It records the central OID and resolved
account IDs before execution. Identity changes reject a mismatched resume.

`employee_mailbox` contains deployment/access configuration:

| Field | Purpose |
| --- | --- |
| `imap_host`, `imap_port` | Mailu TLS IMAP endpoint (`mail.company.example.test`, `2993`) |
| `mail_credentials_ref` | Private mailbox credential reference |
| `webmail_url` | Actual browser interface (`https://mail.company.example.test/webmail/`) |
| `submission_smtp_host`, `submission_smtp_port`, `submission_tls` | Forwarding only: Mailu host, `2587`, `starttls` |
| `rocketchat_base_url`, `rocketchat_room_id`, `chat_credentials_ref` | Chat only: server URL, existing room ID and employee credential reference |

Pure transport checks can omit `directory`; in that mode the mailbox `address`
and, for chat, `rocketchat_expected_user_id` must be supplied explicitly.

## OwnCloud source files and access policy

A fixture may add an `owncloud` object with the native service `base_url` and
`file_references`. Each reference identifies a stable `reference_id`, an exact
`owner_employee_id`, an owner-relative `path`, and an owner credential
reference. OwnCloud source files require MidPoint identity resolution: the
runner verifies the employee's linked OwnCloud shadow and native UID, then reads
the file over WebDAV using credentials for that exact UID. It captures the
native file ID, ETag, size, last-modified value, and SHA-256 and attaches those
exact bytes to the synthetic inbound/forwarded message. The step receipt records
the source identifiers and hashes, not file contents. If a file's native
identity, ETag, or bytes change on a retry using the same run ID, the runner
rejects the changed fixture instead of sending a different attachment under
the same Message-ID.

The company identity profile contains the selected `department_based` access
policy, with membership derived from the native employee department directory.
Policy application uses native OwnCloud OCS groups/shares and explicit employee
IDs, file reference IDs, and native permission bitmasks. Existing unmanaged
shares are preserved and reported. For owned group shares, the durable
coordinator SQLite ledger pins the exact native share ID and source identity;
the adapter verifies owner, group, and file/folder ID before updating
permissions. OwnCloud 10.16 ignores custom names on group shares, so names are
not used as ownership markers. Policy resource identity consists of the exact
owner, path, native file ID, and file/folder type. Content ETags, hashes, and
sizes are not policy identity because normal company edits must not invalidate
sharing. Scenario attachment references keep their strict ETag and hash checks.
Employee membership reconciliation follows exact linked MidPoint identities.
The department groups and 14 group shares were applied and read back on
2026-10-30; the existing public share and file ownership were preserved. The
second reconciliation confirmed all shares unchanged. See
[`deploy/single-host/owncloud-access-policy-review.md`](../../deploy/single-host/owncloud-access-policy-review.md)
for the live mapping and verification evidence.

Optional `attachments` entries contain `path`, `filename`, and MIME `maintype` /
`subtype`. Referenced bytes contribute to the fixture hash. `forward_to_viv: true`
also requires `viv_route_address` (`agent@agent.example.test`) and `smtp4dev_base_url`
(`http://127.0.0.1:19157` on the host). Set `post_chat: true` with `chat_message`
for a handoff. An optional top-level `mention_account_id` selects an assistant by
its immutable Rocket.Chat user ID. Before posting, the runner resolves that ID
through `users.info`, checks the returned ID, and prefixes the message with the
resolved `@username`; the audit receipt records the selected account ID. This
records the resolved target, but does not independently verify Rocket.Chat's
rendering or notification behavior for the mention. The
runner appends the confirmed inbox reference as well. Without this field, the
configured `chat_message` is posted unchanged before that reference, so existing
messages containing a manual mention continue to work. Email-only fixtures do
not resolve or require a mention account.

The credential file is a JSON object keyed by the named references. Each value
has `username` and `password`; keep the file mode `0600`, outside Git. midPoint
credentials, employee mailbox credentials and employee chat credentials are
separate references. Existing generated mailbox credentials are stored on the
Mini under `/data/theagentcompany/mailu/employee-credentials.json`; that provisioning
file has its own nested format and is not directly a runner credential file.

## Outcomes and recovery

Incoming synthetic mail is inserted by authenticated IMAP APPEND, preserving its
sender. Forwarding uses authenticated SMTP. Neither operation succeeds in the
audit until exact Message-ID receipt confirmation. Chat uses the employee's
authenticated account and records the returned room/message/thread IDs.

A run records confirmed steps and safe error types. Use `--run-id` to resume a
specific run. Known failures before submission can retry after repair; uncertain
APPEND/SMTP submissions reconcile before proceeding and never blindly resend.
Use `--new-run` only for a deliberate fresh run with new Message-IDs. Existing
mail is retained. The current runner serializes execution against one audit
database; this does not change the dashboard's experiment concurrency.

These receipts establish transport and sender identity. They do not report Viv
execution, assignment completion or a benchmark score.
