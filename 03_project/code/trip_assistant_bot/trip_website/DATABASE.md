# 資料庫連接說明

## 概覽

trip_website 使用 **MongoDB Atlas** 作為資料庫，透過 Flask 後端 (`api.py`) 提供 REST API 給前端使用。

```
前端 (app.js)
    │  fetch /api/itineraries
    ▼
後端 (api.py / Flask)
    │  pymongo
    ▼
MongoDB Atlas
  資料庫: linebot
  集合:   itineraries
```

---

## 環境需求

| 項目 | 版本需求 |
|------|----------|
| Python | 3.8 以上 |
| pip 套件 | `flask` `flask-cors` `pymongo[srv]` |

---

## 安裝步驟

### 1. 安裝套件

```bash
cd trip_website
pip install -r requirements.txt
```

`requirements.txt` 內容：

```
flask
flask-cors
pymongo[srv]
```

> `pymongo[srv]` 的 `[srv]` 是必要的，才能解析 `mongodb+srv://` 格式的連線字串。

---

### 2. 連線字串

連線字串已寫在 `api.py` 第 13 行：

```python
MONGO_URI = "mongodb+srv://11246066:11246066@cluster0.go9e1.mongodb.net/linebot"
```

| 欄位 | 值 |
|------|----|
| 帳號 | `11246066` |
| 密碼 | `11246066` |
| Cluster | `cluster0.go9e1.mongodb.net` |
| 資料庫 | `linebot` |
| 集合 | `itineraries` |

若需要更換連線字串，直接修改 `api.py` 中的 `MONGO_URI` 變數即可。

---

### 3. 啟動伺服器

```bash
python api.py
```

成功啟動後會看到：

```
[seed] 新增 25 筆靜態行程到 MongoDB linebot.itineraries   ← 第一次啟動才出現
 * Running on http://127.0.0.1:5000
```

開啟瀏覽器前往 `http://localhost:5000` 即可使用網站。

---

## 資料庫結構

### 集合：`itineraries`

每筆文件（Document）對應一條一日遊行程，格式如下：

```json
{
  "_id": "newtaipei-pingxi-day",
  "id": "newtaipei-pingxi-day",
  "title": "平溪鐵道放天燈一日遊",
  "region": "新北",
  "budget": "每人約 500-800 元",
  "distance": "近郊",
  "type": "山城",
  "transport": "台鐵/公車",
  "duration": "一日遊",
  "summary": "沿著平溪支線慢慢玩...",
  "description": "從十分瀑布開始走到老街...",
  "bestFor": "想拍照、想走輕鬆山城...",
  "comment": "安排者覺得這條最適合...",
  "spots": [
    {
      "name": "十分瀑布",
      "description": "先看瀑布與吊橋，當作一日遊開場。",
      "lat": 25.0492,
      "lng": 121.7871
    }
  ]
}
```

> `spots` 陣列直接嵌入行程文件內（MongoDB Embedded Document 模式），不需額外查詢。

---

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/api/itineraries` | 取得所有行程（只回傳有 `title` 欄位的文件） |
| `GET` | `/api/itineraries/<id>` | 取得單筆行程 |
| `GET` | `/` | 網站首頁 (`index.html`) |

### 範例

```bash
# 取得所有行程
curl http://localhost:5000/api/itineraries

# 取得單筆行程
curl http://localhost:5000/api/itineraries/newtaipei-pingxi-day
```

---

## 種子資料 (Seed)

`api.py` 啟動時會自動執行 `seed_if_empty()`：

- 讀取 `src/data.js` 裡的 25 筆靜態行程
- 使用 `replace_one(upsert=True)` 寫入 MongoDB
- **不會覆蓋**集合內其他既有資料（例如 LINE Bot 產生的行程）

若需要重新匯入，可手動刪除集合中 `_id` 為字串格式的文件後再重啟。

---

## 新增 / 修改行程

### 方法一：直接操作 MongoDB Atlas

1. 前往 [MongoDB Atlas](https://cloud.mongodb.com)
2. 選擇 Cluster → `linebot` 資料庫 → `itineraries` 集合
3. 新增或編輯文件，欄位格式參考上方 [資料庫結構](#集合itineraries)

### 方法二：修改 `src/data.js`

1. 在 `src/data.js` 的 `window.ITINERARIES` 陣列中新增行程物件
2. 刪除 MongoDB 中對應的舊文件（或清空所有字串 `_id` 的文件）
3. 重啟 `python api.py`，seed 會自動補入新資料

---

## 常見問題

### 連線失敗 `ServerSelectionTimeoutError`

- 確認網路可連外（MongoDB Atlas 需要網際網路）
- 確認 Atlas 的 IP 白名單有開放目前的 IP（Atlas → Network Access）
- 確認連線字串帳號密碼正確

### 安裝 `pymongo[srv]` 失敗

```bash
# 若使用 PowerShell，需要用引號包住
pip install "pymongo[srv]"
```

### 前端顯示 0 筆行程

1. 確認 Flask 伺服器正在執行
2. 開啟瀏覽器開發者工具 → Network，確認 `/api/itineraries` 有回傳資料
3. 確認 MongoDB 集合內有含 `title` 欄位的文件
