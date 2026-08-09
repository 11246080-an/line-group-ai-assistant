# 資料庫設計文件
**專案：** LINE 群組 AI 旅遊助理  
**資料庫：** MongoDB Atlas（文件型 NoSQL）  
**資料庫名稱：** `linebot`  
**版本：** 1.0  
**日期：** 2026-05-24  

---

## 一、Collection 關係圖（ERD）

> 說明：MongoDB 使用 `line_group_id` / `line_user_id` 做跨 Collection 的邏輯參照。  
> 內嵌文件（members、stops、options 等）以巢狀方式表示。

```mermaid
erDiagram
    groups {
        ObjectId _id PK
        string line_group_id UK
        string group_name
        datetime created_at
        array members "內嵌：line_user_id, display_name, joined_at"
    }

    messages {
        ObjectId _id PK
        string line_group_id FK
        string line_user_id
        string display_name
        string message_text
        datetime sent_at
        array embedding "語意向量，供 RAG 相似度檢索用"
        string topic_hint "主題標記，可為 null"
        string conversation_key "同群組/room 的對話脈絡分組"
        string message_role "user／bot"
    }

    api_query_cache {
        ObjectId _id PK
        string query_type
        string query_key
        object query_params
        object result
        datetime created_at
        datetime updated_at
        datetime expires_at
    }

    summaries {
        ObjectId _id PK
        string line_group_id FK
        datetime window_start
        datetime window_end
        string summary_text
        string destination_city
        string destination_country
        date travel_date_start
        date travel_date_end
        int budget_min
        int budget_max
        string budget_currency
        int people_count
        string decision_state
        string need_type
        bool has_conflict
        string conflict_description
        object scenario_result "內嵌：scenario_code, confidence_score, suggested_reply ..."
    }

    itineraries {
        ObjectId _id PK
        string line_group_id FK
        string itinerary_text
        datetime created_at
        array stops "內嵌：stop_order, stop_name, arrive_time, duration_min ..."
    }

    vote_sessions {
        ObjectId _id PK
        string line_group_id FK
        string title
        string status
        datetime created_at
        datetime closed_at
        array options "內嵌：option_id, option_text, votes[]"
    }

    user_preferences {
        ObjectId _id PK
        string line_user_id FK "UK：與 line_group_id 組合唯一"
        string line_group_id FK "UK：與 line_user_id 組合唯一"
        array preferences "內嵌：type, value, updated_at"
    }

    groups ||--o{ messages : "line_group_id"
    groups ||--o{ summaries : "line_group_id"
    groups ||--o{ itineraries : "line_group_id"
    groups ||--o{ vote_sessions : "line_group_id"
    groups ||--o{ user_preferences : "line_group_id"
```

---

## 二、Collection Schema（資料字典）

### 2.1 groups

儲存 LINE 群組基本資訊，成員清單直接內嵌。

| 欄位 | 型態 | 必填 | 說明 |
|------|------|------|------|
| `_id` | ObjectId | 是 | MongoDB 自動產生主鍵 |
| `line_group_id` | string | 是 | LINE 群組 ID（唯一，如 `C1234...`） |
| `group_name` | string | 否 | 群組名稱 |
| `created_at` | datetime | 是 | 首次記錄時間（UTC） |
| `members` | array | 是 | 成員清單（見下方內嵌結構） |

**members 內嵌結構：**

| 欄位 | 型態 | 說明 |
|------|------|------|
| `line_user_id` | string | LINE 使用者 ID |
| `display_name` | string | 顯示名稱 |
| `joined_at` | datetime | 加入時間（UTC） |

---

### 2.2 messages

儲存群組內每一筆使用者訊息，為 AI 分析提供對話歷史。

| 欄位 | 型態 | 必填 | 說明 |
|------|------|------|------|
| `_id` | ObjectId | 是 | 自動產生主鍵 |
| `line_group_id` | string | 是 | 所屬群組 ID |
| `line_user_id` | string | 是 | 發送者 LINE ID |
| `display_name` | string | 否 | 發送者顯示名稱 |
| `message_text` | string | 是 | 訊息內容 |
| `sent_at` | datetime | 是 | 發送時間（UTC） |
| `embedding` | array\<float\> | 否 | 訊息語意向量，由 AI 後端計算後寫入；尚未計算時為 `null`，供 RAG 相似度檢索用 |
| `topic_hint` | string | 否 | 主題標記，預設 `null` / 空字串，之後可放話題分類結果 |
| `conversation_key` | string | 是 | 同一群組 / room 的對話脈絡分組 key；未指定時預設等於 `line_group_id` |
| `message_role` | string | 是 | `user` 或 `bot`，方便過濾是誰發的訊息 |

---

### 2.3 summaries

儲存每次 AI 分析的結論與情境判斷結果，scenario_result 直接內嵌。

| 欄位 | 型態 | 必填 | 說明 |
|------|------|------|------|
| `_id` | ObjectId | 是 | 自動產生主鍵 |
| `line_group_id` | string | 是 | 所屬群組 ID |
| `window_start` | datetime | 是 | 分析時間視窗起點 |
| `window_end` | datetime | 是 | 分析時間視窗終點 |
| `summary_text` | string | 否 | 對話摘要文字 |
| `destination_city` | string | 否 | 目的地城市 |
| `destination_country` | string | 否 | 目的地國家 |
| `travel_date_start` | date | 否 | 旅遊開始日期 |
| `travel_date_end` | date | 否 | 旅遊結束日期 |
| `budget_min` | int | 否 | 最低預算 |
| `budget_max` | int | 否 | 最高預算 |
| `budget_currency` | string | 否 | 幣別（預設 `TWD`） |
| `people_count` | int | 否 | 出遊人數 |
| `decision_state` | string | 否 | `討論中` / `確認中` / `已決定` |
| `need_type` | string | 否 | AI 判斷的需求類型 |
| `has_conflict` | bool | 是 | 是否有意見衝突 |
| `conflict_description` | string | 否 | 衝突描述 |
| `scenario_result` | object | 否 | AI 情境判斷結果（見下方） |

**scenario_result 內嵌結構：**

| 欄位 | 型態 | 說明 |
|------|------|------|
| `scenario_code` | string | 情境代碼（如 `劇本四`） |
| `scenario_name` | string | 情境名稱 |
| `should_intervene` | bool | 是否介入 |
| `intervention_type` | string | `顯性介入` / `隱性介入` / `不介入` |
| `confidence_score` | decimal | 信心分數（0.0 ~ 1.0） |
| `suggested_reply` | string | AI 建議回覆文字 |

---

### 2.4 itineraries

儲存 AI 產生的旅遊行程，站點清單直接內嵌。

| 欄位 | 型態 | 必填 | 說明 |
|------|------|------|------|
| `_id` | ObjectId | 是 | 自動產生主鍵 |
| `line_group_id` | string | 是 | 所屬群組 ID |
| `itinerary_text` | string | 否 | 行程描述文字 |
| `created_at` | datetime | 是 | 建立時間（UTC） |
| `stops` | array | 是 | 行程站點清單（見下方） |

**stops 內嵌結構：**

| 欄位 | 型態 | 說明 |
|------|------|------|
| `stop_order` | int | 站點順序（從 1 開始） |
| `stop_name` | string | 地點名稱 |
| `stop_address` | string | 地址 |
| `arrive_time` | string | 預計到達時間（`HH:MM`） |
| `duration_min` | int | 預計停留分鐘數 |
| `category` | string | 類別（`景點` / `餐廳` / `交通`） |

---

### 2.5 vote_sessions

儲存投票主題，選項與投票紀錄直接內嵌。

| 欄位 | 型態 | 必填 | 說明 |
|------|------|------|------|
| `_id` | ObjectId | 是 | 自動產生主鍵 |
| `line_group_id` | string | 是 | 所屬群組 ID |
| `title` | string | 是 | 投票標題 |
| `status` | string | 是 | `進行中` / `已結束` |
| `created_at` | datetime | 是 | 建立時間（UTC） |
| `closed_at` | datetime | 否 | 結束時間（UTC） |
| `options` | array | 是 | 投票選項（見下方） |

**options 內嵌結構：**

| 欄位 | 型態 | 說明 |
|------|------|------|
| `option_id` | int | 選項編號 |
| `option_text` | string | 選項文字 |
| `votes` | array | 已投票的使用者清單（`line_user_id`、`voted_at`） |

---

### 2.6 user_preferences

儲存使用者在特定群組的個人偏好，多筆偏好合併為陣列。

| 欄位 | 型態 | 必填 | 說明 |
|------|------|------|------|
| `_id` | ObjectId | 是 | 自動產生主鍵 |
| `line_user_id` | string | 是 | LINE 使用者 ID |
| `line_group_id` | string | 是 | 所屬群組 ID |
| `preferences` | array | 是 | 偏好清單（見下方） |

> `line_user_id` + `line_group_id` 組合唯一。另有一個 `line_group_id` 單欄索引，
> 支援「整個群組」的偏好查詢（見 `get_group_preferences()`）。

**preferences 內嵌結構：**

| 欄位 | 型態 | 說明 |
|------|------|------|
| `type` | string | 偏好類型（如 `飲食`、`交通`、`住宿`） |
| `value` | string | 偏好值（如 `不吃辣`、`不開車`） |
| `updated_at` | datetime | 最後更新時間（UTC） |

**存取函式（`db.py`，皆嚴格以 `line_group_id` 隔離，不會跨群組讀寫）：**

| 函式 | 說明 |
|------|------|
| `upsert_user_preference(line_group_id, line_user_id, preference_type, preference_value)` | 新增/更新單一使用者在該群組的一筆偏好 |
| `get_user_preferences(line_group_id, line_user_id)` | 取得單一使用者在該群組的偏好清單 |
| `get_group_preferences(line_group_id)` | 取得該群組所有成員合併後的偏好清單，給 AI 組 context 用 |

`summaries` collection 對應「已確認條件」的讀取，見 `get_latest_summary(line_group_id)`（同樣只以 `line_group_id` 查詢）。

---

### 2.7 api_query_cache

儲存外部 API（餐廳、路線等即時查詢）的回傳結果暫存，避免短時間內重複打外部 API。以
`query_type` + `line_group_id` + `query_key` 三者一起唯一識別一筆快取，重複查詢時直接 upsert。

| 欄位 | 型態 | 必填 | 說明 |
|------|------|------|------|
| `_id` | ObjectId | 是 | 自動產生主鍵 |
| `query_type` | string | 是 | 查詢類型（如 `restaurant` / `movie`） |
| `line_group_id` | string | 是 | 這次查詢所屬的 LINE 群組 ID，與 `query_type` + `query_key` 組合唯一 |
| `query_key` | string | 是 | 正規化後的查詢關鍵字 / 條件字串 |
| `query_params` | object | 否 | 原始查詢條件，方便除錯或重新查詢 |
| `result` | object | 否 | 外部 API 回傳結果（原樣存放） |
| `created_at` | datetime | 是 | 建立時間（UTC） |
| `updated_at` | datetime | 是 | 最後更新時間（UTC） |
| `expires_at` | datetime | 是 | 到期時間（UTC），搭配 TTL 索引到期自動清除 |

> `line_group_id` 是快取鍵的一部分：同樣的 `query_key` 在不同群組會各自存成獨立的一筆，
> 群組 A 查過的結果不會被群組 B 讀到，呼叫端不用再自己手動把 `line_group_id` 拼進
> `query_key` 字串。對應存取函式：`save_api_query_cache()` / `get_api_query_cache()`。

---

### 2.8 weather_daily_cache

儲存「每日排程同步」的天氣資料，取代使用者一問就即時打中央氣象署 API 的做法。以
`provider` + `county_name` + `source_date` + `forecast_type` 唯一識別一筆資料。

| 欄位 | 型態 | 必填 | 說明 |
|------|------|------|------|
| `_id` | ObjectId | 是 | 自動產生主鍵 |
| `provider` | string | 是 | 資料來源，例如 `cwa_weather` |
| `county_name` | string | 是 | 縣市名稱，例如 `臺北市`、`宜蘭縣` |
| `source_date` | string | 是 | 這筆資料是同步哪一天的，建議統一用 `YYYY-MM-DD` |
| `forecast_type` | string | 是 | 預報類型，先固定 `36h` |
| `raw_data` | object | 是 | 中央氣象署回傳的原始 JSON，不在資料庫層解析，後端自己挑今天/明天的內容 |
| `updated_at` | datetime | 是 | 實際更新時間（UTC） |
| `expires_at` | datetime | 否 | 到期時間（UTC），選填；有帶 `ttl_seconds` 才會設定，搭配 TTL 索引清舊資料用 |

對應存取函式：`save_weather_daily_cache()` / `get_weather_daily_cache()`（`db.py`）。

---

## 三、索引設計

| Collection | 索引欄位 | 類型 | 目的 |
|---|---|---|---|
| `groups` | `line_group_id` | 唯一索引 | 快速查找群組、防重複建立 |
| `messages` | `line_group_id` + `sent_at DESC` | 複合索引 | 取得群組最近 N 筆訊息 |
| `messages` | `line_user_id` | 單欄索引 | 查詢特定使用者訊息 |
| `messages` | `conversation_key` + `sent_at DESC` | 複合索引 | 依對話脈絡取訊息 |
| `summaries` | `line_group_id` + `window_start DESC` | 複合索引 | 取得群組最新分析結果 |
| `itineraries` | `line_group_id` + `created_at DESC` | 複合索引 | 取得群組最新行程 |
| `vote_sessions` | `line_group_id` + `status` | 複合索引 | 查詢進行中的投票 |
| `user_preferences` | `line_user_id` + `line_group_id` | 唯一複合索引 | 快速查找偏好、防重複 |
| `user_preferences` | `line_group_id` | 單欄索引 | 取得「整個群組」的偏好清單（`get_group_preferences()`） |
| `api_query_cache` | `query_type` + `line_group_id` + `query_key` | 唯一複合索引 | 快速查找快取、防重複、支援 upsert，並以群組隔離 |
| `api_query_cache` | `expires_at` | TTL 索引（`expireAfterSeconds=0`） | 到期自動清除快取 |
| `weather_daily_cache` | `provider` + `county_name` + `source_date` + `forecast_type` | 唯一複合索引 | 快速查找當日同步資料、防重複、支援 upsert |
| `weather_daily_cache` | `expires_at` | TTL 索引（`expireAfterSeconds=0`） | 選填，若有設定到期時間就自動清除 |

> **注意（重大變更）：** `api_query_cache` 的唯一鍵從舊版的 `query_type` + `query_key`
> 改成加入 `line_group_id`。`ensure_indexes()` 會自動偵測並砍掉舊索引再建新的，
> 但 `save_api_query_cache()` / `get_api_query_cache()` 的函式簽名也跟著改了
> （多了必填的 `line_group_id` 參數），呼叫端的程式碼需要一併更新。

> `messages.embedding` 目前用 Python 端 brute-force 算 cosine similarity 做語意檢索（見 `get_similar_messages()`），未建立 Atlas Vector Search 索引；等訊息量變大或 cluster 有支援時可再補上 `$vectorSearch` 索引。

---

## 四、設計決策說明

### 為何從 10 個 SQL 表縮減為 6 個 Collection？

| 決策 | 原因 |
|------|------|
| `users` 不獨立存 | LINE 使用者資訊分散在各 Collection，查詢不需要單獨一張表 |
| `group_members` 內嵌進 `groups` | 群組與成員永遠一起讀，內嵌減少查詢次數 |
| `scenario_results` 內嵌進 `summaries` | 每次分析只有一個最終情境結果，一對一關係適合內嵌 |
| `itinerary_stops` 內嵌進 `itineraries` | 站點不會脫離行程單獨存取，內嵌符合存取模式 |
| `vote_options` + `votes` 內嵌進 `vote_sessions` | 投票結果需要原子性操作，內嵌確保一致性 |

### 時間統一使用 UTC
所有 `datetime` 欄位儲存 UTC 時間，顯示時由前端或應用層轉換為 UTC+8（台灣時間）。
