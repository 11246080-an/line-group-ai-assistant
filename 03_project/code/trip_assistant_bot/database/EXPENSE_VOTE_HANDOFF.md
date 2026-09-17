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
規格寫「只有帳本建立者／指定 editor 可確認起訖時間、提前關閉與重新開啟」，但 `expense_books` 的欄位定義裡**沒有 `editors` 陣列**，只有 `created_by`。目前 `rename_expense_book()` / `update_expense_book_schedule()` / `reopen_expense_book()` 這三支函式都只認 `created_by`，沒有「指定 editor」這個角色。如果之後要支援多人可編輯，schema 要先加一個 `editors: [line_user_id, ...]` 欄位，這幾支函式的權限檢查也要跟著改。

> **2026-09-17 更新：`close_expense_book()` 已經改成群組成員都能操作，不再限定 `created_by`**——見下方「同群組成員結束行程」章節，這是產品明確要的行為，不是待補項目。

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

## 七、已在 Atlas 上驗證過（2026-08-16）

`ensure_indexes()` 已經跑到你的 Atlas 叢集上，7 個新 collection 的索引都建好了，`vote_sessions` 舊的 `(line_group_id, status)` 索引也確認已經被砍掉、換成新的 partial unique 索引 `uniq_active_vote_per_group`。

同時跑了一次涵蓋規格「上線順序」第 3 步要求的端到端 smoke test（用專屬測試群組 ID，測完會清乾淨，不影響現有真實資料）：建帳本、重複建立 active 帳本會被拒絕、加成員去重、改名／改起訖時間的權限檢查、記三筆手動支出（含編號連續遞增、取消後編號不重用）、發票匯入去重、`create_expenses_from_invoice` 的 transaction 展開與重複展開防護、關閉／重開帳本、`claim_due_expense_books` 到期認領、草稿存取、建投票（含選項數與重複 active 投票的檢查）、三人投票＋改票（改票不增加總票數）＋全員投完自動關閉、`get_vote_results` 統計、`claim_due_vote_sessions` 到期認領（且不重複認領已公布結果的投票）、webhook 事件 claim/release 冪等。**55 項檢查全部通過。**

### 過程中抓到並修正一個真的會炸的 bug

`cast_anonymous_vote()` 原本直接拿呼叫端傳進來的 `now`（timezone-aware）跟從 MongoDB 讀回來的 `poll["deadline_at"]` 做 `>=` 比較。**pymongo 預設把 BSON date 讀回來會是 naive datetime**（沒有 tzinfo，但實際上是 UTC 時刻）——這是 pymongo 的已知行為，本來 `get_api_query_cache()` 就有為了同樣的原因做過處理，但這次新增的投票邏輯漏掉了。第一次跑 smoke test 時就直接噴 `TypeError: can't compare offset-naive and offset-aware datetimes`。

修法是加了一個共用的 `_ensure_aware_utc()` 小工具，任何要拿 Mongo 讀回來的 datetime 跟呼叫端傳入的 aware datetime 做 Python 層級比較之前，都先補上 `tzinfo=UTC`。已經修好並重新測過，55 項全過。這也代表如果之後還有其他函式要加類似「拿資料庫裡的時間跟現在時間比大小」的邏輯，記得用這個工具，不要直接比較。

---

## 同群組成員結束行程（2026-09-17，依「DB交接文件_同群組成員結束行程.md」）

### 問題

只有帳本建立者能按「確認結束」成功關閉帳本並產生報表；同群組其他成員操作時，`close_expense_book()` 因為 `created_by != closed_by` 丟 `PermissionError`，被 `expense_flow.py` 的萬用 `except Exception` 接住，變成「記帳功能暫時無法完成這個操作，請稍後再試」這種看不出原因的訊息。

### 修改內容

```python
def close_expense_book(*, book_id: Any, closed_by: str, line_group_id: str) -> dict:
```

- 查詢／更新條件改成 `{"_id": book_id, "line_group_id": line_group_id, "status": "active"}`，不再比對 `created_by`。
- `closed_by` 現在會被**存進 `expense_books.closed_by`**（原本完全沒存這個欄位）。
- 找不到符合條件的進行中帳本（群組不對，或帳本已經 `closed`）一律丟 `PermissionError`，訊息不再暗示「只有建立者可以」。
- `reopen_expense_book()` 不在這次範圍內，維持原本只認 `created_by` 的邏輯。

### ⚠️ 這次額外修正：交接文件對 `expense_flow.py` 現況的描述不準確

文件說「`expense_flow.py` 已經有向後相容處理：只要函式宣告包含 `line_group_id`，應用程式就會自動傳入」。實際檢查 `expense_flow.py` 的 `_db_function()`，發現它只是單純 `getattr(module, name)` 拿函式參照，**沒有任何 signature introspection 或自動注入參數的邏輯**；`action == ["close"]` 那段呼叫點原本寫死只傳 `book_id` / `closed_by`，完全沒帶 `line_group_id`。

如果只改 `db.py`、不改呼叫端，`line_group_id` 是必填參數會讓這個呼叫直接 `TypeError`——連原本的帳本建立者都會壞掉，比修改前更糟。所以這次也一併：

1. 補上 `expense_flow.py` 第 914 行呼叫點的 `line_group_id=line_group_id`（這個變數在該函式作用域內本來就存在，不需要多傳）。
2. 同步修改 `database/in_memory_feature_db.py` 裡的測試用 `close_expense_book()`（`USE_IN_MEMORY_FEATURE_DB=true` 時會換成這個實作，呼叫點是共用的，不改的話本地測試模式一樣會 `TypeError`），邏輯改成比對 `line_group_id` 而不是 `created_by`，並補存 `closed_by`。

### 驗證結果（10 項全過，涵蓋文件驗收清單 1~6 項）

建立者可關閉自己的帳本、**同群組非建立者可以關閉帳本**（這是本次修復的核心）、`closed_by` 正確存實際操作者（不是建立者）、關閉後 `list_expenses` 正常、不同群組無法用 `book_id` 跨群組關閉（`PermissionError`，帳本狀態不受影響）、已關閉的帳本不能重複關閉（`PermissionError`）、缺 `line_group_id` 的舊呼叫方式會直接 `TypeError`（故意的，逼呼叫端一定要更新，而不是悄悄用錯邏輯）。

---

## 相關檔案索引

- 程式碼：`03_project/code/trip_assistant_bot/db.py`、`03_project/code/trip_assistant_bot/expense_flow.py`、`03_project/code/trip_assistant_bot/database/in_memory_feature_db.py`
- 原始規格：`DB修改文件(記帳、投票等).md`、`DB交接文件_同群組成員結束行程.md`
- 其他 DB 交接文件：[RAG_HANDOFF.md](RAG_HANDOFF.md)、[GROUP_ISOLATION_AUDIT.md](GROUP_ISOLATION_AUDIT.md)
