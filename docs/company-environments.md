# Named company environments

The dashboard company switcher keeps one running VM and the installed software.
Each named company retains an independent application dataset: ERP/Frappe site
and database data, HR/Lending/CRM/Helpdesk records, chat, mailboxes, files,
directory state, employee browser profiles, experiments, pack imports, private
configuration and company clock/deadline state.

This is application-data isolation, not multiple `Company` rows in one shared
ERP database, and not a collection of VM images. The same application addresses
serve whichever company is active. Runtime code, Docker images, VM/kernel,
transport certificates and the external model sign-in remain shared infrastructure.
Inactive companies do not run and their company time is paused.

## Using the switcher

The switcher appears above the dashboard content:

- **Rename active company** changes its namespace label. It does not rename the
  legal entity inside ERP or replace the company persona.
- **Duplicate active company** captures its current application data and creates
  another named company at that point. The source remains active. The copy can
  subsequently be switched to and changed independently.
- **Switch company** captures the source, restores the selected company's data
  and clock, then resumes its applications. Imports and runs subsequently use
  that company's restored application state.

Switching and duplication temporarily stop company application services so
databases and file stores are consistent. They do not reset the company or start
an experiment. Company URLs can be temporarily unavailable during the operation.
The browser observes progress through the host control plane, which remains
available while guest application services are stopped. Preparing an operation
requires the private controller API and the dashboard's CSRF protection. A scoped
job token acknowledges receipt before work starts and then reads that job's
progress. It cannot change the authorized company or action. The authenticated
company list can recover an unacknowledged receipt if the original response was
lost; company services remain running until that acknowledgement.

The first installation registers the existing live company as **Current company**.
It does not capture, import, duplicate or switch its data automatically.

## Storage and recovery

The guest inventory explicitly identifies persistent data roots and Compose
projects. The installer inspects the existing deployment and prepares storage
for canonical employees' profiles; it does not start employee desktops or model
calls. Company generations contain application data/configuration rather than
Docker images or VM disks. A duplicate initially references an immutable captured
generation; later captures publish new generations without modifying the old one.

During a transition, native service shutdown and Docker restart suppression stop
writers. A persistent service interlock prevents startup against partially
restored data after a reboot. Restore stages each root on its existing filesystem
and journals the original, staged and installed paths. Original data stays
available until the complete operation is committed. Recovery restores the source
company and clock when a switch fails; unresolved recovery stays visible and
retains its interlock instead of presenting a mixed company as ready.

Clock checkpoints include the clock database and issue-deadline records. While
applications are stopped, the host uses the existing guest agent and kernel-clock
controller to restore the selected company's saved time and rate. The VM and
clock-capable kernel are reused. No application sees another company's active
time while it is running.

Existing explicitly real-time transport/handoff rules retain their timing
semantics. For example, the five-real-minute unread-mail handoff is not converted
to company time. Inactive namespace timers cannot write into the active company;
restoring a company reconciles its own outstanding timers.

Captured generations consume disk space. Referenced generations are not deleted
by switching. Redundant temporary restore copies are cleaned only after commit;
longer-term generation retention is an operator storage policy. Keep namespace
storage private and include it in any backup policy required by the deployment.
Shared application upgrades must remain compatible with saved datasets or apply
the applications' normal migrations; namespaces are not software-version archives.

## Deployment

The normal VM bootstrap installs the namespace control code, guest inventory,
writer interlocks and startup wiring. The host clock API also serves namespace
operations and scoped job status. The public experiment frontend forwards only
the job-status GET and receipt-acknowledgement POST paths to that host API.

The guest installer can also update an existing deployment from its source tree:

```sh
sudo /data/theagentcompany/enterprise/venv/bin/python \
  deploy/virtual-time-world/guest/configure_company_environments.py \
  --source /data/theagentcompany/enterprise/source \
  --compose-dir /etc/company-workplace/compose
```

For older deployments, pass the actual rendered Compose directory. The private
inventory is `/etc/company-workplace/company-environments.json`; snapshot storage
defaults to `/data/theagentcompany/company-environments` inside the guest. The
host registry and clock checkpoints live under the configured world root's
`company-environments` directory. These are independent of the active company
application state and must never be included in the data roots being swapped.

The installer preserves an existing inventory. Changing application storage after
snapshots exist requires reconciling the inventory and saved generations; blindly
changing path positions would restore data to the wrong place. It fails clearly
rather than silently excluding a new persistent store. The inventory, snapshot
storage, job markers and control-plane credentials must remain root-owned.

No automatic reset, namespace deletion, concurrent company execution or scheduled
experiment execution is part of this feature.
