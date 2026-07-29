#!/usr/bin/env python3
"""Small Perforce helper for per-file actions.

Commands:
- open: open the file in P4V (best-effort, configurable)
- checkout: p4 edit <file>
- diff: p4vc diffhave <file> (fallback: p4 diff)

This utility is editor-agnostic. VS Code can call it with ${file} later.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _is_windows() -> bool:
    return os.name == "nt"


def _quote_for_template(value: str) -> str:
    if _is_windows():
        # subprocess list invocation handles quoting, but this is used for env templates.
        return f'"{value}"'
    return shlex.quote(value)


def _resolve_file(path_text: str) -> Path:
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"Expected file, got directory: {path}")
    return path


def _require_executable(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f"Required executable not found in PATH: {name}")
    return exe


def _in_vscode_terminal() -> bool:
    return os.environ.get("TERM_PROGRAM", "").lower() == "vscode" or "VSCODE_PID" in os.environ


def _run(cmd: list[str], wait: bool = True, cwd: Path | None = None) -> int:
    cmd = _wrap_windows_batch_command(cmd)

    if wait:
        proc = subprocess.run(cmd, check=False, cwd=str(cwd) if cwd else None)
        return int(proc.returncode)

    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if _is_windows():
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, **kwargs)
    return 0


def _run_capture(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    cmd = _wrap_windows_batch_command(cmd)
    proc = subprocess.run(
        cmd,
        check=False,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    return int(proc.returncode), proc.stdout, proc.stderr


def _wrap_windows_batch_command(cmd: list[str]) -> list[str]:
    if _is_windows() and Path(cmd[0]).suffix.lower() in (".bat", ".cmd"):
        # Windows batch files need cmd.exe for reliable process launch.
        return ["cmd", "/c", *cmd]
    return cmd


def _build_p4vc_base_command() -> list[str] | None:
    p4vc = shutil.which("p4vc")
    if not p4vc:
        return None

    # On Windows, p4vc is often a .bat wrapper with a forced timeout and temp-file print.
    # Calling p4v directly in "-p4vc" mode is faster and avoids console countdown output.
    if _is_windows() and Path(p4vc).suffix.lower() in (".bat", ".cmd"):
        p4v = shutil.which("p4v")
        if p4v:
            return [p4v, "-p4vc"]

    return [p4vc]


def _parse_p4_ztag(output: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in output.splitlines():
        if not line.startswith("... "):
            continue
        parts = line[4:].split(" ", 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].strip(), parts[1].strip()
        if key:
            data[key] = value
    return data


def _read_p4config_vars(start_dir: Path) -> dict[str, str]:
    config_name = os.environ.get("P4CONFIG", "").strip()
    if not config_name:
        return {}

    current = start_dir.resolve()
    while True:
        cfg = current / config_name
        if cfg.exists() and cfg.is_file():
            values: dict[str, str] = {}
            for raw_line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue

                # Support both KEY=VALUE and KEY VALUE formats.
                if "=" in line:
                    key, value = line.split("=", 1)
                else:
                    parts = line.split(None, 1)
                    if len(parts) != 2:
                        continue
                    key, value = parts

                key = key.strip().upper()
                value = value.strip()
                if key in ("P4PORT", "P4USER", "P4CLIENT") and value:
                    values[key] = value
            return values

        parent = current.parent
        if parent == current:
            break
        current = parent

    return {}


def _p4_connection_args(cwd: Path) -> list[str]:
    # Explicit overrides win.
    client = os.environ.get("P4M_CLIENT", "").strip()
    user = os.environ.get("P4M_USER", "").strip()
    port = os.environ.get("P4M_PORT", "").strip()

    # Then try P4CONFIG from target folder hierarchy.
    if not (client and user and port):
        cfg = _read_p4config_vars(cwd)
        if not port:
            port = cfg.get("P4PORT", "").strip()
        if not user:
            user = cfg.get("P4USER", "").strip()
        if not client:
            client = cfg.get("P4CLIENT", "").strip()

    # Last fallback: ask p4 in that folder context.
    if not (client and user and port):
        p4 = shutil.which("p4")
        if p4:
            proc = subprocess.run(
                [p4, "-ztag", "info"],
                check=False,
                cwd=str(cwd),
                capture_output=True,
                text=True,
            )
            info = _parse_p4_ztag(proc.stdout)
            if not client:
                client = info.get("clientName", "").strip()
            if not user:
                user = info.get("userName", "").strip()
            if not port:
                port = info.get("serverAddress", "").strip()

    args: list[str] = []
    if port and port.lower() not in ("unknown", "*unknown*"):
        args.extend(["-p", port])
    if user and user.lower() not in ("unknown", "*unknown*"):
        args.extend(["-u", user])
    if client and client.lower() not in ("unknown", "*unknown*"):
        args.extend(["-c", client])
    return args


def _snapshot_visible_top_level_windows() -> set[int]:
    if not _is_windows():
        return set()

    try:
        import ctypes

        user32 = ctypes.windll.user32
        handles: set[int] = set()
        enum_cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @enum_cb_t
        def enum_windows(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindow(hwnd, 4):  # GW_OWNER
                return True
            handles.add(hwnd)
            return True

        user32.EnumWindows(enum_windows, 0)
        return handles
    except Exception:
        return set()


def _focus_new_diff_window_with_retry(existing_handles: set[int]) -> None:
    if not _is_windows():
        return

    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        SW_RESTORE = 9
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        excluded_exes = {
            "p4v.exe",
            "p4vfs.exe",
            "explorer.exe",
            "cmd.exe",
            "conhost.exe",
            "powershell.exe",
            "pwsh.exe",
            "python.exe",
            "py.exe",
        }

        def process_name_for_window(hwnd: int) -> str:
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == 0:
                return ""

            proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not proc:
                return ""
            try:
                size = ctypes.c_ulong(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size))
                if not ok:
                    return ""
                return Path(buf.value).name.lower()
            finally:
                kernel32.CloseHandle(proc)

        def activate_window(hwnd: int) -> None:
            fg = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            my_thread = kernel32.GetCurrentThreadId()
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)

            if fg_thread and fg_thread != my_thread:
                user32.AttachThreadInput(my_thread, fg_thread, True)
            if target_thread and target_thread != my_thread:
                user32.AttachThreadInput(my_thread, target_thread, True)

            try:
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.BringWindowToTop(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if target_thread and target_thread != my_thread:
                    user32.AttachThreadInput(my_thread, target_thread, False)
                if fg_thread and fg_thread != my_thread:
                    user32.AttachThreadInput(my_thread, fg_thread, False)

        def find_new_external_window() -> int | None:
            found: list[int] = []
            enum_cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            @enum_cb_t
            def enum_windows(hwnd: int, _lparam: int) -> bool:
                if hwnd in existing_handles:
                    return True
                if not user32.IsWindowVisible(hwnd):
                    return True
                if user32.GetWindow(hwnd, 4):  # GW_OWNER
                    return True
                title_len = user32.GetWindowTextLengthW(hwnd)
                if title_len <= 0:
                    return True
                exe = process_name_for_window(hwnd)
                if exe in excluded_exes:
                    return True
                found.append(hwnd)
                return True

            user32.EnumWindows(enum_windows, 0)
            return found[0] if found else None

        # Diff tools may take a moment to appear.
        for _ in range(30):
            hwnd = find_new_external_window()
            if hwnd:
                activate_window(hwnd)
                return
            time.sleep(0.1)
    except Exception:
        return


def _focus_p4v_with_retry() -> None:
    if not _is_windows():
        return

    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        SW_RESTORE = 9

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        def _is_p4v_window(hwnd: int) -> bool:
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == 0:
                return False

            proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not proc:
                return False
            try:
                size = ctypes.c_ulong(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size))
                if not ok:
                    return False
                exe_name = Path(buf.value).name.lower()
                return exe_name == "p4v.exe"
            finally:
                kernel32.CloseHandle(proc)

        def find_p4v_window() -> int | None:
            found: list[int] = []
            enum_cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            @enum_cb_t
            def enum_windows(hwnd: int, _lparam: int) -> bool:
                if not user32.IsWindowVisible(hwnd):
                    return True

                if user32.GetWindow(hwnd, 4):  # GW_OWNER
                    return True

                if _is_p4v_window(hwnd):
                    found.append(hwnd)
                return True

            user32.EnumWindows(enum_windows, 0)
            return found[0] if found else None

        def activate_window(hwnd: int) -> None:
            fg = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            my_thread = kernel32.GetCurrentThreadId()
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)

            if fg_thread and fg_thread != my_thread:
                user32.AttachThreadInput(my_thread, fg_thread, True)
            if target_thread and target_thread != my_thread:
                user32.AttachThreadInput(my_thread, target_thread, True)

            try:
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.BringWindowToTop(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if target_thread and target_thread != my_thread:
                    user32.AttachThreadInput(my_thread, target_thread, False)
                if fg_thread and fg_thread != my_thread:
                    user32.AttachThreadInput(my_thread, fg_thread, False)

        # P4V may need a moment to create/activate the target window.
        for _ in range(30):
            hwnd = find_p4v_window()
            if hwnd:
                activate_window(hwnd)
                return
            time.sleep(0.1)
    except Exception:
        # Best effort only. Opening should still succeed even if focus fails.
        return


def cmd_checkout(file_path: Path) -> int:
    p4 = _require_executable("p4")
    return _run([p4, "edit", str(file_path)], cwd=file_path.parent)


def cmd_diff(file_path: Path) -> int:
    if _in_vscode_terminal():
        return _cmd_diff_vscode(file_path)

    p4vc_base = _build_p4vc_base_command()
    if p4vc_base:
        before_handles = _snapshot_visible_top_level_windows()
        rc = _run(
            [*p4vc_base, *_p4_connection_args(file_path.parent), "diffhave", str(file_path)],
            wait=False,
            cwd=file_path.parent,
        )
        _focus_new_diff_window_with_retry(before_handles)
        return rc

    p4 = _require_executable("p4")
    return _run([p4, "diff", str(file_path)], cwd=file_path.parent)


def _cmd_diff_vscode(file_path: Path) -> int:
    p4 = _require_executable("p4")
    code = _require_executable("code")

    # Resolve depot path and have revision so we can compare "have" vs local file.
    rc, stdout, stderr = _run_capture(
        [p4, "-ztag", "fstat", str(file_path)],
        cwd=file_path.parent,
    )
    if rc != 0:
        raise RuntimeError((stderr or stdout or "p4 fstat failed").strip())

    fstat = _parse_p4_ztag(stdout)
    depot_file = fstat.get("depotFile", "").strip()
    have_rev = fstat.get("haveRev", "").strip()
    if not depot_file:
        raise RuntimeError("Could not determine depot file from p4 fstat.")
    if not have_rev or have_rev == "0":
        raise RuntimeError("File has no synced 'have' revision to diff against.")

    left_ref = f"{depot_file}#{have_rev}"
    with tempfile.NamedTemporaryFile(
        prefix=f"p4m-have-{file_path.stem}-{have_rev}-",
        suffix=file_path.suffix,
        delete=False,
    ) as tmp:
        left_path = Path(tmp.name)

    rc, stdout, stderr = _run_capture(
        [p4, "print", "-q", "-o", str(left_path), left_ref],
        cwd=file_path.parent,
    )
    if rc != 0:
        raise RuntimeError((stderr or stdout or "p4 print failed").strip())

    # Open an editor diff tab instead of printing textual diff to the terminal.
    return _run([code, "--diff", str(left_path), str(file_path)], wait=False, cwd=file_path.parent)


def _build_open_command(file_path: Path) -> list[str]:
    template = os.environ.get("P4M_OPEN_CMD", "").strip()
    if template:
        rendered = template.replace("{file}", _quote_for_template(str(file_path)))
        return shlex.split(rendered, posix=not _is_windows())

    # Best-effort defaults. Users can override with P4M_OPEN_CMD.
    p4vc_base = _build_p4vc_base_command()
    if p4vc_base:
        # Open/activate workspace window and select this file.
        return [
            *p4vc_base,
            *_p4_connection_args(file_path.parent),
            "workspacewindow",
            "-s",
            str(file_path),
        ]

    p4v = shutil.which("p4v")
    if p4v:
        # Fallback for environments without p4vc.
        return [p4v, str(file_path)]

    raise RuntimeError("Neither 'p4v' nor 'p4vc' found in PATH")


def cmd_open(file_path: Path) -> int:
    cmd = _build_open_command(file_path)
    rc = _run(cmd, wait=False, cwd=file_path.parent)
    _focus_p4v_with_retry()
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="p4m",
        description="Small Perforce helper for a specific file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("open", "Open file in P4V workspace view (best effort)."),
        ("checkout", "Run p4 edit on file."),
        ("diff", "In VS Code: open code --diff against have revision. Otherwise p4vc diffhave (fallback p4 diff)."),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("file", help="Path to file")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        file_path = _resolve_file(args.file)

        if args.command == "open":
            return cmd_open(file_path)
        if args.command == "checkout":
            return cmd_checkout(file_path)
        if args.command == "diff":
            return cmd_diff(file_path)

        parser.error(f"Unknown command: {args.command}")
        return 2
    except Exception as exc:
        print(f"p4m: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
