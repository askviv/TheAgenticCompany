## Workspaces directory

This directory hosts tasks that are local to the examinee (e.g. agents or human testers). It is structured as follows:

```
├── base_image/
│   ├── Dockerfile
│   ├── init.sh
│   ├── eval.py
│   └── ...
├── tasks/
│   └── admin-arrange-meeting-rooms-image/
│       ├── Dockerfile
│       ├── evaluator.py
│       ├── checkpoints.md
│       ├── dependencies.yml
│       ├── task.md
|   └── ...
```

Explanations:
- base_image is the folder that contains shared functions, evaluation utilities, image build scripts, and other scaffolds.
- tasks is the folder for definitions of all 175 tasks, within which
  - Dockerfile defines the environment of each task setup
  - evaluator.py defines all checkpoint grading functions
  - checkpoints.md is the documentation for grading functions (for human reference only)
  - dependencies.yml defines the list of service dependencies
  - task.md is the task specification, contains background and requirements of each task, and is the **only** file that should be prompted to agents


## Full list of task images

We built and published all 175 task images. Please find the full list below, or download
the list [here](https://github.com/TheAgentCompany/TheAgentCompany/releases/download/1.0.0/tasks.md).

If you'd like to learn how task images are built, see [this GitHub workflow](https://github.com/TheAgentCompany/TheAgentCompany/blob/main/.github/workflows/task-release.yml).

- ghcr.io/theagentcompany/admin-arrange-meeting-rooms-image:1.0.0
- ghcr.io/theagentcompany/admin-ask-for-meeting-feedback-image:1.0.0
- ghcr.io/theagentcompany/admin-ask-for-upgrade-reimbursement-image:1.0.0
- ghcr.io/theagentcompany/admin-check-employees-budget-and-reply-image:1.0.0
- ghcr.io/theagentcompany/admin-check-employees-budget-and-reply-2-image:1.0.0
- ghcr.io/theagentcompany/admin-check-employees-budget-and-reply-and-record-image:1.0.0
- ghcr.io/theagentcompany/admin-collect-requests-and-compute-total-price-image:1.0.0
- ghcr.io/theagentcompany/admin-employee-info-reconciliation-image:1.0.0
- ghcr.io/theagentcompany/admin-get-best-vendor-quote-image:1.0.0
- ghcr.io/theagentcompany/admin-make-spreadsheet-image:1.0.0
- ghcr.io/theagentcompany/admin-mass-forms-filling-image:1.0.0
- ghcr.io/theagentcompany/admin-read-survey-and-summarise-image:1.0.0
- ghcr.io/theagentcompany/admin-remove-pages-pdf-image:1.0.0
- ghcr.io/theagentcompany/admin-translate-sales-chat-image:1.0.0
- ghcr.io/theagentcompany/admin-watch-video-image:1.0.0
- ghcr.io/theagentcompany/bm-classify-nationality-image:1.0.0
- ghcr.io/theagentcompany/ds-answer-numerical-data-question-image:1.0.0
- ghcr.io/theagentcompany/ds-answer-spreadsheet-questions-image:1.0.0
- ghcr.io/theagentcompany/ds-calculate-spreadsheet-stats-image:1.0.0
- ghcr.io/theagentcompany/ds-coffee-shop-database-management-image:1.0.0
- ghcr.io/theagentcompany/ds-find-meeting-spreadsheet-image:1.0.0
- ghcr.io/theagentcompany/ds-fix-table-values-and-missing-answers-image:1.0.0
- ghcr.io/theagentcompany/ds-format-excel-sheets-image:1.0.0
- ghcr.io/theagentcompany/ds-merge-multiple-sheets-image:1.0.0
- ghcr.io/theagentcompany/ds-organise-report-sus-data-image:1.0.0
- ghcr.io/theagentcompany/ds-predictive-modeling-image:1.0.0
- ghcr.io/theagentcompany/ds-sql-exercise-image:1.0.0
- ghcr.io/theagentcompany/ds-stock-analysis-slides-image:1.0.0
- ghcr.io/theagentcompany/ds-visualize-data-in-pie-and-bar-chart-image:1.0.0
- ghcr.io/theagentcompany/example-image:1.0.0
- ghcr.io/theagentcompany/finance-apply-tax-credit-image:1.0.0
- ghcr.io/theagentcompany/finance-budget-variance-image:1.0.0
- ghcr.io/theagentcompany/finance-check-attendance-payroll-image:1.0.0
- ghcr.io/theagentcompany/finance-create-10k-income-report-image:1.0.0
- ghcr.io/theagentcompany/finance-expense-validation-image:1.0.0
- ghcr.io/theagentcompany/finance-find-signatories-image:1.0.0
- ghcr.io/theagentcompany/finance-invoice-matching-image:1.0.0
- ghcr.io/theagentcompany/finance-nonqualified-bill-ask-for-reimburse-image:1.0.0
- ghcr.io/theagentcompany/finance-qualified-bill-ask-for-reimburse-image:1.0.0
- ghcr.io/theagentcompany/finance-r-d-activities-image:1.0.0
- ghcr.io/theagentcompany/finance-revenue-reconciliation-image:1.0.0
- ghcr.io/theagentcompany/finance-substantial-presence-test-image:1.0.0
- ghcr.io/theagentcompany/hr-analyze-outing-bills-image:1.0.0
- ghcr.io/theagentcompany/hr-check-attendance-multiple-days-image:1.0.0
- ghcr.io/theagentcompany/hr-check-attendance-multiple-days-department-image:1.0.0
- ghcr.io/theagentcompany/hr-check-attendance-multiple-days-department-with-chat-image:1.0.0
- ghcr.io/theagentcompany/hr-check-attendance-one-day-image:1.0.0
- ghcr.io/theagentcompany/hr-check-for-invalid-passwords-and-ask-for-valid-passwords-image:1.0.0
- ghcr.io/theagentcompany/hr-collect-feedbacks-image:1.0.0
- ghcr.io/theagentcompany/hr-collect-multiple-valid-passwords-image:1.0.0
- ghcr.io/theagentcompany/hr-create-career-ladder-image:1.0.0
- ghcr.io/theagentcompany/hr-create-employee-manual-image:1.0.0
- ghcr.io/theagentcompany/hr-delete-and-insert-user-image:1.0.0
- ghcr.io/theagentcompany/hr-get-valid-password-image:1.0.0
- ghcr.io/theagentcompany/hr-green-card-consultation-image:1.0.0
- ghcr.io/theagentcompany/hr-make-slides-introduce-leadership-image:1.0.0
- ghcr.io/theagentcompany/hr-mass-survey-image:1.0.0
- ghcr.io/theagentcompany/hr-massive-resume-screening-image:1.0.0
- ghcr.io/theagentcompany/hr-new-grad-job-description-image:1.0.0
- ghcr.io/theagentcompany/hr-new-grad-job-description-2-image:1.0.0
- ghcr.io/theagentcompany/hr-new-grad-job-description-3-image:1.0.0
- ghcr.io/theagentcompany/hr-organize-talent-info-image:1.0.0
- ghcr.io/theagentcompany/hr-pick-interviewer-1-image:1.0.0
- ghcr.io/theagentcompany/hr-pick-interviewer-2-image:1.0.0
- ghcr.io/theagentcompany/hr-pick-interviewer-3-image:1.0.0
- ghcr.io/theagentcompany/hr-populate-salary-increase-memo-image:1.0.0
- ghcr.io/theagentcompany/hr-resume-categorization-image:1.0.0
- ghcr.io/theagentcompany/hr-resume-screening-image:1.0.0
- ghcr.io/theagentcompany/hr-salary-analysis-image:1.0.0
- ghcr.io/theagentcompany/hr-transfer-group-image:1.0.0
- ghcr.io/theagentcompany/ml-generate-gradcam-image:1.0.0
- ghcr.io/theagentcompany/ml-grade-exam-image:1.0.0
- ghcr.io/theagentcompany/pm-add-new-moderator-image:1.0.0
- ghcr.io/theagentcompany/pm-ask-issue-assignee-for-issue-status-and-update-in-plane-image:1.0.0
- ghcr.io/theagentcompany/pm-assign-issues-image:1.0.0
- ghcr.io/theagentcompany/pm-change-channel-ownership-image:1.0.0
- ghcr.io/theagentcompany/pm-check-backlog-update-issues-image:1.0.0
- ghcr.io/theagentcompany/pm-create-channel-message-image:1.0.0
- ghcr.io/theagentcompany/pm-create-channel-message-medium-image:1.0.0
- ghcr.io/theagentcompany/pm-create-channel-new-leader-image:1.0.0
- ghcr.io/theagentcompany/pm-create-plane-issue-image:1.0.0
- ghcr.io/theagentcompany/pm-create-teammate-channel-from-spreadsheet-image:1.0.0
- ghcr.io/theagentcompany/pm-monthly-attendance-slides-image:1.0.0
- ghcr.io/theagentcompany/pm-plan-personnel-for-new-project-image:1.0.0
- ghcr.io/theagentcompany/pm-present-engineer-group-members-image:1.0.0
- ghcr.io/theagentcompany/pm-projects-analytics-image:1.0.0
- ghcr.io/theagentcompany/pm-schedule-meeting-1-image:1.0.0
- ghcr.io/theagentcompany/pm-schedule-meeting-2-image:1.0.0
- ghcr.io/theagentcompany/pm-send-hello-message-image:1.0.0
- ghcr.io/theagentcompany/pm-send-notification-to-corresponding-user-image:1.0.0
- ghcr.io/theagentcompany/pm-update-sprint-cycles-image:1.0.0
- ghcr.io/theagentcompany/qa-escalate-emergency-image:1.0.0
- ghcr.io/theagentcompany/qa-update-issue-status-according-to-colleagues-image:1.0.0
- ghcr.io/theagentcompany/research-answer-questions-on-paper-image:1.0.0
- ghcr.io/theagentcompany/research-reproduce-figures-image:1.0.0
