# Changelog

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
