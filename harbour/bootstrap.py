"""Console-only bootstrap receipt. Never returns a password or password hash."""
import json
import os
import sys

from . import store


def receipt():
    saved = store.one("SELECT value FROM settings WHERE key='bootstrap'")
    if not saved or store.DEMO:
        return {"seeded": False}
    value = json.loads(saved["value"])
    account = store.one("SELECT name,role FROM users WHERE id=?", (value["user_id"],))
    return {"seeded": bool(account and account["role"] == "admin" and account["name"] == value["name"]),
            "id": value["id"], "name": value["name"]}


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-clean"]:
        print(json.dumps({"clean": os.environ.get("FIRST_RUN", "").lower() == "false" and not any(
            os.environ.get(k) for k in ("HARBOUR_ADMIN_PASSWORD_HASH", "HARBOUR_PASSWORD", "HARBOUR_ADMIN", "HARBOUR_BOOTSTRAP_ID"))}))
    else:
        print(json.dumps(receipt()))
