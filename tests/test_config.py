"""Settings from the environment / .env, and the bank picker endpoint."""
import os
import subprocess
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def load_config(env):
    """Import config in a fresh interpreter (it reads the environment at import)."""
    full_env = {k: v for k, v in os.environ.items() if k not in ("BANKS_DATA_DIR", "KNOWN_BANKS")}
    full_env.update(env)
    code = "import config, json; print(json.dumps([config.DATA_DIR, config.KNOWN_BANKS]))"
    out = subprocess.run([sys.executable, "-c", code], cwd=SRC, env=full_env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    import json
    return json.loads(out.stdout)


def test_environment_overrides_settings(tmp_path):
    data_dir, banks = load_config({"BANKS_DATA_DIR": str(tmp_path / "d"), "KNOWN_BANKS": " A, B ,,C "})
    assert os.path.normcase(data_dir) == os.path.normcase(str(tmp_path / "d"))
    assert banks == ["A", "B", "C"]
    assert os.path.isdir(tmp_path / "d" / "config")


def test_bank_options_merge_env_and_registry(client, sandbox, monkeypatch):
    import config
    monkeypatch.setattr(config, "KNOWN_BANKS", ["SBI", "HDFC"])
    with open(sandbox.BANKS_FILE, "w") as fh:
        fh.write("Name,Owner,Tags\nCOOP,me,\nHDFC,me,\nOther,x,\n")
    response = client.get("/api/bank-options")
    assert response.status_code == 200
    assert response.json() == ["COOP", "HDFC", "SBI", "Other"]


def test_bank_options_without_registry(client, sandbox, monkeypatch):
    import config
    monkeypatch.setattr(config, "KNOWN_BANKS", ["ICICI"])
    assert client.get("/api/bank-options").json() == ["ICICI", "Other"]
