# TheAgentCompany seeded-input audit

## Scope and source-of-truth limits

Static review of all task `dependencies.yml`, `task.md`, `populate_data.py`, task-local files and Dockerfile `COPY` directives. There are 71 task images declaring an `owncloud` dependency. No task `populate_data.py` references OwnCloud or writes its files. `servers/owncloud/owncloud_data` is not present in this checkout and is ignored by `.gitignore`; the OwnCloud Dockerfile expects that omitted directory when building a local service image. Therefore task text and image-local evaluator fixtures are not proof that the external OwnCloud backup currently contains a particular file. Names below are authoritative requested inputs from the benchmark tasks; actual presence must be compared with the live OwnCloud inventory and file contents.

## Exact task-requested OwnCloud inputs

| Task source | Requested OwnCloud path or file(s) |
| --- | --- |
| `workspaces/tasks/admin-ask-for-upgrade-reimbursement/task.md` | `Documents/Financials/expenses` (task describes an expense record; file vs folder is unspecified) |
| `workspaces/tasks/admin-check-employees-budget-and-reply/task.md`; `.../admin-check-employees-budget-and-reply-2/task.md`; `.../admin-check-employees-budget-and-reply-and-record/task.md`; `.../admin-collect-requests-and-compute-total-price/task.md` | `Documents/Administrative Specialist/products.pdf` |
| `workspaces/tasks/admin-get-best-vendor-quote/task.md` | Quote files placed by Emma Lewis; task expects their path from Rocket.Chat, so the filenames/folder are intentionally dynamic in task text |
| `workspaces/tasks/admin-make-spreadsheet/task.md`; `.../admin-read-survey-and-summarise/task.md` | `drinks_survey.pdf` (folder not stated) |
| `workspaces/tasks/admin-mass-forms-filling/task.md` | `/Documents/Admin/i-9.pdf`, `/Documents/Admin/TAC_personell_data.csv`; same instructions separately refer to `/Documents/Admin/TAC_personell_info.csv` for employee rows (filename discrepancy in benchmark task text) |
| `workspaces/tasks/admin-remove-pages-pdf/task.md`; `.../sde-copy-table-from-pdf-to-xlsx/task.md` | `Documents/Research/OpenHands.pdf` |
| `workspaces/tasks/admin-translate-sales-chat/task.md` | `Documents/Admin/office_sales_chat_history.jpg` |
| `workspaces/tasks/admin-watch-video/task.md` | `gym.mp4` (folder not stated) |
| `workspaces/tasks/bm-classify-nationality/task.md` | `Documents/Data Analysis/Customer.xlsx` |
| `workspaces/tasks/ds-answer-numerical-data-question/task.md` | `Documents/Data Analysis/beverage_stats.xlsx` |
| `workspaces/tasks/ds-answer-spreadsheet-questions/task.md` | `Documents/Data Analysis/agriculture.xlsx` |
| `workspaces/tasks/ds-calculate-spreadsheet-stats/task.md`; `.../ds-organise-report-sus-data/task.md` | All spreadsheet files under `Documents/Data Analysis` (the latter uses `/Documents` as its listing URL); the task gives only example bad names `rxuts 31t3b.xlsx` and `3030.xlsx`, not a complete static inventory |
| `workspaces/tasks/ds-coffee-shop-database-management/task.md` | All CSVs plus `analysis.txt` under `Documents/Data Analysis/Coffee Shop`; source CSV names are not specified |
| `workspaces/tasks/ds-find-meeting-spreadsheet/task.md` | One unspecified spreadsheet under `Documents/Data Analysis`, described as the AI / Canada field-crop seed-area meeting file |
| `workspaces/tasks/ds-fix-table-values-and-missing-answers/task.md` | `Documents/Data-Analysis/Other Corp Area Totals.xlsx` (hyphenated directory spelling in the task) |
| `workspaces/tasks/ds-format-excel-sheets/task.md` | `Documents/Data Analysis/respondent_counts.xlsx` |
| `workspaces/tasks/ds-merge-multiple-sheets/task.md` | `Documents/Data Analysis/total-sugar-sources.xlsx` and `Documents/Data Analysis/Top Sources of Total Sugars.xlsx` |
| `workspaces/tasks/ds-predictive-modeling/task.md` | `Documents/Financials/stock_train_raw.csv` and `Documents/Financials/stock_test.csv` |
| `workspaces/tasks/ds-stock-analysis-slides/task.md` | `Documents/Financials/stock_data_raw.csv` |
| `workspaces/tasks/ds-visualize-data-in-pie-and-bar-chart/task.md` | `Documents/Data Analysis/Number of Agri Ops.xlsx` |
| `workspaces/tasks/finance-apply-tax-credit/task.md` | `/Documents/Financials/f6765.pdf`, `/Documents/Financials/TAC_financials.csv`, `/Documents/Financials/f6765_instructions.pdf`, `/Documents/Human Resources Team/Personell_File.odt` |
| `workspaces/tasks/finance-budget-variance/task.md` | `/Documents/Financials/budget.xlsx` and `/Documents/Financials/actual_spending.xlsx` |
| `workspaces/tasks/finance-check-attendance-payroll/task.md`; `.../hr-check-attendance-multiple-days/task.md`; `.../hr-check-attendance-multiple-days-department/task.md`; `.../hr-check-attendance-multiple-days-department-with-chat/task.md` | `Documents/Human Resources Team/Attendance/april-attendance-data.csv` |
| `workspaces/tasks/finance-check-attendance-payroll/task.md` | Also `Documents/Human Resources Team/Attendance/salary-rates.pdf` |
| `workspaces/tasks/hr-check-attendance-multiple-days-department/task.md` | Also `Documents/Human Resources Team/Attendance/employee-department.odt` |
| `workspaces/tasks/hr-check-attendance-one-day/task.md` | `Documents/Human Resources Team/Attendance/attendance-2024-03-01.csv` |
| `workspaces/tasks/finance-create-10k-income-report/task.md` | All reports under `/Documents/Financials/Annual Reports/10Ks` |
| `workspaces/tasks/finance-expense-validation/task.md` | `Documents/Financials/Expenses/expenses.xlsx` and `Documents/Financials/Expenses/category_rules.txt` (capitalized `Expenses`) |
| `workspaces/tasks/finance-find-signatories/task.md` | Amazon annual-update reports and an Alphabet report under the annual-company-updates folder path supplied by the Finance director over Rocket.Chat; exact path and file names are intentionally dynamic |
| `workspaces/tasks/finance-invoice-matching/task.md` | `/Documents/Financials/payments.xlsx` and all invoice PDFs under `/Documents/Financials/invoices_pdfs` |
| `workspaces/tasks/finance-nonqualified-bill-ask-for-reimburse/task.md` | `/Documents/Financials/bill.pdf`; `/Documents/Administrative Specialist/Reimbursement Policy.pdf` |
| `workspaces/tasks/finance-qualified-bill-ask-for-reimburse/task.md` | `/Documents/Financials/receipt.jpg` |
| `workspaces/tasks/finance-r-d-activities/task.md` | `/Documents/Human Resources Team/Personnell_File.odt`; `/Documents/Financials/Qualified_R&D_Activities.md`; `/Documents/Financials/TAC_salary.xlsx` |
| `workspaces/tasks/finance-revenue-reconciliation/task.md` | `Documents/Human Resources Team/Contracts/contracts.xlsx`, `revenue_schedule.xlsx`, and `employee_contracts.xlsx` |
| `workspaces/tasks/hr-analyze-outing-bills/task.md` | All receipts in `Documents/Financials/outings` (image/PDF formats) |
| `workspaces/tasks/hr-create-career-ladder/task.md`; `.../hr-mass-survey/task.md`; `.../hr-internal-tooling-slides/task.md`; `.../hr-pick-interviewer-1/task.md`; `.../pm-present-engineer-group-members/task.md` | `/Documents/Human Resources Team/Personnell_File.odt` (other task instructions use the spelling `Personell_File.odt`) |
| `workspaces/tasks/hr-create-employee-manual/task.md` | `Documents/TAC_overview.md` |
| `workspaces/tasks/hr-green-card-consultation/task.md` | One unnamed screenshot under `Documents/Human Resources Team/Immigration` |
| `workspaces/tasks/hr-massive-resume-screening/task.md`; `.../hr-resume-categorization/task.md` | `Documents/Human Resources Team/resumes/resumes.zip` |
| `workspaces/tasks/hr-organize-talent-info/task.md`; `.../hr-pick-interviewer-1/task.md` | Candidate resume(s) in `Documents/Human Resources Team/resumes` (named candidates in the task text, but no exact resume filenames) |
| `workspaces/tasks/hr-populate-salary-increase-memo/task.md` | `Documents/Human Resources Team/salary_increase.txt` |
| `workspaces/tasks/hr-salary-analysis/task.md` | `Documents/Human Resources Team/salary.txt` |
| `workspaces/tasks/ml-generate-gradcam/task.md` | `Documents/Research/test_image.jpg` |
| `workspaces/tasks/ml-grade-exam/task.md` | All versions of `ML BACKGROUND EXAM - AGENT COMPANY` PDFs across OwnCloud; no exact filenames or directories |
| `workspaces/tasks/pm-create-teammate-channel-from-spreadsheet/task.md` | `Documents/Marketing/Task_assignments.xlsx` (line 3 calls it `Task_assignment.xlsx`, singular; task text discrepancy) |
| `workspaces/tasks/pm-monthly-attendance-slides/task.md` | Attendance sheets under `Documents/Human Resources Team/Attendance` |
| `workspaces/tasks/research-answer-questions-on-paper/task.md` | `Documents/Research/Noise Simulation/noise_simulation_paper.pdf` and `noise_simulation_analysis_sheet.txt` |
| `workspaces/tasks/research-reproduce-figures/task.md` | `Documents/Research/survey.pdf` |
| `workspaces/tasks/sde-add-all-repos-to-docs/task.md` | Scattered repository taglines somewhere in OwnCloud (no specific source filenames) |
| `workspaces/tasks/sde-create-sqlite-database/task.md` | July–September 2024 company financial report(s) under unspecified OwnCloud path |
| `workspaces/tasks/sde-find-answer-in-codebase-3/task.md` | Current on-call arrangement file under unspecified OwnCloud path |
| `workspaces/tasks/sde-run-rising-wave-locally/task.md` | `Quick_start_RisingWave.pdf` (folder not stated) |

## Task-local authoritative fixtures and exact limitations

Dockerfile `COPY` directives show these checked-in files are evaluator/task fixtures, not automatically restored to OwnCloud:

- `workspaces/tasks/ds-format-excel-sheets/respondent_counts.xlsx` is copied to `/utils`; same basename as the OwnCloud task input.
- `workspaces/tasks/finance-budget-variance/{budget.xlsx,actual_spending.xlsx}` are copied to `/utils`; same basenames as OwnCloud task inputs.
- `workspaces/tasks/finance-check-attendance-payroll/april-attendance-data.csv` is copied to `/utils`; `rates_info.txt` is also local but task asks for `salary-rates.pdf`, so it is not proof of that PDF’s existence/content.
- `workspaces/tasks/hr-check-attendance-{multiple-days,multiple-days-department,multiple-days-department-with-chat}/april-attendance-data.csv` and `workspaces/tasks/hr-check-attendance-one-day/attendance-2024-03-01.csv` are evaluator fixtures, not OwnCloud seed proof.
- `workspaces/tasks/finance-invoice-matching/{payments.xlsx,invoices.xlsx}` are copied to `/utils`, while the OwnCloud task requests `payments.xlsx` plus invoice PDFs; the local `invoices.xlsx` is not a substitute for those PDFs.
- `workspaces/tasks/ds-format-excel-sheets/respondent_counts.xlsx`, `workspaces/tasks/ds-fix-table-values-and-missing-answers/ref_data.xlsx`, and other reference outputs are local evaluator inputs only unless content and intended cloud destination are independently confirmed.
- `workspaces/tasks/ml-generate-gradcam/eval_data/reference_gradcam.jpg`, `reference_gradients.pt`, and `reference_activations.pt` are grader references; no checked-in `test_image.jpg` was found.
- `workspaces/tasks/finance-revenue-reconciliation` has no local `contracts.xlsx`, `revenue_schedule.xlsx`, or `employee_contracts.xlsx` fixture.

The only local checked-in OwnCloud payload path expected by the service builder is absent from this checkout; there is no complete seed bundle here from which missing OwnCloud state can be reconstructed. Do not synthesize replacements. For any missing exact file, use an authoritative benchmark backup/source copy if available; preserve its original name, path, and bytes.

## Comparison against live inventory

Compared exact task inputs to the 682-path original OwnCloud volume inventory at `/tmp/tac-ops/owncloud-original-files.txt` (stripped the volume-specific prefix, preserving OwnCloud-relative paths). Four exact requested files are absent:

| Missing authoritative input | Task source | Upstream provenance |
| --- | --- | --- |
| `Documents/Human Resources Team/Contracts/contracts.xlsx` | `workspaces/tasks/finance-revenue-reconciliation/task.md` | Finance source-data recovery associated with [upstream PR 844](https://github.com/TheAgentCompany/TheAgentCompany/pull/844) |
| `Documents/Human Resources Team/Contracts/revenue_schedule.xlsx` | same | PR 844 |
| `Documents/Human Resources Team/Contracts/employee_contracts.xlsx` | same (required to map flagged contracts to employees) | PR 844 |
| `Documents/Research/test_image.jpg` | `workspaces/tasks/ml-generate-gradcam/task.md` | Image source-data recovery associated with [upstream PR 867](https://github.com/TheAgentCompany/TheAgentCompany/pull/867) |

No additional distinct authoritative input dataset was confirmed absent beyond these four. Several prompt strings are exact-path/extension mismatches despite same-purpose files being present; those are listed separately below and must not be silently treated as file equivalence. The required surrounding corpora are present by path: 10Ks (8 files), coffee-shop CSVs and `analysis.txt`, admin quote PDFs, ownCloud research PDFs, HR resumes archive, HR attendance files, outing receipts, immigration screenshot, and financial invoice PDFs. This validates file-path presence only, not byte-level equivalence or task outcomes. The local source repository does not contain the contract/image source files; use the authoritative benchmark backup source confirmed by the operator, never synthesized files.

### Instruction-to-inventory naming/path mismatches

These are not additional confirmed missing originals; the inventory contains near or semantic matches. They can still hinder exact-path navigation and should be resolved from the established benchmark contract if needed, without duplicating or renaming seeded files blindly.

| Task reference | Live inventory | Assessment |
| --- | --- | --- |
| `workspaces/tasks/admin-mass-forms-filling/task.md` also names `/Documents/Admin/TAC_personell_info.csv` | Only `Documents/Admin/TAC_personell_data.csv` is present; the task's first and final lines also refer to `TAC_personell_data.csv` | Likely an instruction typo; no distinct info CSV is available |
| `workspaces/tasks/ds-merge-multiple-sheets/task.md` names `Documents/Data Analysis/total-sugar-sources.xlsx` | `Documents/Data Analysis/top-sugar-sources.xlsx` and `Top Sources of Total Sugars.xlsx` are present; exact `total-sugar-sources.xlsx` is absent | Possible filename typo versus missing distinct source; don't assume byte/content equivalence without source evidence |
| `workspaces/tasks/finance-expense-validation/task.md` names `category_rules.txt` | `Documents/Financials/Expenses/category_rules.xlsx` is present, plus a root `Documents/Financials/category_rules.xlsx`; no `.txt` file is present | Format/extension discrepancy, not an absent policy dataset based on names alone |
| `workspaces/tasks/finance-invoice-matching/task.md` requests `/Documents/Financials/invoices_pdfs` | 500 PDFs are under `Documents/Financials/invoices_pdf` (singular) | Directory spelling mismatch; invoice corpus is present |
| `workspaces/tasks/ds-fix-table-values-and-missing-answers/task.md` says `Documents/Data-Analysis/Other Corp Area Totals.xlsx` | File exists under `Documents/Data Analysis/Other Corp Area Totals.xlsx` | Hyphen/space mismatch in folder path |
| Several task prompts say `Personell_File.odt` | File is `Documents/Human Resources Team/Personnell_File.odt` (double `n`) | Prompt spelling mismatch; expected personnel file is present |
| `workspaces/tasks/pm-create-teammate-channel-from-spreadsheet/task.md` first says `Documents/Marketing/Task_assignments.xlsx`, later says `Task_assignment.xlsx` without a path | Both `Documents/Marketing/Task_assignments.xlsx` and `Documents/Admin/Task_assignment.xlsx` are present | Two distinct candidate files exist; the task's second reference is ambiguous, not an absent asset |

The known source references that share a task-local fixture do not change the OwnCloud inventory result: for example, finance-revenue-reconciliation has no local contract workbook copy, and ml-generate-gradcam has only evaluator reference outputs, not `test_image.jpg`. The earlier fixture inventory is useful for regression/scoring but not a substitute for missing cloud input.

## Recovery resolution

The five original task-author assets are restored by the derived ownCloud recipe: the four finance/GradCAM assets above plus the final prose policy from PR #815. Served byte hashes were checked through WebDAV, rather than inferring availability from the filesystem. The seven unambiguous task path references are corrected in the fork. For the sugar task, the published `top-sugar-sources.xlsx` is the age-2-to-8 table named “Top sources of total sugars”; the second workbook is the age-9-to-18 table. Their food/value rows match the task’s source-topic/reference values. The prompt now uses the actual seeded filename; no substitute workbook or filename alias is created. Evaluator references and scores remain unchanged.
