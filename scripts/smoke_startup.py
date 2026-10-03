"""Exercise first boot, hash removal and restart on a disposable Compose project."""
import json
from pathlib import Path
import secrets
import socket
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
        # Choose an unused, non-default host port without disrupting local services.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            selected_port = str(listener.getsockname()[1])
        # A legacy bind setting must not override the agreed short port mapping.
        values = {"COMPOSE_PROJECT_NAME": project, "FIRST_RUN": "True", "HARBOUR_ADMIN": "bootstrap-admin",
                  "HARBOUR_ADMIN_PASSWORD_HASH": password_hash(password), "HARBOUR_BOOTSTRAP_ID": secrets.token_hex(16),
                  "HARBOUR_SECRET": secrets.token_urlsafe(48), "HARBOUR_PORT": selected_port,
                  "HARBOUR_ORIGIN": "http://localhost", "HARBOUR_SECURE_COOKIE": "false", "HARBOUR_BIND_ADDRESS": "127.0.0.1"}
        setup_env.write_env(start.ROOT / ".env", values)
        compose = (ROOT / "compose.yaml").read_text().replace("build: .", "build: " + json.dumps(str(ROOT)))
        (start.ROOT / "compose.yaml").write_text(compose)
        try:
            config = json.loads(start.compose("config", "--format", "json", capture=True))
            assert config["services"]["harbour"]["environment"]["HARBOUR_ADMIN_PASSWORD_HASH"] == values["HARBOUR_ADMIN_PASSWORD_HASH"]
            assert password not in json.dumps(config)
            port = config["services"]["harbour"]["ports"][0]
            assert port["published"] == selected_port and port["target"] == 8080 and "host_ip" not in port
            # Also resolve the reported port exactly, even when it is occupied locally.
            env_path = start.ROOT / ".env"
            original = env_path.read_text()
            try:
                env_path.write_text(original.replace("HARBOUR_PORT=" + selected_port, "HARBOUR_PORT=8384"))
                selected_config = json.loads(start.compose("config", "--format", "json", capture=True))
                assert selected_config["services"]["harbour"]["ports"][0]["published"] == "8384"
                assert selected_config["services"]["harbour"]["ports"][0]["target"] == 8080
                assert "host_ip" not in selected_config["services"]["harbour"]["ports"][0]
            finally:
                env_path.write_text(original)
            start.start()
            text, cleaned = start.read_env(start.ROOT / ".env")
            assert cleaned["FIRST_RUN"] == "False" and not start.BOOTSTRAP_KEYS.intersection(cleaned)
            assert values["HARBOUR_ADMIN_PASSWORD_HASH"] not in text
            assert cleaned["HARBOUR_SECRET"] == values["HARBOUR_SECRET"]
            for attempt in range(2):
                address = start.compose("port", "harbour", "8080", capture=True).strip()
                assert all(binding.rsplit(":", 1)[-1] == selected_port for binding in address.splitlines())
                request = urllib.request.Request("http://127.0.0.1:" + selected_port + "/api/login",
                    data=json.dumps({"name": values["HARBOUR_ADMIN"], "password": password}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    assert json.load(response)["role"] == "admin"
                if attempt == 0:
                    start.compose("stop")
                    start.start()
            # Verify the optional proxy network keeps the same simple port mapping.
            (start.ROOT / "compose.npm.yaml").write_text((ROOT / "compose.npm.yaml").read_text())
            with (start.ROOT / ".env").open("a") as output:
                output.write("COMPOSE_FILE=compose.yaml:compose.npm.yaml\nHARBOUR_PROXY_NETWORK=harbour-test-proxy\n")
            config = json.loads(start.compose("config", "--format", "json", capture=True))
            assert config["networks"]["proxy"]["external"] is True
            assert "host_ip" not in config["services"]["harbour"]["ports"][0]
            assert config["services"]["harbour"]["ports"][0]["published"] == selected_port
            assert "proxy" in config["services"]["harbour"]["networks"]
            print("PASS: port 8384 interpolation, custom host-port HTTP access, Docker bootstrap, credential cleanup, persistent restart and NPM configuration.")
        finally:
            # These resources belong only to our random test project, never the user's deployment.
            start.compose("-f", "compose.yaml", "down", "--volumes", "--remove-orphans", "--rmi", "local")


if __name__ == "__main__":
    main()
