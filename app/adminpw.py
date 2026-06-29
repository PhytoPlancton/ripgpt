"""Generate an ADMIN_PASSWORD_HASH for the .env file.

Usage (interactive, password never echoed or stored):

    python -m app.adminpw

or, inside the running container:

    docker compose exec api python -m app.adminpw

It prints the line to paste into .env. The plaintext password is never written anywhere.
"""

from __future__ import annotations

import getpass
import sys

from app.auth import hash_password


def main() -> int:
    print("Generate ADMIN_PASSWORD_HASH for ripgpt admin console.\n")
    pw = getpass.getpass("New admin password: ")
    if len(pw) < 8:
        print("Password too short (min 8 chars).", file=sys.stderr)
        return 1
    pw2 = getpass.getpass("Confirm password: ")
    if pw != pw2:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    digest = hash_password(pw)
    print("\nAdd these to your .env (keep them secret):\n")
    print("ADMIN_USER=admin")
    print("ADMIN_PASSWORD_HASH=" + digest)
    print("\n(change ADMIN_USER to whatever username you want)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
