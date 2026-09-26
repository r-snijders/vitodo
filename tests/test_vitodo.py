import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "vitodo.py"
SPEC = importlib.util.spec_from_file_location("vitodo", MODULE_PATH)
vitodo = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = vitodo
SPEC.loader.exec_module(vitodo)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = vitodo.TaskStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_lifecycle_and_git_commits(self):
        task = self.store.add("Write tests", date(2026, 9, 21))
        self.assertEqual(self.store.load()[0].title, "Write tests")

        self.store.update(task.id, title="Run tests", due=date(2026, 9, 22))
        changed = self.store.load()[0]
        self.assertEqual((changed.title, changed.due), ("Run tests", "2026-09-22"))

        self.store.toggle(task.id)
        self.assertIsNotNone(self.store.load()[0].completed_at)
        self.store.toggle(task.id)
        self.assertIsNone(self.store.load()[0].completed_at)

        self.store.delete(task.id)
        self.assertEqual(self.store.load(), [])
        if (self.root / ".git").exists():
            import subprocess
            count = subprocess.check_output(
                ["git", "rev-list", "--count", "HEAD"], cwd=self.root, text=True
            ).strip()
            self.assertEqual(count, "6")

    def test_json_is_human_readable(self):
        self.store.add("Readable", date(2026, 9, 21))
        raw = json.loads((self.root / "tasks.json").read_text())
        self.assertEqual(raw[0]["title"], "Readable")

    def test_subtasks_and_cascade_delete(self):
        parent = self.store.add("Major", date(2026, 9, 21))
        child = self.store.add("Small", date(2026, 9, 21), parent_id=parent.id)
        grandchild = self.store.add("Tiny", date(2026, 9, 21), parent_id=child.id)
        self.assertEqual(self.store.load()[1].parent_id, parent.id)

        self.store.delete(parent.id)
        self.assertEqual(self.store.load(), [])


class DisplayTests(unittest.TestCase):
    def make_task(self, title, due, completed=False, parent_id=None):
        return vitodo.Task(
            "id-" + title, title, due, "2026-09-01T00:00:00+00:00",
            "done" if completed else None, parent_id,
        )

    def test_days_overdue_is_negative(self):
        task = self.make_task("late", "2026-09-18")
        self.assertEqual(vitodo.days_overdue(task, date(2026, 9, 21)), -3)

    def test_completed_task_is_not_overdue(self):
        task = self.make_task("done", "2026-09-18", completed=True)
        self.assertEqual(vitodo.days_overdue(task, date(2026, 9, 21)), 0)

    def test_visible_includes_overdue_and_selected_day(self):
        tasks = [
            self.make_task("late", "2026-09-18"),
            self.make_task("today", "2026-09-21"),
            self.make_task("future", "2026-09-22"),
            self.make_task("old done", "2026-09-17", completed=True),
        ]
        shown = vitodo.visible_tasks(tasks, date(2026, 9, 21), date(2026, 9, 21))
        self.assertEqual([task.title for task in shown], ["late", "today"])

    def test_all_view_includes_open_and_completed_from_all_dates(self):
        tasks = [
            self.make_task("old done", "2026-09-17", completed=True),
            self.make_task("open", "2026-09-21"),
            self.make_task("new done", "2026-09-20", completed=True),
        ]
        shown = vitodo.visible_tasks(
            tasks,
            date(2026, 9, 21),
            date(2026, 9, 21),
            show_all=True,
        )
        self.assertEqual(
            [task.title for task in shown], ["old done", "new done", "open"]
        )

    def test_tree_rows_and_collapse(self):
        parent = self.make_task("parent", "2026-09-21")
        child = self.make_task("child", "2026-09-21", parent_id=parent.id)
        grandchild = self.make_task("grandchild", "2026-09-21", parent_id=child.id)
        tasks = [parent, child, grandchild]

        expanded = vitodo.tree_rows(tasks, tasks)
        self.assertEqual([row.task.title for row in expanded], ["parent", "child", "grandchild"])
        self.assertTrue(expanded[0].has_children)
        self.assertIn("└─", expanded[1].prefix)

        collapsed = vitodo.tree_rows(tasks, tasks, {parent.id})
        self.assertEqual([row.task.title for row in collapsed], ["parent"])


if __name__ == "__main__":
    unittest.main()
