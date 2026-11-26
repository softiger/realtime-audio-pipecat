(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    sections: $("config-sections"),
    note: $("config-note"),
    status: $("config-status"),
    chipOv: $("chip-overrides"),
    chipFlags: $("chip-flags"),
    btnSave: $("btn-save"),
    btnReset: $("btn-reset"),
    btnReload: $("btn-reload"),
    clock: $("clock"),
  };

  /** @type {{ fields: any[], categories: any[] } | null} */
  let snapshot = null;
  /** key → input/select/textarea/checkbox */
  const inputs = new Map();
  const turnDetectionLabels = {
    smart_turn_v2: "智能轮次检测 V2",
    smart_turn: "智能轮次检测",
    server_vad: "服务端语音活动检测",
    manual: "手动控制",
  };
  const turnDetectionFields = {
    server_vad_threshold: ["server_vad"],
    server_vad_silence_duration_ms: ["server_vad"],
    voiceprint_audio_urls: ["smart_turn_v2", "smart_turn"],
  };

  const pad = (n) => String(n).padStart(2, "0");
  const tickClock = () => {
    const d = new Date();
    els.clock.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  setInterval(tickClock, 1000);
  tickClock();

  const showStatus = (msg, kind = "ok") => {
    els.status.hidden = !msg;
    els.status.textContent = msg || "";
    els.status.className = `config-status ${kind}`;
  };

  const setBusy = (busy) => {
    els.btnSave.disabled = busy;
    els.btnReset.disabled = busy;
    els.btnReload.disabled = busy;
  };

  const fieldControl = (f) => {
    const wrap = document.createElement("div");
    wrap.className = "cfg-field";
    if (f.overridden) wrap.classList.add("is-overridden");
    if (f.readonly) wrap.classList.add("is-readonly");

    const lab = document.createElement("label");
    lab.className = "cfg-label";
    lab.htmlFor = `cfg-${f.key}`;
    lab.innerHTML = `<span>${f.label}</span>` +
      (f.overridden ? `<span class="cfg-badge">热更中</span>` : "");

    let control;
    const id = `cfg-${f.key}`;

    if (f.type === "bool") {
      control = document.createElement("input");
      control.type = "checkbox";
      control.id = id;
      control.checked = !!f.value;
      control.disabled = !!f.readonly;
    } else if (f.type === "enum" && Array.isArray(f.options)) {
      control = document.createElement("select");
      control.id = id;
      f.options.forEach((opt) => {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = f.key === "turn_detection" ? (turnDetectionLabels[opt] || opt) : opt;
        if (String(f.value) === opt) o.selected = true;
        control.appendChild(o);
      });
      control.disabled = !!f.readonly;
    } else if (f.type === "textarea" || (f.type === "secret" && f.key === "dz_private_key")) {
      control = document.createElement("textarea");
      control.id = id;
      control.rows = f.type === "secret" ? 3 : 2;
      control.value = f.type === "secret" ? "" : (f.value ?? "");
      control.disabled = !!f.readonly;
      if (f.type === "secret") {
        control.placeholder = f.configured ? "已配置 · 留空不修改" : "未配置 · 可在此填入（仅内存）";
      }
    } else if (f.type === "secret") {
      control = document.createElement("input");
      control.type = "password";
      control.id = id;
      control.value = "";
      control.autocomplete = "off";
      control.placeholder = f.configured ? "已配置 · 留空不修改" : "未配置";
      control.disabled = !!f.readonly;
    } else {
      control = document.createElement("input");
      control.type = f.type === "int" || f.type === "float" ? "number" : "text";
      control.id = id;
      if (f.type === "float") control.step = "any";
      if (f.type === "int") control.step = "1";
      control.value = f.value == null ? "" : String(f.value);
      control.disabled = !!f.readonly;
    }

    control.className = "cfg-control";
    control.dataset.key = f.key;
    control.dataset.type = f.type;
    inputs.set(f.key, control);

    const help = document.createElement("div");
    help.className = "cfg-help";
    help.textContent = f.help || "";
    if (f.type === "secret" && f.configured) {
      help.textContent = (help.textContent ? help.textContent + " · " : "") + "当前已配置";
    }

    wrap.appendChild(lab);
    wrap.appendChild(control);
    wrap.appendChild(help);
    return wrap;
  };

  const render = (data) => {
    snapshot = data;
    inputs.clear();
    els.sections.innerHTML = "";
    els.note.textContent = data.note || "仅内存热更";

    const n = (data.override_keys || []).length;
    els.chipOv.textContent = `OVERRIDE ${n}`;
    if (n > 0) els.chipOv.classList.add("ok");
    else els.chipOv.classList.remove("ok");

    const flags = data.flags || {};
    els.chipFlags.textContent =
      `KEY ${flags.has_api_key ? "OK" : "NO"} · DZ ${flags.has_dezhu ? "OK" : "NO"}`;
    if (!flags.has_api_key || !flags.has_dezhu) els.chipFlags.classList.add("warn");
    else {
      els.chipFlags.classList.remove("warn");
      els.chipFlags.classList.add("ok");
    }

    const byCat = new Map();
    (data.fields || []).forEach((f) => {
      const c = f.category || "other";
      if (!byCat.has(c)) byCat.set(c, []);
      byCat.get(c).push(f);
    });

    (data.categories || []).forEach((cat) => {
      const list = byCat.get(cat.id) || [];
      if (!list.length) return;
      const sec = document.createElement("section");
      sec.className = "config-section";
      const h = document.createElement("h2");
      h.className = "config-section-title";
      h.textContent = cat.label;
      sec.appendChild(h);
      const grid = document.createElement("div");
      grid.className = "config-grid";
      list.forEach((f) => grid.appendChild(fieldControl(f)));
      sec.appendChild(grid);
      els.sections.appendChild(sec);
    });

    const mode = inputs.get("turn_detection");
    if (mode) {
      const updateTurnFields = () => {
        Object.entries(turnDetectionFields).forEach(([key, modes]) => {
          const control = inputs.get(key);
          if (!control) return;
          const field = data.fields.find((f) => f.key === key);
          control.disabled = !!field?.readonly || !modes.includes(mode.value);
          control.closest(".cfg-field").classList.toggle("is-readonly", control.disabled);
        });
      };
      mode.addEventListener("change", updateTurnFields);
      updateTurnFields();
    }
  };

  const collectValues = () => {
    const values = {};
    if (!snapshot) return values;
    (snapshot.fields || []).forEach((f) => {
      if (f.readonly) return;
      const el = inputs.get(f.key);
      if (!el) return;
      const t = f.type;
      let next;
      if (t === "bool") {
        next = !!el.checked;
        if (next === !!f.value) return;
        values[f.key] = next;
        return;
      }
      if (t === "secret") {
        const v = (el.value || "").trim();
        if (v) values[f.key] = el.value;
        return;
      }
      if (t === "int") {
        const n = parseInt(el.value, 10);
        if (Number.isNaN(n)) return;
        if (Number(f.value) === n) return;
        values[f.key] = n;
        return;
      }
      if (t === "float") {
        const n = parseFloat(el.value);
        if (Number.isNaN(n)) return;
        if (Number(f.value) === n) return;
        values[f.key] = n;
        return;
      }
      next = el.value;
      if (String(f.value ?? "") === String(next)) return;
      values[f.key] = next;
    });
    return values;
  };

  const load = async () => {
    setBusy(true);
    showStatus("");
    try {
      const res = await fetch("/api/config");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      render(data);
    } catch (err) {
      showStatus(`加载失败: ${err.message || err}`, "err");
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    showStatus("保存中…", "ok");
    try {
      const res = await fetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: collectValues(), replace: false }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      render(data);
      showStatus("已热更 · 下一通语音 / 下一次文本请求生效（不写 .env）", "ok");
    } catch (err) {
      showStatus(`保存失败: ${err.message || err}`, "err");
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (!confirm("清空全部运行时覆盖，恢复为当前 .env 底座？")) return;
    setBusy(true);
    try {
      const res = await fetch("/api/config/reset", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      render(data);
      showStatus("已恢复 .env 底座", "ok");
    } catch (err) {
      showStatus(`重置失败: ${err.message || err}`, "err");
    } finally {
      setBusy(false);
    }
  };

  els.btnSave.addEventListener("click", save);
  els.btnReset.addEventListener("click", reset);
  els.btnReload.addEventListener("click", load);
  load();
})();
