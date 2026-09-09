"""MOTrade 可调参数。"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "motrade.db"

GOATBOTS_BASE = "https://www.goatbots.com"
GOATBOTS_CARD_DEFINITIONS_URL = f"{GOATBOTS_BASE}/download/prices/card-definitions.zip"
GOATBOTS_PRICE_HISTORY_URL = f"{GOATBOTS_BASE}/download/prices/price-history.zip"

SCRYFALL_BULK_DATA_API = "https://api.scryfall.com/bulk-data"

# 请求头：GoatBots 对无 UA 的请求返回 403，需要带一个常规浏览器 UA。
# 只做每日一次的批量下载，不做高频请求。
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Watchlist 筛选参数
WATCHLIST_FORMATS = ["modern", "legacy"]
WATCHLIST_MIN_PRICE_TIX = 0.5  # 历史最高卖价需 >= 此值，过滤纯废卡
