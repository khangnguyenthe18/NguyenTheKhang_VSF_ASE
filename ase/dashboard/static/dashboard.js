/* ASE BSS NOC Dashboard JS — ECharts 5.5.1 + Leaflet 1.9.4
 * Nguyên tắc bảo mật (DASHBOARD_PLAN §2.6): chỉ dùng textContent, tuyệt đối không dùng innerHTML với dữ liệu.
 * Bố cục widget grid console IoT (DASHBOARD_UI_PLAN §1, §3).
 */
(function () {
  "use strict";

  var activeEchartsInstances = [];
  var globalLeafletMap = null;

  function parsePageData() {
    var el = document.getElementById("page-data");
    if (!el || !el.textContent) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (_e) {
      return null;
    }
  }

  function initFilterControls(_pageData) {
    var slider = document.getElementById("asof-slider");
    var input = document.getElementById("filter-asof");
    var resetBtn = document.getElementById("btn-reset-asof");
    var form = document.getElementById("global-filter-form");
    var profileSelect = document.getElementById("filter-profile");
    var countrySelect = document.getElementById("filter-country");

    if (slider && input) {
      slider.addEventListener("input", function () {
        var sec = parseInt(slider.value, 10);
        if (!isNaN(sec)) {
          var iso = new Date(sec * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
          input.value = iso;
        }
      });
    }

    // Không gửi `as_of=` rỗng lên URL: tắt ô trống trước khi submit (ô disabled không được gửi).
    function submitFilters() {
      if (input && !input.value.trim()) input.disabled = true;
      form.submit();
    }

    if (form) {
      form.addEventListener("submit", function () {
        if (input && !input.value.trim()) input.disabled = true;
      });
    }

    if (resetBtn && input && form) {
      resetBtn.addEventListener("click", function () {
        input.value = "";
        submitFilters();
      });
    }

    if (profileSelect && form && input) {
      profileSelect.addEventListener("change", function () {
        input.value = "";
        submitFilters();
      });
    }

    if (countrySelect && form) {
      countrySelect.addEventListener("change", function () {
        submitFilters();
      });
    }
  }

  /* --- Fullscreen API & Widget Actions (DASHBOARD_UI_PLAN §3) -------------- */

  function setupWidgetFullscreen() {
    var buttons = document.querySelectorAll(".btn-widget-fullscreen");
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var card = btn.closest(".widget-card");
        if (!card) return;

        if (card.classList.contains("is-fullscreen")) {
          card.classList.remove("is-fullscreen");
          if (document.fullscreenElement) {
            document.exitFullscreen().catch(function () { });
          }
        } else {
          card.classList.add("is-fullscreen");
          if (card.requestFullscreen) {
            card.requestFullscreen().catch(function () { });
          }
        }

        setTimeout(function () {
          activeEchartsInstances.forEach(function (c) {
            try { c.resize(); } catch (_e) { }
          });
          if (globalLeafletMap) {
            try { globalLeafletMap.invalidateSize(); } catch (_e) { }
          }
        }, 150);
      });
    });

    document.addEventListener("fullscreenchange", function () {
      if (!document.fullscreenElement) {
        document.querySelectorAll(".widget-card.is-fullscreen").forEach(function (el) {
          el.classList.remove("is-fullscreen");
        });
      }
      setTimeout(function () {
        activeEchartsInstances.forEach(function (c) {
          try { c.resize(); } catch (_e) { }
        });
        if (globalLeafletMap) {
          try { globalLeafletMap.invalidateSize(); } catch (_e) { }
        }
      }, 100);
    });

    window.addEventListener("resize", function () {
      activeEchartsInstances.forEach(function (c) {
        try { c.resize(); } catch (_e) { }
      });
      if (globalLeafletMap) {
        try { globalLeafletMap.invalidateSize(); } catch (_e) { }
      }
    });
  }

  /* --- Bản đồ nguồn tủ/trạm VinFast (Canvas layer) ------------------------
   * 3 nguồn KHÁC BẢN CHẤT, phải ghi nhãn rõ để người xem không nhầm là cùng loại:
   *  - VN: 43.297 tủ đổi pin VinFast thật.
   *  - ID: trạm SẠC VinFast Indonesia (không phải tủ đổi pin), chỉ để tham khảo.
   *  - PH: dữ liệu MÔ PHỎNG bám theo đại lý & hạ tầng V-Green.
   * Chỉ để hiển thị; không dùng đo SF. Giao dịch chỉ có ở trạm mô phỏng của simulator. */
  var BSS_SOURCES = {
    VN: { label: "Tủ đổi pin VinFast (dữ liệu thật)", short: "Tủ đổi pin", unit: "tủ đổi pin VN",
          color: "#1464f4", halo: "rgba(20, 100, 244, 0.22)" },
    ID: { label: "Trạm sạc VinFast Indonesia (tham khảo)", short: "Trạm sạc ID (tham khảo)",
          unit: "trạm sạc ID (tham khảo)", color: "#0d9488", halo: "rgba(13, 148, 136, 0.22)" },
    PH: { label: "Dữ liệu mô phỏng bám theo đại lý & hạ tầng V-Green", short: "Mô phỏng PH",
          unit: "điểm mô phỏng PH", color: "#d97706", halo: "rgba(217, 119, 6, 0.22)" }
  };
  function bssSource(row) {
    return BSS_SOURCES[row._cc] || BSS_SOURCES.VN;
  }

  function buildVinFastBssPopupNode(row) {
    // row = [lat, lon, code, name, address, country]
    var lat = row[0];
    var lon = row[1];
    var code = row[2] || "—";
    var name = row[3] || "Tủ đổi pin VinFast";
    var address = row[4] || "—";
    var src = bssSource(row);

    var wrap = document.createElement("div");

    var badgeRow = document.createElement("div");
    badgeRow.style.marginBottom = "4px";
    var pill = document.createElement("span");
    pill.className = "status-pill";
    pill.style.background = src.color;
    pill.style.color = "#ffffff";
    pill.textContent = src.short + " · " + code;
    badgeRow.appendChild(pill);
    wrap.appendChild(badgeRow);

    if (src !== BSS_SOURCES.VN) {
      var srcNote = document.createElement("div");
      srcNote.style.cssText = "font-size:11px;font-style:italic;margin-bottom:4px;color:" + src.color + ";";
      srcNote.textContent = src.label;
      wrap.appendChild(srcNote);
    }

    var title = document.createElement("strong");
    title.style.display = "block";
    title.style.fontSize = "13px";
    title.style.marginBottom = "4px";
    title.textContent = name;
    wrap.appendChild(title);

    var addrLine = document.createElement("div");
    addrLine.textContent = "Địa chỉ: " + address;
    wrap.appendChild(addrLine);

    var coordLine = document.createElement("div");
    coordLine.style.color = "#64748b";
    coordLine.style.marginTop = "2px";
    coordLine.textContent = "Tọa độ: " + lat + ", " + lon;
    wrap.appendChild(coordLine);

    var link = document.createElement("a");
    link.href = "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(lat + "," + lon);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Chỉ đường Google Maps →";
    link.style.display = "inline-block";
    link.style.marginTop = "6px";
    link.style.fontWeight = "600";
    wrap.appendChild(link);

    return wrap;
  }

  var BSS_COUNTRIES = [
    { code: "ALL", label: "Tất cả", bounds: [[-11.5, 95.0], [23.4, 127.0]] },
    { code: "VN", label: "VN", bounds: [[8.4, 102.1], [23.4, 109.5]] },
    { code: "ID", label: "ID", bounds: [[-11.0, 95.0], [6.2, 141.0]] },
    { code: "PH", label: "PH", bounds: [[4.5, 116.5], [21.2, 127.0]] }
  ];

  // Quốc gia của một tủ: ưu tiên trường row[5] nếu dữ liệu có, nếu không suy ra từ tọa độ.
  function detectBssCountry(row) {
    if (row[5]) return String(row[5]).toUpperCase();
    var lat = row[0], lon = row[1];
    if (lat >= 8.0 && lat <= 23.5 && lon >= 102.0 && lon <= 110.0) return "VN";
    if (lat >= 4.5 && lat <= 21.5 && lon >= 116.5 && lon <= 127.0) return "PH";
    if (lat >= -11.5 && lat <= 6.5 && lon >= 95.0 && lon <= 141.0) return "ID";
    return "OTHER";
  }

  function rowsBounds(rows) {
    var minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
    for (var i = 0; i < rows.length; i++) {
      var rl = rows[i][0], rg = rows[i][1];
      if (rl < minLat) minLat = rl;
      if (rl > maxLat) maxLat = rl;
      if (rg < minLon) minLon = rg;
      if (rg > maxLon) maxLon = rg;
    }
    return [[minLat, minLon], [maxLat, maxLon]];
  }

  function initStationMap(_stations, _meta) {
    var mapEl = document.getElementById("stations-map");
    if (!mapEl || typeof L === "undefined") return;

    if (globalLeafletMap) {
      try { globalLeafletMap.remove(); } catch (_e) { }
    }

    var seaBounds = BSS_COUNTRIES[0].bounds;
    var map = L.map(mapEl, { minZoom: 3, preferCanvas: true }).setView([8.0, 112.0], 4);
    globalLeafletMap = map;

    // Nền bản đồ Vietmap (vector, qua plugin @vietmap/vietmap-gl-leaflet). Không dùng OSM.
    // Plugin tự đọc attribution từ style, nên KHÔNG tắt attributionControl.
    var vmKey = mapEl.getAttribute("data-vietmap-key") || "";
    if (vmKey && typeof L.vietmapGL === "function") {
      L.vietmapGL({
        style: "https://maps.vietmap.vn/maps/styles/tm/style.json?apikey=" + encodeURIComponent(vmKey)
      }).addTo(map);
    } else {
      var note = document.createElement("div");
      note.className = "map-key-missing";
      note.textContent = vmKey
        ? "Không tải được thư viện bản đồ Vietmap (kiểm tra kết nối mạng)."
        : "Chưa cấu hình ASE_VIETMAP_API_KEY trong .env: chỉ hiển thị vị trí tủ đổi pin, không có nền bản đồ.";
      mapEl.appendChild(note);
    }

    // 4 ô chọn quốc gia theo 1 hàng dọc ở góc trên bên trái bản đồ (Leaflet Control)
    var activeCountry = "ALL";
    var countryBtns = {};

    var CountryToolbarControl = L.Control.extend({
      options: { position: "topleft" },
      onAdd: function () {
        var container = L.DomUtil.create("div", "leaflet-bar bss-country-toolbar");
        L.DomEvent.disableClickPropagation(container);
        L.DomEvent.disableScrollPropagation(container);

        BSS_COUNTRIES.forEach(function (c) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "bss-country-btn" + (c.code === activeCountry ? " is-active" : "");
          btn.textContent = c.label;
          btn.title = c.label === "Tất cả" ? "Tất cả quốc gia" : ("Lọc trạm " + c.label);
          btn.addEventListener("click", function (ev) {
            if (ev) {
              ev.preventDefault();
              ev.stopPropagation();
            }
            activeCountry = c.code;
            applyBssFilter(false);
            if (filteredRows && filteredRows.length > 0) {
              map.fitBounds(rowsBounds(filteredRows), { padding: [20, 20] });
            } else {
              map.fitBounds(c.bounds, { padding: [20, 20] });
            }
          });
          countryBtns[c.code] = btn;
          container.appendChild(btn);
        });
        return container;
      }
    });
    map.addControl(new CountryToolbarControl());

    function refreshCountryBtns() {
      Object.keys(countryBtns).forEach(function (k) {
        countryBtns[k].classList.toggle("is-active", k === activeCountry);
      });
    }
    refreshCountryBtns();

    // Ô tìm kiếm đặt trên thanh tiêu đề widget (cạnh icon kính lúp)
    var searchInput = document.createElement("input");
    searchInput.type = "text";
    searchInput.className = "map-header-search-input";
    searchInput.placeholder = "Tìm mã tủ B.xxx, tên, địa chỉ...";

    var widgetCard = document.getElementById("widget-station-map");
    if (widgetCard) {
      var actionsEl = widgetCard.querySelector(".widget-actions");
      var searchBtn = widgetCard.querySelector(".btn-widget-search");
      if (actionsEl) {
        var wrap = document.createElement("div");
        wrap.className = "map-header-search-wrap";
        wrap.appendChild(searchInput);
        if (searchBtn) {
          actionsEl.insertBefore(wrap, searchBtn);
          searchBtn.addEventListener("click", function () {
            searchInput.focus();
            searchInput.select();
          });
        } else {
          actionsEl.appendChild(wrap);
        }
      }
    }

    // Lớp Canvas hiệu năng cao vẽ tủ/trạm của 3 nguồn (màu theo nguồn)
    // Canvas nằm trong một pane riêng của Leaflet (z-index 450: trên nền bản đồ, DƯỚI popup 700),
    // không gắn thẳng vào mapEl: nếu gắn ngoài map pane thì canvas đè lên popup đang mở.
    var bssPane = map.getPane("bssPane") || map.createPane("bssPane");
    bssPane.style.zIndex = "450";
    bssPane.style.pointerEvents = "none";
    var canvas = document.createElement("canvas");
    canvas.className = "vinfast-bss-canvas";
    bssPane.appendChild(canvas);

    var allRows = [];
    var filteredRows = [];
    var hitTargets = [];
    var rafPending = false;

    function scheduleRender() {
      if (rafPending) return;
      rafPending = true;
      window.requestAnimationFrame(function () {
        rafPending = false;
        renderBssCanvas();
      });
    }

    function renderBssCanvas() {
      var width = mapEl.clientWidth;
      var height = mapEl.clientHeight;
      if (width <= 0 || height <= 0) return;

      // Pane di chuyển theo map pane khi kéo bản đồ: đặt lại góc canvas về góc (0,0) của khung
      // nhìn, để toạ độ `latLngToContainerPoint` vẫn đúng trên canvas.
      L.DomUtil.setPosition(canvas, map.containerPointToLayerPoint([0, 0]));
      canvas.style.width = width + "px";
      canvas.style.height = height + "px";

      var dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
      }

      var ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);

      hitTargets = [];
      if (!filteredRows.length) return;

      var b = map.getBounds();
      var south = b.getSouth() - 0.02;
      var north = b.getNorth() + 0.02;
      var west = b.getWest() - 0.02;
      var east = b.getEast() + 0.02;
      var zoom = map.getZoom();

      // Khi zoom xa (< 13), gom cụm theo lưới pixel để hiển thị mượt và rõ mật độ tủ
      if (zoom < 13 && filteredRows.length > 250) {
        var cellSize = zoom <= 6 ? 26 : (zoom <= 8 ? 22 : (zoom <= 10 ? 18 : 14));
        var grid = Object.create(null);

        for (var i = 0; i < filteredRows.length; i++) {
          var r = filteredRows[i];
          var lat = r[0];
          var lon = r[1];
          if (lat < south || lat > north || lon < west || lon > east) continue;
          var pt = map.latLngToContainerPoint([lat, lon]);
          if (pt.x < -20 || pt.x > width + 20 || pt.y < -20 || pt.y > height + 20) continue;

          var gx = Math.floor(pt.x / cellSize);
          var gy = Math.floor(pt.y / cellSize);
          var key = r._cc + ":" + gx + ":" + gy; // không gộp lẫn nguồn khác nhau
          var cell = grid[key];
          if (!cell) {
            grid[key] = {
              sumX: pt.x,
              sumY: pt.y,
              sumLat: lat,
              sumLon: lon,
              count: 1,
              row: r
            };
          } else {
            cell.sumX += pt.x;
            cell.sumY += pt.y;
            cell.sumLat += lat;
            cell.sumLon += lon;
            cell.count += 1;
          }
        }

        var keys = Object.keys(grid);
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.font = "bold 9.5px Inter, system-ui, sans-serif";

        for (var k = 0; k < keys.length; k++) {
          var c = grid[keys[k]];
          var cx = c.sumX / c.count;
          var cy = c.sumY / c.count;
          var cSrc = bssSource(c.row);
          if (c.count === 1) {
            ctx.beginPath();
            ctx.arc(cx, cy, 5.5, 0, Math.PI * 2);
            ctx.fillStyle = cSrc.color;
            ctx.fill();
            ctx.lineWidth = 1.5;
            ctx.strokeStyle = "#ffffff";
            ctx.stroke();

            ctx.beginPath();
            ctx.arc(cx, cy, 1.8, 0, Math.PI * 2);
            ctx.fillStyle = "#ffffff";
            ctx.fill();

            hitTargets.push({ x: cx, y: cy, radius: 9, count: 1, row: c.row });
          } else {
            var rad = Math.min(15, 7.5 + Math.log10(c.count) * 3.2);
            ctx.beginPath();
            ctx.arc(cx, cy, rad + 2, 0, Math.PI * 2);
            ctx.fillStyle = cSrc.halo;
            ctx.fill();

            ctx.beginPath();
            ctx.arc(cx, cy, rad, 0, Math.PI * 2);
            ctx.fillStyle = cSrc.color;
            ctx.fill();
            ctx.lineWidth = 1.5;
            ctx.strokeStyle = "#ffffff";
            ctx.stroke();

            if (zoom >= 7 || c.count >= 50) {
              var label = c.count >= 1000 ? (c.count / 1000).toFixed(1) + "k" : String(c.count);
              ctx.fillStyle = "#ffffff";
              ctx.fillText(label, cx, cy);
            }

            hitTargets.push({
              x: cx,
              y: cy,
              radius: rad + 4,
              count: c.count,
              lat: c.sumLat / c.count,
              lon: c.sumLon / c.count,
              row: c.row
            });
          }
        }
      } else {
        ctx.font = "bold 10.5px Inter, system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";

        for (var j = 0; j < filteredRows.length; j++) {
          var row = filteredRows[j];
          var rLat = row[0];
          var rLon = row[1];
          if (rLat < south || rLat > north || rLon < west || rLon > east) continue;
          var p = map.latLngToContainerPoint([rLat, rLon]);
          if (p.x < -20 || p.x > width + 20 || p.y < -20 || p.y > height + 20) continue;

          ctx.beginPath();
          ctx.arc(p.x, p.y, 6.5, 0, Math.PI * 2);
          ctx.fillStyle = bssSource(row).color;
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = "#ffffff";
          ctx.stroke();

          ctx.beginPath();
          ctx.arc(p.x, p.y, 2.2, 0, Math.PI * 2);
          ctx.fillStyle = "#ffffff";
          ctx.fill();

          if (zoom >= 15 && row[2]) {
            ctx.fillStyle = "#0f172a";
            ctx.strokeStyle = "rgba(255, 255, 255, 0.9)";
            ctx.lineWidth = 3;
            ctx.strokeText(row[2], p.x + 9, p.y);
            ctx.fillText(row[2], p.x + 9, p.y);
          }

          hitTargets.push({ x: p.x, y: p.y, radius: 11, count: 1, row: row });
        }
      }
    }

    function findHitTarget(containerPoint) {
      var best = null;
      var bestDistSq = Infinity;
      for (var i = 0; i < hitTargets.length; i++) {
        var t = hitTargets[i];
        var dx = containerPoint.x - t.x;
        var dy = containerPoint.y - t.y;
        var dSq = dx * dx + dy * dy;
        var maxR = t.radius + 4;
        if (dSq <= maxR * maxR && dSq < bestDistSq) {
          bestDistSq = dSq;
          best = t;
        }
      }
      return best;
    }

    map.on("click", function (ev) {
      if (!ev.containerPoint) return;
      var hit = findHitTarget(ev.containerPoint);
      if (!hit) return;
      if (hit.count > 1 && map.getZoom() < 15) {
        map.setView([hit.lat, hit.lon], Math.min(map.getZoom() + 3, 16));
      } else if (hit.row) {
        L.popup()
          .setLatLng([hit.row[0], hit.row[1]])
          .setContent(buildVinFastBssPopupNode(hit.row))
          .openOn(map);
      }
    });

    map.on("mousemove", function (ev) {
      if (!ev.containerPoint) return;
      var hit = findHitTarget(ev.containerPoint);
      mapEl.style.cursor = hit ? "pointer" : "";
    });

    map.on("move moveend zoomend viewreset resize", scheduleRender);

    function applyBssFilter(zoomToMatches) {
      var q = searchInput.value.trim().toLowerCase();
      refreshCountryBtns();
      var base = activeCountry === "ALL"
        ? allRows
        : allRows.filter(function (r) { return r._cc === activeCountry; });
      if (!q) {
        filteredRows = base;
      } else {
        filteredRows = base.filter(function (r) {
          return (
            (r[2] && r[2].toLowerCase().indexOf(q) >= 0) ||
            (r[3] && r[3].toLowerCase().indexOf(q) >= 0) ||
            (r[4] && r[4].toLowerCase().indexOf(q) >= 0)
          );
        });
        if (zoomToMatches && filteredRows.length > 0) {
          if (filteredRows.length === 1) {
            map.setView([filteredRows[0][0], filteredRows[0][1]], 16);
            L.popup()
              .setLatLng([filteredRows[0][0], filteredRows[0][1]])
              .setContent(buildVinFastBssPopupNode(filteredRows[0]))
              .openOn(map);
          } else {
            map.fitBounds(rowsBounds(filteredRows), { padding: [30, 30] });
          }
        }
      }
      scheduleRender();
    }

    searchInput.addEventListener("input", function () {
      applyBssFilter(false);
    });
    searchInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        applyBssFilter(true);
      }
    });

    var hasInitialFit = false;
    function fitAll() {
      map.invalidateSize();
      if (!hasInitialFit) {
        hasInitialFit = true;
        map.fitBounds(seaBounds, { padding: [20, 20], animate: false });
      }
      scheduleRender();
    }
    fitAll();
    setTimeout(fitAll, 0);
    window.addEventListener("load", fitAll);
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(function () {
        map.invalidateSize();
        scheduleRender();
      }).observe(mapEl);
    }

    fetch("/api/dashboard/vinfast-bss")
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (rows) {
        if (Array.isArray(rows)) {
          rows.forEach(function (r) { r._cc = detectBssCountry(r); });
          allRows = rows;
          filteredRows = rows;
          // Khung nhìn ôm toàn bộ tủ đang có (VN + ID + PH nếu dữ liệu có đủ)
          var known = rows.filter(function (r) { return r._cc !== "OTHER"; });
          if (known.length) map.fitBounds(rowsBounds(known), { padding: [20, 20], animate: false });
          scheduleRender();
        }
      })
      .catch(function () {
        // silent fail
      });
  }

  /* --- Cabinet Temperature Time Series Chart (DASHBOARD_UI_PLAN §1, §2.1) -- */

  function renderCabinetTempChart(chartEl, legendEl, data) {
    if (!chartEl || typeof echarts === "undefined" || !data) return;

    var chart = echarts.init(chartEl);
    activeEchartsInstances.push(chart);

    var colors = ["#2563eb", "#7c3aed", "#db2777", "#059669", "#d97706", "#0891b2"];
    var seriesConfig = [];
    var times = [];

    (data.series || []).forEach(function (s, sIdx) {
      var color = colors[sIdx % colors.length];
      var pts = s.points || [];
      pts.forEach(function (p) {
        var tLabel = p.timestamp_local || p.timestamp_utc;
        if (times.indexOf(tLabel) < 0) times.push(tLabel);
      });

      var sObj = {
        name: s.cabinet_id,
        type: "line",
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: color },
        itemStyle: { color: color },
        data: pts.map(function (p) {
          return [p.timestamp_local || p.timestamp_utc, p.temperature_c];
        })
      };

      // Chỉ gắn markLine lên series đầu tiên để tránh trùng lặp nhãn
      if (sIdx === 0) {
        sObj.markLine = {
          silent: true,
          symbol: ["none", "circle"],
          data: [
            {
              yAxis: data.warn_threshold_c || 55,
              lineStyle: { color: "#ea580c", type: "dashed", width: 1.5 },
              label: {
                formatter: "Cảnh báo " + (data.warn_threshold_c || 55) + "°C",
                position: "insideEndTop",
                color: "#ea580c"
              }
            },
            {
              yAxis: data.critical_threshold_c || 60,
              lineStyle: { color: "#dc2626", type: "dashed", width: 1.5 },
              label: {
                formatter: "Ngắt nguồn " + (data.critical_threshold_c || 60) + "°C",
                position: "insideEndTop",
                color: "#dc2626"
              }
            }
          ]
        };
      }
      seriesConfig.push(sObj);
    });

    times.sort();

    chart.setOption({
      backgroundColor: "#ffffff",
      tooltip: {
        trigger: "axis",
        backgroundColor: "#ffffff",
        borderColor: "#e2e8f0",
        textStyle: { color: "#1e293b", fontSize: 12 }
      },
      grid: { left: 45, right: 90, top: 25, bottom: 25 },
      xAxis: {
        type: "category",
        data: times,
        axisLine: { lineStyle: { color: "#e2e8f0" } },
        axisLabel: { color: "#64748b", fontSize: 11 }
      },
      yAxis: {
        type: "value",
        min: 20,
        axisLine: { lineStyle: { color: "#e2e8f0" } },
        splitLine: { lineStyle: { color: "#f1f5f9" } },
        axisLabel: { color: "#64748b", formatter: "{value}°C" }
      },
      series: seriesConfig
    });

    // Render Bảng Chú thích Avg dưới biểu đồ (§1)
    if (legendEl) {
      while (legendEl.firstChild) {
        legendEl.removeChild(legendEl.firstChild);
      }
      var table = document.createElement("table");
      table.className = "echarts-legend-table";
      (data.series || []).forEach(function (s, sIdx) {
        var tr = document.createElement("tr");
        var tdName = document.createElement("td");
        var wrap = document.createElement("div");
        wrap.className = "legend-series-cell";
        var dot = document.createElement("span");
        dot.className = "legend-dot";
        dot.style.background = colors[sIdx % colors.length];
        var nameSpan = document.createElement("span");
        nameSpan.textContent = s.cabinet_id;
        wrap.appendChild(dot);
        wrap.appendChild(nameSpan);
        tdName.appendChild(wrap);
        tr.appendChild(tdName);

        var tdAvg = document.createElement("td");
        tdAvg.className = "legend-avg-cell";
        tdAvg.textContent = "Avg: " + (s.avg_c !== null && s.avg_c !== undefined ? s.avg_c + " °C" : "—");
        tr.appendChild(tdAvg);

        table.appendChild(tr);
      });
      legendEl.appendChild(table);
    }
  }

  /* --- Bộ lọc trượt của Danh sách giao dịch đổi pin ------------------------ */

  function initTxnFilterDrawer() {
    var drawer = document.getElementById("txn-filter-drawer");
    var openBtn = document.getElementById("txn-filter-open");
    if (!drawer || !openBtn) return;
    var backdrop = document.getElementById("txn-filter-backdrop");
    var closeBtn = document.getElementById("txn-filter-close");
    var resetBtn = document.getElementById("txn-filter-reset");
    var form = document.getElementById("txn-filter-form");

    function setOpen(open) {
      drawer.classList.toggle("is-open", open);
      drawer.setAttribute("aria-hidden", open ? "false" : "true");
      openBtn.setAttribute("aria-expanded", open ? "true" : "false");
      if (backdrop) backdrop.hidden = !open;
      if (open) {
        var first = drawer.querySelector("input[type=text]");
        if (first) first.focus();
      }
    }
    openBtn.addEventListener("click", function () { setOpen(true); });
    if (closeBtn) closeBtn.addEventListener("click", function () { setOpen(false); });
    if (backdrop) backdrop.addEventListener("click", function () { setOpen(false); });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && drawer.classList.contains("is-open")) setOpen(false);
    });
    // "Đặt lại": xoá mọi tiêu chí trong form (chưa gửi), giống màn hình thật
    if (resetBtn && form) {
      resetBtn.addEventListener("click", function () {
        form.querySelectorAll("input[type=text], input[type=datetime-local]").forEach(function (el) {
          el.value = "";
        });
        form.querySelectorAll("input[type=checkbox]").forEach(function (el) { el.checked = false; });
      });
    }

    var pageSize = document.getElementById("txn-page-size");
    if (pageSize && pageSize.form) {
      pageSize.addEventListener("change", function () { pageSize.form.submit(); });
    }
  }

  /* --- Main Init on DOMContentLoaded --------------------------------------- */

  document.addEventListener("DOMContentLoaded", function () {
    var pageData = parsePageData();
    initFilterControls(pageData);
    setupWidgetFullscreen();
    initTxnFilterDrawer();

    if (!pageData) return;

    // Bản đồ trạm
    if (pageData.stations_map) {
      initStationMap(pageData.stations_map, pageData.meta);
    }

    // Chi tiết trạm: biểu đồ nhiệt độ tủ
    if (pageData.station && pageData.temperature_timeseries) {
      renderCabinetTempChart(
        document.getElementById("chart-cabinet-temperature"),
        document.getElementById("cabinet-temp-legend-table"),
        pageData.temperature_timeseries
      );
    }
  });
})();
