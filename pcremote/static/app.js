(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  // ------------------------------------------------------------ storage
  const store = {
    get(k, d) { try { const v = localStorage.getItem("pcr." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("pcr." + k, JSON.stringify(v)); } catch { /* private mode */ } },
    del(k) { try { localStorage.removeItem("pcr." + k); } catch { } },
  };

  const settings = Object.assign({ sens: 1.6, scroll: 1.5, natural: true }, store.get("settings", {}));
  let token = store.get("token", null);

  // --------------------------------------------------------- connection
  const statusEl = $("#status"), statusText = $("#status-text");
  let ws = null, authed = false, retry = 0, retryTimer = null, lastRx = 0, pcName = "PC";

  function setStatus(kind, text) {
    statusEl.className = "status " + kind;
    statusText.textContent = text;
  }

  function connect() {
    clearTimeout(retryTimer);
    if (!token) return showPairing();
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    authed = false;
    setStatus("", retry ? "Reconnecting…" : "Connecting…");
    const sock = new WebSocket(`ws://${location.host}/ws`);
    ws = sock;
    sock.onopen = () => { lastRx = Date.now(); sock.send(JSON.stringify({ t: "auth", token })); };
    sock.onmessage = (ev) => {
      lastRx = Date.now();
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.t === "auth_ok") {
        authed = true; retry = 0; pcName = msg.name || "PC";
        setStatus("ok", pcName);
      } else if (msg.t === "auth_fail") {
        token = null; store.del("token");
        showPairing("This device is no longer paired. Enter the PIN from your PC.");
      }
    };
    sock.onclose = () => {
      if (ws !== sock) return;
      ws = null; authed = false;
      if (!token) return;
      setStatus("bad", "Disconnected — retrying…");
      const delay = Math.min(250 * 2 ** retry++, 3000);
      retryTimer = setTimeout(connect, delay);
    };
    sock.onerror = () => { /* onclose follows */ };
  }

  function reconnectNow() {
    retry = 0;
    if (ws) { const old = ws; ws = null; try { old.close(); } catch { } }
    connect();
  }

  function send(msg) {
    if (ws && authed && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(msg)); return true; }
    return false;
  }

  // Heartbeat: detect dead sockets quickly (Wi-Fi drops, phone sleep).
  setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (Date.now() - lastRx > 6000) return reconnectNow();
    ws.send(JSON.stringify({ t: "ping" }));
  }, 2000);

  // iOS suspends pages in the background; reconnect the moment we return.
  function onResume() {
    if (document.visibilityState !== "visible" || !token) return;
    if (!ws || ws.readyState !== WebSocket.OPEN || Date.now() - lastRx > 3000) reconnectNow();
    else ws.send(JSON.stringify({ t: "ping" }));
  }
  document.addEventListener("visibilitychange", onResume);
  window.addEventListener("pageshow", onResume);
  window.addEventListener("focus", onResume);
  window.addEventListener("online", reconnectNow);

  // ------------------------------------------------------------ pairing
  const pairEl = $("#pair"), pinEl = $("#pin"), pairErr = $("#pair-error");

  function showPairing(err) {
    setStatus("bad", "Not paired");
    pairErr.textContent = err || "";
    pairEl.hidden = false;
    setTimeout(() => pinEl.focus(), 50);
  }

  async function pair(pin) {
    pairErr.textContent = "";
    const btn = $("#pair-form button");
    btn.disabled = true;
    try {
      const name = /iPad/.test(navigator.userAgent) ? "iPad" : /iPhone/.test(navigator.userAgent) ? "iPhone" : "Phone";
      const res = await fetch("/api/pair", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin: pin.replace(/\D/g, ""), name }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.token) throw new Error(data.error || "Pairing failed");
      token = data.token; store.set("token", token);
      pairEl.hidden = true; pinEl.value = ""; pinEl.blur();
      reconnectNow();
    } catch (e) {
      pairErr.textContent = e instanceof TypeError ? "Can't reach your PC. Is PC Remote running?" : e.message;
      pairEl.hidden = false;
    } finally {
      btn.disabled = false;
    }
  }

  $("#pair-form").addEventListener("submit", (e) => { e.preventDefault(); pair(pinEl.value); });
  pinEl.addEventListener("input", () => { if (pinEl.value.replace(/\D/g, "").length === 6) pair(pinEl.value); });

  // ---------------------------------------------- coalesced pointer I/O
  // Moves/scrolls are summed and flushed once per animation frame, so we never
  // flood the socket and the cursor always reflects the latest finger position.
  let mvx = 0, mvy = 0, scx = 0, scy = 0, frameQueued = false;

  function queueFlush() {
    if (!frameQueued) { frameQueued = true; requestAnimationFrame(flush); }
  }
  function flush() {
    frameQueued = false;
    const batch = [];
    if (mvx || mvy) { batch.push({ t: "m", x: +mvx.toFixed(2), y: +mvy.toFixed(2) }); mvx = mvy = 0; }
    const ix = Math.trunc(scx), iy = Math.trunc(scy);
    if (ix || iy) { batch.push({ t: "s", x: ix, y: iy }); scx -= ix; scy -= iy; }
    if (batch.length) send(batch.length === 1 ? batch[0] : batch);
  }
  function move(dx, dy) { mvx += dx; mvy += dy; queueFlush(); }
  // dx/dy in finger pixels -> wheel units (120 = one notch)
  function scroll(dx, dy) {
    const k = 3 * settings.scroll, dir = settings.natural ? 1 : -1;
    scx += -dx * k * dir;
    scy += dy * k * dir;
    queueFlush();
  }
  function click(b, a = "click") { flush(); send({ t: "b", b, a }); }

  // ----------------------------------------------------------- trackpad
  const TAP_MOVE = 10, TAP_TIME = 260, HOLD_TIME = 420;
  let inertia = null;

  function stopInertia() { if (inertia) { cancelAnimationFrame(inertia); inertia = null; } }
  function startInertia(vx, vy) {
    if (Math.hypot(vx, vy) < 0.25) return;
    let last = performance.now();
    const step = (now) => {
      const dt = Math.min(now - last, 50); last = now;
      const decay = Math.pow(0.994, dt);
      vx *= decay; vy *= decay;
      if (Math.hypot(vx, vy) < 0.03) { inertia = null; return; }
      scroll(vx * dt, vy * dt);
      inertia = requestAnimationFrame(step);
    };
    inertia = requestAnimationFrame(step);
  }

  function ripple(pad, x, y) {
    const r = pad.getBoundingClientRect(), el = document.createElement("div");
    el.className = "ripple";
    el.style.left = (x - r.left) + "px"; el.style.top = (y - r.top) + "px";
    pad.appendChild(el);
    setTimeout(() => el.remove(), 400);
  }

  function attachTrackpad(pad) {
    let g = null;              // current gesture
    const pts = new Map();     // touch id -> {x, y}

    const centroid = (touches) => {
      let x = 0, y = 0;
      for (const t of touches) { x += t.clientX; y += t.clientY; }
      return { x: x / touches.length, y: y / touches.length };
    };

    pad.addEventListener("touchstart", (e) => {
      e.preventDefault();
      pad.classList.add("used");
      stopInertia();
      const now = performance.now();
      if (!g) {
        const t = e.touches[0];
        g = { start: now, max: 0, moved: 0, dragging: false, hold: null, lastT: now,
              vx: 0, vy: 0, sx: t.clientX, sy: t.clientY };
      }
      for (const t of e.changedTouches) pts.set(t.identifier, { x: t.clientX, y: t.clientY });
      g.max = Math.max(g.max, e.touches.length);
      g.c = centroid(e.touches);
      clearTimeout(g.hold);
      if (e.touches.length === 1 && g.max === 1) {
        // press & hold without moving -> start a drag
        g.hold = setTimeout(() => {
          if (g && g.moved < TAP_MOVE && pts.size === 1) {
            g.dragging = true; pad.classList.add("dragging");
            click("left", "down");
          }
        }, HOLD_TIME);
      }
    }, { passive: false });

    pad.addEventListener("touchmove", (e) => {
      e.preventDefault();
      if (!g) return;
      const now = performance.now();
      const n = e.touches.length;
      const c = centroid(e.touches);
      // Only use touches we already know about, so adding/removing a finger
      // never makes the cursor jump.
      let dx = 0, dy = 0, k = 0;
      for (const t of e.touches) {
        const p = pts.get(t.identifier);
        if (p) { dx += t.clientX - p.x; dy += t.clientY - p.y; k++; }
        pts.set(t.identifier, { x: t.clientX, y: t.clientY });
      }
      if (!k) return;
      dx /= k; dy /= k;
      g.moved += Math.abs(dx) + Math.abs(dy);
      if (g.moved >= TAP_MOVE) clearTimeout(g.hold);
      const dt = Math.max(now - g.lastT, 1);
      g.lastT = now; g.c = c;

      if (n === 1 && (g.max === 1 || g.dragging)) {
        const speed = Math.hypot(dx, dy) / dt; // px per ms
        const gain = settings.sens * (1 + 1.4 * Math.min(speed, 2.5));
        move(dx * gain, dy * gain);
      } else if (n >= 2 && g.max === 2 && !g.dragging) {
        scroll(dx, dy);
        // smoothed velocity for momentum
        g.vx = g.vx * 0.6 + (dx / dt) * 0.4;
        g.vy = g.vy * 0.6 + (dy / dt) * 0.4;
      }
    }, { passive: false });

    const end = (e) => {
      e.preventDefault();
      for (const t of e.changedTouches) pts.delete(t.identifier);
      if (!g || e.touches.length) return;
      clearTimeout(g.hold);
      const dur = performance.now() - g.start;
      if (g.dragging) {
        click("left", "up");
        pad.classList.remove("dragging");
      } else if (g.moved < TAP_MOVE && dur < TAP_TIME * (g.max > 1 ? 1.4 : 1)) {
        const t = e.changedTouches[0];
        ripple(pad, t.clientX, t.clientY);
        click(g.max === 1 ? "left" : g.max === 2 ? "right" : "middle");
      } else if (g.max === 2 && performance.now() - g.lastT < 120) {
        startInertia(g.vx, g.vy);
      }
      g = null;
    };
    pad.addEventListener("touchend", end, { passive: false });
    pad.addEventListener("touchcancel", end, { passive: false });
  }
  $$("[data-pad]").forEach(attachTrackpad);

  // Physical-style mouse buttons: press = down, release = up, so you can hold
  // "Left" with your thumb and drag with another finger.
  $$("[data-mouse]").forEach((btn) => {
    const b = btn.dataset.mouse;
    let down = false;
    const release = () => { if (down) { down = false; btn.classList.remove("down"); click(b, "up"); } };
    btn.addEventListener("touchstart", (e) => {
      e.preventDefault();
      if (down) return;
      down = true; btn.classList.add("down"); click(b, "down");
    }, { passive: false });
    btn.addEventListener("touchend", (e) => { e.preventDefault(); release(); }, { passive: false });
    btn.addEventListener("touchcancel", release);
    // desktop testing fallback
    btn.addEventListener("mousedown", () => { if (!down) { down = true; click(b, "down"); } });
    btn.addEventListener("mouseup", release);
    btn.addEventListener("mouseleave", release);
  });

  // ------------------------------------------------------------ keyboard
  const mods = new Set();
  function setMod(m, on) {
    on ? mods.add(m) : mods.delete(m);
    $$(`[data-mod="${m}"]`).forEach((b) => b.classList.toggle("on", on));
  }
  function consumeMods() { const list = [...mods]; list.forEach((m) => setMod(m, false)); return list; }

  function pressKey(k, extraMods = []) {
    const all = [...new Set([...extraMods, ...consumeMods()])];
    send({ t: "k", k, mods: all });
  }

  // Hidden-diff typing: we keep a zero-width sentinel in the field and diff the
  // value after every input event. This handles iOS quirks (autocomplete,
  // dictation, held backspace) that per-key events miss.
  const kb = $("#kb-input");
  const SENT = "​​";
  let prev = SENT;
  function resetField() {
    kb.value = SENT; prev = SENT;
    try { kb.setSelectionRange(SENT.length, SENT.length); } catch { }
  }
  resetField();

  function processInput() {
    const cur = kb.value;
    let i = 0;
    while (i < prev.length && i < cur.length && prev[i] === cur[i]) i++;
    const removed = [...prev.slice(i)].length;
    const added = cur.slice(i).replace(/​/g, "");
    const batch = [];
    for (let j = 0; j < removed; j++) batch.push({ t: "k", k: "backspace" });
    if (added) {
      if (mods.size && [...added].length === 1) {
        batch.push({ t: "k", k: added, mods: consumeMods() });
      } else {
        batch.push({ t: "txt", s: added });
      }
    }
    if (batch.length) send(batch);
    prev = cur;
    if (!cur.startsWith(SENT) || cur.length > 48) resetField();
  }
  kb.addEventListener("input", (e) => { if (!e.isComposing) processInput(); });
  kb.addEventListener("compositionend", processInput);
  kb.addEventListener("focus", () => { resetField(); setTimeout(resetField, 0); });

  const HW_KEYS = { Enter: "enter", Tab: "tab", Escape: "esc", ArrowUp: "up", ArrowDown: "down",
    ArrowLeft: "left", ArrowRight: "right", Delete: "delete", Home: "home", End: "end",
    PageUp: "pageup", PageDown: "pagedown" };
  kb.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    const k = HW_KEYS[e.key];
    if (k) {
      e.preventDefault();
      pressKey(k);
      if (k === "enter") resetField();
    }
  });

  $$("[data-focus-kb]").forEach((b) => b.addEventListener("click", () => { showTab("tab-keys"); kb.focus(); }));

  // Generic action buttons (keys, shortcuts, media). Fire on touch-down for
  // responsiveness, auto-repeat when held, and don't steal focus from the
  // text field so the iOS keyboard stays open.
  function bindAction(btn, fire) {
    const repeat = btn.hasAttribute("data-repeat");
    let t1 = null, t2 = null;
    const stop = () => { clearTimeout(t1); clearInterval(t2); btn.classList.remove("pressed"); };
    const start = (e) => {
      e.preventDefault();
      stop();
      btn.classList.add("pressed");
      fire();
      if (repeat) t1 = setTimeout(() => { t2 = setInterval(fire, 70); }, 380);
    };
    btn.addEventListener("touchstart", start, { passive: false });
    btn.addEventListener("touchend", (e) => { e.preventDefault(); stop(); }, { passive: false });
    btn.addEventListener("touchcancel", stop);
    btn.addEventListener("mousedown", start);
    btn.addEventListener("mouseup", stop);
    btn.addEventListener("mouseleave", stop);
  }

  $$("[data-key]").forEach((btn) => {
    const k = btn.dataset.key;
    const extra = btn.dataset.mods ? btn.dataset.mods.split(",") : [];
    bindAction(btn, () => pressKey(k, extra));
  });
  $$("[data-mod]").forEach((btn) => {
    bindAction(btn, () => setMod(btn.dataset.mod, !mods.has(btn.dataset.mod)));
  });

  // ---------------------------------------------------------------- tabs
  function showTab(id) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === id));
    $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === id));
    store.set("tab", id);
    if (id !== "tab-keys") kb.blur();
  }
  $$(".tab-btn").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  const savedTab = store.get("tab", "tab-pad");
  if (document.getElementById(savedTab)) showTab(savedTab);

  // Keep the layout above the iOS on-screen keyboard.
  const app = $("#app");
  function fitViewport() {
    const vv = window.visualViewport;
    const h = vv ? vv.height : window.innerHeight;
    app.style.setProperty("--app-h", h + "px");
    app.style.transform = vv && vv.offsetTop ? `translateY(${vv.offsetTop}px)` : "";
    document.body.classList.toggle("kb-open", !!vv && vv.height < window.innerHeight * 0.8);
  }
  if (window.visualViewport) {
    visualViewport.addEventListener("resize", fitViewport);
    visualViewport.addEventListener("scroll", fitViewport);
  }
  window.addEventListener("resize", fitViewport);
  fitViewport();

  // ------------------------------------------------------------ settings
  const settingsEl = $("#settings");
  const sens = $("#sens"), scrollEl = $("#scroll"), natural = $("#natural");
  function renderSettings() {
    sens.value = settings.sens; scrollEl.value = settings.scroll; natural.checked = settings.natural;
    $("#sens-val").textContent = (+settings.sens).toFixed(1) + "×";
    $("#scroll-val").textContent = (+settings.scroll).toFixed(1) + "×";
    $("#pc-name").textContent = authed ? `Connected to ${pcName} (${location.host})` : `Server: ${location.host}`;
  }
  function saveSettings() {
    settings.sens = +sens.value; settings.scroll = +scrollEl.value; settings.natural = natural.checked;
    store.set("settings", settings); renderSettings();
  }
  [sens, scrollEl, natural].forEach((el) => el.addEventListener("input", saveSettings));
  $("#open-settings").addEventListener("click", () => { renderSettings(); settingsEl.hidden = false; });
  $("#close-settings").addEventListener("click", () => { settingsEl.hidden = true; });
  settingsEl.addEventListener("click", (e) => { if (e.target === settingsEl) settingsEl.hidden = true; });
  $("#unpair").addEventListener("click", () => {
    if (!confirm("Unpair this device? You'll need a new PIN from your PC to reconnect.")) return;
    token = null; store.del("token");
    settingsEl.hidden = true;
    if (ws) { const old = ws; ws = null; old.close(); }
    showPairing();
  });

  // Block page-level pinch zoom / double-tap zoom gestures.
  document.addEventListener("gesturestart", (e) => e.preventDefault());
  document.addEventListener("dblclick", (e) => e.preventDefault());

  // --------------------------------------------------------------- start
  const urlPin = new URLSearchParams(location.search).get("pin");
  if (urlPin) {
    history.replaceState(null, "", location.pathname);  // don't keep the PIN in history
    if (!token) { pairEl.hidden = false; pair(urlPin); }
    else connect();
  } else {
    connect();
  }
})();
