"""
audit.py — check nothing sensitive is about to be published.

    py audit.py

Run before every push while this repo is public. It is cheap, and the failure
it prevents is not recoverable: a key or a client name in a public commit stays
in the history even after the file is deleted.
"""

from __future__ import annotations

import glob
import io
import os
import re
import sys

SECRET = re.compile(
    r"sk-(?:proj|ant)-[A-Za-z0-9_-]{20,}"
    r"|pplx-[A-Za-z0-9]{20,}"
    r"|AIzaSy[A-Za-z0-9_-]{20,}"
    r"|re_[A-Za-z0-9]{20,}"
    r"|serpapi[^\n]{0,20}[A-Za-z0-9]{40,}")

# The client denylist lives in an untracked file, not here. Written inline it
# would publish the very names it exists to keep out of a public repo - the
# tool becomes the leak. audit.py is safe to publish; .audit-denylist is not.
DENYLIST_FILE = ".audit-denylist"


def _client_pattern() -> re.Pattern | None:
    if not os.path.exists(DENYLIST_FILE):
        print(f"  WARNING: no {DENYLIST_FILE} - client names are NOT being checked")
        return None
    names = [ln.strip() for ln in io.open(DENYLIST_FILE, encoding="utf-8")
             if ln.strip() and not ln.startswith("#")]
    if not names:
        return None
    return re.compile("|".join(re.escape(n) for n in names), re.I)


LOCAL = re.compile(r"[Cc]:[\\/]+Users[\\/]+\w+")

SKIP_DIRS = ("__pycache__", ".git", "data", ".venv")


def main() -> int:
    CLIENT = _client_pattern()
    issues: list[str] = []
    checked = 0
    for path in glob.glob("**/*", recursive=True):
        if not os.path.isfile(path):
            continue
        if any(d in path.replace("\\", "/").split("/") for d in SKIP_DIRS):
            continue
        if path.endswith((".png", ".jpg", ".db", ".pyc")):
            continue
        checked += 1
        text = io.open(path, encoding="utf-8", errors="replace").read()

        if SECRET.search(text):
            issues.append(f"SECRET-SHAPED STRING in {path}")
        if CLIENT:
            for m in sorted(set(CLIENT.findall(text))):
                issues.append(f"client reference '{m}' in {path}")
        for m in sorted(set(LOCAL.findall(text))):
            issues.append(f"local path '{m}' in {path}")

    # Existing locally is fine and expected; being TRACKED is the mistake, and
    # it is the one that cannot be walked back once pushed.
    try:
        import subprocess
        tracked = set(subprocess.run(["git", "ls-files"], capture_output=True,
                                     text=True, timeout=20).stdout.split())
    except Exception:
        tracked = set()
        issues.append("could not read git index - verify tracked files by hand")
    for bad in (".streamlit/secrets.toml", ".env", DENYLIST_FILE):
        if bad in tracked:
            issues.append(f"{bad} IS TRACKED BY GIT - remove it from the index")

    print(f"  {checked} files checked")
    if issues:
        for i in sorted(set(issues)):
            print(f"  FAIL  {i}")
        print(f"\n  {len(set(issues))} issue(s). Do not push.")
        return 1
    print("  clean - no secrets, no client references, no local paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
