"""Generate fresh Layer 2 vault keys for local/dev use.

Prints .env-ready lines. Writes nothing to disk — paste the output into your
own .env manually so key material never ends up in shell history files or
this script's own diff.

    python scripts/gen_vault_keys.py
"""
import base64
import json
import os
import secrets


def _b64_key() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


def main() -> None:
    kek = _b64_key()
    hsk = _b64_key()
    admin_key = secrets.token_urlsafe(32)

    print("# Paste into .env — do not commit these values.")
    print("VAULT_KEY_SOURCE=env")
    print("VAULT_KEK_VERSION=1")
    print(f"VAULT_KEKS={json.dumps({'1': kek})}")
    print(f"VAULT_HSK={hsk}")
    print(f"VAULT_ADMIN_KEY={admin_key}")


if __name__ == "__main__":
    main()
