import aiohttp
from config.settings import settings
from utils.logger import log

class TelegramNotifier:
    def __init__(self):
        self.enabled = settings.TELEGRAM_ENABLED and bool(settings.TELEGRAM_BOT_TOKEN) and bool(settings.TELEGRAM_CHAT_ID)
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID

    async def send_message(self, text: str):
        if not self.enabled:
            return
        
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        log.warning(f"[Telegram] Failed to send notification: {await resp.text()}")
        except Exception as e:
            log.warning(f"[Telegram] Error sending message: {e}")

notifier = TelegramNotifier()
