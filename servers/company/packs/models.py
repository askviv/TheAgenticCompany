"""Company pack v1 contract; native document validation belongs to Frappe."""
from __future__ import annotations
from graphlib import TopologicalSorter, CycleError
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Slug = Annotated[str, Field(pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$', max_length=100)]
Text = Annotated[str, Field(min_length=1)]
Application = Literal['erpnext', 'email', 'rocketchat', 'owncloud', 'plane', 'midpoint', 'payroll', 'lending', 'crm', 'helpdesk']
NativeDocType = Literal['Customer', 'Supplier', 'Item', 'Purchase Order', 'Purchase Receipt', 'Purchase Invoice', 'Sales Order', 'Delivery Note', 'Sales Invoice', 'Payment Entry', 'Bank Transaction', 'Account', 'Bank Account', 'Salary Component', 'Salary Structure', 'Salary Structure Assignment', 'Attendance', 'Leave Application', 'Timesheet', 'Expense Claim', 'HD Ticket', 'CRM Lead', 'Project', 'Task', 'Contact', 'Address', 'Quotation', 'Material Request', 'Stock Entry', 'Journal Entry', 'Warehouse', 'Cost Center', 'BOM', 'Work Order', 'Request for Quotation', 'Supplier Quotation', 'Payment Request', 'Issue', 'Opportunity', 'Lead', 'ToDo', 'Payroll Entry', 'Salary Slip', 'Additional Salary', 'Employee Checkin', 'Employee Advance', 'Loan Application', 'Loan', 'Loan Product', 'Loan Disbursement', 'Loan Repayment']

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class CompanyPersona(Model):
    applications: list[Application]
    company_name: Text
    business_type: Text
    industry: Text
    description: Text
    what_it_sells: list[Text] = Field(min_length=1)
    how_it_operates: Text
    day_to_day: list[Text] = Field(min_length=1)
    workflows: dict[str, Text] = Field(min_length=1)
    policies: dict[str, Text] = Field(min_length=1)
    trading_context: Text
    currency: Annotated[str, Field(pattern=r'^[A-Z]{3}$')]
    country: Text

class Department(Model):
    id: Slug
    name: Text
    description: Text

class Employee(Model):
    id: Slug
    display_name: Text
    department_id: Slug
    job_title: Text
    responsibilities: list[Text] = Field(min_length=1)
    reports_to: Slug | None = None
    context: Text

class RoleUnderEvaluation(Model):
    title: Text
    department_id: Slug
    responsibilities: list[Text] = Field(min_length=1)

class Record(Model):
    id: Slug
    doctype: NativeDocType
    fields: dict[str, JsonValue]
    state: Literal['draft', 'submitted'] = 'draft'

class PackFile(Model):
    id: Slug
    path: Text
    drive_path: Text
    visibility: Literal['staff', 'evaluation']
    department_ids: list[Slug] = Field(default_factory=list)

class Experiment(Model):
    id: Slug
    title: Text
    department_id: Slug
    sender_employee_id: Slug
    brief: Text
    request_message: Text
    files: list[Slug] = Field(default_factory=list)
    records: list[Slug] = Field(default_factory=list)
    evaluation: dict[str, JsonValue]


def safe_path(value: str) -> str:
    from pathlib import PurePosixPath
    path = PurePosixPath(value)
    if not value or '\\' in value or '\x00' in value or path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')) or ':' in value:
        raise ValueError('Pack paths must be normalized relative POSIX paths')
    return value


def record_dependencies(value: JsonValue, record_ids: set[str], employee_ids: set[str], department_ids: set[str]) -> set[str]:
    deps: set[str] = set()
    if isinstance(value, dict):
        markers = set(value) & {'$ref', '$binding', '$date_offset'}
        if any(key.startswith('$') and key not in markers for key in value):
            raise ValueError('Unknown native reference marker')
        if markers:
            if len(value) != 1 or len(markers) != 1:
                raise ValueError('References must contain exactly one marker')
            if '$ref' in value:
                target = value['$ref']
                if not isinstance(target, str) or target not in record_ids:
                    raise ValueError('Unknown record reference')
                deps.add(target)
            elif '$binding' in value:
                binding = value['$binding']
                allowed = {'company'} | {f'employee:{x}' for x in employee_ids} | {f'department:{x}' for x in department_ids}
                if not isinstance(binding, str) or binding not in allowed:
                    raise ValueError('Unknown directory binding')
            elif type(value['$date_offset']) is not int:
                raise ValueError('Date offsets must be integer days')
        else:
            for item in value.values():
                deps |= record_dependencies(item, record_ids, employee_ids, department_ids)
    elif isinstance(value, list):
        for item in value:
            deps |= record_dependencies(item, record_ids, employee_ids, department_ids)
    return deps

class Manifest(Model):
    schema_version: Literal['1.0']
    id: Slug
    version: Text
    title: Text
    company_persona: CompanyPersona
    departments: list[Department] = Field(min_length=1)
    employees: list[Employee] = Field(min_length=1)
    role_under_evaluation: RoleUnderEvaluation
    records: list[Record] = Field(default_factory=list)
    files: list[PackFile] = Field(default_factory=list)
    experiments: list[Experiment] = Field(min_length=1)

    @model_validator(mode='after')
    def references(self) -> Manifest:
        applications = set(self.company_persona.applications)
        if any(file.visibility == 'staff' for file in self.files) and 'owncloud' not in applications:
            raise ValueError('Staff-visible files require the owncloud application')
        if self.records and not applications.intersection({'erpnext', 'payroll', 'lending', 'crm', 'helpdesk'}):
            raise ValueError('Native records require an enabled shared Frappe application')
        groups = [self.departments, self.employees, self.records, self.files, self.experiments]
        for group in groups:
            if len({x.id for x in group}) != len(group):
                raise ValueError('IDs must be unique within each entity kind')
        departments, employees, records, files, _ = [{x.id for x in group} for group in groups]
        if self.role_under_evaluation.department_id not in departments:
            raise ValueError('Unknown role department')
        managers = {}
        for employee in self.employees:
            if employee.department_id not in departments or (employee.reports_to and employee.reports_to not in employees):
                raise ValueError('Unknown employee department or manager')
            managers[employee.id] = {employee.reports_to} if employee.reports_to else set()
        paths, drive_paths = set(), set()
        for file in self.files:
            safe_path(file.path)
            safe_path(file.drive_path)
            prefix = 'files/' if file.visibility == 'staff' else 'private/'
            if not file.path.startswith(prefix) or not set(file.department_ids) <= departments:
                raise ValueError('File visibility prefix or departments are invalid')
            if file.path in paths or file.drive_path in drive_paths:
                raise ValueError('File paths and drive paths must be unique')
            paths.add(file.path)
            drive_paths.add(file.drive_path)
        for experiment in self.experiments:
            if experiment.department_id not in departments or experiment.sender_employee_id not in employees or not set(experiment.files) <= files or not set(experiment.records) <= records:
                raise ValueError('Unknown experiment directory, file or record reference')
        for experiment in self.experiments:
            if any(file.visibility != 'staff' for file in self.files if file.id in experiment.files):
                raise ValueError('Experiment request files must be staff-visible')
            reference_file = experiment.evaluation.get('reference_file_id')
            private_files = {file.id for file in self.files if file.visibility == 'evaluation'}
            if reference_file is not None and (not isinstance(reference_file, str) or reference_file not in private_files):
                raise ValueError('Evaluation reference file must name a private file')
        graph = {r.id: record_dependencies(r.fields, records, employees, departments) for r in self.records}
        try:
            tuple(TopologicalSorter(graph).static_order())
            tuple(TopologicalSorter(managers).static_order())
        except CycleError as exc:
            raise ValueError('Record or reporting references contain a cycle') from exc
        return self
