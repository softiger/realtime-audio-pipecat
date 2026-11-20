/**
 * 浏览器音频通路（低时延 + 稳播）：
 *   Mic → 降采样 16k PCM16 → WebSocket binary（优先）/ JSON base64（兼容）
 *   WebSocket JSON response.audio.delta(24k base64) → 抖动缓冲 → 无缝调度播放
 *
 * 颤音/断续常见原因与对策：
 *   1) 过小 delta 块 + 网络抖动 → underrun 空隙 → 合并到 ~60ms 再 schedule
 *   2) nextPlayTime 落后 currentTime → 硬跳 now+lookahead 产生周期性缺口 → 预缓冲
 *   3) AudioContext@48k 播 24k buffer 隐式重采样毛刺 → 线性升采样到 ctx.sampleRate
 *   4) 块边界硬切 → 极短 crossfade 抹平
 *
 * duplex soft：播报中低能量不上送，高能量持续约 180ms 才允许 barge-in
 */

function createAudioPipeline({ onPcmChunk, onMicLevel, onPlayLevel, getDuplex }) {
  let audioCtx = null;
  let mediaStream = null;
  let processor = null;
  let source = null;
  let muted = false;

  /** 原始 f32 块队列（未合并） */
  let playQueue = [];
  /** 已 schedule 的 BufferSource */
  let activeSources = [];
  let nextPlayTime = 0;
  let playing = false;
  let pumpTimer = null;

  /** soft 模式：播报中累计高能量时长（秒） */
  let bargeHoldSec = 0;
  let toolWaitToneOn = false;
  let toolWaitTimer = null;

  const INPUT_RATE = 16000;
  const STREAM_RATE = 24000; // Qwen / 服务端 PCM 采样率
  const BARGE_ENERGY = 0.12;
  const BARGE_HOLD_SEC = 0.18;

  // 播放稳定参数
  const MERGE_MS = 60; // 合并到约 60ms 再播，减少 schedule 次数与边界毛刺
  const PREBUFFER_MS = 100; // 首包前抖动缓冲
  const LOOKAHEAD_SEC = 0.03; // 相对 currentTime 的安全提前量
  const CROSSFADE_MS = 2; // 块间接力淡入淡出
  const PUMP_INTERVAL_MS = 20;

  /** 上一块末尾样本，用于 crossfade */
  let tailSamples = null;

  function duplexMode() {
    try {
      return (getDuplex?.() || "soft").toLowerCase();
    } catch {
      return "soft";
    }
  }

  function botIsPlaying() {
    return activeSources.length > 0 || playQueue.length > 0;
  }

  function ctxRate() {
    return audioCtx ? audioCtx.sampleRate : 48000;
  }

  async function start() {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      const hint =
        location.protocol === "https:"
          ? "当前浏览器不支持麦克风 API"
          : "请使用 HTTPS 访问（或 localhost）。远程部署请走 Nginx 443，勿用 http://IP:端口";
      throw new Error(hint);
    }
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: 48000,
    });
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    source = audioCtx.createMediaStreamSource(mediaStream);
    processor = audioCtx.createScriptProcessor(2048, 1, 1);
    const silent = audioCtx.createGain();
    silent.gain.value = 0;

    processor.onaudioprocess = (ev) => {
      if (muted) {
        onMicLevel?.(0);
        return;
      }
      const input = ev.inputBuffer.getChannelData(0);
      const pcm16 = downsampleToPcm16(input, audioCtx.sampleRate, INPUT_RATE);
      let energy = 0;
      for (let i = 0; i < pcm16.length; i++) energy += Math.abs(pcm16[i]);
      energy = energy / Math.max(1, pcm16.length) / 32768;
      onMicLevel?.(Math.min(1, energy * 8));

      const mode = duplexMode();
      const frameSec = pcm16.length / INPUT_RATE;

      if (mode === "half" && botIsPlaying()) {
        bargeHoldSec = 0;
        return;
      }

      if (mode === "soft" && botIsPlaying()) {
        if (energy >= BARGE_ENERGY) {
          bargeHoldSec += frameSec;
        } else {
          bargeHoldSec = Math.max(0, bargeHoldSec - frameSec * 1.5);
        }
        if (bargeHoldSec < BARGE_HOLD_SEC) {
          return;
        }
      } else {
        bargeHoldSec = 0;
      }

      onPcmChunk?.(pcm16);
    };

    source.connect(processor);
    processor.connect(silent);
    silent.connect(audioCtx.destination);
    if (audioCtx.state === "suspended") await audioCtx.resume();
  }

  function setMuted(v) {
    muted = !!v;
  }

  function enqueueBase64Pcm(b64) {
    if (!audioCtx) return;
    stopToolWaitTone();
    const raw = base64ToUint8(b64);
    // 奇数长度截齐，避免 Int16 错位产生爆音/颤感
    const even = raw.byteLength & ~1;
    if (even < 2) return;
    const samples = new Int16Array(raw.buffer, raw.byteOffset, even / 2);
    const f32 = new Float32Array(samples.length);
    let energy = 0;
    for (let i = 0; i < samples.length; i++) {
      // 对称量化：避免 +1.0 削波不对称
      const s = samples[i] / 32768;
      f32[i] = s < -1 ? -1 : s > 1 ? 1 : s;
      energy += Math.abs(f32[i]);
    }
    onPlayLevel?.(Math.min(1, (energy / Math.max(1, f32.length)) * 4));
    playQueue.push(f32);
    ensurePump();
  }

  function queuedDurationSec() {
    let n = 0;
    for (const c of playQueue) n += c.length;
    return n / STREAM_RATE;
  }

  function takeMergedChunk() {
    const targetSamples = Math.max(1, Math.round((MERGE_MS / 1000) * STREAM_RATE));
    if (!playQueue.length) return null;

    // 不够一整块时：若还在播（时间轴未空）可等；若已 underrun 风险则先吐出
    const total = playQueue.reduce((a, c) => a + c.length, 0);
    const behind =
      audioCtx && nextPlayTime > 0 && nextPlayTime < audioCtx.currentTime + LOOKAHEAD_SEC;
    if (total < targetSamples && !behind && activeSources.length > 0) {
      return null;
    }

    const parts = [];
    let need = targetSamples;
    while (playQueue.length && need > 0) {
      const head = playQueue[0];
      if (head.length <= need) {
        parts.push(playQueue.shift());
        need -= head.length;
      } else {
        parts.push(head.subarray(0, need));
        playQueue[0] = head.subarray(need);
        need = 0;
      }
    }
    if (!parts.length) return null;
    if (parts.length === 1) return parts[0];
    const len = parts.reduce((a, p) => a + p.length, 0);
    const out = new Float32Array(len);
    let off = 0;
    for (const p of parts) {
      out.set(p, off);
      off += p.length;
    }
    return out;
  }

  /**
   * 24k mono f32 → ctx.sampleRate，线性插值（比浏览器隐式转换更稳）。
   */
  function upsampleLinear(f32, fromRate, toRate) {
    if (fromRate === toRate) return f32;
    const ratio = toRate / fromRate;
    const outLen = Math.max(1, Math.round(f32.length * ratio));
    const out = new Float32Array(outLen);
    const last = f32.length - 1;
    for (let i = 0; i < outLen; i++) {
      const src = i / ratio;
      const i0 = Math.floor(src);
      const i1 = i0 < last ? i0 + 1 : last;
      const t = src - i0;
      out[i] = f32[i0] * (1 - t) + f32[i1] * t;
    }
    return out;
  }

  /** 与上一块尾部做极短 crossfade，消除块边界咔哒/颤感 */
  function applyCrossfade(chunk) {
    const n = Math.min(
      chunk.length,
      Math.round((CROSSFADE_MS / 1000) * ctxRate()),
      tailSamples ? tailSamples.length : 0
    );
    if (!n || !tailSamples) {
      // 保存尾部供下一块
      const keep = Math.min(chunk.length, Math.max(n, 32));
      tailSamples = chunk.subarray(chunk.length - keep);
      return chunk;
    }
    const out = chunk.length === n ? new Float32Array(chunk) : chunk;
    // 若 chunk 是原数组视图且会被改写，复制一份
    const mutable = out === chunk ? new Float32Array(chunk) : out;
    for (let i = 0; i < n; i++) {
      const a = i / n;
      const prev = tailSamples[tailSamples.length - n + i];
      mutable[i] = prev * (1 - a) + mutable[i] * a;
    }
    const keep = Math.min(mutable.length, Math.max(n, 32));
    tailSamples = mutable.subarray(mutable.length - keep);
    return mutable;
  }

  function scheduleChunk(f32StreamRate) {
    if (!audioCtx || !f32StreamRate.length) return;

    const f32 = applyCrossfade(upsampleLinear(f32StreamRate, STREAM_RATE, ctxRate()));
    const buf = audioCtx.createBuffer(1, f32.length, ctxRate());
    // copyToChannel 在部分浏览器对共享 buffer 更安全
    buf.copyToChannel(f32, 0);

    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);

    const now = audioCtx.currentTime;
    // 时间轴连续：落后则贴 now+lookahead（underrun 恢复），不引入额外随机抖动
    let startAt;
    if (nextPlayTime <= 0) {
      startAt = now + LOOKAHEAD_SEC;
    } else if (nextPlayTime < now + 0.005) {
      // 已 underrun：紧贴当前，避免叠加重影
      startAt = now + 0.008;
    } else {
      startAt = nextPlayTime;
    }

    src.onended = () => {
      const i = activeSources.indexOf(src);
      if (i >= 0) activeSources.splice(i, 1);
    };
    activeSources.push(src);
    try {
      src.start(startAt);
      nextPlayTime = startAt + buf.duration;
    } catch {
      const i = activeSources.indexOf(src);
      if (i >= 0) activeSources.splice(i, 1);
    }
  }

  function pumpOnce() {
    if (!audioCtx) return;

    // 首启：等抖动缓冲够了再出声，避免开头一卡一卡
    if (nextPlayTime <= 0 && activeSources.length === 0) {
      if (queuedDurationSec() * 1000 < PREBUFFER_MS) {
        return;
      }
    }

    // 保持约 2～3 块在时间轴上，吃掉网络抖动
    const horizon = audioCtx.currentTime + 0.18;
    while (playQueue.length && (nextPlayTime <= 0 || nextPlayTime < horizon)) {
      const chunk = takeMergedChunk();
      if (!chunk) break;
      scheduleChunk(chunk);
      if (nextPlayTime > horizon) break;
    }
  }

  function ensurePump() {
    if (playing) return;
    playing = true;
    const tick = () => {
      if (!audioCtx) {
        playing = false;
        pumpTimer = null;
        return;
      }
      pumpOnce();
      if (playQueue.length > 0 || activeSources.length > 0) {
        pumpTimer = setTimeout(tick, PUMP_INTERVAL_MS);
      } else {
        playing = false;
        pumpTimer = null;
        nextPlayTime = 0;
        tailSamples = null;
        setTimeout(() => onPlayLevel?.(0.05), 120);
      }
    };
    tick();
  }

  function interruptPlayback() {
    playQueue = [];
    tailSamples = null;
    if (pumpTimer) {
      clearTimeout(pumpTimer);
      pumpTimer = null;
    }
    playing = false;
    for (const src of activeSources) {
      try {
        src.stop(0);
      } catch {}
      try {
        src.disconnect();
      } catch {}
    }
    activeSources = [];
    nextPlayTime = 0;
    bargeHoldSec = 0;
    onPlayLevel?.(0);
  }

  /** 得助等待：本地轻提示音（不阻塞上游垫词） */
  function startToolWaitTone() {
    if (!audioCtx || toolWaitToneOn) return;
    toolWaitToneOn = true;
    const beat = () => {
      if (!toolWaitToneOn || !audioCtx) return;
      try {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = "sine";
        osc.frequency.value = 660;
        gain.gain.value = 0.0001;
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        const t0 = audioCtx.currentTime;
        gain.gain.exponentialRampToValueAtTime(0.03, t0 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.18);
        osc.start(t0);
        osc.stop(t0 + 0.2);
      } catch {}
      toolWaitTimer = setTimeout(beat, 900);
    };
    beat();
  }

  function stopToolWaitTone() {
    toolWaitToneOn = false;
    if (toolWaitTimer) {
      clearTimeout(toolWaitTimer);
      toolWaitTimer = null;
    }
  }

  async function stop() {
    stopToolWaitTone();
    interruptPlayback();
    try {
      processor?.disconnect();
    } catch {}
    try {
      source?.disconnect();
    } catch {}
    mediaStream?.getTracks().forEach((t) => t.stop());
    if (audioCtx) await audioCtx.close();
    audioCtx = null;
    mediaStream = null;
    processor = null;
    source = null;
  }

  return {
    start,
    stop,
    setMuted,
    enqueueBase64Pcm,
    interruptPlayback,
    startToolWaitTone,
    stopToolWaitTone,
    botIsPlaying,
  };
}

function downsampleToPcm16(float32, inRate, outRate) {
  if (outRate === inRate) {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }
  const ratio = inRate / outRate;
  const newLen = Math.floor(float32.length / ratio);
  const out = new Int16Array(newLen);
  for (let i = 0; i < newLen; i++) {
    // 线性插值，减轻上行锯齿
    const src = i * ratio;
    const i0 = Math.floor(src);
    const i1 = Math.min(float32.length - 1, i0 + 1);
    const t = src - i0;
    const s = Math.max(
      -1,
      Math.min(1, (float32[i0] || 0) * (1 - t) + (float32[i1] || 0) * t)
    );
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function base64ToUint8(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

window.createAudioPipeline = createAudioPipeline;
