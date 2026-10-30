# Company email and identity configuration

Mailu owns company mailboxes, SMTP submission and IMAP access. midPoint links
employees to their service-assigned accounts. Keep existing authentication,
ownership and company content when adopting accounts.

Configure the company domain and application URLs for the target deployment.
Keep mailbox, connector and relay credentials in protected files outside Git.
An explicit external-recipient route connects the evaluated agent's inbox;
employee mail must remain in the company mailboxes.

The delivery runner uses authenticated IMAP for incoming fixtures and SMTP for
forwarding. It confirms exact Message-IDs before recording delivery and retains
separate email, forwarding and employee-chat receipts. Retries reconcile
uncertain submissions instead of blindly sending another copy. ownCloud source
references retain native IDs, ETags and hashes without copying file bodies into
audit records. See servers/company/README.md for the runner contract.

Review the Mailu and midPoint deployment templates before installation. Store
native delivery proofs and operational inventories privately, not in source
control. Integration checks do not establish agent performance.
