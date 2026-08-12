# 記帳／發票／投票 — 資料庫層實作交接

**對應規格：** `DB修改文件(記帳、投票等).md`（組員提供）
**異動檔案：** `03_project/code/trip_assistant_bot/db.py`
**日期：** 2026-08-16

---

## 一、實作範圍

規格文件要求的所有 collection 與「必須提供的 Python 介面」都已經照文件的函式名稱／參數／回傳型態實作在 `db.py` 裡（規格文件說非 DB 模組會檢查這些函式是否存在，所以名稱、參數簽名完全對齊文件，沒有自己改名或調整）。

新增的 7 個 collection：`expense_books`、`expenses`、`feature_drafts`、`invoice_imports`、`votes`、`feature_event_dedup`，以及沿用並擴充既有的 `vote_sessions`。

## 二、索引

除了文件列出的索引，實作上有兩個地方需要特別注意：

1. **`expense_books`** 和 **`vote_sessions`** 的「同群組只能有一筆 active」規則，都是用 **partial unique index** 實作（`partialFilterExpression={"status": "active"}`），不是在程式碼裡手動檢查再插入——這樣才能真正防止併發請求同時建立兩筆 active 資料的 race condition。
2. **`vote_sessions` 舊索引遷移**：原本 `ensure_indexes()` 就有一個 `(line_group_id, status)` 的普通複合索引（沒有 unique）。新規格要求同一鍵組合但要加 `unique + partialFilterExpression`，MongoDB 不會自動覆蓋舊索引（會噴 `IndexOptionsConflict`），所以程式碼裡會先偵測舊索引存不存在、是不是已經是 unique，不是就先 `drop_index` 再建新的。這段邏輯在 `ensure_indexes()` 裡，重啟服務或手動呼叫都會自動處理，冪等、可以重複執行。

## 三、我做的幾個判斷（規格沒寫清楚的地方）

規格文件非常詳細，但有幾處實作時發現規格沒有明確定義，我先做了保守的判斷，**這幾點建議跟另一位組員確認一次**：

### 1. 「指定 editor」沒有對應的 schema 欄位
規格寫「只有帳本建立者／指定 editor 可確認起訖時間、提前關閉與重新開啟」，但 `expense_books` 的欄位定義裡**沒有 `editors` 陣列**，只有 `created_by`。目前 `rename_expense_book()` / `update_expense_book_schedule()` / `close_expense_book()` / `reopen_expense_book()` 這四支函式都只認 `created_by`，沒有「指定 editor」這個角色。如果之後要支援多人可編輯，schema 要先加一個 `editors: [line_user_id, ...]` 欄位，這幾支函式的權限檢查也要跟著改。

### 2. `invoice_imports.expires_at` 沒有定義到期規則
Schema 裡有 `expires_at` 欄位，但「索引」章節只列了 `(book_id, source_fingerprint)` 這個唯一索引，沒有提到 TTL 索引，函式簽名 `create_invoice_import(*, book_id, source_fingerprint, created_by)` 也沒有 `expires_at` 或 `ttl_seconds` 參數。目前這個欄位存 `None`，沒有實際到期行為。如果之後想讓太久沒確認的 draft 自動失效，需要先決定到期時長，再補一個 TTL 索引——這個我沒有自己編一個數字進去。

### 3. 發票「加總 = 總額」一致性檢查，DB 層做不到
規格寫「展開明細時必須包含服務費、折扣及其他調整，且所有建立支出的加總必須等於發票總額；不一致時 DB 層也應拒絕 transaction」。但 `expenses` 和 `invoice_imports` 的 schema 都**沒有「發票總額」這個欄位**可以拿來比對——`create_expenses_from_invoice()` 收到的 `payload` 就是唯一的資料來源，沒有一個獨立的「應該等於多少」的數字可以核對。

所以目前的實作**沒有做加總比對**，這個一致性保證必須由呼叫端在組出 `payload`（也就是解析發票 OCR/QR 結果、算好服務費與折扣調整項）之後、呼叫這支函式**之前**就先確保加總正確。函式的 docstring 裡有寫這個限制。如果要讓 DB 層也能檢查，需要在 `invoice_imports` schema 額外加一個 `expected_total` 欄位，讓呼叫端把發票的總額也一併傳進來。

### 4. HMAC 匿名化的計算不在 db.py 裡
`created_by_key`、`eligible_voter_keys`、`voter_key` 這些欄位，函式簽名都是直接接收「已經算好的 HMAC key」，`db.py` 不會、也不應該知道怎麼把 LINE user ID 轉成 HMAC——這是呼叫端（規格裡提到的 `privacy_redaction` 之類的模組）的職責，`db.py` 只負責原樣存放、原樣查詢。`VOTE_ANONYMIZATION_SECRET` 這個環境變數也不會在 `db.py` 裡被讀取。

## 四、Transaction 的使用

`create_expenses_from_invoice()` 和 `cast_anonymous_vote()` 都用 `client.start_session()` + `session.with_transaction(...)` 包起來，任何一步丟出例外都會讓整個 transaction rollback，不會有「只成功一半」的情況。**已經在你的 Atlas 叢集上實測過，transaction 可以正常運作**（Atlas 的 cluster 本質上都是 replica set，M0 免費層也支援 transaction）。

`with_transaction` 內建會自動重試 transient transaction error（例如短暫的網路問題或 write conflict），不需要自己再包一層重試邏輯。

## 五、新的例外類型 `DbConflictError`

新增了一個 `db.DbConflictError`（繼承 `RuntimeError`），代表「這個操作現在不能做」而不是系統錯誤，例如：

- 同群組已經有進行中的帳本／投票
- 發票已經確認過，不能重複展開
- 投票已經結束或超過截止時間

呼叫端應該 `except DbConflictError` 來給使用者一個「現在不行」的訊息，跟一般的程式錯誤分開處理，不要整包 `except Exception`。

## 六、原子編號、原子 claim 的實作方式

- **`next_expense_number`**：用 `$inc` 一次性配置連續 N 個編號（`find_one_and_update` 搭配 `return_document=ReturnDocument.BEFORE` 取出遞增前的值），保證併發請求不會拿到重疊編號；取消支出不會歸還號碼，所以編號永遠不重用。
- **`claim_due_expense_books()` / `claim_due_vote_sessions()`**：用迴圈搭配單筆 `find_one_and_update`（而不是 `update_many`）逐筆認領，每一筆的認領本身是原子操作，多個 worker 同時跑排程不會搶到同一筆、也不會漏掉。

## 七、上線前還沒做的事

1. **`ensure_indexes()` 還沒有在 Atlas 上執行**（本次改動只在本機檔案完成，還沒有部署到你的 Atlas 叢集）。需要重啟服務或手動執行一次才會生效，而且這次牽涉到 `vote_sessions` 舊索引的刪除／重建，跟之前的 `api_query_cache` 遷移是類似的性質。
2. 規格文件「上線順序」第 3 步「在 staging 執行一筆手動記帳、發票合併、發票展開、改票及到期 claim」——這幾個端到端流程我還沒有實際跑過，只做過程式碼層級的檢查跟 `py_compile`。

---

## 相關檔案索引

- 程式碼：`03_project/code/trip_assistant_bot/db.py`
- 原始規格：`DB修改文件(記帳、投票等).md`
- 其他 DB 交接文件：[RAG_HANDOFF.md](RAG_HANDOFF.md)、[GROUP_ISOLATION_AUDIT.md](GROUP_ISOLATION_AUDIT.md)
