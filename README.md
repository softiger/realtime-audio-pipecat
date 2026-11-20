# 全双工实时语音机器人 · Pipecat + Qwen Audio Realtime + 得助

## 架构

```
浏览器（16k PCM 二进制上行 / 24k PCM 播放）
        │  /ws/voice
        ▼
FastAPI
        │
        ▼
Pipecat Pipeline
  transport.input  →  QwenRealtimeService  →  meta  →  transport.output
                              │
                              ├─ DashScope Realtime WS（ASR / VAD / TTS / FC）
                              └─ tool query_business_bot → 得助 /chat/reply
```

- **框架**：[Pipecat](https://docs.pipecat.ai) Pipeline + `FastAPIWebsocketTransport`
- **语音通路**：浏览器 ↔ Serializer(PCM) ↔ Qwen `qwen-audio-3.1-realtime-plus`
- **业务脑**：模型 Function Calling → 得助 OpenAPI（通话内复用 `sessionId`）
- **低时延**：二进制 PCM 上行、小块出音频、server VAD/smart_turn 打断、工具结果立即 `response.create`

## 快速开始

```powershell
cd c:\pythonprojects\realtime-dezhuapi-pipecat
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
copy .env.example .env
# 编辑 .env：DASHSCOPE_API_KEY + 得助参数（向管理员获取，勿使用明文凭证文件）
python -m app.main
```

浏览器打开：http://127.0.0.1:8766

## 环境变量

| 变量 | 说明 |
|------|------|
| `DASHSCOPE_API_KEY` | 百炼 API Key |
| `DASHSCOPE_WORKSPACE_ID` | 可选 Workspace |
| `REALTIME_MODEL` | 默认 `qwen-audio-3.1-realtime-plus` |
| `REALTIME_VOICE` | 默认 `longanqian_v3.1` |
| `TURN_DETECTION` | `smart_turn_v2` / `server_vad` / `manual` |
| `DZ_API_BASE_URL` | 默认 `https://dzai.dezhuyun.com` |
| `DZ_APP_ID` | 得助 appId |
| `DZ_CHANNEL_ID` | 渠道 ID |
| `DZ_PRIVATE_KEY` | RSA 私钥（PKCS#8，可无 PEM 头） |
| `DZ_SIGN_MODE` | 默认 `sha256_pkcs1`；另有 `sha1_pkcs1` / `pss_sha256` |

## 目录

```
app/
  dezhu/          # 签名、文本应答、CallSession
  pipeline/       # Pipecat 管线：Serializer / QwenRealtime / bot
  realtime/       # 兼容保留的 session 配置（已迁到 pipeline）
  static/         # 科幻前端（二进制 PCM 上行）
  config.py
  main.py
```

## 技术方案
- 双脑架构：Qwen Realtime 语音脑 + 得助 OpenAPI 业务脑
- 电话呼入（SIP/FreeSWITCH）、WebRTC、媒体处理与转人工
- 低时延预算（P50/P95）、语义判停、三级打断、投机预取
- 得助 `/chat/reply` 与 `/dm/reply/answer` 双引擎集成
- 高可用、容量、成本、安全合规、可观测、实施路线与验收标准

## 时延相关设计

| 环节 | 做法 |
|------|------|
| 上行音频 | WebSocket **binary PCM16**，避免 base64 JSON |
| 采集块 | ScriptProcessor 2048（约 40ms@48k） |
| 出音频 | `audio_out_10ms_chunks=2`（约 20ms@24k） |
| 打断 | `speech_started` → `InterruptionFrame` + 前端 `interruptPlayback` |
| 业务 | FC 完成后立刻 `function_call_output` + `response.create` |
| 轮次 | 默认 `smart_turn_v2`；`server_vad` 静音 500ms |

## 注意

- 麦克风需要 HTTPS 或 localhost
- API Key / 私钥只放服务端 `.env`，不要进前端
- 若得助鉴权失败，尝试切换 `DZ_SIGN_MODE`
