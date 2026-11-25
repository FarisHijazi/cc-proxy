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
def test_noninteractive_or_disabled_launch_is_unchanged(config, args):
    Path(config["key_file"]).unlink()
    env = {"CC_PROXY_SESSION_EVENTS_URL": "https://old.example.com", "CC_PROXY_SESSION_EVENTS_KEY_FILE": "/old"}
    assert mod(config, args, env) is args
    assert env == {}


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://example.com/path", "https://example.com/?secret=x", "https://example.com/#fragment", "ftp://example.com"])
def test_invalid_destination_cannot_receive_events(config, url):
    with pytest.raises(ValueError, match="HQ origin"):
        mod({**config, "url": url}, [], {})


def test_publicly_readable_key_refused(config):
    Path(config["key_file"]).chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        mod(config, [], {})


def test_proxy_opt_in_uses_existing_transport_without_a_native_key():
    env = {"HTTPS_PROXY": "http://127.0.0.1:19000", "CC_PROXY_SESSION_EVENTS_KEY_FILE": "/old/key"}
    args = mod(True, [], env)
    assert env == {
        "HTTPS_PROXY": "http://127.0.0.1:19000",
        "CC_PROXY_SESSION_EVENTS_URL": "https://session-events.cc-proxy.invalid/v1/code/sessions/mod-events",
    }
    assert args.count("--plugin-dir") == args.count("--remote-control") == 1


def test_proxy_opt_in_requires_launcher_transport():
    with pytest.raises(ValueError, match="existing HTTPS proxy"):
        mod(True, [], {})


@pytest.mark.parametrize("args", [["-p"], ["--bare"], ["--safe-mode"], ["--no-remote-control"]])
def test_proxy_opt_in_does_not_enable_headless_or_disabled_native_sessions(args):
    env = {"CC_PROXY_SESSION_EVENTS_URL": "https://old.example.com", "CC_PROXY_SESSION_EVENTS_KEY_FILE": "/old"}
    assert mod(True, args, env) is args
    assert env == {}


def test_interactive_malformed_configuration_is_still_rejected():
    with pytest.raises(ValueError, match="session_mod needs"):
        mod({"unexpected": True}, [], {})


def test_prompt_after_option_separator_does_not_disable_interactive_mod():
    env = {"HTTPS_PROXY": "http://127.0.0.1:19000"}
    args = mod(True, ["--", "--print"], env)
    assert args[-2:] == ["--", "--print"]
    assert args.count("--remote-control") == 1


def test_metadata_only_loads_existing_mod_without_event_transport_or_remote_control():
    env = {"CC_PROXY_SESSION_EVENTS_URL": "https://old.example.com", "CC_PROXY_SESSION_EVENTS_KEY_FILE": "/old"}
    original = ["--plugin-dir", "/existing-plugin", "--model", "sol"]
    args = mod(False, original, env, model_metadata=True)
    assert args.count("--plugin-dir") == 2
    assert "/existing-plugin" in args
    assert args[args.index("--model") + 1] == "sol"
    assert "--remote-control" not in args
    assert env == {}


@pytest.mark.parametrize("events", [None, False, True])
@pytest.mark.parametrize("metadata", [False, True])
def test_metadata_and_event_combinations_preserve_independent_enablement(events, metadata):
    env = {"HTTPS_PROXY": "http://127.0.0.1:19000"}
    args = mod(events, ["--model", "sol"], env, metadata)
    assert ("--plugin-dir" in args) is (events is True or metadata)
    assert ("--remote-control" in args) is (events is True)
    assert ("CC_PROXY_SESSION_EVENTS_URL" in env) is (events is True)
    assert "CC_PROXY_SESSION_EVENTS_KEY_FILE" not in env


@pytest.mark.parametrize("args", [["-p"], ["--bare"], ["--safe-mode"], ["--no-remote-control"]])
def test_metadata_only_keeps_headless_and_disabled_launches_unmodified(args):
    env = {"CC_PROXY_SESSION_EVENTS_URL": "https://old.example.com"}
    assert mod(False, args, env, True) is args
    assert not env


def test_metadata_opt_in_requires_boolean():
    with pytest.raises(ValueError, match="model_metadata must be a boolean"):
        mod(False, [], {}, "yes")


def test_all_proxy_modes_intercept_reserved_host_once(tmp_path):
    import re
    from types import SimpleNamespace

    launcher = runpy.run_path(str(Path(__file__).parents[1] / "scripts/claude-account-router"))
    for chrome in (None, tmp_path / "chrome.json"):
        command = launcher["proxy_command"]("http://127.0.0.1:19001", 19000, tmp_path, chrome_manifest=chrome)
        pattern = command[command.index("--allow-hosts") + 1]
        assert re.fullmatch(pattern, "session-events.cc-proxy.invalid:443")
        assert not re.fullmatch(pattern, "session-events.cc-proxy.invalid.evil.test:443")
        addon = str(Path(__file__).parents[1] / "scripts/account_remote_route.py")
        assert command.count(addon) == 1
        options = launcher["hq_remote_route"](SimpleNamespace(
            remote_control_backend="http://127.0.0.1:9000", remote_control_key_file=tmp_path / "key",
        ), tmp_path)
        assert addon not in options
