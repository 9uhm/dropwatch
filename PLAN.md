# dropwatch — Design Plan

Automated Twitch drop farmer for Overwatch campaigns (OWCS / Twitch Rivals / any
drops-enabled Overwatch campaign), with full Discord control.

**Stack:** Python 3.14, asyncio + aiohttp, discord.py, SQLite
**Watch engine:** API-only telemetry (no browser, no video decoded)
**Targeting:** priority list, with auto-discovery fallback

---

## 0. Constraints that shape everything

1. **Twitch credits drop progress for exactly one stream at a time, per account.**
   Watching five channels in parallel earns the same as watching one. So the bot is
   a *single-target watcher with smart rotation*, not a fan-out miner. Parallelism
   only matters if you add multiple accounts (out of scope for v1).
2. **Drops don't credit on reruns/vodcasts.** `stream.type == "rerun"` means live but
   useless — must be a distinct state from offline.
3. **Progress is server-side.** The bot doesn't accumulate a local timer; it reports
   watch minutes and reads Twitch's authoritative progress. The local counter is only
   ever a display/heuristic value.
4. **Automation here violates Twitch ToS.** Acknowledged; not re-litigated below.

---

## 1. Repository layout

```
dropwatch/
├── pyproject.toml
├── .env.example                 # secrets only: DISCORD_TOKEN
├── config.example.toml          # everything else, hot-reloadable
├── data/                        # gitignored
│   ├── dropwatch.db               # SQLite: config, drop history, watch log
│   └── tokens.json              # OAuth tokens, mode 0600
└── src/dropwatch/
    ├── __main__.py              # entrypoint; wires App and runs event loop
    ├── config.py                # pydantic-settings, env + TOML, hot reload
    ├── events.py                # internal async pub/sub bus
    ├── store.py                 # SQLite access layer
    ├── twitch/
    │   ├── auth.py              # device-code flow, token store + refresh
    │   ├── gql.py               # GraphQL client + operation registry
    │   ├── spade.py             # minute-watched telemetry
    │   ├── pubsub.py            # WebSocket, topic subs, heartbeat
    │   ├── drops.py             # campaigns, inventory, progress diffing
    │   └── channels.py          # live check, discovery, priority resolution
    ├── liveness.py              # multi-signal stream-state detector  ← core ask
    ├── watcher.py               # target selection + watch loop + rotation
    └── discordbot/
        ├── bot.py               # client, cog loading, lifecycle
        ├── commands.py          # slash commands
        ├── views.py             # embeds, progress bars
        └── notify.py            # event bus → Discord messages
```

---

## 2. Authentication

**Device code flow**, the same one Twitch's own smart-TV/console apps use — no
password ever touches the bot, and no browser scraping.

1. `POST /oauth2/device` with Twitch's public TV client ID and scopes
   (`user:read:follows`, channel-read as needed).
2. Bot surfaces `user_code` + `https://twitch.tv/activate` via Discord DM (`/login`).
3. Poll `POST /oauth2/token` with `grant_type=device_code` until authorized.
4. Persist `access_token` + `refresh_token` to `data/tokens.json`, mode `0600`.
5. Refresh proactively at 80% of TTL; on `401` mid-flight, refresh once and retry
   the request before surfacing an error.
6. Refresh failure → `AUTH_EXPIRED` event → Discord alert pinging the owner, watcher
   pauses rather than hot-looping.

---

## 3. Twitch client layer

### `gql.py`
Single `GQLClient` holding the aiohttp session, auth header, and Client-ID.

All persisted-query operation hashes live in **one dict in one file** —
`OPERATIONS: dict[str, Operation]`. When Twitch rotates a hash, exactly one file
changes. Operations needed:

| Purpose | Operation |
| --- | --- |
| Is channel live, type, game, viewer count | `UseLive` / `StreamMetadata` |
| Playback token (validates watchability) | `PlaybackAccessToken` |
| Available drop campaigns for a game | `ViewerDropsDashboard` / `Campaigns` |
| Current watch session progress | `DropCurrentSessionContext` |
| Inventory: in-progress + claimable | `Inventory` |
| Claim a drop | `DropsPage_ClaimDropRewards` |
| Category directory (auto-discovery) | `DirectoryPage_Game` w/ `DROPS_ENABLED` tag |

Cross-cutting: per-op concurrency limit, exponential backoff with jitter on
429/5xx, and a hard rule that a malformed/changed response raises a typed
`SchemaDriftError` — surfaced to Discord as "Twitch API changed, needs a fix"
rather than silently returning zero progress.

### `spade.py`
The actual "watching" mechanism:

1. Fetch the Twitch web player JS bundle once, regex out the current `spade_url`
   (it moves between deploys). Cache with a TTL, re-resolve on failure.
2. Get `broadcast_id` + `channel_id` via `PlaybackAccessToken` / stream metadata.
3. Every **~58s**, POST a base64'd `minute-watched` payload (event, channel id,
   broadcast id, player `site`, user id, hostname) to the spade endpoint.
4. Non-2xx or a changed payload contract → `TELEMETRY_DEGRADED` signal into the
   liveness detector (see §4, S4).

Interval is jittered ±3s so it isn't a perfect metronome.

### `pubsub.py`
WebSocket to Twitch PubSub, subscribing per watched channel:

- `video-playback-by-id.<channel_id>` → `stream-up`, `stream-down`, `viewcount`,
  `commercial`. This is the **low-latency** stream-end signal (~1–5s).

Handles `PING` every 4min (Twitch drops the socket at 5), `RECONNECT` frames, and
reconnects with exponential backoff. A dead socket is not treated as "stream
offline" — it degrades to `UNKNOWN` and lets the polling signals decide.

---

## 4. Smart stream-end detection (`liveness.py`)

The centerpiece. **No single signal is trusted.** Five independent signals, each
reporting `ONLINE | OFFLINE | UNKNOWN`, fed into a state machine with hysteresis.

| # | Signal | Source | Latency | Weight |
| --- | --- | --- | --- | --- |
| S1 | PubSub `stream-down` / `stream-up` | WebSocket push | 1–5s | high |
| S2 | `user(login).stream is None` | GQL poll, 60s | ≤60s | high |
| S3 | Playback token / manifest 404 | active probe | on demand | medium |
| S4 | Spade POST rejected | watch loop | ≤60s | low (degraded, not offline) |
| S5 | Drop progress Δ == 0 across N cycles | `DropCurrentSessionContext` | ~3min | → STALLED |

### State machine

```
                  ┌──────────────────────────────────────┐
                  │                                      │
   IDLE ──pick──> WATCHING ──any OFFLINE signal──> SUSPECT
    ^                 │                              │
    │                 │                        active probe
    │                 │                        (force S2 + S3)
    │                 │                              │
    │                 │              ┌───────────────┴──────────────┐
    │                 │         confirmed OFFLINE            still ONLINE
    │                 │              │                              │
    │                 │              v                              │
    └──no targets── OFFLINE ──rotate target──> WATCHING <───────────┘
                      ^
                      │
   WATCHING ──S5: live but Δ==0 ×3──> STALLED ──rotate + notify──┘
```

**Anti-flap rules:**
- `SUSPECT → OFFLINE` requires either one high-weight signal *confirmed by an active
  probe*, or two consecutive independent OFFLINE reads. A single PubSub blip never
  triggers rotation on its own.
- **Grace period (default 90s, configurable)** absorbs ad breaks, brief encoder
  drops, and `commercial` events.
- `STALLED` is deliberately separate: the stream *is* live and telemetry *is*
  accepted, but Twitch isn't crediting (rerun, campaign ended, region-locked,
  account not linked). Rotating is right, but the Discord message must say
  "live but not crediting" — that distinction is what makes silent failures
  debuggable.
- Every transition emits an event carrying **which signals voted and how**, so
  `/status` and the logs explain *why* it switched, not just that it did.

---

## 5. Watcher loop (`watcher.py`)

```
resolve targets:
    1. configured priority list, in order → first that is live AND drops-eligible
    2. if none and auto_discovery enabled:
         query Overwatch directory filtered to DROPS_ENABLED, live, sorted by viewers
    3. if still none → IDLE, poll every 5 min, notify once (not every cycle)

while watching target:
    send minute-watched          (§3 spade)
    refresh drop progress        (every 3rd cycle, to stay light)
    evaluate liveness            (§4)
    on OFFLINE / STALLED → rotate
    on progress milestone → emit event
    on drop complete → auto-claim if enabled → emit event
```

Also: **campaign-aware prioritization** — among eligible live channels, prefer one
whose active campaign is closest to completion, then closest to expiry. Avoids
leaving a 90%-done drop to rot.

Crash recovery: on startup, reconcile local state against Twitch inventory before
watching anything, so a restart mid-drop doesn't double-count or miss a claim.

---

## 6. Discord layer

### Slash commands
| Command | Does |
| --- | --- |
| `/status` | Live embed: target, session uptime, minutes watched, per-campaign progress bars, ETA to next drop, current liveness signals |
| `/watch add <channel> [priority]` | Add to priority list (autocompletes from active campaigns) |
| `/watch remove <channel>` · `/watch list` | Manage the list |
| `/pause` · `/resume` | Stop/start watching without killing the process |
| `/drops` | Inventory: in-progress, claimable, claimed this campaign |
| `/claim [all]` | Manually claim pending drops |
| `/config set <key> <value>` · `/config show` | Poll intervals, grace period, auto-discovery, auto-claim, notify channel, ping role |
| `/login` | DMs the device-code activation link |
| `/logs [n]` | Last n state transitions with signal breakdown |

Gated to an owner ID + optional role. Guild-scoped command sync for instant updates.

### Notifications (event bus → channel)
- Started watching **X** (and *why* X)
- Switched target: **X → Y**, reason (`offline` / `stalled` / `higher priority live`)
- Drop progress: 25 / 50 / 75%
- **Drop claimed** — name + reward image
- Stream ended
- Campaign expiring in <24h with incomplete progress ⚠️
- Auth expiring / expired 🔴
- `SchemaDriftError` — API changed 🔴
- No eligible channels (once per idle period, not per poll)

Pinned auto-updating status embed, edited in place on a throttle (≤1 edit/60s) so
it never hits Discord rate limits.

---

## 7. Build phases

| Phase | Deliverable | Done when |
| --- | --- | --- |
| ~~1~~ | ~~Skeleton, config, SQLite, event bus, device-code auth~~ | **DONE** — device flow verified against live Twitch; 19 tests |
| ~~2~~ | ~~GQL client, live check, spade watch loop~~ | **DONE** — all transports verified live; 53 tests. See §10 |
| ~~3~~ | ~~Liveness detector + state machine + rotation~~ | **DONE** — 76 tests; verified live. See §11 |
| 4 | Drop tracking ~~+ auto-claim~~ | Tracking **DONE**; auto-claim is **impossible** — see §12 |
| 5 | Discord bot: commands, embeds, notifications | Full control from Discord, no shell access needed |
| 6 | Hardening: backoff, reconnect, crash recovery, Task Scheduler service | Survives network loss, Twitch 5xx, and reboot |

Phase 2 is the gate. If watch minutes don't credit, nothing downstream matters —
validate that before building anything else.

---

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| Twitch rotates GraphQL hashes / spade contract | All hashes in one registry file; typed `SchemaDriftError` → loud Discord alert instead of silent zero-progress |
| Account action for automation | User's accepted risk. Jittered intervals, single session, no view-count inflation beyond one viewer, no chat activity |
| Token theft | `tokens.json` at `0600`, never logged, never echoed to Discord |
| Rate limits | Per-operation concurrency caps, backoff with jitter, embed edit throttle |
| Silent non-crediting | That's exactly what `STALLED` (S5) exists to catch |
| ~~discord.py wheel on Python 3.14~~ | **Resolved** — discord.py 2.7.1, aiohttp 3.14.3, pydantic 2.13.4 all ship cp314 wheels. Staying on 3.14 |

---

## 9. Phase 1 notes (as built)

- **Client ID validated.** `ue6666qo983tsx6so1t0vnawi233wa` does support the device
  flow — live Twitch issued a real user code, 1800s TTL, 5s poll interval. It stays
  overridable in `config.toml` in case Twitch rotates it.
- **Config is three layers, not two.** Model defaults → `config.toml` → SQLite
  overrides. The bot never writes back to the TOML, so hand-edits survive
  `/config set`. Unknown keys and out-of-range values are rejected *at set time*;
  a stale key already in the DB only warns, so a renamed field can't brick startup.
- **Secrets are off the config model entirely.** `AppConfig` holds no secrets, so
  `/config show` can be dumped straight into Discord. `Secrets` is separate and
  env-only, and every token is registered with a logging filter that scrubs it from
  all records.
- **Empty env var ≠ set.** `DISCORD_TOKEN=` reads as unconfigured, not as an empty
  token — otherwise phase 5 would fail at connect time instead of at `doctor`.
- **Token file is written atomically** (temp + `replace`) so a crash mid-write can't
  leave a truncated token, and locked to the current user via `icacls` on Windows —
  verified: inheritance stripped, only `admin:(F)` on the ACL.
- **One refresh under concurrency.** `ensure_valid()` holds a lock, so a burst of
  callers produces exactly one refresh request. Covered by test.
- **A revoked refresh token clears state** and emits `AUTH_EXPIRED` rather than
  hot-looping on a token that will never work again.

---

## 10. Phase 2 notes (as built)

Everything below was established against live Twitch, not from documentation.

- **Raw query documents work, so the bot is no longer hash-dependent.** This is the
  single biggest change from the plan. §3 assumed persisted-query hashes were the
  only option and that rotation was the top risk. In fact Twitch accepts raw
  GraphQL documents on the web Client-ID, so `_DOCS` holds documents we wrote and
  verified, and `prefer_documents` defaults to true. Hashes remain as a fallback.
  Rotation drops from "top risk" to "cosmetic" — `gql-check` currently reports 4
  of 7 hashes already stale while every operation still works.
- **Two schema facts that cost a round trip each.** `Channel` has no `login` field
  (it's `name`), and `Game.streams` accepts no `sortTypeIsRecency` argument even
  though the web client sends one as a top-level variable.
- **`ViewerDropsDashboard` is unavailable and has been dropped.** It returns
  `failed integrity check` — it sits behind the Client-Integrity header, which is
  Twitch's anti-automation gate. Not worked around. It isn't needed: `Inventory`
  returns `dropCampaignsInProgress` with per-drop minute counts and claim state,
  which is what phases 4 and 5 run on. The only capability lost is browsing
  campaigns that haven't started.
- **The GQL client uses a separate Client-ID from auth.** The token comes from the
  TV client (the only one offering the device flow) but GQL calls must identify as
  the web client. Two config fields, deliberately.
- **A rejected persisted hash arrives as a GraphQL error, not an HTTP status.**
  Found by test: the document fallback was unreachable because only the POST was
  inside the `try`, while `PersistedQueryNotFound` is raised during response
  unwrapping. `gql-check` masked it by wrapping both calls itself.
- **`overwatchleague` is dead**, as expected — permanently offline. The live OWCS
  channel is `ow_esports`. The shipped `config.toml` should not lead with a
  channel that can never go live.
- **Twitch labels official rebroadcasts as `type: "live"`.** A stream titled
  `[REBROADCAST] OWCS 2026` reports as `live`, not `rerun`, and appears under the
  `DROPS_ENABLED` filter. So `type == "rerun"` is a *necessary* but not sufficient
  rerun check — the real authority on whether watching counts is S5 (progress
  delta), which is exactly why `STALLED` exists as a separate state.
- **Telemetry needs integers.** `channel_id`, `broadcast_id` and `user_id` go on
  the wire as numbers; the string forms are rejected.
- **`stdout` is line-buffered.** `watch` prints one line a minute for hours, and
  redirected to a file it would otherwise show nothing until exit.

---

## 11. Phase 3 notes (as built)

- **Specified in the simulator, then ported.** `ui/console.html` ran the state
  machine and its tests before any Python existed, and it caught two design bugs
  up front: rotating in the same tick as entering `OFFLINE`/`STALLED` made those
  states invisible, and rotation that didn't exclude the outgoing channel rotated
  in place forever. Both were fixed in the design before they could ship.
- **`commercial` beats the grace period.** The plan leaned on grace to absorb ad
  breaks. PubSub sends an explicit `commercial` frame with a duration, so S1 now
  abstains outright for that window — strictly better than waiting out a timer,
  and grace remains only as a backstop for encoder drops.
- **The detector performs no I/O, so the whole machine is synchronously testable.**
  It returns `needs_probe` and lets the watcher run the active probe, rather than
  owning a network call. Every phase 3 test runs on a fake clock with no sleeping.
- **One time source.** `PlaybackState.in_commercial` originally called
  `time.monotonic()` itself, which meant a fake clock couldn't govern the ad-break
  path — the test failed for the right reason. The detector now compares against
  its own clock.
- **Abstention is a vote.** `S1=UNKNOWN` for the first cycle of every session is
  correct: PubSub subscribes immediately but hears nothing until the next playback
  event, and inventing `ONLINE` there would be a guess.
- **Broadcast ids change under you.** A broadcaster who reconnects gets a new
  `stream_id`, and telemetry against the stale one credits nothing while still
  returning HTTP 204. The stream poll retargets on change.
- **Not built:** campaign-aware prioritisation from §5 (preferring the channel
  whose campaign is closest to completion or expiry). Selection is priority list
  then viewer count. Worth adding once phase 4 can enumerate per-campaign
  progress, since that's the data it needs.

---

## 12. Auto-claim is impossible (phase 4 finding)

`DropsPage_ClaimDropRewards` answers with `extensions.challenge.type == "integrity"`.
Claiming is gated behind the Client-Integrity token the real web player attaches,
which exists precisely to distinguish it from automation. **Not worked around** —
same decision as `ViewerDropsDashboard` in §10, and the second confirmed instance of
this gate.

So §6's `/claim [all]` command cannot be built as specified. What replaced it:

- `DROP_CLAIMABLE` fires within a cycle of a reward being earned, and
  `CLAIM_BLOCKED` fires **once** with the inventory URL.
- The dashboard shows a gold banner with a one-click link; `dropwatch drops --open`
  does the same from a shell.
- `DropsClient.claim()` is implemented correctly and left live rather than stubbed,
  so a lifted gate needs no code change. `watch.auto_claim` gates the attempt.
- `IntegrityChallengeError` is checked *before* the `errors` array, because Twitch
  emits a generic error alongside the challenge and reporting that reads as schema
  drift — sending someone after a field rename that never happened.

**Corollary worth remembering:** anything that mutates account state is likely
gated. Read paths have all worked; the only two refusals so far are a mutation and
a full-campaign browse. Assume phase 5's Discord controls can *observe and steer the
watcher* but cannot *act on Twitch* beyond what watching already does.

Other findings from wiring inventory up:

- **Campaigns credit in parallel.** Three were active simultaneously — OWCS MSC,
  OW 2026 S3 Midseason, EWC 2026 — all advancing from the same watch minutes. Any
  UI that shows "the" progress number is lying; the inventory is the real picture.
- **`DropCurrentSessionContext` names a drop, and it may not be the one you expect.**
  It reported the EWC Bronze drop while the OWCS campaign was the notional target,
  which is exactly how a progress readout ends up mislabelled. Show inventory data
  to humans; use the session call only as proof telemetry is landing.
- **`dropInstanceID` is null until a drop is earned**, so it can't be pre-fetched.
  Absence on an unearned drop is expected, not drift.
```
