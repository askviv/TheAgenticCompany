# OwnCloud access policy review

**Status:** the user approved department-based access and the concrete mapping
below. It was applied on 2026-10-30 through the native OwnCloud OCS API. The
verification and idempotent retry both passed.

**Inventory date:** 2026-10-30. Native OwnCloud Classic 10.16 was queried over
the configured HTTPS service URL `https://owncloud.company.example.test` using the
existing operator app-password account. Inventory used GET, WebDAV PROPFIND,
and conditional GET. The subsequent policy application used native OCS group
and Share API mutations, with a private pre-change metadata snapshot.

## Current native state

- OwnCloud contains 20 accounts: the 17 MidPoint-linked employees and three
  system/verification accounts. The MidPoint verification record reports all
  17 employee accounts enabled, linked, and home-present.
- The existing `admin` group and its membership were preserved. The policy
  added the `company-staff` group and six department groups. The one existing
  share is a read-only public-link share owned by `theagentcompany` for
  `Documents/job_description.md` (native file ID 851); its ID and metadata
  remain unchanged.
- The `theagentcompany` owner account's `Documents` folder is native folder
  ID 135. Its immediate folders include Admin (188), Administrative Specialist
  (184), Data Analysis (178), Engineering (192), Financials (187), Human
  Resources Team (186), Marketing (183), Q1 Planning and Allocation (179), and
  Research (182).
- `Documents/Employee_Manual.odt` is file ID 180 and
  `Documents/TAC_overview.md` is file ID 193. Both are owned by
  `theagentcompany`.
- `Documents/Human Resources Team/Attendance/employee-department.odt` is the
  native staff directory. Its 17 names match the employee profile and it gives
  six department assignments: Technical (10), Sales/Marketing (2), HR (1),
  Finance (1), Product/UX (2), and Documentation (1). The native file was
  read-only inspected; its SHA-256 is
  `169ca94a6931747ccae7af245c663170fbaf242e8d2ea71b06569431c83f6400`.
  The Employee Manual and company overview have no employee-to-department or
  title mapping. The contract workbook only has an Employee_Name field; it
  supplies no departments or titles. No titles are added to the profile.
- `Documents/Financials` contains budgets, invoices, payments, spending data,
  and `TAC_salary.xlsx`. `Human Resources Team` contains employee contracts,
  personnel, immigration, attendance, and salary material. These folders are
  not proposed for all-staff sharing.

The five task-author assets recorded in [`servers/owncloud/seed-repair/provenance.md`](../../servers/owncloud/seed-repair/provenance.md)
were independently checked through WebDAV and matched their documented SHA-256
values: contracts.xlsx (ID 841), revenue_schedule.xlsx (843),
employee_contracts.xlsx (842), test_image.jpg (844), and category_rules.txt
(845). The files already exist; no recovery or file copy was needed.

The live OCS JSON shapes were also checked. Group data is a list under
`ocs.data.groups`; share collection data is a list under `ocs.data`, and
GET `/shares/{id}` returns a one-item list. The installed Share API stores the
group-share native ID and permissions but ignores its `name` parameter. The
adapter therefore tracks only explicitly created share IDs in the coordinator
SQLite ledger and verifies owner, group, file/folder ID, and item type before
an update. Access-policy retries bind a target by its native owner, path, file
ID, and resource type. ETag, size, and content hash are inventory facts, not
policy identity: normal edits to shared company folders must not invalidate
the grant. Scenario attachment snapshots continue to use strict content and
ETag checks.

## Applied department policy

The applied profile in
[`company-identities.json`](company-identities.json) uses the 17 exact linked
employee UIDs and six groups built only from the native department directory.
It does not use ERP workflow roles as a substitute for department membership.

The policy gives all employees read access to the Employee Manual (ID 180)
and TAC overview (ID 193). It gives the corresponding department group
collaborative access (read, update, create, delete; no reshare, native mask 15)
to its department folders. `Financials` (ID 187) maps to Finance and `Human
Resources Team` (ID 186) maps to HR by matching names. `Marketing` (ID 183)
maps to Sales/Marketing by its department-name component.

The remaining folder assignments were not facts in the source directory, but
the user approved them: Technical to Engineering (192), Data Analysis
(178), and Research (182); HR to Admin (188) and Administrative Specialist
(184); Product/UX to Q1 Planning and Allocation (179). The Q1 folder also has
read-only access for all staff, with collaborative access for Product/UX. The
Documentation group gets read/update access (native mask 3) to the Employee
Manual and TAC overview, while all staff retain read access. Native ownCloud
10.16 `Share20OCS::createShare` strips create/delete bits for single-file shares;
folder mask 15 cannot be used for those two files.

These mappings cover all nine existing department folders and all six listed
departments. The user approved the inferred assignments for Technical,
HR, and Product/UX folders. Native file ownership remains
`theagentcompany`; no content is copied or moved.

## Applied state and verification

The first reconciliation created seven groups, added the 17 employees to the
company-staff group and their native-directory department groups, and created
14 group shares. Readback verified exact membership for all seven groups,
cross-department membership boundaries, and all 14 file/folder share targets
and permission masks. The public share on file ID 851 remained unchanged.

A second run against the same profile and coordinator ledger reported all 14
shares unchanged and no new staff memberships. The run used the private native
pre-change snapshot at `/data/theagentcompany/enterprise/backups/` and the
existing coordinator database at `/data/theagentcompany/enterprise/state/state.sqlite`.
No employee files or ownership were changed. The backing groups and shares are
native OwnCloud permissions; changes to the declarative profile alone do not
apply a different policy until the operator runs the reconciler.
