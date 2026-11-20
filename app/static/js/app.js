(() => {
  const $ = (id) => document.getElementById(id);

  const ui = {
    chipModel: $("chip-model"),
    chipLink: $("chip-link"),
    chipSession: $("chip-session"),
    chipLatency: $("chip-latency"),
    micBar: $("mic-bar"),
    ttsBar: $("tts-bar"),
    telemetry: $("telemetry"),
    toolLog: $("tool-log-body"),
    status: $("status-line"),
    btnConnect: $("btn-connect"),
    btnMute: $("btn-mute"),
    transcript: $("transcript"),
    orb: $("orb"),
    clock: $("clock"),
    wave: $("wave-canvas"),
  };

  let ws = null;
  let audio = null;
  let connected = false;
  let muted = false;
  let userBuf = "";
  let aiBuf = "";
  let userEl = null;
  let aiEl = null;
  let pingAt = 0;
  let micLevel = 0;
  let ttsLevel = 0;
  let callId = "";
  let dezhuSession = "";
  let voiceDuplex = "soft";
  // 最近一轮分段时延（ms）
  let lastLatency = null;
  let lastRtt = null;

  initParticles();
  initWave();
  tickClock();
  setInterval(tickClock, 1000);

  ui.btnConnect.addEventListener("click", async () => {
    if (connected) {
      await teardown();
      return;
    }
    await engage();
  });

  ui.btnMute.addEventListener("click", () => {
    muted = !muted;
    audio?.setMuted(muted);
    ui.btnMute.textContent = muted ? "麦克风 关" : "麦克风 开";
  });

  async function engage() {
    setStatus("请求麦克风权限…");
    try {
      audio = createAudioPipeline({
        getDuplex: () => voiceDuplex,
        onPcmChunk: (buf) => {
          if (!ws || ws.readyState !== WebSocket.OPEN) return;
          // 二进制 PCM16 上行：只发有效字节，避免 ArrayBuffer 尾部脏数据
          try {
            const u8 =
              buf instanceof ArrayBuffer
                ? new Uint8Array(buf)
                : new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
            ws.send(u8);
          } catch {
            const b64 = arrayBufferToBase64(
              buf instanceof ArrayBuffer ? buf : buf.buffer
            );
            ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: b64 }));
          }
        },
        onMicLevel: (lv) => {
          micLevel = lv;
          ui.micBar.style.width = `${Math.round(lv * 100)}%`;
          if (lv > 0.08) setOrb("listening");
        },
        onPlayLevel: (lv) => {
          ttsLevel = lv;
          ui.ttsBar.style.width = `${Math.round(lv * 100)}%`;
          if (lv > 0.08) setOrb("speaking");
        },
      });
      await audio.start();
    } catch (err) {
      setStatus(`麦克风失败: ${err.message || err}`);
      return;
    }

    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/voice`);
    setStatus("建立实时语音链路…");
    ui.chipLink.textContent = "LINK HANDSHAKE";
    ui.chipLink.className = "chip";
    ui.transcript.innerHTML = "";
    ui.toolLog.textContent = "等待业务调用…";

    ws.onopen = () => {
      connected = true;
      ui.btnConnect.textContent = "结束通话";
      ui.btnMute.disabled = false;
      setStatus("链路已接通 · 通话 Session 建立中");
      addSystem("全双工通道开启 · 本通话为独立 Session");
      setIntervalPing();
    };

    ws.onmessage = (ev) => {
      let event;
      try {
        event = JSON.parse(ev.data);
      } catch {
        return;
      }
      handleServerEvent(event);
    };

    ws.onclose = async () => {
      await teardown(false);
      setStatus("链路已断开 · Session 结束");
      ui.chipLink.textContent = "LINK OFFLINE";
      ui.chipLink.className = "chip warn";
    };

    ws.onerror = () => {
      setStatus("链路异常");
      ui.chipLink.className = "chip warn";
    };
  }

  async function teardown(sendClose = true) {
    connected = false;
    ui.btnConnect.textContent = "开启通话";
    ui.btnMute.disabled = true;
    muted = false;
    ui.btnMute.textContent = "麦克风 开";
    setOrb("");
    if (ws) {
      try {
        if (sendClose && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "client.close" }));
        }
        ws.close();
      } catch {}
      ws = null;
    }
    if (audio) {
      await audio.stop();
      audio = null;
    }
  }

  function handleServerEvent(event) {
    const t = event.type;

    if (t === "bridge.status") {
      if (event.call_id) {
        callId = event.call_id;
        ui.chipSession.textContent = `CALL ${callId.slice(0, 8)}`;
      }
      if (event.dezhu_session) {
        dezhuSession = event.dezhu_session;
        ui.chipSession.textContent = `DZ ${String(dezhuSession).slice(-10)}`;
      }
      if (event.status === "connected") {
        ui.chipLink.textContent = "LINK LIVE";
        ui.chipLink.className = "chip live";
        ui.chipModel.textContent = `MODEL ${shortModel(event.model)}`;
        if (event.voice_duplex) voiceDuplex = String(event.voice_duplex);
        updateTelemetry(event);
        setStatus(
          `已连接 · 音色 ${event.voice || "--"} · ${event.turn_detection || ""} · duplex ${voiceDuplex}`
        );
      } else if (event.status === "connecting") {
        setStatus("上游握手中…");
      } else if (event.status === "disconnected") {
        setStatus("上游断开");
      }
      return;
    }

    if (t === "client.pong") {
      const lat = Date.now() - (event.t || pingAt);
      lastRtt = lat;
      refreshLatencyChip();
      if (event.dezhu_session) {
        dezhuSession = event.dezhu_session;
        ui.chipSession.textContent = `DZ ${String(dezhuSession).slice(-10)} · T${event.turn_count || 0}`;
      }
      return;
    }

    if (t === "error") {
      addSystem(`错误: ${event.message || event.error?.message || JSON.stringify(event)}`);
      setStatus("发生错误，请检查配置");
      ui.chipLink.className = "chip warn";
      return;
    }

    if (t === "dezhu.welcome") {
      if (event.session_id) {
        dezhuSession = event.session_id;
        ui.chipSession.textContent = `DZ ${String(dezhuSession).slice(-10)}`;
      }
      addSystem(`欢迎语已就绪 · session ${event.session_id || "--"}`);
      appendTool(`WELCOME → ${event.text || ""}`);
      return;
    }

    if (t === "input_audio_buffer.speech_started") {
      audio?.interruptPlayback();
      setOrb("listening");
      setStatus("检测到语音 · 正在聆听");
      userBuf = "";
      userEl = ensureBubble("user", "YOU");
      return;
    }

    if (t === "input_audio_buffer.speech_stopped") {
      setStatus("语音结束 · 理解中");
      return;
    }

    if (t === "bridge.phase") {
      const phase = event.phase || "";
      if (phase === "tool_wait") {
        setOrb("listening");
        setStatus(event.filler ? `查询中 · ${event.filler}` : "得助大脑处理中…");
        audio?.startToolWaitTone?.();
      } else if (phase === "filler") {
        setStatus("稍候提示播报中…");
      } else if (phase === "speaking") {
        audio?.stopToolWaitTone?.();
        setOrb("speaking");
        setStatus("智能体正在回应");
      } else if (phase === "thinking") {
        audio?.stopToolWaitTone?.();
        setStatus("理解中…");
      } else if (phase === "idle") {
        audio?.stopToolWaitTone?.();
        setStatus("待命 · 继续说话即可（同 Session 多轮）");
      }
      return;
    }

    if (t === "conversation.item.input_audio_transcription.delta") {
      const preview = (event.text || "") + (event.stash || "") + (event.delta || "");
      if (preview) {
        userBuf = preview;
        if (!userEl) userEl = ensureBubble("user", "YOU");
        userEl.querySelector(".text").textContent = userBuf;
        scrollTranscript();
      }
      return;
    }

    if (t === "conversation.item.input_audio_transcription.completed") {
      userBuf = event.transcript || userBuf;
      if (!userEl) userEl = ensureBubble("user", "YOU");
      userEl.querySelector(".text").textContent = userBuf;
      userEl = null;
      return;
    }

    if (t === "response.created") {
      audio?.stopToolWaitTone?.();
      audio?.interruptPlayback();
      aiBuf = "";
      aiEl = ensureBubble("ai", "DEZHU-AI");
      setOrb("speaking");
      setStatus("智能体正在回应");
      return;
    }

    if (t === "response.audio_transcript.delta" || t === "response.text.delta") {
      aiBuf += event.delta || "";
      if (!aiEl) aiEl = ensureBubble("ai", "DEZHU-AI");
      aiEl.querySelector(".text").textContent = aiBuf;
      scrollTranscript();
      return;
    }

    if (t === "response.audio_transcript.done") {
      aiBuf = event.transcript || aiBuf;
      if (!aiEl) aiEl = ensureBubble("ai", "DEZHU-AI");
      aiEl.querySelector(".text").textContent = aiBuf;
      aiEl = null;
      return;
    }

    if (t === "response.audio.delta") {
      if (event.delta) audio?.enqueueBase64Pcm(event.delta);
      return;
    }

    if (t === "response.done") {
      audio?.stopToolWaitTone?.();
      setOrb(connected ? "" : "");
      setStatus("待命 · 继续说话即可（同 Session 多轮）");
      return;
    }

    if (t === "tool.started") {
      const q =
        typeof event.arguments === "object" && event.arguments
          ? event.arguments.question || formatArgs(event.arguments)
          : formatArgs(event.arguments);
      const mark = event.calibrated ? " [ASR校准]" : "";
      appendTool(`→ DEZHU ${q}${mark}`);
      if (event.transcript) {
        appendTool(`   ASR: ${truncate(event.transcript, 80)}`);
      }
      addSystem("调用得助文本应答…");
      setStatus("得助大脑处理中…");
      audio?.startToolWaitTone?.();
      return;
    }

    if (t === "tool.finished") {
      audio?.stopToolWaitTone?.();
      if (event.dezhu_session) {
        dezhuSession = event.dezhu_session;
        ui.chipSession.textContent = `DZ ${String(dezhuSession).slice(-10)} · T${event.turn_count || 0}`;
      }
      const dms =
        event.duration_ms != null && event.duration_ms !== undefined
          ? ` [${event.duration_ms}ms]`
          : "";
      const flags = [
        event.timed_out ? "timeout" : "",
        event.cached ? "cache" : "",
        event.stale ? "stale" : "",
      ]
        .filter(Boolean)
        .join(",");
      const flagMark = flags ? ` (${flags})` : "";
      appendTool(`← ${truncate(event.result, 160)}${dms}${flagMark}`);
      return;
    }

    if (t === "bridge.latency") {
      lastLatency = event.ms || null;
      const stage = event.stage || "";
      const ms = lastLatency || {};
      const parts = [
        ms.e2e != null ? `E2E ${ms.e2e}` : null,
        ms.asr != null ? `ASR ${ms.asr}` : null,
        ms.to_fc != null ? `FC ${ms.to_fc}` : null,
        ms.tool != null ? `TOOL ${ms.tool}` : null,
        ms.ttfb != null ? `TTFB ${ms.ttfb}` : null,
        ms.total != null ? `TOT ${ms.total}` : null,
      ].filter(Boolean);
      if (parts.length) {
        appendTool(`⏱ T${event.turn_seq ?? "-"} ${stage}: ${parts.join(" · ")} ms`);
      }
      refreshLatencyChip();
      paintLatencyTelemetry();
      return;
    }
  }

  function fmtMs(v) {
    return v == null || v === undefined ? "--" : `${v}`;
  }

  function refreshLatencyChip() {
    const e2e = lastLatency && lastLatency.e2e;
    if (e2e != null) {
      const rtt = lastRtt != null ? ` · RTT ${lastRtt}` : "";
      ui.chipLatency.textContent = `E2E ${e2e}ms${rtt}`;
    } else if (lastRtt != null) {
      ui.chipLatency.textContent = `RTT ${lastRtt} ms`;
    } else {
      ui.chipLatency.textContent = "LAT -- ms";
    }
  }

  function paintLatencyTelemetry() {
    if (!lastLatency) return;
    const ms = lastLatency;
    // 在已有 telemetry 列表上追加/刷新时延行
    const root = ui.telemetry;
    if (!root) return;
    const lines = Array.from(root.querySelectorAll("li"));
    const keep = lines.filter((li) => !String(li.textContent || "").startsWith("LAT."));
    root.innerHTML = "";
    keep.forEach((li) => root.appendChild(li));
    const add = (k, v) => {
      const li = document.createElement("li");
      li.textContent = `LAT.${k}: ${fmtMs(v)}ms`;
      root.appendChild(li);
    };
    add("E2E", ms.e2e);
    add("ASR", ms.asr);
    add("TOOL", ms.tool);
    add("TTFB", ms.ttfb);
    if (ms.total != null) add("TOT", ms.total);
  }

  function shortModel(m) {
    if (!m) return "--";
    const s = String(m);
    return s.length > 22 ? s.slice(0, 20) + "…" : s;
  }

  function truncate(s, n) {
    const t = String(s || "");
    return t.length > n ? t.slice(0, n) + "…" : t;
  }

  function ensureBubble(kind, who) {
    const el = document.createElement("div");
    el.className = `msg ${kind}`;
    el.innerHTML = `<div class="who">${who}</div><div class="text"></div>`;
    ui.transcript.appendChild(el);
    scrollTranscript();
    return el;
  }

  function addSystem(text) {
    const el = ensureBubble("system", "SYSTEM");
    el.querySelector(".text").textContent = text;
  }

  function appendTool(line) {
    const prev = ui.toolLog.textContent === "等待业务调用…" ? "" : ui.toolLog.textContent + "\n";
    ui.toolLog.textContent = prev + line;
    ui.toolLog.scrollTop = ui.toolLog.scrollHeight;
  }

  function formatArgs(args) {
    if (!args) return "";
    if (typeof args === "string") return args;
    try {
      return JSON.stringify(args);
    } catch {
      return String(args);
    }
  }

  function updateTelemetry(event) {
    const duplex = event.voice_duplex || voiceDuplex || "soft";
    ui.telemetry.innerHTML = `
      <li>CORE: ONLINE</li>
      <li>PROTOCOL: WSS DUPLEX</li>
      <li>BRAIN: 得助 reply API</li>
      <li>VAD: ${event.turn_detection || "smart_turn_v2"}</li>
      <li>MIC: ${duplex}</li>
      <li>VOICE: ${event.voice || "--"}</li>
      <li>CALL: ${(event.call_id || callId || "--").toString().slice(0, 12)}</li>
    `;
    paintLatencyTelemetry();
  }

  function setStatus(text) {
    ui.status.textContent = text;
  }

  function setOrb(mode) {
    ui.orb.classList.remove("listening", "speaking");
    if (mode) ui.orb.classList.add(mode);
  }

  function scrollTranscript() {
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
  }

  function setIntervalPing() {
    const timer = setInterval(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        clearInterval(timer);
        return;
      }
      pingAt = Date.now();
      ws.send(JSON.stringify({ type: "client.ping", t: pingAt }));
    }, 5000);
  }

  function tickClock() {
    const d = new Date();
    ui.clock.textContent = d.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function arrayBufferToBase64(buf) {
    const bytes = new Uint8Array(buf);
    let binary = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }

  function initParticles() {
    const canvas = $("particle-canvas");
    const ctx = canvas.getContext("2d");
    let w, h, dots;
    const resize = () => {
      w = canvas.width = window.innerWidth;
      h = canvas.height = window.innerHeight;
      dots = Array.from({ length: 48 }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.35,
        vy: (Math.random() - 0.5) * 0.35,
        r: Math.random() * 1.8 + 0.4,
      }));
    };
    resize();
    window.addEventListener("resize", resize);
    const draw = () => {
      ctx.clearRect(0, 0, w, h);
      for (const d of dots) {
        d.x += d.vx;
        d.y += d.vy;
        if (d.x < 0 || d.x > w) d.vx *= -1;
        if (d.y < 0 || d.y > h) d.vy *= -1;
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(42,246,255,0.55)";
        ctx.fill();
      }
      for (let i = 0; i < dots.length; i++) {
        for (let j = i + 1; j < dots.length; j++) {
          const a = dots[i],
            b = dots[j];
          const dx = a.x - b.x,
            dy = a.y - b.y;
          const dist = Math.hypot(dx, dy);
          if (dist < 120) {
            ctx.strokeStyle = `rgba(123,92,255,${(1 - dist / 120) * 0.25})`;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();
          }
        }
      }
      requestAnimationFrame(draw);
    };
    draw();
  }

  function initWave() {
    const canvas = ui.wave;
    const ctx = canvas.getContext("2d");
    let t = 0;
    const draw = () => {
      const { width: w, height: h } = canvas;
      ctx.clearRect(0, 0, w, h);
      const cx = w / 2,
        cy = h / 2;
      const amp = 18 + micLevel * 50 + ttsLevel * 40;
      for (let ring = 0; ring < 3; ring++) {
        ctx.beginPath();
        for (let i = 0; i <= 360; i++) {
          const rad = (i * Math.PI) / 180;
          const wobble =
            Math.sin(rad * (3 + ring) + t * (1.2 + ring * 0.3)) * amp * (0.4 + ring * 0.2);
          const r = 110 + ring * 28 + wobble;
          const x = cx + Math.cos(rad) * r;
          const y = cy + Math.sin(rad) * r;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.strokeStyle = ring === 1 ? "rgba(255,43,214,0.35)" : "rgba(42,246,255,0.28)";
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      t += 0.04;
      requestAnimationFrame(draw);
    };
    draw();
  }

  fetch("/api/health")
    .then((r) => r.json())
    .then((h) => {
      ui.chipModel.textContent = `MODEL ${shortModel(h.model)}`;
      if (!h.has_api_key) {
        addSystem("未检测到 DASHSCOPE_API_KEY，请配置 .env");
      }
      if (!h.has_dezhu) {
        addSystem("未检测到得助凭证 DZ_APP_ID / DZ_CHANNEL_ID / DZ_PRIVATE_KEY");
      }
    })
    .catch(() => {});
})();
