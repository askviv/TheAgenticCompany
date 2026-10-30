# Company identity and delivery contracts

The company roster provides explicit employee identities and immutable service
links. midPoint manages linked accounts; Mailu provides employee mailboxes;
ownCloud groups and shares apply the configured department access policy.
Preserve existing credentials, ownership, unmanaged shares and documents.

The delivery runner resolves the linked employee, fetches configured ownCloud
sources through WebDAV, delivers mail and optionally posts an employee-authored
chat handoff. Receipts retain message/file identities, versions, hashes and
safe outcome metadata. Bodies, attachments and credentials stay outside audit
rows. Optional assistant mentions resolve an immutable chat account ID.

Controlled tests cover sender identity, source attachments, browser references,
partial retries, duplicate prevention and additive receipt migrations. Native
account/permission checks and their private operational reports are separate
from business experiments. New runs retain old company mail and audit history;
no clean-mailbox deletion reset is implied. GitLab employee identity work is
outside the current scope.
