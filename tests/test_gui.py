"""Tests for the desktop shell: single-instance handoff and window control.

No window is created and no process is spawned. What's under test is the wiring
that decides *whether* to create one, and the contract the window depends on:

* a second launch must find the first instance rather than starting a rival
* something else on the port must not be mistaken for us
* close means hide, and hide must not stop the watcher
* the page must not offer window controls that nothing is listening to
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_desktop import FakeRequest, FakeWatcher, _json_of

from dropwatch import ipc
from dropwatch.config import AppConfig, ConfigManager
from dropwatch.web import Dashboard, ShellHooks

# ------------------------------------------------------------ single instance


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


def test_base_url_asks_localhost_for_a_wildcard_bind() -> None:
    # A server bound to 0.0.0.0 answers on 127.0.0.1; connecting *to* 0.0.0.0 is
    # not portable, so the probe must rewrite it.
    assert ipc.base_url("0.0.0.0", 8787) == "http://127.0.0.1:8787"
    assert ipc.base_url("", 8787) == "http://127.0.0.1:8787"
    assert ipc.base_url("127.0.0.1", 9000) == "http://127.0.0.1:9000"


def test_probe_identifies_a_running_instance(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        ipc.urllib.request, "urlopen",
        lambda *a, **k: FakeResponse({"app": "dropwatch", "pid": 42, "windowed": True}),
    )
    found = ipc.probe("127.0.0.1", 8787)
    assert found is not None
    assert found["pid"] == 42


def test_probe_rejects_a_stranger_on_our_port(monkeypatch: Any) -> None:
    # Something answering on 8787 is not evidence it is us. Treating it as ours
    # would make the second launch silently do nothing at all.
    monkeypatch.setattr(
        ipc.urllib.request, "urlopen", lambda *a, **k: FakeResponse({"app": "jenkins"})
    )
    assert ipc.probe("127.0.0.1", 8787) is None


def test_probe_treats_a_dead_port_as_no_instance(monkeypatch: Any) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(ipc.urllib.request, "urlopen", boom)
    assert ipc.probe("127.0.0.1", 8787) is None


def test_probe_survives_a_garbage_response(monkeypatch: Any) -> None:
    class Garbage(FakeResponse):
        def read(self) -> bytes:
            return b"<html>not json</html>"

    monkeypatch.setattr(ipc.urllib.request, "urlopen", lambda *a, **k: Garbage(None))
    assert ipc.probe("127.0.0.1", 8787) is None


def test_second_launch_raises_the_window_of_a_windowed_instance(monkeypatch: Any) -> None:
    sent: list[str] = []
    monkeypatch.setattr(ipc, "probe", lambda *a, **k: {"app": "dropwatch", "windowed": True})
    monkeypatch.setattr(ipc, "control", lambda h, p, action, **k: sent.append(action) or True)

    assert ipc.show_running_instance("127.0.0.1", 8787) is not None
    assert sent == ["show"]


def test_second_launch_does_not_signal_a_headless_instance(monkeypatch: Any) -> None:
    # A console `serve` holds the port but has no window. Asking it to "show"
    # would be answered with an error the launcher then has to explain away.
    sent: list[str] = []
    monkeypatch.setattr(ipc, "probe", lambda *a, **k: {"app": "dropwatch", "windowed": False})
    monkeypatch.setattr(ipc, "control", lambda h, p, action, **k: sent.append(action) or True)

    assert ipc.show_running_instance("127.0.0.1", 8787) is not None
    assert sent == []


def test_control_never_raises_when_nothing_is_listening(monkeypatch: Any) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("no route to host")

    monkeypatch.setattr(ipc.urllib.request, "urlopen", boom)
    assert ipc.control("127.0.0.1", 8787, "show") is False


# ------------------------------------------------------------ window control


def _dashboard_with(hooks: ShellHooks, watcher: Any = None) -> Dashboard:
    manager = ConfigManager.__new__(ConfigManager)
    manager._current = AppConfig()  # noqa: SLF001
    manager._overrides = {}  # noqa: SLF001
    manager._store = None  # noqa: SLF001
    return Dashboard(
        watcher=watcher or FakeWatcher(),
        bus=None,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        drops=None,  # type: ignore[arg-type]
        config=manager,
        hooks=hooks,
    )


async def test_ping_identifies_us_and_reports_whether_there_is_a_window() -> None:
    dash = _dashboard_with(ShellHooks(windowed=True))
    body = await _json_of(await dash._ping(FakeRequest()))  # noqa: SLF001
    assert body["app"] == "dropwatch"
    assert body["windowed"] is True
    assert isinstance(body["pid"], int)


async def test_ping_reports_headless_for_a_plain_serve() -> None:
    dash = _dashboard_with(ShellHooks())
    body = await _json_of(await dash._ping(FakeRequest()))  # noqa: SLF001
    assert body["windowed"] is False


@pytest.mark.parametrize("action", ["show", "hide", "minimize", "maximize"])
async def test_window_actions_reach_their_hook(action: str) -> None:
    called: list[str] = []
    hooks = ShellHooks(
        windowed=True,
        show=lambda: called.append("show"),
        hide=lambda: called.append("hide"),
        minimize=lambda: called.append("minimize"),
        maximize=lambda: called.append("maximize"),
    )
    dash = _dashboard_with(hooks)
    body = await _json_of(await dash._control(FakeRequest({"action": action})))  # noqa: SLF001
    assert body["ok"] is True
    assert called == [action]


@pytest.mark.parametrize("action", ["show", "hide", "minimize", "maximize"])
async def test_window_actions_decline_politely_with_no_window(action: str) -> None:
    # `serve` has no window. These must report that rather than raising a 500,
    # so a page left open across a restart degrades instead of erroring.
    dash = _dashboard_with(ShellHooks())
    body = await _json_of(await dash._control(FakeRequest({"action": action})))  # noqa: SLF001
    assert body["ok"] is False
    assert action in body["error"]


async def test_hiding_the_window_does_not_touch_the_watcher() -> None:
    # The whole premise of close-to-tray: the window is a view, not the app.
    watcher = FakeWatcher()
    hooks = ShellHooks(windowed=True, hide=lambda: None)
    dash = _dashboard_with(hooks, watcher)
    await dash._control(FakeRequest({"action": "hide"}))  # noqa: SLF001
    assert watcher.paused is False
    assert watcher.resumed is False


async def test_window_actions_still_require_the_csrf_header() -> None:
    hooks = ShellHooks(windowed=True, hide=lambda: None)
    dash = _dashboard_with(hooks)
    from aiohttp import web

    with pytest.raises(web.HTTPForbidden):
        await dash._control(FakeRequest({"action": "hide"}, headers={}))  # noqa: SLF001


async def test_open_is_restricted_to_known_pages() -> None:
    dash = _dashboard_with(ShellHooks())
    from aiohttp import web

    with pytest.raises(web.HTTPBadRequest):
        await dash._control(  # noqa: SLF001
            FakeRequest({"action": "open", "what": "http://evil.example/"})
        )


# ------------------------------------------------------------------- signing in


class FakeAuth:
    def __init__(self, authenticated: bool = False) -> None:
        self.authenticated = authenticated
        self.tokens = None


async def test_auth_endpoint_reports_signed_out_so_the_page_can_gate() -> None:
    dash = _dashboard_with(ShellHooks())
    dash._auth = FakeAuth(authenticated=False)  # noqa: SLF001
    body = await _json_of(await dash._auth_get(FakeRequest()))  # noqa: SLF001
    assert body["supported"] is True
    assert body["authenticated"] is False
    assert body["flow"] is None


async def test_auth_endpoint_is_inert_when_auth_was_not_wired_in() -> None:
    # `serve` constructs the Dashboard without an AuthManager. The page must read
    # that as "nothing to gate", not as "signed out" — otherwise a working
    # console run would render behind a sign-in wall it can never dismiss.
    dash = _dashboard_with(ShellHooks())
    body = await _json_of(await dash._auth_get(FakeRequest()))  # noqa: SLF001
    assert body["supported"] is False
    assert body["authenticated"] is False


def test_the_icon_is_found_from_the_bundle_not_the_state_dir(monkeypatch: Any) -> None:
    """Regression: DROPWATCH_HOME must not decide whether the icon is found.

    The icon is a shipped, read-only asset, so it lives with the bundle. Looking
    for it under ROOT meant that relocating state moved the lookup somewhere the
    file had never been — and the window silently wore python.exe's icon instead
    of failing in any visible way.
    """
    from dropwatch import paths

    monkeypatch.setattr(paths, "ROOT", paths.ROOT / "nowhere-in-particular")
    found = paths.icon_file()
    assert found is not None, "the icon must still be found with state relocated"
    assert found.is_file()
    assert found.suffix == ".ico"


def test_icon_image_renders_a_mark_at_every_shipped_size() -> None:
    from dropwatch.desktop import icon_image

    for size in (16, 32, 256):
        image = icon_image(size)
        assert image.size == (size, size)
        assert image.mode == "RGBA"
        # Not a blank square: the mark has to actually be drawn on it.
        assert image.getbbox() is not None


async def test_a_non_expiring_token_reports_no_expiry() -> None:
    # Twitch's TV-client tokens never expire, and computing "seconds remaining"
    # from their zero timestamp yields a huge negative that renders as "expired".
    class Tokens:
        login = "someone"
        user_id = "1"
        never_expires = True
        seconds_remaining = -1785551729.0

    dash = _dashboard_with(ShellHooks())
    auth = FakeAuth(authenticated=True)
    auth.tokens = Tokens()  # type: ignore[assignment]
    dash._auth = auth  # noqa: SLF001

    body = await _json_of(await dash._auth_get(FakeRequest()))  # noqa: SLF001
    assert body["never_expires"] is True
    assert body["expires_in"] is None


# ------------------------------------------------- the add-account device flow


class FakeFlow:
    user_code = "ABCD1234"
    verification_uri = "https://www.twitch.tv/activate"
    seconds_remaining = 1800.0
    device_code = "dc"
    interval = 5.0


class SlowAuth:
    """An AuthManager whose device flow never completes."""

    def __init__(self) -> None:
        self.authenticated = False
        self.tokens = None
        self.cancelled = False

    async def start_device_flow(self) -> Any:
        return FakeFlow()

    async def complete_device_flow(self, _flow: Any) -> Any:
        import asyncio

        try:
            await asyncio.sleep(3600)  # nobody ever types the code
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_an_unused_device_code_times_out_instead_of_hanging_around(
    monkeypatch: Any,
) -> None:
    """Changing your mind must not leave a dead code on screen.

    Twitch keeps the code alive for 30 minutes, but it stops being useful the
    moment nobody is acting on it — and a stale "in progress" banner blocks the
    UI while claiming something is still happening.
    """
    import asyncio

    from dropwatch import web as web_mod

    monkeypatch.setattr(web_mod, "FLOW_TIMEOUT", 0.15)
    dash = _dashboard_with(ShellHooks())
    auth = SlowAuth()
    dash._auth = auth  # noqa: SLF001

    await dash._auth_login(FakeRequest({}))  # noqa: SLF001
    assert dash._flow_state == "pending"  # noqa: SLF001

    await asyncio.sleep(0.4)
    assert dash._flow_state == "timeout"  # noqa: SLF001
    # And the countdown the page shows has actually reached zero.
    body = await _json_of(await dash._auth_get(FakeRequest()))  # noqa: SLF001
    assert body["flow"]["expires_in"] == 0


async def test_the_countdown_reports_our_deadline_not_twitchs(monkeypatch: Any) -> None:
    # Twitch's code lives 1800s; we watch it for FLOW_TIMEOUT. Showing Twitch's
    # number would promise 28 minutes of waiting that is not going to happen.
    from dropwatch import web as web_mod

    monkeypatch.setattr(web_mod, "FLOW_TIMEOUT", 120.0)
    dash = _dashboard_with(ShellHooks())
    dash._auth = SlowAuth()  # noqa: SLF001

    await dash._auth_login(FakeRequest({}))  # noqa: SLF001
    body = await _json_of(await dash._auth_get(FakeRequest()))  # noqa: SLF001
    assert 0 < body["flow"]["expires_in"] <= 120.0
    assert FakeFlow.seconds_remaining == 1800.0  # unchanged, and deliberately unused
    await dash._auth_cancel(FakeRequest({}))  # noqa: SLF001


async def test_cancelling_clears_the_flow_and_allows_another(monkeypatch: Any) -> None:
    from dropwatch import web as web_mod

    monkeypatch.setattr(web_mod, "FLOW_TIMEOUT", 120.0)
    dash = _dashboard_with(ShellHooks())
    auth = SlowAuth()
    dash._auth = auth  # noqa: SLF001

    await dash._auth_login(FakeRequest({}))  # noqa: SLF001
    # Let the polling task actually start; cancelling a task that has not run
    # yet would never reach the code under test.
    import asyncio

    await asyncio.sleep(0.05)

    body = await _json_of(await dash._auth_cancel(FakeRequest({})))  # noqa: SLF001
    assert body["flow"] is None
    assert auth.cancelled is True, "the polling coroutine must actually be stopped"

    # A cancelled flow must not block the next attempt.
    await dash._auth_login(FakeRequest({}))  # noqa: SLF001
    assert dash._flow_state == "pending"  # noqa: SLF001
    await dash._auth_cancel(FakeRequest({}))  # noqa: SLF001


async def test_cancel_requires_the_csrf_header() -> None:
    from aiohttp import web

    dash = _dashboard_with(ShellHooks())
    with pytest.raises(web.HTTPForbidden):
        await dash._auth_cancel(FakeRequest({}, headers={}))  # noqa: SLF001
