# TheAgentCompany OwnCloud seed provenance

Original task-author assets were located and verified against upstream GitHub sources on 2026-10-29. The downloaded files were checked for format, byte size and SHA-256. Native XLSX readers confirmed 200 contract records, 4,968 recognition records and 200 employee mappings. The derived image adds these exact bytes and indexes them using ownCloud’s native `occ files:scan` command.

## Original assets and destination paths

The files are recoverable from the original task PRs (not invented/reconstructed data):

| Destination in OwnCloud | Original source | Size | SHA-256 |
| --- | --- | ---: | --- |
| `Documents/Human Resources Team/Contracts/contracts.xlsx` | [PR #844 attachment](https://github.com/user-attachments/files/17966152/contracts.xlsx) | 13,469 B | `5fde7f7334504fdc2f5d46e6e110f0461bbba9049b1da486f32127992c435351` |
| `Documents/Human Resources Team/Contracts/revenue_schedule.xlsx` | [PR #844 attachment](https://github.com/user-attachments/files/17966150/revenue_schedule.xlsx) | 82,417 B | `cd1e6684d6191415a7aff0ae241befdb208f421bac20d8b3874138587426d07b` |
| `Documents/Human Resources Team/Contracts/employee_contracts.xlsx` | [PR #844 attachment](https://github.com/user-attachments/files/17966153/employee_contracts.xlsx) | 12,766 B | `0d24827a7482d6b6cfea948ffa3ce9562d9da401a80275507392c7936b715214` |


Official provenance records:

- [Original finance task PR #844](https://github.com/TheAgentCompany/TheAgentCompany/pull/844), body has the three exact spreadsheet attachments and source path. Its task spec says `Documents/Human Resources Team/Contracts`.

Caveat: `workspaces/tasks/finance-revenue-reconciliation/checkpoints.md` says `Documents/Financials/Contracts` for its trajectory checkpoint, but task.md and original seed PR #844 both specify `Documents/Human Resources Team/Contracts`. This looks like an existing checkpoint path typo, not a different intended storage location. The requested employee lookup file is also explicitly required by task.md and PR #844, even though #1081 omitted it; restore it with the other two.

## Broader OwnCloud seed provenance and coverage

- The official backup-data repository is [TheAgentCompany/the-agent-company-backup-data](https://github.com/TheAgentCompany/the-agent-company-backup-data). Its complete Git tree only contains `LICENSE` and `README.md`. Its three releases are `plane-20241031-0351` (Postgres/Redis/uploads assets), `rocketchat-20241123` (`db.dump`), and `setup-script-20241208` (`setup.sh`, `setup.bat`). There is **no OwnCloud backup release or asset** there.
- The code's intended seed mechanism is `servers/owncloud/Dockerfile` copying `./owncloud_data` into the image and `servers/owncloud/init.sh` moving it into OwnCloud's data directory. The task repo's `.gitignore` ignores `*owncloud_data*` and `*owncloud_apps*`; neither is present/tracked in this clone. `docs/DEVELOPMENT.md` says server services are pre-baked with data and directs OwnCloud data modifications to an issue. Thus the full seed bundle is outside the public source checkout; the original finance task PR provides authoritative standalone originals for the three missing contract inputs above.
- Local scan finds 56 retained task `dependencies.yml` entries naming `owncloud`, so restore only the three contract assets to make the finance task's inputs available; this does not validate the entire seed tree. Broader coverage would require auditing all paths referenced by the 56 dependent tasks against the pre-baked service's file listing.
- Hugging Face's official TheAgentCompany org lists only [`TheAgentCompany/ubuntu_os_images`](https://huggingface.co/datasets/TheAgentCompany/ubuntu_os_images), a 91.5 GB OS-image dataset. No OwnCloud seed dataset is listed there; it is not a source for these files.

## Validation boundaries

Hashes and file-format checks establish the identity/type of retrieved originals only. I did not inspect workbook/image contents or verify whether a currently running OwnCloud deployment has these files. The deployment must verify served bytes at the exact paths (including spaces and capitalization), and then verify that the same files survive a benchmark reset. The broader 56-task path audit is recorded in the deployment seed audit; file presence alone does not prove all task outcomes.

## Expense policy correction

Original [PR #815](https://github.com/TheAgentCompany/TheAgentCompany/pull/815) explicitly replaced the old machine-matchable `category_rules.xlsx` with a prose `category_rules.txt`, but its comments confirm the old workbook was accidentally backed up. The task instructions already name `.txt`. Restore the final PR-body attachment to `Documents/Financials/Expenses/category_rules.txt`: [original asset](https://github.com/user-attachments/files/17966249/category_rules.txt), SHA-256 `708c6f4b060b90f962ff24833269301469c314560a0a8a2468f694cf38a8c587`. Existing files are preserved; the examinee is directed to the original prose policy.
