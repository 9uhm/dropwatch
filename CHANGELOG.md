# Changelog

## v0.4.0

Farm several Twitch accounts at once, and keep state out of the exe's folder.

### Fixed: the add-account code stayed on screen forever

Starting *Add another account* and then changing your mind left the code banner
sitting there indefinitely, claiming something was in progress.

Two causes, and the second was the one that mattered:

- The page only polled `/api/auth` while **signed out**. Adding a *second*
  account means you are already signed in, so the poll stopped on its first pass
  — the banner was painted once and then never updated or cleared, whatever the
  server did. It now polls while signed out **or** while a flow is pending.
- Nothing bounded the wait. The dashboard now gives up after `FLOW_TIMEOUT`
  (2 minutes) rather than Twitch's 30, since the code is only useful while
  someone is acting on it. The banner counts down *our* deadline, not Twitch's,
  so the UI promises exactly as long as the server will actually wait.

There is also a **Cancel** button now, which stops the polling and discards the
empty account slot the flow had reserved.

One subtlety worth recording: `asyncio.TimeoutError` *is* `TimeoutError`, so the
new handler had to distinguish our deadline elapsing from an HTTP call inside the
flow timing out — otherwise a network fault would be reported as "nobody typed
the code" and send someone looking in the wrong place. The clock decides which.

### The Battle.net link is now actually checked

Twitch credits **zero minutes** when the account isn't linked to Battle.net, and
says nothing about it anywhere — the watch loop looks perfectly healthy while
earning nothing. Until now the app only warned about it in prose and never
checked.

`Inventory` now asks for `self { isAccountConnected }` and `accountLinkURL` on
each campaign, verified against live Twitch. It rides on the query the dashboard
already runs, so it costs nothing extra.

- **`doctor`** reports it per account: `battle.net link — connected`, or `NOT
  linked — Twitch will credit 0 minutes` with the exact page to fix it.
- **The dashboard** shows a red banner with a *Link accounts* button, using the
  publisher-specific URL Twitch returns rather than a hardcoded one.
- **`/api/drops`** carries `account_linked` and `account_link_url`, and each
  campaign carries `account_connected`.

The verdict is deliberately three-valued. With no campaign in progress, an
unlinked account and an account merely between campaigns look identical, so that
case reports "unknown" and stays silent rather than showing a red banner to
someone whose setup is fine.

### Fixed: a console window on other people's machines

The packaged app is now built for the **windowed** subsystem, so Windows never
creates a console for it — there is nothing to flash, hide, or leave sitting
behind the app.

The previous approach, `console=True` with PyInstaller's
`hide_console="hide-early"`, hides whatever `GetConsoleWindow()` returns. That is
the classic conhost window; when **Windows Terminal** is the default terminal
host — the default on Windows 11 — the visible window belongs to
`WindowsTerminal.exe` instead, so there is nothing for us to hide and the console
stays on screen. It worked on the conhost machine it was written on and failed on
the next one.

CLI output survives: `main()` calls `attach_parent_console()`, which borrows the
launching terminal's console when there is one and does nothing when there isn't.
Inherited handles are preferred over `CONOUT$` so redirection still works —
verified that `dropwatch.exe accounts > out.txt` captures its output and that a
double-clicked launch creates no console window at all.

### Multiple accounts

Every account gets its own HTTP session, token, telemetry stream, PubSub
connection and watcher. Nothing that carries an identity is shared, because
Twitch credits watch time to whoever sent it — sharing a session between two
logins would credit the wrong one. The config, database and event bus stay
shared.

- Accounts pick targets **independently**, so they usually land on the same
  stream. That is correct: drops accrue per account, so watching one channel
  with three accounts earns three times.
- **Add one from the app**: the "+ Add another account" tile runs the device
  flow into a fresh account slot, which names itself from the login Twitch
  returns. The accounts already farming keep farming throughout — the sign-in
  gate is only shown when *nothing* is signed in.
- **The dashboard shows them side by side**, one card each with state, target,
  credited minutes and per-account Pause / Disable / Remove. The combined log
  tags every line with the account that produced it.
- `dropwatch accounts` lists them; `accounts enable|disable|remove <name>`
  manages them. `login` now *adds* an account when one already exists rather
  than overwriting it. `whoami` and `logout` take `--account`.
- Tokens moved from `data/tokens.json` to `data/accounts/<login>.json`. An
  existing install is migrated on first run, and the old file is renamed rather
  than deleted.
- History is per-account: `watch_session`, `watch_sample`, `state_transition`
  and `drop_claim` gained an `account` column (schema v3). Rows written before
  this are backfilled as `(before accounts)` rather than being attributed to
  whichever account happens to be first — their minutes were real, but the
  database never recorded whose they were.
- **Newly added accounts are named correctly.** The device flow saves its token
  once before `/validate` reports who you are, and naming the file from that
  empty login filed every added account as `default.json` — so the second
  account overwrote the first. An unidentified token is now held rather than
  written; the save after validation names it.
- Session reconciliation is scoped per account. Several watchers reconcile at
  the same moment on startup, and an unscoped sweep let the first one close
  sessions the others still held.

### State lives in %LOCALAPPDATA%

The frozen exe keeps `data/`, `config.toml` and `.env` in
`%LOCALAPPDATA%\dropwatch` instead of beside itself, so the executable is just a
file you can leave on the Desktop, move, or replace without losing your tokens
and history. Writing beside the exe also fails outright under `Program Files`.

Portable mode is still available: drop a `dropwatch.portable` file next to the
exe, or keep an existing `data/` folder there — either makes it use its own
folder, so an install that has been running that way is not silently abandoned.
Running from a source checkout is unchanged.

## v0.3.0

A desktop app: one exe you double-click, that stays out of the way.

### The app

`dropwatch` with no arguments — or a double-clicked exe — now opens a window
instead of printing an argparse error. It is the same dashboard, hosted in a
frameless WebView2 window with a title bar drawn by the page.

- **Close hides, it does not quit.** The close is cancelled and the window
  hidden, which on Win32 also removes the taskbar button. The watcher keeps
  running and the tray icon stays. Verified with a real `WM_CLOSE`: the window
  survives hidden, and the watcher is still `WATCHING` afterwards.
- **A second launch raises the first.** The dashboard's HTTP port is the lock —
  whoever holds it is the live instance — so launching again finds it, tells it
  to show itself, and exits. No named mutex and no lock file to go stale. A
  stranger on the port is rejected by a `/api/ping` identity check rather than
  mistaken for us.
- **Signing in happens in the window.** A copy with no token opens to a sign-in
  gate that runs the device flow and starts watching by itself on approval, so a
  fresh exe never has to be driven from a terminal first.
- **A custom title bar**: drag region, minimise / maximise / close, and the live
  state and channel. Frameless would normally cost the resize border and Aero
  Snap, so `WS_THICKFRAME` and `WS_MAXIMIZEBOX` are put back explicitly.
- **An app icon.** `desktop.icon_image` draws the mark, the tray renders it at
  runtime, and `packaging/make_icon.py` bakes the same drawing into a
  multi-resolution `.ico` for the exe and taskbar.

Two loops share the process: WinForms owns the main thread because it cannot run
anywhere else, and the watcher, dashboard and tray get their own thread and event
loop. Without WebView2 the app still runs, keeps its tray icon, and opens the
dashboard in the default browser.

### Packaging

The exe is built console-mode with `hide_console="hide-early"`, which hides only
a console the process created itself. Double-clicked it is a GUI app; run from an
existing terminal it is still a normal CLI, from one binary.

### Fixed

- The window icon fell back to `python.exe`'s. `paths.icon_file` searched under
  `ROOT` — the *state* directory, which `DROPWATCH_HOME` can point anywhere —
  rather than the bundle the icon actually ships in. It now searches the bundle,
  with an override beside the exe. An explicit AppUserModelID is also set, since
  the shell resolves the taskbar icon through that rather than through the
  window.
- The sign-in gate covered the title bar, leaving a frameless window that could
  not be moved or closed until you were signed in. The bar now outranks the
  overlay and the gate starts below it.
- `/api/auth` reported a huge negative `expires_in` for Twitch's non-expiring
  TV-client tokens, which renders as "expired". It now reports `null`.
- The app window restored its previous scroll position on launch, opening
  halfway down a chart. `history.scrollRestoration` is now manual.

### Changed

- The wordmark is **dropwatch** everywhere; the old `ow·watch·bot` name is gone
  from the UI.

## v0.2.1

Fixes the background run opening a console window.

`DETACHED_PROCESS` and `CREATE_NO_WINDOW` are mutually exclusive on Win32, and
passing both let `DETACHED_PROCESS` win — which means "do not inherit the parent's
console", so a console-subsystem binary allocated a brand new one. The supposedly
headless run popped up an empty black window. `CREATE_NO_WINDOW` alone gives a
console with no window, which is what headless has to mean.

Verified against the packaged exe: `MainWindowHandle` is 0, no titled window
exists, and it keeps serving after its launching shell exits.

Also adds `help` as a subcommand — argparse only offers `--help`, so `dropwatch
help` failed with an invalid-choice error.

## v0.2.0

Background running, a system tray, and settings that persist.

### Background running

- `serve --detach` runs with no console window, so the terminal can be closed.
  It waits for the detached copy's dashboard to answer before reporting success —
  a windowless process has nowhere to print a port conflict.
- A **system-tray icon** carries the controls a console window used to: open the
  dashboard, open the channel on Twitch, pause/resume, quit. Labels track live
  state, so it shows the current target and whether watching is paused. Optional;
  its absence degrades to a log line.
- `stop` ends a detached run and distinguishes a stale pid record from a live
  process, so a crash can't leave `serve` believing it's still running.

### Settings

Previously CLI-only flags are now persisted config, editable from a new dashboard
panel or from `config set`:

| Setting | Does |
| --- | --- |
| `ui.open_dashboard` | Open the dashboard when watching starts |
| `ui.open_twitch` | Open the channel's Twitch page when a target is picked |
| `ui.reopen_twitch_on_rotate` | Re-open on every rotation, not just the first |
| `ui.tray` | Show the tray icon |
| `ui.host` / `ui.port` | Where the dashboard listens (CLI only, by design) |

Opening Twitch is purely so you can watch the stream; crediting comes from
telemetry either way and the browser plays no part in it.

The write endpoint is guarded three ways: a custom header no cross-site request
can set without a preflight the server never answers, an `Origin` check, and a
key allowlist. `ui.host`/`ui.port` are off that allowlist deliberately — they need
a restart, and a bad value entered from the page would make the page unreachable.

### Fixed

- **PIL was excluded from the frozen build**, which would have left the tray
  permanently unavailable in the executable while working fine from source.
- **The detached child shared the parent's PyInstaller unpack directory.**
  PyInstaller advertises it through the environment; inherited, the child reused
  it rather than extracting its own, so every bundled asset began 404ing the
  moment the parent exited and cleaned up. Only reproducible frozen *and*
  detached — the configuration people actually run.
- `ui/stats.js` wasn't bundled, so the packaged dashboard rendered with silently
  missing charts.

115 tests.

## v0.1.0

First release. Watches Twitch drop campaigns for Overwatch, detects when a stream
really has ended, and rotates. API-only — no browser, no video decoded.

### Watching

- Replicates the web player's minute-watched telemetry. The endpoint is resolved
  at runtime from Twitch's settings bundle, since it moves between deploys.
- Interval jittered around 58s rather than fixed.
- Verified against live Twitch: reported minutes credit as drop progress.

### Stream-end detection

Five independent signals feed a weighted quorum with hysteresis. The rules that
matter:

- An **ad break** never causes a rotation — PubSub sends an explicit `commercial`
  window, and the signal abstains for its duration.
- A **dead PubSub socket** reports `UNKNOWN`, never `OFFLINE`. Losing our
  connection says nothing about the broadcast.
- **Live but not crediting** is its own state (`STALLED`), separate from
  `OFFLINE`, so a silent non-crediting failure doesn't read as a network fault.
- `OFFLINE` requires both enough independent confirmations *and* the full grace
  period, and offline weight must exceed online weight — a minority signal can
  never rotate on its own.

Behaviour was specified and tested in a browser simulator (`ui/console.html`)
before being written in Python; it ships with fault injection for exercising it.

### Rewards

- Reads every active campaign's ladder with per-drop progress and claim state.
  Campaigns credit in parallel, so all of them are shown rather than one.
- **Claiming is not automated and cannot be.** Twitch gates the claim mutation
  behind a Client-Integrity token that only its real web player produces. The bot
  detects earned rewards, announces them once, and links out. The claim path is
  implemented and left live so it starts working if that gate is ever lifted.

### Interfaces

- **Local dashboard** on `127.0.0.1:8787` — live logs over Server-Sent Events,
  watch-time charts, reward artwork, filters, and a table view. Localhost-only by
  default, with no authentication.
- **CLI** covering auth, diagnostics, discovery, and the watch loop.
- **Single-file executable** that keeps its state beside itself, plus a
  double-click launcher for the source checkout.

### Reliability

- Query documents are sent instead of Twitch's persisted-query hashes, which
  rotate on every web deploy. Hashes remain a fallback; `gql-check` reports which
  transport works per operation, and a stale hash is fixable from a JSON file
  without a code change.
- A changed response shape raises a typed error rather than reading as zero
  progress — the two are otherwise indistinguishable.
- Sessions left open by a crash are reconciled on the next start, so they can't
  skew statistics.
- Tokens are written atomically and locked to the current user. `logout` revokes
  at Twitch rather than only deleting locally, which matters because Twitch issues
  non-expiring tokens to this client class.

### Known limitations

- No Discord control layer yet.
- Auto-claim is impossible (see above).
- Campaign-aware target prioritisation isn't implemented; selection is the
  priority list, then viewer count.
- The executable is unsigned, so SmartScreen warns on first run.

88 tests, no network required.
