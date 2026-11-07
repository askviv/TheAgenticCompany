# Company pack v1 authoring

For an agent-ready handoff, start with [Instructions for a company-pack authoring
agent](AUTHORING_AGENT.md). Give the agent that document, repository access (or the
listed source files), your job description and the target deployment inventory.

A pack is a ZIP containing exactly one structured contract, `manifest.json`, and exactly the files declared by that manifest. Staff evidence goes under `files/`; private evaluation evidence goes under `private/`. Do not include another manifest, executable import scripts, SQL, environment configuration or credentials. Documents are inert bytes; the importer never executes their contents. XLSX workbooks are normal evidence files. No file-extension allowlist substitutes for native document validation.

The authoritative Python contract is `servers/company/packs/models.py`. Its generated JSON Schema is `schemas/company-packs/manifest-v1.schema.json`. Every model forbids unknown keys. Required keys and native DocType names are enumerated in that schema. `manifest.json` in the example directory is a complete authoring example.

## Structure and meaning

- `schema_version`: exactly `"1.0"`; `id`: lowercase hyphenated slug; `version`: nonempty author version; `title`: display title.
- `company_persona`: reusable company context, separate from the role being evaluated. Select required `applications` using the application keys below. Describe company name, business type, industry, description, what it sells, how it operates, day-to-day work, workflows, policies, trading context, ISO currency and country. Do not rebrand the existing company by writing a job title here.
- `departments`: `{id,name,description}` directory entries.
- `employees`: `{id,display_name,department_id,job_title,responsibilities,context,reports_to?}`. Use canonical existing employee IDs from `deploy/single-host/company-identities.json`. A persona does not grant permissions or create identities. Department IDs are local manifest IDs mapped to existing native departments at import.
- `role_under_evaluation`: `{title,department_id,responsibilities}`.
- `records`: `{id,doctype,fields,state}`. `state` defaults to `draft`; `submitted` requests native submission. Native DocType names contain spaces: e.g. `Purchase Order`, `Purchase Receipt`, `Sales Invoice`, `Salary Structure Assignment`, `HD Ticket`, `CRM Lead`. Use the schema enum for the complete supported list. `fields` is a native Frappe document field mapping, including native child-table arrays. Do not put pack IDs in native Link fields as plain text.
- `files`: `{id,path,drive_path,visibility,department_ids?}`. `visibility` is `staff` or `evaluation`. `path` is a normalized ZIP member path, `drive_path` is the destination relative path. Paths must be unique. Evaluation files remain private and must never be published into staff Drive or included in an agent's initial request. Department access uses existing native directory authorization.
- `experiments`: `{id,title,department_id,sender_employee_id,brief,request_message,files,records,evaluation}`. `files` and `records` list manifest IDs. `evaluation` is private JSON evidence/criteria, not executable evaluator code. Optional `evaluation.reference_file_id` names a declared private evidence file. The importer persists experiment definitions; selecting Run uses the existing workplace request path. V1 contains no timed events or scheduler and does not automatically run imported experiments.

IDs are unique within each entity kind. Employee manager links and record dependency links must be acyclic. All directory, record and file links must resolve. Keep files referenced by experiment requests staff-visible; private reference facts belong only in evaluation.

## Native references

Use an exact single-key object anywhere inside native `fields`, including child tables:

```json
{"$ref": "supplier"}
{"$binding": "company"}
{"$binding": "employee:alex-turner"}
{"$binding": "department:finance"}
{"$date_offset": -7}
```

`$ref` resolves to the newly created native document name for a record ID. `$binding` resolves an existing company, employee or department. `$date_offset` is an integer number of days relative to the company date at import; it resolves to a native date string. Marker objects cannot contain additional keys. References determine creation order; input order does not need to be sorted. Plain strings refer to established native values, such as `COMP-001` and `Stores - TAC`.

Do not set native identity, ownership, audit metadata, `docstatus`, or importer-owned `custom_source_*` fields. Native insert/submit APIs own defaults, mandatory fields, Link validation, workflow rules, accounting rules and submission. The pack contract intentionally does not reproduce Frappe schemas: inspect installed DocType metadata and existing native seed examples under `deploy/enterprise-simulation/erpnext/admin/` when authoring document fields. A syntactically valid pack can fail native validation if its business fields or deployment fixtures are incomplete. Submission can have stock/accounting consequences; start documents in their intended initial state rather than pre-solving the work.

## Offline validator

From the repository root, with Pydantic v2 installed:

```sh
python -m servers.company.packs validate_zip path/to/pack.zip
python -m servers.company.packs schema schemas/company-packs/manifest-v1.schema.json
```

`validate_zip` reads bytes and reports pack identity, digest and counts; it performs no writes, network calls, imports or experiment runs. It rejects malformed/duplicate JSON keys, schema errors, unresolved references, cycles, undeclared or missing members, traversal paths, duplicate member names, symlinks, encrypted entries and excessive archive sizes. It never uses `extractall`. Default limits: 50 MiB compressed, 30 MiB per member, 100 MiB expanded, 500 members. Python callers may pass `ArchiveLimits` to `load_pack` to adjust these limits.

Offline validation cannot establish live native permissions, existing identities, installed DocTypes, mandatory fields, account/stock setup, native transaction success, workbook factual accuracy, or evaluation outcomes. Review does not imply import or run. Errors return nonzero status; underlying exceptions retain their cause for operational handling.

## Example and generation

`examples/company-packs/industrial-office-operations-v1.zip` contains a full manufacturer/distributor persona, the existing 17-employee directory, six departments, a finance/payroll/customer-service role, five draft native records, three Excel workbooks, a staff operating handbook, private reference facts and three connected assignments. Workbooks intentionally contain unresolved receipt, overtime and delivery issues. The example assumes existing `The Agent Company`, GBP, United Kingdom, `COMP-001`, `Stores - TAC` and canonical identities; it does not create or rename the company.

Generate the artifact without importing, validating or running it:

```sh
uv run --no-project --with openpyxl python examples/company-packs/build_example.py
```

The generator writes the example ZIP and authoring manifest only. It uses synthetic business counterparties and no private employee details. Native business validation and actual workplace outcomes have deliberately not been executed for this example.

## Application selection and native lending/support records

`company_persona.applications` is a required list selected from `erpnext`, `email`, `rocketchat`, `owncloud`, `plane`, `midpoint`, `payroll`, `lending`, `crm`, and `helpdesk`. This is the desired application selection for the company: unselected application links and employee tools are hidden, and unused standalone services are stopped by the deployment application configuration. The example explicitly selects all ten keys. Selecting an application does not create dummy users or arbitrary cross-application accounts: existing canonical identities and native authorization remain authoritative.

Payroll, lending, CRM and Helpdesk share the existing Frappe site/backend. Their native business DocTypes are selected by each record's exact `doctype`; they do not need a custom pack mapping or a separate account schema. Supported lending types include `Loan Application`, `Loan`, `Loan Product`, `Loan Disbursement`, and `Loan Repayment`; support uses `HD Ticket` and CRM uses `CRM Lead`. Native installed metadata, mandatory fields, Link fields, accounting configuration, permissions and workflows still determine whether a document can be inserted/submitted. Application selection does not establish comprehensive lending products, chart-of-accounts setup or external integrations.

Disabling the ERPNext application link does not stop the shared Frappe backend while payroll, lending, CRM or Helpdesk remains enabled. Stopping an unused standalone service must preserve its volumes and existing company work. File-backed assignments require their selected native document application to be available when used; selection does not silently substitute an imitation application.

### Deployment service mapping

The operator owns the application-to-service mapping; company ZIPs cannot provide Compose paths, service names or commands. `deploy/virtual-time-world/application-services.example.json` shows the guest layout. Configure the deployed mapping path for the import service and adjust its paths to the actual rendered Compose files. The file is a JSON list of groups. Each group has `id`, `compose_file`, `services`, `applications`, and optional `required` (default `false`); unknown fields and application keys fail validation. Relative Compose paths resolve beside the mapping file. An empty `services` list targets the whole Compose project, so use explicit service lists for projects shared by several applications.

A group is enabled when it is required or any of its application keys are selected. Import starts all enabled groups first using `docker compose -f FILE up --detach --pull never [SERVICES]`, then stops disabled groups using `docker compose -f FILE stop [SERVICES]`. Failures stop the import with a recorded failure; retry reuses the saved import. Images must already be installed. These operations never run `down` or delete volumes, accounts or existing company work.

Keep ERPNext, payroll, lending, CRM and Helpdesk together in the shared Frappe group, so selecting any of them keeps that backend running. The example keeps midPoint required because it supplies canonical directory infrastructure even when its application link is hidden. Keep other shared infrastructure required where the deployment needs it. Selected links and visible employee tools follow the manifest selection; required infrastructure does not make an unselected application link visible. The example splits Rocket.Chat and ownCloud service groups within their shared Compose project, preventing one application's selection from stopping the other's services.

The offline contract checks concrete import dependencies before any services are reconfigured: staff-visible files require `owncloud` in the selected applications, and nonempty native `records` require at least one of `erpnext`, `payroll`, `lending`, `crm` or `helpdesk` to keep the shared Frappe backend available. Import alone does not require a chat application; experiment delivery uses its existing Run pathway.
