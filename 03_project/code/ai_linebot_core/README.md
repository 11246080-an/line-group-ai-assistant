# LINE 群組 AI 助理判斷核心

這個專案是「LINE 群組 AI 助理」的判斷核心，負責接收一段群組對話文字後，輸出結構化判斷結果，提供 LINE Bot 或其他上層服務使用。

它不是完整的 LINE Bot，而是可以被 LINE Bot 直接呼叫的核心模組，主要負責：

- 對話資訊抽取
- 情境判斷
- 是否介入判斷
- 外部查詢需求判斷
- 中繼回覆與正式回覆建議

目前架構為：

- `Groq LLM-first`
- `rule-based fallback`

也就是說，系統預設先使用 Groq LLM 進行情境判斷；如果沒有 API key、沒有安裝套件、LLM 呼叫失敗、或回傳格式錯誤，才會退回 rule-based classifier。

## 專案結構

```text
專題製作/
├─ app/
│  ├─ __init__.py
│  ├─ cli.py
│  ├─ engine.py
│  ├─ extractor.py
│  ├─ classifier.py
│  ├─ llm_judge.py
│  ├─ knowledge_base.py
│  └─ models.py
├─ tests/
│  └─ manual_cases.py
├─ .env.example
├─ requirements.txt
└─ README.md
```

模組分工如下：

- `app/extractor.py`
  負責從群組對話中抽取時間、地點、活動、風險、需求類型等摘要資訊。

- `app/llm_judge.py`
  負責將對話全文與摘要資訊交給 Groq LLM，輸出固定格式 JSON。

- `app/classifier.py`
  保留作為 fallback classifier，當 LLM 無法使用時啟用。

- `app/engine.py`
  專案統一入口，整合 extractor、llm_judge 與 fallback classifier。

- `app/models.py`
  定義 `ExtractedInfo` 與 `AnalysisResult` 等資料模型。

## 安裝方式

請先建立虛擬環境，再安裝套件：

```powershell
pip install -r requirements.txt
```

## 環境設定

請依照 `.env.example` 建立 `.env`：

```env
GROQ_API_KEY=your_api_key_here
GROQ_MODEL=llama-3.3-70b-versatile
```

也可以直接用環境變數設定。

PowerShell：

```powershell
$env:GROQ_API_KEY="your_api_key_here"
$env:GROQ_MODEL="llama-3.3-70b-versatile"
```

Windows CMD：

```cmd
set GROQ_API_KEY=your_api_key_here
set GROQ_MODEL=llama-3.3-70b-versatile
```

## 執行方式

使用文字檔輸入：

```powershell
python -m app.cli --input sample_dialogue.txt
```

或互動輸入：

```powershell
python -m app.cli
```

## 統一入口

LINE Bot 同學應直接呼叫這個入口：

```python
from app.engine import analyze_dialogue

result = analyze_dialogue(text)
payload = result.to_dict()
```

其中 `text` 只需要是一段群組對話文字。

## LINE Bot 串接方式

如果 LINE Bot webhook 收到群組訊息，可以直接把整理好的對話文字丟進：

```python
from app.engine import analyze_dialogue

group_text = """A：現在要不要吃東西
B：好啊
C：附近有什麼
D：不要等太久"""

result = analyze_dialogue(group_text)
payload = result.to_dict()
```

之後可依照 `payload` 內容決定要不要回覆：

- `should_intervene = false`
  表示暫時不需要主動發言。

- `should_intervene = true` 且 `requires_external_search = false`
  可直接回 `suggested_reply`。

- `should_intervene = true` 且 `requires_external_search = true`
  建議先回 `intermediate_reply`，查完資料後再回 `suggested_reply`。

## JSON 輸出欄位

`analyze_dialogue(text).to_dict()` 會輸出固定格式資料，欄位如下：

- `scenario_code`
  劇本編號，例如 `劇本十六`

- `scenario_name`
  劇本名稱

- `stage`
  劇本所屬階段，例如 `發起階段`、`決策確認階段`、`特殊情境`

- `should_intervene`
  AI 是否應介入

- `intervention_type`
  介入類型，例如 `顯性介入`、`隱性介入`、`不介入`

- `confidence_score`
  本次判斷的信心值

- `evidence`
  判斷依據列表

- `system_behavior`
  建議系統行為列表

- `requires_external_search`
  是否需要查詢外部資訊

- `intermediate_reply`
  若需要查詢外部資訊，先回的 LINE 群組口語訊息

- `suggested_reply`
  最後正式回覆內容

- `extracted_info`
  抽取出的摘要資訊物件，包含：
  - `time`
  - `location`
  - `people_count`
  - `budget`
  - `constraints`
  - `activity_types`
  - `options`
  - `decision_state`
  - `risk_info`
  - `need_type`

## 回應邏輯

建議 LINE Bot 同學依照以下規則使用：

1. `should_intervene = false`
   不主動發言，僅保留判斷結果供內部紀錄或後續觀察。

2. `should_intervene = true` 且 `requires_external_search = false`
   直接使用 `suggested_reply` 作為群組回覆候選。

3. `should_intervene = true` 且 `requires_external_search = true`
   採用兩階段回覆：
   - 先回 `intermediate_reply`
   - 執行外部查詢
   - 查完後再回 `suggested_reply`

## 測試方式

有設定 Groq API key：

```powershell
python -m tests.manual_cases
```

沒有 API key，測 fallback：

```powershell
Remove-Item Env:GROQ_API_KEY -ErrorAction SilentlyContinue
python -m tests.manual_cases
```

Windows CMD：

```cmd
set GROQ_API_KEY=
python -m tests.manual_cases
```

## 目前可交接範圍

目前這個專案已可直接交給 LINE Bot 同學作為「判斷核心模組」使用，因為它已具備：

- 統一入口 `analyze_dialogue(text)`
- 固定 JSON 輸出
- LLM-first 判斷
- fallback 機制
- 外部查詢與非查詢情境區分
- 兩階段回覆欄位設計

但它仍不是完整 LINE Bot，尚未包含：

- webhook server
- LINE reply token / push message 實作
- 真正的外部資料查詢模組
- 查詢任務狀態管理

因此，最適合的交接定位是：

**可直接交給 LINE Bot 同學串接的判斷核心服務**
