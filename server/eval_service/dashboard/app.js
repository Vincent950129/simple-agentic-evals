(() => {
  "use strict";

  const state = { data: null, selected: null, timer: null, loading: false };
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const number = (value) => new Intl.NumberFormat("en-US").format(value || 0);
  const percent = (value, digits = 1) => value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
  const timeAgo = (value) => {
    if (!value) return "—";
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (seconds < 60) return `${Math.floor(seconds)}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  };
  const shortDate = (value) => value ? new Date(value).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
  }) : "—";

  function setConnection(connected, message) {
    const status = $("liveStatus");
    status.classList.toggle("muted", !connected);
    status.innerHTML = `<i></i> ${esc(message || (connected ? "live · 10s" : "disconnected"))}`;
  }

  async function loadData({ first = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    $("refreshButton").classList.add("loading");
    try {
      const response = await fetch("/api/analytics?recent=50&days=60", { cache: "no-store" });
      if (!response.ok) {
        let detail = "Unable to load analytics.";
        try { detail = (await response.json()).detail || detail; } catch (_) { /* no-op */ }
        throw new Error(`${detail} (${response.status})`);
      }
      state.data = await response.json();
      if (!state.selected || !state.data.users.some((u) => u.user === state.selected)) {
        state.selected = state.data.users[0]?.user || null;
      }
      $("refreshButton").disabled = false;
      setConnection(true);
      render();
      if (first) {
        clearInterval(state.timer);
        state.timer = setInterval(loadData, 10000);
      }
    } catch (error) {
      setConnection(false, "connection failed");
      if (first) {
        $("refreshButton").disabled = false;
      }
      const notice = $("coverageNotice");
      notice.classList.remove("hidden");
      notice.textContent = error.message;
    } finally {
      state.loading = false;
      $("refreshButton").classList.remove("loading");
    }
  }

  function render() {
    renderHeader();
    renderKpis();
    renderUsers();
    renderTrend();
    renderRecent();
  }

  function renderHeader() {
    const { aggregation, generated_at: generatedAt } = state.data;
    $("freshness").textContent = `Updated ${timeAgo(generatedAt)} · aggregate through request #${number(aggregation.cursor)}`;
    const notice = $("coverageNotice");
    if (!aggregation.ready || aggregation.progress < 1) {
      notice.classList.remove("hidden");
      notice.textContent = `Historical aggregation is ${percent(aggregation.progress)} complete. Current totals are partial and will update automatically.`;
    } else {
      notice.classList.add("hidden");
    }
  }

  function renderKpis() {
    const t = state.data.totals;
    const unknownShare = t.requests ? t.unknown_requests / t.requests : 0;
    const successRate = t.requests ? t.successful_requests / t.requests : null;
    const gradeRate = t.grade_attempts ? t.successful_grades / t.grade_attempts : null;
    const cards = [
      ["Total calls", number(t.requests), `${number(t.successful_requests)} successful`, ""],
      ["Issued users", number(t.issued_users ?? t.known_users), `${number(t.active_known_users ?? t.known_users)} with traffic`, ""],
      ["Unknown traffic", percent(unknownShare), `${number(t.unknown_requests)} calls`, unknownShare > .5 ? "warn" : ""],
      ["Sessions", number(t.sessions_created), "successfully created", ""],
      ["Grade attempts", number(t.grade_attempts), `${percent(gradeRate)} successful`, ""],
      ["Request health", percent(successRate, 2), `${number(t.requests - t.successful_requests)} errors`, ""],
    ];
    $("kpis").innerHTML = cards.map(([label, value, sub, klass]) =>
      `<article class="kpi ${klass}"><span class="label">${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(sub)}</small></article>`
    ).join("");
  }

  function renderUsers() {
    const query = $("userSearch").value.trim().toLowerCase();
    const users = state.data.users.filter((u) => u.user.toLowerCase().includes(query));
    $("userCount").textContent = `${number(users.length)} shown`;
    $("emptyUsers").classList.toggle("hidden", users.length !== 0);
    $("usersBody").innerHTML = users.map((user) => {
      const unknown = user.user === "unknown user";
      return `<tr data-user="${esc(user.user)}" class="${user.user === state.selected ? "selected" : ""}">
        <td><span class="user-name"><i class="avatar ${unknown ? "unknown" : ""}">${unknown ? "?" : esc(user.user.slice(0, 1).toUpperCase())}</i>${esc(user.user)}</span></td>
        <td>${number(user.requests)}</td>
        <td class="${user.success_rate >= .95 ? "metric-good" : "metric-bad"}">${percent(user.success_rate)}</td>
        <td>${number(user.sessions_created)}</td>
        <td>${number(user.grade_attempts)}</td>
        <td>${esc(timeAgo(user.last_request))}</td>
      </tr>`;
    }).join("");
    $("usersBody").querySelectorAll("tr").forEach((row) => row.addEventListener("click", () => {
      state.selected = row.dataset.user;
      renderUsers();
    }));
    renderUserDetail();
  }

  function renderUserDetail() {
    const user = state.data.users.find((item) => item.user === state.selected);
    if (!user) {
      $("userDetail").innerHTML = '<p class="empty">Select a user to inspect usage.</p>';
      return;
    }
    const breakdown = (title, values) => {
      const entries = Object.entries(values || {}).sort((a, b) => b[1] - a[1]);
      if (!entries.length) return "";
      const max = entries[0][1] || 1;
      return `<div class="breakdown"><h3>${esc(title)}</h3>${entries.map(([label, count]) =>
        `<div class="bar-row" title="${esc(label)}: ${number(count)}"><span class="bar-label">${esc(label)}</span><span class="bar-track"><i class="bar-fill" style="width:${Math.max(2, count / max * 100)}%"></i></span><span class="bar-count">${number(count)}</span></div>`
      ).join("")}</div>`;
    };
    $("userDetail").innerHTML = `
      <h3 class="detail-title">${esc(user.user)}</h3>
      <p class="detail-meta">First seen ${esc(shortDate(user.first_request))} · ${number(user.unique_ips)} source IP${user.unique_ips === 1 ? "" : "s"}</p>
      <div class="detail-stats">
        <div class="detail-stat"><span>Errors</span><strong>${number(user.errors)}</strong></div>
        <div class="detail-stat"><span>Avg grade</span><strong>${percent(user.avg_pass_rate)}</strong></div>
        <div class="detail-stat"><span>Sessions</span><strong>${number(user.sessions_created)}</strong></div>
        <div class="detail-stat"><span>Grades</span><strong>${number(user.grade_attempts)}</strong></div>
      </div>
      ${breakdown("Service functions", user.by_route)}
      ${breakdown("Sessions by dataset", user.sessions_by_dataset)}
      ${breakdown("Sessions by benchmark", user.sessions_by_benchmark)}`;
  }

  function renderTrend() {
    const entries = Object.entries(state.data.by_day || {});
    if (!entries.length) {
      $("trendChart").innerHTML = '<p class="empty">No daily traffic yet.</p>';
      return;
    }
    const width = 800, height = 235, left = 43, right = 10, top = 12, bottom = 28;
    const chartW = width - left - right, chartH = height - top - bottom;
    const max = Math.max(...entries.map(([, v]) => v.requests), 1);
    const x = (i) => left + (entries.length === 1 ? chartW / 2 : i / (entries.length - 1) * chartW);
    const y = (v) => top + chartH - v / max * chartH;
    const points = entries.map(([, v], i) => `${x(i).toFixed(1)},${y(v.requests).toFixed(1)}`).join(" ");
    const area = `${left},${top + chartH} ${points} ${left + chartW},${top + chartH}`;
    const grid = [0, .25, .5, .75, 1].map((ratio) => {
      const yy = top + chartH * ratio;
      return `<line class="grid-line" x1="${left}" y1="${yy}" x2="${left + chartW}" y2="${yy}"/><text class="axis-label" x="${left - 7}" y="${yy + 3}" text-anchor="end">${esc(number(Math.round(max * (1 - ratio))))}</text>`;
    }).join("");
    const labelIndexes = [...new Set([0, Math.floor((entries.length - 1) / 2), entries.length - 1])];
    const labels = labelIndexes.map((i) => `<text class="axis-label" x="${x(i)}" y="${height - 5}" text-anchor="middle">${esc(entries[i][0].slice(5))}</text>`).join("");
    $("trendChart").innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Daily request volume">
      <defs><linearGradient id="trendGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4ee6a4" stop-opacity=".25"/><stop offset="1" stop-color="#4ee6a4" stop-opacity="0"/></linearGradient></defs>
      ${grid}<polygon class="area" points="${area}"/><polyline class="trend-line" points="${points}"/>${labels}
    </svg>`;
  }

  function renderRecent() {
    const rows = state.data.recent || [];
    $("recentList").innerHTML = rows.length ? rows.map((row) => `
      <div class="activity">
        <i class="activity-dot ${row.success ? "" : "error"}"></i>
        <div class="activity-main"><strong>${esc(row.user)}</strong><small>${esc(row.route || row.method)}${row.dataset ? ` · ${esc(row.dataset)}` : ""}${row.benchmark ? ` / ${esc(row.benchmark.toUpperCase())}` : ""}</small></div>
        <div class="activity-time">${esc(timeAgo(row.ts))}<br><span class="status-code ${row.success ? "metric-good" : "metric-bad"}">${esc(row.status)}</span></div>
      </div>`).join("") : '<p class="empty">No requests recorded yet.</p>';
  }

  $("refreshButton").addEventListener("click", () => loadData());
  $("userSearch").addEventListener("input", renderUsers);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadData();
  });
  loadData({ first: true });
})();
