# RAG / 外部查詢快取 — 資料庫層交接說明

**交接對象：** 負責 AI decision core / 後端流程的同學
**異動檔案：** `03_project/code/trip_assistant_bot/db.py`
**對應設計文件：** `03_project/database/database_design.md`（已同步更新）
**日期：** 2026-07-19

---

## 一、這次做了什麼、為什麼

背景：群組切換話題要改用 RAG（不只抓最近 N 句，還要抓「同群組裡語意最相關的舊對話」）。
資料庫這邊先把**欄位**和**查詢接口**準備好，實際的 AI 判斷邏輯（embedding 計算、要不要用 RAG、怎麼組 prompt）留給 AI decision core 接。

三件事：
1. 新增 `api_query_cache` collection，存外部 API 查詢結果暫存
2. `messages` collection 補 4 個 RAG 欄位
3. 提供 `get_similar_messages()` 查詢接口，做同群組語意相似檢索

全部都在 `db.py` 這一個檔案裡，沒有動 `app.py` 的既有邏輯（新參數都有預設值，舊的呼叫方式不用改就能繼續跑）。

---

## 二、`messages` collection 新欄位

| 欄位 | 型態 | 預設值 | 說明 |
|---|---|---|---|
| `embedding` | `list[float] \| None` | `None` | 語意向量。**資料庫層不會自動計算**，要由呼叫端（AI decision core）算好傳進來 |
| `topic_hint` | `str \| None` | `None` | 主題標記，先留空，之後可以放分類結果（例如 `"訂房"`、`"投票"`） |
| `conversation_key` | `str` | 沒給時 = `line_group_id` | 對話脈絡分組 key。目前 `app.py` 裡已經有 `_get_conversation_key(event)` 這個函式（用 group_id/room_id/user_id 決定），建議沿用同一套邏輯傳進來 |
| `message_role` | `str` | `"user"` | 傳 `"user"` 或 `"bot"`。目前系統**沒有**把 bot 的回覆存進 `messages`，如果要讓 bot 說過的話也能被 RAG 檢索到，要自己在發送回覆的地方多呼叫一次 `save_message(..., message_role="bot")` |

### `save_message()` 新的完整簽名

```python
def save_message(
    line_group_id: str,
    line_user_id: str,
    message_text: str,
    display_name: str = "",
    conversation_key: str = "",
    message_role: str = "user",
    embedding: list[float] | None = None,
    topic_hint: str | None = None,
) -> None
```

**呼叫範例（存使用者訊息 + embedding）：**

```python
from db import save_message

save_message(
    line_group_id=line_group_id,
    line_user_id=line_user_id,
    message_text=user_text,
    display_name=display_name,
    conversation_key=conversation_key,   # 沿用 app.py 的 _get_conversation_key()
    message_role="user",
    embedding=embedding_vector,          # 自己算好的向量，見下方「待補」
)
```

**呼叫範例（存 bot 回覆，目前 app.py 沒接，如需要要自己加）：**

```python
save_message(
    line_group_id=line_group_id,
    line_user_id="bot",              # 或固定用一個 bot 的識別字串
    message_text=suggested_reply,
    conversation_key=conversation_key,
    message_role="bot",
)
```

> 注意：現有 `app.py` 的呼叫 `save_message(line_group_id, line_user_id, user_text)` 完全相容，不改也能跑，只是不會存到 embedding／conversation_key 會自動 fallback 成 line_group_id、message_role 固定是 "user"。**要真的用上 RAG，就必須改成上面傳滿參數的寫法。**

---

## 三、語意相似檢索 — `get_similar_messages()`

```python
def get_similar_messages(
    line_group_id: str,
    query_embedding: list[float],
    exclude_message_id: Any = None,
    limit: int = 5,
    min_score: float = 0.0,
) -> list[dict]
```

### 參數說明

| 參數 | 說明 |
|---|---|
| `line_group_id` | 限定只在同一個群組內找 |
| `query_embedding` | 目前這則訊息的向量，用它去跟歷史訊息比對 |
| `exclude_message_id` | 傳目前這筆訊息的 `_id`，排除自己（避免「和自己最相似」變成第一名） |
| `limit` | 回傳幾筆，預設 5 |
| `min_score` | 相似度門檻（0~1），低於這個分數的不會回傳，預設 0（不篩選） |

### 回傳格式

`list[dict]`，依相似度**高到低**排序，每筆長這樣：

```python
{
    "_id": ObjectId(...),
    "line_group_id": "...",
    "line_user_id": "...",
    "display_name": "...",
    "message_text": "...",
    "message_role": "user",
    "topic_hint": None,
    "conversation_key": "...",
    "embedding": [...],
    "sent_at": datetime(...),
    "similarity_score": 0.83,   # 新增的欄位，代表跟 query_embedding 的 cosine similarity
}
```

### 呼叫範例

```python
from db import save_message, get_similar_messages

# 1. 先把這則新訊息存起來，save_message() 會回傳新增文件的 _id
current_message_id = save_message(
    line_group_id=line_group_id,
    line_user_id=line_user_id,
    message_text=user_text,
    conversation_key=conversation_key,
    embedding=embedding_vector,
)

# 2. 用同一個 embedding 查相關歷史訊息，排除自己
similar_msgs = get_similar_messages(
    line_group_id=line_group_id,
    query_embedding=embedding_vector,
    exclude_message_id=current_message_id,
    limit=5,
)

# 3. 組 context_text
context_text = "\n".join(f"{m['display_name']}: {m['message_text']}" for m in similar_msgs)
```

### 重要限制（要跟你說清楚）

- **目前是 Python 端 brute-force 算 cosine similarity**，不是 MongoDB Atlas Vector Search 的 `$vectorSearch`。做法是：把該群組所有「有 embedding」的訊息全部撈出來，在應用程式記憶體裡逐筆算相似度、排序、取前 N 筆。
- 這樣做的原因：Atlas Vector Search 需要在 Atlas 後台額外建向量索引，且通常要求 M10 以上的付費 cluster，現階段先求「功能可用、介面穩定」。
- 影響：如果單一群組的訊息量變得非常大（例如破萬筆），這個函式的延遲會變高，因為每次查詢都要把全部候選訊息載進記憶體算一次。**現階段（一般群組聊天量）不會有感**，但如果之後量大了，要優化的話有兩個方向：
  1. 加一個時間範圍限制（例如只在最近 N 天內的訊息中找），這個可以之後在 `get_similar_messages` 裡加 `sent_at` 條件，不需要動呼叫端的介面。
  2. 換成 Atlas `$vectorSearch` aggregation（需要先在 Atlas 開向量索引），函式的輸入輸出介面不需要變，只需要換內部實作。
- 目前**沒有**對 `embedding` 欄位建索引（因為 brute-force 用不到向量索引，一般 B-tree 索引對高維向量也沒意義）。

---

## 四、外部 API 查詢結果暫存 — `api_query_cache`

新 collection，欄位：

| 欄位 | 型態 | 說明 |
|---|---|---|
| `query_type` | string | 查詢類型，例如 `"restaurant"` / `"weather"` / `"movie"` |
| `query_key` | string | 正規化後的查詢關鍵字或條件字串，**由呼叫端自己決定怎麼組**（例如 `"台中_火鍋"`，或把查詢條件 dict 做 `json.dumps(sort_keys=True)` 後當 key） |
| `query_params` | object | 原始查詢條件，方便除錯或重查，非必填 |
| `result` | object | API 回傳結果，原樣存放 |
| `created_at` | datetime | 第一次建立時間 |
| `updated_at` | datetime | 最後更新時間 |
| `expires_at` | datetime | 到期時間，過期後 `get_api_query_cache` 回傳 `None`，MongoDB 也會用 TTL 索引自動把這筆文件刪掉 |

### 兩支函式

```python
def save_api_query_cache(
    query_type: str,
    query_key: str,
    result: Any,
    query_params: dict | None = None,
    ttl_seconds: int = 3600,   # 預設快取 1 小時
) -> None

def get_api_query_cache(query_type: str, query_key: str) -> Any | None
```

### 標準使用模式（快取判斷邏輯要自己接）

```python
from db import get_api_query_cache, save_api_query_cache

def query_restaurant(city: str, food_type: str):
    query_key = f"{city}_{food_type}"
    cached = get_api_query_cache("restaurant", query_key)
    if cached is not None:
        return cached  # 命中快取，不用打外部 API

    result = call_external_restaurant_api(city, food_type)  # 真的去打外部 API

    save_api_query_cache(
        "restaurant",
        query_key,
        result,
        query_params={"city": city, "food_type": food_type},
        ttl_seconds=1800,  # 想存多久自己決定
    )
    return result
```

`db.py` 只提供存取，「查詢前先看快取、查完後存快取」這個判斷流程要自己寫在呼叫外部 API 的地方。

---

## 五、需要後端同學自己補的部分（資料庫層沒做）

1. **Embedding 計算**：`db.py` 完全沒有呼叫任何 embedding API。要在 AI decision core 裡自己呼叫（例如 OpenAI `embeddings.create`），拿到向量後傳進 `save_message()` 和 `get_similar_messages()`。
   - 專案裡已經有 `OPENAI_API_KEY` 這個環境變數（`.env` 裡），可以直接沿用同一組 key。
   - 如果要用固定 model，建議另外加一個環境變數，例如 `OPENAI_EMBEDDING_MODEL=text-embedding-3-small`，跟現有 `OPENAI_MODEL` 的命名風格一致。

2. **把 bot 回覆也存進 `messages`（如果要用到）**：目前 `app.py` 只有 `save_message(...)` 存使用者訊息，bot 送出回覆的地方（`_reply_text` / `_push_text` 附近）沒有存進資料庫。如果 RAG 需要參考 bot 說過的話，要自己在那幾個地方加一次 `save_message(..., message_role="bot")`。

3. **外部 API 查詢的快取判斷邏輯**：要自己在呼叫外部 API 的程式碼裡加上「先查快取、沒有才真的呼叫、呼叫完存快取」的流程（範例見上面第四節）。

4. **`conversation_key` 怎麼決定**：資料庫層只提供欄位和預設值（fallback 成 `line_group_id`），實際「怎樣算切換話題、要不要開新的 conversation_key」的邏輯要 AI 那邊決定。建議先沿用 `app.py` 現有的 `_get_conversation_key(event)`，如果之後要做「同群組但話題切換就換一個 key」，再另外設計切換規則。

5. **`topic_hint` 怎麼標記**：欄位已經有了，但目前沒有任何程式碼會去填它，要 AI decision core 判斷完主題後自己 `update_one` 寫回去，或是在 `save_message` 時就一起傳進來。

---

## 六、部署 / 索引注意事項

- 新的索引（`messages.conversation_key`、`api_query_cache` 的唯一索引和 TTL 索引）定義在 `ensure_indexes()` 裡，`app.py` 啟動時本來就會呼叫這個函式，**正常重啟一次服務就會自動建好**，不用去 Atlas 後台手動建。
- `api_query_cache` 的 TTL 索引（`expires_at`, `expireAfterSeconds=0`）：MongoDB 的 TTL 背景清理程序大約**每 60 秒**跑一次，所以過期後不是「立即」刪除，會有幾十秒的延遲，`get_api_query_cache()` 本身有做過期時間比對，所以就算文件還沒被刪掉，過期的話也一樣會回傳 `None`，不會拿到舊資料。

---

## 七、驗收建議

如果要驗證資料庫層有正常運作，可以用這幾個手動測試（不需要真的接 AI）：

```python
from db import save_message, get_similar_messages, save_api_query_cache, get_api_query_cache

# 1. 測試 RAG 欄位存取
save_message("test_group", "test_user", "我們要不要去台中玩", conversation_key="test_group", embedding=[1.0, 0.0, 0.0])
save_message("test_group", "test_user", "台中哪裡好吃", conversation_key="test_group", embedding=[0.9, 0.1, 0.0])
save_message("test_group", "test_user", "今天天氣真好", conversation_key="test_group", embedding=[0.0, 0.0, 1.0])

results = get_similar_messages("test_group", query_embedding=[1.0, 0.0, 0.0], limit=2)
# 預期：前兩筆是「去台中玩」「台中哪裡好吃」，「天氣真好」排最後或被篩掉

# 2. 測試 API 快取
save_api_query_cache("weather", "台中", {"temp": 28, "condition": "晴"}, ttl_seconds=5)
print(get_api_query_cache("weather", "台中"))  # 應該拿到剛存的結果
import time; time.sleep(6)
print(get_api_query_cache("weather", "台中"))  # 應該回傳 None（過期）
```

---

## 相關檔案索引

- 程式碼：`03_project/code/trip_assistant_bot/db.py`
- 資料庫設計文件（欄位字典、ERD、索引清單）：`03_project/database/database_design.md`
- 目前串接這些函式的地方：`03_project/code/trip_assistant_bot/app.py`（`handle_message()`）
