# vitodo

`vitodo` is a small, keyboard-first terminal todo list. It uses Vim-style keys,
shows missed work in red with a negative day count, shows completed work in
green, and automatically records every change in a local Git repository.

It is deliberately dependency-free: Python 3, `curses`, and Git are enough.

## Install

Ubuntu normally already has everything required. If needed:

```bash
sudo apt install python3 git
```

Then install the command for your user:

```bash
chmod +x install.sh
./install.sh
```

Run it from Kitty or another terminal:

```bash
vitodo
```

The task data and its Git repository live in `~/.local/share/vitodo/` by
default. Override that location with `VITODO_DATA_DIR` or `--data-dir`.

## Keys

| Key | Action |
| --- | --- |
| `j` / `k` | Select next / previous task |
| `J` / `K` | Move the selected task down / up within its group |
| `h` / `l` | Previous / next day |
| `gg` / `G` | First / last visible task |
| `a` | Add a task to the selected day |
| `s` | Add a subtask to the selected task |
| `Enter` | Collapse or expand a task's subtree |
| `e` | Edit the selected task and its due date |
| `x` or `Space` | Toggle completed |
| `c` | Toggle all tasks across all dates, including completed |
| `dd` | Delete, with confirmation |
| `gd` | Go to a date (`YYYY-MM-DD`) |
| `t` | Jump to today |
| `?` | Show help |
| `q` | Quit |

`Esc` cancels an input prompt. Arrow keys also work for navigation.

## Overdue and completed tasks

Unfinished tasks dated before today remain visible. A task at `-1d` is orange;
tasks at `-2d` and earlier are red. Completed tasks are green.
On the selected day, unfinished tasks appear before completed tasks.

Press `c` to switch to an all-task view containing open and completed items
from every date. Press `c` again to return to the daily view.

Subtasks form a tree and initially inherit their parent's due date. Select a
task and press `s` to add a child. Press `Enter` on a task with children to
collapse or expand its subtree. Deleting a parent also deletes its descendants.

Use `J` and `K` to change the persistent order of tasks. Reordering stays
within the same date, completion group, and parent, so the task tree remains
intact. Each move is automatically committed to the task-data Git repository.

## Git history

Every add, edit, completion, reopening, and deletion writes `tasks.json` and
immediately creates a Git commit. Inspect it with:

```bash
vitodo --history
```

Or directly:

```bash
git -C ~/.local/share/vitodo log --oneline
```

The repository gets its own local Git identity (`vitodo <vitodo@localhost>`),
so it does not alter your global Git configuration.

## Optional Hyprland shortcut

For Kitty, add this to `~/.config/hypr/hyprland.conf`:

```ini
bind = SUPER, T, exec, kitty --class vitodo -e vitodo
windowrulev2 = float, class:^(vitodo)$
windowrulev2 = size 900 620, class:^(vitodo)$
```

Reload Hyprland, then `Super+T` opens `vitodo` in a floating terminal.

## Development and tests

```bash
python3 -m unittest discover -s tests -v
```
