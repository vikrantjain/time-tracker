#!/usr/bin/env python3
"""Time Tracker — manage the project -> customer mapping (projects.toml).

Backs the `tt map` verb. The mapping file stays hand-editable: adding a NEW
project appends a table and leaves the rest of the file byte-for-byte intact
(comments included); updating or removing an EXISTING project touches only
that table's lines. Stdlib only.
"""

import argparse
import os
import sys
import tomllib

MAPPING_FILE = "projects.toml"


def toml_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def load(path):
    """Parsed mapping, {} when missing, None when malformed (refuse to edit)."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        return None


def render_table(project, customer, name):
    lines = [f'["{toml_escape(project)}"]', f'customer = "{toml_escape(customer)}"']
    if name:
        lines.append(f'name = "{toml_escape(name)}"')
    return "\n".join(lines) + "\n"


def _header_matches(line, project):
    """Does this line open the TOML table for `project`?"""
    s = line.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return False
    inner = s[1:-1].strip()
    if inner.startswith('"') and inner.endswith('"') and len(inner) >= 2:
        key = inner[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return key == project
    if inner.startswith("'") and inner.endswith("'") and len(inner) >= 2:
        return inner[1:-1] == project
    return inner == project


def _table_span(lines, project):
    """(start, end) line range of `project`'s table, or None when absent.

    The range runs from the header through the table's last key line.
    Comments and blank lines after that are left out: they usually belong to
    the next table, and a hand-written note above it must survive.
    """
    for i, line in enumerate(lines):
        if not _header_matches(line, project):
            continue
        end = i + 1
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith("["):
            s = lines[j].strip()
            if s and not s.startswith("#"):
                end = j + 1
            j += 1
        return i, end
    return None


def upsert(path, project, customer, name):
    data = load(path)
    if data is None:
        return f"tt map: {path} is malformed TOML — fix it by hand first."
    existing = data.get(project) if isinstance(data.get(project), dict) else None
    table = render_table(project, customer, name)

    if existing is None:
        text = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        if text and not text.endswith("\n"):
            text += "\n"
        if text:
            text += "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + table)
        label = f'"{customer}"' + (f" (name: {name})" if name else "")
        return f"✓ Mapped {project} → {label}\n  stored in {path}"

    # Rewrite just this project's table; every other line stays verbatim.
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    span = _table_span(lines, project)
    if span is None:
        return f"tt map: couldn't find the table for {project} in {path}. Edit it by hand."
    start, end = span
    lines[start:end] = [table]
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    was = existing.get("customer", "?")
    label = f'"{customer}"' + (f" (name: {name})" if name else "")
    if was == customer and not name:
        return f"Already mapped: {project} → \"{customer}\" (nothing changed)."
    return f"✓ Updated {project} → {label} (was \"{was}\")"


def remove(path, project):
    data = load(path)
    if data is None:
        return f"tt map: {path} is malformed TOML — fix it by hand first."
    existing = data.get(project) if isinstance(data.get(project), dict) else None
    if existing is None:
        return f"Not mapped: {project} (nothing changed). Run 'tt map' to list the mappings."

    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    span = _table_span(lines, project)
    if span is None:
        return f"tt map: couldn't find the table for {project} in {path}. Edit it by hand."
    start, end = span
    del lines[start:end]
    # The blank lines on both sides of the table would otherwise double up.
    if start > 0 and lines[start - 1].strip() == "" and (
        start == len(lines) or lines[start].strip() == ""
    ):
        del lines[start - 1]
    elif start == 0 and lines and lines[0].strip() == "":
        del lines[0]
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    was = existing.get("customer", "?")
    return (
        f"✓ Removed the mapping for {project} (was \"{was}\").\n"
        "  Its time now shows as ⚠ unmapped in reports."
    )


def render_list(path):
    data = load(path)
    if data is None:
        return f"tt map: {path} is malformed TOML — fix it by hand first."
    rows = [
        (proj, m.get("customer", "?"), m.get("name"))
        for proj, m in sorted(data.items())
        if isinstance(m, dict)
    ]
    if not rows:
        return (
            "No project → customer mappings yet.\n"
            "Run 'tt map \"<Customer>\"' inside a project to add one"
            f" (writes {path})."
        )
    lines = [f"Project → customer mappings ({path}):"]
    for proj, cust, name in rows:
        lines.append(f"  {proj} → {cust}" + (f" ({name})" if name else ""))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage the project -> customer mapping.")
    parser.add_argument("customer", nargs="?", help="Customer to map the current project to.")
    parser.add_argument("--name", help="Optional display label for the project.")
    parser.add_argument("--list", action="store_true", help="List current mappings.")
    parser.add_argument(
        "--remove",
        nargs="?",
        const="",
        default=None,
        metavar="PROJECT",
        help="Remove a mapping: the current project's, or the given absolute project path's.",
    )
    parser.add_argument("--dir", required=True, help="Store directory.")
    parser.add_argument("--project", default="", help="Project path to map (the current cwd).")
    args = parser.parse_args(argv)

    path = os.path.join(args.dir, MAPPING_FILE)
    if args.remove is not None:
        if args.customer:
            print("tt map: --remove takes a project path, not a customer: tt map --remove [<path>].")
            return 0
        # A path names a project you may no longer have a session in (a deleted repo).
        target = os.path.normpath(os.path.expanduser(args.remove)) if args.remove else args.project
        if not target:
            print("tt map: no current project. Pass its path: tt map --remove <path>.")
            return 0
        if not os.path.isabs(target):
            print(
                f"tt map: '{args.remove}' isn't an absolute path."
                " Use the project path exactly as 'tt map' lists it."
            )
            return 0
        print(remove(path, target))
        return 0
    if args.list or not args.customer:
        print(render_list(path))
        return 0
    if not args.project:
        print("tt map: no current project — run it from inside the project you want to map.")
        return 0
    print(upsert(path, args.project, args.customer, args.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
