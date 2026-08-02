# dropwatch

A desktop app that farms Twitch drops for Overwatch — on as many accounts as you
like, at once. API-only: no browser, no video decoded, ~30 MB resident per account.

**Download `dropwatch.exe`, double-click it, sign in inside the window.** That's
the whole setup. Closing the window keeps it farming in the tray.

New to this, or on a fresh Windows install? → [SETUP.md](SETUP.md)

## What works

| | |
| --- | --- |
| **Desktop app** | Frameless window, tray icon, close-to-tray, single instance — [details](#the-app) |
| **Multiple accounts** | Each with its own session, token and watcher, farming simultaneously — [details](#farming-several-accounts) |
| **Signing in** | Device-code flow inside the app; no terminal needed |
| **Watching** | Reports minute-watched telemetry; verified crediting against live Twitch |
| **Stream-end detection** | Five weighted signals with hysteresis — ad breaks and dropped sockets never trigger a rotation |
| **Rotation** | Switches target when a stream ends or stops crediting, priority list then auto-discovery |
| **Rewards** | Reads every active campaign's ladder and flags what's earned |
| **Battle.net check** | Detects the unlinked-account trap that silently credits zero minutes |
| **Packaging** | Single-file `.exe` that is also a full CLI when run from a terminal |

Not built: the Discord control layer. **Auto-claim is impossible** — Twitch gates
claiming behind an integrity token; see [Rewards and claiming](#rewards-and-claiming).

[PLAN.md](PLAN.md) has the full design and a record of what each phase actually
found, including the things that turned out not to work.

## Quick start

**Just want to use it?** Grab `dropwatch.exe` from
[Releases](https://github.com/9uhm/dropwatch/releases), put it anywhere, and
double-click. Nothing to install — no Python, no config file, no terminal.

The app opens, shows a code to type at `twitch.tv/activate`, and starts farming
the moment you approve it. No password ever touches this process. Add more
accounts from the **+ Add another account** tile whenever you like.

**Running from source instead:**

```bash
py -3.14 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"

.venv/Scripts/python -m dropwatch       # opens the app, same as the exe
```

`config.toml` is optional — without one it uses sensible defaults and
auto-discovery. Copy `config.example.toml` if you want to pin a channel list.

**Then link Twitch to Battle.net** at <https://www.twitch.tv/settings/connections>.
Without it Twitch credits zero minutes no matter how long the bot runs — it's the
single most common reason a working setup earns nothing.

The app checks this for you rather than trusting you remembered: `doctor` reports
`battle.net link` per account, and the dashboard shows a red banner with a fix
link if any campaign reports the connection missing. If no campaign is in
progress there is nothing to check against, and it says so instead of guessing.

## Commands

You don't need any of these — the app does everything through its window. They
exist for scripting, for diagnosis, and because a windowed exe run from a terminal
is still a normal CLI (see [Standalone .exe](#standalone-exe)).

| Command | Purpose |
| --- | --- |
| `dropwatch` *(no arguments)* | Open the app — window, tray icon, no console |
| `dropwatch gui` | The same thing, said explicitly |
| `dropwatch accounts` | List every account and what it is doing |
| `dropwatch accounts enable\|disable <name>` | Start or stop one account farming |
| `dropwatch accounts remove <name> --yes` | Delete an account's token from this machine |
| `dropwatch serve [--open]` | Run the watcher with the live dashboard on localhost |
| `dropwatch run` | Run the watcher, console output only |
| `dropwatch drops [--open]` | Reward ladders and what's claimable |
| `dropwatch status` | Recorded sessions and state transitions |
| `dropwatch live [channel...]` | Are these channels live and drops-eligible? |
| `dropwatch discover` | Live drops-enabled channels in the category |
| `dropwatch watch <channel>` | Watch one fixed target and show what Twitch credits |
| `dropwatch doctor` | Check local setup; reports exactly what's unconfigured |
| `dropwatch gql-check` | Validate every GraphQL transport against live Twitch |
| `dropwatch login [--force] [--account N]` | Authorise an account — adds another if you have some |
| `dropwatch whoami [--account N]` | Validate a stored token, show identity and expiry |
| `dropwatch refresh` | Force a token refresh now |
| `dropwatch logout [--local-only]` | Revoke the token at Twitch and delete it locally |
| `dropwatch config show [--paths]` | Effective config, and where the files live |
| `dropwatch config set <key> <value>` | Override a value, e.g. `set liveness.grace_period 120` |
| `dropwatch config unset <key>` | Revert to the `config.toml` value |
| `dropwatch events` | Smoke-test the event bus |

## Configuration layering

Three layers, lowest priority first:

1. **Model defaults** — [config.py](src/dropwatch/config.py)
2. **`config.toml`** — your hand-edited bootstrap values
3. **Runtime overrides** — SQLite, set via `config set` or Discord `/config set`

The bot never writes back to `config.toml`, so hand-edits are never clobbered.
`config show` displays the merge. Invalid keys and out-of-range values are
rejected at set time with a specific error, not silently ignored.

Secrets live only in the environment / `.env` and are kept off the config model
entirely, so `/config show` can be dumped into Discord without leaking a token.

## Layout

| Path | Role |
| --- | --- |
| [app.py](src/dropwatch/app.py) | Wires the shared singletons; construction/teardown order |
| [config.py](src/dropwatch/config.py) | Three-layer config, override validation |
| [store.py](src/dropwatch/store.py) | SQLite: overrides, sessions, claims, transitions |
| [events.py](src/dropwatch/events.py) | Async pub/sub; a failing handler can't stop the loop |
| [log.py](src/dropwatch/log.py) | Logging with token redaction |
| [twitch/auth.py](src/dropwatch/twitch/auth.py) | Device code flow, token store, refresh |
| [paths.py](src/dropwatch/paths.py) | Filesystem layout (`DROPWATCH_HOME` to relocate) |

`data/` holds `dropwatch.db` and `tokens.json`, and is gitignored. The token file is
written atomically and locked to the current user (`icacls` on Windows, `0600`
elsewhere).

## Twitch commands (phase 2)

```bash
dropwatch gql-check              # validate every GraphQL transport against live Twitch
dropwatch live [channel...]      # live? drops-eligible? defaults to the config list
dropwatch discover               # live drops-enabled channels in the category
dropwatch watch <channel>        # report watching and show what Twitch actually credits
dropwatch watch <channel> --cycles 7
```

`gql-check` is the one to run first when anything breaks. It reports each operation
over both transports:

```
  operation                    document       hash           verdict
  DirectoryPage_Game           ok             STALE          usable (document only)
  DropCurrentSessionContext    ok             ok             usable
  UseLive                      ok             ok             usable
```

Twitch's persisted-query hashes rotate on every web deploy, so the bot sends its
own verified query documents by default (`twitch.prefer_documents`) and keeps the
hashes only as a fallback. A stale hash is therefore cosmetic — but if you need to
correct one, put it in `data/gql_operations.json` as `{"OperationName": "hash"}`
rather than editing code.

`watch` is deliberately a single fixed target with no rotation; rotation is phase 3.
It answers one question — does Twitch credit what we send — by diffing its own
server-side minute count before and after.

## Running the bot (phase 3)

```bash
dropwatch run                     # watch, detect stream end, rotate. Ctrl-C to stop
dropwatch run --duration 3600     # stop after an hour
dropwatch run --status-every 60   # one-line summary this often
dropwatch status                  # recorded sessions and state transitions
```

`run` picks the highest-priority live, drops-eligible channel, reports watch
minutes, and rotates when the stream ends or stops crediting. Every transition is
logged with the signal votes that caused it:

```
  IDLE -> WATCHING       ow_esports is live and eligible
            S1=UNKNOWN S2=ONLINE S3=UNKNOWN S4=ONLINE S5=UNKNOWN
```

`S1=UNKNOWN` there is correct, not a fault — PubSub abstains until it has actually
received a playback message. Abstaining is a first-class vote in this design.

### The three rules the detector exists to enforce

| Situation | Correct behaviour |
| --- | --- |
| Ad break / commercial | S1 abstains; stays `WATCHING`, **never rotates** |
| PubSub socket dies | S1 abstains — a dead socket says nothing about the stream |
| Live but not crediting | `STALLED`, **not** `OFFLINE` — so nobody debugs the network |

Tune with `liveness.grace_period`, `liveness.confirm_reads` and
`liveness.stall_cycles`. `OFFLINE` needs *both* enough confirmations *and* the full
grace period, and offline weight must exceed online weight — a minority signal can
never rotate on its own.

## The app

Double-clicking the exe — or running `dropwatch` with no arguments from source —
opens a desktop window. It is the dashboard described below, in a frameless window
with its own title bar, and it behaves the way a tray app is expected to:

- **Closing the window doesn't quit it.** The close button hides the window and
  drops it out of the taskbar. Watching carries on; the tray icon stays.
- **Launching it again brings it back.** A second run finds the copy already
  running, raises its window, and exits — rather than starting a rival watcher,
  which would report the same account to Twitch twice.
- **Quit is explicit**: the tray menu, or the *Quit* button in the window.
- **Signing in happens in the window** if there's no token yet, so a fresh copy
  is usable without touching a terminal.

Two loops share the process: the native window owns the main thread and the
watchers get their own. If WebView2 is missing — it ships with Edge, so this is
rare — the app still runs headless with its tray icon and opens the dashboard in
your browser instead.

**From source, there's also [dropwatch.cmd](dropwatch.cmd)** — a double-click menu
that opens the app, or runs doctor, history and the API check against the project
venv with no build step.

### Standalone .exe

For a portable copy with no Python install at all:

```bash
build-exe.cmd            # or: .venv/Scripts/python -m PyInstaller dropwatch.spec --noconfirm
```

Produces `dist/dropwatch.exe` — one file. Copy it anywhere and **double-click it**:
the app opens, and signing in happens in the window.

From a terminal the same exe is still a normal CLI:

```
dropwatch.exe doctor     check the install
dropwatch.exe serve      watch with a console instead of a window
```

That works because the exe is built console-mode with PyInstaller's
`hide_console="hide-early"`, which only hides a console the process *created
itself*. Double-clicked, there is no console window at all; run from an existing
`cmd`, output goes to that window as usual.

**State lives in `%LOCALAPPDATA%\dropwatch`.** `data/`, `config.toml` and `.env`
are kept there rather than beside the executable, so the exe is just a file — put
it on the Desktop, move it, replace it with a new build, and your tokens and
history stay put. (Writing beside the exe also fails outright under
`Program Files`, and leaves a database in whatever folder you downloaded it to.)

`config.toml` is seeded from the bundled example on first run and is optional —
without one it uses defaults with auto-discovery.

**Portable mode** if you want the old behaviour: drop an empty file named
`dropwatch.portable` next to the exe, or keep a `data/` folder there. Either one
makes it use its own folder instead, so a USB-stick install keeps working and an
existing beside-the-exe install is never silently abandoned. `DROPWATCH_HOME`
overrides everything.

The dashboard HTML is bundled *inside* the exe, but a `ui/` folder placed next to
it wins, so it can be re-skinned without a rebuild.

Two things worth knowing before you distribute it: the exe is unsigned, so
SmartScreen will warn on first run (More info → Run anyway), and PyInstaller
binaries draw occasional false positives from antivirus. UPX compression is
deliberately off in the spec because it makes that markedly worse.

## Farming several accounts

```bash
dropwatch login          # again — adds a second account, keeps the first
dropwatch accounts       # who is farming, and what each one is doing
```

Or use the **+ Add another account** tile in the app. Either way the accounts
already running keep running while you authorise the new one.

Each account is fully separate: its own HTTP session, token, telemetry stream,
PubSub connection and watcher. Nothing identifying is shared, because Twitch
credits watch time to whoever sent it — one session spanning two logins would
credit the wrong account.

They pick targets **independently**, so they normally all land on the same
stream. That is the point: drops accrue per account, so three accounts watching
one channel earn three times.

| | |
| --- | --- |
| Tokens | `data/accounts/<login>.json`, one file per account |
| History | every `watch_session`, sample and transition carries an `account` |
| Disable | keeps the token, stops the farming — `accounts disable <name>` |
| Remove | deletes the local token only; revoke at Twitch with `logout --account` |

Upgrading from a single-account install migrates `data/tokens.json` into
`data/accounts/` automatically, keeping the old file as `.migrated`.

The dashboard shows one card per account with its state, target and credited
minutes, and tags every log line with the account that produced it. The
**Rewards** ladder is read for one account at a time and says which.

## Rewards and claiming

```bash
dropwatch drops           # every campaign, its reward ladder, and what's claimable
dropwatch drops --open    # ...and open Twitch if something is waiting
```

Several campaigns credit from the same watch time at once, so one hour of watching
advances all of them. The dashboard shows the full ladder with reward artwork, and
puts a gold banner at the top the moment anything is earned.

### Auto-claim is not possible, and that's deliberate

**Twitch gates the claim mutation behind a Client-Integrity token.** Firing
`DropsPage_ClaimDropRewards` returns:

```json
"extensions": { "challenge": { "type": "integrity" } }
```

That token is produced by the real web player specifically to distinguish it from
automation. Forging it is out of scope for this project, so **claiming is a browser
action** — one click from the dashboard banner or `dropwatch drops --open`.

What the bot does instead:

1. Detects an earned reward within one progress cycle
2. Emits `DROP_CLAIMABLE` (phase 5 will ping Discord with it)
3. Attempts the claim once, hits the gate, and emits `CLAIM_BLOCKED` **once** — not
   every cycle, or it would bury the log
4. Keeps announcing each new reward individually so nothing is missed

The claim code is correct and kept live rather than stubbed out, so if Twitch ever
stops gating the mutation, auto-claim starts working with no code change.
`watch.auto_claim` controls whether the attempt is made at all.

`IntegrityChallengeError` is a distinct exception type, deliberately not schema
drift — Twitch emits a generic error alongside the challenge, and reporting that
would send you hunting a field rename that doesn't exist.

## Background running and the tray

```bash
dropwatch serve --detach     # runs with no console window; close the terminal freely
dropwatch stop               # stop a detached run
```

`--detach` spawns a windowless copy and **waits for its dashboard to answer before
reporting success** — otherwise a port conflict would look like a clean launch, since
the detached process has nowhere to print its error.

A **system-tray icon** is what makes that controllable: right-click for *Show
window* (or *Open dashboard*, without one), *Hide window*, *Open
twitch.tv/&lt;channel&gt;*, *Pause* / *Resume*, and *Quit*. Its labels track the live
state, so it shows the current target and whether watching is paused. Double-click
brings the window back. Disable with `--no-tray` or `ui.tray = false` — though a
detached run then has no controls except `dropwatch stop`.

The icon is drawn at runtime rather than loaded, so it cannot go missing from a
bundle. `packaging/make_icon.py` bakes the identical drawing into
`packaging/dropwatch.ico` for the two places Windows insists on a file: the
executable and the taskbar. Re-run it after editing `desktop.icon_image`.

## Settings

Persisted in `config.toml` and the database, so `serve` behaves the same however it's
launched. Editable **from the dashboard** (right-hand *Settings* panel) or the CLI:

```bash
dropwatch config set ui.open_twitch true      # open the channel when one is picked
dropwatch config set ui.open_dashboard true   # open the dashboard on start
dropwatch config set liveness.grace_period 120
dropwatch config unset liveness.grace_period  # back to the file default
```

| Setting | Does |
| --- | --- |
| `ui.open_dashboard` | Open the dashboard when watching starts |
| `ui.open_twitch` | Open the channel's Twitch page when a target is picked |
| `ui.reopen_twitch_on_rotate` | Re-open on every rotation, not just the first |
| `ui.tray` | Show the tray icon |
| `ui.host` / `ui.port` | Where the dashboard listens (CLI only — see below) |

Opening Twitch is purely so you can *watch* the stream; crediting comes from
telemetry either way, and the browser plays no part in it.

The dashboard's settings panel writes through an allowlist, and `ui.host`/`ui.port`
are deliberately **not** on it: they need a restart to take effect, and a bad value
entered there would make the dashboard unreachable. Set those in `config.toml` or via
`config set`. Writes require a custom header, so another site open in your browser
can't drive the unauthenticated local endpoint.

CLI flags still win for a single run: `--host`, `--port`, `--open`, `--open-twitch`,
`--no-open-twitch`, `--no-tray`.

## The dashboard

This is what the app window shows; `serve` puts the identical page in a browser.



```bash
dropwatch serve                    # watcher + dashboard on http://127.0.0.1:8787
dropwatch serve --open             # ...and open a browser
dropwatch serve --port 9000
```

Served by the running bot, so these are its **real logs** — Python log records, bus
events and state transitions, streamed over Server-Sent Events. Filter by
transitions / events / warnings, follow-tail that releases when you scroll up, and
a signal table plus drop-progress bar updated every 2s.

The feed keeps a 500-record backlog, so opening the page late still shows the run
from the start. It stops updating when the bot stops — the connection indicator says
so rather than pretending.

Endpoints, if you'd rather script against it:

| Route | Returns |
| --- | --- |
| `/api/state` | current state, signal votes, session counters |
| `/api/history` | recent transitions and sessions from SQLite |
| `/api/events` | SSE feed of logs, events and transitions |

Binds to localhost only by default. `--host 0.0.0.0` exposes it to your network,
and there is **no authentication**, so only do that on a network you trust.

> This is a different page from the [console simulator](ui/console.html) below. The
> dashboard shows a real run; the simulator runs a fake Twitch for exercising the
> state machine. The simulator can't show real logs, and the dashboard can't inject
> faults.

## Console UI

[ui/console.html](ui/console.html) is a self-contained client-side console — open it
directly in a browser, no server and no build step. It runs the **phase 3 liveness
state machine** against a simulated Twitch so the quorum-and-hysteresis design can
be exercised before it's written in Python.

Fault-injection buttons check the design holds:

| Inject | Expected behaviour |
| --- | --- |
| Ad break | enters `SUSPECT`, recovers to `WATCHING`, **no rotation** |
| Kill PubSub | S1 abstains (`UNKNOWN`) — must **never** read as offline |
| Stop crediting | lands in `STALLED`, **not** `OFFLINE` |
| End stream | confirms via active probe, waits out grace, then rotates |
| Switch to rerun | live but drops-ineligible → rotate |

The threshold sliders (grace period, confirm reads, stall cycles) change the
simulation live, so you can see what a given setting actually does.

## Tests

```bash
.venv/Scripts/python -m pytest -q      # 53 tests, no network
.venv/Scripts/python -m ruff check src tests

node ui/console.test.js ui/console.html # state machine behaviour
node ui/console.lint.js ui/console.html # id refs, state classes, theme parity
```

The device flow and refresh paths run against a fake aiohttp session, so the token
state machine — pending, denied, expired, revoked, concurrent refresh — is covered
without a live Twitch.
