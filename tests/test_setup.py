import json
import os
import stat
import subprocess

import pytest

from harbour import bootstrap, store
from harbour.passwords import password_hash, password_ok
from scripts import setup_env, start


@pytest.fixture
def seed(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path / "data")
    monkeypatch.setattr(store, "DEMO", False)
    values = {"FIRST_RUN": "True", "HARBOUR_ADMIN": "first-admin",
              "HARBOUR_ADMIN_PASSWORD_HASH": password_hash("an-example-long-password"),
              "HARBOUR_BOOTSTRAP_ID": "test-receipt", "HARBOUR_SECRET": "x" * 64}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(start, "ROOT", tmp_path)
    setup_env.write_env(tmp_path / ".env", values)
    return values


def test_hash_seeds_first_admin_once_and_receipt_is_durable(seed, monkeypatch):
    store.initialize()
    account = store.one("SELECT * FROM users")
    assert account["password"] == seed["HARBOUR_ADMIN_PASSWORD_HASH"]
    assert password_ok("an-example-long-password", account["password"])
    assert bootstrap.receipt() == {"seeded": True, "name": "first-admin", "id": "test-receipt"}
    monkeypatch.setenv("FIRST_RUN", "False")
    monkeypatch.delenv("HARBOUR_ADMIN_PASSWORD_HASH")
    monkeypatch.delenv("HARBOUR_PASSWORD", raising=False)
    store.initialize()
    assert store.one("SELECT * FROM users") == account
    monkeypatch.setenv("FIRST_RUN", "True")
    monkeypatch.setenv("HARBOUR_ADMIN_PASSWORD_HASH", password_hash("replacement-password"))
    monkeypatch.setenv("HARBOUR_BOOTSTRAP_ID", "another-installation")
    store.initialize()
    assert store.one("SELECT * FROM users") == account
    assert bootstrap.receipt()["id"] == "test-receipt"


def test_first_run_false_never_reseeds_an_empty_database(seed, monkeypatch):
    monkeypatch.setenv("FIRST_RUN", "False")
    with pytest.raises(RuntimeError, match="No accounts exist"):
        store.initialize()
    assert not store.rows("SELECT * FROM users")


def test_malformed_hash_does_not_fall_back_to_plaintext(seed, monkeypatch):
    monkeypatch.setenv("HARBOUR_ADMIN_PASSWORD_HASH", "broken")
    monkeypatch.setenv("HARBOUR_PASSWORD", "valid-legacy-password")
    with pytest.raises(RuntimeError, match="salted scrypt"):
        store.initialize()
    assert not store.rows("SELECT * FROM users")


def test_setup_hashes_secret_input_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_env, "ROOT", tmp_path)
    answers = iter(["3", "admin", "8384"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    password = "private $with#quotes'\\unicode-£"
    monkeypatch.setattr(setup_env.getpass, "getpass", lambda _: password)
    setup_env.main()
    path = tmp_path / ".env"
    original, values = start.read_env(path)
    output = capsys.readouterr().out
    assert password not in original and password not in output
    assert values["HARBOUR_PORT"] == "8384"
    assert values["HARBOUR_ORIGIN"] == "http://localhost:8384"
    assert "http://<Harbour-host-IP>:8384" in output
    assert password_ok(password, values["HARBOUR_ADMIN_PASSWORD_HASH"])
    assert len(values["HARBOUR_SECRET"]) == 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(RuntimeError, match="already exists"):
        setup_env.main()
    assert path.read_text() == original
    assert password_hash(password) != password_hash(password)


@pytest.mark.parametrize("value", ["http://harbour.test", "https://user:pass@harbour.test", "https://harbour.test/path", "https://harbour.test?query", "https://harbour.test:99999", "https://harbour.test/#fragment"])
def test_invalid_proxy_origins(value):
    with pytest.raises(ValueError):
        setup_env.origin(value)


def test_proxy_and_bind_validation():
    assert setup_env.origin("https://harbour.test:8443/") == "https://harbour.test:8443"
    assert setup_env.proxies("192.0.2.200,2001:db8::200") == "192.0.2.200/32,2001:db8::200/128"
    for value in ("0.0.0.0/0", "::/0", "", "not-an-address"):
        with pytest.raises(ValueError):
            setup_env.proxies(value)
    assert setup_env.bind_address("0.0.0.0") == "0.0.0.0"
    assert setup_env.lan_bind_address("0.0.0.0") == "0.0.0.0"


def test_cleanup_only_after_receipt_and_recreates_without_secrets(seed, monkeypatch):
    path = start.ROOT / ".env"
    calls = []
    def compose(*args, **kwargs):
        calls.append(args)
        if args[0] == "port":
            return "127.0.0.1:8080\n"
        if args[0] == "up" and "--force-recreate" in args:
            _, values = start.read_env(path)
            assert values["FIRST_RUN"] == "False"
            assert not start.BOOTSTRAP_KEYS.intersection(values)
        elif "--check-clean" in args:
            return '{"clean":true}'
        elif "harbour.bootstrap" in args:
            return json.dumps({"seeded": True, "name": seed["HARBOUR_ADMIN"], "id": seed["HARBOUR_BOOTSTRAP_ID"]})
    monkeypatch.setattr(start, "compose", compose)
    start.start()
    text, values = start.read_env(path)
    assert seed["HARBOUR_ADMIN_PASSWORD_HASH"] not in text
    assert values["HARBOUR_SECRET"] == seed["HARBOUR_SECRET"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len([c for c in calls if c[0] == "up"]) == 2
    start.start()
    assert len([c for c in calls if c[0] == "up"]) == 3


@pytest.mark.parametrize("failure", ["startup", "receipt", "recreate"])
def test_failed_startup_is_retryable_without_losing_required_values(seed, monkeypatch, failure):
    path = start.ROOT / ".env"
    original = path.read_text()
    def compose(*args, **kwargs):
        if args[0] == "up" and (failure == "startup" or "--force-recreate" in args):
            raise subprocess.CalledProcessError(1, ["docker", "compose"])
        if "harbour.bootstrap" in args:
            return json.dumps({"seeded": True, "name": seed["HARBOUR_ADMIN"], "id": "mismatch" if failure == "receipt" else seed["HARBOUR_BOOTSTRAP_ID"]})
    monkeypatch.setattr(start, "compose", compose)
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        start.start()
    if failure == "recreate":
        _, values = start.read_env(path)
        assert values["FIRST_RUN"] == "False" and values["HARBOUR_SECRET"] == seed["HARBOUR_SECRET"]
    else:
        assert path.read_text() == original


def test_refuse_symlink_or_changed_env(tmp_path):
    target = tmp_path / "target"
    target.write_text("FIRST_RUN=False\n")
    link = tmp_path / ".env"
    link.symlink_to(target)
    with pytest.raises((OSError, RuntimeError)):
        setup_env.write_env(link, {"FIRST_RUN": "True"})
    with pytest.raises(RuntimeError, match="regular file"):
        start.read_env(link)
    with pytest.raises(RuntimeError, match="changed"):
        start.retire_credentials(target, "FIRST_RUN=True\n")
    assert target.read_text() == "FIRST_RUN=False\n"


@pytest.mark.parametrize("address", ["127.0.0.1", "127.3.4.5", "169.254.1.2", "224.0.0.1"])
def test_lan_proxy_cannot_accidentally_use_unreachable_bind(address):
    with pytest.raises(ValueError):
        setup_env.lan_bind_address(address)


def test_lan_proxy_wizard_uses_selected_host_port(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_env, "ROOT", tmp_path)
    answers = iter(["2", "admin", "8384", "https://harbour.example.test", "198.51.100.2", "127.0.0.1", "198.51.100.3"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(setup_env.getpass, "getpass", lambda _: "setup-test-password")
    setup_env.main()
    _, values = start.read_env(tmp_path / ".env")
    assert values["HARBOUR_PORT"] == "8384" and values["HARBOUR_BIND_ADDRESS"] == "198.51.100.3"
    assert "COMPOSE_FILE" not in values
    assert "NPM upstream: scheme=http, hostname=<Harbour-host-IP>, port=8384" in capsys.readouterr().out


def test_shared_network_reports_internal_port_separately(capsys):
    setup_env.show_connections({"HARBOUR_PORT": "8384", "COMPOSE_FILE": "compose.yaml:compose.npm.yaml",
                               "HARBOUR_BIND_ADDRESS": "127.0.0.1",
                               "HARBOUR_PROXY_NETWORK": "test-proxy"})
    output = capsys.readouterr().out
    assert "http://<Harbour-host-IP>:8384" in output
    assert "NPM upstream: scheme=http, hostname=harbour, port=8080" in output


def test_separate_proxy_default_needs_no_specific_bind_address(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_env, "ROOT", tmp_path)
    answers = iter(["2", "admin", "8384", "https://harbour.example.test", "198.51.100.2", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(setup_env.getpass, "getpass", lambda _: "setup-test-password")
    setup_env.main()
    _, values = start.read_env(tmp_path / ".env")
    assert values["HARBOUR_PORT"] == "8384" and values["HARBOUR_BIND_ADDRESS"] == "0.0.0.0"
    output = capsys.readouterr().out
    assert "NPM upstream: scheme=http, hostname=<Harbour-host-IP>, port=8384" in output
    assert "hostname=0.0.0.0" not in output and "http://0.0.0.0" not in output


def test_start_rejects_unexpected_published_port(seed, monkeypatch):
    path = start.ROOT / ".env"
    start.retire_credentials(path, path.read_text())
    with path.open("a") as output:
        output.write("HARBOUR_PORT=8384\n")
    def compose(*args, **kwargs):
        if args[0] == "port":
            return "127.0.0.1:8080\n"
        if "--check-clean" in args:
            return '{"clean":true}'
    monkeypatch.setattr(start, "compose", compose)
    with pytest.raises(RuntimeError, match="published port does not match"):
        start.start()
