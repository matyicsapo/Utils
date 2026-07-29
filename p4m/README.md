# p4m

Tiny, editor-agnostic command line helper for Perforce actions on a single file.

## Commands

- `p4m open <file>`: Open file in P4V (best effort)
- `p4m checkout <file>`: Run `p4 edit <file>`
- `p4m diff <file>`:
	- In VS Code terminal: use `p4` + `code --diff` against your `have` revision (opens editor diff tab)
	- Outside VS Code: launch `p4vc diffhave <file>` (fallback to `p4 diff`)

## Why this exists

- Works from terminal, scripts, or any editor task.
- VS Code can later map shortcuts to this tool by passing `${file}`.
- No dependency on your active workspace.

## Install (Windows)

1. Keep this repo somewhere stable, e.g. `E:\Public\Tools\p4m`.
2. Add `E:\Public\Tools\p4m` to your `PATH`.
3. Run commands via `p4m.cmd`.

Optional: copy `p4m.cmd` to a central tools folder on `PATH`.

## Install (macOS)

1. Ensure Python 3 is installed.
2. `chmod +x p4m`
3. Add repo folder to `PATH`.
4. Run `p4m <command> <file>`.

## VS Code template (shipped in repo)

This repo includes a VS Code tasks template at `templates/vscode/tasks.p4m.json`.

It defines three commands for the active editor file (`${file}`):

- `p4m: diff current file`
- `p4m: open current file`
- `p4m: checkout current file`

To use it in a workspace:

1. Create `.vscode/tasks.json` in your workspace if needed.
2. Copy the contents of `templates/vscode/tasks.p4m.json` into it.
3. Run with **Tasks: Run Task**.

You can also configure them in users tasks. (Ctrl + Shift + P for commands and type User Tasks)

If `p4m` is not on your `PATH`, set `"command"` to the full path to `p4m.cmd`.

Note: the VS Code behavior for `p4m diff` requires the `code` CLI command to be available in `PATH`.

## P4V open behavior

Default behavior tries:
1. `p4vc workspacewindow -s <file>`
2. fallback `p4v <file>`

If your P4V install needs different arguments, set environment variable `P4M_OPEN_CMD`.

Example:

```sh
export P4M_OPEN_CMD='p4v -s {file}'
```

`{file}` will be replaced with the target file path.

## Connection Context

`open` and `diff` resolve Perforce connection details in this order:

1. `P4M_PORT` / `P4M_USER` / `P4M_CLIENT` env overrides
2. `P4CONFIG` file found by walking upward from the target file folder
3. fallback to `p4 -ztag info` in the target file folder

When found, `-p`, `-u`, and `-c` are passed explicitly to P4V/P4VC.

If needed, you can force values with env vars:

- `P4M_PORT`
- `P4M_USER`
- `P4M_CLIENT`
