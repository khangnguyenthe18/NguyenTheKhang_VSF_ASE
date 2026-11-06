/* ASE BSS NOC Dashboard JS — ECharts 5.5.1 + Leaflet 1.9.4
 * Nguyên tắc bảo mật (DASHBOARD_PLAN §2.6): chỉ dùng textContent, tuyệt đối không dùng innerHTML với dữ liệu.
 */
(function () {
  "use strict";

  function parsePageData() {
    var el = document.getElementById("page-data");
    if (!el || !el.textContent) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (_e) {
      return null;
    }
  }

  function initFilterControls(pageData) {
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

  function buildStationPopupNode(st, meta) {
    var wrap = document.createElement("div");
    var title = document.createElement("strong");
    title.textContent = st.station_id + " (" + st.country + " · " + st.timezone_offset + ")";
    wrap.appendChild(title);

    var lines = [
      "Giờ địa phương: " + st.as_of_local,
      "Tủ online: " + st.cabinets_online + "/" + st.cabinets_total,
      "Pin sẵn sàng: " + st.ready_batteries + "/" + st.total_slots,
      "Lỗi 1h: " + st.failed_1h + "/" + st.swaps_1h + " (" + (st.fail_rate_1h * 100).toFixed(1) + "%)",
      "Lượt đổi hôm nay: " + st.swaps_today
    ];
    lines.forEach(function (txt) {
      var p = document.createElement("div");
      p.textContent = txt;
      wrap.appendChild(p);
    });

    var link = document.createElement("a");
    var q = "?profile=" + encodeURIComponent(meta.profile) + "&as_of=" + encodeURIComponent(meta.as_of);
    if (meta.country) q += "&country=" + encodeURIComponent(meta.country);
    link.href = "/dashboard/stations/" + encodeURIComponent(st.station_id) + q;
    link.textContent = "Xem chi tiết trạm →";
    wrap.appendChild(link);
    return wrap;
  }

  function initOverview(data) {
    var mapEl = document.getElementById("stations-map");
    if (mapEl && typeof L !== "undefined" && Array.isArray(data.stations_map)) {
      var map = L.map(mapEl).setView([10.5, 112.0], 4);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 18,
        attribution: "© OpenStreetMap"
      }).addTo(map);

      var colorHex = { green: "#22c55e", yellow: "#eab308", red: "#ef4444" };
      var bounds = [];
      data.stations_map.forEach(function (st) {
        var col = colorHex[st.status_color] || "#38bdf8";
        var marker = L.circleMarker([st.lat, st.lon], {
          radius: 9,
          color: "#0f172a",
          weight: 2,
          fillColor: col,
          fillOpacity: 0.92
        }).addTo(map);
        marker.bindPopup(buildStationPopupNode(st, data.meta));
        bounds.push([st.lat, st.lon]);
      });
      if (bounds.length === 1) {
        map.setView(bounds[0], 11);
      } else if (bounds.length > 1) {
        map.fitBounds(bounds, { padding: [30, 30] });
      }
    }

    var hourlyEl = document.getElementById("chart-hourly-swaps");
    if (hourlyEl && typeof echarts !== "undefined" && Array.isArray(data.hourly_swaps_24h)) {
      var chart = echarts.init(hourlyEl);
      chart.setOption({
        tooltip: { trigger: "axis" },
        legend: { data: ["OK", "Lỗi"], textStyle: { color: "#cbd5e1" } },
        grid: { left: 45, right: 20, top: 35, bottom: 30 },
        xAxis: {
          type: "category",
          data: data.hourly_swaps_24h.map(function (p) { return p.hour_local; }),
          axisLabel: { color: "#94a3b8" }
        },
        yAxis: { type: "value", axisLabel: { color: "#94a3b8" } },
        series: [
          {
            name: "OK",
            type: "bar",
            stack: "total",
            itemStyle: { color: "#22c55e" },
            data: data.hourly_swaps_24h.map(function (p) { return p.ok_count; })
          },
          {
            name: "Lỗi",
            type: "bar",
            stack: "total",
            itemStyle: { color: "#ef4444" },
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
      fChart.setOption({
        tooltip: { trigger: "axis" },
        legend: { data: ["OK", "FAILED/TIMEOUT"], textStyle: { color: "#cbd5e1" } },
        grid: { left: 50, right: 20, top: 35, bottom: 55 },
        xAxis: {
          type: "category",
          data: data.funnel.map(function (f) { return f.step_label; }),
          axisLabel: { color: "#94a3b8", rotate: 20 }
        },
        yAxis: { type: "value", axisLabel: { color: "#94a3b8" } },
        series: [
          {
            name: "OK",
            type: "bar",
            stack: "step",
            itemStyle: { color: "#22c55e" },
            data: data.funnel.map(function (f) { return f.ok_count; })
          },
          {
            name: "FAILED/TIMEOUT",
            type: "bar",
            stack: "step",
            itemStyle: { color: "#ef4444" },
            data: data.funnel.map(function (f) { return f.failed_count; })
          }
        ]
      });
    }

    var latEl = document.getElementById("chart-step-latency");
    if (latEl && Array.isArray(data.step_latency)) {
      var lChart = echarts.init(latEl);
      lChart.setOption({
        tooltip: { trigger: "axis" },
        legend: { data: ["p50 (ms)", "p95 (ms)", "SLA (ms)"], textStyle: { color: "#cbd5e1" } },
        grid: { left: 60, right: 20, top: 35, bottom: 55 },
        xAxis: {
          type: "category",
          data: data.step_latency.map(function (s) { return s.step_label; }),
          axisLabel: { color: "#94a3b8", rotate: 20 }
        },
        yAxis: { type: "log", min: 1, axisLabel: { color: "#94a3b8" } },
        series: [
          {
            name: "p50 (ms)",
            type: "bar",
            itemStyle: { color: "#38bdf8" },
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
            itemStyle: { color: "#ef4444" },
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
      mChart.setOption({
        tooltip: { position: "top" },
        grid: { left: 130, right: 30, top: 20, bottom: 85 },
        xAxis: {
          type: "category",
          data: codeKeys.map(function (k) { return mat.error_labels[k] || k; }),
          axisLabel: { color: "#cbd5e1", rotate: 35 }
        },
        yAxis: {
          type: "category",
          data: stepKeys.map(function (s) { return mat.step_labels[s] || s; }),
          axisLabel: { color: "#cbd5e1" }
        },
        visualMap: {
          min: 0,
          max: maxVal,
          calculable: true,
          orient: "horizontal",
          left: "center",
          bottom: 0,
          inRange: { color: ["#1e293b", "#f59e0b", "#ef4444"] },
          textStyle: { color: "#cbd5e1" }
        },
        series: [
          {
            name: "FAILED/TIMEOUT",
            type: "heatmap",
            data: heatData,
            label: { show: true, color: "#f8fafc" }
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
      pChart.setOption({
        tooltip: { trigger: "axis" },
        legend: { data: providers, textStyle: { color: "#cbd5e1" } },
        grid: { left: 45, right: 20, top: 35, bottom: 55 },
        xAxis: { type: "category", data: buckets, axisLabel: { color: "#94a3b8", rotate: 25 } },
        yAxis: { type: "value", axisLabel: { color: "#94a3b8" } },
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
      rChart.setOption({
        tooltip: { trigger: "axis" },
        legend: { data: currencies, textStyle: { color: "#cbd5e1" } },
        grid: { left: 65, right: 20, top: 35, bottom: 45 },
        xAxis: { type: "category", data: dates, axisLabel: { color: "#94a3b8" } },
        yAxis: { type: "log", min: 1, axisLabel: { color: "#94a3b8" } },
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
      sChart.setOption({
        tooltip: { trigger: "item" },
        grid: { left: 50, right: 20, top: 30, bottom: 35 },
        xAxis: {
          type: "category",
          data: data.soh_by_lot.map(function (b) { return b.lot_id; }),
          axisLabel: { color: "#cbd5e1" }
        },
        yAxis: { type: "value", min: 50, max: 100, axisLabel: { color: "#94a3b8" } },
        series: [
          {
            name: "SOH BoxPlot",
            type: "boxplot",
            data: data.soh_by_lot.map(function (b) {
              return [b.min_soh, b.q1_soh, b.median_soh, b.q3_soh, b.max_soh];
            }),
            itemStyle: { color: "#1e293b", borderColor: "#38bdf8" }
          }
        ]
      });
    }

    var socEl = document.getElementById("chart-soc-out");
    if (socEl && Array.isArray(data.soc_out_histogram)) {
      var cChart = echarts.init(socEl);
      cChart.setOption({
        tooltip: { trigger: "axis" },
        grid: { left: 50, right: 20, top: 25, bottom: 35 },
        xAxis: {
          type: "category",
          data: data.soc_out_histogram.map(function (b) { return b.bin_label; }),
          axisLabel: { color: "#94a3b8" }
        },
        yAxis: { type: "value", axisLabel: { color: "#94a3b8" } },
        series: [
          {
            name: "Lần xuất pin",
            type: "bar",
            data: data.soc_out_histogram.map(function (b) {
              return {
                value: b.count,
                itemStyle: { color: b.is_below_threshold ? "#ef4444" : "#22c55e" }
              };
            })
          }
        ]
      });
    }

    var tempEl = document.getElementById("chart-cabinet-temp");
    if (tempEl && Array.isArray(data.cabinet_temperatures)) {
      var tChart = echarts.init(tempEl);
      tChart.setOption({
        tooltip: { trigger: "axis" },
        grid: { left: 50, right: 20, top: 30, bottom: 65 },
        xAxis: {
          type: "category",
          data: data.cabinet_temperatures.map(function (c) { return c.cabinet_id; }),
          axisLabel: { color: "#94a3b8", rotate: 30 }
        },
        yAxis: { type: "value", min: 20, axisLabel: { color: "#94a3b8" } },
        series: [
          {
            name: "Max Temp (°C)",
            type: "bar",
            data: data.cabinet_temperatures.map(function (c) {
              var col = c.exceeds_warn_2 ? "#ef4444" : (c.exceeds_warn_1 ? "#eab308" : "#38bdf8");
              return { value: c.max_temp_c, itemStyle: { color: col } };
            })
          }
        ]
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var pageData = parsePageData();
    initFilterControls(pageData);
    if (!pageData) return;

    if (pageData.stations_map) initOverview(pageData);
    if (pageData.funnel) initSessionsCharts(pageData);
    if (pageData.by_provider) initPaymentsCharts(pageData);
    if (pageData.soh_by_lot) initBatteriesCharts(pageData);
  });
})();

