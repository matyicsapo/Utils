#!/usr/bin/env python3
"""
p4_changelist_diff.py

Compare the fileset of two Perforce changelists (or the shelved vs. opened
filesets of a single changelist) using Araxis Merge's folder-diff mode.

Usage:
    python p4_changelist_diff.py <changelist1> [changelist2] [--araxis PATH] [--keep]

Two-changelist mode (both arguments given):
    For each changelist, the script inspects it via `p4 fstat` and classifies
    it as having "opened" (checked-out) files, "shelved" files, or both.

    Validation:
        - At least one of the two changelists must have shelved files.
        - Each changelist must have opened files, shelved files, or both.
          (i.e. neither changelist may be completely empty)

        Extraction:
                - A changelist that has opened (checked-out) files is materialized as
                    direct hardlinks to the local workspace when possible, so the diff
                    points at the persistent checked-out files instead of a throwaway
                    copy.
                - A changelist that has ONLY shelved files (no opened files) has its
                    shelved file revisions downloaded via `p4 print ... file@=change`
                    into a temporary folder and marked read-only.

        (If a changelist happens to have both, the opened/local copy is
        preferred, since it reflects the current, possibly-newer, working
        state.)

Single-changelist mode (only changelist1 given):
    The changelist must have BOTH opened and shelved files. Its shelved
    fileset and its opened fileset are compared against each other (e.g. to
    see what changed between what was last shelved and the current local,
    checked-out edits). The same extraction rules as above apply to each
    side (shelved files downloaded via `p4 print file@=change`, opened files
    linked directly from the local workspace when possible).

Relative paths of files inside each temporary folder are computed by
stripping the longest common depot-path prefix shared across all files
involved, so that the two folders line up file-for-file for a meaningful
Araxis Merge folder comparison.

Finally, Araxis Merge's Compare.exe is launched in two-way folder
comparison mode (/a2) against the two temporary folders, and the script
waits for it to close before optionally cleaning up the temporary folders.

If a file is touched on only one side (one changelist / one of
shelved-vs-opened), but the file already exists in the depot independently
of that change (i.e. it was edited/deleted/integrated rather than newly
added/branched), the script also fetches the depot #head revision of that
file into the *other* side's temp folder. This way Araxis Merge shows a
real file-vs-file diff for that path instead of treating it as a one-sided
add/delete purely because only one side happened to touch it.
"""

import argparse
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import stat


DELETE_ACTIONS = ("delete", "move/delete")

# Actions for which no prior depot revision of the file exists (so there is
# nothing meaningful to fetch as a "baseline" copy for the other side).
ADD_LIKE_ACTIONS = ("add", "branch", "move/add")

# Candidate locations to look for Araxis Merge's command line compare tool.
DEFAULT_ARAXIS_CANDIDATES = [
    r"C:\Program Files\Araxis\Araxis Merge\Compare.exe",
    r"C:\Program Files (x86)\Araxis\Araxis Merge\Compare.exe",
]


def run_p4_json(args):
    """Run a p4 command with -Mj -ztag and return a list of parsed JSON records.

    p4's -Mj output is a sequence of concatenated JSON objects (not
    necessarily newline-delimited), so we use json.JSONDecoder.raw_decode
    to pull them out one at a time.
    """
    cmd = ["p4", "-Mj", "-ztag"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout

    decoder = json.JSONDecoder()
    records = []
    idx = 0
    length = len(output)
    while idx < length:
        while idx < length and output[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break
        obj, end = decoder.raw_decode(output, idx)
        records.append(obj)
        idx = end
    return records


def is_empty_result(records):
    """p4 reports an empty match set as a single info/error record rather
    than an empty list. Detect that case."""
    if not records:
        return True
    if len(records) == 1:
        rec = records[0]
        if "data" in rec and "depotFile" not in rec:
            return True
    return False


def check_for_real_errors(records, context):
    for rec in records:
        if "data" in rec and "depotFile" not in rec:
            severity = rec.get("severity", 0)
            # severity 2 == warning/"no such files", not a hard failure.
            if severity is not None and severity > 2:
                raise RuntimeError(f"p4 error while {context}: {rec['data'].strip()}")


def get_files(changelist, mode):
    """mode is 'opened' or 'shelved'. Returns a list of fstat records."""
    flag = "-Ro" if mode == "opened" else "-Rs"
    records = run_p4_json(["fstat", "-e", str(changelist), flag, "-Op", "//..."])
    check_for_real_errors(records, f"listing {mode} files for changelist {changelist}")
    if is_empty_result(records):
        return []
    return [r for r in records if "depotFile" in r]


class ChangelistInfo:
    def __init__(self, changelist):
        self.changelist = changelist
        self.opened = get_files(changelist, "opened")
        self.shelved = get_files(changelist, "shelved")

    @property
    def has_opened(self):
        return len(self.opened) > 0

    @property
    def has_shelved(self):
        return len(self.shelved) > 0

    def extraction_plan(self):
        """Returns (mode, file_list) describing how to materialize this
        changelist's files on disk."""
        if self.has_opened:
            return "opened", self.opened
        if self.has_shelved:
            return "shelved", self.shelved
        return None, []


class Side:
    """One folder's worth of material to diff: a label (for the temp dir
    prefix/log messages), an extraction mode ('opened' or 'shelved'), the
    fstat entries to materialize, and the changelist they came from (needed
    for `p4 print file@=change` when mode is 'shelved')."""

    def __init__(self, label, mode, entries, changelist):
        self.label = label
        self.mode = mode
        self.entries = entries
        self.changelist = changelist


def compute_common_prefix(all_depot_files):
    if not all_depot_files:
        return ""
    dirs = [posixpath.dirname(f) + "/" for f in all_depot_files]
    prefix = posixpath.commonprefix(dirs)
    # Trim back to the last complete path segment.
    if not prefix.endswith("/"):
        prefix = prefix[: prefix.rfind("/") + 1]
    return prefix


def relative_path_for(depot_file, common_prefix):
    rel = depot_file[len(common_prefix):] if depot_file.startswith(common_prefix) else depot_file.lstrip("/")
    return rel.replace("/", os.sep)


def make_read_only(path):
    os.chmod(path, stat.S_IREAD)


def copy_file_read_only(src_path, dest_path):
    shutil.copy2(src_path, dest_path)
    make_read_only(dest_path)


def materialize_opened(entries, common_prefix, dest_root):
    for entry in entries:
        action = entry.get("action", "")
        depot_file = entry["depotFile"]
        rel = relative_path_for(depot_file, common_prefix)
        dest_path = os.path.join(dest_root, rel)

        if action in DELETE_ACTIONS:
            continue

        src_path = entry.get("path")
        if not src_path or not os.path.exists(src_path):
            print(f"  warning: local file missing on disk, skipping: {depot_file}", file=sys.stderr)
            continue

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        try:
            os.link(src_path, dest_path)
        except OSError:
            print(f"  warning: could not hardlink checked-out file, copying instead: {depot_file}", file=sys.stderr)
            copy_file_read_only(src_path, dest_path)


def materialize_shelved(entries, common_prefix, dest_root, changelist):
    for entry in entries:
        action = entry.get("action", "")
        depot_file = entry["depotFile"]
        rel = relative_path_for(depot_file, common_prefix)
        dest_path = os.path.join(dest_root, rel)

        if action in DELETE_ACTIONS:
            continue

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        spec = f"{depot_file}@={changelist}"
        cmd = ["p4", "print", "-q", "-o", dest_path, spec]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(dest_path):
            print(f"  warning: failed to download shelved file {spec}: {result.stderr.strip()}", file=sys.stderr)
        else:
            make_read_only(dest_path)


def materialize(mode, entries, changelist, common_prefix, dest_root):
    if mode == "opened":
        materialize_opened(entries, common_prefix, dest_root)
    elif mode == "shelved":
        materialize_shelved(entries, common_prefix, dest_root, changelist)


def entry_rel_map(entries, common_prefix):
    """Build a dict of relative path -> fstat entry for a changelist's files."""
    return {relative_path_for(e["depotFile"], common_prefix): e for e in entries}


def fetch_baseline_copies(missing_entries, dest_root):
    """Download the depot #head revision of files that were NOT touched by
    this changelist but were touched by the other one, so Araxis Merge shows
    a real diff instead of a pure one-sided add/delete for those paths.

    Files whose only existence is due to an add/branch/move-add action in
    the *other* changelist are skipped, since there is no independent prior
    depot revision to compare against.
    """
    for rel, entry in missing_entries.items():
        if entry.get("action") in ADD_LIKE_ACTIONS:
            continue

        depot_file = entry["depotFile"]
        dest_path = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        cmd = ["p4", "print", "-q", "-o", dest_path, depot_file]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(dest_path):
            print(f"  warning: failed to download baseline copy of {depot_file}: {result.stderr.strip()}", file=sys.stderr)
        else:
            make_read_only(dest_path)
            print(f"  baseline copy fetched for untouched file: {rel}")


def choose_temp_dir_base(entries):
    for entry in entries:
        src_path = entry.get("path")
        if src_path:
            drive_root = os.path.splitdrive(src_path)[0]
            if drive_root:
                return drive_root + os.sep
    return None


def _rmtree_onerror(func, path, _exc_info):
    """Retry failed removals after making the path writable.

    On Windows, read-only files can cause shutil.rmtree to fail.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def remove_tree(path):
    """Best-effort recursive delete with read-only handling."""
    if not path or not os.path.exists(path):
        return
    try:
        shutil.rmtree(path, onerror=_rmtree_onerror)
    except OSError as exc:
        print(f"  warning: failed to remove temporary folder {path}: {exc}", file=sys.stderr)


def find_araxis(explicit_path):
    if explicit_path:
        if os.path.isfile(explicit_path):
            return explicit_path
        raise RuntimeError(f"Araxis Merge executable not found at specified path: {explicit_path}")

    env_path = os.environ.get("ARAXIS_COMPARE_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    for candidate in DEFAULT_ARAXIS_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate

    raise RuntimeError(
        "Could not locate Araxis Merge's Compare.exe. Pass --araxis <path> "
        "or set the ARAXIS_COMPARE_PATH environment variable."
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Diff two P4 changelists (or the shelved vs. opened filesets of a single "
            "changelist) using Araxis Merge folder compare."
        )
    )
    parser.add_argument("changelist1", help="First P4 changelist number")
    parser.add_argument(
        "changelist2",
        nargs="?",
        default=None,
        help="Second P4 changelist number. If omitted, changelist1 must have both "
        "opened and shelved files, and those two filesets are compared against each other.",
    )
    parser.add_argument("--araxis", help="Path to Araxis Merge's Compare.exe", default=None)
    parser.add_argument("--keep", action="store_true", help="Keep the temporary folders after Araxis Merge closes")
    parser.add_argument(
        "--unordered",
        action="store_true",
        help="Ignore the order the two changelists were given and always compare the "
        "lower changelist number first against the higher one. Ignored in single-changelist mode.",
    )
    args = parser.parse_args()

    if args.unordered and args.changelist2 is not None:
        try:
            if int(args.changelist1) > int(args.changelist2):
                args.changelist1, args.changelist2 = args.changelist2, args.changelist1
        except ValueError:
            sys.exit("Error: --unordered requires both changelist arguments to be numeric.")

    if args.changelist2 is None:
        print(f"Inspecting changelist {args.changelist1} (single-changelist mode)...")
        cl1 = ChangelistInfo(args.changelist1)
        print(f"  opened files: {len(cl1.opened)}, shelved files: {len(cl1.shelved)}")

        if not (cl1.has_opened and cl1.has_shelved):
            sys.exit(
                f"Error: single-changelist mode requires changelist {args.changelist1} to have "
                f"BOTH opened and shelved files (opened: {len(cl1.opened)}, shelved: {len(cl1.shelved)})."
            )

        side1 = Side(f"{args.changelist1}_shelved", "shelved", cl1.shelved, cl1.changelist)
        side2 = Side(f"{args.changelist1}_opened", "opened", cl1.opened, cl1.changelist)
    else:
        print(f"Inspecting changelist {args.changelist1}...")
        cl1 = ChangelistInfo(args.changelist1)
        print(f"  opened files: {len(cl1.opened)}, shelved files: {len(cl1.shelved)}")

        print(f"Inspecting changelist {args.changelist2}...")
        cl2 = ChangelistInfo(args.changelist2)
        print(f"  opened files: {len(cl2.opened)}, shelved files: {len(cl2.shelved)}")

        if not (cl1.has_shelved or cl2.has_shelved):
            sys.exit(
                f"Error: at least one of the two changelists must have shelved files "
                f"(neither {args.changelist1} nor {args.changelist2} does)."
            )

        for info in (cl1, cl2):
            if not info.has_opened and not info.has_shelved:
                sys.exit(f"Error: changelist {info.changelist} has neither opened nor shelved files.")

        mode1, entries1 = cl1.extraction_plan()
        mode2, entries2 = cl2.extraction_plan()
        side1 = Side(str(args.changelist1), mode1, entries1, cl1.changelist)
        side2 = Side(str(args.changelist2), mode2, entries2, cl2.changelist)

    all_depot_files = [e["depotFile"] for e in (side1.entries + side2.entries)]
    common_prefix = compute_common_prefix(all_depot_files)
    print(f"Common depot path prefix: {common_prefix}")

    araxis_exe = find_araxis(args.araxis)
    print(f"Using Araxis Merge: {araxis_exe}")

    temp_dir1 = None
    temp_dir2 = None
    try:
        temp_base1 = choose_temp_dir_base(side1.entries)
        temp_base2 = choose_temp_dir_base(side2.entries)
        temp_dir1 = tempfile.mkdtemp(prefix=f"p4_changelist_diff_{side1.label}_", dir=temp_base1)
        temp_dir2 = tempfile.mkdtemp(prefix=f"p4_changelist_diff_{side2.label}_", dir=temp_base2)

        print(f"Side '{side1.label}': using '{side1.mode}' files ({len(side1.entries)}) -> {temp_dir1}")
        materialize(side1.mode, side1.entries, side1.changelist, common_prefix, temp_dir1)

        print(f"Side '{side2.label}': using '{side2.mode}' files ({len(side2.entries)}) -> {temp_dir2}")
        materialize(side2.mode, side2.entries, side2.changelist, common_prefix, temp_dir2)

        map1 = entry_rel_map(side1.entries, common_prefix)
        map2 = entry_rel_map(side2.entries, common_prefix)

        missing_in_2 = {rel: e for rel, e in map1.items() if rel not in map2}
        missing_in_1 = {rel: e for rel, e in map2.items() if rel not in map1}

        if missing_in_2:
            print(f"Fetching baseline copies into {temp_dir2} for files only touched by side '{side1.label}'...")
            fetch_baseline_copies(missing_in_2, temp_dir2)
        if missing_in_1:
            print(f"Fetching baseline copies into {temp_dir1} for files only touched by side '{side2.label}'...")
            fetch_baseline_copies(missing_in_1, temp_dir1)

        print("Launching Araxis Merge folder comparison...")
        subprocess.run([araxis_exe, "/wait", "/a2", temp_dir1, temp_dir2])
    finally:
        if args.keep:
            kept = [d for d in (temp_dir1, temp_dir2) if d]
            if kept:
                print("Temporary folders kept:")
                for d in kept:
                    print(f"  {d}")
        else:
            remove_tree(temp_dir1)
            remove_tree(temp_dir2)


if __name__ == "__main__":
    main()
