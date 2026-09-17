# 行程分享與公開網站 — 資料庫層交接

**對應規格：** `資料庫交接文件_行程分享與公開網站.md`（2026-09-08 版，文件自述為唯一正式依據）
**異動檔案：** `03_project/code/trip_assistant_bot/db.py`
**完成日期：** 2026-09-10
**狀態：** 已實作、已在 Atlas 建索引、已跑端到端 smoke test（59 項全過），測試資料已清乾淨。

---

## 一、Collection 與索引（已在 Atlas 建好）

| Collection | 說明 |
|---|---|
| `itineraries` | 私人正式行程 + 分享狀態。**沿用既有 collection**（裡面本來有 26 筆舊版行程），新行程用新結構寫入。 |
| `itinerary_share_consents` | 每位符合資格成員對單一行程的最新匿名同意決定 |
| `published_itineraries` | 公開網站**唯一**可讀的去識別快照 |

索引全部照規格第 7 節，只有一個必要的偏離：

> **`itineraries.itinerary_id` 唯一索引改用 `unique=True, sparse=True`。**
> 規格寫的是純 `unique`，但這個 collection 裡有 26 筆舊版文件（只有 `itinerary_text` / `stops`，沒有 `itinerary_id`）。純 unique 索引會把多筆「缺欄位」當成多個 `null` 而**建立失敗**。`sparse` 只索引有 `itinerary_id` 的文件，舊資料不受影響，新行程照樣保證唯一。已驗證：索引建立成功，26 筆舊資料原封不動。

其餘：`expense_book_id` sparse、`(line_group_id, status)`、`(share_status, share_deadline_at)`、`itinerary_share_consents` 的 `(itinerary_id, voter_key)` unique + `(itinerary_id, responded_at)`、`published_itineraries` 的 `public_id` unique / `source_itinerary_id` unique / `published_at DESC` / `(region, published_at DESC)`。

---

## 二、新增的函式（名稱與參數完全照規格第 5 節，未改名）

```python
create_itinerary(*, itinerary_id, line_group_id, title, spots, transport, created_by,
                 participant_user_ids, expense_book_id=None, region="", summary="",
                 duration="", budget=None) -> dict
get_itinerary(*, itinerary_id, line_group_id) -> dict | None
get_itinerary_by_expense_book(*, expense_book_id) -> dict | None
update_itinerary_schedule(*, expense_book_id, start_at, end_at, timezone, updated_by) -> dict | None
mark_itinerary_completed(*, expense_book_id, completed_at, budget_summary) -> dict
create_itinerary_share_request(*, itinerary_id, line_group_id, eligible_consent_keys,
                               consent_salt, requested_at, deadline_at) -> dict   # {"created_now": bool, "itinerary": dict}
record_itinerary_share_consent(*, itinerary_id, line_group_id, voter_key, decision, responded_at) -> dict
get_itinerary_share_consents(*, itinerary_id) -> list[dict]
publish_itinerary_snapshot(*, itinerary_id, published_at) -> dict   # {"created_now": bool, "snapshot": dict, "itinerary": dict}
reject_itinerary_sharing(*, itinerary_id, line_group_id, resolved_at) -> dict
list_published_itineraries(*, region=None, limit=20) -> list[dict]
get_published_itinerary(*, public_id) -> dict | None
```

### 行為重點

- **`create_itinerary`**：驗證必填 + `spots` 至少一筆；`spot_id` = `f"{itinerary_id}_s{sequence}"`（穩定且唯一）；transport 進來用 `from_sequence`/`to_sequence`，DB 依 `spots.sequence` 轉成 `from_spot_id`/`to_spot_id`（也接受已直接給 spot id）；`participant_user_ids` 去重、保序、保證含 `created_by`；初始 `status="confirmed"`、`share_status="not_requested"`；相同 `itinerary_id` 重複建立 → `DbConflictError`（靠 sparse unique index）。
- **`get_itinerary`**：`itinerary_id` + `line_group_id` 同時比對，跨群組查不到。
- **`update_itinerary_schedule`**：optional sync，找不到對應行程回 `None`（不當錯誤，不影響記帳本的時間更新）。
- **`mark_itinerary_completed`**：`completed_at` 只在第一次寫入（保持穩定），`budget` 每次都用最新 `budget_summary` 合併刷新；重複呼叫 idempotent；完全沒有綁定行程時 raise `ValueError`。
- **`create_itinerary_share_request`**：只有 `completed` 行程可建立；用條件式 `find_one_and_update`（`status="completed"` 且 `share_status="not_requested"`）保證只建立一次；已問過回 `created_now=False`；`eligible_consent_keys` 去重保序。
- **`record_itinerary_share_consent`**：`decision` 只能 `approve`/`decline`（否則 `ValueError`）；行程須屬該群組（否則 `ValueError`）；`share_status` 須是 `pending`、未過期（否則 `DbConflictError`）；`voter_key` 須在 `eligible_consent_keys`（否則 `PermissionError`）；以 `(itinerary_id, voter_key)` upsert，改票只更新原 document、不加票數。回傳含 `tally`（第 6 節票數統計）。
- **`publish_itinerary_snapshot`**：在 **transaction** 內重新查票、重新驗證 `approvals >= (eligible_count + 1)//2`，不只信應用層；不足 → `DbConflictError`。建立 `published_itineraries` 快照 + 更新私人行程 `share_status="published"` 同一 transaction 完成。`source_itinerary_id` unique 保證一筆來源只有一筆快照；已存在或併發撞索引 → 回既有結果 `created_now=False`。
- **`list_published_itineraries`**：只查 `published_itineraries`，`published_at` 新到舊，`limit` 在 DB 層再夾 1~100。
- **`get_published_itinerary`**：只用 `public_id` 查，私人 `itinerary_id` 查不到。

---

## 三、隱私（規格第 4.3 / 第 11 節「隱私」驗收）

`publish_itinerary_snapshot` 建快照時**只複製白名單欄位**：`title` / `region` / `summary` / `duration` /（published 專有的 `description` / `distance` / `type` / `bestFor` / `comment`，私人行程沒有就存空字串）/ `spots` / `transport` / `budget` / `public_id` / `source_itinerary_id` / `version` / `published_at` / `updated_at`。

快照裡**不會出現**：`line_group_id`、`created_by`、`participant_user_ids`、`eligible_consent_keys`、`consent_salt`、`voter_key`、群組/成員名稱、聊天原文、付款人/代墊/分攤明細、私人備註。smoke test 有斷言驗證。

`voter_key` / `consent_salt` / `eligible_consent_keys` 一律當**不透明字串**存放 —— HMAC 是應用層用 `ITINERARY_SHARE_SECRET` 產生的，`db.py` 不做雜湊、不讀那個環境變數（跟匿名投票的處理方式一致）。

---

## 四、原子性（規格第 8 節）

| 要求 | 實作方式 |
|---|---|
| 相同 `itinerary_id` 不可建兩筆 | sparse unique index + `insert_one` 撞索引 → `DbConflictError` |
| 同一行程只建立一次分享要求 | 條件式 `find_one_and_update`（`share_status="not_requested"`），非「先查再寫」 |
| 同一 voter 只保存一筆最新決定 | `(itinerary_id, voter_key)` unique + upsert |
| 同一來源行程只建一筆公開快照 | `source_itinerary_id` unique + transaction 內 check + 撞索引 fallback 回既有 |
| `mark_itinerary_completed` 重複呼叫不重複副作用 | `completed_at: None` 條件式更新，第二次走「只刷新 budget」分支 |
| 發布前重新驗票 | transaction 內從 `itinerary_share_consents` 重新 count，不信應用層 |

---

## 五、已驗證（smoke test，59 項，測完清乾淨）

建行程 → 重複 id 被擋 → 跨群組隔離 → 時間同步 → 未完成不能發分享要求 → 標記完成（idempotent + budget 合併刷新）→ 建分享要求（去重、重複呼叫不重送）→ 收票（非法 decision / 非 eligible / 跨群組 / 改票不加票數 / tally 由 pending 變 approved）→ 發布（白名單欄位、無私人欄位、重複呼叫回同一 `public_id`、只有一筆快照、發布後不能再投票）→ 公開查詢（region 篩選、limit 夾 1~100、private id 查不到）→ 拒絕流程（declined、idempotent、未達門檻不能發布）→ 門檻公式對照表 1~6 人。

datetime 從 Mongo 讀回是 naive-UTC（pymongo 預設行為，全 DB 一致）；`db.py` 內部唯一需要拿它跟現在時間比大小的地方（`record_itinerary_share_consent` 檢查是否過期）已用 `_ensure_aware_utc()` 正規化。

---

## 六、還沒做 / 不在這次範圍（2026-09-15 版本的狀態）

- 規格第 2 節明列不含：修改草稿、`update_itinerary_plan()`、把網站假資料匯入 MongoDB、管理員手動新增公開行程。這些都沒做。
- 26 筆舊版 `itineraries` 文件（舊結構 `itinerary_text` / `stops`）保留原狀，沒有遷移 —— 新函式都用 `itinerary_id` / `expense_book_id` / `status` 查詢，不會撈到舊資料。

> 下面第七節是後來「近期行程功能補強」交接文件要求的內容，包含這裡原本列的
> 「過期 share request 自動清理」——已經補上了。

---

## 七、近期行程功能補強（2026-09-17，依「資料庫交接文件：近期行程功能補強」）

### 7.1 P0：`create_itinerary()` 新增選用參數（向後相容）

```python
create_itinerary(..., itinerary_type: str = "", best_for: str = "", confirmed_by: str = "")
```

- `itinerary_type` 只接受網站七分類（山城/都市/河岸/自然/美食/文化/海線）其中之一，其餘（含「使用者分享」）一律存空字串，欄位存成 `type`。
- `best_for` 去空白後最長 160 字。
- `confirmed_by` 是實際按下確認的人，跟 `created_by`（草稿建立者）分開存，可以不同。
- `spots` 可選帶 `coordinate_source`，只接受 `"tourism_open_data"` / `"google_places"`，其餘存 `None`；這個欄位**不會**進公開快照。
- 三個參數都有預設值，舊呼叫端不傳也能正常建立行程（已測試驗證）。

### 7.2 P0：修正公開 snapshot 欄位對應

`_build_public_snapshot()` 不再用「同名迴圈」處理 `type`/`bestFor`/`comment`，改成明確映射：

```python
snapshot.update({
    "status": "published",
    "type": itinerary.get("type", ""),
    "bestFor": itinerary.get("best_for", ""),       # 私人叫 best_for，公開叫 bestFor
    "comment": itinerary.get("recommendation_reason", ""),
})
```

- `distance` 已從 `_PUBLIC_SNAPSHOT_EXTRA_FIELDS` 移除，不會再出現在新快照裡。
- 公開快照的 `spots` 現在會過濾欄位（`_public_spot()`），只留 `spot_id/sequence/name/description/address/latitude/longitude`——`coordinate_source` 不會進公開快照，`address/latitude/longitude` 仍保留、值可以是 `null`。

### 7.3 P0：推薦理由（新 collection `itinerary_recommendation_reasons`）

三支函式（應用端用 introspection 檢查是否同時存在）：

```python
record_itinerary_recommendation_reason(*, itinerary_id, line_group_id, voter_key, reason, submitted_at) -> dict
get_itinerary_recommendation_reasons(*, itinerary_id) -> list[dict]
set_published_itinerary_recommendation_reason(*, itinerary_id, line_group_id, reason, selected_voter_key, selected_at) -> dict
```

- 提交理由前會檢查：行程屬於該群組、`voter_key` 在 `eligible_consent_keys` 內、這個 voter 在 `itinerary_share_consents` 裡的**最新**決定是 `approve`。
- `(itinerary_id, voter_key)` unique + upsert，同一人重送覆蓋，不會變多筆候選。
- 理由長度上限 240 字，超過丟 `ValueError`。
- 選定時用 transaction 同步寫：私人 `itineraries` 存 `recommendation_reason` + `recommendation_reason_selected_voter_key`（稽核用）+ `recommendation_reason_selected_at`；公開 `published_itineraries` **只更新 `comment`**，絕不寫 `selected_voter_key`。

### 7.4 P0：PDF 24 小時下載 session（新 collection `expense_report_downloads`）

```python
save_expense_report_download(*, token_hash, snapshot, expires_at) -> dict
get_expense_report_download(*, token_hash, now) -> dict | None
```

- 用 `token_hash`（呼叫端算好的 SHA-256）upsert，**不存** URL 裡的原始 token。
- `get` 查詢條件本身就包含 `expires_at: {"$gt": now}`，TTL 索引另外做背景清除（不即時，`get` 不能只靠 TTL）。
- 不是一次性消耗，效期內可重複下載。

### 7.5 P0：既有資料清理（已用 `migrate_itinerary_enhancements.py --apply` 執行）

規格 §8.2 指定的 3 個 spot（`trip_jYaMS-CLicTgamGZ_s2`、`trip_lVi51hJBO4Hrm-yZ_s2`、`trip_lVi51hJBO4Hrm-yZ_s3`）在私人 `itineraries` 與公開 `published_itineraries` 都已清空 `address/latitude/longitude/coordinate_source`，**3 個全部命中且只改這 3 個**，沒有用名稱做全庫批次更新。

### 7.6 P1：`published_itineraries` 加 `status`

- 新快照一律帶 `status="published"`（見 7.2）。
- 既有文件已用 migration 補上 `status="published"`（詳見下面的「migration 結果」）。
- `list_published_itineraries()` / `get_published_itinerary()` 現在都強制加 `status: "published"` 條件，下架（`withdrawn`）文件不會再被公開 API 查到。

### 7.7 P1：`publish_itinerary_snapshot()` 加強 DB 層重驗

除了原本的「重新查票驗門檻」，現在還會檢查（任一不符合都丟 `DbConflictError`）：

- `itinerary.status == "completed"`
- `itinerary.share_status == "pending"`
- `share_deadline_at` 存在，且 `published_at` 沒超過期限

### 7.8 P1：逾期／下架

新增兩支規格要求但原本缺的函式：

```python
expire_itinerary_share_requests(*, now, limit=50) -> list[dict]   # 逐筆認領過期 pending -> expired
unpublish_itinerary(*, itinerary_id, line_group_id, resolved_at) -> dict  # 下架，不刪除稽核資料
```

`unpublish_itinerary` 把私人行程設 `share_status="withdrawn"`、公開快照設 `status="withdrawn"`（**不刪除**那筆 `published_itineraries` 文件），公開 API 只查 `status="published"` 所以自然不再顯示。

### 7.9 §10：`expense_book_id` 一對一 —— ⚠️ 這裡需要你們決定，我沒有自己判斷

規格要我先跑 aggregation 檢查有沒有重複 `expense_book_id`，再決定要不要加 unique 索引。**檢查結果：目前資料庫裡真的有活躍資料撞到這個問題，而且不只一種情況：**

1. **第一種（已經處理掉了）**：`trip_CVXFjZp97rgPjj54`（completed + 已發布）跟 `trip_o81FWTcatNP1WMlK`（confirmed，較晚建立）共用同一本已經 `closed` 的帳本——這個看起來就是誤連（帳本已經關閉，新行程不應該連上去），已經用 migration 把較晚那筆的 `expense_book_id` 解除連結（改成 `None`）。

2. **第二種（**沒有動**，需要你們確認產品邏輯）**：群組 `C1ab2da476adc948a9524187c3849f845` 有 **4 份標題完全不同**（嘉義美食與自然步道 / 澎湖跳島一日遊 ×2 / 台北老街一日遊）的行程，全部連到同一本**目前還是 active** 的帳本。這 4 筆都是 `status="confirmed"`，沒有一筆特別像是「誤連」——看起來比較像是「群組在同一本帳本開著的期間，陸續確認了好幾份不同的候選行程」這種真實使用情境，而不是 bug。

   **我沒有自己決定怎麼處理**，因為：
   - 如果這其實是 bug（每次確認新行程都應該各自開一本新帳本），那應該去查 `itinerary_flow.py` 的 `_confirm_draft()` 為什麼會重複拿到同一本 active 帳本，而不是我在 DB 層武斷解除連結、可能弄丟使用者的真實資料。
   - 如果這其實是合法的產品行為（一本帳本本來就可以連多份候選行程），那照規格 §10 的指示，應該把 `expense_book_id` 索引**維持非 unique**，並把 `get_itinerary_by_expense_book()` 改成回傳 `list[dict]`（目前還是回傳單筆 `dict | None`），同步調整應用端——這是要跨 `db.py` 和 `itinerary_flow.py` 一起改的事。

   **目前的暫時狀態**：`expense_book_id` 索引維持原本的 `sparse`、**非 unique**，`get_itinerary_by_expense_book()` 行為不變（單筆、可能非決定性——如果同一本帳本真的連了兩份以上行程，回傳哪一筆要看 MongoDB 怎麼選，不保證是哪一份）。這是刻意的：寧可讓查詢結果暫時「語意不明」，也不要在不確定的情況下砍斷使用者資料的關聯，或是擋住可能合法的操作。

   寫了一個新的一次性 migration script `03_project/code/trip_assistant_bot/migrate_itinerary_enhancements.py`，裡面第 3 步**只會列出目前的重複情況，不會自動修改**（原本第一版會自動「保留最早、解除其餘」，但這在測試階段就真的誤觸過一次——把上面第二種情況的 3 份行程解除連結，已經手動復原——所以拿掉自動修正，避免同一個問題重演）。

### 7.10 Migration 結果（`migrate_itinerary_enhancements.py --apply`）

| 項目 | 結果 |
|---|---|
| §8 三個不可靠座標 spot | 3/3 命中，private + published 都已清空 |
| `published_itineraries` 補 `status="published"` | 第一次 apply：5 筆；後續發現又有 3 筆新文件沒帶 status（見下方「⚠️ 有一個線上服務還在用舊程式碼」），第二次 apply 補上 |
| `expense_book_id` 重複 | 1 組（原始）已修正；1 組（4 份行程共用一本帳本）**維持原狀，見 7.9** |

### 7.11 ⚠️ 重要：有一個線上服務目前還在用舊的 `db.py`

migration 跑完後，資料庫裡又多了 3 筆新的 `published_itineraries`，但**沒有** `status` 欄位——代表有一個還在執行中的服務行程（Flask process）在我改完 `db.py` 之後，用**記憶體裡舊版**的 `_build_public_snapshot()` 又發布了 3 份行程快照。Python 改了原始碼不會讓已經在跑的 process 自動重新載入。

**這代表：現在正式環境還沒有真正吃到這次的程式碼變更**，包含：
- 新發布的快照不會有 `type`/`bestFor`/`comment` 正確映射（`bestFor` 之前用同名迴圈複製，永遠是空的）
- `publish_itinerary_snapshot()` 還沒有 7.7 的加強驗證
- `list_published_itineraries()` / `get_published_itinerary()` 還沒有過濾 `status="published"`
- 推薦理由、PDF 下載 session 兩組新函式都還無法使用

**部署後（重啟 Flask process）記得再跑一次** `python migrate_itinerary_enhancements.py --apply` 補齊重啟前這段期間累積的資料，並用 §10 的 aggregation 重新確認有沒有更多 `expense_book_id` 重複的情況。

---

## 相關檔案

- 程式碼：`03_project/code/trip_assistant_bot/db.py`
- 一次性 migration：`03_project/code/trip_assistant_bot/migrate_itinerary_enhancements.py`（`--apply` 才會寫入，預設 dry-run）
- 資料庫設計總表：[database_design.md](database_design.md)
