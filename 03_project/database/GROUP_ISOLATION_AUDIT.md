# 群組資料隔離 — 資料庫層稽核與交接

**對應需求：** 另一位組員提出的「群組對話紀錄隔離」需求
**異動檔案：** `03_project/code/trip_assistant_bot/db.py`
**範圍：** 只處理「資料庫相關的部分」（MongoDB 讀寫）。記憶體內的 `conversation_states` 與
`#reset` 指令屬於 `app.py` 的應用邏輯，稽核結果附在本文件最後一節，但**沒有**在這次改動裡實作。
**日期：** 2026-08-03（2026-08-09 更新：`api_query_cache` 改為強制以 `line_group_id` 隔離，見第二節）

---

## 一、稽核結論總表

| 資料類別 | 對應位置 | 隔離狀態 | 說明 |
|---|---|---|---|
| 最近對話 | `db.py: get_recent_messages()` | ✅ 已隔離 | 查詢條件本來就有 `line_group_id` |
| 歷史相關對話（RAG） | `db.py: get_similar_messages()` | ✅ 已隔離 | `line_group_id` 是必填參數，語意檢索前就先過濾 |
| 已確認條件 | `db.py: summaries` collection | ⚠️ 有記錄但沒有讀取介面 | `save_summary()` 有存 `line_group_id`，但**沒有對應的查詢函式**；已新增 `get_latest_summary()` |
| 群組偏好 | `db.py: user_preferences` collection | ⚠️ 設計文件有，程式完全沒實作 | 這次從零新增 `upsert_user_preference()` / `get_user_preferences()` / `get_group_preferences()` |
| 對話狀態（記憶體） | `app.py: conversation_states` | ✅ 已隔離（現況） | 見第四節，非本次改動範圍 |
| API 查詢紀錄 | `db.py: api_query_cache` collection | ✅ 已隔離（2026-08-09 起強制） | `line_group_id` 現在是唯一鍵的一部分，見第二節 |

**白話總結：** 訊息和 RAG 檢索原本就做對了，這次真正補上的缺口是「群組偏好」（之前完全沒有存取函式）跟「已確認條件」（存了但讀不到）。這兩塊如果照舊放著不管，之後串 AI 流程時很容易寫出「忘記加 `line_group_id`」的查詢，造成題目描述的那種跨群組污染。

---

## 二、新增的函式（全部嚴格以 `line_group_id` 隔離）

### 群組偏好

```python
upsert_user_preference(line_group_id, line_user_id, preference_type, preference_value) -> None
get_user_preferences(line_group_id, line_user_id) -> list[dict]
get_group_preferences(line_group_id) -> list[dict]
```

- `upsert_user_preference` 的更新條件永遠同時帶 `line_group_id` + `line_user_id`，就算同一個使用者同時待在群組 A、B，兩邊的偏好也是各自獨立的文件，不會互相覆蓋。
- `get_group_preferences(line_group_id)` 是給 AI 組 context 用的入口：**查詢條件只有 `line_group_id`**，物理上就不可能撈到別的群組。
- 對應的 Mongo 索引也一併補上：`user_preferences` 原本只有 `(line_user_id, line_group_id)` 的複合唯一索引（`line_user_id` 是前綴），單獨用 `line_group_id` 查整個群組時吃不到這個索引；已在 `ensure_indexes()` 另外加一個 `line_group_id` 單欄索引。

### 已確認條件（summaries）

```python
get_latest_summary(line_group_id) -> dict | None
```

- 查詢條件只有 `line_group_id`，回傳該群組最新一筆 `summaries`（裡面有 `budget_min/max`、`need_type`、`decision_state` 等已確認資訊）。
- 這之前完全沒有讀取函式，`save_summary()` 存了資料但沒人能安全地讀回來 —— 也就是說目前系統還沒有「已確認條件跨群組污染」的 bug，因為根本沒人在讀；但如果之後 AI 流程需要用到，寫的人很可能會手滑漏掉 `line_group_id` 條件。先把這個函式準備好，確保這個讀取路徑一開始就是對的。

### API 查詢紀錄（2026-08-09 更新：改為強制隔離）

> 這節取代原本 2026-08-03 版本的說法。原本 `api_query_cache` 是刻意設計成跨群組共用快取
> （理由是天氣/餐廳這類外部資料本身是公開資訊），`line_group_id` 只是選填的追蹤欄位。
> 後來收到明確需求，要把「不同群組先前各自在 `location_flow.py` / `weather_flow.py`
> 外層手動拼 `line_group_id` 進 query_key」的暫時做法，正式收進 `db.py`，所以改成強制隔離。

`save_api_query_cache()` / `get_api_query_cache()` 現在都多了一個必填的 `line_group_id`
參數，唯一鍵變成 `query_type` + `line_group_id` + `query_key` 三者一起比對：

```python
save_api_query_cache(query_type, line_group_id, query_key, result, query_params=None, ttl_seconds=3600)
get_api_query_cache(query_type, line_group_id, query_key)
```

同樣的 `query_key` 在不同群組現在會各自存成獨立的一筆，群組 A 查過的結果不會被群組 B 讀到。
`ensure_indexes()` 會自動把舊版 `(query_type, query_key)` 的唯一索引砍掉、換成新的三欄位索引。

**這是一個 breaking change**：舊的呼叫方式（不帶 `line_group_id`）會直接噴 `TypeError`，
呼叫端（`location_flow.py`、`weather_flow.py`）要同步更新成新的參數順序，並拿掉外層原本
手動拼接 `line_group_id` 到 query_key 的邏輯（現在不用了，直接把 `line_group_id` 當參數傳）。

天氣資料另外有獨立的 `weather_daily_cache` collection（見
[database_design.md](database_design.md#28-weather_daily_cache)），用於「每天排程同步一次」
的天氣資料，跟這裡的即時查詢快取是分開的兩個東西，不要混用。

---

## 三、既有函式的稽核結果（沒有改動，因為本來就是對的）

```python
get_recent_messages(line_group_id, limit=15)          # 查詢條件有 line_group_id
get_similar_messages(line_group_id, query_embedding, ...)  # line_group_id 必填參數
save_message(line_group_id, ...)                        # 存入時就帶 line_group_id
save_summary(line_group_id, result)                      # 存入時就帶 line_group_id
upsert_group(line_group_id)                              # 用 line_group_id 當 upsert 條件
upsert_member(line_group_id, line_user_id, ...)          # 用 line_group_id 當 upsert 條件
```

這幾支從一開始寫的時候就有把 `line_group_id` 當成必要條件，這次逐一檢查過查詢語句，沒有發現「忘記加條件」或「用了會撈到全部群組」的寫法（例如 `find()` 不帶任何條件）。

---

## 四、不在本次改動範圍：`app.py` 的記憶體對話狀態與 `#reset`

這兩塊是應用邏輯（不是資料庫查詢），但既然另一位組員的需求文件裡也列了，稽核結果一併記錄在這，交給負責 `app.py` 的組員參考：

### 4.1 `conversation_states`（現況：已經是對的）

`app.py` 裡的 `conversation_states` 字典是用 `_get_conversation_key(event)` 的回傳值當 key，而這個 key 對群組訊息來說就是 `group_id`（`_get_push_target_id()` 優先取 `group_id`，其次 `room_id`、`user_id`）。也就是說**現在的程式碼並沒有「所有群組共用同一份 history」的 bug** —— 每個群組本來就有自己獨立的 `ConversationState`。

### 4.2 `#reset` 指令（現況：整個指令都還沒實作）

目前 `app.py` 的 `handle_message()` 完全沒有處理 `#reset` 這種指令文字，所以「`#reset` 只清除目前群組」這件事目前是無從測起的 —— 因為 `#reset` 根本還不存在。

等負責 `app.py` 的組員要實作時，正確寫法是（**只清掉呼叫端自己的 conversation_key，絕對不要 `.clear()`**）：

```python
if user_text.strip() == "#reset":
    with conversation_lock:
        conversation_states.pop(conversation_key, None)   # 只清這個群組
    # 絕對不要寫 conversation_states.clear()，那會把所有群組的狀態一起清掉
    _reply_text(line_bot_api, event.reply_token, "已重設這個群組的對話狀態。")
    return
```

這段要放在 `handle_message()` 裡，抓到 `conversation_key` 之後、送進 AI 分析之前。

---

## 五、驗收測試（依照需求文件的驗收流程）

用兩個不同 LINE 群組手動測試：

1. **群組 A** 傳「我們預算500元，而且有人不吃辣。」
2. **群組 B** 傳「幫我們找附近的餐廳。」
3. 確認群組 B 這次送進 AI 的 `context_text` / `get_group_preferences()` / `get_similar_messages()` 結果裡**不會**出現「500元」「不吃辣」或群組 A 的任何訊息。
4. 在群組 A 輸入 `#reset`，確認群組 B 的對話紀錄（`get_recent_messages("群組B的group_id")`）仍然存在、不受影響。

### 資料庫層可以先獨立驗證的部分（不用等 `#reset` 實作完成）

```python
from db import save_message, get_recent_messages, upsert_user_preference, get_group_preferences

# 群組 A 的訊息與偏好
save_message("group_A", "user_1", "我們預算500元，而且有人不吃辣。", conversation_key="group_A")
upsert_user_preference("group_A", "user_1", "budget", "500元內")
upsert_user_preference("group_A", "user_1", "food_constraints", "不吃辣")

# 群組 B 的訊息
save_message("group_B", "user_2", "幫我們找附近的餐廳。", conversation_key="group_B")

# 驗證：群組 B 讀不到群組 A 的任何東西
assert get_group_preferences("group_B") == []
assert all("500" not in m and "不吃辣" not in m for m in get_recent_messages("group_B"))
assert get_recent_messages("group_A") != []   # 群組 A 自己的資料還在，只是群組 B 看不到
```

---

## 相關檔案索引

- 程式碼：`03_project/code/trip_assistant_bot/db.py`
- 資料庫設計文件：`03_project/database/database_design.md`
- RAG 欄位交接文件：`03_project/database/RAG_HANDOFF.md`
- 需要接續實作 `#reset` 的地方：`03_project/code/trip_assistant_bot/app.py`（`handle_message()`）
