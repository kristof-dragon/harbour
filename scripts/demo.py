"""Run an isolated demo: .venv/bin/python scripts/demo.py"""
import os
import secrets
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
data = root / ".demo-data"
data.mkdir(exist_ok=True, mode=0o700)
secret = data / "secret"
if not secret.exists():
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(48))
os.environ.update(HARBOUR_DATA=str(data), HARBOUR_DEMO="true", HARBOUR_SECRET=secret.read_text(),
                  HARBOUR_PASSWORD=secrets.token_urlsafe(24), HARBOUR_SECURE_COOKIE="false",
                  HARBOUR_ORIGIN="http://127.0.0.1:8097")
import uvicorn

if __name__ == "__main__":
    uvicorn.run("harbour.app:app", host="127.0.0.1", port=8097, log_level="warning", proxy_headers=False)
