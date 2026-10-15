"""Release main to the `prod` branch: SecureGPT as the only LLM provider, nothing else that
names another provider or model (code, config, tests, docs).

    python scripts/release_prod.py                    # build, check, commit on prod (no push)
    python scripts/release_prod.py --push             # ... and push origin prod
    python scripts/release_prod.py --refresh          # rewrite scripts/prod.patch from main..prod

`prod` has its own history (no commits from main). A release:
1. checks out origin/prod in a temporary worktree and replaces its files with main's;
2. applies scripts/prod.patch (the main -> prod difference, 3-way, so unrelated changes on
   main apply cleanly) and removes this script and the patch;
3. scans for the forbidden terms below, then runs ruff, mypy and pytest on that tree;
4. commits on prod with your git identity and no co-author line.
Any failure stops with the worktree left in place to fix by hand. After fixing there and
committing, run --refresh so the next release carries the fix.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = "scripts/prod.patch"
SELF = "scripts/release_prod.py"
FORBIDDEN = re.compile(
    r"claude|anthropic|runware|deepseek|haiku|sonnet|\bopus\b|fable|gemini|grok|gpt-6|gpt@|"
    r"co-authored|luna\b",
    re.IGNORECASE,
)
VENV = ROOT / ".venv" / "bin"


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout


def refresh() -> int:
    diff = git("diff", "--full-index", "--binary", "main", "prod", "--", ".",
               f":(exclude){PATCH}", f":(exclude){SELF}")  # fmt: skip
    (ROOT / PATCH).write_text(diff)
    print(f"{PATCH}: {diff.count(chr(10) + 'diff --git ')} files differ between main and prod")
    return 0


def scan(tree: Path) -> list[str]:
    hits = []
    for name in git("ls-files", cwd=tree).splitlines():
        if FORBIDDEN.search(name):
            hits.append(f"{name}: file name")
            continue
        try:
            text = (tree / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{name}:{n}: {line.strip()[:120]}")
    return hits


def checks(tree: Path) -> bool:
    env = os.environ | {"PYTHONPATH": f"{tree / 'src'}{os.pathsep}{tree}"}
    for cmd in (
        [str(VENV / "ruff"), "check", "."],
        [str(VENV / "ruff"), "format", "--check", "."],
        [str(VENV / "mypy")],
        [str(VENV / "pytest"), "-q", "-p", "no:cacheprovider"],
    ):
        r = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True)
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
        print(f"  {Path(cmd[0]).name} {' '.join(cmd[1:2])}: {tail[0]}")
        if r.returncode:
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("-m", "--message", default=None)
    args = ap.parse_args()
    if args.refresh:
        return refresh()
    if git("status", "--porcelain").strip():
        sys.exit("commit or stash your changes first (the release takes main as committed)")
    git("fetch", "-q", "origin")
    main_sha = git("rev-parse", "--short", "main").strip()
    tree = Path(tempfile.mkdtemp(prefix="prod-release-"))
    shutil.rmtree(tree)
    git("worktree", "add", "-q", "-B", "prod", str(tree), "origin/prod")
    git("rm", "-r", "-q", ".", cwd=tree)
    git("checkout", "main", "--", ".", cwd=tree)
    r = subprocess.run(["git", "apply", "--3way", "--index", str(ROOT / PATCH)], cwd=tree,
                       capture_output=True, text=True)  # fmt: skip
    if r.returncode:
        print(r.stderr.strip())
        print(f"\nThe patch no longer applies cleanly. Fix the conflicts in {tree}, commit "
              "there, then run --refresh.")  # fmt: skip
        return 1
    for path in (PATCH, SELF):
        if (tree / path).exists():
            git("rm", "-q", "-f", path, cwd=tree)
    for name in git("grep", "-l", "release_prod", cwd=tree, check=False).splitlines():
        f = tree / name  # lines about this script (it is not on prod)
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
        keep = [ln for ln in lines if "release_prod" not in ln and "--refresh`)" not in ln]
        f.write_text("".join(keep).rstrip("\n") + "\n", encoding="utf-8")
        git("add", name, cwd=tree)
    hits = scan(tree)
    if hits:
        print("Forbidden terms on the prod tree:\n  " + "\n  ".join(hits[:40]))
        print(f"\nEdit them in {tree}, commit there, then run --refresh.")
        return 1
    print("scan: clean")
    if not checks(tree):
        print(f"\nChecks failed; the tree is in {tree}.")
        return 1
    if not git("status", "--porcelain", cwd=tree).strip():
        print(f"prod already matches main {main_sha}; nothing to release")
        git("worktree", "remove", str(tree))
        return 0
    message = args.message or f"Release from main {main_sha}"
    git("commit", "-q", "-m", message, cwd=tree)
    print(git("log", "--oneline", "-1", cwd=tree).strip())
    if args.push:
        git("push", "-q", "origin", "prod", cwd=tree)
        print("pushed origin prod")
    git("worktree", "remove", str(tree))
    return 0


if __name__ == "__main__":
    sys.exit(main())
