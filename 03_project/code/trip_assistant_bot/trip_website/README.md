# 聚餐與出遊行程參考系統

這是學生專題用的前端展示版，可直接雙擊 `index.html` 開啟，不需要後端、不需要 `python -m http.server`、不需要 npm。

## 如何執行

直接雙擊：

```text
index.html
```

也可以開啟交付資料夾：

```text
release/index.html
```

## 交付資料夾

`release/` 內只保留必要檔案：

```text
release
├── index.html
├── assets
│   ├── line-bot-qr.svg
│   └── taiwan-osm-z10.png
└── src
    ├── app.js
    ├── data.js
    └── styles.css
```

## 功能

- 左側完整行程卡片輪播，約每 10 秒自動切換一次。
- 右側台灣互動地圖，可拖曳、滾輪縮放。
- 地圖上有小圓形景點標籤，滑過可看行程摘要，資訊框右上角會顯示 LINE Bot QR Code。
- 點擊卡片或地圖標籤會定位到該行程。
- 篩選功能保留在可收合區塊中。
- 每張行程卡底部有 LINE Bot QR Code 展示區。
- 假資料已內嵌到 `src/data.js`，不再使用 `fetch` 讀取本地 JSON。
- 地圖不再依賴 Leaflet 或外部 CDN，離線也能開啟。

## 未來串接 LINE Bot

目前資料來源是 `src/data.js` 的 `window.ITINERARIES`。未來若要串接後端或 LINE Bot，可以把資料來源改成 API，但交付展示版已固定成可雙擊開啟的純前端版本。

目前 QR Code 是展示用，圖片放在 `assets/line-bot-qr.svg`，連結常數在 `src/app.js` 的 `LINE_BOT_URL`。拿到正式 LINE Bot ID 後，把這兩個地方替換成正式連結即可。
