"""
Example: WorkspaceDB standalone usage

Shows how to use the SQLite database that lives inside each workspace
for persisting engineering artifacts (problems, requirements, use cases, issues).
"""

from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.workspace.workspace_db import WorkspaceDB
from bizniz.workspace.temp_workspace import TempWorkspace


if __name__ == "__main__":

    workspace = TempWorkspace()

    with WorkspaceDB(workspace) as db:

        # Save a problem statement
        pid = db.save_problem("Build a task management CLI tool.")
        print(f"Problem #{pid}: {db.get_problem(pid)['statement']}")

        # Save requirements
        db.save_requirement(pid, "business", "Users can create, list, and complete tasks.")
        db.save_requirement(pid, "functional", "Tasks are stored in a JSON file.")
        db.save_requirement(pid, "nonfunctional", "Startup time under 100ms.")

        print(f"\nAll requirements ({len(db.get_requirements(pid))}):")
        for r in db.get_requirements(pid):
            print(f"  [{r['type']}] {r['text']}")

        # Save use cases
        db.save_use_case(pid, "Add Task", "User adds a new task with a title and optional due date.")
        db.save_use_case(pid, "Complete Task", "User marks a task as done by ID.")

        print(f"\nUse cases ({len(db.get_use_cases(pid))}):")
        for uc in db.get_use_cases(pid):
            print(f"  {uc['title']}: {uc['description']}")

        # Save issues (coding tasks)
        i1 = db.save_issue(pid, "Implement task storage", "CRUD for tasks in JSON", "storage.py", "test_storage.py")
        i2 = db.save_issue(pid, "Implement CLI interface", "argparse-based CLI", "cli.py", "test_cli.py")

        print(f"\nOpen issues: {len(db.get_open_issues(pid))}")

        # Update issue status
        db.update_issue_status(i1, "in_progress")
        print(f"Issue #{i1} status: {db.get_issue(i1)['status']}")

        db.close_issue(i1)
        print(f"Issue #{i1} status: {db.get_issue(i1)['status']}")
        print(f"Issue #{i1} closed_at: {db.get_issue(i1)['closed_at']}")

        print(f"\nOpen issues remaining: {len(db.get_open_issues(pid))}")

    print(f"\nDB location: {workspace.root / '.bizniz' / 'bizniz.db'}")
