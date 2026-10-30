import json
from pathlib import Path


PROFILE = Path(__file__).parents[3] / "deploy/single-host/company-identities.json"


def _profile():
    return json.loads(PROFILE.read_text())


def test_department_assignments_match_the_native_directory_roster():
    expected = {
        "sarah-johnson": "Technical",
        "li-ming": "Technical",
        "zhang-wei": "Technical",
        "wang-fang": "Technical",
        "mike-chen": "Technical",
        "emily-zhou": "Technical",
        "liu-qiang": "Technical",
        "priya-sharma": "Documentation",
        "mark-johnson": "Sales/Marketing",
        "jessica-lee": "Sales/Marketing",
        "chen-xinyi": "HR",
        "david-wong": "Finance",
        "huang-jie": "Product/UX",
        "sophia-rodriguez": "Product/UX",
        "alex-turner": "Technical",
        "emma-lewis": "Technical",
        "jessica-chen": "Technical",
    }
    employees = {employee["id"]: employee for employee in _profile()["employees"]}

    assert set(employees) == set(expected)
    assert {employee_id: employees[employee_id]["department"] for employee_id in expected} == expected
    assert all(
        employees[employee_id]["departmentSource"]
        == "ownCloud:Documents/Human Resources Team/Attendance/employee-department.odt"
        for employee_id in expected
    )


def test_department_policy_covers_every_staff_member_and_native_department_folder():
    owncloud = _profile()["company"]["background"]["owncloud"]
    policy = owncloud["access_policy_candidates"]["department_based"]
    employees = {employee["id"]: employee for employee in _profile()["employees"]}
    memberships = [
        employee_id
        for group in policy["department_groups"]
        for employee_id in group["employee_ids"]
    ]

    assert policy["status"] == "selected"
    assert owncloud["selected_access_policy"] == "department_based"
    assert len(memberships) == len(set(memberships)) == len(employees)
    assert set(memberships) == set(employees)
    assert all(
        employees[employee_id]["department"] == group["department"]
        for group in policy["department_groups"]
        for employee_id in group["employee_ids"]
    )

    folder_resources = {
        resource_id
        for resource_id, resource in owncloud["candidate_resources"].items()
        if resource.get("is_collection") is True and resource_id != "company-documents"
    }
    folder_assignments = policy["department_folder_shares"]
    assert len(folder_assignments) == 9
    assert {share["file_reference_id"] for share in folder_assignments} == folder_resources
    assert all(share["permissions"] == 15 for share in folder_assignments)
    assert all(
        share["assignment_basis"] == "proposed_inference"
        for share in folder_assignments
        if share["file_reference_id"]
        in {
            "folder-admin", "folder-administrative-specialist", "folder-data-analysis",
            "folder-engineering", "folder-q1-planning", "folder-research",
        }
    )
