(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    log: $("chat-log"),
    empty: $("chat-empty"),
    form: $("chat-form"),
    input: $("ask-input"),
    send: $("btn-send"),
    welcome: $("btn-welcome"),
    reset: $("btn-reset"),
    raw: $("raw-body"),
    chipDz: $("chip-dz"),
    chipSession: $("chip-session"),
    chipTurns: $("chip-turns"),
    clock: $("clock"),
  };

  /** 本页会话：call_id 多轮复用；reset 清空 */
  const state = {
    callId: null,
    busy: false,
  };

  const pad = (n) => String(n).padStart(2, "0");
  const tickClock = () => {
    const d = new Date();
    els.clock.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  setInterval(tickClock, 1000);
  tickClock();

  const shortId = (id) => (id ? `${String(id).slice(0, 8)}…` : "--");

  const setBusy = (busy) => {
    state.busy = busy;
    els.send.disabled = busy;
    els.welcome.disabled = busy;
    els.input.disabled = busy;
  };

  const hideEmpty = () => {
    if (els.empty) els.empty.style.display = "none";
  };

  const appendBubble = (role, text, meta = "") => {
    hideEmpty();
    const row = document.createElement("div");
    row.className = `bubble ${role}`;
    const label = role === "user" ? "你" : role === "sys" ? "系统" : "得助";
    row.innerHTML = `
      <div class="bubble-meta">${label}${meta ? ` · ${meta}` : ""}</div>
      <div class="bubble-text"></div>
    `;
    row.querySelector(".bubble-text").textContent = text;
    els.log.appendChild(row);
    els.log.scrollTop = els.log.scrollHeight;
  };

  const updateChips = (data = {}) => {
    if (data.call_id) state.callId = data.call_id;
    els.chipSession.textContent = `SESSION ${shortId(data.dezhu_session || state.callId)}`;
    if (typeof data.turn_count === "number") {
      els.chipTurns.textContent = `TURN ${data.turn_count}`;
    }
  };

  const showRaw = (payload) => {
    try {
      els.raw.textContent = JSON.stringify(payload, null, 2);
    } catch {
      els.raw.textContent = String(payload);
    }
  };

  const loadHealth = async () => {
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      els.chipDz.textContent = data.has_dezhu
        ? `DZ ${data.dz_channel || "OK"}`
        : "DZ OFF";
      els.chipDz.classList.toggle("ok", !!data.has_dezhu);
    } catch {
      els.chipDz.textContent = "DZ ERR";
    }
  };

  /**
   * POST /api/chat
   * body: { ask, call_id?, welcome? }
   * → { text, call_id, dezhu_session, turn_count, raw }
   */
  const requestChat = async ({ ask = "", welcome = false } = {}) => {
    if (state.busy) return;
    setBusy(true);

    const payload = {
      ask: ask || (welcome ? "welcome" : ""),
      welcome,
    };
    if (state.callId) payload.call_id = state.callId;

    if (!welcome && ask) appendBubble("user", ask);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = data.detail || res.statusText || "请求失败";
        appendBubble("sys", typeof detail === "string" ? detail : JSON.stringify(detail));
        showRaw(data);
        return;
      }
      updateChips(data);
      appendBubble("bot", data.text || "(空回复)", `t${data.turn_count || "-"}`);
      showRaw(data.raw ?? data);
    } catch (err) {
      appendBubble("sys", err.message || String(err));
    } finally {
      setBusy(false);
      els.input.focus();
    }
  };

  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const ask = (els.input.value || "").trim();
    if (!ask) return;
    els.input.value = "";
    requestChat({ ask });
  });

  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      els.form.requestSubmit();
    }
  });

  els.welcome.addEventListener("click", () => {
    requestChat({ welcome: true, ask: "welcome" });
  });

  els.reset.addEventListener("click", () => {
    state.callId = null;
    els.log.innerHTML =
      '<div class="chat-empty" id="chat-empty">已重置会话。输入问题或点「开场欢迎」。</div>';
    els.empty = $("chat-empty");
    els.chipSession.textContent = "SESSION --";
    els.chipTurns.textContent = "TURN 0";
    els.raw.textContent = "等待请求…";
    els.input.focus();
  });

  loadHealth();
  els.input.focus();
})();
