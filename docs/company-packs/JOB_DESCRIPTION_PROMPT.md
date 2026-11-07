# Prompt: generate two job-description company packs

Copy the prompt below to the authoring agent and give it access to this repository.
The agent should produce the packs independently of the importer implementation.
The optional construction adaptation is a prospective scenario, not a claim about
the engineering employer's industry or software.

---

You are authoring two separate synthetic company problem packs for the existing
The Agentic Company platform. Produce actual ZIP artifacts and real spreadsheets.
Do not implement or modify the importer, schema, validator, runtime or applications.

Read and follow:

- `docs/company-packs/AUTHORING_AGENT.md`
- `docs/company-packs/README.md`
- `schemas/company-packs/manifest-v1.schema.json`
- `servers/company/packs/models.py` and `archive.py`
- `examples/company-packs/manifest.json` and `build_example.py`

Use the target deployment inventory and canonical directory when supplied. With
repository access alone, use the example's documented current-company fixtures
and state that native import has not been verified. Inspect available native
DocType definitions/seed contracts for fields and existing Link identifiers;
identify missing essential metadata rather than inventing valid-looking fields.

## Deliver two independent packs

### Pack 1: finance, payroll and customer-service administration

Source: the following user-supplied job description. Do not invent an employer
name or imply these are real company records.

Finance and accounts responsibilities:

- Maintain day-to-day bookkeeping and financial records.
- Process purchase and sales invoices.
- Record/reconcile supplier payments and customer receipts.
- Complete bank reconciliations; manage payable and receivable balances.
- Follow up overdue customers and reconcile supplier/customer statements.
- Assist with VAT preparation, company expenses, month-end/year-end working
  papers and basic management reports for directors.

Payroll responsibilities:

- Prepare monthly payroll inputs and check hours, salaries, overtime, holidays
  and deductions.
- Maintain payroll records, process starters/leavers and prepare payslip/P45
  working documentation.
- Assist with pensions administration, answer payroll questions and meet deadlines.

Customer service and administration responsibilities:

- Handle customer calls/emails and take/process orders.
- Answer product, price, invoice, payment and delivery enquiries.
- Investigate complaints and maintain customer relationships.
- Liaise with warehouse, sales, suppliers and transport companies.
- Maintain customer records, price lists and account information; support management.

The role expects bookkeeping/payroll experience, strong Excel, accounting software
such as Xero/Sage, VAT/invoicing/reconciliation knowledge, clear communication,
organisation and independent prioritisation. Wholesale, food distribution,
import/export or FMCG experience is advantageous; it is not a mandatory industry.

Create a coherent fictional UK wholesaler/distributor persona, with a clear range
of products, customers, suppliers, operational responsibilities and daily routines.
Use GBP and the existing native company binding. Keep canonical employee IDs and
department names; give those existing people relevant business context. New
business counterparties/products may be pack records where native types support
them. Do not assume the industrial sample item represents a food product.

Cover the source responsibilities through connected, realistic assignments:

- Invoice/receipt matching with a quantity discrepancy, partial receipt or credit note.
- Customer cash allocation and an overdue-balance query with supporting correspondence.
- A bank reconciliation with explainable unmatched entries.
- Payroll-input reconciliation with approved time/leave and at least one exception
  requiring a colleague's clarification.
- A customer order/delivery complaint requiring warehouse or transport evidence.
- A short period-end working-paper/management-summary task using the same facts.

Show both native-application work and spreadsheet work: source records in the
installed ERP/HR applications, Excel working reconciliations, then an appropriate
native update, saved working paper or colleague response. Do not claim ERPNext
is Sage or Xero, or install those products. Payroll/VAT are simulations: use
explicit synthetic configured inputs; do not guess current statutory rates or
claim legally valid submissions. Where native setup is missing, document exactly
which part can be demonstrated as a working-paper task and which needs setup.

### Pack 2: engineering-company admin/accounts assistant

Source: BRACE Engineering Ltd, part-time Admin/Accounts Assistant listing:
https://uk.indeed.com/viewjob?jk=85bc2b52b95ce5ab

The supplied listing describes:

- Accurate data entry, typing and document creation.
- Purchase-ledger processing, matching delivery notes and entering invoices.
- Professional follow-up of outstanding customer payments by phone/email.
- Calls, mail, filing and general office/team administration.
- Word/Excel proficiency; Sage knowledge is preferred, not essential.

Create a fictional engineering supplier/contractor persona inspired by these
duties. Do not copy the real employer's name into synthetic company data or
claim knowledge of its internal systems. Keep the existing native Company and
directory bindings, and clearly identify the fictional business scenario.

Include several related engineering jobs/orders so the work has a business
context. Create assignments for delivery-note/invoice matching, duplicate or
incorrect invoice data, overdue-customer follow-up, an incoming administrative
query and maintaining an organised working record. Include native records,
usable XLSX working files and realistic correspondence/documents. The initial
facts must be sufficient to investigate, with some issues needing a colleague's
answer rather than a fabricated resolution.

Use existing ERPNext Projects/Tasks where useful for job follow-up, plus ERPNext
accounts, ownCloud spreadsheets/documents, email and chat. Do not add a new project
application merely for this pack. The current importer does not seed Plane through
the native Frappe record route; do not label Plane data as a Frappe DocType.

Optional construction adaptation: explain briefly in the handoff how this second
scenario could become a small construction/subcontractor business—site/job cost
codes, material deliveries, subcontractor invoices, approved labour inputs and
payment queries. Do not generate a third pack unless requested. Do not assume
the cited employer is a construction company, or invent construction-specific
accounting/payment rules. Dedicated construction software is not required for
these office/accounting responsibilities.

## Common authoring requirements

1. Make each pack a separate company persona/scenario, with its own stable `id`
   and initial `version`. The persona must explain the business, what it sells,
   operating model, departments, people, workflows and policies; the role under
   evaluation is a separate manifest object.
2. Keep initial records unresolved. Include coherent source facts, dates and
   cross-references, with independent expected outcomes in private evaluation
   material. Avoid unrelated puzzles and unsupported complexity.
3. Use existing identities and native permissions. Packs do not create accounts,
   grant roles, install applications or restore databases. Do not invent employees
   or departments that the current importer cannot provision.
4. Select only the applications needed. Current workplace Run delivery uses
   `rocketchat`; staff files need `owncloud`; native ERP/HR records need their
   installed shared Frappe backend. Use `email` for mailbox work and `payroll` for
   the HR/payroll scenario. Leave Lending/CRM/Helpdesk/Plane unselected unless
   the scenario actually uses them. Application selection stops unused standalone
   services when imported; shared backend dependencies remain managed separately.
5. Write natural colleague `request_message` text. Refer to records/files through
   manifest IDs; the importer supplies native links. No fake URLs, model-operation
   instructions, private answers or full-manifest dumps in employee requests.
6. Create real XLSX files with readable sheets, units, source references and working
   areas. Include staff-readable company/process context. Keep original evidence
   separate from the working copy the employee should produce.
7. Map every job responsibility to an assignment, supporting evidence and expected
   output in a separate handoff coverage table. Mark unsupported live channels
   honestly: a written call note is evidence, not a claim that voice calling exists.
8. Native records must use supported DocTypes and actual fields/child tables.
   Use `$ref`, `$binding` and `$date_offset` exactly as specified. Don't put native
   identity/audit/provenance fields inside the record payload. Don't pre-submit
   a document unless that is genuinely the intended starting state.
9. Every ZIP member must be declared: root `manifest.json`, staff `files/**`,
   private `private/**`. Keep generators, reports and authoring notes outside
   the import ZIP. Never include credentials, real customer data or real payroll data.
10. Give a reviewable validation report. You may run the existing offline ZIP
    validator to check the generated artifacts, unless the operator forbids it:

    ```sh
    uv run --no-project --with 'pydantic>=2,<3' python -m servers.company.packs validate_zip /absolute/path/to/pack.zip
    ```

    Do not alter the validator to accept a pack. Do not run platform test suites,
    import either pack, seed the company, start experiments, send communications
    or change any live configuration. Report native import as unverified.

## Final deliverables

- Two clearly named import ZIPs.
- Their readable manifests and reproducible generator(s), outside the ZIPs.
- A handoff for each: persona, application selection, prerequisites, record/file/
  assignment inventory, responsibility coverage table, unresolved setup and the
  actual offline validation result (or an explicit statement it was not run).
- Private reference facts/evaluation criteria inside each ZIP, never in staff files.

These represent alternative personas on the existing shared platform. Do not
merge the two businesses into one pack or import them to demonstrate your output.
Changing a persona does not reset existing company data. Do not describe schema
validation as successful native import, automatic scoring or end-to-end execution.

Deliver the files to the operator for their independent importer evaluation.
