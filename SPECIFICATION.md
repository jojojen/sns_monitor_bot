# SNS Monitor Bot - 規格文件

**Project**: sns_monitor_bot for aka no claw  
**Version**: 0.1.0  
**Status**: ✓ Implementation Complete  
**Date**: 2026-05-12

---

## 目錄

1. [概述](#概述)
2. [功能規格](#功能規格)
3. [架構設計](#架構設計)
4. [資料模型](#資料模型)
5. [API 規格](#api-規格)
6. [儲存層](#儲存層)
7. [CLI 命令](#cli-命令)
8. [測試結果](#測試結果)
9. [部署指南](#部署指南)

---

## 概述

### 目的

sns_monitor_bot 是一個監控 X (Twitter) 的自動化機器人，用於追蹤特定帳號、關鍵字和熱門話題。適用於 aka no claw 的社群監控和資訊收集。

### 核心功能

- **帳號監控** - 追蹤特定 X 帳號的發文
- **關鍵字搜尋** - 監控包含特定關鍵字的推文
- **熱門話題追蹤** - 監控 X 上的熱門話題變化
- **Telegram 通知** - 通過 Telegram Bot 發送實時通知
- **人類化行為** - 隨機延遲和請求間隔以避免被識別為機器人
- **持久化儲存** - SQLite 資料庫持久化監控規則和推文歷史

### 設計原則

1. **模組化** - 清晰分離 storage、client、monitor、formatter
2. **非同步優先** - twikit 是 asyncio-native，monitor 在後台線程中運行
3. **容錯機制** - 電路斷路器模式處理速率限制和認證失敗
4. **人類模擬** - 隨機延遲和請求頻率控制
5. **無狀態監控** - 透過資料庫追蹤已見推文和最後檢查時間

---

## 功能規格

### 1. 帳號監控 (Account Watch)

**描述**: 監控特定 X 帳號的新發文

**使用場景**:
- 監控官方帳號公告
- 追蹤特定用戶的活動
- 監控競爭對手

**參數**:
- `screen_name` (必須): 帳號名稱，不需要 @ 符號
- `user_id` (可選): 數字帳號 ID，首次檢查時自動解析
- `label` (可選): 人類可讀的標籤
- `include_keywords` (可選): 只通知推文本文包含任一指定詞的帳號發文；空值表示通知所有新推文
- `schedule_minutes` (可選): 檢查間隔，預設 15 分鐘
- `chat_id`: Telegram 聊天 ID

**檢查邏輯**:
1. 首次檢查: 取得最新發文，標記所有為「已通知」(避免初始化時洪泛)
2. 後續檢查: 取得最新發文，比較資料庫，只通知通過 `include_keywords` 篩選的新推文
3. 檢查頻率: 根據 `last_checked_at + schedule_minutes` 決定是否執行

**數據流**:
```
Schedule → Check Due? → Resolve user_id → Get timeline → Deduplicate → Record → Notify
```

### 2. 關鍵字監控 (Keyword Watch)

**描述**: 搜尋並監控包含特定關鍵字的推文

**使用場景**:
- 監控品牌提及
- 追蹤特定話題
- 監控用戶評論

**參數**:
- `query` (必須): X 搜尋查詢語句 (支持 X 搜尋語法)
- `label` (可選): 人類可讀的標籤
- `schedule_minutes` (可選): 檢查間隔，預設 30 分鐘
- `chat_id`: Telegram 聊天 ID

**檢查邏輯**:
- 首次檢查: 記錄所有結果為「已通知」
- 後續檢查: 搜尋，去重，通知新推文
- 按 `created_at` 降序排列，取最新結果

### 3. 熱門話題追蹤 (Trend Watch)

**描述**: 監控 X 熱門話題，通知新出現的話題

**使用場景**:
- 跟踪全球熱門話題
- 監控特定類別趨勢 (新聞、運動等)
- 識別新興話題

**參數**:
- `category` (必須): 話題分類
  - `trending` - 全球趨勢
  - `for-you` - 為你推薦
  - `news` - 新聞
  - `sports` - 運動
  - `entertainment` - 娛樂
- `label` (可選): 人類可讀的標籤
- `schedule_minutes` (可選): 檢查間隔，預設 60 分鐘
- `chat_id`: Telegram 聊天 ID

**檢查邏輯**:
1. 首次檢查: 記錄話題快照，無通知
2. 後續檢查: 比較前次快照，通知新出現的話題
3. `TrendSnapshot` 儲存快照，用於增量更新

---

## 架構設計

### 系統架構圖

```
┌─────────────────────────────────────────────────────┐
│                   CLI Layer                         │
│              (src/sns_monitor/__main__.py)          │
│  add-account  add-keyword  add-trend  run  list     │
└──────────────────────┬──────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
    Storage       Monitor        X Client
    (SQLite)      (Async)        (twikit)
        │              │              │
        ▼              ▼              ▼
   ┌─────────────────────────────────────────┐
   │  models.py                              │
   │  - Tweet, WatchRule, TrendSnapshot      │
   └─────────────────────────────────────────┘
```

### 模組結構

```
src/sns_monitor/
├── __init__.py          - 包初始化
├── __main__.py          - CLI 入口點，命令調度
├── models.py            - 資料類別 (凍結 dataclass)
├── storage.py           - SQLite 資料庫操作
├── x_client.py          - twikit 非同步包裝，電路斷路器
├── monitor.py           - 後台監控守護線程
├── formatters.py        - 通知文本格式化
└── telegram.py          - Telegram Bot API 客戶端
```

### 線程模型

```
Main Thread
    ↓
CLI → initialize SnsMonitor
    ↓
    └─→ [Background Thread: sns-monitor]
            ↓
            asyncio.new_event_loop()
            ↓
            loop.run_until_complete(_async_loop)
                ↓
                while not _stop:
                  ├─ tick all rules
                  ├─ await get_timeline() / search() / get_trends()
                  ├─ notify via callback
                  └─ sleep
```

### 非同步/同步邊界

- **邊界**: `monitor.py` 中的 `_run_loop()` 創建和擁有 asyncio 事件迴圈
- **接口**: `start()`, `stop()`, `is_running()` 是同步的
- **內部**: `_async_loop()` 及其下層都是 async/await

### 容錯機制

#### 電路斷路器模式

```python
class XClient:
    _COOLDOWN_SECONDS = 600.0       # 10 分鐘
    _RATE_LIMIT_BACKOFF = 900.0    # 15 分鐘
    
    def _trip_circuit(self, cooldown: float):
        self._disabled_until = time.monotonic() + cooldown
        # 後續呼叫返回 [] 而不接觸 twikit
```

**觸發條件**:
- `TooManyRequests` (429) → 15 分鐘冷卻
- `Unauthorized`, `AccountSuspended`, `AccountLocked` → 10 分鐘冷卻 + 刪除 cookies.json
- 其他異常 → 記錄並返回空結果

#### 首次檢查基線

```python
if is_first_check:
    # 標記所有為「已通知」，不返回新推文
    record_tweets(..., mark_all_notified=True)
    return []
```

**目的**: 避免初始化時推送大量歷史推文

---

## 資料模型

### Tweet

```python
@dataclass(frozen=True)
class Tweet:
    tweet_id: str
    author_handle: str          # 不含 @
    author_id: str
    text: str
    created_at: datetime
    lang: str | None = None
    retweet_count: int = 0
    like_count: int = 0
    url: str = ""               # https://x.com/{handle}/status/{id}
```

### AccountWatch

```python
@dataclass(frozen=True)
class AccountWatch:
    rule_id: str
    screen_name: str            # 不含 @，首次檢查時解析為 user_id
    user_id: str | None
    label: str
    include_keywords: tuple[str, ...] = ()
    enabled: bool = True
    schedule_minutes: int = 15
    chat_id: str = ""
    last_checked_at: datetime | None = None
```

### KeywordWatch

```python
@dataclass(frozen=True)
class KeywordWatch:
    rule_id: str
    query: str                  # X 搜尋查詢語句
    label: str
    enabled: bool = True
    schedule_minutes: int = 30
    chat_id: str = ""
    last_checked_at: datetime | None = None
```

### TrendWatch

```python
@dataclass(frozen=True)
class TrendWatch:
    rule_id: str
    category: str               # trending|for-you|news|sports|entertainment
    label: str
    enabled: bool = True
    schedule_minutes: int = 60
    chat_id: str = ""
    last_checked_at: datetime | None = None
```

### TrendSnapshot

```python
@dataclass(frozen=True)
class TrendSnapshot:
    snapshot_id: str
    rule_id: str
    names: tuple[str, ...]      # 話題名稱列表
    captured_at: datetime
```

### 型別別名

```python
WatchKind = Literal["account", "keyword", "trend"]
WatchRule = Union[AccountWatch, KeywordWatch, TrendWatch]
```

---

## API 規格

### XClient

非同步 twikit 包裝，提供人類化行為和容錯。

```python
class XClient:
    async def ensure_logged_in() -> None
    async def get_timeline(user_id: str, count: int = 20) -> list[Tweet]
    async def resolve_user_id(screen_name: str) -> str
    async def search(query: str, count: int = 20) -> list[Tweet]
    async def get_trends(category: str, count: int = 20) -> list[str]
    
    def _is_disabled() -> bool
    def _trip_circuit(cooldown: float) -> None
```

**錯誤處理**:
- 返回空列表 `[]` 或 `""` 而非異常
- 所有例外都被捕獲和記錄

### SnsDatabase

SQLite 資料庫操作。

```python
class SnsDatabase:
    def bootstrap() -> None                          # 建立表和索引
    def save_watch_rule(rule: WatchRule) -> None
    def get_watch_rule(rule_id: str) -> WatchRule | None
    def list_watch_rules(kind: WatchKind | None = None) -> list[WatchRule]
    def delete_watch_rule(rule_id: str) -> bool
    def toggle_watch_rule(rule_id: str, enabled: bool) -> bool
    
    def record_tweets(rule_id: str, tweets: list[Tweet]) -> list[Tweet]
    def mark_tweets_notified(rule_id: str, tweet_ids: list[str]) -> None
    
    def save_trend_snapshot(snapshot: TrendSnapshot) -> None
    def latest_trend_snapshot(rule_id: str) -> TrendSnapshot | None
    
    def mark_rule_checked(rule_id: str) -> None
    def update_user_id(rule_id: str, user_id: str) -> None
    
    @staticmethod
    def _watch_rule_id(kind: str, key: str) -> str
    @staticmethod
    def _snapshot_id(rule_id: str, captured_at_iso: str) -> str
```

### SnsMonitor

後台監控守護線程。

```python
class SnsMonitor:
    def __init__(
        db_path: str | Path,
        x_client: XClient,
        notify_fn: Callable[[str, str], None],
        interval_seconds: int = 60,
    ) -> None
    
    def start() -> None                             # 啟動後台線程
    def stop() -> None                              # 信號停止
    def is_running() -> bool
    
    async def _async_loop() -> None                 # 主監控迴圈
    async def _async_tick() -> None                 # 檢查所有規則
    def _is_due(rule: WatchRule) -> bool           # 檢查時間表
```

### TelegramClient

Telegram Bot API 客戶端，使用 stdlib urllib。

```python
class TelegramClient:
    def __init__(token: str, timeout_seconds: float = 35.0) -> None
    def send_message(chat_id: str | int, text: str) -> dict[str, object]
```

---

## 儲存層

### 資料庫架構

#### watch_rules 表

```sql
CREATE TABLE watch_rules (
    rule_id      TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,          -- "account" | "keyword" | "trend"
    label        TEXT NOT NULL,
    query_json   TEXT NOT NULL,          -- JSON 序列化規則特定欄位
    enabled      INTEGER NOT NULL DEFAULT 1,
    schedule_minutes INTEGER NOT NULL,
    chat_id      TEXT NOT NULL,
    last_checked_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
```

**query_json 格式**:
```json
// account
{ "screen_name": "...", "user_id": "..." }

// keyword
{ "query": "..." }

// trend
{ "category": "..." }
```

#### seen_tweets 表

```sql
CREATE TABLE seen_tweets (
    tweet_id     TEXT NOT NULL,
    rule_id      TEXT NOT NULL,
    author_handle TEXT NOT NULL,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    notified     INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (tweet_id, rule_id),
    FOREIGN KEY (rule_id) REFERENCES watch_rules(rule_id) ON DELETE CASCADE
);
```

**目的**: 
- 去重 - 避免相同推文重複通知
- 通知追蹤 - `notified` 欄位記錄是否已通知
- 去重能夠跨檢查週期

#### trend_snapshots 表

```sql
CREATE TABLE trend_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    rule_id      TEXT NOT NULL,
    names_json   TEXT NOT NULL,          -- JSON 陣列
    captured_at  TEXT NOT NULL,
    FOREIGN KEY (rule_id) REFERENCES watch_rules(rule_id) ON DELETE CASCADE
);
```

**目的**: 追蹤話題快照，識別新話題

### ID 生成

**watch_rule_id**:
```python
sha1(f"{kind}|{key}".encode()).hexdigest()[:12]
# 範例: "account_d0e3942ba427"
```

**snapshot_id**:
```python
sha1(f"{rule_id}|{captured_at_iso}".encode()).hexdigest()[:16]
```

**優點**: 確定性、簡潔、避免序列 ID

---

## CLI 命令

### add-account

監控特定帳號。

```bash
sns-monitor add-account @username [--label "My Label"] --chat-id 123 [--interval 15] [--db path]
```

**參數**:
- `screen_name`: 帳號名稱 (@ 可選)
- `--label`: 人類可讀標籤 (預設: screen_name)
- `--chat-id` (必須): Telegram 聊天 ID
- `--interval`: 檢查間隔，分鐘 (預設: 15)
- `--db`: 資料庫路徑 (預設: data/sns.sqlite3)

**範例**:
```bash
sns-monitor add-account elonmusk --chat-id 123 --label "Elon Musk" --interval 10
```

### add-keyword

監控關鍵字搜尋。

```bash
sns-monitor add-keyword "search query" [--label "My Label"] --chat-id 123 [--interval 30] [--db path]
```

**範例**:
```bash
sns-monitor add-keyword "機動戰士" --chat-id 123 --label "Gundam" --interval 30
```

### add-trend

監控熱門話題。

```bash
sns-monitor add-trend {trending|for-you|news|sports|entertainment} --chat-id 123 [--interval 60] [--db path]
```

**範例**:
```bash
sns-monitor add-trend trending --chat-id 123 --interval 60
```

### list

列出所有監控規則。

```bash
sns-monitor list [--kind {account|keyword|trend}] [--db path]
```

**輸出範例**:
```
✓ ENABLED | @elonmusk (@elonmusk)
         ID: account_d0e3942ba427 | Last: 2026-05-12T10:30:00+00:00
✗ DISABLED | Keyword: 機動戰士 (Gundam)
         ID: keyword_1ef668326c55 | Last: Never
```

### delete

刪除監控規則。

```bash
sns-monitor delete RULE_ID [--db path]
```

### toggle

啟用或停用規則。

```bash
sns-monitor toggle RULE_ID {--enabled|--disabled} [--db path]
```

### run

啟動監控守護線程。

```bash
sns-monitor run [--db path] [--interval 60]
```

**環境變數要求**:
- `X_USERNAME`: X 帳號名稱
- `X_USER_MAIL`: X 帳號信箱
- `X_USER_PASSWORD`: X 帳號密碼
- `TELEGRAM_BOT_TOKEN` (可選): Telegram Bot Token
- `TELEGRAM_CHAT_ID` (可選): 預設 Telegram 聊天 ID

**行為**:
1. 讀取 `.env`
2. 初始化 SQLite 資料庫
3. 啟動非同步事件迴圈
4. 連續監控，按 Ctrl+C 停止

---

## 測試結果

### 單元測試

✓ All tests passed

```
Test Suite: sns_monitor_bot Integration Tests
============================================================

[Test 1: Account Watch]
  ✓ Saved and retrieved account watch

[Test 2: Keyword Watch]
  ✓ Saved and retrieved keyword watch

[Test 3: Trend Watch]
  ✓ Saved and retrieved trend watch

[Test 4: Tweet Deduplication]
  ✓ First check marks all as notified
  ✓ Duplicates are skipped

[Test 5: Notification Formatting]
  ✓ Account notification formatted
  ✓ Keyword notification formatted
  ✓ Trend notification formatted

[Test 6: List Rules]
  ✓ Listed 3 rules
  ✓ Filtered by kind: 1 account rules

[Test 7: Enable/Disable Rules]
  ✓ Disabled rule successfully
  ✓ Re-enabled rule successfully

[Test 8: Delete Rule]
  ✓ Deleted rule successfully

============================================================
ALL TESTS PASSED ✓
```

### CLI 測試

✓ add-account 成功
✓ add-keyword 成功
✓ add-trend 成功
✓ list 成功
✓ toggle 成功
✓ delete 成功

### 功能測試

**資料持久化**: ✓ SQLite 資料庫正確保存和檢索規則

**去重邏輯**: ✓ 推文去重按 (tweet_id, rule_id) 正確工作

**通知格式化**: ✓ 繁體中文通知格式正確

**錯誤處理**: ✓ 資料庫錯誤和無效輸入正確處理

---

## 部署指南

### 系統要求

- Python 3.10+
- pip (Python 套件管理)
- 網路連接 (X 和 Telegram API)

### 安裝步驟

1. **複製專案**:
```bash
cd /path/to/sns_monitor_bot
```

2. **建立虛擬環境**:
```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate  # Windows
```

3. **安裝依賴**:
```bash
pip install twikit python-dotenv
```

4. **設定認證**:
編輯 `.env` 檔案:
```env
X_USERNAME=你的X帳號
X_USER_MAIL=你的信箱@example.com
X_USER_PASSWORD=你的密碼
TELEGRAM_BOT_TOKEN=你的Bot Token
TELEGRAM_CHAT_ID=你的Chat ID
```

5. **測試安裝**:
```bash
python -m sns_monitor list
```

應該會顯示「No watch rules found」。

### 使用步驟

1. **新增監控規則**:
```bash
python -m sns_monitor add-account elonmusk --chat-id 123
python -m sns_monitor add-keyword "python" --chat-id 123
python -m sns_monitor add-trend trending --chat-id 123
```

2. **檢查規則**:
```bash
python -m sns_monitor list
```

3. **啟動監控**:
```bash
python -m sns_monitor run
```

4. **在後台運行** (Linux/Mac):
```bash
nohup python -m sns_monitor run > logs/sns-monitor.log 2>&1 &
```

5. **停止監控**:
按 Ctrl+C 或殺死進程

### 故障排除

**ImportError: twikit**
- 確認 Python 3.10+ 已安裝
- `python --version`

**"Unauthorized" 錯誤**
- 刪除 `cookies.json`，強制重新認證
- 驗證 X 認證信息正確

**Telegram 通知未送達**
- 驗證 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 正確
- 檢查 Bot 權限

**沒有推文被返回**
- 帳號可能被速率限制，等待 15 分鐘
- 增加檢查間隔以減少 API 呼叫

---

## 架構對標

本項目遵循 `price_monitor_bot` 的架構模式:

| 層級 | price_monitor_bot | sns_monitor_bot |
|------|-------------------|-----------------|
| Model | TrackItem, Offer | Tweet, WatchRule |
| Storage | MonitorDatabase | SnsDatabase |
| Client | HttpClient | XClient (async) |
| Monitor | MercariWatchMonitor | SnsMonitor |
| Bot | TelegramBotClient | TelegramClient |

**共同模式**:
- 凍結 dataclass (frozen=True)
- SQLite + INSERT OR REPLACE
- 後台守護線程
- 電路斷路器容錯
- 人類化行為 (延遲、請求頻率)

---

## 未來增強

- [ ] Web UI 規則管理
- [ ] 多帳號支援自動輪轉
- [ ] Discord Webhook 通知
- [ ] 進階篩選 (僅回覆、僅轉發等)
- [ ] 推文存檔和搜尋
- [ ] 整合 aka_no_claw Agent 系統

---

## 變更記錄

### v0.1.0 - 2026-05-12
- ✓ 初始實現
- ✓ 帳號、關鍵字、話題監控
- ✓ SQLite 儲存
- ✓ Telegram 通知
- ✓ CLI 命令
- ✓ 所有測試通過

---

**文件準備者**: Claude Code  
**最後更新**: 2026-05-12  
**狀態**: ✓ 完成並測試
