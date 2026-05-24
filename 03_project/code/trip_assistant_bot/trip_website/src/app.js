(function () {
  const AUTOPLAY_DELAY = 10000;
  const TAIWAN_CENTER = [23.75, 120.95];
  const DEFAULT_ZOOM = 8;
  const MOBILE_QUERY = "(max-width: 760px)";
  const LINE_SHARE_URL = "https://line.me/R/share?text=";

  const state = {
    itineraries: normalizeItineraries(Array.isArray(window.ITINERARIES) ? window.ITINERARIES : []),
    filtered: [],
    selectedId: null,
    carouselIndex: 0,
    carouselTimer: null,
    isMobile: window.matchMedia(MOBILE_QUERY).matches,
    map: null,
    markersLayer: null,
    routeLayer: null,
    markerEntries: [],
    sheetHeight: 38,
    sheetSnapPoints: [32, 52, 78],
  };

  const elements = {
    itineraryList: document.querySelector("#itineraryList"),
    resultCount: document.querySelector("#resultCount"),
    totalSpotCount: document.querySelector("#totalSpotCount"),
    spotCount: document.querySelector("#spotCount"),
    mapTitle: document.querySelector("#mapTitle"),
    mobileSheetTitle: document.querySelector("#mobileSheetTitle"),
    mobileSheetMeta: document.querySelector("#mobileSheetMeta"),
    mobileSheetDescription: document.querySelector("#mobileSheetDescription"),
    mobileSheetTags: document.querySelector("#mobileSheetTags"),
    mobileSheetComment: document.querySelector("#mobileSheetComment"),
    mobileLineImportButton: document.querySelector("#mobileLineImportButton"),
    filtersPanel: document.querySelector("#filtersPanel"),
    sidebar: document.querySelector(".sidebar"),
    listSection: document.querySelector(".itinerary-list-section"),
    sheetHandle: document.querySelector(".sheet-handle"),
    prevItinerary: document.querySelector("#prevItinerary"),
    nextItinerary: document.querySelector("#nextItinerary"),
    carouselStatus: document.querySelector("#carouselStatus"),
    map: document.querySelector("#map"),
  };

  const filters = {
    region: document.querySelector("#regionFilter"),
    budget: document.querySelector("#budgetFilter"),
    distance: document.querySelector("#distanceFilter"),
    type: document.querySelector("#typeFilter"),
    transport: document.querySelector("#transportFilter"),
  };

  const resetFilters = document.querySelector("#resetFilters");

  function init() {
    state.filtered = [...state.itineraries];
    state.selectedId = state.filtered[0]?.id ?? null;
    setupResponsiveBehavior();
    setupMap();
    setupFilters();
    setupCarouselControls();
    setupBottomSheetControls();
    setupLineImportActions();
    renderAll();
    resetMobileSheetScroll();
    fitSelectedAfterPaint(false);
    restartCarouselAutoplay();
  }

  function setupResponsiveBehavior() {
    const mobileMedia = window.matchMedia(MOBILE_QUERY);
    elements.filtersPanel.open = !mobileMedia.matches;

    mobileMedia.addEventListener("change", (event) => {
      state.isMobile = event.matches;
      elements.filtersPanel.open = !event.matches;
      renderMapMarkers();
      updateCarousel();
      fitSelectedAfterPaint(false);
      restartCarouselAutoplay();
    });
  }

  function setupMap() {
    if (!window.L) {
      elements.map.innerHTML = '<div class="map-error">地圖載入失敗，請確認網路連線後重新整理。</div>';
      return;
    }

    state.map = L.map(elements.map, {
      center: TAIWAN_CENTER,
      zoom: DEFAULT_ZOOM,
      minZoom: 7,
      maxZoom: 18,
      scrollWheelZoom: true,
      zoomControl: true,
    });

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(state.map);

    state.markersLayer = L.layerGroup().addTo(state.map);
    state.routeLayer = L.layerGroup().addTo(state.map);

    window.addEventListener("resize", () => fitSelectedAfterPaint(false));
  }

  function setupFilters() {
    populateSelect(filters.region, uniqueOptions("region"));
    populateSelect(filters.budget, uniqueOptions("budget"));
    populateSelect(filters.distance, uniqueOptions("distance"));
    populateSelect(filters.type, uniqueOptions("type"));
    populateSelect(filters.transport, uniqueOptions("transport"));

    Object.values(filters).forEach((select) => {
      select.addEventListener("change", applyFilters);
    });

    resetFilters.addEventListener("click", () => {
      Object.values(filters).forEach((select) => {
        select.value = "all";
      });
      applyFilters();
    });
  }

  function setupCarouselControls() {
    elements.prevItinerary.addEventListener("click", () => {
      moveCarousel(-1);
      selectVisibleItinerary(false);
      restartCarouselAutoplay();
    });

    elements.nextItinerary.addEventListener("click", () => {
      moveCarousel(1);
      selectVisibleItinerary(false);
      restartCarouselAutoplay();
    });
  }

  function setupBottomSheetControls() {
    if (!elements.sidebar || !elements.sheetHandle) return;

    const setHeight = (height) => {
      state.sheetHeight = Math.max(28, Math.min(82, height));
      elements.sidebar.style.setProperty("--sheet-height", `${state.sheetHeight}dvh`);
    };

    const snapHeight = (height) => {
      return state.sheetSnapPoints.reduce((closest, point) => {
        return Math.abs(point - height) < Math.abs(closest - height) ? point : closest;
      }, state.sheetSnapPoints[0]);
    };

    setHeight(state.sheetHeight);

    elements.sheetHandle.addEventListener("click", () => {
      setHeight(state.sheetHeight < 62 ? 78 : 46);
      if (state.sheetHeight <= 46 && elements.listSection) {
        elements.listSection.scrollTop = 0;
      }
      fitSelectedAfterPaint(false);
    });

    elements.sheetHandle.addEventListener("pointerdown", (event) => {
      if (!state.isMobile) return;
      event.preventDefault();

      const startY = event.clientY;
      const startHeight = state.sheetHeight;
      elements.sheetHandle.setPointerCapture(event.pointerId);
      elements.sidebar.classList.add("is-dragging");

      const onMove = (moveEvent) => {
        const delta = startY - moveEvent.clientY;
        setHeight(startHeight + (delta / window.innerHeight) * 100);
      };

      const onUp = () => {
        elements.sidebar.classList.remove("is-dragging");
        setHeight(snapHeight(state.sheetHeight));
        if (state.sheetHeight <= 46 && elements.listSection) {
          elements.listSection.scrollTop = 0;
        }
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
        fitSelectedAfterPaint(false);
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    });
  }

  function setupLineImportActions() {
    document.addEventListener("click", async (event) => {
      if (!(event.target instanceof Element)) return;
      const trigger = event.target.closest("[data-line-import]");
      if (!trigger) return;

      event.preventDefault();
      const payload = trigger.dataset.linePayload;
      if (!payload) return;

      const payloadData = parseLinePayload(payload);
      if (!payloadData) {
        showImportFeedback(trigger, "資料異常");
        window.alert("這份行程資料目前無法匯入 LINE，請稍後再試。");
        return;
      }

      const shareText = buildLineShareText(payloadData);
      await copyLineImportPayload(shareText);
      showImportFeedback(
        trigger,
        isProbablyMobileDevice() ? "正在開啟 LINE" : "已複製匯入訊息"
      );
      await openLineShareTarget(shareText);
    });
  }

  function applyFilters() {
    state.filtered = state.itineraries.filter((item) => {
      return (
        matchesFilter(item, "region", filters.region.value) &&
        matchesFilter(item, "budget", filters.budget.value) &&
        matchesFilter(item, "distance", filters.distance.value) &&
        matchesFilter(item, "type", filters.type.value) &&
        matchesFilter(item, "transport", filters.transport.value)
      );
    });

    if (!state.filtered.some((item) => item.id === state.selectedId)) {
      state.selectedId = state.filtered[0]?.id ?? null;
      state.carouselIndex = 0;
    }

    renderAll();

    const selected = getSelectedItinerary();
    if (selected) {
      fitSelectedAfterPaint(false);
    } else if (state.map) {
      state.map.setView(TAIWAN_CENTER, DEFAULT_ZOOM);
    }

    restartCarouselAutoplay();
  }

  function renderAll() {
    renderStats();
    renderCarousel();
    renderMapMarkers();
  }

  function renderStats() {
    const spotTotal = countSpots(state.filtered);
    elements.resultCount.textContent = `${state.filtered.length} 筆行程`;
    elements.totalSpotCount.textContent = `${spotTotal} 個景點`;
    if (elements.spotCount) {
      elements.spotCount.textContent = `${spotTotal} 個景點`;
    }

    const selected = getSelectedItinerary();
    if (elements.mapTitle) {
      elements.mapTitle.textContent = selected ? selected.title : "點選行程即可查看路線";
    }
    if (elements.mobileSheetTitle) {
      elements.mobileSheetTitle.textContent = selected ? selected.title : "台灣一日遊行程推薦";
    }
    if (elements.mobileSheetMeta) {
      elements.mobileSheetMeta.textContent = selected
        ? `${selected.region} / ${selected.duration} / ${selected.type}`
        : "推薦行程";
    }
    if (elements.mobileSheetDescription) {
      elements.mobileSheetDescription.textContent = selected
        ? selected.description
        : "選擇喜歡的路線，查看景點順序與行程資訊。";
    }
    if (elements.mobileSheetTags) {
      elements.mobileSheetTags.innerHTML = selected
        ? [selected.transport, selected.budget, selected.distance, selected.type].map((tag) => `<span>${html(tag)}</span>`).join("")
        : "";
    }
    if (elements.mobileSheetComment) {
      elements.mobileSheetComment.textContent = selected ? selected.comment : "選一條行程後會顯示推薦理由。";
    }
    if (elements.mobileLineImportButton) {
      if (selected) {
        elements.mobileLineImportButton.dataset.linePayload = createItineraryImportPayload(selected);
        elements.mobileLineImportButton.disabled = false;
        elements.mobileLineImportButton.setAttribute(
          "aria-label",
          `分享到 LINE 群組，匯入 ${selected.title}`
        );
      } else {
        delete elements.mobileLineImportButton.dataset.linePayload;
        elements.mobileLineImportButton.disabled = true;
        elements.mobileLineImportButton.setAttribute("aria-label", "目前沒有可匯入的行程");
      }
    }
  }
  function renderCarousel() {
    if (state.filtered.length === 0) {
      elements.itineraryList.innerHTML = '<div class="empty-state">找不到符合條件的行程，請調整篩選條件。</div>';
      updateCarousel();
      return;
    }

    elements.itineraryList.innerHTML = state.filtered.map(createItineraryCard).join("");

    elements.itineraryList.querySelectorAll(".itinerary-card").forEach((card) => {
      const chooseCard = () => selectItinerary(card.dataset.itineraryId, true);
      card.addEventListener("click", (event) => {
        if (event.target.closest("[data-line-import]")) return;
        chooseCard();
      });
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          chooseCard();
        }
      });
    });

    if (state.isMobile && elements.listSection) {
      resetMobileSheetScroll();
    }

    updateCarousel();
  }

  function createItineraryCard(item) {
    const activeClass = item.id === state.selectedId ? " active" : "";
    const payload = createItineraryImportPayload(item);
    return `
      <article
        class="itinerary-card${activeClass}"
        data-itinerary-id="${html(item.id)}"
        data-line-key="${html(item.lineBotKey)}"
        role="button"
        tabindex="0"
      >
        <div class="card-topline">
          <p class="eyebrow">${html(item.region)} / ${html(item.duration)}</p>
        </div>
        <h2>${html(item.title)}</h2>
        <p class="card-description">${html(item.description)}</p>

        <div class="card-meta-grid">
          ${createMetaBox("地區", item.region)}
          ${createMetaBox("預算", item.budget)}
          ${createMetaBox("距離", item.distance)}
          ${createMetaBox("類型", item.type)}
          ${createMetaBox("交通", item.transport)}
        </div>

        <p class="card-line"><strong>適合：</strong>${html(item.bestFor)}</p>
        <p class="card-line"><strong>行程時間：</strong>${html(item.duration)}</p>

        <div class="comment-box">
          <span>推薦理由</span>
          <p>${html(item.comment)}</p>
        </div>

        <div class="line-bot-panel" aria-label="LINE Bot 匯入行程">
          <div>
            <span>LINE Bot</span>
            <strong>分享到 LINE 群組並匯入行程</strong>
            <p>按下後會開啟 LINE 分享，選好群組送出後，群組裡的 AI 會立刻依這份路線提供建議。</p>
          </div>
          <button
            class="line-import-button"
            type="button"
            data-line-import
            data-line-payload="${html(payload)}"
          >分享到 LINE 群組</button>
        </div>

        <h3>景點順序</h3>
        <ol class="spot-list">
          ${item.spots.map((spot) => createSpotItem(item, spot)).join("")}
        </ol>
      </article>
    `;
  }

  function createSpotItem(itinerary, spot) {
    const payload = createSpotImportPayload(itinerary, spot);
    return `
      <li data-itinerary-id="${html(itinerary.id)}" data-spot-id="${html(spot.id)}">
        <div>
          <strong>${html(spot.name)}</strong>
          <span>${html(spot.description)}</span>
        </div>
        <button
          class="spot-import-button"
          type="button"
          data-line-import
          data-line-payload="${html(payload)}"
          aria-label="分享到 LINE 群組，匯入 ${html(spot.name)}"
        >分享至 LINE</button>
      </li>
    `;
  }
  function renderMapMarkers() {
    if (!state.map || !state.markersLayer || !state.routeLayer) return;

    state.markersLayer.clearLayers();
    state.routeLayer.clearLayers();
    state.markerEntries = [];

    state.filtered.forEach((itinerary) => {
      const isActive = itinerary.id === state.selectedId;

      itinerary.spots.forEach((spot, spotIndex) => {
        if (!isValidSpot(spot)) return;

        const marker = L.marker([spot.lat, spot.lng], {
          icon: createMarkerIcon(itinerary, spotIndex, isActive),
          title: `${itinerary.title} - ${spot.name}`,
        });

        marker.bindPopup(createPopupContent(itinerary, spot), {
          maxWidth: state.isMobile ? 260 : 320,
          className: state.isMobile ? "travel-popup mobile-popup" : "travel-popup",
          autoPanPaddingTopLeft: state.isMobile ? [18, 86] : [20, 96],
          autoPanPaddingBottomRight: state.isMobile ? [18, Math.round(window.innerHeight * 0.5)] : [20, 20],
        });

        marker.on("click", () => {
          selectItinerary(itinerary.id, false);
          if (state.isMobile) {
            elements.sidebar?.style.setProperty("--sheet-height", "42dvh");
            state.sheetHeight = 42;
          }
          window.setTimeout(() => {
            const refreshedMarker = state.markerEntries.find((entry) => {
              return entry.itineraryId === itinerary.id && entry.spotId === spot.id;
            });
            refreshedMarker?.marker.openPopup();
          }, 0);
        });

        marker.addTo(state.markersLayer);
        state.markerEntries.push({ itineraryId: itinerary.id, spotId: spot.id, marker });
      });
    });

    drawSelectedRoute();
  }

  function drawSelectedRoute() {
    if (!state.map || !state.routeLayer) return;
    state.routeLayer.clearLayers();

    const itinerary = getSelectedItinerary();
    if (!itinerary) return;

    const points = itinerary.spots.filter(isValidSpot).map((spot) => [spot.lat, spot.lng]);
    if (points.length < 2) return;

    L.polyline(points, {
      color: "#ef7b45",
      weight: 5,
      opacity: 0.92,
      dashArray: "10 8",
      lineJoin: "round",
    }).addTo(state.routeLayer);
  }

  function selectItinerary(id, focusMap = true) {
    state.selectedId = id;
    const selectedIndex = state.filtered.findIndex((item) => item.id === id);
    if (selectedIndex >= 0) {
      state.carouselIndex = selectedIndex;
    }

    renderStats();
    renderCarousel();
    renderMapMarkers();

    const itinerary = getSelectedItinerary();
    if (focusMap && itinerary) {
      fitItineraryBounds(itinerary, true);
      document.querySelector(".map-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    restartCarouselAutoplay();
  }

  function selectVisibleItinerary(focusMap) {
    const visible = state.filtered[state.carouselIndex];
    if (visible) {
      selectItinerary(visible.id, focusMap);
    }
  }

  function fitSelectedAfterPaint(openFirstPopup) {
    window.setTimeout(() => {
      if (!state.map) return;
      state.map.invalidateSize();
      const itinerary = getSelectedItinerary();
      if (itinerary) {
        fitItineraryBounds(itinerary, openFirstPopup);
      }
    }, 120);
  }

  function fitItineraryBounds(itinerary, openFirstPopup) {
    if (!state.map) return;

    const points = itinerary.spots.filter(isValidSpot).map((spot) => [spot.lat, spot.lng]);
    if (points.length === 0) return;

    const bounds = L.latLngBounds(points);
    state.map.fitBounds(bounds, {
      paddingTopLeft: state.isMobile ? [22, 96] : [36, 120],
      paddingBottomRight: [32, 32],
      maxZoom: state.isMobile ? 12 : 13,
      animate: true,
    });

    if (openFirstPopup) {
      window.setTimeout(() => {
        const firstMarker = state.markerEntries.find((entry) => entry.itineraryId === itinerary.id);
        firstMarker?.marker.openPopup();
      }, 260);
    }
  }

  function updateCarousel() {
    const total = state.filtered.length;
    if (state.carouselIndex >= total) state.carouselIndex = Math.max(0, total - 1);

    elements.itineraryList.style.transform = `translateX(-${state.carouselIndex * 100}%)`;
    elements.carouselStatus.textContent = total === 0 ? "0 / 0" : `${state.carouselIndex + 1} / ${total}`;

    elements.prevItinerary.disabled = total <= 1;
    elements.nextItinerary.disabled = total <= 1;
  }

  function moveCarousel(direction) {
    if (state.filtered.length === 0) return;

    const total = state.filtered.length;
    state.carouselIndex = (state.carouselIndex + direction + total) % total;
    updateCarousel();
  }

  function restartCarouselAutoplay() {
    window.clearInterval(state.carouselTimer);
    state.carouselTimer = null;

    if (state.isMobile || state.filtered.length <= 1) return;

    state.carouselTimer = window.setInterval(() => {
      moveCarousel(1);
      selectVisibleItinerary(false);
    }, AUTOPLAY_DELAY);
  }

  function createMarkerIcon(itinerary, spotIndex, isActive) {
    const variant = getMarkerVariant(itinerary);
    return L.divIcon({
      className: "",
      html: `<span class="map-marker-dot ${variant.className}${isActive ? " active" : ""}" aria-hidden="true">${variant.label}</span>`,
      iconSize: [34, 34],
      iconAnchor: [17, 17],
      popupAnchor: [0, -18],
    });
  }
  function resetMobileSheetScroll() {
    if (!state.isMobile || !elements.listSection) return;
    elements.listSection.scrollTop = 0;
    window.setTimeout(() => {
      elements.listSection.scrollTop = 0;
    }, 80);
    window.setTimeout(() => {
      elements.listSection.scrollTop = 0;
    }, 260);
  }

  function createPopupContent(itinerary, spot) {
    const payload = createSpotImportPayload(itinerary, spot);
    if (state.isMobile) {
      return `
        <div class="popup-card mobile-spot-card" data-itinerary-id="${html(itinerary.id)}" data-spot-id="${html(spot.id)}">
          <p class="popup-kicker">${html(itinerary.title)}</p>
          <h3>${html(spot.name)}</h3>
          <p>${html(spot.description)}</p>
          <button
            class="line-import-button"
            type="button"
            data-line-import
            data-line-payload="${html(payload)}"
          >分享至 LINE 群組</button>
        </div>
      `;
    }

    return `
      <div class="popup-card" data-itinerary-id="${html(itinerary.id)}" data-spot-id="${html(spot.id)}">
        <p class="popup-kicker">${html(itinerary.title)}</p>
        <h3>${html(spot.name)}</h3>
        <p>${html(spot.description)}</p>
        <dl>
          <div><dt>行程</dt><dd>${html(itinerary.duration)}</dd></div>
          <div><dt>類型</dt><dd>${html(itinerary.type)}</dd></div>
          <div><dt>交通</dt><dd>${html(itinerary.transport)}</dd></div>
        </dl>
        <div class="popup-actions">
          <button
            class="line-import-button"
            type="button"
            data-line-import
            data-line-payload="${html(payload)}"
          >分享至 LINE 群組</button>
        </div>
      </div>
    `;
  }
  function createItineraryImportPayload(itinerary) {
    return JSON.stringify({
      kind: "travel_itinerary_import",
      version: 1,
      itinerary_id: itinerary.id,
      title: itinerary.title,
      region: itinerary.region,
      budget: itinerary.budget,
      distance: itinerary.distance,
      type: itinerary.type,
      transport: itinerary.transport,
      duration: itinerary.duration,
      summary: itinerary.summary,
      description: itinerary.description,
      bestFor: itinerary.bestFor,
      comment: itinerary.comment,
      spots: itinerary.spots.map((spot) => ({
        spot_id: spot.id,
        sequence: spot.sequence,
        name: spot.name,
        description: spot.description,
      })),
    });
  }

  function createSpotImportPayload(itinerary, spot) {
    return JSON.stringify({
      kind: "travel_spot_import",
      version: 1,
      itinerary_id: itinerary.id,
      itinerary_title: itinerary.title,
      spot_id: spot.id,
      sequence: spot.sequence,
      spot_name: spot.name,
      spot_description: spot.description,
      next_prompt: `下一站是 ${spot.name}，可以提醒使用者怎麼前往或附近有什麼可做。`,
    });
  }
  function parseLinePayload(payloadText) {
    try {
      return JSON.parse(payloadText);
    } catch (error) {
      console.warn("LINE import payload parse failed", error);
      return null;
    }
  }

  function buildLineShareText(payloadData) {
    if (payloadData.kind === "travel_spot_import") {
      return [
        "【Trip Assistant 景點同步】",
        `${payloadData.itinerary_title || "目前行程"} / ${payloadData.spot_name || "景點"}`,
        "請把群組目前討論焦點切到這一站，並接著提供下一站建議。",
      ].join("\n");
    }

    const routePreview = Array.isArray(payloadData.spots)
      ? payloadData.spots.slice(0, 4).map((spot) => spot.name).join(" -> ")
      : "";

    return [
      "【Trip Assistant 行程匯入】",
      payloadData.title || "未命名行程",
      routePreview ? `路線：${routePreview}` : "",
      "請 AI 旅遊行程助理匯入這份群組行程，之後依這份內容提供建議。",
    ].filter(Boolean).join("\n");
  }

  function isProbablyMobileDevice() {
    if (navigator.userAgentData && typeof navigator.userAgentData.mobile === "boolean") {
      return navigator.userAgentData.mobile;
    }
    return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
  }

  async function openLineShareTarget(shareText) {
    const shareUrl = `${LINE_SHARE_URL}${encodeURIComponent(shareText)}`;

    if (isProbablyMobileDevice()) {
      window.location.href = shareUrl;
      return true;
    }

    if (navigator.share) {
      try {
        await navigator.share({
          title: "分享到 LINE 群組",
          text: shareText,
        });
        return true;
      } catch (error) {
        if (error && error.name === "AbortError") {
          return false;
        }
        console.warn("Web Share failed", error);
      }
    }

    window.alert(
      "已複製匯入訊息。LINE 群組分享目前以手機操作最順，請在手機上開啟本頁，或把剛剛複製的訊息貼到群組裡送出。"
    );
    return false;
  }

  async function copyLineImportPayload(payload) {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(payload);
        return;
      } catch (error) {
        console.warn("Clipboard write failed", error);
      }
    }

    const textarea = document.createElement("textarea");
    textarea.value = payload;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.top = "-999px";
    textarea.style.left = "-999px";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }

  function showImportFeedback(trigger, temporaryText = "已準備匯入") {
    const button = trigger.matches("button") ? trigger : trigger.closest("button");
    if (!button) return;

    const originalText = button.textContent;
    button.textContent = temporaryText;
    button.classList.add("is-copied");
    window.setTimeout(() => {
      button.textContent = originalText;
      button.classList.remove("is-copied");
    }, 1400);
  }
  function getMarkerVariant(itinerary) {
    const type = itinerary.type;
    if (type === "美食") return { className: "food", label: "吃" };
    if (type === "海線") return { className: "water", label: "海" };
    if (type === "河岸") return { className: "water", label: "水" };
    if (type === "文化") return { className: "culture", label: "文" };
    if (type === "都市") return { className: "city", label: "市" };
    if (type === "自然" || type === "山城") return { className: "nature", label: "景" };
    return { className: "city", label: "景" };
  }
  function normalizeItineraries(itineraries) {
    return itineraries.map((itinerary) => {
      const id = itinerary.id || slugify(itinerary.title);
      return {
        ...itinerary,
        id,
        lineBotKey: `linebot:${id}`,
        spots: (itinerary.spots || []).map((spot, index) => ({
          ...spot,
          id: spot.id || `${id}-spot-${String(index + 1).padStart(2, "0")}`,
          sequence: index + 1,
        })),
      };
    });
  }

  function uniqueOptions(key) {
    return [...new Set(state.itineraries.map((item) => item[key]))].filter(Boolean);
  }

  function populateSelect(select, options) {
    options.forEach((option) => {
      const element = document.createElement("option");
      element.value = option;
      element.textContent = option;
      select.appendChild(element);
    });
  }

  function matchesFilter(item, key, value) {
    return value === "all" || item[key] === value;
  }

  function createMetaBox(label, value) {
    return `
      <div class="meta-box">
        <span>${html(label)}</span>
        <strong>${html(value)}</strong>
      </div>
    `;
  }

  function getSelectedItinerary() {
    return state.filtered.find((item) => item.id === state.selectedId) ?? null;
  }

  function countSpots(itineraries) {
    return itineraries.reduce((total, itinerary) => total + itinerary.spots.length, 0);
  }

  function isValidSpot(spot) {
    return Number.isFinite(spot?.lat) && Number.isFinite(spot?.lng);
  }

  function slugify(value) {
    return String(value ?? "")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, "-")
      .replace(/[^\w-]+/g, "")
      .replace(/--+/g, "-")
      .replace(/^-|-$/g, "");
  }

  function html(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => {
      const entities = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;",
      };
      return entities[char];
    });
  }

  init();
})();
