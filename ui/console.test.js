/**
 * Black-box test of the console's liveness state machine.
 * Stubs just enough DOM for the script to run, captures the tick function via
 * setInterval, then drives faults through the real click handlers and reads the
 * resulting state out of the rendered fake DOM.
 */
const fs = require("fs");

const html = fs.readFileSync(process.argv[2], "utf8");
const src = html.match(/<script>([\s\S]*)<\/script>/)[1];

const handlers = {};           // id -> { event: fn }
const els = {};

function mkEl(id) {
  const el = {
    id, textContent: "", innerHTML: "", className: "", value: "",
    style: {}, dataset: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener(ev, fn) { (handlers[id] ||= {})[ev] = fn; },
    closest() { return null; },
    sort: undefined,
  };
  return el;
}

global.document = {
  getElementById: id => (els[id] ||= mkEl(id)),
};
let tickFn = null;
global.setInterval = fn => { tickFn = fn; return 0; };

eval(src);
if (!tickFn) throw new Error("tick was never registered");

const state = () => els.stateName.textContent;
const rotations = () => Number(els.mRot.textContent);
const credited = () => Number(els.mCredited.textContent);
const click = id => {
  const h = handlers[id] && handlers[id].click;
  if (!h) throw new Error("no click handler for " + id);
  h({ target: { closest: () => null } });
};
const run = n => { for (let i = 0; i < n; i++) tickFn(); };
const seen = new Set();
const runWatching = n => { for (let i = 0; i < n; i++) { tickFn(); seen.add(state()); } };

let fails = 0;
function check(label, cond, detail) {
  if (!cond) fails++;
  console.log(`  [${cond ? "PASS" : "FAIL"}] ${label}${detail ? "  — " + detail : ""}`);
}

console.log("\nliveness state machine\n");

// 1 — cold start picks the highest-priority live, eligible channel.
run(3);
check("cold start reaches WATCHING", state() === "WATCHING", `state=${state()}`);
check("picked highest-priority live channel", els.tgtLogin.textContent === "overwatchleague",
  `target=${els.tgtLogin.textContent}`);
check("credits watch minutes", credited() > 0, `credited=${credited()}`);

// 2 — an ad break must be absorbed, never rotate.
const rotBefore = rotations();
click("fAd");
seen.clear();
runWatching(12);
check("ad break enters SUSPECT", seen.has("SUSPECT"), `states seen: ${[...seen].join(",")}`);
check("ad break returns to WATCHING", state() === "WATCHING", `state=${state()}`);
check("ad break causes no rotation", rotations() === rotBefore,
  `rotations ${rotBefore} -> ${rotations()}`);

// 3 — a dead PubSub socket must abstain, not vote OFFLINE.
click("fClear");
run(2);
const rotBefore2 = rotations();
click("fSocket");
seen.clear();
runWatching(14);
check("dead socket never reaches SUSPECT", !seen.has("SUSPECT"),
  `states seen: ${[...seen].join(",")}`);
check("dead socket causes no rotation", rotations() === rotBefore2,
  `rotations ${rotBefore2} -> ${rotations()}`);
click("fSocket");   // restore

// 4 — stopped crediting must land in STALLED, not OFFLINE.
run(2);
click("fStall");
seen.clear();
runWatching(14);
check("no crediting reaches STALLED", seen.has("STALLED"), `states seen: ${[...seen].join(",")}`);
check("no crediting never reaches OFFLINE", !seen.has("OFFLINE"),
  `states seen: ${[...seen].join(",")}`);

// 5 — a genuine stream end must confirm, then rotate.
click("fClear");
run(3);
const rotBefore3 = rotations();
click("fEnd");
seen.clear();
runWatching(40);
check("stream end reaches OFFLINE", seen.has("OFFLINE"), `states seen: ${[...seen].join(",")}`);
check("stream end triggers rotation", rotations() > rotBefore3,
  `rotations ${rotBefore3} -> ${rotations()}`);

// 6 — grace period is actually honoured (raise it, confirm it holds longer).
console.log("\ngrace period honoured\n");
function ticksToOffline(graceSeconds) {
  for (const k of Object.keys(els)) delete els[k];
  for (const k of Object.keys(handlers)) delete handlers[k];
  tickFn = null;
  eval(src);
  handlers.kGrace.input({ target: { value: String(graceSeconds) } });
  run(3);
  click("fEnd");
  for (let i = 0; i < 200; i++) { tickFn(); if (state() === "OFFLINE") return i + 1; }
  return -1;
}
const t30 = ticksToOffline(30);
const t180 = ticksToOffline(180);
check("longer grace holds longer before OFFLINE", t180 > t30,
  `30s grace -> ${t30} ticks, 180s grace -> ${t180} ticks`);

console.log(fails ? `\n${fails} check(s) FAILED\n` : "\nall checks passed\n");
process.exit(fails ? 1 : 0);
