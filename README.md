# Sky Gate

Sky Gate 是一個可直接在瀏覽器開啟的 HTML5 小遊戲原型，核心體驗類似 Flappy Bird：

- `Space` 控制角色上升
- 穿過能量門即可得分
- 碰撞天花板、地面或障礙物會立即結束

## 快速啟動

1. 直接以瀏覽器開啟 `index.html`
2. 按下 `Space` 開始遊戲
3. 再次按下 `Space` 維持飛行

## 檔案結構

```text
Codex/
├─ index.html      # 頁面結構與 HUD
├─ style.css       # 版面與視覺樣式
├─ game.js         # 遊戲主迴圈、碰撞、計分與輸入
└─ README.md       # 操作說明
```

## 核心介面與模組

### `config`

集中管理遊戲參數：

- 畫布尺寸
- 重力與起飛速度
- 障礙物寬度、間距、門縫大小
- 角色半徑與地面高度

### `state`

保存執行時狀態：

- `mode`: `ready | running | gameover`
- `score` / `best`
- `player`: 位置、速度、旋轉
- `pipes`: 障礙物資料
- `clouds`: 背景演出資料

### 核心函式

- `startRound()`: 開始新回合
- `flap()`: 處理飛行輸入
- `update(delta)`: 推進物理與場景
- `updatePipes(delta)`: 障礙物移動與回收
- `checkCollisions()`: 碰撞判定
- `draw()`: 每幀繪製畫面

## 開發計畫

### MVP（目前已完成）

- 單頁 HTML 可直接啟動
- 空白鍵飛行控制
- 隨機障礙物生成
- 穿門得分
- 碰撞失敗與重新開始
- 本地最高分儲存
- 基本 HUD 與狀態提示

### Phase 2

- 開始選單與暫停功能
- 音效、粒子、鏡頭震動
- 多組角色外觀或主題地圖
- 難度曲線調整

### Phase 3

- 手機觸控優化與 PWA
- 排行榜串接
- 任務系統與成就
- 關卡編輯器或自訂障礙節奏

## 後續文件建議

若要擴大成正式專案，建議新增：

- `docs/game-design.md`: 規則、節奏、難度曲線
- `docs/technical-design.md`: 模組分層、渲染策略、儲存方案
- `docs/interfaces.md`: UI 事件、資料結構、儲存鍵值
- `docs/roadmap.md`: 里程碑與驗收條件
