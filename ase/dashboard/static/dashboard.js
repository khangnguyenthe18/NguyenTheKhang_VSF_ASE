/* ASE BSS NOC Dashboard JS — ECharts 5.5.1 + Leaflet 1.9.4
 * Nguyên tắc bảo mật (DASHBOARD_PLAN §2.6): chỉ dùng textContent, tuyệt đối không dùng innerHTML với dữ liệu.
 * Bố cục widget grid console IoT (DASHBOARD_UI_PLAN §1, §3).
 */
(function () {
  "use strict";

  var activeEchartsInstances = [];
  var globalLeafletMap = null;

  function escapeHTML(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

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

    if (resetBtn && input && form) {
      resetBtn.addEventListener("click", function () {
        input.value = "";
        form.submit();
      });
    }

    if (profileSelect && form && input) {
      profileSelect.addEventListener("change", function () {
        input.value = "";
        form.submit();
      });
    }

    if (countrySelect && form) {
      countrySelect.addEventListener("change", function () {
        form.submit();
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

  /* --- Reusable Entity Table Component (DASHBOARD_UI_PLAN §1, §3) ---------- */

  function createEntityTable(config) {
    var container = config.container;
    if (!container) return;
    while (container.firstChild) {
      container.removeChild(container.firstChild);
    }

    var state = {
      data: config.rows.slice(),
      filtered: config.rows.slice(),
      sortKey: config.defaultSortKey || null,
      sortAsc: config.defaultSortAsc !== undefined ? config.defaultSortAsc : true,
      pageSize: config.pageSize || 5,
      pageIndex: 0,
      searchQuery: ""
    };

    var searchWrap = document.createElement("div");
    searchWrap.className = "table-search-bar";
    var searchInput = document.createElement("input");
    searchInput.type = "text";
    searchInput.className = "table-search-input";
    searchInput.placeholder = config.searchPlaceholder || "Tìm kiếm...";
    searchWrap.appendChild(searchInput);
    container.appendChild(searchWrap);

    var tableWrap = document.createElement("div");
    tableWrap.className = "table-wrap";
    var table = document.createElement("table");
    table.className = "data-table" + (config.compact ? " data-table-compact" : "");
    var thead = document.createElement("thead");
    var theadRow = document.createElement("tr");

    config.columns.forEach(function (col) {
      var th = document.createElement("th");
      th.textContent = col.label;
      if (col.sortable && col.key) {
        th.className = "sortable-th";
        var arrow = document.createElement("span");
        arrow.className = "sort-arrow";
        arrow.textContent = "▲";
        th.appendChild(arrow);

        th.addEventListener("click", function () {
          if (state.sortKey === col.key) {
            state.sortAsc = !state.sortAsc;
          } else {
            state.sortKey = col.key;
            state.sortAsc = true;
          }
          applyFilterAndSort();
        });
      }
      theadRow.appendChild(th);
    });
    thead.appendChild(theadRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    container.appendChild(tableWrap);

    var paginationBar = document.createElement("div");
    paginationBar.className = "table-pagination";

    var sizeWrap = document.createElement("div");
    sizeWrap.className = "page-size-selector";
    var sizeLabel = document.createElement("span");
    sizeLabel.textContent = "Số dòng:";
    var sizeSelect = document.createElement("select");
    sizeSelect.className = "page-size-select";
    [5, 10, 25, 50].forEach(function (sz) {
      var opt = document.createElement("option");
      opt.value = sz;
      opt.textContent = String(sz);
      if (sz === state.pageSize) opt.selected = true;
      sizeSelect.appendChild(opt);
    });
    sizeSelect.addEventListener("change", function () {
      state.pageSize = parseInt(sizeSelect.value, 10) || 5;
      state.pageIndex = 0;
      renderTableRows();
    });
    sizeWrap.appendChild(sizeLabel);
    sizeWrap.appendChild(sizeSelect);
    paginationBar.appendChild(sizeWrap);

    var infoSpan = document.createElement("span");
    infoSpan.className = "page-info-text";
    paginationBar.appendChild(infoSpan);

    var navWrap = document.createElement("div");
    navWrap.className = "page-nav-btns";
    var btnFirst = document.createElement("button");
    btnFirst.type = "button";
    btnFirst.className = "page-nav-btn";
    btnFirst.textContent = "«";
    btnFirst.title = "Trang đầu";

    var btnPrev = document.createElement("button");
    btnPrev.type = "button";
    btnPrev.className = "page-nav-btn";
    btnPrev.textContent = "‹";
    btnPrev.title = "Trang trước";

    var btnNext = document.createElement("button");
    btnNext.type = "button";
    btnNext.className = "page-nav-btn";
    btnNext.textContent = "›";
    btnNext.title = "Trang sau";

    var btnLast = document.createElement("button");
    btnLast.type = "button";
    btnLast.className = "page-nav-btn";
    btnLast.textContent = "»";
    btnLast.title = "Trang cuối";

    btnFirst.addEventListener("click", function () {
      state.pageIndex = 0;
      renderTableRows();
    });
    btnPrev.addEventListener("click", function () {
      if (state.pageIndex > 0) {
        state.pageIndex--;
        renderTableRows();
      }
    });
    btnNext.addEventListener("click", function () {
      var maxPage = Math.max(0, Math.ceil(state.filtered.length / state.pageSize) - 1);
      if (state.pageIndex < maxPage) {
        state.pageIndex++;
        renderTableRows();
      }
    });
    btnLast.addEventListener("click", function () {
      var maxPage = Math.max(0, Math.ceil(state.filtered.length / state.pageSize) - 1);
      state.pageIndex = maxPage;
      renderTableRows();
    });

    navWrap.appendChild(btnFirst);
    navWrap.appendChild(btnPrev);
    navWrap.appendChild(btnNext);
    navWrap.appendChild(btnLast);
    paginationBar.appendChild(navWrap);
    container.appendChild(paginationBar);

    function applyFilterAndSort() {
      var q = state.searchQuery.trim().toLowerCase();
      if (!q) {
        state.filtered = state.data.slice();
      } else {
        state.filtered = state.data.filter(function (row) {
          return config.columns.some(function (c) {
            var val = row[c.key];
            if (val === null || val === undefined) return false;
            return String(val).toLowerCase().indexOf(q) >= 0;
          });
        });
      }

      if (state.sortKey) {
        var key = state.sortKey;
        var asc = state.sortAsc ? 1 : -1;
        state.filtered.sort(function (a, b) {
          var valA = a[key];
          var valB = b[key];
          if (valA === null || valA === undefined) return 1;
          if (valB === null || valB === undefined) return -1;
          if (typeof valA === "number" && typeof valB === "number") {
            return (valA - valB) * asc;
          }
          return String(valA).localeCompare(String(valB)) * asc;
        });
      }

      var ths = theadRow.querySelectorAll("th");
      config.columns.forEach(function (col, idx) {
        var th = ths[idx];
        if (!th) return;
        th.classList.remove("sorted-asc", "sorted-desc");
        var arrow = th.querySelector(".sort-arrow");
        if (col.key === state.sortKey) {
          th.classList.add(state.sortAsc ? "sorted-asc" : "sorted-desc");
          if (arrow) arrow.textContent = state.sortAsc ? "▲" : "▼";
        } else if (arrow) {
          arrow.textContent = "▲";
        }
      });

      state.pageIndex = 0;
      renderTableRows();
    }

    function renderTableRows() {
      while (tbody.firstChild) {
        tbody.removeChild(tbody.firstChild);
      }

      var total = state.filtered.length;
      var start = state.pageIndex * state.pageSize;
      var end = Math.min(start + state.pageSize, total);
      var maxPage = Math.max(0, Math.ceil(total / state.pageSize) - 1);

      if (total === 0) {
        var emptyTr = document.createElement("tr");
        var emptyTd = document.createElement("td");
        emptyTd.colSpan = config.columns.length;
        emptyTd.textContent = "Không có dữ liệu phù hợp.";
        emptyTd.style.textAlign = "center";
        emptyTd.style.padding = "20px";
        emptyTd.style.color = "#94a3b8";
        emptyTr.appendChild(emptyTd);
        tbody.appendChild(emptyTr);
        infoSpan.textContent = "0 – 0 of 0";
        btnFirst.disabled = true;
        btnPrev.disabled = true;
        btnNext.disabled = true;
        btnLast.disabled = true;
        return;
      }

      infoSpan.textContent = (start + 1) + " – " + end + " of " + total;
      btnFirst.disabled = (state.pageIndex === 0);
      btnPrev.disabled = (state.pageIndex === 0);
      btnNext.disabled = (state.pageIndex >= maxPage);
      btnLast.disabled = (state.pageIndex >= maxPage);

      var pageRows = state.filtered.slice(start, end);
      pageRows.forEach(function (row) {
        var tr = document.createElement("tr");
        if (config.renderRow) {
          config.renderRow(tr, row);
        } else {
          config.columns.forEach(function (col) {
            var td = document.createElement("td");
            td.textContent = row[col.key] !== null && row[col.key] !== undefined ? String(row[col.key]) : "—";
            tr.appendChild(td);
          });
        }
        tbody.appendChild(tr);
      });
    }

    searchInput.addEventListener("input", function () {
      state.searchQuery = searchInput.value;
      applyFilterAndSort();
    });

    applyFilterAndSort();

    return {
      updateData: function (newRows) {
        state.data = newRows.slice();
        applyFilterAndSort();
      }
    };
  }

  /* --- VinFast Battery Swap Cabinet Map (43.298 tủ thực tế, Canvas layer) -- */

  function buildVinFastBssPopupNode(row) {
    // row = [lat, lon, code, name, address]
    var lat = row[0];
    var lon = row[1];
    var code = row[2] || "—";
    var name = row[3] || "Tủ đổi pin VinFast";
    var address = row[4] || "—";

    var wrap = document.createElement("div");

    var badgeRow = document.createElement("div");
    badgeRow.style.marginBottom = "4px";
    var pill = document.createElement("span");
    pill.className = "status-pill status-blue";
    pill.textContent = "Tủ đổi pin · " + code;
    badgeRow.appendChild(pill);
    wrap.appendChild(badgeRow);

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

  function initStationMap(_stations, _meta) {
    var mapEl = document.getElementById("stations-map");
    if (!mapEl || typeof L === "undefined") return;

    if (globalLeafletMap) {
      try { globalLeafletMap.remove(); } catch (_e) { }
    }

    var vnBounds = [[8.4, 102.1], [23.4, 109.5]];
    var map = L.map(mapEl, { minZoom: 4, preferCanvas: true }).setView([16.0, 106.5], 5);
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

    // Thanh công cụ hiển thị tổng số tủ đổi pin + tìm kiếm nhanh trên bản đồ
    var toolbar = document.createElement("div");
    toolbar.className = "bss-map-toolbar";
    L.DomEvent.disableClickPropagation(toolbar);
    L.DomEvent.disableScrollPropagation(toolbar);

    var countBadge = document.createElement("span");
    countBadge.className = "bss-count-badge";
    countBadge.textContent = "Đang tải tủ đổi pin VinFast...";
    toolbar.appendChild(countBadge);

    var searchInput = document.createElement("input");
    searchInput.type = "text";
    searchInput.className = "bss-search-input";
    searchInput.placeholder = "Tìm mã tủ B.xxx, tên, địa chỉ...";
    toolbar.appendChild(searchInput);
    mapEl.appendChild(toolbar);

    var widgetCard = document.getElementById("widget-station-map");
    if (widgetCard) {
      var searchBtn = widgetCard.querySelector(".btn-widget-search");
      if (searchBtn) {
        searchBtn.addEventListener("click", function () {
          searchInput.focus();
          searchInput.select();
        });
      }
    }

    // Lớp Canvas hiệu năng cao vẽ 43.298 tủ đổi pin
    var canvas = document.createElement("canvas");
    canvas.className = "vinfast-bss-canvas";
    mapEl.appendChild(canvas);

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
          var key = gx + ":" + gy;
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
          if (c.count === 1) {
            ctx.beginPath();
            ctx.arc(cx, cy, 5.5, 0, Math.PI * 2);
            ctx.fillStyle = "#1464f4";
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
            ctx.fillStyle = "rgba(20, 100, 244, 0.22)";
            ctx.fill();

            ctx.beginPath();
            ctx.arc(cx, cy, rad, 0, Math.PI * 2);
            ctx.fillStyle = "#1464f4";
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
          ctx.fillStyle = "#1464f4";
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
      if (!q) {
        filteredRows = allRows;
        countBadge.textContent = allRows.length.toLocaleString("vi-VN") + " Tủ đổi pin VinFast";
      } else {
        filteredRows = allRows.filter(function (r) {
          return (
            (r[2] && r[2].toLowerCase().indexOf(q) >= 0) ||
            (r[3] && r[3].toLowerCase().indexOf(q) >= 0) ||
            (r[4] && r[4].toLowerCase().indexOf(q) >= 0)
          );
        });
        countBadge.textContent =
          filteredRows.length.toLocaleString("vi-VN") +
          " / " +
          allRows.length.toLocaleString("vi-VN") +
          " tủ";
        if (zoomToMatches && filteredRows.length > 0) {
          if (filteredRows.length === 1) {
            map.setView([filteredRows[0][0], filteredRows[0][1]], 16);
            L.popup()
              .setLatLng([filteredRows[0][0], filteredRows[0][1]])
              .setContent(buildVinFastBssPopupNode(filteredRows[0]))
              .openOn(map);
          } else {
            var minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
            for (var i = 0; i < filteredRows.length; i++) {
              var rl = filteredRows[i][0];
              var rg = filteredRows[i][1];
              if (rl < minLat) minLat = rl;
              if (rl > maxLat) maxLat = rl;
              if (rg < minLon) minLon = rg;
              if (rg > maxLon) maxLon = rg;
            }
            map.fitBounds([[minLat, minLon], [maxLat, maxLon]], { padding: [30, 30] });
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
        map.fitBounds(vnBounds, { padding: [20, 20], animate: false });
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
          allRows = rows;
          filteredRows = rows;
          countBadge.textContent = rows.length.toLocaleString("vi-VN") + " Tủ đổi pin VinFast";
          scheduleRender();
        }
      })
      .catch(function () {
        countBadge.textContent = "Không tải được dữ liệu tủ đổi pin";
      });
  }

  /* --- Station Table in Overview (§2.1) ------------------------------------- */

  function initStationTable(stations, meta) {
    var container = document.getElementById("station-table-widget");
    if (!container || !Array.isArray(stations)) return;

    createEntityTable({
      container: container,
      compact: true,
      pageSize: 5,
      searchPlaceholder: "Tìm theo tên trạm, nước...",
      defaultSortKey: "station_id",
      defaultSortAsc: true,
      columns: [
        { key: "station_id", label: "Tên Trạm", sortable: true },
        { key: "status_label", label: "Trạng thái", sortable: true },
        { key: "ready_battery_pct", label: "Pin sẵn sàng", sortable: true },
        { key: "max_temp_c", label: "Nhiệt max", sortable: true },
        { key: "fail_rate_today", label: "Tỷ lệ lỗi", sortable: true }
      ],
      rows: stations.map(function (s) {
        var isOnline = (s.cabinets_online === s.cabinets_total && s.cabinets_total > 0);
        return {
          station_id: s.station_id,
          country: s.country,
          status_color: s.status_color,
          is_online: isOnline,
          status_label: isOnline ? "Online" : "Offline",
          ready_battery_pct: s.ready_battery_pct !== undefined ? s.ready_battery_pct : (s.total_slots > 0 ? Math.round(s.ready_batteries / s.total_slots * 100) : 0),
          max_temp_c: s.max_temp_c !== null && s.max_temp_c !== undefined ? s.max_temp_c : null,
          fail_rate_today: s.fail_rate_today !== undefined ? s.fail_rate_today : 0,
          raw: s
        };
      }),
      renderRow: function (tr, row) {
        // Col 1: Station ID Link
        var tdName = document.createElement("td");
        var link = document.createElement("a");
        var q = "?profile=" + encodeURIComponent(meta.profile) + "&as_of=" + encodeURIComponent(meta.as_of);
        if (meta.country) q += "&country=" + encodeURIComponent(meta.country);
        link.href = "/dashboard/stations/" + encodeURIComponent(row.station_id) + q;
        link.textContent = row.station_id;
        link.style.fontWeight = "600";
        tdName.appendChild(link);
        tr.appendChild(tdName);

        // Col 2: Status Dot + Text
        var tdStatus = document.createElement("td");
        var statusWrap = document.createElement("span");
        statusWrap.className = "status-indicator";
        var dot = document.createElement("span");
        dot.className = "status-dot dot-" + (row.status_color || "green");
        var text = document.createElement("span");
        text.textContent = row.status_label;
        statusWrap.appendChild(dot);
        statusWrap.appendChild(text);
        tdStatus.appendChild(statusWrap);
        tr.appendChild(tdStatus);

        // Col 3: Ready battery %
        var tdReady = document.createElement("td");
        tdReady.textContent = row.ready_battery_pct + "%";
        tr.appendChild(tdReady);

        // Col 4: Max Temp
        var tdTemp = document.createElement("td");
        tdTemp.textContent = row.max_temp_c !== null ? row.max_temp_c + "°C" : "—";
        tr.appendChild(tdTemp);

        // Col 5: Error rate today
        var tdErr = document.createElement("td");
        var errPct = (row.fail_rate_today * 100).toFixed(1);
        tdErr.textContent = errPct + "%";
        if (row.fail_rate_today > 0.05) {
          tdErr.style.color = "var(--red)";
          tdErr.style.fontWeight = "700";
        }
        tr.appendChild(tdErr);
      }
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

  function initCabinetTempWidget(initialData, meta) {
    var chartEl = document.getElementById("chart-cabinet-temperature");
    var legendEl = document.getElementById("cabinet-temp-legend-table");
    var selectEl = document.getElementById("temp-station-select");
    if (!chartEl || !initialData) return;

    if (selectEl && Array.isArray(initialData.available_stations)) {
      while (selectEl.firstChild) {
        selectEl.removeChild(selectEl.firstChild);
      }
      initialData.available_stations.forEach(function (st) {
        var opt = document.createElement("option");
        opt.value = st.station_id;
        opt.textContent = st.label || st.station_id;
        if (st.station_id === initialData.station_id) opt.selected = true;
        selectEl.appendChild(opt);
      });

      selectEl.addEventListener("change", function () {
        var targetStation = selectEl.value;
        var url = "/api/dashboard/timeseries/cabinet-temperature?profile=" + encodeURIComponent(meta.profile) +
          "&hours=1&as_of=" + encodeURIComponent(meta.as_of) + "&station_id=" + encodeURIComponent(targetStation);
        if (meta.country) url += "&country=" + encodeURIComponent(meta.country);

        fetch(url)
          .then(function (res) { return res.json(); })
          .then(function (respJson) {
            renderCabinetTempChart(chartEl, legendEl, respJson);
          })
          .catch(function () { });
      });
    }

    renderCabinetTempChart(chartEl, legendEl, initialData);
  }

  /* --- Ready Batteries Area Chart (DASHBOARD_UI_PLAN §1, §2.1) --------------- */

  function initReadyBatteriesChart(data) {
    var chartEl = document.getElementById("chart-ready-batteries");
    if (!chartEl || typeof echarts === "undefined" || !data || !Array.isArray(data.series)) return;

    var chart = echarts.init(chartEl);
    activeEchartsInstances.push(chart);

    var colors = ["#2563eb", "#059669", "#7c3aed", "#d97706", "#0891b2"];
    var seriesConfig = [];
    var timestamps = data.timestamps || [];

    data.series.forEach(function (s, idx) {
      var col = colors[idx % colors.length];
      seriesConfig.push({
        name: s.station_name || s.station_id,
        type: "line",
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: col },
        itemStyle: { color: col },
        areaStyle: { opacity: 0.22, color: col },
        data: (s.points || []).map(function (p) {
          return [p.timestamp_local || p.timestamp_utc, p.ready_pct];
        })
      });
    });

    chart.setOption({
      backgroundColor: "#ffffff",
      tooltip: {
        trigger: "axis",
        backgroundColor: "#ffffff",
        borderColor: "#e2e8f0",
        textStyle: { color: "#1e293b", fontSize: 12 },
        valueFormatter: function (val) { return val !== null ? val + "%" : "—"; }
      },
      legend: {
        data: data.series.map(function (s) { return s.station_name || s.station_id; }),
        top: 0,
        textStyle: { color: "#64748b", fontSize: 11 }
      },
      grid: { left: 45, right: 20, top: 32, bottom: 25 },
      xAxis: {
        type: "category",
        data: timestamps,
        axisLine: { lineStyle: { color: "#e2e8f0" } },
        axisLabel: { color: "#64748b", fontSize: 11 }
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 100,
        axisLine: { lineStyle: { color: "#e2e8f0" } },
        splitLine: { lineStyle: { color: "#f1f5f9" } },
        axisLabel: { color: "#64748b", formatter: "{value}%" }
      },
      series: seriesConfig
    });
  }

  /* --- Alarms Table in Overview (DASHBOARD_UI_PLAN §2.3) ---------------------- */

  function initAlarmsTable(alarmsData, _meta) {
    var container = document.getElementById("alarms-table-widget");
    if (!container || !alarmsData || !Array.isArray(alarmsData.alarms)) return;

    createEntityTable({
      container: container,
      compact: true,
      pageSize: 5,
      searchPlaceholder: "Tìm cảnh báo theo mã, thiết bị, loại lỗi...",
      defaultSortKey: "start_time_utc",
      defaultSortAsc: false,
      columns: [
        { key: "start_time_local", label: "Start Time", sortable: true },
        { key: "originator", label: "Originator", sortable: true },
        { key: "alarm_type", label: "Type", sortable: true },
        { key: "severity", label: "Severity", sortable: true },
        { key: "status_line2", label: "Status", sortable: true },
        { key: "actions", label: "Thao tác", sortable: false }
      ],
      rows: alarmsData.alarms,
      renderRow: function (tr, alarm) {
        // Col 1: Start Time
        var tdTime = document.createElement("td");
        tdTime.className = "alarm-time";
        tdTime.textContent = alarm.start_time_local || alarm.start_time_utc;
        tr.appendChild(tdTime);

        // Col 2: Originator
        var tdOriginator = document.createElement("td");
        tdOriginator.className = "alarm-originator";
        tdOriginator.textContent = alarm.originator;
        tr.appendChild(tdOriginator);

        // Col 3: Type + Optional Mock badge
        var tdType = document.createElement("td");
        var typeText = document.createElement("span");
        typeText.className = "alarm-type-text";
        typeText.textContent = alarm.alarm_type;
        tdType.appendChild(typeText);
        if (alarm.is_mock) {
          var mockBadge = document.createElement("span");
          mockBadge.className = "badge-mock";
          mockBadge.textContent = "mẫu";
          mockBadge.title = "Dữ liệu incident mẫu từ notify-demo";
          tdType.appendChild(mockBadge);
        }
        tr.appendChild(tdType);

        // Col 4: Severity (Critical / Major / Minor)
        var tdSev = document.createElement("td");
        var sevSpan = document.createElement("span");
        sevSpan.textContent = alarm.severity;
        if (alarm.severity === "Critical") {
          sevSpan.className = "sev-critical";
        } else if (alarm.severity === "Major") {
          sevSpan.className = "sev-major";
        } else {
          sevSpan.className = "sev-minor";
        }
        tdSev.appendChild(sevSpan);
        tr.appendChild(tdSev);

        // Col 5: Status 2 dòng (§2.3)
        var tdStatus = document.createElement("td");
        var statusGrp = document.createElement("div");
        statusGrp.className = "alarm-status-group";
        var line1 = document.createElement("span");
        line1.className = "status-line1";
        line1.textContent = alarm.status_line1 || "Active";
        var line2 = document.createElement("span");
        line2.className = "status-line2";
        line2.textContent = alarm.status_line2 || "Chưa xác nhận";
        if (alarm.status_line2 === "Đã xác nhận" || alarm.status_line2 === "Đã xử lý") {
          line2.classList.add("confirmed");
        }
        statusGrp.appendChild(line1);
        statusGrp.appendChild(line2);
        tdStatus.appendChild(statusGrp);
        tr.appendChild(tdStatus);

        // Col 6: Nút "…" và nút "✓" (§2.3)
        var tdActions = document.createElement("td");
        var actWrap = document.createElement("div");
        actWrap.className = "alarm-actions";

        if (alarm.details_link) {
          var btnDetail = document.createElement("a");
          btnDetail.href = alarm.details_link;
          btnDetail.className = "btn-alarm-action btn-alarm-details";
          btnDetail.textContent = "…";
          btnDetail.title = "Xem chi tiết";
          actWrap.appendChild(btnDetail);
        }

        if (alarm.ack_link) {
          var btnAck = document.createElement("a");
          btnAck.href = alarm.ack_link;
          btnAck.className = "btn-alarm-action btn-alarm-ack";
          btnAck.textContent = "✓";
          btnAck.title = "Xác nhận đúng";
          actWrap.appendChild(btnAck);
        }
        tdActions.appendChild(actWrap);
        tr.appendChild(tdActions);
      }
    });
  }

  /* --- Existing Tab Charts (Sessions, Payments, Batteries) ------------------ */

  function initOverviewLegacy(data) {
    var hourlyEl = document.getElementById("chart-hourly-swaps");
    if (hourlyEl && typeof echarts !== "undefined" && Array.isArray(data.hourly_swaps_24h)) {
      var chart = echarts.init(hourlyEl);
      activeEchartsInstances.push(chart);
      chart.setOption({
        backgroundColor: "#ffffff",
        tooltip: { trigger: "axis" },
        legend: { data: ["OK", "Lỗi"], textStyle: { color: "#64748b" } },
        grid: { left: 45, right: 20, top: 32, bottom: 25 },
        xAxis: {
          type: "category",
          data: data.hourly_swaps_24h.map(function (p) { return p.hour_local; }),
          axisLabel: { color: "#64748b" },
          axisLine: { lineStyle: { color: "#e2e8f0" } }
        },
        yAxis: {
          type: "value",
          axisLabel: { color: "#64748b" },
          splitLine: { lineStyle: { color: "#f1f5f9" } }
        },
        series: [
          {
            name: "OK",
            type: "bar",
            stack: "total",
            itemStyle: { color: "#16a34a" },
            data: data.hourly_swaps_24h.map(function (p) { return p.ok_count; })
          },
          {
            name: "Lỗi",
            type: "bar",
            stack: "total",
            itemStyle: { color: "#dc2626" },
            data: data.hourly_swaps_24h.map(function (p) { return p.failed_count; })
          }
        ]
      });
    }
  }

  function initSessionsCharts(data) {
    if (typeof echarts === "undefined") return;

    var funnelEl = document.getElementById("chart-funnel");
    if (funnelEl && Array.isArray(data.funnel)) {
      var fChart = echarts.init(funnelEl);
      activeEchartsInstances.push(fChart);
      fChart.setOption({
        backgroundColor: "#ffffff",
        tooltip: { trigger: "axis" },
        legend: { data: ["OK", "FAILED/TIMEOUT"], textStyle: { color: "#64748b" } },
        grid: { left: 50, right: 20, top: 35, bottom: 55 },
        xAxis: {
          type: "category",
          data: data.funnel.map(function (f) { return f.step_label; }),
          axisLabel: { color: "#64748b", rotate: 20 }
        },
        yAxis: { type: "value", axisLabel: { color: "#64748b" } },
        series: [
          {
            name: "OK",
            type: "bar",
            stack: "step",
            itemStyle: { color: "#16a34a" },
            data: data.funnel.map(function (f) { return f.ok_count; })
          },
          {
            name: "FAILED/TIMEOUT",
            type: "bar",
            stack: "step",
            itemStyle: { color: "#dc2626" },
            data: data.funnel.map(function (f) { return f.failed_count; })
          }
        ]
      });
    }

    var latEl = document.getElementById("chart-step-latency");
    if (latEl && Array.isArray(data.step_latency)) {
      var lChart = echarts.init(latEl);
      activeEchartsInstances.push(lChart);
      lChart.setOption({
        backgroundColor: "#ffffff",
        tooltip: { trigger: "axis" },
        legend: { data: ["p50 (ms)", "p95 (ms)", "SLA (ms)"], textStyle: { color: "#64748b" } },
        grid: { left: 60, right: 20, top: 35, bottom: 55 },
        xAxis: {
          type: "category",
          data: data.step_latency.map(function (s) { return s.step_label; }),
          axisLabel: { color: "#64748b", rotate: 20 }
        },
        yAxis: { type: "log", min: 1, axisLabel: { color: "#64748b" } },
        series: [
          {
            name: "p50 (ms)",
            type: "bar",
            itemStyle: { color: "#2563eb" },
            data: data.step_latency.map(function (s) { return s.p50_ms || 1; })
          },
          {
            name: "p95 (ms)",
            type: "bar",
            itemStyle: { color: "#f59e0b" },
            data: data.step_latency.map(function (s) { return s.p95_ms || 1; })
          },
          {
            name: "SLA (ms)",
            type: "line",
            itemStyle: { color: "#dc2626" },
            data: data.step_latency.map(function (s) { return s.sla_ms; })
          }
        ]
      });
    }

    var matEl = document.getElementById("chart-error-matrix");
    if (matEl && data.error_matrix && Array.isArray(data.error_matrix.error_codes)) {
      var mat = data.error_matrix;
      var stepKeys = mat.steps;
      var codeKeys = mat.error_codes;
      var heatData = [];
      var maxVal = 1;
      mat.cells.forEach(function (c) {
        var x = codeKeys.indexOf(c.error_code);
        var y = stepKeys.indexOf(c.step);
        if (x >= 0 && y >= 0) {
          if (c.count > maxVal) maxVal = c.count;
          heatData.push([x, y, c.count]);
        }
      });
      var mChart = echarts.init(matEl);
      activeEchartsInstances.push(mChart);
      mChart.setOption({
        backgroundColor: "#ffffff",
        tooltip: { position: "top" },
        grid: { left: 130, right: 30, top: 20, bottom: 85 },
        xAxis: {
          type: "category",
          data: codeKeys.map(function (k) { return mat.error_labels[k] || k; }),
          axisLabel: { color: "#1e293b", rotate: 35 }
        },
        yAxis: {
          type: "category",
          data: stepKeys.map(function (s) { return mat.step_labels[s] || s; }),
          axisLabel: { color: "#1e293b" }
        },
        visualMap: {
          min: 0,
          max: maxVal,
          calculable: true,
          orient: "horizontal",
          left: "center",
          bottom: 0,
          inRange: { color: ["#f1f5f9", "#fed7aa", "#ef4444"] },
          textStyle: { color: "#64748b" }
        },
        series: [
          {
            name: "FAILED/TIMEOUT",
            type: "heatmap",
            data: heatData,
            label: { show: true, color: "#0f172a" }
          }
        ]
      });
    }
  }

  function initPaymentsCharts(data) {
    if (typeof echarts === "undefined") return;

    var toEl = document.getElementById("chart-provider-timeout");
    if (toEl && Array.isArray(data.timeout_series)) {
      var buckets = [];
      var providers = [];
      var byProvBucket = {};
      data.timeout_series.forEach(function (pt) {
        if (buckets.indexOf(pt.bucket_label) < 0) buckets.push(pt.bucket_label);
        if (providers.indexOf(pt.payment_provider) < 0) providers.push(pt.payment_provider);
        byProvBucket[pt.payment_provider + "|" + pt.bucket_label] = pt.timeout_count;
      });
      var pChart = echarts.init(toEl);
      activeEchartsInstances.push(pChart);
      pChart.setOption({
        backgroundColor: "#ffffff",
        tooltip: { trigger: "axis" },
        legend: { data: providers, textStyle: { color: "#64748b" } },
        grid: { left: 45, right: 20, top: 35, bottom: 55 },
        xAxis: { type: "category", data: buckets, axisLabel: { color: "#64748b", rotate: 25 } },
        yAxis: { type: "value", axisLabel: { color: "#64748b" } },
        series: providers.map(function (prov) {
          return {
            name: prov,
            type: "bar",
            stack: "timeout",
            data: buckets.map(function (b) { return byProvBucket[prov + "|" + b] || 0; })
          };
        })
      });
    }

    var revEl = document.getElementById("chart-daily-revenue");
    if (revEl && Array.isArray(data.daily_revenue)) {
      var dates = [];
      var currencies = [];
      var revMap = {};
      data.daily_revenue.forEach(function (r) {
        if (dates.indexOf(r.local_date) < 0) dates.push(r.local_date);
        var seriesName = r.country + " (" + r.currency + ")";
        if (currencies.indexOf(seriesName) < 0) currencies.push(seriesName);
        revMap[seriesName + "|" + r.local_date] = r.revenue;
      });
      var rChart = echarts.init(revEl);
      activeEchartsInstances.push(rChart);
      rChart.setOption({
        backgroundColor: "#ffffff",
        tooltip: { trigger: "axis" },
        legend: { data: currencies, textStyle: { color: "#64748b" } },
        grid: { left: 65, right: 20, top: 35, bottom: 45 },
        xAxis: { type: "category", data: dates, axisLabel: { color: "#64748b" } },
        yAxis: { type: "log", min: 1, axisLabel: { color: "#64748b" } },
        series: currencies.map(function (cName) {
          return {
            name: cName,
            type: "line",
            data: dates.map(function (d) { return revMap[cName + "|" + d] || null; })
          };
        })
      });
    }
  }

  function initBatteriesCharts(data) {
    if (typeof echarts === "undefined") return;

    var sohEl = document.getElementById("chart-lot-soh");
    if (sohEl && Array.isArray(data.soh_by_lot)) {
      var sChart = echarts.init(sohEl);
      activeEchartsInstances.push(sChart);
      sChart.setOption({
        backgroundColor: "#ffffff",
        tooltip: { trigger: "item" },
        grid: { left: 50, right: 20, top: 30, bottom: 35 },
        xAxis: {
          type: "category",
          data: data.soh_by_lot.map(function (b) { return b.lot_id; }),
          axisLabel: { color: "#1e293b" }
        },
        yAxis: { type: "value", min: 50, max: 100, axisLabel: { color: "#64748b" } },
        series: [
          {
            name: "SOH BoxPlot",
            type: "boxplot",
            data: data.soh_by_lot.map(function (b) {
              return [b.min_soh, b.q1_soh, b.median_soh, b.q3_soh, b.max_soh];
            }),
            itemStyle: { color: "#f8fafc", borderColor: "#2563eb" }
          }
        ]
      });
    }

    var socEl = document.getElementById("chart-soc-out");
    if (socEl && Array.isArray(data.soc_out_histogram)) {
      var cChart = echarts.init(socEl);
      activeEchartsInstances.push(cChart);
      cChart.setOption({
        backgroundColor: "#ffffff",
        tooltip: { trigger: "axis" },
        grid: { left: 50, right: 20, top: 25, bottom: 35 },
        xAxis: {
          type: "category",
          data: data.soc_out_histogram.map(function (b) { return b.bin_label; }),
          axisLabel: { color: "#64748b" }
        },
        yAxis: { type: "value", axisLabel: { color: "#64748b" } },
        series: [
          {
            name: "Lần xuất pin",
            type: "bar",
            data: data.soc_out_histogram.map(function (b) {
              return {
                value: b.count,
                itemStyle: { color: b.is_below_threshold ? "#dc2626" : "#16a34a" }
              };
            })
          }
        ]
      });
    }

    var tempEl = document.getElementById("chart-cabinet-temp");
    if (tempEl && Array.isArray(data.cabinet_temperatures)) {
      var tChart = echarts.init(tempEl);
      activeEchartsInstances.push(tChart);
      tChart.setOption({
        backgroundColor: "#ffffff",
        tooltip: { trigger: "axis" },
        grid: { left: 50, right: 20, top: 30, bottom: 65 },
        xAxis: {
          type: "category",
          data: data.cabinet_temperatures.map(function (c) { return c.cabinet_id; }),
          axisLabel: { color: "#64748b", rotate: 30 }
        },
        yAxis: { type: "value", min: 20, axisLabel: { color: "#64748b" } },
        series: [
          {
            name: "Max Temp (°C)",
            type: "bar",
            data: data.cabinet_temperatures.map(function (c) {
              var col = c.exceeds_warn_2 ? "#dc2626" : (c.exceeds_warn_1 ? "#ea580c" : "#2563eb");
              return { value: c.max_temp_c, itemStyle: { color: col } };
            })
          }
        ]
      });
    }
  }

  /* --- Main Init on DOMContentLoaded --------------------------------------- */

  document.addEventListener("DOMContentLoaded", function () {
    var pageData = parsePageData();
    initFilterControls(pageData);
    setupWidgetFullscreen();

    if (!pageData) return;

    // Overview widgets (§2.1)
    if (pageData.stations_map) {
      initStationMap(pageData.stations_map, pageData.meta);
      initStationTable(pageData.stations_map, pageData.meta);
    }
    if (pageData.temperature_timeseries) {
      initCabinetTempWidget(pageData.temperature_timeseries, pageData.meta);
    }
    if (pageData.ready_batteries_timeseries) {
      initReadyBatteriesChart(pageData.ready_batteries_timeseries);
    }
    if (pageData.alarms) {
      initAlarmsTable(pageData.alarms, pageData.meta);
    }
    if (pageData.hourly_swaps_24h) {
      initOverviewLegacy(pageData);
    }

    // Station Detail temperature chart (§2.2)
    if (pageData.station && pageData.temperature_timeseries) {
      renderCabinetTempChart(
        document.getElementById("chart-cabinet-temperature"),
        document.getElementById("cabinet-temp-legend-table"),
        pageData.temperature_timeseries
      );
    }

    // Other Tabs
    if (pageData.funnel) initSessionsCharts(pageData);
    if (pageData.by_provider) initPaymentsCharts(pageData);
    if (pageData.soh_by_lot) initBatteriesCharts(pageData);
  });
})();
