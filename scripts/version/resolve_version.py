from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

def _resolve_version() -> str:
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from trip_time_service.versioning import resolve_display_version

    return resolve_display_version()


def main() -> None:
    print(_resolve_version())


if __name__ == "__main__":
    main()
