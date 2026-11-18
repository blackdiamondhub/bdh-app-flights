(function () {
  const root = document.getElementById("fl");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const base = root.dataset.baseUrl;
  let state = JSON.parse($("fl-initial").textContent);
  let selected = null, mode = "ident", picked = null, positions = {};

  function csrf() {
    const el = document.querySelector("[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }
  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  }
  function toast(msg) {
    const t = $("fl-toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, 3000);
  }
  const money = v => `$${Number(v || 0).toFixed(2)}`;
  const pillClass = p => p.toLowerCase().replace(/\s+/g, "-");
  function ago(sec) {
    const m = Math.round(sec / 60);
    return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.floor(m / 60)}h ${m % 60}m ago`;
  }
  function route(f) {
    return `${esc((f.origin || {}).code)} → ${esc((f.destination || {}).code)}`;
  }
  function city(a) { return esc((a || {}).city || (a || {}).name || ""); }

  function progress(f) {
    if (f.phase === "landed") return 100;
    if (f.phase !== "air") return 0;
    if (f.progress != null) return Math.max(2, Math.min(98, f.progress));
    const off = f.act_off, on = f.est_on || f.est_in || f.sched_in;
    if (!off || !on || on <= off) return 50;
    return Math.max(2, Math.min(98, 100 * (state.now - off) / (on - off)));
  }

  function card(f) {
    const err = f.error ? `<div class="fl-errline">${esc(f.error)}</div>` : "";
    return `<div class="fl-card${f.id === selected ? " sel" : ""}${["landed", "cancelled"].includes(f.phase) ? " dim" : ""}" data-id="${esc(f.id)}">
      <div class="fl-what"><b>${esc(f.ident)}</b><span class="fl-route">${route(f)}</span>
        <div class="fl-sub">${esc(f.detail)}</div>${err}</div>
      <span class="fl-pill ${pillClass(f.pill)}">${esc(f.pill)}</span></div>`;
  }

  function detail(f) {
    const L = f.labels || {}, o = f.origin || {}, d = f.destination || {};
    const pct = progress(f);
    const left = f.act_off ? `Off ${L.act_off}` : `Departs ${L.est_out || L.sched_out}`;
    const right = f.act_in ? `At gate ${L.act_in}` : f.act_on ? `Landed ${L.act_on}` : `Arrives ${L.est_in || L.sched_in || L.est_on}`;
    const plane = f.phase === "air" ? `<b style="left:${pct}%"><i class="fa-solid fa-plane"></i></b>` : "";
    const depGate = [f.terminal_out && `Terminal ${esc(f.terminal_out)}`, f.gate_out && `Gate ${esc(f.gate_out)}`].filter(Boolean).join(" · ") || "—";
    const arrGate = [f.gate_in && `Gate ${esc(f.gate_in)}`, f.baggage && `Bags ${esc(f.baggage)}`].filter(Boolean).join(" · ") || "—";
    const pos = positions[f.id];
    const posText = f.phase !== "air" ? "—" : pos ? `${Math.round(pos.alt_ft).toLocaleString()} ft · ${esc(pos.speed_kt)} kt` : "…";
    const late = f.arr_delay >= 300 ? ` (+${Math.round(f.arr_delay / 60)}m)` : f.dep_delay >= 300 ? ` (+${Math.round(f.dep_delay / 60)}m)` : "";
    const est = (L.est_in || L.est_out) ? `${esc(L.sched_in || L.sched_out)} → ${esc(L.est_in || L.est_out)}${late}` : esc(L.sched_in || L.sched_out);
    const updated = f.last_poll ? `Updated ${ago(state.now - f.last_poll)}` : "Not looked up yet";
    const next = f.next_poll ? ` · next ${esc(f.next_label || "")}` : "";
    return `<div class="fl-top"><div><b>${esc(f.ident)}</b>
        <div class="fl-sub">${[f.registration, f.aircraft_type].filter(Boolean).map(esc).join(" · ")}</div></div>
        <span class="fl-pill ${pillClass(f.pill)}">${esc(f.pill)}</span></div>
      <div class="fl-big">
        <div class="fl-ap">${esc(o.code)}<small>${city(o)}<br>${esc(left)}</small></div>
        <div class="fl-bar"><i style="width:${pct}%"></i>${plane}</div>
        <div class="fl-ap r">${esc(d.code)}<small>${city(d)}<br>${esc(right)}</small></div>
      </div>
      <div class="fl-grid">
        <div class="fl-cell"><div class="fl-sub">Departure</div>${depGate}</div>
        <div class="fl-cell"><div class="fl-sub">Arrival</div>${arrGate}</div>
        <div class="fl-cell"><div class="fl-sub">Altitude · speed</div>${posText}</div>
        <div class="fl-cell"><div class="fl-sub">Scheduled → estimated</div>${est}</div>
      </div>
      ${f.error ? `<div class="fl-errline">${esc(f.error)}</div>` : ""}
      <div class="fl-foot"><span>${updated}${next}</span>
        <span class="fl-btns">
          <button class="fl-btn small ghost" data-act="${f.popups ? "popups_off" : "popups_on"}">
            <i class="fa-solid ${f.popups ? "fa-bell" : "fa-bell-slash"}"></i>${f.popups ? "Popups on" : "Popups off"}</button>
          <button class="fl-btn small ghost" data-act="refresh"><i class="fa-solid fa-rotate"></i>Refresh</button>
          <button class="fl-btn small ghost" data-act="remove"><i class="fa-solid fa-xmark"></i>Stop tracking</button>
        </span></div>`;
  }

  function render() {
    const fl = state.flights || [];
    if (!fl.find(f => f.id === selected)) selected = fl.length ? fl[0].id : null;
    $("fl-count").textContent = fl.length ? `${fl.length} tracked` : "";
    const b = state.budget || {};
    $("fl-budget").textContent = b.limit != null ? `FlightAware ${money(b.spent)} / ${money(b.limit)} this month` : "";
    $("fl-nokey").hidden = !!state.has_key;
    $("fl-empty").hidden = fl.length > 0;
    $("fl-list").innerHTML = fl.map(card).join("");
    const cur = fl.find(f => f.id === selected);
    $("fl-detail").innerHTML = cur ? detail(cur) : "";
    if (cur && cur.phase === "air" && !(cur.id in positions)) loadPosition(cur.id);
  }

  async function loadPosition(id) {
    positions[id] = null;
    try {
      const r = await fetch(`${base}api/flights/${id}/position/`, {credentials: "same-origin"});
      const data = await r.json();
      if (r.ok) { positions[id] = data.position; if (data.budget) state.budget = data.budget; render(); }
    } catch (e) { /* leave it blank */ }
    setTimeout(() => { delete positions[id]; }, 5 * 60 * 1000);
  }

  $("fl-list").addEventListener("click", e => {
    const c = e.target.closest(".fl-card");
    if (c) { selected = c.dataset.id; render(); }
  });
  $("fl-detail").addEventListener("click", async e => {
    const b = e.target.closest("[data-act]");
    if (!b || !selected) return;
    b.disabled = true;
    try {
      state = await post(`${base}api/flights/${selected}/${b.dataset.act}/`);
      if (b.dataset.act === "remove") toast("Stopped tracking");
      render();
    } catch (err) { toast(err.message); b.disabled = false; }
  });

  // --- add sheet ---
  function today() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }
  function setMode(m) {
    mode = m;
    document.querySelectorAll("#fl-seg button").forEach(b => b.classList.toggle("on", b.dataset.mode === m));
    $("fl-by-ident").hidden = m !== "ident";
    $("fl-by-route").hidden = m !== "route";
    const c = state.costs || {};
    $("fl-cost").textContent = `A search costs about ${Math.max(1, Math.round(100 * (m === "route" ? c.route : c.flights) || 1))}¢`;
  }
  function openSheet() {
    picked = null;
    $("fl-results").innerHTML = ""; $("fl-err").textContent = "";
    $("fl-track").disabled = true; $("fl-popups").checked = false;
    if (!$("fl-date").value) $("fl-date").value = today();
    setMode(mode);
    $("fl-sheet").hidden = false;
    (mode === "ident" ? $("fl-ident") : $("fl-origin")).focus();
  }
  $("fl-new").addEventListener("click", openSheet);
  $("fl-cancel").addEventListener("click", () => { $("fl-sheet").hidden = true; });
  $("fl-seg").addEventListener("click", e => {
    const b = e.target.closest("button");
    if (b) setMode(b.dataset.mode);
  });
  async function find() {
    $("fl-err").textContent = ""; $("fl-results").innerHTML = ""; picked = null; $("fl-track").disabled = true;
    const btn = $("fl-find");
    btn.disabled = true;
    try {
      const data = await post(root.dataset.searchUrl, {
        mode, ident: $("fl-ident").value, origin: $("fl-origin").value, destination: $("fl-dest").value,
        date: $("fl-date").value,
      });
      if (data.budget) state.budget = data.budget;
      const res = data.results || [];
      if (!res.length) { $("fl-err").textContent = "No flights found for that day."; return; }
      $("fl-results").innerHTML = res.map(f => {
        const L = f.labels || {};
        const extra = [f.aircraft_type, f.live ? "" : "from the airline schedule"].filter(Boolean).map(esc).join(" · ");
        return `<div class="fl-opt" data-ref="${esc(f.ref)}"><div><b>${esc(f.ident)}</b> ${city(f.origin) || esc(f.origin.code)} → ${city(f.destination) || esc(f.destination.code)}
          <div class="fl-sub">Dep ${esc(L.est_out || L.sched_out)} · Arr ${esc(L.est_in || L.sched_in)}${extra ? " · " + extra : ""}</div></div>
          <span class="fl-pill ${pillClass(f.pill)}">${esc(f.pill)}</span></div>`;
      }).join("");
      if (res.length === 1) pick($("fl-results").firstElementChild);
    } catch (err) {
      $("fl-err").textContent = err.message;
    } finally { btn.disabled = false; render(); }
  }
  function pick(el) {
    document.querySelectorAll(".fl-opt").forEach(o => o.classList.toggle("sel", o === el));
    picked = el.dataset.ref;
    $("fl-track").disabled = false;
  }
  $("fl-find").addEventListener("click", find);
  ["fl-ident", "fl-origin", "fl-dest"].forEach(id => $(id).addEventListener("keydown", e => { if (e.key === "Enter") find(); }));
  $("fl-results").addEventListener("click", e => {
    const o = e.target.closest(".fl-opt");
    if (o) pick(o);
  });
  $("fl-track").addEventListener("click", async () => {
    if (!picked) return;
    $("fl-track").disabled = true;
    try {
      state = await post(root.dataset.trackUrl, {ref: picked, popups: $("fl-popups").checked});
      const newest = (state.flights || []).reduce((a, f) => (!a || f.added > a.added ? f : a), null);
      if (newest) selected = newest.id;
      $("fl-sheet").hidden = true;
      render();
      toast("Tracking");
    } catch (err) { $("fl-err").textContent = err.message; $("fl-track").disabled = false; }
  });

  async function poll() {
    try {
      const r = await fetch(root.dataset.stateUrl, {credentials: "same-origin"});
      if (r.ok) { state = await r.json(); render(); }
    } catch (e) { /* keep what we have */ }
  }
  render();
  setInterval(poll, 30000);
})();
