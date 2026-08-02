"""Tests for desktop integration and the dashboard's write endpoints.

No browser is opened, no tray is created, and no process is spawned — the boundary
functions are patched. What's under test is the decision-making around them:

* a CLI flag must beat the persisted setting, but only for that run
* the config write endpoint must reject cross-site callers and unknown keys
* a stale pid file must be distinguishable from a live background process
"""

from __future__ import annotations

from typing import Any

import pytest

from dropwatch import desktop
from dropwatch.config import AppConfig, ConfigManager, UIConfig
from dropwatch.web import Dashboard

# ------------------------------------------------------------------- config


def test_ui_defaults_are_conservative() -> None:
    ui = UIConfig()
    # Nothing pops open a window unless asked, and the server stays local.
    assert ui.host == "127.0.0.1"
    assert ui.open_dashboard is False
    assert ui.open_twitch is False
    assert ui.reopen_twitch_on_rotate is False
    # The tray is on: without it, a detached run has no way to be stopped.
    assert ui.tray is True


def test_ui_port_is_range_checked() -> None:
    from pydantic import ValidationError

    assert UIConfig(port=9000).port == 9000
    for bad in (0, 70000):
        with pytest.raises(ValidationError):
            UIConfig(port=bad)


async def test_ui_settings_are_overridable_at_runtime() -> None:
    manager = ConfigManager()
    await manager.load()
    assert manager.current.ui.open_twitch is False

    await manager.set_override("ui.open_twitch", True)
    assert manager.current.ui.open_twitch is True
    assert "ui.open_twitch" in manager.overrides

    await manager.clear_override("ui.open_twitch")
    assert manager.current.ui.open_twitch is False


async def test_ui_override_rejects_a_bad_value_and_rolls_back() -> None:
    from dropwatch.config import ConfigError

    manager = ConfigManager()
    await manager.load()
    with pytest.raises(ConfigError):
        await manager.set_override("ui.port", 999999)
    # The rejected value must not have been left applied.
    assert manager.current.ui.port == 8787
    assert "ui.port" not in manager.overrides


def test_ui_section_is_in_the_flat_view() -> None:
    """`config show` and the dashboard both read the flattened form."""
    flat = ConfigManager().flat()
    assert "ui.open_twitch" in flat and "ui.tray" in flat


# -------------------------------------------------------------------- browser


def test_open_url_never_raises(monkeypatch: Any) -> None:
    def boom(_url: str) -> bool:
        raise RuntimeError("no browser here")

    monkeypatch.setattr(desktop.webbrowser, "open", boom)
    # A machine with no browser must still be able to watch streams.
    assert desktop.open_url("http://x/") is False


def test_open_channel_builds_the_twitch_url(monkeypatch: Any) -> None:
    seen: list[str] = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda u: seen.append(u) or True)

    assert desktop.open_channel("ow_esports") is True
    assert seen == ["https://www.twitch.tv/ow_esports"]


def test_open_url_reports_when_no_browser_is_registered(monkeypatch: Any) -> None:
    monkeypatch.setattr(desktop.webbrowser, "open", lambda _u: False)
    assert desktop.open_url("http://x/") is False


# ------------------------------------------------------------------ pid file


def test_pid_roundtrip_and_stale_detection(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr(desktop.paths, "PID_FILE", tmp_path / "pid")
    monkeypatch.setattr(desktop.paths, "DATA_DIR", tmp_path)

    assert desktop.read_pid() is None

    desktop.write_pid(4242)
    assert desktop.read_pid() == 4242

    desktop.clear_pid()
    assert desktop.read_pid() is None
    desktop.clear_pid()  # idempotent — a missing file is not an error


def test_unreadable_pid_file_reads_as_absent(monkeypatch: Any, tmp_path: Any) -> None:
    pid_file = tmp_path / "pid"
    pid_file.write_text("not a number", "utf-8")
    monkeypatch.setattr(desktop.paths, "PID_FILE", pid_file)
    # Garbage must read as "nothing running", not crash `stop`.
    assert desktop.read_pid() is None


def test_current_process_is_reported_alive() -> None:
    import os

    assert desktop.process_alive(os.getpid()) is True
    # A pid that cannot exist; treated as dead so `stop` clears the stale record.
    assert desktop.process_alive(999999999) is False


def test_relaunch_command_strips_detach() -> None:
    cmd = desktop._relaunch_command(["serve", "--detach", "--port", "9000"])
    assert "--detach" not in cmd
    assert cmd[-3:] == ["serve", "--port", "9000"]


def test_relaunch_uses_module_form_when_not_frozen(monkeypatch: Any) -> None:
    """Re-running __main__.py as a script would break its relative imports."""
    monkeypatch.setattr(desktop.paths, "FROZEN", False)
    cmd = desktop._relaunch_command(["serve"])
    assert cmd[1:3] == ["-m", "dropwatch"]


def test_relaunch_calls_the_exe_directly_when_frozen(monkeypatch: Any) -> None:
    monkeypatch.setattr(desktop.paths, "FROZEN", True)
    cmd = desktop._relaunch_command(["serve"])
    assert "-m" not in cmd


def test_child_env_strips_pyinstaller_handoff(monkeypatch: Any) -> None:
    """Regression: inheriting these made the detached child share the parent's
    unpack directory, so every bundled asset 404'd once the parent exited."""
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI12345")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\Temp\_MEI12345")
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", r"C:\app.exe")
    monkeypatch.setenv("_PYI_SOMETHING_NEW", "x")   # forward-compatibility sweep
    monkeypatch.setenv("PATH_KEEP_ME", "yes")

    env = desktop._child_env()

    assert "_MEIPASS2" not in env
    assert "_PYI_APPLICATION_HOME_DIR" not in env
    assert "_PYI_ARCHIVE_FILE" not in env
    assert "_PYI_SOMETHING_NEW" not in env, "unknown _PYI_ vars must go too"
    assert env["PATH_KEEP_ME"] == "yes", "the rest of the environment is preserved"


# ------------------------------------------------------- settings endpoint


class FakeRequest:
    def __init__(self, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers if headers is not None else {"X-Dropwatch": "1"}
        self.host = "127.0.0.1:8787"

    async def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeWatcher:
    """Stands in for a Watcher. Mirrors the real WatcherStatus shape.

    The dashboard renders every field of it, so a double that is missing one
    fails at render time rather than at the assertion — keep this in step with
    :class:`dropwatch.watcher.WatcherStatus`.
    """

    def __init__(self, channel: str | None = "ow_esports") -> None:
        self.paused = False
        self.resumed = False
        self.stopped = False
        self._channel = channel

    def status(self) -> Any:
        channel = self._channel

        class Stats:
            cycles = 3
            minutes_sent = 3
            telemetry_rejected = 0
            credited_start = 100
            credited_now = 103
            credited_gain = 3
            required = 720
            uptime = 180.0

        class S:
            state = "WATCHING"
            reason = "live and eligible"
            signals: dict[str, str] = {}
            stats = Stats()
            rotations = 0
            paused = False
            grace_remaining = 0.0
            pubsub_connected = True
            last_verdict_at = 0.0

        S.channel = channel  # type: ignore[attr-defined]
        return S()

    async def pause(self) -> None:
        self.paused = True

    async def resume(self) -> None:
        self.resumed = True

    async def stop(self) -> None:
        self.stopped = True


def _dashboard(manager: ConfigManager, watcher: Any = None) -> Dashboard:
    return Dashboard(
        watcher=watcher or FakeWatcher(),
        bus=None,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        drops=None,  # type: ignore[arg-type]
        config=manager,
    )


async def _json_of(response: Any) -> Any:
    import json

    return json.loads(response.text)


async def test_settings_list_reports_values_and_override_state() -> None:
    manager = ConfigManager()
    await manager.load()
    await manager.set_override("ui.open_twitch", True)

    body = await _json_of(await _dashboard(manager)._settings_get(FakeRequest()))
    by_key = {s["key"]: s for s in body["settings"]}

    assert by_key["ui.open_twitch"]["value"] is True
    assert by_key["ui.open_twitch"]["overridden"] is True
    assert by_key["ui.tray"]["overridden"] is False
    assert body["groups"], "settings are grouped for rendering"


async def test_settings_write_persists_and_is_readable_back() -> None:
    manager = ConfigManager()
    await manager.load()
    dash = _dashboard(manager)

    result = await _json_of(await dash._settings_set(
        FakeRequest({"key": "ui.open_twitch", "value": True})
    ))
    assert result["ok"] is True and result["value"] is True
    assert manager.current.ui.open_twitch is True

    # And can be reset back to the file default.
    await dash._settings_set(FakeRequest({"key": "ui.open_twitch", "reset": True}))
    assert manager.current.ui.open_twitch is False


async def test_settings_write_requires_the_custom_header() -> None:
    """Without this, any page in the browser could rewrite the config."""
    from aiohttp import web

    manager = ConfigManager()
    await manager.load()

    with pytest.raises(web.HTTPForbidden):
        await _dashboard(manager)._settings_set(
            FakeRequest({"key": "ui.tray", "value": False}, headers={})
        )


async def test_settings_write_rejects_a_cross_origin_caller() -> None:
    from aiohttp import web

    manager = ConfigManager()
    await manager.load()

    with pytest.raises(web.HTTPForbidden):
        await _dashboard(manager)._settings_set(FakeRequest(
            {"key": "ui.tray", "value": False},
            headers={"X-Dropwatch": "1", "Origin": "https://evil.example"},
        ))


async def test_settings_write_refuses_keys_outside_the_allowlist() -> None:
    """Valid config keys that aren't dashboard-editable must still be refused."""
    from aiohttp import web

    manager = ConfigManager()
    await manager.load()
    dash = _dashboard(manager)

    for key in ("ui.host", "ui.port", "twitch.client_id", "nonsense.key"):
        with pytest.raises(web.HTTPForbidden):
            await dash._settings_set(FakeRequest({"key": key, "value": "x"}))


async def test_settings_write_returns_the_reason_a_value_was_rejected() -> None:
    manager = ConfigManager()
    await manager.load()

    response = await _dashboard(manager)._settings_set(
        FakeRequest({"key": "liveness.grace_period", "value": 99999})
    )
    assert response.status == 400
    body = await _json_of(response)
    assert body["ok"] is False and "grace_period" in body["error"]


async def test_control_pauses_and_resumes() -> None:
    manager = ConfigManager()
    await manager.load()
    watcher = FakeWatcher()
    dash = _dashboard(manager, watcher)

    await dash._control(FakeRequest({"action": "pause"}))
    assert watcher.paused is True

    await dash._control(FakeRequest({"action": "resume"}))
    assert watcher.resumed is True


async def test_control_rejects_unknown_actions_and_bad_headers() -> None:
    from aiohttp import web

    manager = ConfigManager()
    await manager.load()
    dash = _dashboard(manager)

    with pytest.raises(web.HTTPBadRequest):
        await dash._control(FakeRequest({"action": "self_destruct"}))
    with pytest.raises(web.HTTPForbidden):
        await dash._control(FakeRequest({"action": "pause"}, headers={}))


async def test_every_advertised_setting_is_a_real_config_key() -> None:
    """A typo in the SETTINGS table would render a control that can never save."""
    manager = ConfigManager()
    await manager.load()
    flat = manager.flat()

    for spec in Dashboard.SETTINGS:
        assert spec["key"] in flat, f"{spec['key']} is not a config key"
        assert spec["type"] in ("bool", "number", "choice")
        if spec["type"] == "choice":
            assert spec.get("choices"), f"{spec['key']} has no choices"


def test_settings_table_excludes_host_and_port() -> None:
    """Editing these from the page could make the page itself unreachable."""
    keys = {s["key"] for s in Dashboard.SETTINGS}
    assert "ui.host" not in keys and "ui.port" not in keys


def test_tray_icon_image_renders_at_small_sizes() -> None:
    for size in (16, 32, 64):
        image = desktop._icon_image(size)
        assert image.size == (size, size)
        assert image.mode == "RGBA"


def test_app_config_still_round_trips_with_the_new_section() -> None:
    dumped = AppConfig().model_dump()
    assert "ui" in dumped
    assert AppConfig.model_validate(dumped).ui.tray is True
