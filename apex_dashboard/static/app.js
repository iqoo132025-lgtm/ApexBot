/* APEX Live Dashboard — واجهة للقراءة فقط.
 * البيانات من /api/* (SQLite بوضع mode=ro + كاش شموع المحرك)، والأسعار الحية
 * من Binance WebSocket للعرض فقط. لا شيء هنا يرسل أمراً أو يكتب في المحرك. */
(function () {
  "use strict";

  const REFRESH_MS = 15000;
  const WS_BASE = "wss://stream.binance.com:9443/stream?streams=";
  const LWC = window.LightweightCharts;

  const state = {
    filter: "",
    positions: [],
    selectedId: null,
    status: null,
    live: {},            // PAIR -> آخر سعر من WS
    ws: null, wsKey: "",
    chart: null, rsiChart: null, candle: null, volume: null, rsiLine: null,
    chartPair: null, chartBars: [], priceLines: [],
    equityChart: null, equityLine: null,
  };

  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ── تنسيق ──
  function fmtNum(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const n = Number(v);
    if (digits === undefined) digits = priceDigits(n);
    return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  function priceDigits(p) {
    const a = Math.abs(p);
    if (a >= 1000) return 2; if (a >= 1) return 4; if (a >= 0.01) return 5; if (a >= 0.0001) return 7;
    return 9;
  }
  function fmtPct(v, digits = 2) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(digits) + "%";
  }
  function fmtUsd(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const n = Number(v);
    return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtTs(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toISOString().slice(0, 16).replace("T", " ");
  }
  function fmtAge(sec) {
    if (sec === null || sec === undefined) return "—";
    if (sec < 90) return Math.round(sec) + " ث";
    if (sec < 5400) return Math.round(sec / 60) + " د";
    if (sec < 172800) return (sec / 3600).toFixed(1) + " س";
    return (sec / 86400).toFixed(1) + " يوم";
  }
  function cls(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function api(path) {
    const r = await fetch(path, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || ("HTTP " + r.status));
    return body;
  }

  // ══════════════════════════════════════
  //  الحالة العامة
  // ══════════════════════════════════════
  function renderStatus(s) {
    state.status = s;
    $("modePill").textContent = "APEX LIVE / " + s.mode;
    const ex = $("execPill");
    ex.textContent = "auto_execute=" + (s.auto_execute ? "True" : "False");
    ex.classList.toggle("danger", !!s.auto_execute);

    const r = s.regime;
    $("kRegime").textContent = r ? r.regime : "—";
    $("kRegimeSub").textContent = r ? `${Math.round(r.score)}/100 · ${r.date}` : "لا تصنيف بعد";
    $("kEquity").textContent = fmtUsd(s.equity);
    $("kEquitySub").textContent = "البداية " + fmtUsd(s.start_equity);
    const pnl = $("kPnl");
    pnl.textContent = fmtUsd(s.pnl);
    pnl.className = "k-value " + cls(s.pnl);
    $("kPnlSub").textContent = fmtPct(s.pnl_pct);
    const u = $("kUnreal");
    u.textContent = fmtUsd(s.unrealized.usd);
    u.className = "k-value " + cls(s.unrealized.usd);
    $("kUnrealSub").textContent = `${s.unrealized.positions_marked}/${s.unrealized.positions_open} مركز · آخر إغلاق بالكاش`;
    const dd = $("kDd");
    dd.textContent = fmtPct(s.max_dd_pct);
    dd.className = "k-value " + (s.max_dd_pct < 0 ? "down" : "");
    $("cOpen").textContent = s.counts.OPEN;
    $("cPending").textContent = s.counts.PENDING;
    $("cClosed").textContent = s.counts.CLOSED;
    $("cExpired").textContent = s.counts.EXPIRED;

    // نبض الحلقة: كل دورة تكتب regime_history و paper_equity
    const age = s.last_activity_ts ? s.server_ts - s.last_activity_ts : null;
    const dot = $("loopDot");
    dot.className = "dot " + (age === null ? "" : age <= s.scan_interval_sec * 2 ? "ok" : age <= s.scan_interval_sec * 6 ? "warn" : "bad");
    $("loopText").textContent = "آخر دورة للمحرك: " + (age === null ? "—" : "قبل " + fmtAge(age));
    $("refreshText").textContent = "تحديث " + new Date().toISOString().slice(11, 19) + " UTC";
  }

  // ══════════════════════════════════════
  //  جدول المراكز
  // ══════════════════════════════════════
  function positionPnl(p) {
    const live = state.live[p.pair];
    const price = live ?? p.mark_price;
    if (p.status === "OPEN" && p.entry && price) {
      const rem = Number(p.remaining || 0);
      return { value: Number(p.realized_pct || 0) + (price / p.entry - 1) * 100 * rem, live: live !== undefined };
    }
    if (p.status === "CLOSED") return { value: Number(p.realized_pct || 0), live: false };
    return { value: null, live: false };
  }

  function renderPositions() {
    const tb = document.querySelector("#positions tbody");
    const rows = state.positions.filter((p) => !state.filter || p.status === state.filter);
    $("posEmpty").hidden = rows.length > 0;
    tb.innerHTML = rows.map((p) => {
      const price = state.live[p.pair] ?? p.mark_price;
      const pnl = positionPnl(p);
      const entry = p.status === "PENDING" || p.entry === null
        ? `≤ ${fmtNum(p.entry_high)}` : fmtNum(p.entry);
      let pnlCell = "—";
      if (p.status === "PENDING" && price && p.entry_high) {
        pnlCell = `<span class="muted">يبعد ${fmtPct((price / p.entry_high - 1) * 100)}</span>`;
      } else if (pnl.value !== null) {
        pnlCell = `<span class="${cls(pnl.value)}">${fmtPct(pnl.value)}</span>`;
      }
      const hits = ["tp1", "tp2", "tp3"].map((k) =>
        `<span class="${(p.hits || []).includes(k) ? "on" : ""}">${k.toUpperCase()}</span>`).join("");
      const stopBe = p.stop !== null && p.invalidation !== null && Number(p.stop) > Number(p.invalidation);
      return `<tr data-id="${p.id}" class="${p.id === state.selectedId ? "sel" : ""}">
        <td><b>${esc(p.symbol)}</b></td>
        <td><span class="badge b-${esc(p.status)}">${esc(p.status)}</span></td>
        <td class="num">${fmtTs(p.signaled_at)}</td>
        <td class="num">${entry}</td>
        <td class="num" data-live="${esc(p.pair)}">${price ? fmtNum(price) : "—"}${state.live[p.pair] !== undefined ? "" : price ? ' <span class="muted small">cache</span>' : ""}</td>
        <td class="num" data-pnl="${p.id}">${pnlCell}</td>
        <td class="num ${cls(p.mfe_pct)}">${p.opened_at === null ? "—" : fmtPct(p.mfe_pct)}</td>
        <td class="num ${cls(p.mae_pct)}">${p.opened_at === null ? "—" : fmtPct(p.mae_pct)}</td>
        <td class="hits">${hits}</td>
        <td class="num">${fmtNum(p.stop)}${stopBe ? ' <span class="muted small">BE</span>' : ""}</td>
        <td class="num">${p.score !== null ? Math.round(p.score) : "—"}</td>
        <td>${esc(p.regime || "—")}</td>
      </tr>`;
    }).join("");
  }

  document.querySelector("#positions tbody").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (tr) selectPosition(Number(tr.dataset.id));
  });
  $("filters").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]");
    if (!b) return;
    state.filter = b.dataset.f;
    document.querySelectorAll("#filters button").forEach((x) => x.classList.toggle("on", x === b));
    renderPositions();
  });

  // ══════════════════════════════════════
  //  الشارت
  // ══════════════════════════════════════
  function chartTheme() {
    return {
      layout: { background: { type: "solid", color: css("--panel") }, textColor: css("--muted"), fontSize: 11 },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line"), timeVisible: false },
      crosshair: { mode: 0 },
    };
  }

  function ensureCharts() {
    if (state.chart || !LWC) return;
    const main = $("chart"), rsiEl = $("rsi");
    state.chart = LWC.createChart(main, { ...chartTheme(), autoSize: true });
    state.candle = state.chart.addCandlestickSeries({
      upColor: css("--up"), downColor: css("--down"), borderVisible: false,
      wickUpColor: css("--up"), wickDownColor: css("--down"),
    });
    state.candle.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.26 } });
    state.volume = state.chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    state.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    state.ma = {
      sma20: state.chart.addLineSeries({ color: "#f0b429", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }),
      sma50: state.chart.addLineSeries({ color: "#4f8cff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }),
      ema21: state.chart.addLineSeries({ color: "#b388ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, lineStyle: 2 }),
    };
    state.rsiChart = LWC.createChart(rsiEl, { ...chartTheme(), autoSize: true });
    state.rsiLine = state.rsiChart.addLineSeries({ color: "#b388ff", lineWidth: 1, priceLineVisible: false });
    [70, 30].forEach((v) => state.rsiLine.createPriceLine({ price: v, color: css("--muted"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "" }));

    // مزامنة المحور الزمني بين الشارت و RSI
    let syncing = false;
    const link = (a, b) => a.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (syncing || !r) return; syncing = true; b.timeScale().setVisibleLogicalRange(r); syncing = false;
    });
    link(state.chart, state.rsiChart);
    link(state.rsiChart, state.chart);

    $("legend").innerHTML = [
      ["SMA20", "#f0b429"], ["SMA50", "#4f8cff"], ["EMA21", "#b388ff"],
      ["Entry", css("--entry")], ["TP1-3", css("--tp")], ["Stop-Loss", css("--sl")], ["Entry zone", css("--zone")],
    ].map(([n, c]) => `<span><i style="background:${c}"></i>${n}</span>`).join("") + "<span>RSI 14 أسفل الشارت</span>";
  }

  const LEVEL_STYLE = {
    entry: () => ({ color: css("--entry"), lineStyle: 0, lineWidth: 2 }),
    zone: () => ({ color: css("--zone"), lineStyle: 2, lineWidth: 1 }),
    tp: () => ({ color: css("--tp"), lineStyle: 1, lineWidth: 1 }),
    sl: () => ({ color: css("--sl"), lineStyle: 0, lineWidth: 2 }),
    sl_be: () => ({ color: css("--warn"), lineStyle: 2, lineWidth: 1 }),
  };
  const MARKER = {
    SIGNAL: { position: "aboveBar", shape: "arrowDown", color: "#b388ff", text: "SIGNAL" },
    FILLED: { position: "belowBar", shape: "arrowUp", color: "#4f8cff", text: "ENTRY" },
    TP1: { position: "aboveBar", shape: "circle", color: "#26a69a", text: "TP1" },
    TP2: { position: "aboveBar", shape: "circle", color: "#26a69a", text: "TP2" },
    TP3: { position: "aboveBar", shape: "circle", color: "#26a69a", text: "TP3" },
    CLOSED: { position: "aboveBar", shape: "arrowDown", color: "#ef5350", text: "EXIT" },
    EXPIRED: { position: "aboveBar", shape: "square", color: "#8b98a8", text: "EXPIRED" },
  };

  async function selectPosition(id, opts) {
    const { scroll = true, keepRange = false } = opts || {};
    state.selectedId = id;
    renderPositions();
    const p = state.positions.find((x) => x.id === id);
    if (!p) return;
    $("detail").hidden = false;
    $("dTitle").textContent = `${p.symbol} — ${p.status}`;
    $("dMeta").textContent = `#${p.id} · ${p.pair} · score ${Math.round(p.score)} · ${p.regime || "—"} · size ${p.size_pct}%`;
    let data;
    try {
      data = await api(`/api/chart/${encodeURIComponent(p.symbol)}?position_id=${id}`);
    } catch (err) {
      $("chartNote").textContent = "تعذّر تحميل الشارت: " + err.message;
      return;
    }
    if (state.selectedId !== id) return;
    renderTimeline(data.position ? data.position.timeline : []);
    if (!LWC) {
      $("chartNote").textContent = "مكتبة الشارت لم تُحمَّل (تحتاج اتصالاً بـ cdn.jsdelivr.net). الـTimeline والجدول يعملان.";
      return;
    }
    ensureCharts();
    drawChart(data, keepRange);
    if (scroll) $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function drawChart(data, keepRange) {
    const bars = data.candles || [];
    state.chartPair = data.pair;
    state.chartBars = bars.slice();
    const digits = bars.length ? priceDigits(bars[bars.length - 1].close) : 4;
    state.candle.applyOptions({ priceFormat: { type: "price", precision: digits, minMove: Math.pow(10, -digits) } });
    state.candle.setData(bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    state.volume.setData(bars.map((b) => ({ time: b.time, value: b.volume,
      color: (b.close >= b.open ? css("--up") : css("--down")) + "66" })));
    const ind = data.indicators || {};
    Object.entries(state.ma).forEach(([k, s]) => s.setData(ind[k] || []));
    // RSI يبدأ بعد 14 شمعة: نملأ ما قبله بنقاط فارغة كي يتطابق المؤشر المنطقي بين الشارتين
    const rsiAt = new Map((ind.rsi14 || []).map((p) => [p.time, p.value]));
    state.rsiLine.setData(bars.map((b) => (rsiAt.has(b.time) ? { time: b.time, value: rsiAt.get(b.time) } : { time: b.time })));

    state.priceLines.forEach((l) => state.candle.removePriceLine(l));
    state.priceLines = (data.levels || []).map((lv) => state.candle.createPriceLine({
      price: lv.price, axisLabelVisible: true, title: lv.label, ...(LEVEL_STYLE[lv.kind] || LEVEL_STYLE.zone)(),
    }));
    const markers = (data.markers || []).map((m) => ({
      time: m.time, ...(MARKER[m.stage] || MARKER.SIGNAL),
      text: (MARKER[m.stage] || {}).text + (m.source === "inferred" ? "*" : ""),
    })).sort((a, b) => a.time - b.time);
    state.candle.setMarkers(markers);

    const notes = [];
    if (!bars.length) notes.push(data.note || "لا شموع لهذا الزوج في كاش المحرك.");
    else notes.push(`شموع 1d من كاش المحرك (عمر الملف ${fmtAge(data.cache_age_sec)}). الشمعة الأخيرة تتحدث من Binance WS إن كان الزوج مدرجاً. * = زمن مستنتج.`);
    $("chartNote").textContent = notes.join(" ");
    if (bars.length && !keepRange) {
      const from = Math.max(0, bars.length - 120);
      state.chart.timeScale().setVisibleLogicalRange({ from, to: bars.length + 3 });
    }
    connectWs();
  }

  function renderTimeline(stages) {
    $("timeline").innerHTML = (stages || []).map((s) => `
      <li class="${esc(s.state)}">
        <div class="st">${esc(s.stage)}</div>
        <div class="tt">${s.ts ? fmtTs(s.ts) : s.state === "waiting" ? "بانتظار" : s.state === "skipped" ? "تخطّى" : "—"}</div>
        ${s.price !== null && s.price !== undefined ? `<div class="tp">${fmtNum(s.price)}</div>` : ""}
        ${s.source === "inferred" && s.state === "done" ? '<div class="inferred" dir="rtl">مستنتج من الشموع</div>' : ""}
        <div class="tl">latency: ${s.latency_ms ?? "— (PAPER)"}</div>
        ${s.note ? `<div class="tn" dir="auto">${esc(s.note)}</div>` : ""}
      </li>`).join("");
  }

  // ══════════════════════════════════════
  //  Binance WebSocket — عرض فقط
  // ══════════════════════════════════════
  function connectWs() {
    const pairs = new Set(state.positions.filter((p) => p.status === "OPEN" || p.status === "PENDING").map((p) => p.pair.toLowerCase()));
    const streams = [...pairs].map((p) => p + "@miniTicker");
    if (state.chartPair) streams.push(state.chartPair.toLowerCase() + "@kline_1d");
    const key = streams.sort().join("/");
    if (key === state.wsKey && state.ws && state.ws.readyState <= 1) return;
    if (state.ws) { state.ws.onclose = null; state.ws.close(); }
    state.wsKey = key;
    if (!streams.length) { setWs("", "Binance WS: لا أزواج نشطة"); return; }
    let ws;
    try { ws = new WebSocket(WS_BASE + key); } catch (e) { setWs("bad", "Binance WS: تعذّر الاتصال"); return; }
    state.ws = ws;
    setWs("warn", "Binance WS: يتصل…");
    ws.onopen = () => setWs("ok", `Binance WS: ${streams.length} stream`);
    ws.onerror = () => setWs("bad", "Binance WS: خطأ");
    ws.onclose = () => { setWs("bad", "Binance WS: انقطع — إعادة المحاولة"); state.wsKey = ""; setTimeout(connectWs, 5000); };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      const d = msg.data || {};
      if (d.e === "24hrMiniTicker") onTicker(d.s, Number(d.c));
      else if (d.e === "kline") onKline(d.s, d.k);
    };
  }
  function setWs(c, t) { $("wsDot").className = "dot " + c; $("wsText").textContent = t; }

  function onTicker(pair, price) {
    state.live[pair] = price;
    document.querySelectorAll(`[data-live="${pair}"]`).forEach((td) => {
      td.textContent = fmtNum(price);
      td.classList.remove("live-flash"); void td.offsetWidth; td.classList.add("live-flash");
    });
    state.positions.filter((p) => p.pair === pair).forEach((p) => {
      const td = document.querySelector(`[data-pnl="${p.id}"]`);
      if (!td) return;
      if (p.status === "PENDING" && p.entry_high) {
        td.innerHTML = `<span class="muted">يبعد ${fmtPct((price / p.entry_high - 1) * 100)}</span>`;
      } else {
        const v = positionPnl(p).value;
        if (v !== null) td.innerHTML = `<span class="${cls(v)}">${fmtPct(v)}</span>`;
      }
    });
  }

  function onKline(pair, k) {
    state.live[pair] = Number(k.c);
    if (!state.candle || pair !== state.chartPair) return;
    const bar = { time: Math.floor(k.t / 1000), open: +k.o, high: +k.h, low: +k.l, close: +k.c };
    const last = state.chartBars[state.chartBars.length - 1];
    if (last && bar.time < last.time) return;   // لا نكتب فوق شمعة أحدث
    state.candle.update(bar);
    state.volume.update({ time: bar.time, value: +k.q, color: (bar.close >= bar.open ? css("--up") : css("--down")) + "66" });
    if (!last || bar.time > last.time) state.chartBars.push({ ...bar, volume: +k.q });
  }

  // ══════════════════════════════════════
  //  الأداء والإشارات
  // ══════════════════════════════════════
  function renderPerformance(perf) {
    const s = perf.stats || {};
    const items = s.trades ? [
      ["صفقات مغلقة", s.trades], ["Win rate", s.win_rate + "%"], ["Expectancy", s.expectancy_r + "R"],
      ["Total", s.total_r + "R"], ["متوسط الرابحة", s.avg_win_r + "R"], ["متوسط الخاسرة", s.avg_loss_r + "R"],
      ["متوسط MFE", fmtPct(s.avg_mfe_pct)], ["متوسط MAE", fmtPct(s.avg_mae_pct)],
      ["متوسط المدة", s.avg_hold_days + " يوم"], ["Max DD", fmtPct(s.max_dd_pct)],
    ] : [["صفقات مغلقة", 0], ["ملاحظة", s.note || "التجربة ما زالت جارية"]];
    const exp = Object.entries(s.expired || {});
    if (exp.length) items.push(["EXPIRED", exp.map(([k, v]) => `${v} ${k}`).join(", ")]);
    $("stats").innerHTML = items.map(([l, v]) => `<div class="stat"><div class="l">${esc(l)}</div><div class="v">${esc(v)}</div></div>`).join("");

    if (!LWC) return;
    if (!state.equityChart) {
      state.equityChart = LWC.createChart($("equityChart"), { ...chartTheme(), autoSize: true });
      state.equityLine = state.equityChart.addLineSeries({ color: css("--accent"), lineWidth: 2 });
      state.equityLine.createPriceLine({ price: perf.start_equity, color: css("--muted"), lineStyle: 2, lineWidth: 1, title: "start" });
    }
    // منحنى المحقق بعد كل صفقة، وإن لم تُغلق صفقة بعد نعرض اللقطات اليومية
    let pts = (perf.realized_curve || []).map((p) => ({ time: p.closed_at, value: p.equity }));
    if (!pts.length) pts = (perf.daily_equity || []).map((p) => ({ time: p.ts, value: p.equity }));
    const dedup = [];
    pts.sort((a, b) => a.time - b.time).forEach((p) => {
      if (dedup.length && dedup[dedup.length - 1].time >= p.time) p = { ...p, time: dedup[dedup.length - 1].time + 1 };
      dedup.push(p);
    });
    state.equityLine.setData(dedup);
    state.equityChart.timeScale().fitContent();
  }

  function renderSignals(list) {
    document.querySelector("#signals tbody").innerHTML = list.slice(0, 30).map((s) => `
      <tr><td class="num">${fmtTs(s.ts)}</td><td><b>${esc(s.symbol)}</b></td><td class="num">${s.rank ?? "—"}</td>
      <td class="num">${s.score !== null ? Math.round(s.score) : "—"}</td><td>${esc(s.regime || "—")}</td>
      <td class="num">${fmtNum(s.entry_low)} – ${fmtNum(s.entry_high)}</td></tr>`).join("")
      || '<tr><td colspan="6" class="empty">لا إشارات بعد</td></tr>';
  }

  // ══════════════════════════════════════
  //  الدورة
  // ══════════════════════════════════════
  async function refresh() {
    try {
      const [status, pos, perf, sig] = await Promise.all([
        api("/api/status"), api("/api/positions"), api("/api/performance"), api("/api/signals?limit=50"),
      ]);
      renderStatus(status);
      state.positions = pos.positions;
      renderPositions();
      renderPerformance(perf);
      renderSignals(sig.signals);
      connectWs();
      // الشارت المفتوح يتبع الدورة أيضاً (مستويات، علامات، Timeline) دون إعادة ضبط التكبير
      if (state.selectedId !== null && !$("detail").hidden) selectPosition(state.selectedId, { scroll: false, keepRange: true });
    } catch (err) {
      $("loopDot").className = "dot bad";
      $("loopText").textContent = "خطأ في API: " + err.message;
    }
  }

  refresh().then(() => {
    const first = state.positions.find((p) => p.status === "OPEN") || state.positions[0];
    if (first) selectPosition(first.id, { scroll: false });
  });
  setInterval(refresh, REFRESH_MS);
})();
