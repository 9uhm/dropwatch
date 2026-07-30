# Setup guide — from a fresh Windows install

Two routes. **Route A needs nothing installed** and takes about five minutes; take
it unless you intend to change the code.

---

## Route A — the .exe (no Python, no build)

1. **Get `dropwatch.exe`** and put it in a folder of its own, e.g.
   `C:\Tools\dropwatch\`. It writes its state *beside itself*, so give it a
   folder rather than dropping it on the Desktop.

2. **Windows will block it on first run.** It's unsigned. Click
   **More info → Run anyway**. See [SmartScreen](#smartscreen-blocks-the-exe) below.

3. **Open a terminal in that folder.** Shift+right-click the folder background →
   *Open in Terminal* (or *PowerShell window here*).

4. **Authorise Twitch:**
   ```
   .\dropwatch.exe login
   ```
   It prints an 8-character code. Open <https://www.twitch.tv/activate>, enter it,
   approve. The bot picks it up within seconds.

5. **Link Twitch to Battle.net — this is the step people miss.** Without it Twitch
   credits *zero* drop progress no matter how long the bot watches. Go to
   <https://www.twitch.tv/settings/connections> and connect **Battle.net**.

6. **Check everything:**
   ```
   .\dropwatch.exe doctor
   ```
   Every row except the two Discord ones should read `[ok]`. Discord is unbuilt, so
   those two failing is expected.

7. **Start it:**
   ```
   .\dropwatch.exe serve --open
   ```
   Your browser opens the dashboard. Leave the terminal window open — closing it
   stops the bot. Ctrl-C to stop cleanly.

That's it. `config.toml` is optional: with no config it uses defaults and finds a
drops-enabled Overwatch channel on its own.

---

## Route B — from source

### 1. Install Python

Get **Python 3.13 or newer** from <https://www.python.org/downloads/>
(3.14 is what this was developed and tested on).

During install, **tick "Add python.exe to PATH"** on the first screen. If you miss
it you'll get [`python: command not found`](#python-is-not-recognised).

Verify in a new terminal:
```
py --version
```

### 2. Get the code

With git: `git clone <repo-url>` — or download the ZIP and extract it. Then:
```
cd dropwatch
```

### 3. Create the environment

```
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

Use `py -3.13` if that's your version. Everything installs from prebuilt wheels —
no compiler needed.

### 4. Create your config

```
copy config.example.toml config.toml
copy .env.example .env
```

Both are optional but recommended — `config.toml` is where you set which channels
to watch.

### 5. Authorise and check

```
.venv\Scripts\python -m dropwatch login
.venv\Scripts\python -m dropwatch doctor
```

**Then link Twitch to Battle.net** at <https://www.twitch.tv/settings/connections>.
Drops credit nothing without it.

### 6. Run it

```
.venv\Scripts\python -m dropwatch serve --open
```

Or just **double-click `dropwatch.cmd`** and pick option 1 from the menu.

### Optional: build your own .exe

```
build-exe.cmd
```
Output lands in `dist\dropwatch.exe`. Takes about a minute.

---

## Verifying it actually works

The one thing worth checking, because everything else is cosmetic by comparison:

```
.venv\Scripts\python -m dropwatch drops
```

If a campaign shows minutes climbing, Twitch is crediting you and the setup is
correct. If it's stuck at 0 after ten minutes of `serve`, jump to
[nothing is crediting](#its-watching-but-nothing-is-crediting).

---

## Troubleshooting

### `python` is not recognised

PATH wasn't set at install time. Either re-run the Python installer and choose
*Modify → Add to PATH*, or use the full path:
```
C:\Users\<you>\AppData\Local\Programs\Python\Python314\python.exe -m venv .venv
```
`py` is usually available even when `python` isn't — try `py --version` first.

### SmartScreen blocks the exe

*"Windows protected your PC"* → **More info → Run anyway**. The exe is unsigned;
code-signing certificates cost money and this is a personal tool. If you'd rather
not trust a binary, use Route B and run from source.

### Antivirus quarantines the exe

A known false positive with PyInstaller binaries — a Python interpreter bundled with
a script matches a lot of packer heuristics. UPX compression is deliberately
**disabled** in `dropwatch.spec` because it makes this markedly worse. Either add an
exclusion for the folder, or run from source.

### `The virtual environment is missing`

`dropwatch.cmd` couldn't find `.venv\Scripts\python.exe`. You're on Route B and
step 3 hasn't been done, or you're running the `.cmd` from a copy of the folder that
doesn't include `.venv`.

### `Not authenticated — run dropwatch login`

No stored token. Run `login`. If you *did* log in but it still says this, check that
`data\tokens.json` exists — if you moved the exe, its state folder moved with it and
you'll need to log in again in the new location.

### `Stored token has no user id`

The token file predates a fix or was written incompletely. Run:
```
dropwatch logout
dropwatch login
```

### It says "nothing is answering" after `--detach`

The background copy started but its dashboard never came up — almost always because
something already holds the port. `--detach` checks before claiming success, since a
windowless process has nowhere to print the error. Run `dropwatch stop`, then plain
`dropwatch serve` in a visible window to see the actual message.

### The tray icon doesn't appear

The log will say `tray unavailable`. It's optional and the watcher runs regardless,
but a detached run then has no controls except `dropwatch stop`. Check that
`ui.tray` is `true` and that `--no-tray` wasn't passed. On Windows the icon may be
hidden in the notification-area overflow — click the chevron next to the clock.

### Settings won't save from the dashboard

- A red message under the control means the value was out of range; it states the
  limit.
- `403` means the write was rejected as cross-site, or the key isn't editable from
  the page. `ui.host` and `ui.port` are intentionally CLI-only, because a bad value
  there would leave the dashboard unreachable. Use
  `dropwatch config set ui.port 9000`.

### `Could not bind 127.0.0.1:8787`

Something is already on that port — usually a copy of the bot you forgot about.
Either stop it, or:
```
dropwatch serve --port 9000
```
To find the culprit:
```powershell
Get-NetTCPConnection -LocalPort 8787 -State Listen | Select-Object OwningProcess
```

### Dashboard says "bot not reachable — retrying"

The bot isn't running, or it's on a different port. That message is honest, not a
bug — the page refuses to show stale numbers as if they were live. Start the bot and
the page reconnects on its own; no reload needed.

### It's watching but nothing is crediting

The bot shows `STALLED` (dashed border), or `drops` shows minutes stuck at zero. In
rough order of likelihood:

1. **Twitch isn't linked to Battle.net.** By far the most common cause.
   <https://www.twitch.tv/settings/connections>
2. **The campaign ended.** `dropwatch drops` shows each campaign's remaining time.
3. **It's watching a rerun.** Drops never credit on vodcasts. The bot detects this
   and rotates, but `--force` on `dropwatch watch` overrides that guard — don't.
4. **The channel isn't in the campaign.** `dropwatch discover` lists channels Twitch
   itself flags as drops-enabled; prefer those.
5. **Region exclusion.** Some campaigns are region-locked and simply won't credit.

`STALLED` specifically means *the stream is live and our telemetry is being accepted,
but Twitch is not counting it* — so don't go looking for a network fault.

### `dropwatch gql-check` reports STALE hashes

Usually harmless. The bot sends its own query documents by default, so stale
persisted-query hashes only matter for operations with no document. The verdict
column is what counts — as long as it says `usable`, you're fine.

If something genuinely broke, put the current hash in
`data\gql_operations.json`:
```json
{ "OperationName": "the-new-sha256" }
```
No code change or restart of anything else needed.

### `failed integrity check` / can't auto-claim

**Working as intended, and unfixable from here.** Twitch gates reward claiming
behind a Client-Integrity token that only its real web player produces. The bot
detects earned rewards and tells you, but **claiming is a browser action** — use the
gold banner on the dashboard, or:
```
dropwatch drops --open
```

### No eligible channels / it sits in `IDLE`

- Nobody in your priority list is live *and* drops-eligible, and discovery found
  nothing. Check with `dropwatch live` and `dropwatch discover`.
- If `discover` errors about `game_slug`, your category slug is wrong. It's the
  Twitch URL fragment — `overwatch-2`, not `Overwatch 2`.
- `overwatchleague` will never work: OWL shut down in 2023 and the channel is
  permanently offline. The live OWCS channel is `ow_esports`.

### Sessions show `0 min / running` forever

The process was killed rather than stopped with Ctrl-C, so it never wrote the
session's end. The next `serve` or `run` closes those out automatically
(`interrupted (recovered)`). Your **watch time is not lost** — Twitch counts that
server-side; only our local bookkeeping row was incomplete.

### `whoami` says the token "does not expire"

Not a bug. Twitch issues non-expiring tokens to TV-class clients, which is what the
device-code flow uses. There's nothing to refresh.

Worth knowing: because it never expires, deleting `tokens.json` by hand leaves a
permanently valid token in existence. Use `dropwatch logout`, which revokes it at
Twitch properly.

### Garbled characters in the terminal

Old Windows console using a legacy codepage. The app forces UTF-8 on startup, so if
you still see mojibake you're likely on `cmd.exe` in a very old build — use Windows
Terminal or PowerShell.

### `discord config` and `discord token` fail in `doctor`

Expected. The Discord layer isn't built yet. Nothing else depends on it.

### Everything looks fine but I want to see what it's doing

```
dropwatch serve --open      # dashboard: live logs, charts, rewards
dropwatch status            # past sessions and state transitions
dropwatch config show --paths   # every setting, and where the files live
```

---

## Where your data lives

Run `dropwatch config show --paths` to see exactly. By default, beside the exe or in
the project root:

| Path | What |
| --- | --- |
| `config.toml` | your settings — safe to hand-edit, never overwritten |
| `.env` | secrets only (`DISCORD_TOKEN`) |
| `data\tokens.json` | OAuth tokens, locked to your Windows user |
| `data\dropwatch.db` | sessions, transitions, watch samples, config overrides |

Set `DROPWATCH_HOME` to relocate all of it.

**Never share `data\tokens.json` or `.env`.** The token grants access to the Twitch
account and does not expire.

---

## Uninstalling

```
dropwatch logout          # revokes the token at Twitch, not just locally
```

Then delete the folder. Optionally remove the app grant at
<https://www.twitch.tv/settings/connections> — and note that disconnecting
**Battle.net** there stops all Twitch drops for that account, not just this bot's.
