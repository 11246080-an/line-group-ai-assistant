# 觀光署開放資料匯入 — 資料庫層交接

**對應規格：** `觀光署開放資料匯入交接文件.docx`
**異動檔案：** `03_project/code/trip_assistant_bot/db.py`、新增 `03_project/code/trip_assistant_bot/import_tourism_data.py`
**日期：** 2026-08-16（2026-08-17 更新：真實資料已匯入）

---

## 一、真實資料已經匯入完成（2026-08-17）

原本這個 repo 沒有設定 GitHub remote，拿到你給的網址
（`https://github.com/11246080-an/line-group-ai-assistant.git`）之後：

1. 加了一個叫 `tourism-data` 的 remote 並 `fetch`（用 `--depth 1` 淺層抓取，避免抓整個歷史）
2. 確認 `Attraction-json/` `Event-json/` 在 `main` 分支的 `03_project/code/trip_assistant_bot/` 底下
3. 用 `git checkout tourism-data/main -- <路徑>` 把這兩個資料夾（含 `AttractionList.json`、`EventList.json`，以及這階段先不處理的 `AttractionFeeList.json`、`AttractionServiceTimeList.json`、schema/manifest csv）拉進這個工作目錄，路徑跟匯入 script 的預設路徑一致
4. 對照 `schema-AttractionList.csv` / `schema-EventList.csv` 確認欄位名稱、對照 JSON 實際內容確認巢狀結構，跟原本設計時的假設完全一致，不用改 `transform_attraction()` / `transform_event()`
5. 執行 `python import_tourism_data.py`，**真的把資料匯進 Atlas 了**：

   | Collection | 結果 |
   |---|---|
   | `tourism_attractions` | 匯入 **6,087** 筆，`inserted=6087, updated=0, skipped=0` |
   | `tourism_events` | 匯入 **946** 筆，`inserted=946, updated=0, skipped=0` |

6. 用 `get_tourism_attractions(city="宜蘭縣")`、`get_tourism_events(city="臺南市", keyword="美食")`、`get_tourism_attractions(keyword="溫泉")` 這幾種組合對真實資料查過，中文名稱、地址、圖片 URL 都正確，結果例如：
   - 宜蘭縣景點：一米特米食點心觀光工廠（蘇澳鎮）、七星嶺步道（蘇澳鎮）...
   - keyword=溫泉 的景點橫跨高雄市、新北市都查得到，代表 city + keyword 的篩選邏輯是分開獨立運作的

**這兩個資料夾目前已經 `git add` 到這個 repo 的暫存區（因為是用 `git checkout` 拉進來的），但我沒有幫你 commit** —— 照慣例交給你確認後自己 commit，或跟這次 `db.py` 的改動一起 commit。

`tourism-data` 這個 remote 我留著沒刪，之後如果那個 GitHub repo 有更新資料，可以直接 `git fetch tourism-data` 再重新 checkout 覆蓋、重跑一次匯入（`save_tourism_attractions` / `save_tourism_events` 是 upsert，重跑不會產生重複資料）。

---

## 二、已經完成並驗證過的部分

### 1. Collection 與索引（已經在 Atlas 上建好）

`ensure_indexes()` 已經加入 `tourism_attractions` / `tourism_events` 的索引並跑到 Atlas 上：

- `tourism_attractions`：`attraction_id`（唯一）、`name`、`city`、`town`、`source_update_time`
- `tourism_events`：`event_id`（唯一）、`name`、`city`、`town`、`start_time`、`end_time`、`event_status`

規格文件提到的地理查詢索引這版沒有做（文件本身也說「不是必要條件」）——`latitude`/`longitude` 目前是原始數值，沒有轉成 GeoJSON 格式，要做 `2dsphere` 索引的話需要先決定座標欄位要不要轉格式。

### 2. `db.py` 新增的 function

```python
save_tourism_attractions(items: list[dict]) -> dict   # {"inserted", "updated", "skipped", "total"}
save_tourism_events(items: list[dict]) -> dict
get_tourism_attractions(city=None, keyword=None, limit=20) -> list[dict]
get_tourism_events(city=None, keyword=None, limit=20) -> list[dict]
get_tourism_attraction_by_id(attraction_id: str) -> dict | None
get_tourism_event_by_id(event_id: str) -> dict | None
```

- `save_*` 依 `attraction_id` / `event_id` 用 `bulk_write` 分批 upsert（每批 500 筆），比逐筆 `update_one` 快很多，適合一次匯入上千筆的開放資料。多回傳一個 `skipped`（缺唯一鍵被跳過的筆數），方便你知道資料乾不乾淨。
- `get_*` 的 `keyword` 是用 `$regex` 對 `name` / `description` 做子字串比對（不是 MongoDB 的 `$text` 全文索引）——因為 `$text` 預設是空白斷詞的英文全文檢索，對沒有空白斷詞的中文地名/描述效果不好，`$regex` 子字串比對雖然吃不到索引，但比較符合「關鍵字搜尋」的直覺。已經對 `keyword` 做 `re.escape()`，不會因為使用者輸入特殊符號（`(`、`*` 之類）而讓查詢噴例外。
- `get_tourism_events()` **沒有做「排除已過期活動」的過濾**——規格裡這項本來就是選配、非硬性要求。原因是 `end_time` 目前原樣存放來源 JSON 的字串值，還沒有拿到真實資料前無法確認日期格式是否穩定一致（時區、有沒有 `+08:00` 之類），貿然用字串比較篩選反而可能誤篩掉合法的未來活動。等真的匯入資料、確認 `end_time` 格式後再補這個過濾條件比較安全，補的時候只需要改 `get_tourism_events()` 內部實作，函式簽名不用變。

### 3. 匯入 script：`import_tourism_data.py`

```bash
python import_tourism_data.py
python import_tourism_data.py --attraction-file path/to/AttractionList.json --event-file path/to/EventList.json
python import_tourism_data.py --skip-events        # 只匯景點
python import_tourism_data.py --skip-attractions   # 只匯活動
```

預設會找 `Attraction-json/AttractionList.json` 和 `Event-json/EventList.json`（相對於 script 自己所在的資料夾，也就是 `03_project/code/trip_assistant_bot/`）。執行時會先呼叫 `ensure_indexes()`，再依序匯入景點、活動，印出 `{"inserted", "updated", "skipped", "total"}` 摘要。

欄位轉換（`transform_attraction()` / `transform_event()`）完全照文件的「對應來源欄位」表格實作，包含巢狀欄位（`PostalAddress.City` 這種點號路徑用 `_get_nested()` 處理）、`Images[0].URL` 沒有圖片時存空字串、最外層的 `UpdateTime`/`UpdateInterval`/`Language`/`ProviderID` 對應到 `dataset_update_time`/`dataset_update_interval`/`language`/`provider_id`。

### 4. 驗證方式

先用手刻的假 JSON（結構照文件描述的 TDX 格式，涵蓋巢狀欄位展開、`image_url` 空圖片情境、`dataset_update_time` vs `source_update_time`、`raw_payload` 保留、缺 `attraction_id` 的資料會被安全 `skip`、索引唯一性等 25 項檢查）驗證過邏輯正確、測試資料清乾淨之後，再對拿到的真實資料重跑一次同樣的驗證（見第一節），確認邏輯在真實資料上一樣正確：中文名稱/地址/描述完整、`city` 與 `keyword` 篩選各自獨立運作、重複匯入正確變成 `updated` 而不是產生重複資料。

---

## 三、之後要重新匯入或更新資料時

```bash
cd 03_project/code/trip_assistant_bot
git fetch tourism-data        # 如果 GitHub 上的資料有更新
git checkout tourism-data/main -- Attraction-json Event-json
python import_tourism_data.py
```

`save_tourism_attractions` / `save_tourism_events` 是 upsert，重跑不會產生重複資料，`fetched_at` 會更新成最新匯入時間。簡單確認資料的方式：

```python
from db import get_tourism_attractions, get_tourism_events
print(get_tourism_attractions(city="宜蘭縣", limit=5))
print(get_tourism_events(city="臺南市", limit=5))
```

---

## 四、補充資料：營運時間（2026-09-10 完成，依「資料庫交接說明.docx」第 1~4 節）

`tourism_attraction_service_times`（`AttractionServiceTimeList.json`，77 筆）沒有再改版，維持
2026-09-10 版本：`attraction_id` / `attraction_name` / `service_time`（來源 `ServiceTimes` 陣列，
含 `ServiceDays`/`StartTime`/`EndTime` 等）/ `source_update_time` / `imported_at` / `raw_data`。
索引：`attraction_id` unique、`attraction_name`、`source_update_time`。函式：
`save_tourism_attraction_service_times()` / `get_tourism_attraction_service_times(attraction_id)`。

---

## 五、票價：改版為「行程預算分析」用的 schema（2026-09-15，依「景點票價資料匯入與行程預算分析交接.docx」）

`tourism_attraction_fees` 這次**整個改版**，不是新增——舊版（2026-09-10 匯入的 38 筆）欄位是
`attraction_name` / `fees`（原始大寫 key）/ `imported_at` / `raw_data`，這次照新規格換成下面這套，
專門給「行程預算分析」用：

| 欄位 | 型態 | 說明 |
|---|---|---|
| `attraction_id` | string | 唯一鍵，對應 `tourism_attractions.attraction_id` |
| `name` | string | 景點名稱（**舊版叫 `attraction_name`**） |
| `fees` | array | 票種明細，**key 全部小寫**：`name` / `price` / `description` / `url` |
| `min_price` / `max_price` | number \| null | 票種裡的最低 / 最高價格 |
| `default_price` | number \| null | 預估用的價格：優先抓「全票」，沒有全票取最高價；完全沒有價格時是 `null` |
| `is_free` | bool | 所有票種價格都是 0 才是 `true` |
| `source_update_time` | string | 單筆景點票價更新時間 |
| `dataset_update_time` / `dataset_update_interval` / `language` / `provider_id` | — | 整份資料集的中繼資訊（**舊版沒有這幾個欄位**） |
| `fetched_at` | datetime | 匯入時間（**舊版叫 `imported_at`**） |
| `raw_payload` | dict | 原始資料（**舊版叫 `raw_data`**） |

### 這次動了什麼

1. **`_bulk_upsert_by_key()` 改成整筆替換（`ReplaceOne` + upsert），不再是 `$set` 合併。**
   這是這次順手修的一個共用邏輯問題：舊的 `$set` 合併方式，遇到欄位改名（`attraction_name`→`name`
   這種）只會疊加新欄位，舊欄位永遠留在文件裡洗不掉。改成整筆替換後，重新匯入的文件形狀會跟目前的
   transform function 輸出完全一致。這個共用函式也被 `tourism_attractions`、`tourism_events`、
   `tourism_attraction_service_times` 用，所以**這四個 collection 都受影響**——但因為它們都是「定期
   整批重新匯入外部資料、沒有其他程式局部更新」的性質，整筆替換對這四個都是更安全的行為，不是只為了
   這次遷移硬改。
2. **函式改名**：`get_tourism_attraction_fees(attraction_id)` 改名成 `get_tourism_attraction_fee_by_id(attraction_id)`（跟規格對齊），新增批次查詢 `get_tourism_attraction_fees_by_ids(attraction_ids: list[str])` 給行程預算分析用。**這是 breaking rename**——目前沒有其他程式呼叫舊名字，所以直接改，沒有做相容別名。
3. 索引：`attraction_name_1` 砍掉，換成 `name_1`，新增 `is_free_1`（`attraction_id_1` unique 不變）。
4. **`import_tourism_data.py`**：`transform_attraction_fee()` 整個重寫，含 `_number_or_none()`（把
   `Price` 轉成 float，非數字/布林一律 `None`，不是 0）跟 `_default_ticket_price()`（全票優先，沒有
   全票取最高價，完全沒價格回 `None`）。CLI 參數維持原本的 `--fee-file` / `--skip-fees`（規格建議的
   名字是 `--attraction-fee-file` / `--skip-attraction-fees`，功能一樣，只是命名不同，沒有特別改）。
5. 重新跑過 `python import_tourism_data.py --skip-attractions --skip-events --skip-service-times`，
   結果 `{'inserted': 0, 'updated': 38, 'skipped': 0, 'total': 38}`——38 筆全部整筆替換成新格式，
   沒有變成 76 筆或留下舊欄位。

### `db.py` 現在的函式

```python
save_tourism_attraction_fees(items: list[dict]) -> dict                  # 依 attraction_id 整筆替換 upsert
get_tourism_attraction_fee_by_id(attraction_id: str) -> dict | None       # 單筆查詢，查不到回 None
get_tourism_attraction_fees_by_ids(attraction_ids: list[str]) -> list[dict]  # 批次查詢，給行程預算分析用
```

### 驗證結果

- 舊欄位（`attraction_name`/`imported_at`/`raw_data`）確認已消失，新欄位齊全，`fees` 是小寫 key ✅
- 總筆數 38，`attraction_id` 沒有重複 ✅
- 免費景點（`is_free=true`）**17 筆**
- 規格範例 `Attraction_376480000A_000253`（清境農場）查得到，`default_price=200`（全票）、
  `min_price=20`、`max_price=200` ✅，跟文件預期完全一致
- `Price=0` 的票種正確被視為「有價格（免費票種）」而不是「未提供」，`min_price` 不會是 `null` ✅
- 批次查詢 `get_tourism_attraction_fees_by_ids()` 過濾不存在 id / 空字串、空清單回空陣列 ✅

**⚠️ attraction_id 交叉比對結果（文件要求確認的項目）**：38 筆票價資料裡，**37 筆**能對應到
`tourism_attractions.attraction_id`，有 **1 筆對不到**：

| attraction_id | 名稱 |
|---|---|
| `Attraction_376480000A_000380` | 臺灣省政資料館 |

這不是程式問題，是兩份原始資料（`AttractionList.json` 6087 筆 / `AttractionFeeList.json` 38 筆）
擷取時間不同步造成的資料落差——這個景點在票價資料裡有，但目前匯入的景點主資料裡沒有這一筆。
後端用 `attraction_id` 去對應景點名稱/地址時，這一筆會拿不到主資料，需要自行決定怎麼處理（例如
直接用 `tourism_attraction_fees.name` 當備援顯示名稱）。**97.4% 的比對成功率**，其餘 37 筆穩定可用。

> 兩份補充資料涵蓋範圍都比主檔小很多（38 / 77 筆 vs 6087 筆景點），大部分景點查不到票價或營運時間。
> 呼叫端要處理 `None` 的情況（顯示「票價未提供」或不顯示）。

---

## 六、docx 第 5~8 節「請確認」項目的稽核結果

這幾項在前幾次交接就做過了，這次只做確認、沒有再改（改欄位名會弄壞已經在跑的 pipeline）：

| docx 項目 | 狀態 | 說明 |
|---|---|---|
| §5 天氣每日快取 `weather_daily_cache` | ✅ 運作中，22 筆 | 每日同步機制正常（第一次 `weather_daily_saved:22`、同日第二次 `:0`）。欄位有 `county_name`/`source_date`/`provider`/`expires_at` + `raw_data`（docx 建議名 `forecast_data`）+ `updated_at`（docx 建議名 `fetched_at`）。**機制對，兩個欄位名和 docx 建議不同**，因為 `weather_flow.py` 已經照現有名稱在讀寫，不動它。 |
| §6 API 快取群組隔離 | ✅ 已隔離 | `save_api_query_cache()` / `get_api_query_cache()` 的 `line_group_id` 是**必填參數**，也是唯一索引 `query_type + line_group_id + query_key` 的一部分。A 群組的快取 B 群組讀不到。欄位名和 docx 建議清單有差（`query_key` vs `cache_key`、`result` vs `result_data`、`query_type` 兼作 `provider`），但「一定要有 line_group_id」這個核心要求已滿足。目前 collection 是空的（0 筆）。 |
| §7 RAG 對話紀錄隔離 | ✅ 順序正確 | `get_similar_messages(line_group_id, ...)` 第一個參數就是 `line_group_id`，查詢條件 `{"line_group_id": ..., "embedding": {"$ne": None}}` 在 Python 端算 cosine similarity **之前**就先過濾群組。不是「先向量搜尋再過濾」。 |
| §8 投票資料群組隔離 | ✅ 都有 | `vote_sessions` 13 筆全部有 `line_group_id`。`votes` 欄位 = `poll_id` / `voter_key` / `option_id` / `created_at` / `updated_at`，跟 docx 建議完全一致。`voter_key` 是應用層 HMAC 後的值，不存原始 LINE user id。 |

---

## 七、你後續要接的部分（照文件，這次沒有做）

- 推薦排序邏輯、LLM prompt 修改、LINE Bot 回覆
- 後端行程組裝、天氣／Google Places／觀光署資料整合推薦
- （選配）票價 / 營運時間如果之後要常用縣市查詢，再視資料補縣市索引

---

## 八、回覆給後端同學（「景點票價資料匯入與行程預算分析交接.docx」第 10 節格式）

```text
1. tourism_attraction_fees 已建立（改版，不是新建——2026-09-10 舊格式已整筆替換成新格式）
2. 匯入總筆數：38 筆
3. 免費景點筆數：17 筆
4. 可用 function：
   - get_tourism_attraction_fee_by_id(attraction_id)
   - get_tourism_attraction_fees_by_ids(attraction_ids)
5. 測試 attraction_id：
   - Attraction_376480000A_000253 清境農場，查得到全票 NT$200（default_price=200）
6. 已知資料落差：38 筆裡有 1 筆（Attraction_376480000A_000380 臺灣省政資料館）
   對不到 tourism_attractions，是兩份原始資料擷取時間不同步造成的，不是程式問題。
   用 attraction_id 對應景點名稱時記得處理這種查不到主資料的情況。
```

---

## 相關檔案索引

- 程式碼：`03_project/code/trip_assistant_bot/db.py`、`03_project/code/trip_assistant_bot/import_tourism_data.py`
- 原始規格：`觀光署開放資料匯入交接文件.docx`、`資料庫交接說明.docx`、`景點票價資料匯入與行程預算分析交接.docx`
- 資料庫設計文件：[database_design.md](database_design.md)
- 行程分享功能：[ITINERARY_SHARING_HANDOFF.md](ITINERARY_SHARING_HANDOFF.md)
