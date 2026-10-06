#!/usr/bin/env python3
"""Package Claude Code sessions into assignment3/a3-sessions.tar.gz.

Claude Code stores one project directory per working directory under
~/.claude/projects. The directory name is the absolute path with every
non-alphanumeric character replaced by a hyphen. This script collects the
directories for this assignment and writes the archive the write-up
submission asks you to commit.

Included by default:
  - sessions started in assignment3/ or any directory under it
  - sessions started in the git repository root
  - other project folders, including ssh-* folders, whose transcript cwd
    is in one of those places

Run it from anywhere:

  python3 package_claude_sessions.py
  python3 package_claude_sessions.py --path ~/other/checkout
  python3 package_claude_sessions.py --no-repo-root
  python3 package_claude_sessions.py --dry-run
  python3 package_claude_sessions.py --path . --output claude-sessions.tar.gz
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ASSIGNMENT_DIR = (
    SCRIPT_DIR.parent if SCRIPT_DIR.name == "starter_code" else SCRIPT_DIR / "assignment3"
)
ARCHIVE_NAME = "a3-sessions.tar.gz"
# Same shapes the course page asks you to scan for before archiving.
SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}"
    r"|ghp_[A-Za-z0-9]{36}"
    r"|AKIA[0-9A-Z]{16}"
    r"|BEGIN [A-Z ]*PRIVATE KEY"
)
CHUNK = 1024 * 1024
SECRET_OVERLAP = 128


def encode_path(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def path_forms(path: Path) -> list[Path]:
    """Absolute path as given, and the symlink-resolved path."""
    absolute = Path(os.path.abspath(path.expanduser()))
    resolved = absolute.resolve()
    if resolved == absolute:
        return [absolute]
    return [absolute, resolved]


def path_slugs(path: Path) -> set[str]:
    return {encode_path(str(form)) for form in path_forms(path)}


def is_under(cwd: str, root: Path) -> bool:
    cwd_forms = path_forms(Path(cwd))
    root_forms = path_forms(root)
    for cwd_form in cwd_forms:
        for root_form in root_forms:
            try:
                cwd_form.relative_to(root_form)
                return True
            except ValueError:
                continue
    return False


def same_dir(cwd: str, root: Path) -> bool:
    return any(form == root_form for form in path_forms(Path(cwd)) for root_form in path_forms(root))


def git_root(start: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    if not text:
        return None
    root = Path(text)
    # A repo rooted at $HOME or / would sweep in unrelated sessions.
    if root.resolve() in {Path.home().resolve(), Path(root.anchor).resolve()}:
        return None
    return root


def claude_projects_dir(override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path.home() / ".claude"
    return base / "projects"


def slug_matches(name: str, roots: list[Path]) -> bool:
    for root in roots:
        for slug in path_slugs(root):
            if name == slug or name.startswith(slug + "-"):
                return True
    return False


def exact_slug_matches(name: str, roots: list[Path]) -> bool:
    slugs = set()
    for root in roots:
        slugs.update(path_slugs(root))
    return name in slugs


def project_cwd(project_dir: Path) -> str | None:
    """Return the cwd recorded in this project, from the first transcript that has one."""
    try:
        files = sorted(project_dir.glob("*.jsonl"))
    except OSError:
        return None
    for path in files:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for count, line in enumerate(handle):
                    if count >= 40 or '"cwd"' not in line:
                        if count >= 40:
                            break
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = record.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return None


def select_projects(
    projects_dir: Path,
    tree_roots: list[Path],
    exact_roots: list[Path],
) -> list[Path]:
    selected: dict[Path, Path] = {}
    entries = sorted(
        (entry for entry in projects_dir.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name,
    )
    for entry in entries:
        if slug_matches(entry.name, tree_roots) or exact_slug_matches(entry.name, exact_roots):
            selected[entry.resolve()] = entry
    for entry in entries:
        if entry.resolve() in selected:
            continue
        cwd = project_cwd(entry)
        if cwd is None:
            continue
        in_tree = any(is_under(cwd, root) for root in tree_roots)
        at_root = any(same_dir(cwd, root) for root in exact_roots)
        if in_tree or at_root:
            selected[entry.resolve()] = entry
    return [selected[key] for key in sorted(selected)]


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


def scan_secrets(project_dirs: list[Path]) -> list[Path]:
    hits: list[Path] = []
    for project_dir in project_dirs:
        for path in iter_files(project_dir):
            if not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    tail = b""
                    while True:
                        block = handle.read(CHUNK)
                        if not block:
                            break
                        if b"\0" in block:
                            break
                        data = tail + block
                        if SECRET_RE.search(data.decode("utf-8", errors="replace")):
                            hits.append(path)
                            break
                        tail = data[-SECRET_OVERLAP:]
            except OSError as exc:
                print(f"Could not read {path}: {exc}", file=sys.stderr)
                hits.append(path)
    return hits


def session_count(project_dir: Path) -> int:
    return sum(
        1
        for path in project_dir.glob("*.jsonl")
        if path.is_file() and not path.name.startswith("agent-")
    )


def dir_size(project_dir: Path) -> int:
    total = 0
    for path in iter_files(project_dir):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def write_archive(project_dirs: list[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".a3-sessions.",
        suffix=".tar.gz",
        dir=output.parent,
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with tarfile.open(tmp_path, "w:gz") as archive:
            for project_dir in project_dirs:
                # Project names start with "-". tarfile records the name as a
                # member, so the leading hyphen is not read as a flag.
                archive.add(project_dir, arcname=project_dir.name, recursive=True)
        os.replace(tmp_path, output)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Package Claude Code sessions into assignment3/a3-sessions.tar.gz."
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="DIR",
        help="another directory you ran Claude Code in; sessions there and in its subdirectories are included (repeatable)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ASSIGNMENT_DIR / ARCHIVE_NAME,
        help=f"archive path (default: assignment3/{ARCHIVE_NAME})",
    )
    parser.add_argument(
        "--projects-dir",
        help="Claude Code projects directory (default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)",
    )
    parser.add_argument(
        "--no-repo-root",
        action="store_true",
        help="skip sessions started in the git repository root",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write the archive even when the secret scan finds a match",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the directories that would be archived and scan them, without writing the archive",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    projects_dir = claude_projects_dir(args.projects_dir)
    tree_roots = [ASSIGNMENT_DIR]
    for raw in args.path:
        path = Path(raw).expanduser()
        if not path.is_dir():
            print(f"Not a directory: {path}", file=sys.stderr)
            return 1
        tree_roots.append(path.resolve())

    exact_roots: list[Path] = []
    repo = git_root(ASSIGNMENT_DIR)
    if repo is not None and not args.no_repo_root:
        exact_roots.append(repo)

    if not projects_dir.is_dir():
        print(f"Claude Code session directory not found: {projects_dir}", file=sys.stderr)
        print("Run Claude Code in this repository, then run this script again.", file=sys.stderr)
        return 1

    project_dirs = select_projects(projects_dir, tree_roots, exact_roots)
    if not project_dirs:
        print("No Claude Code sessions found for this assignment.", file=sys.stderr)
        print(f"Looked in {projects_dir} for project directories matching:", file=sys.stderr)
        for root in tree_roots:
            for slug in sorted(path_slugs(root)):
                print(f"  {slug} and its subdirectories", file=sys.stderr)
        for root in exact_roots:
            for slug in sorted(path_slugs(root)):
                print(f"  {slug} (repository root only)", file=sys.stderr)
        print(
            "Also checked other project folders, including ssh-* folders, whose cwd is in those trees.",
            file=sys.stderr,
        )
        print("Pass --path DIR for each other checkout you worked in.", file=sys.stderr)
        return 1

    print("Claude Code project directories:")
    for project_dir in project_dirs:
        label = ""
        if exact_roots and exact_slug_matches(project_dir.name, exact_roots):
            label = " (repository root; includes every session started there)"
        print(
            f"  {project_dir.name}{label}: "
            f"{session_count(project_dir)} session(s), {format_size(dir_size(project_dir))}"
        )

    hits = scan_secrets(project_dirs)
    if hits:
        print("Secret scan found matches. The archive was not written." if not args.force else "Secret scan found matches.", file=sys.stderr)
        for path in hits:
            print(f"  {path}", file=sys.stderr)
        print(
            "Remove credentials from those sessions, or re-run with --force if you have already checked them.",
            file=sys.stderr,
        )
        if not args.force:
            return 2

    output = args.output.expanduser()
    if not output.is_absolute():
        output = (Path.cwd() / output).resolve()
    if args.dry_run:
        print(f"Dry run: would write {output}")
        return 0

    write_archive(project_dirs, output)
    print(f"Wrote {output}")
    warn_if_ignored(output)
    print(f"Commit {output}. Do not gitignore it.")
    return 0


def warn_if_ignored(path: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path.parent), "check-ignore", "-q", "--", path.name],
            check=False,
            capture_output=True,
        )
    except OSError:
        return
    if result.returncode == 0:
        print(
            f"Warning: git ignores {path.name}. Include this archive in your commit.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
