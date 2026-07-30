const fs = require("fs");
const h = fs.readFileSync(process.argv[2], "utf8");

// Every $("id") the script touches must exist in the markup.
const refs = [...h.matchAll(/\$\("([^"]+)"\)/g)].map(m => m[1]);
const unique = [...new Set(refs)];
const missing = unique.filter(id => !h.includes(`id="${id}"`));
console.log(missing.length
  ? "MISSING IDS: " + missing.join(", ")
  : `all ${unique.length} referenced ids exist`);

// Every CSS class used in markup/JS that looks like a state class should be defined.
const defined = new Set([...h.matchAll(/\.([a-z][a-z0-9-]*)\s*[,{:]/gi)].map(m => m[1]));
const stateClasses = ["s-watching","s-suspect","s-offline","s-stalled","s-idle","s-paused",
  "v-online","v-offline","v-unknown","v-degraded",
  "h-online","h-offline","h-unknown","h-degraded",
  "lv-crit","lv-warn","lv-ok","lv-good","w-high","w-med","w-low"];
const undef = stateClasses.filter(c => !defined.has(c));
console.log(undef.length ? "UNDEFINED CLASSES: " + undef.join(", ") : "all state classes styled");

// Both themes must define the same token set.
const block = name => {
  const re = new RegExp(name.replace(/[[\]"^$.*+?()|{}\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}");
  const m = h.match(re);
  return m ? new Set([...m[1].matchAll(/(--[a-z0-9-]+)\s*:/g)].map(x => x[1])) : null;
};
const root = block(":root");
for (const sel of ['@media (prefers-color-scheme: light)', ':root[data-theme="dark"]', ':root[data-theme="light"]']) {
  const b = sel.startsWith("@media")
    ? new Set([...h.match(/@media \(prefers-color-scheme: light\)\s*\{\s*:root\s*\{([^}]*)\}/)[1]
        .matchAll(/(--[a-z0-9-]+)\s*:/g)].map(x => x[1]))
    : block(sel);
  const miss = [...root].filter(t => !b.has(t));
  console.log(`${sel}: ${b.size} tokens` + (miss.length ? ` — missing ${miss.join(", ")}` : " — complete"));
}
