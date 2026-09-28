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

    def test_move_tasks_and_subtasks_within_their_groups(self):
        first = self.store.add("First", date(2026, 9, 21))
        second = self.store.add("Second", date(2026, 9, 21))
        third = self.store.add("Third", date(2026, 9, 21))
        child_one = self.store.add("Child one", date(2026, 9, 21), parent_id=first.id)
        child_two = self.store.add("Child two", date(2026, 9, 21), parent_id=first.id)

        self.assertTrue(self.store.move(third.id, -1))
        tasks = self.store.load()
        roots = vitodo.visible_tasks(tasks, date(2026, 9, 21))
        rows = vitodo.tree_rows(tasks, roots)
        root_titles = [row.task.title for row in rows if row.task.parent_id is None]
        self.assertEqual(root_titles, ["First", "Third", "Second"])

        self.assertTrue(self.store.move(child_two.id, -1))
        tasks = self.store.load()
        rows = vitodo.tree_rows(tasks, vitodo.visible_tasks(tasks, date(2026, 9, 21)))
        children = [row.task.title for row in rows if row.task.parent_id == first.id]
        self.assertEqual(children, ["Child two", "Child one"])
        self.assertFalse(self.store.move(first.id, -1))

    def test_parent_due_date_moves_earlier_descendants_forward(self):
        parent = self.store.add("Parent", date(2026, 9, 21))
        child = self.store.add("Child", date(2026, 9, 22), parent_id=parent.id)
        grandchild = self.store.add(
            "Grandchild", date(2026, 9, 23), parent_id=child.id
        )
        later_child = self.store.add(
            "Already later", date(2026, 9, 27), parent_id=parent.id
        )

        adjusted = self.store.update(parent.id, due=date(2026, 9, 25))
        by_id = {task.id: task for task in self.store.load()}
        self.assertEqual(adjusted, 2)
        self.assertEqual(by_id[child.id].due, "2026-09-25")
        self.assertEqual(by_id[grandchild.id].due, "2026-09-25")
        self.assertEqual(by_id[later_child.id].due, "2026-09-27")

    def test_reparent_to_task_and_back_to_root(self):
        parent = self.store.add("Parent", date(2026, 9, 25))
        child = self.store.add("Child", date(2026, 9, 21))
        grandchild = self.store.add("Grandchild", date(2026, 9, 22), child.id)

        adjusted = self.store.reparent(child.id, parent.id)
        by_id = {task.id: task for task in self.store.load()}
        self.assertEqual(adjusted, 2)
        self.assertEqual(by_id[child.id].parent_id, parent.id)
        self.assertEqual(by_id[child.id].due, "2026-09-25")
        self.assertEqual(by_id[grandchild.id].due, "2026-09-25")

        self.store.reparent(child.id, None)
        self.assertIsNone({task.id: task for task in self.store.load()}[child.id].parent_id)

    def test_reparent_rejects_cycles_and_resolves_short_ids(self):
        parent = self.store.add("Parent", date(2026, 9, 21))
        child = self.store.add("Child", date(2026, 9, 21), parent.id)
        self.assertEqual(self.store.resolve_id(parent.id[:6]).id, parent.id)
        with self.assertRaises(ValueError):
            self.store.reparent(parent.id, child.id)
        with self.assertRaises(ValueError):
            self.store.reparent(parent.id, parent.id)

    def test_daily_recurrence_creates_next_occurrence_once(self):
        task = self.store.add("Daily review", date(2026, 9, 21))
        self.store.update(task.id, recurrence="daily", update_recurrence=True)
        next_task = self.store.toggle(task.id)
        self.assertIsNotNone(next_task)
        self.assertEqual(next_task.due, "2026-09-22")
        self.assertEqual(next_task.recurrence, "daily")
        self.assertEqual(next_task.series_id, task.id)

        self.store.toggle(task.id)
        duplicate = self.store.toggle(task.id)
        self.assertIsNone(duplicate)
        occurrences = [
            item for item in self.store.load()
            if item.series_id == task.id and item.due == "2026-09-22"
        ]
        self.assertEqual(len(occurrences), 1)

    def test_weekly_recurrence_and_postpone_propagation(self):
        parent = self.store.add("Weekly", date(2026, 9, 21))
        child = self.store.add("Child", date(2026, 9, 21), parent.id)
        self.store.update(parent.id, recurrence="weekly", update_recurrence=True)
        created = self.store.toggle(parent.id)
        self.assertEqual(created.due, "2026-09-28")

        adjusted = self.store.update(parent.id, due=date(2026, 9, 22))
        self.assertEqual(adjusted, 1)
        by_id = {task.id: task for task in self.store.load()}
        self.assertEqual(by_id[child.id].due, "2026-09-22")


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

    def test_open_view_includes_only_open_tasks_from_all_dates(self):
        tasks = [
            self.make_task("past open", "2026-09-17"),
            self.make_task("future open", "2026-09-24"),
            self.make_task("done", "2026-09-18", completed=True),
        ]
        shown = vitodo.visible_tasks(
            tasks,
            date(2026, 9, 21),
            date(2026, 9, 21),
            show_open=True,
        )
        self.assertEqual([task.title for task in shown], ["past open", "future open"])

    def test_open_tree_does_not_restore_completed_parent(self):
        parent = self.make_task("done parent", "2026-09-17", completed=True)
        child = self.make_task(
            "open child", "2026-09-18", parent_id=parent.id
        )
        tasks = [parent, child]
        visible = vitodo.visible_tasks(
            tasks, date(2026, 9, 21), show_open=True
        )
        rows = vitodo.tree_rows(
            tasks, visible, include_hidden_ancestors=False
        )
        self.assertEqual([row.task.title for row in rows], ["open child"])

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
