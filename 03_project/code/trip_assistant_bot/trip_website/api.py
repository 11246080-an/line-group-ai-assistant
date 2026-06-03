import json
from pathlib import Path

from bson import ObjectId
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from pymongo import MongoClient

# ── 基本設定 ────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
MONGO_URI = "mongodb+srv://11246066:11246066@cluster0.go9e1.mongodb.net/linebot"

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
CORS(app)

# ── MongoDB 連線（應用程式啟動時建立一次）────────────────
client = MongoClient(MONGO_URI)
db = client["linebot"]
col = db["itineraries"]          # 行程集合（含嵌入式 spots）


# ── 工具：把 ObjectId 轉成字串，前端才看得懂 ────────────
def serialize(doc: dict) -> dict:
    doc["_id"] = str(doc["_id"])
    return doc


# ── 首次啟動：若集合是空的就從 data.js 填入種子資料 ──────
def seed_if_empty():
    data_js = BASE_DIR / "src" / "data.js"
    content = data_js.read_text(encoding="utf-8-sig").strip()
    if content.startswith("window.ITINERARIES"):
        content = content.split("=", 1)[1].strip().rstrip(";")
    itineraries = json.loads(content)

    inserted = 0
    for it in itineraries:
        doc = {**it, "_id": it["id"]}   # 用字串 id 當主鍵
        result = col.replace_one({"_id": it["id"]}, doc, upsert=True)
        if result.upserted_id:
            inserted += 1

    if inserted:
        print(f"[seed] 新增 {inserted} 筆靜態行程到 MongoDB linebot.itineraries")


# ── API ─────────────────────────────────────────────────
@app.route("/api/itineraries")
def get_itineraries():
    # 只回傳有 title 欄位的行程文件（過濾舊有格式不符的資料）
    docs = [serialize(d) for d in col.find({"title": {"$exists": True}})]
    return jsonify(docs)


@app.route("/api/itineraries/<itinerary_id>")
def get_itinerary(itinerary_id):
    doc = col.find_one({"_id": itinerary_id})
    if doc is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(serialize(doc))


# ── 靜態頁面 ─────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")


# ── 啟動 ─────────────────────────────────────────────────
if __name__ == "__main__":
    seed_if_empty()
    app.run(debug=True, port=5000)
