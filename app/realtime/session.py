"""兼容：session 配置已迁至 app.pipeline.session_config。"""

from app.pipeline.session_config import (  # noqa: F401
    DEFAULT_INSTRUCTIONS,
    QUERY_BUSINESS_BOT_TOOL,
    build_session_dict,
    build_session_update,
    build_turn_detection,
)
