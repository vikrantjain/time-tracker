#!/usr/bin/env python3
"""Time Tracker — manage the project -> customer mapping (projects.toml).

Backs the `tt map` verb. The mapping file stays hand-editable: adding a NEW
project appends a table and leaves the rest of the file byte-for-byte intact
(comments included); updating an EXISTING project rewrites only that table's
lines in place. Stdlib only.
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
    out = []
    i = 0
    while i < len(lines):
        if _header_matches(lines[i], project):
            out.append(table)
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("["):
                i += 1
        else:
            out.append(lines[i])
            i += 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    was = existing.get("customer", "?")
    label = f'"{customer}"' + (f" (name: {name})" if name else "")
    if was == customer and not name:
        return f"Already mapped: {project} → \"{customer}\" (nothing changed)."
    return f"✓ Updated {project} → {label} (was \"{was}\")"


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
    parser.add_argument("--dir", required=True, help="Store directory.")
    parser.add_argument("--project", default="", help="Project path to map (the current cwd).")
    args = parser.parse_args(argv)

    path = os.path.join(args.dir, MAPPING_FILE)
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
