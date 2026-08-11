(() => {
  "use strict";

  const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;
  const LAUNCH_STORAGE_KEY = "trip_invoice_launch_v1";
  const params = collectLaunchParams();
  const sessionToken = params.get("session_token") || "";
  const liffId = params.get("liff_id") || "";
  const mode = ["camera", "library", "qr"].includes(params.get("mode"))
    ? params.get("mode")
    : "camera";

  const elements = {
    cameraPanel: document.querySelector("#cameraPanel"),
    libraryPanel: document.querySelector("#libraryPanel"),
    cameraInput: document.querySelector("#cameraInput"),
    libraryInput: document.querySelector("#libraryInput"),
    qrButton: document.querySelector("#qrButton"),
    previewPanel: document.querySelector("#previewPanel"),
    previewImage: document.querySelector("#previewImage"),
    submitButton: document.querySelector("#submitButton"),
    resetButton: document.querySelector("#resetButton"),
    statusLine: document.querySelector("#statusLine"),
    statusHint: document.querySelector("#statusHint"),
    closeButton: document.querySelector("#closeButton"),
  };

  let selectedFile = null;
  let previewUrl = "";
  let accessToken = "";
  let busy = false;

  function collectLaunchParams() {
    const result = new URLSearchParams(window.location.search);
    const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    fragment.forEach((value, key) => {
      if (!result.has(key)) result.set(key, value);
    });
    const nestedState = result.get("liff.state");
    if (nestedState) {
      const nestedUrl = new URL(nestedState, window.location.origin);
      nestedUrl.searchParams.forEach((value, key) => {
        if (!result.has(key)) result.set(key, value);
      });
      const nestedFragment = new URLSearchParams(nestedUrl.hash.replace(/^#/, ""));
      nestedFragment.forEach((value, key) => {
        if (!result.has(key)) result.set(key, value);
      });
    }
    try {
      const stored = JSON.parse(window.sessionStorage.getItem(LAUNCH_STORAGE_KEY) || "{}");
      ["session_token", "liff_id", "mode"].forEach((key) => {
        if (!result.has(key) && stored[key]) result.set(key, stored[key]);
      });
      if (result.get("session_token") && result.get("liff_id")) {
        window.sessionStorage.setItem(
          LAUNCH_STORAGE_KEY,
          JSON.stringify({
            session_token: result.get("session_token"),
            liff_id: result.get("liff_id"),
            mode: result.get("mode") || "camera",
          })
        );
      }
    } catch (error) {
      console.warn("Unable to persist invoice launch parameters.", error);
    }
    return result;
  }

  function clearLaunchState() {
    try {
      window.sessionStorage.removeItem(LAUNCH_STORAGE_KEY);
    } catch (error) {
      console.warn("Unable to clear invoice launch parameters.", error);
    }
    window.history.replaceState(null, document.title, window.location.pathname);
  }

  function setStatus(line, hint = "", error = false) {
    elements.statusLine.textContent = line;
    elements.statusHint.textContent = hint;
    elements.statusLine.classList.toggle("error", error);
    elements.statusHint.classList.toggle("error", error);
  }

  function setBusy(value) {
    busy = value;
    elements.submitButton.disabled = value;
    elements.qrButton.disabled = value;
  }

  function resetPreview() {
    selectedFile = null;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = "";
    elements.previewImage.removeAttribute("src");
    elements.previewPanel.classList.add("hidden");
    elements.cameraInput.value = "";
    elements.libraryInput.value = "";
    setStatus("請拍攝或選擇一張發票。", "照片要完整、清楚且避免反光。")
  }

  function selectFile(file) {
    if (!file || !file.type.startsWith("image/")) {
      setStatus("檔案格式不支援。", "請使用 JPEG、PNG 或手機相片。", true);
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setStatus("圖片太大。", "請改用較低解析度重新拍攝。", true);
      return;
    }
    selectedFile = file;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(file);
    elements.previewImage.src = previewUrl;
    elements.previewPanel.classList.remove("hidden");
    setStatus("請確認發票是否清楚完整。", "確認後才會送往雲端辨識。")
  }

  async function postForm() {
    if (!selectedFile || busy) return;
    setBusy(true);
    setStatus("正在辨識發票…", "通常需要數秒，請不要關閉這個畫面。")
    const form = new FormData();
    form.append("session_token", sessionToken);
    form.append("image", selectedFile, "invoice.jpg");
    try {
      const response = await fetch("/api/liff/invoice/recognize", {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
        body: form,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error(payload.error || "辨識失敗");
      clearLaunchState();
      setStatus("辨識完成。", "記帳草稿已送回 LINE 群組，請回群組確認。")
      elements.closeButton.classList.remove("hidden");
      elements.submitButton.classList.add("hidden");
      elements.resetButton.classList.add("hidden");
    } catch (error) {
      console.error(error);
      setStatus("發票辨識失敗。", "請重新拍攝清楚完整的發票。", true);
    } finally {
      setBusy(false);
    }
  }

  async function scanQr() {
    if (busy || !window.liff || !window.liff.isApiAvailable("scanCodeV2")) {
      setStatus("這個裝置無法直接掃描 QR Code。", "請改用拍照或相簿上傳。", true);
      return;
    }
    setBusy(true);
    try {
      const scan = await window.liff.scanCodeV2();
      const response = await fetch("/api/liff/invoice/recognize", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ session_token: sessionToken, qr_payload: scan.value || "" }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error(payload.error || "QR Code 辨識失敗");
      clearLaunchState();
      setStatus("QR Code 辨識完成。", "草稿已送回 LINE 群組。")
      elements.closeButton.classList.remove("hidden");
    } catch (error) {
      console.error(error);
      setStatus("QR Code 辨識失敗。", "請拍攝整張發票改用雲端辨識。", true);
    } finally {
      setBusy(false);
    }
  }

  async function init() {
    if (!sessionToken) {
      setStatus("缺少發票工作階段。", "請回 LINE 群組重新輸入「發票記帳」，不要沿用舊頁面。", true);
      return;
    }
    if (!liffId) {
      setStatus("缺少發票 LIFF ID。", "請確認 Bot 的 LIFF_INVOICE_ID 設定後重新開啟。", true);
      return;
    }
    if (!window.liff) {
      setStatus("LINE LIFF SDK 載入失敗。", "請確認網路後關閉頁面並重新開啟。", true);
      return;
    }
    try {
      await window.liff.init({ liffId });
      if (!window.liff.isLoggedIn()) {
        window.liff.login({ redirectUri: window.location.href });
        return;
      }
      accessToken = window.liff.getAccessToken() || "";
      if (!accessToken) throw new Error("LIFF access token unavailable");
      elements.cameraPanel.classList.toggle("hidden", mode === "library" || mode === "qr");
      elements.libraryPanel.classList.toggle("hidden", mode === "camera" || mode === "qr");
      elements.qrButton.classList.toggle("hidden", mode !== "qr");
      setStatus(
        mode === "qr" ? "請掃描電子發票 QR Code。" : "已準備完成。",
        mode === "qr" ? "若掃描失敗，請回群組改用直接拍照。" : "照片要完整、清楚且避免反光。"
      );
      if (mode === "camera") elements.cameraInput.click();
      if (mode === "library") elements.libraryInput.click();
    } catch (error) {
      console.error(error);
      setStatus("無法啟動發票頁面。", "請回 LINE 群組重新開啟。", true);
    }
  }

  elements.cameraInput.addEventListener("change", (event) => selectFile(event.target.files[0]));
  elements.libraryInput.addEventListener("change", (event) => selectFile(event.target.files[0]));
  elements.submitButton.addEventListener("click", postForm);
  elements.resetButton.addEventListener("click", resetPreview);
  elements.qrButton.addEventListener("click", scanQr);
  elements.closeButton.addEventListener("click", () => {
    clearLaunchState();
    if (window.liff && window.liff.isInClient()) window.liff.closeWindow();
    else window.close();
  });

  init();
})();
