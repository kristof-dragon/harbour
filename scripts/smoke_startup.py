"""Exercise first boot, hash removal and restart on a disposable Compose project."""
import json
from pathlib import Path
import secrets
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harbour.passwords import password_hash
from scripts import setup_env, start


def main():
    with tempfile.TemporaryDirectory(prefix="harbour-startup-") as directory:
        start.ROOT = Path(directory)
        project = "harbour-startup-" + secrets.token_hex(4)
        password = secrets.token_urlsafe(24)
        values = {"COMPOSE_PROJECT_NAME": project, "FIRST_RUN": "True", "HARBOUR_ADMIN": "bootstrap-admin",
                  "HARBOUR_ADMIN_PASSWORD_HASH": password_hash(password), "HARBOUR_BOOTSTRAP_ID": secrets.token_hex(16),
                  "HARBOUR_SECRET": secrets.token_urlsafe(48), "HARBOUR_PORT": "0",
                  "HARBOUR_ORIGIN": "http://localhost", "HARBOUR_SECURE_COOKIE": "false"}
        setup_env.write_env(start.ROOT / ".env", values)
        compose = (ROOT / "compose.yaml").read_text().replace("build: .", "build: " + json.dumps(str(ROOT)))
        (start.ROOT / "compose.yaml").write_text(compose)
        try:
            config = json.loads(start.compose("config", "--format", "json", capture=True))
            assert config["services"]["harbour"]["environment"]["HARBOUR_ADMIN_PASSWORD_HASH"] == values["HARBOUR_ADMIN_PASSWORD_HASH"]
            assert password not in json.dumps(config)
            start.start()
            text, cleaned = start.read_env(start.ROOT / ".env")
            assert cleaned["FIRST_RUN"] == "False" and not start.BOOTSTRAP_KEYS.intersection(cleaned)
            assert values["HARBOUR_ADMIN_PASSWORD_HASH"] not in text
            assert cleaned["HARBOUR_SECRET"] == values["HARBOUR_SECRET"]
            for attempt in range(2):
                address = start.compose("port", "harbour", "8080", capture=True).strip()
                request = urllib.request.Request("http://" + address + "/api/login",
                    data=json.dumps({"name": values["HARBOUR_ADMIN"], "password": password}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    assert json.load(response)["role"] == "admin"
                if attempt == 0:
                    start.compose("stop")
                    start.start()
            # Verify the wizard's optional proxy file merges without publishing to the LAN.
            (start.ROOT / "compose.npm.yaml").write_text((ROOT / "compose.npm.yaml").read_text())
            with (start.ROOT / ".env").open("a") as output:
                output.write("COMPOSE_FILE=compose.yaml:compose.npm.yaml\nHARBOUR_PROXY_NETWORK=harbour-test-proxy\n")
            config = json.loads(start.compose("config", "--format", "json", capture=True))
            assert config["networks"]["proxy"]["external"] is True
            assert config["services"]["harbour"]["ports"][0]["host_ip"] == "127.0.0.1"
            assert "proxy" in config["services"]["harbour"]["networks"]
            print("PASS: real Docker bootstrap, salted-hash sign-in, credential cleanup, persistent restart and NPM configuration.")
        finally:
            # These resources belong only to our random test project, never the user's deployment.
            start.compose("-f", "compose.yaml", "down", "--volumes", "--remove-orphans", "--rmi", "local")


if __name__ == "__main__":
    main()
