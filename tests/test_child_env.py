"""The environment Keryx's operational children run in: the speech-to-speech download, the
package installers and the checkout's unit installer. None of Keryx's secrets reaches them;
what they need to work — `PATH`, `HOME`, Keryx's directories, a proxy, a Hugging Face token
for a gated download — does. Each spawn site is run for real, on a child that writes down
the environment it was given."""

import json
import os
import sys

import pytest

from keryx import child_env, units
from keryx.localmodels import speech, voice
from keryx.localmodels.speech import Repo

#: Credentials of Keryx's, exported the ways an owner might: a setting's name, a setting
#: read case-insensitively, and a credential that is no setting at all.
SECRETS = {
    "KERYX_PIN": "482915",
    "keryx_pin": "482915",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "TWILIO_AUTH_TOKEN": "twilio-secret",
    "OPENAI_API_KEY": "sk-secret",
    "ANTHROPIC_AUTH_TOKEN": "bearer-secret",
    "keryx_local_api_key": "local-secret",
}
#: What the children are owed.
KEPT = {
    "HF_TOKEN": "hf_gated",
    "HUGGING_FACE_HUB_TOKEN": "hf_gated_too",
    "HTTPS_PROXY": "http://proxy.invalid:3128",
}
#: A child that writes the environment it was given to the file `ENV_OUT` names.
DUMP = "import json, os; open(os.environ['ENV_OUT'], 'w').write(json.dumps(dict(os.environ)))"


@pytest.fixture
def exported(monkeypatch, tmp_path):
    for name, value in {**SECRETS, **KEPT}.items():
        monkeypatch.setenv(name, value)
    out = tmp_path / "env.json"
    monkeypatch.setenv("ENV_OUT", str(out))
    return lambda: json.loads(out.read_text())


def assert_scrubbed(env: dict[str, str]) -> None:
    assert not set(SECRETS) & set(env)
    assert all(value not in env.values() for value in SECRETS.values())
    for name, value in KEPT.items():
        assert env[name] == value
    for name in ("PATH", "HOME", "KERYX_HOME", "XDG_DATA_HOME"):
        assert env.get(name) == os.environ.get(name) and name in env, name


def script(tmp_path, name: str = "child") -> str:
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{DUMP}\n")
    path.chmod(0o755)
    return str(path)


def test_the_operational_environment_holds_no_secret_and_what_children_need(exported):
    assert_scrubbed(child_env.operational_env())
    assert child_env.operational_env(EXTRA="1")["EXTRA"] == "1"


def test_the_speech_download_child_holds_no_secret(exported, tmp_path):
    speech.fetch(script(tmp_path, "python"), [Repo("a/one")], lambda done, total: None)

    assert_scrubbed(exported())


def test_a_package_installer_holds_no_secret(exported):
    assert voice.run_detached([sys.executable, "-c", DUMP]) == 0

    assert_scrubbed(exported())


def test_the_checkouts_unit_installer_holds_no_secret(exported, tmp_path):
    code, _out, _err = units.run_captured([script(tmp_path, "install-launchd.sh")])

    assert code == 0
    assert_scrubbed(exported())
