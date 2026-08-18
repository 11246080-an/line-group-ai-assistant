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

## 四、你後續要接的部分（照文件，這次沒有做）

- `AttractionFeeList.json` / `AttractionServiceTimeList.json` 匯入
- 推薦排序邏輯、LLM prompt 修改、LINE Bot 回覆
- 後端行程組裝、天氣／Google Places／觀光署資料整合推薦

---

## 相關檔案索引

- 程式碼：`03_project/code/trip_assistant_bot/db.py`、`03_project/code/trip_assistant_bot/import_tourism_data.py`
- 原始規格：`觀光署開放資料匯入交接文件.docx`
- 資料庫設計文件：[database_design.md](database_design.md)
