"""CLI entrypoint.

Phase 1 surface. The Discord bot (phase 5) will call the same App and AuthManager;
these commands exist so auth can be driven and verified before the bot exists.

    dropwatch login      authorise this machine via the device code flow
    dropwatch whoami     show the stored token's identity and expiry
    dropwatch refresh    force a token refresh now
    dropwatch logout     delete stored tokens
    dropwatch config     show / set / unset runtime configuration
    dropwatch doctor     check the local install and report what's missing

    dropwatch gql-check  validate every GraphQL transport against live Twitch
    dropwatch live       are these channels live, and can they credit drops
    dropwatch discover   live drops-enabled channels in the category
    dropwatch watch      report watching one channel and show what Twitch credits
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time

from . import __version__, paths
from .app import App
from .config import ConfigError, ConfigManager, parse_override_value
from .desktop import (
    Tray,
    clear_pid,
    detach_and_exit,
    open_channel,
    open_url,
    process_alive,
    read_pid,
    terminate,
    write_pid,
)
from .events import Event, EventType
from .log import attach_parent_console
from .store import Store
from .twitch.auth import AuthError, AuthNeededError, TokenSet
from .twitch.drops import SessionProgress
from .twitch.gql import GQLError, PersistedQueryNotFound
from .web import Dashboard

EXIT_OK = 0
EXIT_ERROR = 1


def _fmt_expiry(tokens: TokenSet) -> str:
    """Twitch issues non-expiring tokens to TV clients; don't render that as '0s'."""
    if tokens.never_expires:
        return "does not expire"
    return _fmt_duration(tokens.seconds_remaining)


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


# ------------------------------------------------------------------- commands


async def cmd_login(args: argparse.Namespace) -> int:
    async with App() as app:
        # With accounts already set up, `login` means "add another one" — the
        # alternative is overwriting a working account by typing one command.
        target = app.get(args.account) if args.account else None
        if args.account and target is None:
            print(f"No account named {args.account!r}.", file=sys.stderr)
            return EXIT_ERROR
        if target is None:
            target = app.primary if not app.primary.authenticated else None
        if target is None:
            target = await app.add_account("")
            print("\n  Adding another account. The existing ones keep farming.")

        if target.authenticated and not args.force:
            tokens = target.auth.tokens
            assert tokens is not None
            print(f"Already authenticated as {tokens.login}. Use --force to re-login.")
            return EXIT_OK

        flow = await target.auth.start_device_flow()
        print()
        print("  Authorise this bot on Twitch:")
        print()
        print(f"    1. Open  {flow.verification_uri}")
        print(f"    2. Enter code  {flow.user_code}")
        print()
        print(f"  Waiting for approval (expires in {_fmt_duration(flow.seconds_remaining)})...")

        try:
            await target.auth.complete_device_flow(flow)
        except AuthError as exc:
            print(f"\n  Login failed: {exc}", file=sys.stderr)
            return EXIT_ERROR

        tokens = target.auth.tokens
        assert tokens is not None
        name = (tokens.login or "").lower()
        if name:
            target.claim(name)
            await app.registry.set_enabled(name, True)
        print(f"\n  Authenticated as {tokens.login} (user id {tokens.user_id})")
        print(f"  Token {_fmt_expiry(tokens)}")
        print(f"  Stored at {paths.token_path(name) if name else paths.ACCOUNTS_DIR}")
        total = len(app.registry.enabled())
        if total > 1:
            print(f"  {total} accounts will farm together.")
        return EXIT_OK


async def cmd_whoami(args: argparse.Namespace) -> int:
    async with App() as app:
        account = app.get(getattr(args, "account", None))
        if account is None:
            print(f"No account named {args.account!r}.", file=sys.stderr)
            return EXIT_ERROR
        tokens = account.auth.tokens
        if tokens is None:
            print("Not authenticated. Run `dropwatch login`.")
            return EXIT_ERROR

        # Round-trip to Twitch so this reports reality, not just what's on disk.
        try:
            await account.auth.validate()
        except AuthError as exc:
            print(f"Stored token is not valid: {exc}", file=sys.stderr)
            return EXIT_ERROR

        tokens = account.auth.tokens
        assert tokens is not None
        fraction = app.config.twitch.refresh_at_lifetime_fraction
        print(f"  Login        {tokens.login}")
        print(f"  User ID      {tokens.user_id}")
        print(f"  Scopes       {', '.join(tokens.scopes) or '(none)'}")
        print(f"  Expiry       {_fmt_expiry(tokens)}")
        if not tokens.never_expires:
            print(f"  Refresh at   {int(fraction * 100)}% of lifetime "
                  f"({'due now' if tokens.needs_refresh(fraction) else 'not yet due'})")
        print(f"  Refreshable  {'yes' if tokens.refresh_token else 'no'}")
        return EXIT_OK


async def cmd_refresh(_: argparse.Namespace) -> int:
    async with App() as app:
        try:
            tokens = await app.auth.refresh()
        except AuthError as exc:
            print(f"Refresh failed: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"Refreshed. Valid for {_fmt_duration(tokens.seconds_remaining)}.")
        return EXIT_OK


async def cmd_logout(args: argparse.Namespace) -> int:
    async with App() as app:
        account = app.get(getattr(args, "account", None))
        if account is None:
            print(f"No account named {args.account!r}.", file=sys.stderr)
            return EXIT_ERROR
        had_token = account.auth.authenticated
        login = account.auth.tokens.login if account.auth.tokens else None

        revoked = await account.auth.logout(revoke=not args.local_only)

        if not had_token:
            print("No stored token — nothing to remove.")
            return EXIT_OK

        print(f"\n  Removed local tokens for {login or 'unknown account'}.")
        if args.local_only:
            print("  Left the token valid at Twitch (--local-only).")
        elif revoked:
            print("  Revoked at Twitch — the token no longer works.")
        else:
            print("  Could NOT revoke at Twitch; the token may still be valid.")

        # The OAuth grant and the Battle.net link are separate things, and only
        # the second one governs whether drops can be earned at all.
        print("\n  This does not unlink Twitch from Battle.net. To review or remove")
        print("  either connection:")
        print("      https://www.twitch.tv/settings/connections\n")
        return EXIT_OK


async def cmd_config(args: argparse.Namespace) -> int:
    async with App() as app:
        manager = app.config_manager

        if args.config_action == "show":
            overrides = manager.overrides
            manager_accounts = app.registry.list()
            print()
            if args.paths:
                print("  Where everything lives")
                print(f"  {'-' * 68}")
                accounts = ", ".join(a.name for a in manager_accounts) or "none yet"
                for label, path, note in (
                    ("state root", paths.ROOT, "DROPWATCH_HOME overrides this"),
                    ("config.toml", paths.CONFIG_FILE, "editable defaults"),
                    (".env", paths.ENV_FILE, "secrets only — DISCORD_TOKEN"),
                    ("database", paths.DB_PATH, "sessions, transitions, overrides"),
                    ("accounts", paths.ACCOUNTS_DIR,
                     f"one token file per account — {accounts}"),
                ):
                    mark = "ok" if path.exists() else "--"
                    print(f"  [{mark}] {label:<12} {path}")
                    print(f"       {'':<12} {note}")
                print()

            section = ""
            for key, value in manager.flat().items():
                top = key.split(".")[0]
                if top != section:
                    section = top
                    print(f"\n  [{top}]")
                mark = "  <-- override" if key in overrides else ""
                shown = key.split(".", 1)[1] if "." in key else key
                print(f"    {shown:<38} {value!r}{mark}")

            print()
            if overrides:
                print("  Values marked <-- override are stored in the database and layered")
                print("  on top of config.toml. Clear one with `dropwatch config unset <key>`.")
            else:
                print("  No runtime overrides — this is config.toml plus defaults.")
            print("\n  Secrets are deliberately absent from this output; check `doctor` "
                  "for\n  whether they are set.\n")
            return EXIT_OK

        if args.config_action == "set":
            try:
                await manager.set_override(args.key, parse_override_value(args.value))
            except ConfigError as exc:
                print(f"{exc}", file=sys.stderr)
                return EXIT_ERROR
            print(f"  {args.key} = {manager.flat().get(args.key)!r}")
            return EXIT_OK

        if args.config_action == "unset":
            await manager.clear_override(args.key)
            print(f"  {args.key} reset to {manager.flat().get(args.key)!r}")
            return EXIT_OK

        return EXIT_ERROR


async def cmd_doctor(_: argparse.Namespace) -> int:
    """Report on local setup — the things that silently break phase 2 onward."""
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  [{'ok' if passed else 'XX'}] {label:<28} {detail}")

    print(f"\ndropwatch v{__version__}  (Python {sys.version.split()[0]})\n")
    check(
        "config.toml",
        paths.CONFIG_FILE.exists(),
        str(paths.CONFIG_FILE) if paths.CONFIG_FILE.exists()
        else "missing — copy config.example.toml",
    )
    check(
        ".env",
        paths.ENV_FILE.exists(),
        str(paths.ENV_FILE) if paths.ENV_FILE.exists()
        else "missing — copy .env.example (optional in phase 1)",
    )

    async with App() as app:
        check("database", paths.DB_PATH.exists(), str(paths.DB_PATH))

        tokens = app.auth.tokens
        if tokens is None:
            check("twitch auth", False, "not authenticated — run `dropwatch login`")
        else:
            reachable = True
            try:
                await app.auth.validate()
            except AuthError as exc:
                reachable = False
                check("twitch auth", False, f"stored but invalid: {exc}")
            if reachable:
                assert app.auth.tokens is not None
                check("twitch auth", True,
                      f"{app.auth.tokens.login}, {_fmt_expiry(app.auth.tokens)}")

        cfg = app.config

        # One cheap call proves the whole GraphQL path — Client-ID, transport
        # choice and response parsing — without walking the registry.
        try:
            probe = await app.channels.fetch("twitch")
            check("twitch graphql", probe.channel_id is not None,
                  f"reachable, sending {'documents' if cfg.twitch.prefer_documents else 'hashes'}")
        except (GQLError, AuthError, TimeoutError) as exc:
            check("twitch graphql", False,
                  f"{type(exc).__name__}: {exc} — try `dropwatch gql-check`")

        try:
            spade_url = await app.spade.resolve_url()
            check("telemetry endpoint", app.spade.scraped,
                  spade_url if app.spade.scraped
                  else f"could not resolve from Twitch; using fallback {spade_url}")
        except (TimeoutError, OSError) as exc:
            check("telemetry endpoint", False, str(exc))

        # The one that silently earns nothing. Checked per account, because with
        # a fleet it is entirely normal for one to be linked and another not.
        for account in app.accounts:
            if not account.ready:
                continue
            try:
                campaigns = await account.drops.inventory()
            except (GQLError, AuthError, TimeoutError) as exc:
                check(f"battle.net link ({account.name})", False,
                      f"could not read inventory: {type(exc).__name__}")
                continue
            linked, fix = account.drops.link_status(campaigns)
            label = f"battle.net link ({account.name})" if len(app.accounts) > 1 \
                else "battle.net link"
            if linked is True:
                check(label, True, "connected — drops can be credited")
            elif linked is False:
                check(label, False,
                      f"NOT linked — Twitch will credit 0 minutes. Fix: {fix}")
            else:
                # No campaign in progress says either way. Saying "not linked"
                # here would alarm someone whose setup is simply between
                # campaigns, so report the uncertainty as uncertainty.
                print(f"  [--] {label:<28} no active campaign to check against")

        channels = cfg.watch.ordered()
        check("watch targets", bool(channels) or cfg.watch.auto_discovery,
              ", ".join(c.login for c in channels) or "none listed; auto-discovery on")
        check("discord config", cfg.discord.configured,
              "configured" if cfg.discord.configured else "owner_id unset — needed for phase 5")
        check("discord token", app.secrets.discord_token is not None,
              "present" if app.secrets.discord_token else "unset — needed for phase 5")

    print()
    return EXIT_OK if ok else EXIT_ERROR


async def cmd_gqlcheck(_: argparse.Namespace) -> int:
    """Validate every persisted-query hash against live Twitch.

    The hashes rotate on Twitch's schedule, not ours, so this is the command that
    turns "the bot mysteriously reports zero progress" into a specific, fixable
    line of output naming the operation that broke.
    """
    # Minimal valid variables per operation, enough to get past validation.
    probes: dict[str, dict[str, object]] = {
        "UseLive": {"channelLogin": "twitch"},
        "StreamMetadata": {"channelLogin": "twitch"},
        "PlaybackAccessToken": {
            "isLive": True, "login": "twitch", "isVod": False,
            "vodID": "", "playerType": "site",
        },
        "DropCurrentSessionContext": {},
        "Inventory": {"fetchRewardCampaigns": False},
        "ViewerDropsDashboard": {"fetchRewardCampaigns": False},
        "DirectoryPage_Game": {
            "limit": 1, "slug": "overwatch-2",
            "options": {
                "includeRestricted": ["SUB_ONLY_LIVE"], "sort": "RELEVANCE",
                "tags": [], "freeformTags": None, "systemFilters": ["DROPS_ENABLED"],
                "recommendationsContext": {"platform": "web"},
            },
            "cursor": None,
        },
    }
    # Mutations are destructive, so they're reported rather than fired.
    skip = {"DropsPage_ClaimDropRewards"}

    async def try_path(app: App, name: str, variables: dict, *, document: bool) -> str:
        """Run one operation by one transport and describe the outcome."""
        op = app.gql.operations[name]
        if document and not op.document:
            return "n/a"
        if not document and not op.sha256:
            return "n/a"
        payload = (
            op.document_payload(dict(variables)) if document
            else op.persisted_payload(dict(variables))
        )
        try:
            body = await app.gql._post(payload, op)  # noqa: SLF001 — diagnostic path
            app.gql._unwrap(body, op)  # noqa: SLF001
        except PersistedQueryNotFound:
            return "STALE"
        except (GQLError, AuthError, TimeoutError) as exc:
            return f"{type(exc).__name__}: {str(exc)[:60]}"
        return "ok"

    usable = True
    async with App() as app:
        ops = app.gql.operations
        print(f"\nChecking {len(ops)} GraphQL operations against live Twitch")
        print("Documents are the primary transport; hashes are the fallback.\n")
        print(f"  {'operation':<28} {'document':<14} {'hash':<14} verdict")
        print(f"  {'-' * 28} {'-' * 14} {'-' * 14} {'-' * 24}")

        for name in sorted(ops):
            if name in skip:
                print(f"  {name:<28} {'—':<14} {'—':<14} skipped (mutation)")
                continue
            variables = probes.get(name)
            if variables is None:
                print(f"  {name:<28} {'—':<14} {'—':<14} no probe defined")
                continue

            doc_result = await try_path(app, name, variables, document=True)
            hash_result = await try_path(app, name, variables, document=False)

            works = doc_result == "ok" or hash_result == "ok"
            usable = usable and works
            verdict = "usable" if works else "BROKEN — needs a fix"
            if doc_result == "ok" and hash_result != "ok":
                verdict = "usable (document only)"
            print(f"  {name:<28} {doc_result[:13]:<14} {hash_result[:13]:<14} {verdict}")

    if not usable:
        print(
            "\n  At least one operation has no working transport.\n"
            "  A stale hash can be corrected without a code change — put the current\n"
            f"  value in {paths.DATA_DIR / 'gql_operations.json'} as\n"
            '  {"OperationName": "hash"}. A broken document needs its fields updated.\n'
        )
    else:
        print("\n  Every operation has a working transport.\n")
    return EXIT_OK if usable else EXIT_ERROR


async def cmd_live(args: argparse.Namespace) -> int:
    """Report the state of the given channels, or the configured priority list."""
    async with App() as app:
        logins = args.channels or [c.login for c in app.config.watch.ordered()]
        if not logins:
            print("No channels given and none configured.")
            return EXIT_ERROR

        print()
        for login in logins:
            try:
                info = await app.channels.fetch(login, detailed=True)
            except (GQLError, AuthError) as exc:
                print(f"  [XX] {login:<24} {type(exc).__name__}: {exc}")
                continue
            mark = "ok" if info.drops_eligible else ("~~" if info.live else "--")
            print(f"  [{mark}] {login:<24} {info.describe()}")
            if info.live:
                up = info.uptime_seconds
                print(f"       {'':<24} channel_id={info.channel_id} "
                      f"broadcast_id={info.stream_id}"
                      + (f" up {_fmt_duration(up)}" if up else ""))
        print()
        return EXIT_OK


async def cmd_discover(args: argparse.Namespace) -> int:
    """List live, drops-enabled channels in the configured category."""
    async with App() as app:
        try:
            found = await app.channels.discover(limit=args.limit)
        except (GQLError, AuthError) as exc:
            print(f"Discovery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_ERROR

        if not found:
            print(f"\n  No drops-enabled live channels in {app.config.watch.game_slug!r}.\n")
            return EXIT_OK

        print(f"\n  {len(found)} drops-enabled live channel(s) in "
              f"{app.config.watch.game_slug!r}:\n")
        for info in found:
            print(f"  {info.viewers or 0:>7,}  {info.login:<24} {info.game or ''}")
        print()
        return EXIT_OK


async def cmd_watch(args: argparse.Namespace) -> int:
    """Phase 2 gate: report watching to Twitch and prove the minutes credit.

    This is deliberately a single fixed target with no rotation — rotation is
    phase 3. The only question it answers is whether Twitch counts what we send.
    """
    async with App() as app:
        if not app.auth.authenticated:
            print("Not authenticated — run `dropwatch login` first.", file=sys.stderr)
            return EXIT_ERROR
        tokens = app.auth.tokens
        assert tokens is not None
        if not tokens.user_id:
            print("Stored token has no user id — re-run `dropwatch login`.", file=sys.stderr)
            return EXIT_ERROR

        login = args.channel.strip().lower()
        info = await app.channels.fetch(login, detailed=True)
        if not info.live:
            print(f"{login} is offline — nothing to watch.", file=sys.stderr)
            return EXIT_ERROR
        if not info.drops_eligible:
            print(f"{login} is live but {info.describe()}.", file=sys.stderr)
            if not args.force:
                print("Refusing to watch an ineligible stream; pass --force to override.",
                      file=sys.stderr)
                return EXIT_ERROR

        url = await app.spade.resolve_url()
        print(f"\n  Watching   {info.login}  ({info.describe()})")
        print(f"  As         {tokens.login} (id {tokens.user_id})")
        print(f"  Telemetry  {url}")
        print(f"  Interval   ~{app.config.watch.minute_watched_interval:.0f}s "
              f"±{app.config.watch.minute_watched_jitter:.0f}s")

        baseline = await _read_progress(app, info.login)
        if baseline is not None:
            print(f"  Baseline   {baseline.describe()}")
        print("\n  Ctrl-C to stop.\n")

        cycles = 0
        started = time.monotonic()
        try:
            while args.cycles == 0 or cycles < args.cycles:
                result = await app.spade.send_minute_watched(info, tokens.user_id)
                cycles += 1
                stamp = time.strftime("%H:%M:%S")
                elapsed = _fmt_duration(time.monotonic() - started)

                line = f"  {stamp}  cycle {cycles:<4} {result.describe():<26} {elapsed}"
                if cycles % app.config.watch.progress_check_every == 0:
                    progress = await _read_progress(app, info.login)
                    if progress is not None:
                        line += f"  |  {progress.describe()}"
                        if baseline and progress.current_minutes > baseline.current_minutes:
                            gained = progress.current_minutes - baseline.current_minutes
                            line += f"  (+{gained} since start)"
                print(line)

                if args.cycles and cycles >= args.cycles:
                    break
                await asyncio.sleep(app.spade.next_interval())
        except KeyboardInterrupt:
            print("\n  Stopped.")

        final = await _read_progress(app, info.login)
        print(f"\n  {cycles} cycle(s) sent over {_fmt_duration(time.monotonic() - started)}.")

        if final is None:
            print("  Twitch reports no active drop session, so nothing could be credited.\n"
                  "  Check twitch.tv/drops/inventory — usually the account isn't linked to\n"
                  "  Battle.net, or no campaign covers this channel right now.")
            return EXIT_ERROR

        # A missing baseline is normal: the drop session only exists once we start
        # reporting, so on a cold start there is nothing to diff against.
        if baseline is None:
            print(f"  Session opened during this run: {final.describe()}")
            return EXIT_OK

        gained = final.current_minutes - baseline.current_minutes
        print(f"  Twitch credited {gained:+d} minute(s): "
              f"{baseline.current_minutes} -> {final.current_minutes}")
        if gained <= 0 and cycles >= 3:
            print("\n  Nothing credited across the whole run. That is the phase 2 gate\n"
                  "  failing — the telemetry is being accepted but not counted.")
            return EXIT_ERROR
        return EXIT_OK


async def _read_progress(app: App, login: str) -> SessionProgress | None:
    """Best-effort progress read; a failure here must not stop the watch loop."""
    try:
        progress = await app.drops.current_session(login)
    except (GQLError, AuthError) as exc:
        log_once = getattr(_read_progress, "_warned", False)
        if not log_once:
            print(f"       (progress unavailable: {type(exc).__name__}: {exc})")
            _read_progress._warned = True  # type: ignore[attr-defined]
        return None
    return progress if progress.active else None


async def cmd_run(args: argparse.Namespace) -> int:
    """Run the watcher: pick a target, watch it, rotate when it dies.

    This is the bot. Discord (phase 5) will drive the same Watcher; the console
    output here exists so it can be operated and verified without Discord.
    """
    async with App() as app:
        if not app.auth.authenticated:
            print("Not authenticated — run `dropwatch login` first.", file=sys.stderr)
            return EXIT_ERROR
        tokens = app.auth.tokens
        assert tokens is not None
        if not tokens.user_id:
            print("Stored token has no user id — re-run `dropwatch login`.", file=sys.stderr)
            return EXIT_ERROR

        interesting = {
            EventType.WATCH_STARTED, EventType.WATCH_STOPPED,
            EventType.TARGET_SWITCHED, EventType.NO_TARGETS,
            EventType.STATE_CHANGED, EventType.STREAM_ENDED, EventType.STREAM_STALLED,
            EventType.DROP_PROGRESS, EventType.DROP_COMPLETED,
            EventType.DROP_CLAIMABLE, EventType.DROP_CLAIMED, EventType.CLAIM_BLOCKED,
            EventType.TELEMETRY_DEGRADED, EventType.SCHEMA_DRIFT, EventType.ERROR,
        }

        async def show(event: Event) -> None:
            stamp = time.strftime("%H:%M:%S")
            payload = {k: v for k, v in event.payload.items() if v is not None}
            if event.type is EventType.STATE_CHANGED:
                arrow = f"{payload.get('from_state')} -> {payload.get('to_state')}"
                signals = " ".join(
                    f"{k}={v}" for k, v in (payload.get("signals") or {}).items()
                )
                print(f"  {stamp}  {arrow:<22} {payload.get('reason', '')}")
                if signals:
                    print(f"  {'':<10}  {signals}")
            else:
                bits = " ".join(f"{k}={v}" for k, v in payload.items() if k != "signals")
                print(f"  {stamp}  {event.type:<26} {bits}")

        app.bus.subscribe(show, *interesting)

        cfg = app.config
        print(f"\n  Account    {tokens.login} (id {tokens.user_id})")
        print("  Targets    " + (", ".join(
            f"{c.login}({c.priority})" for c in cfg.watch.ordered()
        ) or "none configured"))
        print(f"  Discovery  {'on' if cfg.watch.auto_discovery else 'off'} "
              f"— category {cfg.watch.game_slug!r}")
        print(f"  Liveness   grace {cfg.liveness.grace_period:.0f}s, "
              f"confirm {cfg.liveness.confirm_reads}, "
              f"stall {cfg.liveness.stall_cycles} cycles")
        print("\n  Ctrl-C to stop.\n")

        task = asyncio.create_task(app.watcher.run(), name="watcher")

        # A periodic one-line summary, so a long run is readable without having to
        # reconstruct state from the event stream.
        async def heartbeat() -> None:
            while not task.done():
                await asyncio.sleep(args.status_every)
                if task.done():
                    return
                s = app.watcher.status()
                gain = s.stats.credited_gain
                print(
                    f"  {time.strftime('%H:%M:%S')}  [{s.state}] {s.channel or '—'}  "
                    f"cycles={s.stats.cycles} sent={s.stats.minutes_sent} "
                    f"credited={s.stats.credited_now}"
                    + (f"/{s.stats.required}" if s.stats.required else "")
                    + (f" (+{gain})" if gain else "")
                    + f" rotations={s.rotations} "
                    f"pubsub={'up' if s.pubsub_connected else 'down'}"
                )

        beat = asyncio.create_task(heartbeat(), name="heartbeat")
        try:
            if args.duration:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), timeout=args.duration)
                await app.watcher.stop()
            await task
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n  Stopping...")
            await app.watcher.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

        s = app.watcher.status()
        print(f"\n  {s.stats.cycles} cycle(s), {s.stats.minutes_sent} minute(s) reported, "
              f"{s.rotations} rotation(s).")
        gain = s.stats.credited_gain
        if gain is not None:
            print(f"  Twitch credited {gain:+d} minute(s) this session "
                  f"({s.stats.credited_start} -> {s.stats.credited_now}).")
        return EXIT_OK


async def cmd_drops(args: argparse.Namespace) -> int:
    """List every active campaign, its reward ladder, and what's claimable."""
    async with App() as app:
        try:
            campaigns = await app.drops.inventory()
        except (GQLError, AuthError) as exc:
            print(f"Could not read inventory: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_ERROR

        if not campaigns:
            print("\n  No active drop campaigns for this account.\n")
            return EXIT_OK

        waiting = 0
        for campaign in campaigns:
            left = campaign.hours_left
            when = (
                f"{left:.0f}h left" if left is not None and left < 48
                else f"{left / 24:.0f}d left" if left is not None else "no end date"
            )
            flag = "  <-- expiring soon" if campaign.expiring_soon else ""
            print(f"\n  {campaign.name}   [{campaign.status}] {when}{flag}")
            print(f"  {'-' * 66}")
            for drop in campaign.drops:
                if drop.claimable:
                    waiting += 1
                    state = "** CLAIM IT **"
                elif drop.claimed:
                    state = "claimed"
                else:
                    state = f"{drop.remaining_minutes} min to go"
                bar = "#" * int(drop.fraction * 12)
                print(f"    {drop.current_minutes:>4}/{drop.required_minutes:<4} "
                      f"[{bar:<12}] {state:<15} {drop.reward_name}")

        print()
        if waiting:
            print(f"  {waiting} reward(s) earned and waiting.\n")
            print("  Twitch requires a Client-Integrity token to claim, which this bot\n"
                  "  cannot produce — so claiming is a browser action:\n")
            print("      https://www.twitch.tv/drops/inventory\n")
            if args.open:
                import webbrowser
                webbrowser.open("https://www.twitch.tv/drops/inventory")
                print("  Opened in your browser.\n")
        else:
            print("  Nothing to claim right now.\n")
        return EXIT_OK


async def cmd_serve(args: argparse.Namespace) -> int:
    """Run every enabled account with a local web dashboard over all of them."""
    async with App() as app:
        farming = app.ready_accounts()
        if not farming:
            print("No account is signed in — run `dropwatch login` first.", file=sys.stderr)
            return EXIT_ERROR

        ui = app.config.ui
        # A CLI flag overrides the persisted setting, but only for this run.
        host = args.host or ui.host
        port = args.port or ui.port
        open_dashboard = ui.open_dashboard or args.open
        open_twitch = ui.open_twitch if args.open_twitch is None else args.open_twitch
        want_tray = ui.tray and not args.no_tray

        dashboard = Dashboard(
            watcher=app.watcher, bus=app.bus, store=app.store, drops=app.drops,
            config=app.config_manager, auth=app.auth, app=app,
        )
        try:
            url = await dashboard.start(host, port)
        except OSError as exc:
            print(f"Could not bind {host}:{port} — {exc}\n"
                  "Another instance may already be running; try --port.",
                  file=sys.stderr)
            return EXIT_ERROR

        print(f"\n  Dashboard  {url}")
        print("  Accounts   " + ", ".join(
            f"{a.name}({a.auth.tokens.user_id})" for a in farming if a.auth.tokens
        ))
        print("  Targets    " + (", ".join(
            f"{c.login}({c.priority})" for c in app.config.watch.ordered()
        ) or "none configured"))

        # Open on every target pick rather than once at startup: after a rotation
        # the old tab is showing a stream that has ended. Tracked per account, or
        # a fleet would only ever open a tab for whichever one started first.
        opened: set[str] = set()

        async def on_watch_started(event: Event) -> None:
            channel = event.get("channel")
            if not channel or not open_twitch:
                return
            who = str(event.get("account") or "")
            if who in opened and not app.config.ui.reopen_twitch_on_rotate:
                return
            opened.add(who)
            open_channel(str(channel))

        app.bus.subscribe(on_watch_started, EventType.WATCH_STARTED)

        tray: Tray | None = None
        if want_tray:
            async def pause_all() -> None:
                for account in farming:
                    await account.watcher.pause()

            async def resume_all() -> None:
                for account in farming:
                    await account.watcher.resume()

            async def stop_all() -> None:
                for account in farming:
                    await account.watcher.stop()

            def describe() -> str | None:
                live = [a.watcher.status().channel for a in farming
                        if a.watcher.status().channel]
                if not live:
                    return None
                unique = sorted(set(live))
                if len(unique) == 1:
                    return unique[0] if len(live) == 1 else f"{unique[0]} ×{len(live)}"
                return f"{len(live)} accounts, {len(unique)} channels"

            tray = Tray(
                loop=asyncio.get_running_loop(),
                dashboard_url=url,
                on_pause=pause_all,
                on_resume=resume_all,
                on_quit=stop_all,
                current_channel=describe,
                is_paused=lambda: all(a.watcher.status().paused for a in farming),
            )
            if tray.start():
                # Keep the menu labels truthful as target and state change.
                app.bus.subscribe(
                    lambda _e: tray.refresh() if tray else None,
                    EventType.WATCH_STARTED, EventType.TARGET_SWITCHED,
                    EventType.STATE_CHANGED, EventType.WATCH_STOPPED,
                )
                print("  Tray       active — right-click the icon to pause or quit")
            else:
                tray = None

        print("\n  Ctrl-C to stop.\n")

        if open_dashboard:
            open_url(url, what="dashboard")

        write_pid()
        tasks = [
            asyncio.create_task(a.watcher.run(), name=f"watcher:{a.name}")
            for a in farming
        ]
        combined = asyncio.gather(*tasks)

        async def stop_watchers() -> None:
            for account in farming:
                await account.watcher.stop()

        try:
            if args.duration:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(combined), timeout=args.duration)
                await stop_watchers()
            await combined
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n  Stopping...")
            await stop_watchers()
            with contextlib.suppress(asyncio.CancelledError):
                await combined
        finally:
            if tray is not None:
                tray.stop()
            await dashboard.stop()
            clear_pid()

        s = app.watcher.status()
        print(f"\n  {s.stats.cycles} cycle(s), {s.stats.minutes_sent} minute(s) reported, "
              f"{s.rotations} rotation(s).")
        gain = s.stats.credited_gain
        if gain is not None:
            print(f"  Twitch credited {gain:+d} minute(s) "
                  f"({s.stats.credited_start} -> {s.stats.credited_now}).")
        return EXIT_OK


async def cmd_accounts(args: argparse.Namespace) -> int:
    """List, enable, disable or forget the accounts that farm together."""
    async with App() as app:
        registry = app.registry
        action = args.accounts_action or "list"

        if action == "list":
            entries = registry.list()
            if not entries:
                print("\n  No accounts yet. Add one with:  dropwatch login\n")
                return EXIT_OK
            print()
            print(f"  {'account':<24} {'state':<10} {'status'}")
            print(f"  {'-' * 24} {'-' * 10} {'-' * 34}")
            for entry in entries:
                live = app.get(entry.name)
                if not entry.usable:
                    status = "token unreadable — re-run `dropwatch login`"
                elif live is not None and live.ready:
                    st = live.watcher.status()
                    status = f"{st.state.lower()}" + (f" {st.channel}" if st.channel else "")
                else:
                    status = "not loaded"
                print(f"  {entry.name:<24} "
                      f"{'enabled' if entry.enabled else 'disabled':<10} {status}")
            print(f"\n  {len(registry.enabled())} of {len(entries)} enabled. "
                  f"Each farms independently.\n")
            return EXIT_OK

        name = (args.name or "").lower()
        known = registry.names()
        if name not in known:
            print(f"No account named {name!r}. Known: {', '.join(known) or 'none'}",
                  file=sys.stderr)
            return EXIT_ERROR

        if action in ("enable", "disable"):
            await registry.set_enabled(name, action == "enable")
            print(f"  {name} {action}d.")
            return EXIT_OK

        if action == "remove":
            if not args.yes:
                print(f"\n  This deletes the stored token for {name!r} from this machine.")
                print("  Its watch history is kept. The Twitch grant is left alone —")
                print(f"  revoke that with:  dropwatch logout --account {name}\n")
                print("  Re-run with --yes to confirm.")
                return EXIT_ERROR
            await registry.remove(name)
            print(f"  Removed {name}.")
            return EXIT_OK

        return EXIT_ERROR


async def cmd_gui(_: argparse.Namespace) -> int:
    """Placeholder so ``gui`` appears in help and parses like every other command.

    Never actually awaited: :func:`main` intercepts ``gui`` before the event loop
    is created, because the GUI has to own the main thread and cannot run inside
    one that ``asyncio.run`` already occupies.
    """
    raise AssertionError("gui is dispatched from main(), not the event loop")


async def cmd_help(_: argparse.Namespace) -> int:
    """`help` as a subcommand, because that is what people type first."""
    build_parser().print_help()
    return EXIT_OK


async def cmd_stop(_: argparse.Namespace) -> int:
    """Stop a detached background run."""
    pid = read_pid()
    if pid is None:
        print("No recorded background process. Nothing to stop.")
        return EXIT_OK
    if not process_alive(pid):
        # Distinguish a stale file from a live process, or the next `serve` would
        # look like it was already running.
        clear_pid()
        print(f"Process {pid} is no longer running; cleared the stale record.")
        return EXIT_OK
    if terminate(pid):
        clear_pid()
        print(f"Stopped background process {pid}.")
        return EXIT_OK
    print(f"Could not stop process {pid}. Try Task Manager.", file=sys.stderr)
    return EXIT_ERROR


async def cmd_status(_: argparse.Namespace) -> int:
    """Show the last recorded transitions and sessions from the database."""
    async with App() as app:
        transitions = await app.store.recent_transitions(15)
        sessions = await app.store.recent_sessions(8)

        print("\n  Recent sessions\n")
        if not sessions:
            print("    none recorded yet")
        for row in sessions:
            started = time.strftime("%m-%d %H:%M", time.localtime(row["started_at"]))
            ended = row.get("ended_at")
            print(f"    {started}  {row['channel']:<24} "
                  f"{row.get('minutes') or 0:>4} min  "
                  f"{row.get('end_reason') or ('running' if not ended else '')}")

        print("\n  Recent transitions\n")
        if not transitions:
            print("    none recorded yet")
        for t in transitions:
            stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(t.ts))
            arrow = f"{t.from_state} -> {t.to_state}"
            print(f"    {stamp}  {(t.channel or '—'):<20} {arrow:<22} {t.reason or ''}")
            if t.signals:
                print(f"    {'':<10}  {' '.join(f'{k}={v}' for k, v in t.signals.items())}")
        print()
        return EXIT_OK


async def cmd_events(args: argparse.Namespace) -> int:
    """Smoke-test the event bus and print what auth emits. Useful while wiring."""
    async with App() as app:
        seen: list[Event] = []

        async def record(event: Event) -> None:
            seen.append(event)
            print(f"  {time.strftime('%H:%M:%S')}  {event.type:<28} {event.payload}")

        app.bus.subscribe(record)
        print("  Subscribed to all events. Triggering an auth check...\n")
        try:
            await app.auth.ensure_valid()
            print("\n  Token is valid.")
        except AuthNeededError as exc:
            print(f"\n  {exc}")
        print(f"\n  {len(seen)} event(s) observed; history holds {len(app.bus.history())}.")
        if args.transitions:
            for t in await app.store.recent_transitions(args.transitions):
                print(f"  {t.from_state} -> {t.to_state}  {t.reason}")
        return EXIT_OK


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dropwatch",
        description="Automated Twitch drop farmer for Overwatch, controlled from Discord.",
    )
    parser.add_argument("--version", action="version", version=f"dropwatch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser(
        "login",
        help="authorise an account (adds another one if you already have some)",
    )
    p_login.add_argument("--force", action="store_true", help="re-login even if already authorised")
    p_login.add_argument("--account", default=None,
                         help="re-authorise this existing account instead of adding one")
    p_login.set_defaults(func=cmd_login)

    p_whoami = sub.add_parser("whoami", help="show a stored token's identity and expiry")
    p_whoami.add_argument("--account", default=None, help="which account (default: the first)")
    p_whoami.set_defaults(func=cmd_whoami)

    p_acc = sub.add_parser("accounts", help="list and manage the accounts that farm together")
    acc_sub = p_acc.add_subparsers(dest="accounts_action")
    acc_sub.add_parser("list", help="show every account and what it is doing")
    for verb, blurb in (
        ("enable", "let this account farm again"),
        ("disable", "stop this account farming, keeping its token"),
        ("remove", "delete this account's stored token from this machine"),
    ):
        sp = acc_sub.add_parser(verb, help=blurb)
        sp.add_argument("name")
        if verb == "remove":
            sp.add_argument("--yes", action="store_true", help="skip the confirmation")
    p_acc.set_defaults(func=cmd_accounts, name=None, yes=False)
    sub.add_parser("refresh", help="force a token refresh now").set_defaults(func=cmd_refresh)
    p_logout = sub.add_parser(
        "logout", help="revoke the Twitch token and delete it locally"
    )
    p_logout.add_argument(
        "--local-only", action="store_true",
        help="only delete the local file, leaving the token valid at Twitch",
    )
    p_logout.add_argument("--account", default=None, help="which account (default: the first)")
    p_logout.set_defaults(func=cmd_logout)
    sub.add_parser("doctor", help="check local setup").set_defaults(func=cmd_doctor)

    p_cfg = sub.add_parser("config", help="show or change runtime configuration")
    cfg_sub = p_cfg.add_subparsers(dest="config_action", required=True)
    p_show = cfg_sub.add_parser("show", help="print the effective configuration")
    p_show.add_argument("--paths", action="store_true",
                        help="also show where config, tokens and the database live")
    p_set = cfg_sub.add_parser("set", help="override a value (persisted)")
    p_set.add_argument("key", help="dotted key, e.g. liveness.grace_period")
    p_set.add_argument("value", help="JSON value, e.g. 120 or true or '[\"a\"]'")
    p_unset = cfg_sub.add_parser("unset", help="clear an override")
    p_unset.add_argument("key")
    p_cfg.set_defaults(func=cmd_config)

    sub.add_parser(
        "gql-check", help="validate every GraphQL operation hash against live Twitch"
    ).set_defaults(func=cmd_gqlcheck)

    p_live = sub.add_parser("live", help="check whether channels are live and drops-eligible")
    p_live.add_argument("channels", nargs="*", help="channel logins; defaults to the config list")
    p_live.set_defaults(func=cmd_live)

    p_disc = sub.add_parser("discover", help="list live drops-enabled channels in the category")
    p_disc.add_argument("--limit", type=int, default=30, help="how many directory entries to scan")
    p_disc.set_defaults(func=cmd_discover)

    p_watch = sub.add_parser(
        "watch", help="report watching one channel and show what Twitch credits"
    )
    p_watch.add_argument("channel", help="channel login to watch")
    p_watch.add_argument("--cycles", type=int, default=0,
                         help="stop after N telemetry cycles (0 = run until Ctrl-C)")
    p_watch.add_argument("--force", action="store_true",
                         help="watch even if the stream is a rerun and cannot credit")
    p_watch.set_defaults(func=cmd_watch)

    p_run = sub.add_parser("run", help="run the watcher: watch, detect, rotate")
    p_run.add_argument("--duration", type=float, default=0,
                       help="stop after N seconds (0 = run until Ctrl-C)")
    p_run.add_argument("--status-every", type=float, default=300,
                       help="seconds between one-line status summaries")
    p_run.set_defaults(func=cmd_run)

    p_drops = sub.add_parser("drops", help="show reward ladders and what's claimable")
    p_drops.add_argument("--open", action="store_true",
                         help="open the Twitch inventory page if anything is waiting")
    p_drops.set_defaults(func=cmd_drops)

    p_serve = sub.add_parser(
        "serve", help="run the watcher with a live web dashboard on localhost"
    )
    p_serve.add_argument("--host", default=None,
                         help="bind address; overrides ui.host for this run")
    p_serve.add_argument("--port", type=int, default=None,
                         help="overrides ui.port for this run")
    p_serve.add_argument("--duration", type=float, default=0,
                         help="stop after N seconds (0 = run until Ctrl-C)")
    p_serve.add_argument("--open", action="store_true",
                         help="open the dashboard, regardless of ui.open_dashboard")
    p_serve.add_argument("--open-twitch", dest="open_twitch", action="store_true",
                         default=None, help="open the channel on Twitch when picked")
    p_serve.add_argument("--no-open-twitch", dest="open_twitch", action="store_false",
                         help="never open Twitch, regardless of ui.open_twitch")
    p_serve.add_argument("--no-tray", action="store_true",
                         help="run without a tray icon")
    p_serve.add_argument("--detach", action="store_true",
                         help="run in the background with no console window")
    p_serve.set_defaults(func=cmd_serve)

    p_gui = sub.add_parser(
        "gui", help="run as a desktop app: a window, a tray icon, and no console"
    )
    p_gui.add_argument("--host", default=None, help="bind address; overrides ui.host")
    p_gui.add_argument("--port", type=int, default=None, help="overrides ui.port")
    p_gui.set_defaults(func=cmd_gui)

    sub.add_parser(
        "stop", help="stop a detached background run"
    ).set_defaults(func=cmd_stop)

    sub.add_parser("help", help="show this help").set_defaults(func=cmd_help)

    sub.add_parser(
        "status", help="show recorded sessions and state transitions"
    ).set_defaults(func=cmd_status)

    p_events = sub.add_parser("events", help="smoke-test the event bus")
    p_events.add_argument("--transitions", type=int, default=0,
                          help="also print the last N state transitions")
    p_events.set_defaults(func=cmd_events)

    return parser


async def _resolve_bind(args: argparse.Namespace) -> tuple[str, int]:
    """Where the detached child will listen, honouring every config layer."""
    store = await Store().open()
    try:
        ui = (await ConfigManager(store).load()).current.ui
    finally:
        await store.close()
    return args.host or ui.host, args.port or ui.port


def _run_gui(args: argparse.Namespace) -> int:
    """Launch the desktop app, or hand the launch to the copy already running.

    The second double-click is the interesting case. Two watchers reporting the
    same account to Twitch is worse than one, so rather than starting a rival —
    or failing with "port in use", which reads as a bug — this finds the live
    instance, tells it to show itself, and exits quietly.
    """
    from . import gui, ipc

    host, port = asyncio.run(_resolve_bind(args))

    running = ipc.show_running_instance(host, port)
    if running is not None:
        if running.get("windowed"):
            print(f"\n  dropwatch is already running (pid {running.get('pid')}).")
            print("  Brought its window to the front.\n")
        else:
            # A headless `serve` holds the port. Nothing to raise, so say where
            # the dashboard is rather than pretending something happened.
            print(f"\n  dropwatch is already running (pid {running.get('pid')}).")
            print(f"  It has no app window — open  {ipc.base_url(host, port)}/\n")
        return EXIT_OK

    return gui.run(args, host=host, port=port)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles still default to a legacy codepage; without this the
    # em dashes and box characters in our output become mojibake.
    #
    # line_buffering matters for `watch`, which prints one line a minute for
    # hours: redirected to a file or a pipe, stdout would otherwise buffer and
    # show nothing at all until the process exited.
    # The packaged app is a windowed binary, so it starts with no stdout at all.
    # Borrow the launching terminal's console when there is one, or CLI commands
    # typed into cmd would print nothing. No console means launched from Explorer
    # or the tray, where printing nowhere is the right answer.
    attach_parent_console()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, OSError):
            pass

    raw = list(sys.argv[1:] if argv is None else argv)

    # A bare launch is a double-click, not someone who forgot the arguments.
    # `dropwatch help` still prints the command list for anyone in a terminal.
    if not raw:
        raw = ["gui"]

    args = build_parser().parse_args(raw)

    # Before the event loop, because the native message pump must own the main
    # thread and asyncio.run() would already have claimed it.
    if args.command == "gui":
        return _run_gui(args)

    # Handled before the event loop exists: detaching means spawning a fresh
    # process and exiting, so there's nothing for this one to run.
    if getattr(args, "detach", False):
        existing = read_pid()
        if existing is not None and process_alive(existing):
            print(f"Already running in the background (pid {existing}).\n"
                  "Stop it first with `dropwatch stop`.", file=sys.stderr)
            return EXIT_ERROR
        # The child resolves ui.host/ui.port the same way this does, so the parent
        # has to load the full three-layer config — a database override of the port
        # would otherwise be missed and we'd poll the wrong address.
        host, port = asyncio.run(_resolve_bind(args))
        return detach_and_exit(raw, host=host, port=port)

    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_ERROR
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
