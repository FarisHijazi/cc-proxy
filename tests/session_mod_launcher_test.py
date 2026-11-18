"""A saved, private event destination opts only new interactive sessions in."""

import runpy
from pathlib import Path

import pytest

mod = runpy.run_path(str(Path(__file__).parents[1] / "scripts/claude-account-router"))["session_mod"]


@pytest.fixture
def config(tmp_path):
    key = tmp_path / "native-key"
    key.write_text("toy-native-launch-key-for-isolated-tests")
    key.chmod(0o600)
    return {"url": "http://127.0.0.1:9000", "key_file": str(key)}


def test_existing_configuration_is_opt_out_and_clears_inherited_target():
    env = {"CC_PROXY_SESSION_EVENTS_URL": "https://old.example.com", "CC_PROXY_SESSION_EVENTS_KEY_FILE": "/old"}
    original = ["--model", "claude-sonnet"]
    assert mod(None, original, env) == original
    assert not env


def test_new_launch_preserves_other_plugins_and_does_not_copy_key(config):
    env = {}
    args = mod(config, ["--plugin-dir", "/existing-plugin", "--model", "claude-sonnet"], env)
    assert args.count("--plugin-dir") == 2
    assert "/existing-plugin" in args
    assert args.count("--remote-control") == 1
    assert env == {
        "CC_PROXY_SESSION_EVENTS_URL": "http://127.0.0.1:9000/v1/code/sessions/mod-events",
        "CC_PROXY_SESSION_EVENTS_KEY_FILE": config["key_file"],
    }
    assert "toy-native" not in str(env)


@pytest.mark.parametrize("args", [["-p", "hello"], ["--print"], ["--print=json"], ["--bare"], ["--safe-mode"], ["--no-remote-control"]])
def test_incompatible_launch_is_explicit(config, args):
    with pytest.raises(ValueError, match="interactive"):
        mod(config, args, {})


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://example.com/path", "https://example.com/?secret=x", "https://example.com/#fragment", "ftp://example.com"])
def test_invalid_destination_cannot_receive_events(config, url):
    with pytest.raises(ValueError, match="HQ origin"):
        mod({**config, "url": url}, [], {})


def test_publicly_readable_key_refused(config):
    Path(config["key_file"]).chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        mod(config, [], {})
