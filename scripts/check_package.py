"""Pre-submission hygiene check for the Veyra repository.

Run as `python scripts/check_package.py` (stdlib only, no venv required).

Each check exists because of a concrete way a submission goes wrong:

* Forbidden artifacts -- a 130 MB `data/finance.db`, a `.venv/`, or a stale
  `web/runtime-config.js` committed by accident makes the package huge and,
  worse, non-reproducible: the reviewer would evaluate our leftovers instead
  of a fresh `./run.sh`. Tracked artifacts fail; present-but-git-ignored ones
  are fine (that is what `.gitignore` is for), but an artifact that is neither
  tracked nor ignored is flagged, since a naive `git add .` or `zip -r` sweeps
  it in.
* Required files -- the entry points a reviewer touches first. A missing
  LICENSE or `.env.example` blocks the "clone, run one command" story.
* Secrets -- `.env.example` is committed, so every `*API_KEY*`/`*TOKEN*`/
  `*SECRET*` line in it must be empty, and the real `.env` must never be
  tracked.
* Size report -- shows at a glance that the shippable set is source-only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

REQUIRED = [
    "app", "tests", "web", "scripts", "README.md", "LICENSE", "requirements.txt",
    "pyproject.toml", "config.py", "cli.py", "run.sh", ".env.example", ".gitignore",
]
SECRET_HINTS = ("API_KEY", "TOKEN", "SECRET")
FORBIDDEN_DIRS = {".venv", "__pycache__", ".pytest_cache"}
FORBIDDEN_FILES = {
    ".env", "data/finance.db", "data/finance.db-wal", "data/finance.db-shm",
    "web/runtime-config.js", "data/raw/generation_summary.json",
}

failures: list[str] = []


def forbidden_label(rel: str) -> str | None:
    """Return the artifact class `rel` belongs to, or None if it is shippable."""
    for part in rel.split("/"):
        if part in FORBIDDEN_DIRS:
            return part + "/"
        if part.startswith(".generation_tmp_"):
            return ".generation_tmp_*/"
    if rel.endswith(".pyc"):
        return "*.pyc"
    if rel in FORBIDDEN_FILES:
        return rel
    if rel.startswith("data/raw/") and rel.endswith(".csv"):
        return "data/raw/*.csv"
    return None


def git(*args: str) -> list[str]:
    proc = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    return proc.stdout.splitlines()


def report(ok: bool, title: str, detail: list[str]) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {title}")
    for line in detail:
        print(f"       {line}")
    if not ok:
        failures.append(title)


def scan_disk() -> dict[str, tuple[int, str]]:
    """Walk the working tree, pruning artifact dirs instead of descending them."""
    found: dict[str, tuple[int, str]] = {}
    for root, dirs, files in os.walk(REPO):
        base = Path(root).relative_to(REPO).as_posix()
        prefix = "" if base == "." else base + "/"
        for name in list(dirs):
            if name == ".git":
                dirs.remove(name)
                continue
            label = forbidden_label(prefix + name)
            if label:
                count, first = found.get(label, (0, prefix + name))
                found[label] = (count + 1, first)
                dirs.remove(name)
        for name in files:
            label = forbidden_label(prefix + name)
            if label:
                count, first = found.get(label, (0, prefix + name))
                found[label] = (count + 1, first)
    return found


_SOURCE_ROOTS = ("app", "tests", "web", "scripts", "docs")


def _shippable_source_files() -> list[str]:
    """Repo-relative source files a submission must actually contain."""
    out: list[str] = []
    for root in _SOURCE_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            rel = path.relative_to(REPO).as_posix()
            if path.is_file() and not forbidden_label(rel):
                out.append(rel)
    return out


def main() -> int:
    tracked = git("ls-files")
    if not tracked:
        report(False, "git tracked-file listing", ["`git ls-files` returned nothing -- not a git repo?"])
        return 1

    tracked_bad = sorted({forbidden_label(rel) or "" for rel in tracked if forbidden_label(rel)})
    report(not tracked_bad, "no forbidden artifacts tracked by git",
           [f"tracked artifact class: {label}" for label in tracked_bad] or
           [f"{len(tracked)} tracked files, none matching the artifact blocklist"])

    on_disk = scan_disk()
    ignored = set(git("check-ignore", *[first for _, first in on_disk.values()])) if on_disk else set()
    loose = [f"{label} ({count} path(s), e.g. {first}) is present, untracked, and NOT git-ignored"
             for label, (count, first) in sorted(on_disk.items()) if first not in ignored]
    report(not loose, "on-disk artifacts are all git-ignored", loose or
           [f"ignored (fine): {label} x{count}" for label, (count, _) in sorted(on_disk.items())] or
           ["working tree is clean of artifacts"])

    missing = [name for name in REQUIRED if not (REPO / name).exists()]
    report(not missing, "required files and directories present",
           [f"missing: {name}" for name in missing] or [f"all {len(REQUIRED)} present"])

    # Presence on disk is not shippability: a package built from git (archive/clone/`git add .`
    # on a clean checkout) contains only TRACKED files. An untracked test suite or vendored
    # asset passes every other check here and then simply is not in the submission.
    tracked_set = set(tracked)
    untracked_source = sorted(
        rel for rel in _shippable_source_files()
        if rel not in tracked_set and not git("check-ignore", rel)
    )
    report(not untracked_source, "shippable source files are tracked by git",
           [f"untracked: {rel}" for rel in untracked_source[:15]]
           + ([f"... and {len(untracked_source) - 15} more"] if len(untracked_source) > 15 else [])
           + ["-> run: git add " + " ".join(sorted({rel.split('/')[0] for rel in untracked_source}))]
           if untracked_source else ["every source file under app/ tests/ web/ scripts/ is tracked"])

    leaks: list[str] = []
    for lineno, line in enumerate((REPO / ".env.example").read_text().splitlines(), 1):
        key, sep, value = line.strip().partition("=")
        if not sep or key.startswith("#"):
            continue
        if any(hint in key.upper() for hint in SECRET_HINTS) and value.strip():
            leaks.append(f".env.example:{lineno}: {key} has a non-empty value")
    if ".env" in tracked:
        leaks.append(".env is tracked by git")
    report(not leaks, "no secrets committed", leaks or
           [".env.example secret slots are empty; .env is not tracked"])

    sizes = [(REPO / rel).stat().st_size for rel in tracked if (REPO / rel).is_file()]
    report(True, "tracked package size",
           [f"{len(sizes)} files, {sum(sizes) / 1024:.1f} KiB total",
            f"largest: {max(sizes) / 1024:.1f} KiB" if sizes else "no files"])

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed -- {'; '.join(failures)}")
        return 1
    print("PASS: repository is fit to ship.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
