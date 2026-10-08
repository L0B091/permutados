#!/usr/bin/env python
"""Rotate ENCRYPTION_KEY.

1. Generate a new key:  python -c "from app.core.crypto import generate_key; print(generate_key())"
2. Stop the worker, let the inbound queue drain.
3. Set ENCRYPTION_KEY=<new> and ENCRYPTION_OLD_KEYS=<old> in .env
4. Run (from backend/):  python ../scripts/rotate_encryption_key.py
5. Start the services again. Once done, ENCRYPTION_OLD_KEYS can be emptied.
Prints only counts, never data.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.crypto import get_cipher  # noqa: E402
from app.core.db import session_scope  # noqa: E402
from app.services.users.pii import rotate_encryption  # noqa: E402


def main() -> None:
    cipher = get_cipher()
    if len(cipher.keys) < 2:
        sys.exit("ENCRYPTION_OLD_KEYS is empty: nothing to rotate from")
    with session_scope() as s:
        print(rotate_encryption(s, cipher))


if __name__ == "__main__":
    main()
