# dropwatch

A Twitch drop campaign miner for Overwatch, with multi-signal stream-end detection
and a live local dashboard. API-only — no browser, no video decoded, ~30 MB resident.

**New here, or on a fresh Windows install? → [SETUP.md](SETUP.md)**

## What works

| | |
| --- | --- |
| **Watching** | Reports minute-watched telemetry; verified crediting against live Twitch |
| **Stream-end detection** | Five weighted signals with hysteresis — ad breaks and dropped sockets never trigger a rotation |
| **Rotation** | Switches target when a stream ends or stops crediting, priority list then auto-discovery |
| **Rewards** | Reads every active campaign's ladder and flags what's earned |
| **Dashboard** | Live logs over SSE, watch-time charts, reward artwork — served on localhost |
| **Packaging** | Single-file `.exe`, or a double-click launcher |

Not built: the Discord control layer. **Auto-claim is impossible** — Twitch gates
claiming behind an integrity token; see [Rewards and claiming](#rewards-and-claiming).

[PLAN.md](PLAN.md) has the full design and a record of what each phase actually
found, including the things that turned out not to work.

## Quick start

```bash
py -3.14 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"

cp config.example.toml config.toml     # channel list, intervals, thresholds
cp .env.example .env                   # DISCORD_TOKEN (unused for now)

.venv/Scripts/python -m dropwatch login    # authorise via twitch.tv/activate
.venv/Scripts/python -m dropwatch doctor   # what's configured, what's missing
.venv/Scripts/python -m dropwatch serve --open
```

`login` prints a code to enter at `twitch.tv/activate`, then polls until you
approve. No password touches this process.

**Then link Twitch to Battle.net** at <https://www.twitch.tv/settings/connections>.
Without it Twitch credits zero minutes no matter how long the bot runs — it's the
single most common reason a working setup earns nothing.

## Commands

| Command | Purpose |
| --- | --- |
| `dropwatch serve [--open]` | Run the watcher with the live dashboard on localhost |
| `dropwatch run` | Run the watcher, console output only |
| `dropwatch drops [--open]` | Reward ladders and what's claimable |
| `dropwatch status` | Recorded sessions and state transitions |
| `dropwatch live [channel...]` | Are these channels live and drops-eligible? |
| `dropwatch discover` | Live drops-enabled channels in the category |
| `dropwatch watch <channel>` | Watch one fixed target and show what Twitch credits |
| `dropwatch doctor` | Check local setup; reports exactly what's unconfigured |
| `dropwatch gql-check` | Validate every GraphQL transport against live Twitch |
| `dropwatch login [--force]` | Authorise via the device code flow |
| `dropwatch whoami` | Validate the stored token, show identity and expiry |
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

## Just run it

**Double-click [dropwatch.cmd](dropwatch.cmd).** A menu appears — pick
*1. Start watching* and the dashboard opens in your browser. No build step; it uses
the project venv directly. Also does login, doctor, history and the API check.

### Standalone .exe

For a portable copy with no Python install at all:

```bash
build-exe.cmd            # or: .venv/Scripts/python -m PyInstaller dropwatch.spec --noconfirm
```

Produces `dist/dropwatch.exe` — one file, ~16 MB. Copy it anywhere and:

```
dropwatch.exe login      once, to authorise
dropwatch.exe serve      watch, with the dashboard
```

**It keeps its state beside itself.** `data/`, `config.toml` and `.env` are read
from and written to the exe's own folder, so you can drop it on a USB stick or in
`C:\Tools\` and it stays self-contained. `config.toml` is optional — without one it
uses defaults with auto-discovery. Relocate state with `DROPWATCH_HOME` if you'd
rather.

The dashboard HTML is bundled *inside* the exe, but a `ui/` folder placed next to
it wins, so it can be re-skinned without a rebuild.

Two things worth knowing before you distribute it: the exe is unsigned, so
SmartScreen will warn on first run (More info → Run anyway), and PyInstaller
binaries draw occasional false positives from antivirus. UPX compression is
deliberately off in the spec because it makes that markedly worse.

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

A **system-tray icon** is what makes that controllable: right-click for *Open
dashboard*, *Open twitch.tv/&lt;channel&gt;*, *Pause* / *Resume*, and *Quit*. Its labels
track the live state, so it shows the current target and whether watching is paused.
Double-click opens the dashboard. Disable with `--no-tray` or `ui.tray = false` —
though a detached run then has no controls except `dropwatch stop`.

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

## Live dashboard

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
