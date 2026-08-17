(function () {
  const elements = {
    statusLine: document.querySelector("#statusLine"),
    statusHint: document.querySelector("#statusHint"),
    retryButton: document.querySelector("#retryButton"),
    closeButton: document.querySelector("#closeButton"),
    syncNote: document.querySelector("#syncNote"),
    results: document.querySelector("#results"),
  };

  const launchParams = parseLaunchParams();
  const sessionToken = launchParams.get("session_token") || "";
  const liffId = launchParams.get("liff_id") || "";

  let isSubmitting = false;

  function parseLaunchParams() {
    const params = new URLSearchParams(window.location.search);
    const nestedState = params.get("liff.state") || "";
    if (!nestedState) {
      return params;
    }

    const decodedState = nestedState.startsWith("?")
      ? nestedState.slice(1)
      : nestedState;
    const nestedParams = new URLSearchParams(decodedState);

    if (!params.get("session_token") && nestedParams.get("session_token")) {
      params.set("session_token", nestedParams.get("session_token"));
    }
    if (!params.get("liff_id") && nestedParams.get("liff_id")) {
      params.set("liff_id", nestedParams.get("liff_id"));
    }

    return params;
  }

  function setStatus(line, hint, isError = false) {
    elements.statusLine.textContent = line;
    elements.statusHint.textContent = hint || "";
    elements.statusHint.classList.toggle("error", Boolean(isError));
  }

  function showRetry(show) {
    elements.retryButton.classList.toggle("hidden", !show);
  }

  function showClose(show) {
    elements.closeButton.classList.toggle("hidden", !show);
  }

  function renderResults(payload) {
    const results = Array.isArray(payload.results) ? payload.results : [];
    elements.results.innerHTML = "";

    if (!results.length) {
      const card = document.createElement("article");
      card.className = "result-card";
      card.innerHTML = `
        <h2>目前沒有更多推薦項目</h2>
        <p>${escapeHtml(payload.group_message || "請稍後再試一次。")}</p>
      `;
      elements.results.appendChild(card);
      return;
    }

    results.forEach((item, index) => {
      const card = document.createElement("article");
      card.className = "result-card";

      const subtitle = item.subtitle ? `<p>${escapeHtml(item.subtitle)}</p>` : "";
      const description = item.description
        ? `<p class="meta">${escapeHtml(item.description)}</p>`
        : "";
      const distance = item.distance_km != null
        ? `<p class="meta">距離約 ${Number(item.distance_km).toFixed(2)} 公里</p>`
        : "";
      const address = item.address ? `<p class="meta">${escapeHtml(item.address)}</p>` : "";
      const mapsLink = item.maps_url
        ? `<p class="meta"><a href="${encodeURI(item.maps_url)}" target="_blank" rel="noopener noreferrer">查看地圖</a></p>`
        : "";

      card.innerHTML = `
        <h2>${index + 1}. ${escapeHtml(item.name || "未命名推薦")}</h2>
        ${subtitle}
        ${description}
        ${distance}
        ${address}
        ${mapsLink}
      `;
      elements.results.appendChild(card);
    });
  }

  function renderSyncState(payload) {
    const synced = payload.synced_to_group !== false;
    elements.syncNote.textContent = synced
      ? "推薦結果已同步到原本的 LINE 群組。"
      : "推薦結果已顯示在這裡，但同步到群組時發生問題。";
    elements.syncNote.classList.remove("hidden");
  }

  function requestPosition() {
    if (isSubmitting) return;

    if (!navigator.geolocation) {
      setStatus("這個裝置不支援定位功能。", "請改用其他裝置或瀏覽器。", true);
      showRetry(true);
      return;
    }

    if (!window.isSecureContext) {
      setStatus("定位功能需要 HTTPS 網址。", "請改用正式部署的 LIFF 網址。", true);
      showRetry(false);
      return;
    }

    isSubmitting = true;
    showRetry(false);
    setStatus("正在取得目前位置...", "請稍候，通常幾秒內會完成。");

    navigator.geolocation.getCurrentPosition(
      async (position) => {
        setStatus("已取得位置，正在整理推薦...", "接著會同步一份摘要到群組。");
        try {
          await submitLocation(position);
        } catch (error) {
          console.error(error);
          setStatus("定位已取得，但推薦整理失敗。", "請稍後重新嘗試。", true);
          showRetry(true);
        } finally {
          isSubmitting = false;
        }
      },
      (error) => {
        console.error(error);
        isSubmitting = false;
        setStatus("無法取得定位。", translateGeolocationError(error), true);
        showRetry(true);
      },
      {
        enableHighAccuracy: true,
        maximumAge: 10000,
        timeout: 15000,
      }
    );
  }

  async function submitLocation(position) {
    const accessToken =
      window.liff && typeof window.liff.getAccessToken === "function"
        ? window.liff.getAccessToken() || ""
        : "";
    const idToken =
      window.liff && typeof window.liff.getIDToken === "function"
        ? window.liff.getIDToken() || ""
        : "";

    const response = await fetch("/api/liff/location/recommendation", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      body: JSON.stringify({
        session_token: sessionToken,
        id_token: idToken,
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
        accuracy: position.coords.accuracy,
      }),
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "Recommendation request failed");
    }

    renderResults(payload);
    renderSyncState(payload);
    showClose(true);
    setStatus("推薦已整理完成。", "你可以直接在這裡查看，也可以回群組看同步摘要。");
  }

  async function init() {
    if (!sessionToken) {
      setStatus("缺少 session token。", "請回到 LINE 群組重新點一次定位連結。", true);
      showRetry(false);
      return;
    }

    if (!liffId) {
      setStatus("缺少 LIFF ID。", "請先在 .env 設定 LIFF_ID。", true);
      showRetry(false);
      return;
    }

    if (!window.liff) {
      setStatus("LIFF SDK 載入失敗。", "請確認網路連線後重新開啟。", true);
      showRetry(true);
      return;
    }

    try {
      await window.liff.init({
        liffId,
        withLoginOnExternalBrowser: false,
      });
    } catch (error) {
      console.error(error);
      setStatus("LIFF 初始化失敗。", "請重新開啟定位頁。", true);
      showRetry(true);
      return;
    }

    showClose(window.liff.isInClient());
    requestPosition();
  }

  function translateGeolocationError(error) {
    if (!error || typeof error.code !== "number") {
      return "請檢查瀏覽器或系統的定位權限設定。";
    }
    switch (error.code) {
      case 1:
        return "你已拒絕定位權限，請允許後再試一次。";
      case 2:
        return "裝置暫時無法判定位置，請移到訊號較好的地方再試。";
      case 3:
        return "定位逾時，請稍後再試一次。";
      default:
        return "請檢查瀏覽器或系統的定位權限設定。";
    }
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  elements.retryButton.addEventListener("click", requestPosition);
  elements.closeButton.addEventListener("click", () => {
    if (window.liff && window.liff.isInClient()) {
      window.liff.closeWindow();
      return;
    }
    window.close();
  });

  init();
})();
