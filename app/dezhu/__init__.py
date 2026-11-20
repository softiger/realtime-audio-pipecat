"""得助文本应答客户端。"""

from app.dezhu.client import DezhuChatClient, extract_speakable_text
from app.dezhu.session import CallSession

__all__ = ["DezhuChatClient", "CallSession", "extract_speakable_text"]
