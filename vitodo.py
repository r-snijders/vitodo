#!/usr/bin/env python3
"""vitodo: a small, Vim-friendly, Git-backed terminal todo list."""

from __future__ import annotations

import argparse
import curses
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable


APP_NAME = "vitodo"
DEFAULT_DATA_DIR = Path(os.environ.get("VITODO_DATA_DIR", "~/.local/share/vitodo")).expanduser()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Task:
    id: str
    title: str
    due: str
    created_at: str
    completed_at: str | None = None
    parent_id: str | None = None
    position: int = 0
    recurrence: str | None = None
    series_id: str | None = None

    @classmethod
    def from_dict(cls, value: dict) -> "Task":
        return cls(
            id=value["id"],
            title=value["title"],
            due=value["due"],
            created_at=value["created_at"],
            completed_at=value.get("completed_at"),
            parent_id=value.get("parent_id"),
            position=value.get("position", 0),
            recurrence=value.get("recurrence"),
            series_id=value.get("series_id"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "due": self.due,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "parent_id": self.parent_id,
            "position": self.position,
            "recurrence": self.recurrence,
            "series_id": self.series_id,
        }


@dataclass
class TaskRow:
    task: Task
    prefix: str = ""
    has_children: bool = False


class TaskStore:
    """JSON task storage with one automatic Git commit per mutation."""

    def __init__(self, root: Path = DEFAULT_DATA_DIR):
        self.root = root
        self.path = root / "tasks.json"

    def initialize(self) -> None:
        if not shutil.which("git"):
            raise RuntimeError("Git is required. Install it with: sudo apt install git")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])
        if shutil.which("git") and not (self.root / ".git").exists():
            self._git("init", "-q")
            self._git("config", "user.name", "vitodo")
            self._git("config", "user.email", "vitodo@localhost")
            self._commit("Initialize vitodo task store")

    def load(self) -> list[Task]:
        self.initialize()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [Task.from_dict(item) for item in raw]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Cannot read {self.path}: {exc}") from exc

    def save(self, tasks: list[Task], message: str) -> None:
        self._write(tasks)
        self._commit(message)

    def add(self, title: str, due: date, parent_id: str | None = None) -> Task:
        tasks = self.load()
        if parent_id is not None:
            self._find(tasks, parent_id)
        peer_positions = [
            item.position
            for item in tasks
            if item.parent_id == parent_id and item.due == due.isoformat()
        ]
        position = max(peer_positions, default=-1) + 1
        task = Task(
            uuid.uuid4().hex[:12], title.strip(), due.isoformat(), now_iso(),
            parent_id=parent_id, position=position,
        )
        tasks.append(task)
        kind = "subtask" if parent_id else "task"
        self.save(tasks, f"Add {kind} for {task.due}: {task.title}")
        return task

    def update(
        self,
        task_id: str,
        *,
        title: str | None = None,
        due: date | None = None,
        recurrence: str | None = None,
        update_recurrence: bool = False,
    ) -> int:
        tasks = self.load()
        task = self._find(tasks, task_id)
        adjusted = 0
        if title is not None:
            task.title = title.strip()
        if update_recurrence:
            if recurrence not in (None, "daily", "weekly"):
                raise ValueError("recurrence must be daily, weekly, or None")
            task.recurrence = recurrence
            task.series_id = (task.series_id or task.id) if recurrence else None
        if due is not None:
            new_due = due.isoformat()
            task.due = new_due
            ancestors = {task.id}
            visited = {task.id}
            while ancestors:
                descendants = [
                    item
                    for item in tasks
                    if item.parent_id in ancestors and item.id not in visited
                ]
                ancestors = {item.id for item in descendants}
                visited.update(ancestors)
                for descendant in descendants:
                    if descendant.due < new_due:
                        descendant.due = new_due
                        adjusted += 1
        suffix = f"; align {adjusted} subtask due date(s)" if adjusted else ""
        self.save(tasks, f"Edit task: {task.title}{suffix}")
        return adjusted

    def toggle(self, task_id: str) -> Task | None:
        tasks = self.load()
        task = self._find(tasks, task_id)
        task.completed_at = None if task.completed_at else now_iso()
        action = "Reopen" if task.completed_at is None else "Complete"
        next_task = None
        if task.completed_at and task.recurrence:
            interval = timedelta(days=1 if task.recurrence == "daily" else 7)
            next_due = date.fromisoformat(task.due) + interval
            series_id = task.series_id or task.id
            task.series_id = series_id
            existing = next(
                (
                    item for item in tasks
                    if item.series_id == series_id
                    and item.due == next_due.isoformat()
                    and item.completed_at is None
                ),
                None,
            )
            if existing is None:
                due_value = next_due.isoformat()
                if task.parent_id:
                    parent = next(
                        (item for item in tasks if item.id == task.parent_id), None
                    )
                    if parent and parent.due > due_value:
                        due_value = parent.due
                peer_positions = [
                    item.position for item in tasks
                    if item.parent_id == task.parent_id and item.due == due_value
                ]
                next_task = Task(
                    uuid.uuid4().hex[:12], task.title, due_value, now_iso(),
                    parent_id=task.parent_id,
                    position=max(peer_positions, default=-1) + 1,
                    recurrence=task.recurrence,
                    series_id=series_id,
                )
                tasks.append(next_task)
        suffix = f"; create next {task.recurrence} occurrence" if next_task else ""
        self.save(tasks, f"{action} task: {task.title}{suffix}")
        return next_task

    def reparent(self, task_id: str, parent_id: str | None) -> int:
        """Move a task below another task, or to the root when parent_id is None."""
        tasks = self.load()
        task = self._find(tasks, task_id)
        parent = self._find(tasks, parent_id) if parent_id else None
        if parent and parent.id == task.id:
            raise ValueError("A task cannot be its own parent")

        ancestor_id = parent.parent_id if parent else None
        seen: set[str] = set()
        while ancestor_id and ancestor_id not in seen:
            if ancestor_id == task.id:
                raise ValueError("A task cannot be moved into its own subtree")
            seen.add(ancestor_id)
            ancestor = next((item for item in tasks if item.id == ancestor_id), None)
            ancestor_id = ancestor.parent_id if ancestor else None

        task.parent_id = parent.id if parent else None
        peer_positions = [
            item.position for item in tasks
            if item.id != task.id
            and item.parent_id == task.parent_id
            and item.due == task.due
        ]
        task.position = max(peer_positions, default=-1) + 1

        adjusted = 0
        if parent and task.due < parent.due:
            new_due = parent.due
            task.due = new_due
            adjusted += 1
            descendants = {task.id}
            visited = {task.id}
            while descendants:
                children = [
                    item for item in tasks
                    if item.parent_id in descendants and item.id not in visited
                ]
                descendants = {item.id for item in children}
                visited.update(descendants)
                for child in children:
                    if child.due < new_due:
                        child.due = new_due
                        adjusted += 1

        destination = parent.title if parent else "root"
        self.save(tasks, f"Move task under {destination}: {task.title}")
        return adjusted

    def resolve_id(self, prefix: str) -> Task:
        matches = [task for task in self.load() if task.id.startswith(prefix.strip())]
        if not matches:
            raise KeyError(f"No task ID starts with {prefix}")
        if len(matches) > 1:
            raise ValueError(f"Task ID {prefix} is ambiguous; enter more characters")
        return matches[0]

    def delete(self, task_id: str) -> None:
        tasks = self.load()
        task = self._find(tasks, task_id)
        deleted = {task_id}
        while True:
            descendants = {item.id for item in tasks if item.parent_id in deleted}
            new_ids = descendants - deleted
            if not new_ids:
                break
            deleted.update(new_ids)
        tasks = [item for item in tasks if item.id not in deleted]
        suffix = f" and {len(deleted) - 1} subtask(s)" if len(deleted) > 1 else ""
        self.save(tasks, f"Delete task{suffix}: {task.title}")

    def move(self, task_id: str, direction: int) -> bool:
        """Move a task among peers at the same level, date, and completion state."""
        if direction not in (-1, 1):
            raise ValueError("direction must be -1 or 1")
        tasks = self.load()
        task = self._find(tasks, task_id)
        peers = [
            item for item in tasks
            if item.parent_id == task.parent_id
            and item.due == task.due
            and (item.completed_at is not None) == (task.completed_at is not None)
        ]
        peers.sort(key=lambda item: (item.position, item.created_at))
        index = peers.index(task)
        target_index = index + direction
        if not 0 <= target_index < len(peers):
            return False
        for position, peer in enumerate(peers):
            peer.position = position
        target = peers[target_index]
        task.position, target.position = target.position, task.position
        action = "up" if direction < 0 else "down"
        self.save(tasks, f"Move task {action}: {task.title}")
        return True

    def _write(self, tasks: list[Task]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        payload = [task.to_dict() for task in tasks]
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.path)

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.root, text=True, capture_output=True, check=False
        )

    def _commit(self, message: str) -> None:
        if not shutil.which("git"):
            return
        if not (self.root / ".git").exists():
            return
        added = self._git("add", "tasks.json")
        if added.returncode != 0:
            raise RuntimeError(added.stderr.strip() or "git add failed")
        committed = self._git("commit", "-q", "-m", message)
        if committed.returncode != 0 and "nothing to commit" not in committed.stdout + committed.stderr:
            raise RuntimeError(committed.stderr.strip() or "git commit failed")

    @staticmethod
    def _find(tasks: list[Task], task_id: str) -> Task:
        try:
            return next(task for task in tasks if task.id == task_id)
        except StopIteration as exc:
            raise KeyError(f"Unknown task: {task_id}") from exc


def days_overdue(task: Task, today: date | None = None) -> int:
    if task.completed_at:
        return 0
    today = today or date.today()
    delta = date.fromisoformat(task.due) - today
    return delta.days if delta.days < 0 else 0


def visible_tasks(
    tasks: list[Task],
    selected: date,
    today: date | None = None,
    show_all: bool = False,
    show_open: bool = False,
) -> list[Task]:
    """Show unfinished overdue work first, followed by selected-day tasks."""
    today = today or date.today()
    if show_all:
        return sorted(
            tasks,
            key=lambda task: (
                task.due, task.completed_at is not None, task.position, task.created_at
            ),
        )
    if show_open:
        return sorted(
            (task for task in tasks if not task.completed_at),
            key=lambda task: (task.due, task.position, task.created_at),
        )
    overdue = [
        task for task in tasks
        if not task.completed_at and date.fromisoformat(task.due) < today and task.due != selected.isoformat()
    ]
    selected_tasks = [task for task in tasks if task.due == selected.isoformat()]
    overdue.sort(key=lambda task: (task.due, task.position, task.created_at))
    selected_tasks.sort(
        key=lambda task: (task.completed_at is not None, task.position, task.created_at)
    )
    return overdue + selected_tasks


def tree_rows(
    all_tasks: list[Task],
    visible: list[Task],
    collapsed: set[str] | None = None,
    include_hidden_ancestors: bool = True,
) -> list[TaskRow]:
    """Arrange visible tasks as a tree, retaining ancestors needed for context."""
    collapsed = collapsed or set()
    by_id = {task.id: task for task in all_tasks}
    included = {task.id for task in visible}
    if include_hidden_ancestors:
        for task in list(visible):
            parent_id = task.parent_id
            seen = {task.id}
            while parent_id and parent_id in by_id and parent_id not in seen:
                included.add(parent_id)
                seen.add(parent_id)
                parent_id = by_id[parent_id].parent_id

    children: dict[str, list[Task]] = {}
    roots: list[Task] = []
    for task in all_tasks:
        if task.id not in included:
            continue
        if task.parent_id and task.parent_id in included:
            children.setdefault(task.parent_id, []).append(task)
        else:
            roots.append(task)

    def sort_key(task: Task) -> tuple:
        return task.due, task.completed_at is not None, task.position, task.created_at

    roots.sort(key=sort_key)
    for child_list in children.values():
        child_list.sort(key=sort_key)

    rows: list[TaskRow] = []
    visited: set[str] = set()

    def visit(task: Task, prefix: str = "", connector: str = "") -> None:
        if task.id in visited:
            return
        visited.add(task.id)
        child_list = children.get(task.id, [])
        rows.append(TaskRow(task, prefix + connector, bool(child_list)))
        if task.id in collapsed:
            return
        if not connector:
            child_prefix = ""
        else:
            child_prefix = prefix + ("   " if connector == "└─ " else "│  ")
        for index, child in enumerate(child_list):
            connector = "└─ " if index == len(child_list) - 1 else "├─ "
            visit(child, child_prefix, connector)

    for root in roots:
        visit(root)
    return rows


class TodoUI:
    def __init__(self, screen, store: TaskStore):
        self.screen = screen
        self.store = store
        self.selected_day = date.today()
        self.cursor = 0
        self.scroll = 0
        self.tasks: list[Task] = []
        self.rows: list[TaskRow] = []
        self.status = ""
        self.pending_d = False
        self.pending_g = False
        self.view_mode = "daily"
        self.collapsed: set[str] = set()
        self.running = True
        self.colors = {}

    def run(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        self._init_colors()
        self.reload()
        while self.running:
            self.draw()
            try:
                key = self.screen.get_wch()
                self.handle_key(key)
            except curses.error:
                pass

    def _init_colors(self) -> None:
        if not curses.has_colors():
            self.colors = {
                "green": curses.A_BOLD,
                "orange": curses.A_BOLD,
                "red": curses.A_BOLD,
                "cyan": curses.A_BOLD,
                "dim": curses.A_DIM,
            }
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)
        curses.init_pair(4, curses.COLOR_WHITE, -1)
        orange = 208 if curses.COLORS > 208 else curses.COLOR_YELLOW
        curses.init_pair(5, orange, -1)
        self.colors = {
            "green": curses.color_pair(1) | curses.A_BOLD,
            "red": curses.color_pair(2) | curses.A_BOLD,
            "cyan": curses.color_pair(3) | curses.A_BOLD,
            "dim": curses.color_pair(4) | curses.A_DIM,
            "orange": curses.color_pair(5) | curses.A_BOLD,
        }

    def reload(self, keep_id: str | None = None) -> None:
        all_tasks = self.store.load()
        visible = visible_tasks(
            all_tasks,
            self.selected_day,
            show_all=self.view_mode == "all",
            show_open=self.view_mode == "open",
        )
        self.rows = tree_rows(
            all_tasks,
            visible,
            self.collapsed,
            include_hidden_ancestors=self.view_mode != "open",
        )
        self.tasks = [row.task for row in self.rows]
        if keep_id:
            self.cursor = next((i for i, task in enumerate(self.tasks) if task.id == keep_id), self.cursor)
        self.cursor = max(0, min(self.cursor, max(0, len(self.tasks) - 1)))

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        labels = {
            "all": "ALL TASKS",
            "open": "OPEN TASKS",
            "daily": self.selected_day.strftime("%A, %d %B %Y"),
        }
        selected_label = labels[self.view_mode]
        today_mark = (
            "  TODAY"
            if self.view_mode == "daily" and self.selected_day == date.today()
            else ""
        )
        self._put(0, 0, f" vitodo  {selected_label}{today_mark}", self.colors["cyan"] | curses.A_REVERSE)
        self._fill_line(0, width, self.colors["cyan"] | curses.A_REVERSE)

        list_height = max(1, height - 4)
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + list_height:
            self.scroll = self.cursor - list_height + 1

        if not self.tasks:
            self._put(2, 2, "No tasks for this day. Press a to add one.", self.colors["dim"])
        for screen_row, item in enumerate(self.rows[self.scroll:self.scroll + list_height], start=1):
            index = self.scroll + screen_row - 1
            task = item.task
            due = date.fromisoformat(task.due)
            overdue = days_overdue(task)
            marker = "✓" if task.completed_at else "○"
            day_label = due.strftime("%d %b")
            overdue_label = f" {overdue:>3}d" if overdue else "     "
            fold = (
                "▸ " if item.has_children and task.id in self.collapsed
                else "▾ " if item.has_children
                else "  "
            )
            repeat = {"daily": "↻D", "weekly": "↻W"}.get(task.recurrence, "  ")
            text = (
                f" {task.id[:6]}  {marker} {day_label}{overdue_label} {repeat} "
                f" {item.prefix}{fold}{task.title}"
            )
            attr = (
                self.colors["green"] if task.completed_at
                else self.colors["orange"] if overdue == -1
                else self.colors["red"] if overdue < -1
                else 0
            )
            if index == self.cursor:
                attr |= curses.A_REVERSE
            self._put(screen_row, 0, text[:max(0, width - 1)], attr)
            if index == self.cursor:
                self._fill_line(screen_row, width, attr)

        help_text = " j/k select  J/K reorder  m nest  p +1d  r repeat  x done  o open  c all  ? help  q quit "
        self._put(max(0, height - 2), 0, help_text[:max(0, width - 1)], curses.A_REVERSE)
        self._fill_line(max(0, height - 2), width, curses.A_REVERSE)
        status = self.status or f"{len(self.tasks)} visible  •  data: {self.store.root}"
        self._put(max(0, height - 1), 0, status[:max(0, width - 1)], self.colors["dim"])
        self.screen.refresh()

    def handle_key(self, key) -> None:
        self.status = ""
        if self.pending_g:
            self.pending_g = False
            if key == "g":
                self.cursor = 0
                return
            if key == "d":
                value = self.prompt("Go to date (YYYY-MM-DD): ", self.selected_day.isoformat())
                if value:
                    self.set_day(value)
                return
        if self.pending_d:
            self.pending_d = False
            if key == "d":
                self.delete_current()
                return
        if key in ("q",):
            self.running = False
        elif key in ("j", curses.KEY_DOWN):
            self.cursor = min(max(0, len(self.tasks) - 1), self.cursor + 1)
        elif key in ("k", curses.KEY_UP):
            self.cursor = max(0, self.cursor - 1)
        elif key == "J":
            self.move_current(1)
        elif key == "K":
            self.move_current(-1)
        elif key == "g":
            self.pending_g = True
            self.status = "g…  press g for first task or d to go to a date"
        elif key == "G":
            self.cursor = max(0, len(self.tasks) - 1)
        elif key == "0":
            self.cursor = 0
        elif key in ("h", curses.KEY_LEFT):
            self.change_day(-1)
        elif key in ("l", curses.KEY_RIGHT):
            self.change_day(1)
        elif key == "t":
            self.selected_day = date.today()
            self.cursor = 0
            self.reload()
        elif key == "a":
            self.add_task()
        elif key == "s":
            self.add_subtask()
        elif key == "e":
            self.edit_task()
        elif key == "m":
            self.reparent_current()
        elif key == "p":
            self.postpone_current()
        elif key == "r":
            self.set_recurrence()
        elif key in ("x", " "):
            self.toggle_current()
        elif key == "c":
            self.switch_view("all")
        elif key == "o":
            self.switch_view("open")
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.toggle_collapse()
        elif key == "d":
            self.pending_d = True
            self.status = "d…  press d again to delete"
        elif key == "?":
            self.help()
        elif key == curses.KEY_RESIZE:
            return

    def current(self) -> Task | None:
        return self.tasks[self.cursor] if self.tasks else None

    def switch_view(self, mode: str) -> None:
        self.view_mode = "daily" if self.view_mode == mode else mode
        self.cursor = self.scroll = 0
        self.reload()
        self.status = {
            "daily": "Daily view",
            "all": "All tasks",
            "open": "Open tasks",
        }[self.view_mode]

    def change_day(self, amount: int) -> None:
        self.selected_day += timedelta(days=amount)
        self.cursor = self.scroll = 0
        self.reload()

    def set_day(self, value: str) -> None:
        try:
            self.selected_day = date.fromisoformat(value.strip())
            self.cursor = self.scroll = 0
            self.reload()
        except ValueError:
            self.status = "Invalid date; use YYYY-MM-DD"

    def add_task(self) -> None:
        title = self.prompt(f"Add for {self.selected_day.isoformat()}: ")
        if title and title.strip():
            task = self.store.add(title, self.selected_day)
            self.reload(task.id)
            self.status = "Task added and committed"

    def add_subtask(self) -> None:
        parent = self.current()
        if not parent:
            self.status = "Select a parent task first"
            return
        title = self.prompt(f"Subtask of ‘{parent.title}’: ")
        if title and title.strip():
            task = self.store.add(
                title, date.fromisoformat(parent.due), parent_id=parent.id
            )
            self.collapsed.discard(parent.id)
            self.reload(task.id)
            self.status = "Subtask added and committed"

    def toggle_collapse(self) -> None:
        task = self.current()
        if not task:
            return
        row = self.rows[self.cursor]
        if not row.has_children:
            self.status = "This task has no subtasks"
            return
        if task.id in self.collapsed:
            self.collapsed.remove(task.id)
            self.status = "Subtasks expanded"
        else:
            self.collapsed.add(task.id)
            self.status = "Subtasks collapsed"
        self.reload(task.id)

    def edit_task(self) -> None:
        task = self.current()
        if not task:
            return
        title = self.prompt("Edit title: ", task.title)
        if title is None or not title.strip():
            return
        due_value = self.prompt("Due date (YYYY-MM-DD): ", task.due)
        if due_value is None:
            return
        try:
            due = date.fromisoformat(due_value.strip())
        except ValueError:
            self.status = "Invalid date; edit cancelled"
            return
        adjusted = self.store.update(task.id, title=title, due=due)
        self.reload(task.id)
        self.status = "Task edited and committed"
        if adjusted:
            self.status += f"; {adjusted} subtask due date(s) updated"

    def toggle_current(self) -> None:
        task = self.current()
        if task:
            next_task = self.store.toggle(task.id)
            self.reload(task.id)
            self.status = "Task status committed"
            if next_task:
                self.status += f"; next occurrence: {next_task.due}"

    def reparent_current(self) -> None:
        task = self.current()
        if not task:
            return
        value = self.prompt("Move under task ID (blank = root): ")
        if value is None:
            return
        try:
            parent_id = self.store.resolve_id(value).id if value.strip() else None
            adjusted = self.store.reparent(task.id, parent_id)
        except (KeyError, ValueError) as exc:
            self.status = str(exc).strip("'")
            return
        if parent_id:
            self.collapsed.discard(parent_id)
        self.reload(task.id)
        self.status = "Task moved and committed"
        if adjusted:
            self.status += f"; {adjusted} due date(s) aligned"

    def postpone_current(self) -> None:
        task = self.current()
        if not task:
            return
        adjusted = self.store.update(
            task.id, due=date.fromisoformat(task.due) + timedelta(days=1)
        )
        self.reload(task.id)
        self.status = "Task postponed by one day and committed"
        if adjusted:
            self.status += f"; {adjusted} subtask due date(s) updated"

    def set_recurrence(self) -> None:
        task = self.current()
        if not task:
            return
        current = task.recurrence or "off"
        value = self.prompt("Repeat [daily/weekly/off]: ", current)
        if value is None:
            return
        recurrence = value.strip().lower()
        if recurrence not in ("daily", "weekly", "off"):
            self.status = "Use daily, weekly, or off"
            return
        self.store.update(
            task.id,
            recurrence=None if recurrence == "off" else recurrence,
            update_recurrence=True,
        )
        self.reload(task.id)
        self.status = f"Recurrence set to {recurrence} and committed"

    def move_current(self, direction: int) -> None:
        task = self.current()
        if not task:
            return
        if self.store.move(task.id, direction):
            self.reload(task.id)
            self.status = "Task moved and committed"
        else:
            edge = "first" if direction < 0 else "last"
            self.status = f"Task is already {edge} in its group"

    def delete_current(self) -> None:
        task = self.current()
        if not task:
            return
        suffix = " and all its subtasks" if self.rows[self.cursor].has_children else ""
        answer = self.prompt(f"Delete ‘{task.title}’{suffix}? [y/N] ")
        if answer and answer.lower() == "y":
            self.store.delete(task.id)
            self.reload()
            self.status = "Task deleted and committed"

    def help(self) -> None:
        lines = [
            "VITODO KEYS", "", "j/k or arrows   select", "J/K             move task down/up",
            "h/l or arrows   previous/next day",
            "gg / G          first/last task", "a               add to selected day",
            "s               add subtask to selected task", "m               move under task ID (blank = root)",
            "p               postpone selected task by one day",
            "r               set daily/weekly recurrence", "Enter           collapse/expand subtasks",
            "e               edit title and due date", "x or Space      toggle completed",
            "o               toggle open tasks across all dates",
            "c               toggle all tasks (including completed)",
            "dd              delete (with confirmation)", "gd              go to YYYY-MM-DD",
            "t               jump to today", "q               quit", "", "Press any key to return",
        ]
        self.screen.erase()
        for row, line in enumerate(lines):
            self._put(row + 1, 2, line, self.colors["cyan"] if row == 0 else 0)
        self.screen.refresh()
        self.screen.get_wch()

    def prompt(self, label: str, initial: str = "") -> str | None:
        height, width = self.screen.getmaxyx()
        row = max(0, height - 1)
        value = list(initial)
        pos = len(value)
        curses.curs_set(1)
        while True:
            shown = label + "".join(value)
            self.screen.move(row, 0)
            self.screen.clrtoeol()
            self._put(row, 0, shown[-max(1, width - 1):])
            cursor_x = min(width - 1, len(label) + pos)
            self.screen.move(row, max(0, cursor_x))
            self.screen.refresh()
            key = self.screen.get_wch()
            if key in ("\n", "\r", curses.KEY_ENTER):
                curses.curs_set(0)
                return "".join(value)
            if key == "\x1b":
                curses.curs_set(0)
                return None
            if key in (curses.KEY_BACKSPACE, "\b", "\x7f") and pos > 0:
                del value[pos - 1]
                pos -= 1
            elif key == curses.KEY_DC and pos < len(value):
                del value[pos]
            elif key == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif key == curses.KEY_RIGHT:
                pos = min(len(value), pos + 1)
            elif key == curses.KEY_HOME:
                pos = 0
            elif key == curses.KEY_END:
                pos = len(value)
            elif isinstance(key, str) and key.isprintable():
                value.insert(pos, key)
                pos += 1

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and x < width:
            try:
                self.screen.addnstr(y, x, text, max(0, width - x - 1), attr)
            except curses.error:
                pass

    def _fill_line(self, y: int, width: int, attr: int) -> None:
        try:
            current_x = self.screen.getyx()[1]
            if current_x < width - 1:
                self.screen.addnstr(y, current_x, " " * (width - current_x - 1), width - current_x - 1, attr)
        except curses.error:
            pass


def run_tui(store: TaskStore) -> None:
    curses.wrapper(lambda screen: TodoUI(screen, store).run())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vim-style, Git-backed terminal todo list")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="task store directory")
    parser.add_argument("--history", action="store_true", help="show the task store Git history")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = TaskStore(args.data_dir.expanduser())
    try:
        store.initialize()
        if args.history:
            return subprocess.run(["git", "log", "--oneline", "--decorate"], cwd=store.root).returncode
        run_tui(store)
    except RuntimeError as exc:
        print(f"vitodo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
