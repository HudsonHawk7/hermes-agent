"""Drop the checkout test-environment after a green suite (called from run_tests.sh)."""
from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    root = Path(args[0]).resolve()
    # Ensure the checkout is importable when the interpreter is a bare venv.
    sys.path.insert(0, str(root))
    from pm.testenv import drop_testenv

    drop_testenv(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
