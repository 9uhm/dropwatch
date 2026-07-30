/* Charts for the dropwatch dashboard.
 *
 * Hand-rolled SVG rather than a charting library: the page is served by the bot
 * itself with no bundler and no CDN reachable, so every byte has to be local.
 *
 * Palette note — the chart colours are NOT the UI's status colours. Marks were
 * stepped into the dark lightness band (OKLCH L 0.48–0.67) and validated for
 * colourblind separation against the #0E131C surface; the brighter UI tokens
 * fail that band. Text never wears a series colour: a swatch beside the label
 * carries identity instead.
 *
 *   series 1  #8A66F2  reported by bot     (CVD ΔE 32.2 vs series 2)
 *   series 2  #C98500  credited by Twitch
 *   status    #2FA876 watching · #C98500 suspect/stalled · #D8474F offline
 */
(() => {
  "use strict";

  const SURFACE = "#0E131C";
  const C = {
    sent: "#8A66F2",
    credited: "#C98500",
    watching: "#2FA876",
    warn: "#C98500",
    crit: "#D8474F",
    idle: "#5A6878",
    grid: "#1C2431",
    axis: "#3A4655",
  };
  const STATE_COLOR = {
    WATCHING: C.watching, SUSPECT: C.warn, STALLED: C.warn,
    OFFLINE: C.crit, IDLE: C.idle, PAUSED: C.idle,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const svgEl = (tag, attrs) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    return n;
  };

  const fmtHM = (mins) => {
    if (mins === null || mins === undefined) return "—";
    const m = Math.round(mins);
    return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
  };
  const fmtClock = (ts) =>
    new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const fmtDay = (iso) => {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  };

  let windowHours = 6;
  let lastPayload = null;
  let tableMode = false;

  // ------------------------------------------------------------------ tooltip
  let tip = null;
  function showTip(html, x, y) {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "viz-tip";
      document.body.appendChild(tip);
    }
    tip.innerHTML = html;
    tip.style.display = "block";
    const pad = 14;
    const rect = tip.getBoundingClientRect();
    let left = x + pad;
    if (left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, y - rect.height - pad)}px`;
  }
  const hideTip = () => { if (tip) tip.style.display = "none"; };

  // -------------------------------------------------------- line / area chart
  /**
   * Two series on ONE axis — both are minutes, so a second scale would be a lie.
   * The gap between them is the whole point: what we reported vs what Twitch counted.
   */
  function drawSeries(host, series) {
    host.innerHTML = "";
    const points = series.filter((p) => p.credited !== null && p.credited !== undefined);
    if (points.length < 2) {
      host.innerHTML = `<div class="viz-empty">Not enough samples yet — the chart needs a
        couple of progress reads. Run the watcher for a few minutes.</div>`;
      return;
    }

    const W = host.clientWidth || 680, H = 210;
    const m = { t: 14, r: 46, b: 26, l: 40 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;

    const t0 = points[0].ts, t1 = points[points.length - 1].ts;
    const span = Math.max(1, t1 - t0);
    const maxY = Math.max(1, ...points.map((p) => Math.max(p.credited || 0, p.sent || 0)));
    const niceMax = Math.ceil(maxY / 10) * 10 || 10;

    const X = (ts) => m.l + ((ts - t0) / span) * iw;
    const Y = (v) => m.t + ih - (v / niceMax) * ih;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`, width: "100%", height: H,
      role: "img", "aria-label": "Watch minutes reported and credited over time",
    });

    // Gridlines: hairline, solid, recessive — never dashed.
    for (let i = 0; i <= 4; i++) {
      const v = (niceMax / 4) * i;
      svg.appendChild(svgEl("line", {
        x1: m.l, x2: m.l + iw, y1: Y(v), y2: Y(v), stroke: C.grid, "stroke-width": 1,
      }));
      const label = svgEl("text", {
        x: m.l - 7, y: Y(v) + 3.5, "text-anchor": "end", class: "viz-tick",
      });
      label.textContent = String(Math.round(v));
      svg.appendChild(label);
    }

    for (const key of ["sent", "credited"]) {
      const valid = points.filter((p) => p[key] !== null && p[key] !== undefined);
      if (valid.length < 2) continue;
      const d = valid.map((p, i) => `${i ? "L" : "M"}${X(p.ts).toFixed(1)},${Y(p[key]).toFixed(1)}`).join(" ");

      if (key === "credited") {
        // Area wash at ~10% under the authoritative series only; two filled
        // areas would muddy each other.
        const base = `${d} L${X(valid[valid.length - 1].ts).toFixed(1)},${Y(0)} L${X(valid[0].ts).toFixed(1)},${Y(0)} Z`;
        svg.appendChild(svgEl("path", { d: base, fill: C[key], "fill-opacity": 0.1, stroke: "none" }));
      }
      svg.appendChild(svgEl("path", {
        d, fill: "none", stroke: C[key], "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      }));

      // End marker: >=8px with a 2px surface ring so overlaps stay readable.
      const last = valid[valid.length - 1];
      svg.appendChild(svgEl("circle", {
        cx: X(last.ts), cy: Y(last[key]), r: 4.5,
        fill: C[key], stroke: SURFACE, "stroke-width": 2,
      }));
      const endLabel = svgEl("text", {
        x: X(last.ts) + 9, y: Y(last[key]) + 4, class: "viz-endlabel",
      });
      endLabel.textContent = String(last[key]);
      svg.appendChild(endLabel);
    }

    // x labels: first and last only — a label per sample would be noise.
    for (const [ts, anchor] of [[t0, "start"], [t1, "end"]]) {
      const tx = svgEl("text", {
        x: anchor === "start" ? m.l : m.l + iw, y: H - 8,
        "text-anchor": anchor, class: "viz-tick",
      });
      tx.textContent = fmtClock(ts);
      svg.appendChild(tx);
    }

    // Crosshair + tooltip.
    const cross = svgEl("line", {
      y1: m.t, y2: m.t + ih, stroke: C.axis, "stroke-width": 1, opacity: 0,
    });
    svg.appendChild(cross);
    const hit = svgEl("rect", {
      x: m.l, y: m.t, width: iw, height: ih, fill: "transparent", cursor: "crosshair",
    });
    svg.appendChild(hit);

    hit.addEventListener("mousemove", (ev) => {
      const box = svg.getBoundingClientRect();
      const px = ((ev.clientX - box.left) / box.width) * W;
      const ts = t0 + ((px - m.l) / iw) * span;
      let near = points[0];
      for (const p of points) if (Math.abs(p.ts - ts) < Math.abs(near.ts - ts)) near = p;
      cross.setAttribute("x1", X(near.ts));
      cross.setAttribute("x2", X(near.ts));
      cross.setAttribute("opacity", 1);
      showTip(
        `<div class="tip-h">${esc(fmtClock(near.ts))} · ${esc(near.channel || "")}</div>
         <div class="tip-r"><i style="background:${C.credited}"></i>credited
           <b>${esc(near.credited)}</b></div>
         <div class="tip-r"><i style="background:${C.sent}"></i>reported
           <b>${esc(near.sent)}</b></div>
         <div class="tip-s">${esc(near.state || "")}</div>`,
        ev.clientX, ev.clientY,
      );
    });
    hit.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); hideTip(); });

    host.appendChild(svg);
  }

  // ------------------------------------------------------------- daily columns
  function drawDaily(host, daily) {
    host.innerHTML = "";
    if (!daily.length) {
      host.innerHTML = `<div class="viz-empty">No completed sessions yet.</div>`;
      return;
    }

    const W = host.clientWidth || 680, H = 150;
    const m = { t: 16, r: 10, b: 24, l: 36 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const maxY = Math.max(1, ...daily.map((d) => d.minutes || 0));
    const niceMax = Math.ceil(maxY / 30) * 30 || 30;

    const band = iw / daily.length;
    const barW = Math.min(24, band - 2);   // cap at 24px; the leftover is air

    const svg = svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`, width: "100%", height: H,
      role: "img", "aria-label": "Watch minutes reported per day",
    });

    for (let i = 0; i <= 3; i++) {
      const v = (niceMax / 3) * i;
      const y = m.t + ih - (v / niceMax) * ih;
      svg.appendChild(svgEl("line", {
        x1: m.l, x2: m.l + iw, y1: y, y2: y, stroke: C.grid, "stroke-width": 1,
      }));
      const t = svgEl("text", { x: m.l - 7, y: y + 3.5, "text-anchor": "end", class: "viz-tick" });
      t.textContent = String(Math.round(v));
      svg.appendChild(t);
    }

    daily.forEach((d, i) => {
      const mins = d.minutes || 0;
      const h = (mins / niceMax) * ih;
      const x = m.l + band * i + (band - barW) / 2;
      const y = m.t + ih - h;

      // 4px rounded top, square at the baseline.
      const r = Math.min(4, h);
      const path = h <= 0 ? "" :
        `M${x},${m.t + ih} L${x},${y + r} Q${x},${y} ${x + r},${y} ` +
        `L${x + barW - r},${y} Q${x + barW},${y} ${x + barW},${y + r} ` +
        `L${x + barW},${m.t + ih} Z`;
      if (path) {
        const bar = svgEl("path", { d: path, fill: C.credited, cursor: "pointer" });
        bar.addEventListener("mouseenter", (ev) => showTip(
          `<div class="tip-h">${esc(fmtDay(d.day))}</div>
           <div class="tip-r"><i style="background:${C.credited}"></i>reported
             <b>${esc(fmtHM(mins))}</b></div>
           <div class="tip-s">${esc(d.sessions)} session${d.sessions === 1 ? "" : "s"}</div>`,
          ev.clientX, ev.clientY));
        bar.addEventListener("mouseleave", hideTip);
        svg.appendChild(bar);
      }

      // Label only the tallest column — never a number on every bar.
      if (mins === maxY && mins > 0) {
        const lab = svgEl("text", {
          x: x + barW / 2, y: y - 5, "text-anchor": "middle", class: "viz-endlabel",
        });
        lab.textContent = fmtHM(mins);
        svg.appendChild(lab);
      }

      if (daily.length <= 10 || i % 2 === 0) {
        const t = svgEl("text", {
          x: x + barW / 2, y: H - 7, "text-anchor": "middle", class: "viz-tick",
        });
        t.textContent = fmtDay(d.day);
        svg.appendChild(t);
      }
    });

    host.appendChild(svg);
  }

  // ------------------------------------------------------- state distribution
  function drawStates(host, transitions) {
    host.innerHTML = "";
    if (!transitions.length) {
      host.innerHTML = `<div class="viz-empty">No transitions recorded yet.</div>`;
      return;
    }
    const total = transitions.reduce((a, x) => a + x.n, 0) || 1;
    // Status ships with a label, never colour alone.
    host.innerHTML = transitions.map((x) => {
      const pct = (x.n / total) * 100;
      const color = STATE_COLOR[x.to_state] || C.idle;
      return `<div class="viz-bar-row">
        <span class="viz-bar-label"><i class="sw" style="background:${color}"></i>${esc(x.to_state)}</span>
        <span class="viz-bar-track"><i style="width:${pct.toFixed(1)}%;background:${color}"></i></span>
        <span class="viz-bar-val">${esc(x.n)}</span>
      </div>`;
    }).join("");
  }

  function drawChannels(host, channels) {
    host.innerHTML = "";
    if (!channels.length) {
      host.innerHTML = `<div class="viz-empty">No sessions recorded yet.</div>`;
      return;
    }
    const max = Math.max(1, ...channels.map((c) => c.minutes || 0));
    host.innerHTML = channels.map((c) => {
      const pct = ((c.minutes || 0) / max) * 100;
      return `<div class="viz-bar-row">
        <span class="viz-bar-label"><i class="sw" style="background:${C.credited}"></i>${esc(c.channel)}</span>
        <span class="viz-bar-track"><i style="width:${pct.toFixed(1)}%;background:${C.credited}"></i></span>
        <span class="viz-bar-val">${esc(fmtHM(c.minutes))}</span>
      </div>`;
    }).join("");
  }

  // ------------------------------------------------------------------- table
  function drawTable(payload) {
    const rows = payload.series.filter((p) => p.credited !== null && p.credited !== undefined);
    return `<div class="viz-tablewrap"><table class="viz-table">
      <thead><tr><th>Time</th><th>Channel</th><th>State</th>
        <th style="text-align:right">Reported</th>
        <th style="text-align:right">Credited</th></tr></thead>
      <tbody>${rows.slice(-60).reverse().map((p) => `<tr>
        <td>${esc(fmtClock(p.ts))}</td><td>${esc(p.channel || "")}</td>
        <td>${esc(p.state || "")}</td>
        <td style="text-align:right">${esc(p.sent)}</td>
        <td style="text-align:right">${esc(p.credited)}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  // ------------------------------------------------------------------ render
  function render(payload) {
    lastPayload = payload;
    const t = payload.totals || {};

    $("statHours").textContent = t.hours_reported ? `${t.hours_reported}` : "0";
    $("statSessions").textContent = t.sessions ?? 0;
    $("statAvg").textContent = fmtHM(t.avg_session_minutes);
    $("statLongest").textContent = fmtHM(t.longest_session_minutes);

    if (tableMode) {
      $("chartSeries").innerHTML = drawTable(payload);
    } else {
      drawSeries($("chartSeries"), payload.series || []);
    }
    drawDaily($("chartDaily"), payload.daily || []);
    drawStates($("chartStates"), payload.transitions || []);
    drawChannels($("chartChannels"), payload.channels || []);
  }

  async function poll() {
    try {
      const r = await fetch(`/api/stats?hours=${windowHours}`);
      if (r.ok) render(await r.json());
    } catch { /* the connection pill reports trouble */ }
  }

  // ----------------------------------------------------------------- wiring
  // Filters in one row above the charts.
  document.querySelectorAll("[data-range]").forEach((btn) => {
    btn.addEventListener("click", () => {
      windowHours = Number(btn.dataset.range);
      document.querySelectorAll("[data-range]").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      poll();
    });
  });
  $("btnTable").addEventListener("click", () => {
    tableMode = !tableMode;
    $("btnTable").classList.toggle("on", tableMode);
    $("btnTable").textContent = tableMode ? "Chart" : "Table";
    if (lastPayload) render(lastPayload);
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (lastPayload) render(lastPayload); }, 150);
  });

  poll();
  setInterval(poll, 20000);
  window.dropwatchStatsRefresh = poll;
})();
