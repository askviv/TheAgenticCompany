# Instructions for a company-pack authoring agent

Give this document to the agent together with repository access, the requested
business scenario/job description, and the target deployment information below.
The deliverable is an importable ZIP, not a proposed pack or a description of one.

## Your task

Create a realistic, internally consistent company problem pack for the supplied
business and role. Follow the existing v1 contract exactly. Do not change the
platform, schema, importer or application permissions to make your pack fit.
Do not import the pack, start experiments, send messages, or change services.
Respect the operator's instructions about running validation or other checks.

## Read these sources first

1. `docs/company-packs/README.md`: format, routing, references and limitations.
2. `schemas/company-packs/manifest-v1.schema.json`: exact fields, types and enums.
3. `servers/company/packs/models.py` and `archive.py`: cross-reference, application
   dependency and archive rules that JSON Schema alone cannot express.
4. `examples/company-packs/manifest.json` and `build_example.py`: a complete
   current-company example and real workbook generation using openpyxl.
5. The target's canonical employee/department directory and installed native
   document metadata. Repository defaults are examples; deployment facts win.

Without repository access, obtain copies of those files plus the whole
`servers/company/packs/` Python package and target inventory. The schema alone
does not include the native applications' business schemas.

## Required inputs

Establish these before choosing native record fields:

| Input | What you need |
| --- | --- |
| Company | Existing native Company identifier, currency, country, business type and operating context |
| People | Canonical employee IDs, display names, exact department names, responsibilities and available account/role assignments |
| Applications | Installed applications/versions and the applications this persona should use |
| Native records | Installed DocType metadata, child-table metadata, required fields, relevant workflow/submission rules and permitted record types from the pack schema |
| Existing references | Exact identifiers of reused items, warehouses, accounts, cost centres, currencies, fiscal years and other native Link values |
| Work to simulate | Job description, responsibilities, scale, period, expected deliverables and intended difficulties |

Use read-only metadata/configuration supplied by the operator or existing
documented APIs when authorized. No credentials belong in your output. If an
essential target fact is unavailable, identify that specific missing fact; do
not invent a native field, account, warehouse, identity or successful check.

For the supplied industrial example, the established baseline is native Company
`The Agent Company`, GBP, United Kingdom, item `COMP-001`, warehouse `Stores - TAC`
and the 17 employee IDs/six departments in the example manifest. Those facts are
specific to that deployment, not universal defaults.

## Plan the business before writing the manifest

Describe what the company sells, its customers and suppliers, how orders and
money move, daily work, responsibilities, operating policies and trading context.
Keep this company persona separate from the job title being evaluated.

Choose realistic workflows from the job description. For example, a finance
assignment can start with native purchase orders, use Excel to match receipt and
invoice evidence, and finish with a reconciled workbook plus a colleague message.
A payroll assignment can reconcile approved hours and leave with working inputs.
A lending assignment should use the installed lending DocTypes and actual product
configuration. Use the selected company's business, rather than copying an
unrelated industry from an example.

Define the intended initial state and the evidence a capable employee needs.
Leave the work unresolved. Private reference facts should specify independently
calculated outcomes, acceptable evidence and genuinely ambiguous issues. A pack
must not accidentally contain the completed answer in a staff workbook or request.

## Build the pack

Use this layout; filenames below illustrate the structure, not mandatory names:

```text
manifest.json
files/
  company/operating-handbook.md
  finance/invoice-reconciliation.xlsx
  hr/payroll-inputs.xlsx
private/
  evaluation/reference-facts.json
```

The ZIP root must contain `manifest.json` directly. Include exactly the files
declared in `manifest.files`. Do not add a surrounding directory, explicit empty
directory entries, generator scripts, schema files, README files, SQL, credentials
or undeclared files to the import ZIP. Deliver those separately if useful.

Populate all required top-level fields:

- `schema_version`, `id`, `version`, `title`.
- `company_persona`, including its required `applications` and business context.
- `departments`, `employees`, `role_under_evaluation`.
- `experiments` (required). `records` and `files` are optional and default to empty arrays.

Use the schema for every nested field; unknown keys are rejected. Use meaningful
lowercase hyphenated IDs. IDs must be unique within each entity type. Reuse
canonical employee IDs and exact existing department names. The importer binds
these people to existing accounts; it does not create a different workforce,
provision accounts, grant roles or rename the native Company.

For each native record:

1. Choose an allowlisted installed `doctype` and verify its actual native fields.
2. Use native child-table arrays and native field names; do not invent a generic
   SQL/table representation.
3. Link pack records with `{"$ref":"record-id"}`. Bind existing company/people
   with `{"$binding":"company"}`, `{"$binding":"employee:employee-id"}` or
   `{"$binding":"department:department-id"}`.
4. Use `{"$date_offset":-7}` for dates relative to the company import date, or
   intentional native literals when the scenario requires fixed dates.
5. Set `state` to `draft` or `submitted` according to the intended starting state.
   Submission runs real native business hooks; do not submit merely to bypass an
   incomplete scenario. Do not set `name`, `doctype`, `docstatus`, ownership,
   timestamps or importer provenance inside `fields`.
6. Reuse established Link identifiers as plain strings only when they actually
   exist. Never guess the eventual native name of a new pack record.

For each staff file, declare its normalized `files/...` ZIP path, relative
`drive_path`, `visibility:"staff"` and appropriate `department_ids`. No department
IDs means company-staff access. The importer adds its own pack folder prefix to
`drive_path`; a short path such as `Finance/Invoice reconciliation.xlsx` is enough.
Private files must have a `private/...` ZIP path and `visibility:"evaluation"`.

Create real XLSX files, not CSV text renamed to `.xlsx`. Include readable sheet
names, units/currencies, source identifiers and suitable working areas. Decide
which values are source evidence and which calculations the employee must perform.
Check the arithmetic and cross-file consistency when authorized. Merely passing
structural validation does not prove workbook formulas or business facts correct.

For each experiment:

- Set a real canonical `sender_employee_id` and a declared `department_id`.
- Write `brief` for the catalogue and `request_message` as the natural colleague
  request to send. Do not put model instructions, evaluation answers, the full
  manifest or private facts in the request. The importer appends links to its
  referenced native records and staff files.
- Reference declared file/record IDs through `files` and `records`; don't embed
  fake links or deployment-specific hostnames. Native names/links are resolved
  during import. Pack markers are resolved inside record `fields`, not inside
  workbook cell text or prose.
- Put expected outcomes and evidence criteria in `evaluation`. An optional
  `evaluation.reference_file_id` must name a private evaluation file.

## Choose applications deliberately

Use only the schema's application keys. This selection affects the running
company when an operator imports the pack: it hides unselected links and stops
unused standalone services through the deployment's trusted Compose mapping.
Shared Frappe infrastructure remains available for selected Frappe applications;
midPoint remains directory infrastructure in the provided mapping.

Staff files require `owncloud`. Native records require at least one selected
Frappe application (`erpnext`, `payroll`, `lending`, `crm` or `helpdesk`). The current
workplace Run delivery path uses Rocket.Chat, so select `rocketchat` for assignments
intended to run through that path. Selecting `email` alone does not turn those
experiments into email-triggered experiments. Select `email` when company mailbox
work is part of the scenario.

Do not add Compose paths, commands or service names to a pack. Application setup,
native permissions and connector provisioning are deployment responsibilities.

## Package, validate and deliver

Keep a stable pack `id`. Change `version` whenever changing the delivered archive:
the importer treats an existing ID/version as immutable and rejects a different
archive digest. Retrying the same archive resumes its saved import; it does not
reset the company or overwrite later user work. A new version creates another
baseline and may encounter native business-identity conflicts; it is not an
automatic migration or update of earlier records.

When validation is authorized, run from the repository root with Pydantic v2:

```sh
python -m servers.company.packs validate_zip /absolute/path/to/company-pack.zip
```

Or use an isolated dependency without installing the platform:

```sh
uv run --no-project --with 'pydantic>=2,<3' python -m servers.company.packs validate_zip /absolute/path/to/company-pack.zip
```

Correct reported structural/reference errors using the existing contract. Do not
modify the validator to accept the output. The validator performs no imports,
network calls, native writes or experiment runs. If checks are prohibited, skip
them and explicitly report the ZIP as not validated.

Deliver separately:

1. The final import ZIP.
2. The readable authoring manifest and reproducible generator, if you used one.
3. A short handoff listing the persona, selected applications, target prerequisites,
   staff/departments, native records, spreadsheets and assignments included.
4. The exact validation command/result, or an explicit statement it was not run.
   Distinguish structural validation from native import and business verification.
5. Any unresolved target-specific configuration or factual uncertainty.

Do not claim live readiness from schema validation. V1 does not implement timed
event schedules, automatic evaluation scoring, arbitrary application installation,
directory/account provisioning or database restoration. Finish by handing the ZIP
to the operator; uploading, importing and choosing Run are separate actions.
