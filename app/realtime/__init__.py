"""兼容导出：旧 RealtimeBridge 已由 Pipecat pipeline 替代。"""

from app.pipeline.session_config import DEFAULT_INSTRUCTIONS, build_session_update

__all__ = ["DEFAULT_INSTRUCTIONS", "build_session_update"]
