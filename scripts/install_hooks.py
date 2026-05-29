#!/usr/bin/env python3
"""install_hooks.py - Install the git pre-commit hook for Tableau sync.

Usage:
    python scripts/install_hooks.py
"""

import os
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / ".git" / "hooks"
PRE_COMMIT_PATH = HOOKS_DIR / "pre-commit"

HOOK_CONTENT = '#!/bin/sh\npython "$(git rev-parse --show-toplevel)/scripts/tableau_sync.py" pre-commit\n'


def install() -> None:
    if not HOOKS_DIR.exists():
        print(f"Error: .git/hooks directory not found at {HOOKS_DIR}", file=sys.stderr)
        sys.exit(1)

    if PRE_COMMIT_PATH.exists():
        backup = HOOKS_DIR / "pre-commit.bak"
        print(f"Existing pre-commit hook found → backing up to {backup.name}")
        if backup.exists():
            backup.unlink()
        PRE_COMMIT_PATH.rename(backup)

    PRE_COMMIT_PATH.write_text(HOOK_CONTENT, encoding="utf-8")

    # Make executable (respected on Unix/macOS; harmless on Windows)
    current_mode = os.stat(PRE_COMMIT_PATH).st_mode
    os.chmod(
        PRE_COMMIT_PATH,
        current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    )

    print(f"Installed pre-commit hook at {PRE_COMMIT_PATH}")
    print()
    print("Next steps:")
    print("  1. Run 'python scripts/tableau_sync.py generate-diff'  (first time only)")
    print("  2. Edit Live/Customers/differences.json to customise values if needed")
    print("  3. git add / git commit as usual — the hook handles the sync automatically")


if __name__ == "__main__":
    install()
