/**
 * 浏览器音频通路（低时延 + 稳播）：
 *   Mic → 降采样 16k PCM16 → WebSocket binary（优先）/ JSON base64（兼容）
 *   WebSocket JSON response.audio.delta(24k base64) → 抖动缓冲 → 无缝调度播放
 *
 * 颤音/断续/哒哒常见原因与对策：
 *   1) 过小 delta + 网络抖动 → underrun → MERGE / PREBUFFER / HORIZON 加大
 *   2) 块与块首尾相接时做「尾混头」crossfade 会跳变哒哒 → 连续 PCM 只做相位连续升采样
 *   3) 24k→48k 分块独立插值会在边界断相 → 保留 stream 历史点 + 分数相位
 *   4) underrun 恢复 → 短 fade-in，避免硬起声
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
  const BARGE_ENERGY = 0.1;
  const BARGE_HOLD_SEC = 0.15;

  // 播放稳定参数（抖动→加缓冲；哒哒→接缝算法，不是盲目加 crossfade）
  const MERGE_MS = 220; // 合并后再播，减少 schedule 次数
  const PREBUFFER_MS = 320; // 首包前抖动缓冲
  const LOOKAHEAD_SEC = 0.05; // 相对 currentTime 的安全提前量
  const HORIZON_SEC = 0.55; // 时间轴预排水位
  const PUMP_INTERVAL_MS = 12;
  const UNDERRUN_FADE_MS = 12; // underrun 恢复时淡入，避免硬哒
  const PLAY_SOFT_CLIP = 0.92;
  /** Hermite 连续升采样：保留的 24k 历史点数 */
  const RESAMPLE_PAD = 3;

  /** 24k 历史样本（升采样相位连续） */
  let streamPad = null;
  /** 下一输出点在「当前块起点」上的分数位置（输入样本单位） */
  let streamFrac = 0;
  /** 下一块是否需要 underrun 淡入 */
  let needFadeIn = false;

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

  function softClip(s) {
    if (s > PLAY_SOFT_CLIP) s = PLAY_SOFT_CLIP + (s - PLAY_SOFT_CLIP) * 0.15;
    else if (s < -PLAY_SOFT_CLIP) s = -PLAY_SOFT_CLIP + (s + PLAY_SOFT_CLIP) * 0.15;
    return s < -1 ? -1 : s > 1 ? 1 : s;
  }

  function resetResampler() {
    streamPad = null;
    streamFrac = 0;
  }

  /**
   * 24k → ctxRate，相位连续 Catmull-Rom。
   * 连续 PCM 首尾相接不要 crossfade；插值历史不断则边界无哒哒。
   */
  function upsampleHermiteContinuous(f32, fromRate, toRate) {
    if (!f32.length) return new Float32Array(0);
    if (fromRate === toRate) {
      const keep = Math.min(RESAMPLE_PAD, f32.length);
      streamPad = new Float32Array(f32.subarray(f32.length - keep));
      streamFrac = 0;
      const out = new Float32Array(f32.length);
      for (let i = 0; i < f32.length; i++) out[i] = softClip(f32[i]);
      return out;
    }

    const padLen = streamPad && streamPad.length ? streamPad.length : 0;
    const input = new Float32Array(padLen + f32.length);
    if (padLen) input.set(streamPad, 0);
    input.set(f32, padLen);
    const last = input.length - 1;
    const at = (i) => input[i < 0 ? 0 : i > last ? last : i];

    const step = fromRate / toRate;
    let pos = streamFrac;
    const endPos = f32.length;
    const est = Math.max(1, Math.ceil((endPos - pos) / step) + 2);
    const tmp = new Float32Array(est);
    let n = 0;
    while (pos < endPos) {
      const ip = pos + padLen;
      const i1 = Math.floor(ip);
      const t = ip - i1;
      const p0 = at(i1 - 1);
      const p1 = at(i1);
      const p2 = at(i1 + 1);
      const p3 = at(i1 + 2);
      const a = -0.5 * p0 + 1.5 * p1 - 1.5 * p2 + 0.5 * p3;
      const b = p0 - 2.5 * p1 + 2 * p2 - 0.5 * p3;
      const c = -0.5 * p0 + 0.5 * p2;
      const d = p1;
      tmp[n++] = softClip(((a * t + b) * t + c) * t + d);
      pos += step;
    }
    streamFrac = pos - endPos;
    const keep = Math.min(RESAMPLE_PAD, f32.length);
    streamPad = new Float32Array(f32.subarray(f32.length - keep));

    if (n === tmp.length) return tmp;
    const out = new Float32Array(n);
    out.set(tmp.subarray(0, n));
    return out;
  }

  function applyFadeIn(chunk, ms) {
    const n = Math.min(chunk.length, Math.round((ms / 1000) * ctxRate()));
    if (n <= 1) return chunk;
    const out = chunk instanceof Float32Array ? new Float32Array(chunk) : chunk;
    for (let i = 0; i < n; i++) {
      out[i] *= Math.sin(((i / n) * Math.PI) / 2);
    }
    return out;
  }

  function scheduleChunk(f32StreamRate) {
    if (!audioCtx || !f32StreamRate.length) return;

    const now = audioCtx.currentTime;
    let startAt;
    let fadeThis = false;
    if (nextPlayTime <= 0) {
      startAt = now + LOOKAHEAD_SEC;
    } else if (nextPlayTime < now + 0.01) {
      startAt = now + LOOKAHEAD_SEC;
      resetResampler();
      fadeThis = true;
    } else {
      startAt = nextPlayTime;
    }
    if (needFadeIn) {
      fadeThis = true;
      needFadeIn = false;
    }

    let f32 = upsampleHermiteContinuous(f32StreamRate, STREAM_RATE, ctxRate());
    if (!f32.length) return;
    if (fadeThis) f32 = applyFadeIn(f32, UNDERRUN_FADE_MS);

    const buf = audioCtx.createBuffer(1, f32.length, ctxRate());
    buf.copyToChannel(f32, 0);

    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    const gain = audioCtx.createGain();
    gain.gain.value = 0.95;
    src.connect(gain);
    gain.connect(audioCtx.destination);

    src.onended = () => {
      const i = activeSources.indexOf(src);
      if (i >= 0) activeSources.splice(i, 1);
      try {
        gain.disconnect();
      } catch {}
    };
    activeSources.push(src);
    try {
      src.start(startAt);
      // 与 buffer 实际时长对齐，避免 1～2 样本缝隙哒哒
      nextPlayTime = startAt + buf.duration;
    } catch {
      const i = activeSources.indexOf(src);
      if (i >= 0) activeSources.splice(i, 1);
    }
  }

  function pumpOnce() {
    if (!audioCtx) return;

    if (nextPlayTime <= 0 && activeSources.length === 0) {
      if (queuedDurationSec() * 1000 < PREBUFFER_MS) return;
    }

    const horizon = audioCtx.currentTime + HORIZON_SEC;
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
        resetResampler();
        needFadeIn = false;
        setTimeout(() => onPlayLevel?.(0.05), 120);
      }
    };
    tick();
  }

  function interruptPlayback() {
    playQueue = [];
    resetResampler();
    needFadeIn = false;
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
