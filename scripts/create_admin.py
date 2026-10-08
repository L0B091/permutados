#!/usr/bin/env python
"""Create an admin user. Usage (from backend/): python ../scripts/create_admin.py <username> <ROLE>
The password is read interactively (never pass it on the command line)."""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.db import session_scope  # noqa: E402
from app.models.enums import AdminRole  # noqa: E402
from app.services.admin.auth import create_admin  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in AdminRole.__members__:
        sys.exit(f"usage: create_admin.py <username> <{'|'.join(AdminRole.__members__)}>")
    pw = getpass.getpass("Password (min 12 chars): ")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("passwords do not match")
    with session_scope() as s:
        a = create_admin(s, sys.argv[1], pw, AdminRole[sys.argv[2]])
        print(f"created admin id={a.id} username={a.username} role={a.role.value}")


if __name__ == "__main__":
    main()
