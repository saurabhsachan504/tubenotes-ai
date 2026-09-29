/* TubeNotes admin dashboard.  No external chart or UI library is needed. */
(function () {
  "use strict";
  const API = "/api/v1/admin";
  const $ = (id) => document.getElementById(id);
  let data = null;
  let operationData = null;
  let operationCategory = "jobs";
  let errorData = null;
  let billingData = null;
  let settingsData = null;
  let dashboardLoading = false;
  let usersLoading = false;
  let operationsLoading = false;
  let errorsLoading = false;
  let billingLoading = false;
  let settingsLoading = false;
  let autoUpdateInFlight = false;
  let lastSettingsAutoUpdate = 0;

  function currentTheme() { return document.documentElement.dataset.adminTheme === "light" ? "light" : "dark"; }
  function applyTheme(theme, persist = true) {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.adminTheme = next;
    if (persist) { try { localStorage.setItem("tn_admin_theme", next); } catch (_) {} }
    const button = $("themeToggle");
    if (!button) return;
    const switchTo = next === "light" ? "dark" : "light";
    button.textContent = switchTo === "light" ? "Light" : "Dark";
    button.title = "Switch to " + switchTo + " mode";
    button.setAttribute("aria-label", button.title);
    button.setAttribute("aria-pressed", String(next === "light"));
  }
  function installThemeToggle() {
    const button = document.createElement("button");
    button.id = "themeToggle";
    button.type = "button";
    button.className = "icon-btn theme-toggle";
    $("refresh").before(button);
    button.onclick = () => applyTheme(currentTheme() === "light" ? "dark" : "light");
    applyTheme(currentTheme(), false);
  }

  function tokens() { try { return JSON.parse(localStorage.getItem("tn_tokens") || "null"); } catch (_) { return null; } }
  async function bearer() {
    const saved = tokens();
    if (!saved) return null;
    if (!saved.expires_at || saved.expires_at > Date.now()) return saved.access_token;
    try {
      const r = await fetch("/api/v1/auth/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: saved.refresh_token }) });
      if (!r.ok) throw new Error();
      const fresh = await r.json();
      const next = { access_token: fresh.access_token, refresh_token: fresh.refresh_token, expires_at: Date.now() + (fresh.expires_in - 60) * 1000 };
      localStorage.setItem("tn_tokens", JSON.stringify(next));
      return next.access_token;
    } catch (_) { localStorage.removeItem("tn_tokens"); return null; }
  }
  async function api(path, options = {}) {
    const token = await bearer();
    if (!token) { const e = new Error("Sign in required"); e.status = 401; throw e; }
    const headers = { Authorization: "Bearer " + token, ...(options.headers || {}) };
    const r = await fetch(API + path, { ...options, headers, cache: "no-store" });
    if (!r.ok) { const e = new Error("Admin access required"); e.status = r.status; throw e; }
    return r.json();
  }
  async function signOutAdmin() {
    const button = $("adminSignoutBtn");
    button.disabled = true;
    try {
      // Obtain a current token first; bearer() safely rotates an expired
      // access token before its matching refresh token is revoked server-side.
      const token = await bearer();
      const saved = tokens();
      if (token && saved && saved.refresh_token) {
        await fetch("/api/v1/auth/logout", {
          method: "POST",
          headers: {"Content-Type": "application/json", Authorization: "Bearer " + token},
          body: JSON.stringify({refresh_token: saved.refresh_token}),
          cache: "no-store",
        });
      }
    } catch (_) {
      // Local token removal still protects this browser if the connection was
      // interrupted while attempting the server-side logout.
    } finally {
      localStorage.removeItem("tn_tokens");
      window.location.replace("/");
    }
  }
  function esc(value) { const e = document.createElement("span"); e.textContent = value == null ? "—" : String(value); return e.innerHTML; }
  function number(value) { return Number(value || 0).toLocaleString(); }
  function ago(iso) { const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000); if (seconds < 60) return "just now"; if (seconds < 3600) return Math.round(seconds / 60) + "m ago"; if (seconds < 86400) return Math.round(seconds / 3600) + "h ago"; return new Date(iso).toLocaleDateString(); }
  function elapsed(ms) { if (ms == null) return "—"; const s = ms / 1000; return s < 60 ? s.toFixed(s < 10 ? 1 : 0) + "s" : Math.floor(s / 60) + "m " + Math.round(s % 60) + "s"; }
  const languages = {hi:"Hindi",en:"English",gu:"Gujarati",pa:"Punjabi",mr:"Marathi",bn:"Bengali",ta:"Tamil",te:"Telugu",kn:"Kannada",ml:"Malayalam",ur:"Urdu",or:"Odia",as:"Assamese",ne:"Nepali",sa:"Sanskrit",es:"Spanish",fr:"French",de:"German",pt:"Portuguese",it:"Italian",nl:"Dutch",ru:"Russian",uk:"Ukrainian",ar:"Arabic",fa:"Persian",tr:"Turkish",he:"Hebrew",zh:"Chinese",ja:"Japanese",ko:"Korean",th:"Thai",vi:"Vietnamese",id:"Indonesian",ms:"Malay",pl:"Polish",ro:"Romanian",el:"Greek",sv:"Swedish",cs:"Czech",hu:"Hungarian",fi:"Finnish",da:"Danish",no:"Norwegian",sw:"Swahili",si:"Sinhala"};
  function language(code) { return languages[String(code || "").toLowerCase()] || code || "Auto"; }
  function dateTime(iso) { return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Never"; }
  function planText(subscription) {
    if (!subscription) return "Free trial";
    const amount = Number(subscription.price_subunits || 0) / 100;
    const currency = String(subscription.currency || "").toUpperCase();
    const money = currency === "INR" ? "₹" + amount.toFixed(0) : (currency === "USD" ? "$" : currency + " ") + amount.toFixed(2);
    return money + "/month · " + String(subscription.status || "unknown");
  }
  function tableEmpty(columns, message) { return `<tr><td colspan="${columns}" class="muted">${esc(message)}</td></tr>`; }

  function lineChart(el, labels, values) {
    const width = 580, height = 164, left = 24, bottom = 22, top = 11, right = 6;
    const max = Math.max(2, ...values);
    const points = values.map((value, i) => {
      const x = left + (i * (width - left - right) / Math.max(1, values.length - 1));
      const y = top + ((max - value) / max) * (height - top - bottom);
      return [x, y];
    });
    const d = points.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    const area = d + ` L ${points.at(-1)[0]} ${height - bottom} L ${points[0][0]} ${height - bottom} Z`;
    const grid = [0, .25, .5, .75, 1].map(f => { const y = top + f * (height - top - bottom); return `<line class="axis" x1="${left}" x2="${width-right}" y1="${y}" y2="${y}"/>`; }).join("");
    const dots = points.map(p => `<circle class="dot" cx="${p[0]}" cy="${p[1]}" r="3.4"/>`).join("");
    const xlabels = points.map((p, i) => `<text class="chart-label" x="${p[0]}" y="${height-4}" text-anchor="middle">${esc(labels[i])}</text>`).join("");
    el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"><defs><linearGradient id="lineGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#347fff" stop-opacity=".4"/><stop offset="1" stop-color="#347fff" stop-opacity="0"/></linearGradient></defs>${grid}<path class="line-fill" d="${area}"/><path class="line-path" d="${d}"/>${dots}${xlabels}</svg>`;
  }
  function barChart(el, labels, groups) {
    const width = 560, height = 164, left = 6, bottom = 23, top = 10, right = 5;
    const sets = Object.entries(groups), max = Math.max(2, ...sets.flatMap(([, v]) => v));
    const band = (width - left - right) / Math.max(1, labels.length), barW = Math.max(2, Math.min(12, band / (sets.length + 1)));
    const colors = ["#47d99c", "#ff5c62", "#ffc234"];
    const grid = [0,.25,.5,.75,1].map(f => { const y = top + f*(height-top-bottom); return `<line class="axis" x1="${left}" x2="${width-right}" y1="${y}" y2="${y}"/>`; }).join("");
    let bars = "";
    sets.forEach(([, values], setIndex) => values.forEach((value, i) => { const h = (value / max) * (height-top-bottom); const x = left + band*i + (band - barW*sets.length)/2 + setIndex*barW; bars += `<rect class="bar" fill="${colors[setIndex]}" x="${x}" y="${height-bottom-h}" width="${Math.max(1,barW-1)}" height="${h}"/>`; }));
    const xlabels = labels.map((label,i) => `<text class="chart-label" x="${left+band*i+band/2}" y="${height-4}" text-anchor="middle">${esc(label)}</text>`).join("");
    el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${grid}${bars}${xlabels}</svg>`;
  }
  function countryLabel(value) {
    const code = String(value || "Unknown").trim().toUpperCase();
    if (code === "UNKNOWN") return "Unknown";
    if (code === "INTL") return "International";
    if (!/^[A-Z]{2}$/.test(code)) return code;
    try { return new Intl.DisplayNames([navigator.language || "en"], {type: "region"}).of(code) || code; }
    catch (_) { return code; }
  }
  function countryChart(countries, total) {
    const colors = ["#3d83ff", "#43d69b", "#ffba26", "#fb6571", "#8d72ff", "#8190a5"];
    const safeTotal = Math.max(1, total);
    let end = 0; const parts = countries.map((c,i) => { const start=end; end += c.count/safeTotal*100; return `${colors[i%colors.length]} ${start.toFixed(1)}% ${end.toFixed(1)}%`; });
    const rows = countries.map((c,i) => `<li><span><i style="background:${colors[i%colors.length]}"></i>${esc(countryLabel(c.name))}</span><b>${number(c.count)}</b></li>`).join("");
    $("countryChart").innerHTML = `<div class="donut" style="background:conic-gradient(${parts.join(",")})"><strong>${number(total)}</strong><small>Users</small></div><ul class="country-list">${rows || "<li>No user data yet</li>"}</ul>`;
  }
  function addJobLocationCells(bodyId, jobs) {
    const rows = $(bodyId).querySelectorAll("tr");
    if (!jobs.length) {
      const empty = $(bodyId).querySelector("td[colspan]");
      if (empty) empty.colSpan = bodyId === "operationsRows" ? 12 : 11;
      return;
    }
    rows.forEach((row, index) => {
      const job = jobs[index];
      if (!job) return;
      const city = document.createElement("td");
      city.textContent = job.city || "Unknown";
      const country = document.createElement("td");
      country.textContent = countryLabel(job.country);
      // Both tables keep the email/user cell as the second or first cell.
      row.children[bodyId === "operationsRows" ? 0 : 1].after(city, country);
    });
  }
  function healthRow(icon, label, detail, kind="good") { return `<div class="health-row"><div class="health-name"><span>${icon}</span><span class="health-label">${esc(label)}</span></div><div class="health-detail ${kind}">${esc(detail)}</div></div>`; }
  function renderHealth(health) {
    const v = health.vllm || {}, g = health.gpu || {}, storage = health.storage || {};
    const vllm = v.running == null ? "Metrics unavailable" : `${v.running} running · ${v.waiting || 0} waiting`;
    const gpu = g.available ? `${g.utilization}% · ${g.temperature}°C · ${g.memory_used}/${g.memory_total} MiB` : "nvidia-smi unavailable";
    const disk = storage.total_gb == null ? "Unavailable" : `${storage.used_gb} GB / ${storage.total_gb} GB`;
    const uptime = Math.floor((health.uptime_seconds || 0) / 60);
    $("healthRows").innerHTML = [
      healthRow("◉", "vLLM", vllm, v.running == null ? "warn" : "good"),
      healthRow("▰", "GPU", gpu, g.available ? "good" : "warn"),
      healthRow("▤", "Database", health.database === "connected" ? "Connected" : "Unavailable", health.database === "connected" ? "good" : "warn"),
      healthRow("▣", "Storage", disk, "good"),
      healthRow("◌", "Queue", `${v.jobs || 0} notes jobs · ${v.capacity || 0} capacity`, (v.waiting || 0) ? "warn" : "good"),
      healthRow("◷", "API uptime", `${uptime} min this process`, "good"),
    ].join("");
  }
  function renderHealthDetails() {
    const rows = $("healthDetailRows");
    if (!rows) return;
    rows.innerHTML = $("healthRows").innerHTML;
    $("healthDetailUpdated").textContent = "Live - updated " + new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  }
  function renderTables(d) {
    const q = $("search").value.trim().toLowerCase();
    const jobs = d.recent_jobs.filter(j => !q || [j.email,j.city,j.country,j.video_id,j.title,j.kind,j.language].join(" ").toLowerCase().includes(q));
    $("jobsRows").innerHTML = jobs.length ? jobs.map((job,i) => {
      const state = String(job.status || "processing").toLowerCase();
      const pdf = job.pdf_generated ? "<span class=\"pdf-yes\">▤</span>" : "<span class=\"muted\">—</span>";
      return `<tr><td>${i+1}</td><td title="${esc(job.email)}">${esc(job.email)}</td><td>${job.video_url ? `<a class="video-link" target="_blank" rel="noreferrer" href="${esc(job.video_url)}">youtube ↗</a>` : "—"}</td><td title="${esc(job.title || job.video_id || job.kind)}">${esc(job.title || job.video_id || job.kind)}</td><td><span class="status ${esc(state)}">${esc(state)}</span></td><td>${pdf}</td><td>${esc(language(job.language))}</td><td title="Started ${esc(job.created_at)}">${elapsed(job.duration_ms)}</td><td>${job.output_tokens == null ? "—" : number(job.output_tokens)}</td></tr>`;
    }).join("") : `<tr><td colspan="9" class="muted">No matching recorded jobs. Run a new summary, full notes or PDF to populate this table.</td></tr>`;
    addJobLocationCells("jobsRows", jobs);
    $("usersRows").innerHTML = d.top_users.length ? d.top_users.map((user,i) => `<tr><td>${i+1}</td><td>${esc(user.email)}</td><td>${number(user.videos)}</td><td>${number(user.pdfs)}</td><td>${number(user.translations)}</td></tr>`).join("") : `<tr><td colspan="5" class="muted">No activity yet.</td></tr>`;
    $("videosRows").innerHTML = d.top_videos.length ? d.top_videos.map((video,i) => `<tr><td>${i+1}</td><td title="${esc(video.title || video.video_id)}">${esc(video.title || video.video_id)}</td><td>${number(video.runs)}</td><td class="good">${Number(video.success_rate).toFixed(1)}%</td></tr>`).join("") : `<tr><td colspan="4" class="muted">No video jobs yet.</td></tr>`;
    $("errorsRows").innerHTML = d.recent_errors.length ? d.recent_errors.map(error => `<tr><td>${ago(error.created_at)}</td><td>${esc(error.email)}</td><td title="${esc(error.message)}">${esc(error.message)}</td></tr>`).join("") : `<tr><td colspan="3" class="muted">No failed jobs recorded.</td></tr>`;
  }
  function render(d) {
    data = d; const m = d.metrics, labels = d.charts.labels;
    $("totalUsers").textContent = number(m.total_users); $("activeToday").textContent = number(m.active_today); $("videoJobs").textContent = number(m.video_jobs); $("summaryOutputs").textContent = number(m.summary_outputs); $("pdfGenerated").textContent = number(m.pdf_requests); $("translations").textContent = number(m.translations); $("failedJobs").textContent = number(m.failed_jobs); $("subscriptions").textContent = number(m.active_subscriptions) + " active subscriptions";
    $("paidUsers").textContent = number(m.paid_users); $("activeProUsers").textContent = number(m.active_pro_users); $("paymentIssueUsers").textContent = number(m.payment_issue_users); $("cancelledUsers").textContent = number(m.cancelled_users);
    const word = `Last ${d.range_days} Days`; $("activityRange").textContent = word; $("rangeJobs").textContent = "In selected period"; $("updated").textContent = "Live · updated " + new Date(d.generated_at).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
    lineChart($("activityChart"), labels, d.charts.user_activity); barChart($("jobsChart"), labels, d.charts.jobs); countryChart(d.charts.countries, d.charts.country_total == null ? m.total_users : d.charts.country_total); renderHealth(d.health); renderHealthDetails(); renderTables(d);
  }
  function userStatus(user) {
    if (!user.is_active) return "Disabled";
    const state = String((user.subscription || {}).status || "").toLowerCase();
    if (["active", "trialing"].includes(state)) return "Subscribed";
    if (["past_due", "unpaid", "incomplete"].includes(state)) return "Payment issue";
    if (state === "canceled") return "Cancelled";
    return "Free trial";
  }
  function renderUsers(payload) {
    const users = payload.users || [];
    $("usersCount").textContent = number(payload.total) + " user" + (payload.total === 1 ? "" : "s");
    $("directoryRows").innerHTML = users.length ? users.map(user => {
      const counts = user.counts || {};
      const trialLimit = Number(user.trial_limit || 0);
      const status = userStatus(user).toLowerCase().replace(" ", "-");
      return `<tr><td><b>${esc(user.full_name || user.email)}</b><br><small>${esc(user.email)}</small></td><td>${esc(dateTime(user.signup_at))}</td><td>${esc(dateTime(user.last_login_at))}</td><td>${esc(countryLabel(user.country))}</td><td>${esc(planText(user.subscription))}</td><td>${number(user.trials_used)} / ${number(trialLimit)}</td><td>${number(counts.videos)}</td><td>${number(counts.pdfs)}</td><td>${number(counts.translations)}</td><td>${number(counts.active_devices)} / ${number(counts.devices)}</td><td><span class="status ${esc(status)}">${esc(userStatus(user))}</span></td><td><button class="view-user" type="button" data-user-id="${esc(user.id)}">View</button></td></tr>`;
    }).join("") : tableEmpty(12, "No users match these filters.");
  }
  async function loadUsers() {
    if (usersLoading) return;
    usersLoading = true;
    const button = $("usersRefresh");
    button.disabled = true;
    try {
      const search = encodeURIComponent($("userSearch").value.trim());
      const status = encodeURIComponent($("userStatus").value);
      renderUsers(await api(`/users/overview?limit=100&query=${search}&status=${status}`));
    } catch (e) {
      $("directoryRows").innerHTML = tableEmpty(12, e.status === 403 ? "Admin access required." : "Could not load users. Please refresh.");
      $("usersCount").textContent = "Unavailable";
    } finally { button.disabled = false; usersLoading = false; }
  }
  function showUsers(filter = "all") {
    $("dashboard").classList.add("hidden");
    $("systemHealth").classList.add("hidden");
    $("users").classList.remove("hidden");
    $("operations").classList.add("hidden");
    $("billing").classList.add("hidden");
    $("errorLogs").classList.add("hidden");
    $("adminSettings").classList.add("hidden");
    $("userStatus").value = filter;
    document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.section === "users"));
    loadUsers();
    window.scrollTo({top: 0, behavior: "smooth"});
  }
  function billingMoney(item) {
    if (typeof item.amount_subunits !== "number" || !item.currency) return "Amount pending";
    try { return new Intl.NumberFormat(undefined, {style: "currency", currency: item.currency}).format(item.amount_subunits / 100); }
    catch (_) { return String(item.currency).toUpperCase() + " " + (item.amount_subunits / 100).toFixed(2); }
  }
  function billingTotalLabel(totals) {
    const entries = Object.entries(totals || {});
    if (!entries.length) return "No captured payments";
    return entries.map(([currency, amount]) => billingMoney({currency, amount_subunits: amount})).join(" · ");
  }
  function renderBillingRows(rows) {
    $("billingRows").innerHTML = rows.length ? rows.map(payment => {
      const status = String(payment.status || "pending").toLowerCase();
      const invoice = payment.invoice_url
        ? `<a class="video-link" target="_blank" rel="noopener noreferrer" href="${esc(payment.invoice_url)}">View invoice</a>`
        : `<span class="muted">Invoice pending</span>`;
      const paymentId = payment.payment_id || "—";
      return `<tr><td>${esc(payment.email)}</td><td>${esc(billingMoney(payment))}</td><td title="${esc(payment.failure_message || "")}"><span class="status ${esc(payment.state || status)}">${esc(status)}</span></td><td>${esc(payment.provider)}</td><td><code>${esc(paymentId)}</code></td><td>${invoice}</td><td>${esc(dateTime(payment.paid_at || payment.created_at))}</td></tr>`;
    }).join("") : tableEmpty(7, "No payment records match these filters.");
  }
  function renderBillingHistory(payload) {
    billingData = payload;
    $("billingSuccessful").textContent = number(payload.successful_payments);
    $("billingFailed").textContent = number(payload.failed_payments);
    $("billingPending").textContent = number(payload.pending_payments);
    $("billingTotal").textContent = number(payload.total_payments);
    $("billingCollected").textContent = billingTotalLabel(payload.paid_by_currency);
    $("billingRange").textContent = "Last " + payload.range_days + " days";
    $("billingCount").textContent = number(payload.total_payments) + " total · latest " + number((payload.payments || []).length) + " shown";
    renderBillingRows(payload.payments || []);
  }
  async function loadBillingHistory() {
    if (billingLoading) return;
    billingLoading = true;
    const button = $("billingRefresh");
    button.disabled = true;
    try {
      const query = encodeURIComponent($("billingSearch").value.trim());
      const status = encodeURIComponent($("billingStatus").value);
      renderBillingHistory(await api("/billing-history?days=" + encodeURIComponent($("range").value) + "&limit=500&query=" + query + "&status=" + status));
    } catch (e) {
      $("billingRows").innerHTML = tableEmpty(7, e.status === 403 ? "Admin access required." : "Could not load billing history. Please refresh.");
      $("billingCount").textContent = "Unavailable";
    } finally { button.disabled = false; billingLoading = false; }
  }
  function showBillingHistory() {
    closeUserDrawer();
    $("dashboard").classList.add("hidden");
    $("systemHealth").classList.add("hidden");
    $("users").classList.add("hidden");
    $("operations").classList.add("hidden");
    $("billing").classList.remove("hidden");
    $("errorLogs").classList.add("hidden");
    $("adminSettings").classList.add("hidden");
    document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.section === "billing"));
    loadBillingHistory();
    window.scrollTo({top: 0, behavior: "smooth"});
  }
  function operationCopy(category) {
    return {
      recent: { heading: "Recent Video Jobs", lead: "All recorded summary, Full Notes, PDF and translation operations in the selected period.", userLabel: "Users with activity", totalLabel: "Recorded operations", table: "Recent Video Jobs" },
      jobs: { heading: "Summary Generation", lead: "Summary and key-points jobs created in the selected period.", userLabel: "Created summaries", totalLabel: "Summary jobs", table: "Summary Generation" },
      successful_summaries: { heading: "Successful Summaries", lead: "Completed summary and key-points jobs in the selected period.", userLabel: "Users with successful summaries", totalLabel: "Successful summaries", table: "Successful Summaries" },
      pdf: { heading: "PDF Generation", lead: "PDF-ready operations created in the selected period.", userLabel: "Created PDFs", totalLabel: "PDF operations", table: "PDF Generation" },
      translations: { heading: "Translations", lead: "Translation jobs created in the selected period.", userLabel: "Created translations", totalLabel: "Translation jobs", table: "Translations" },
      completed_translations: { heading: "Completed Translations", lead: "Completed translation jobs in the selected period.", userLabel: "Users with translations", totalLabel: "Completed translations", table: "Completed Translations" },
    }[category];
  }
  function renderOperationRows(rows) {
    const query = $("operationSearch").value.trim().toLowerCase();
    const filtered = rows.filter(job => !query || [job.email, job.city, job.country, job.video_url, job.title, job.kind, job.language, job.status].join(" ").toLowerCase().includes(query));
    $("operationsRows").innerHTML = filtered.length ? filtered.map(job => {
      const state = String(job.status || "processing").toLowerCase();
      const video = job.video_url ? `<a class="video-link" target="_blank" rel="noreferrer" href="${esc(job.video_url)}">youtube ↗</a>` : "—";
      return `<tr><td>${esc(job.email)}</td><td>${video}</td><td title="${esc(job.title || job.video_id || job.kind)}">${esc(job.title || job.video_id || job.kind)}</td><td>${esc(job.kind)}</td><td><span class="status ${esc(state)}">${esc(state)}</span></td><td>${job.pdf_generated ? "Yes" : "—"}</td><td>${esc(language(job.language))}</td><td>${esc(elapsed(job.duration_ms))}</td><td>${job.output_tokens == null ? "—" : number(job.output_tokens)}</td><td>${esc(dateTime(job.started_at))}</td></tr>`;
    }).join("") : tableEmpty(10, "No matching operations in the selected period.");
    addJobLocationCells("operationsRows", filtered);
  }
  function renderOperations(payload) {
    operationData = payload;
    const copy = operationCopy(payload.category);
    $("operationsHeading").textContent = copy.heading;
    $("operationsLead").textContent = copy.lead;
    $("operationsTableTitle").textContent = copy.table;
    $("operationUsersLabel").textContent = copy.userLabel;
    $("operationTotalLabel").textContent = copy.totalLabel + " in last " + payload.range_days + " days";
    $("operationUsers").textContent = number(payload.unique_users);
    $("operationTotal").textContent = number(payload.total_jobs);
    $("operationSuccess").textContent = number(payload.success);
    $("operationState").textContent = number(payload.failed) + " / " + number(payload.processing);
    $("operationStateLabel").textContent = "Failed / processing";
    $("operationsCount").textContent = number(payload.total_jobs) + " total · latest " + number((payload.operations || []).length) + " shown";
    renderOperationRows(payload.operations || []);
  }
  async function loadOperations() {
    if (operationsLoading) return;
    operationsLoading = true;
    const button = $("operationsRefresh");
    button.disabled = true;
    try {
      renderOperations(await api("/operations?category=" + encodeURIComponent(operationCategory) + "&days=" + encodeURIComponent($("range").value) + "&limit=500"));
    } catch (e) {
      $("operationsRows").innerHTML = tableEmpty(10, e.status === 403 ? "Admin access required." : "Could not load operations. Please refresh.");
      $("operationsCount").textContent = "Unavailable";
    } finally { button.disabled = false; operationsLoading = false; }
  }
  function showOperations(category) {
    operationCategory = category;
    closeUserDrawer();
    $("dashboard").classList.add("hidden");
    $("systemHealth").classList.add("hidden");
    $("users").classList.add("hidden");
    $("operations").classList.remove("hidden");
    $("billing").classList.add("hidden");
    $("errorLogs").classList.add("hidden");
    $("adminSettings").classList.add("hidden");
    document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.section === category));
    loadOperations();
    window.scrollTo({top: 0, behavior: "smooth"});
  }
  function renderErrorRows(rows) {
    const query = $("errorSearch").value.trim().toLowerCase();
    const filtered = rows.filter(job => !query || [job.email, job.video_url, job.title, job.kind, job.language, job.error_message].join(" ").toLowerCase().includes(query));
    $("errorLogRows").innerHTML = filtered.length ? filtered.map(job => {
      const video = job.video_url ? `<a class="video-link" target="_blank" rel="noreferrer" href="${esc(job.video_url)}">youtube ↗</a>` : "—";
      return `<tr><td>${esc(job.email)}</td><td>${video}</td><td title="${esc(job.title || job.video_id || job.kind)}">${esc(job.title || job.video_id || job.kind)}</td><td>${esc(job.kind)}</td><td>${esc(language(job.language))}</td><td title="${esc(job.error_message || "Processing failed")}">${esc(job.error_message || "Processing failed")}</td><td>${esc(dateTime(job.started_at))}</td></tr>`;
    }).join("") : tableEmpty(7, "No matching failed jobs in the selected period.");
  }
  function renderErrorLogs(payload) {
    errorData = payload;
    $("errorTotal").textContent = number(payload.total_errors);
    $("errorUsers").textContent = number(payload.unique_users);
    $("errorCount").textContent = number(payload.total_errors) + " total · latest " + number((payload.errors || []).length) + " shown";
    renderErrorRows(payload.errors || []);
  }
  async function loadErrorLogs() {
    if (errorsLoading) return;
    errorsLoading = true;
    const button = $("errorsRefresh");
    button.disabled = true;
    try { renderErrorLogs(await api("/errors?days=" + encodeURIComponent($("range").value) + "&limit=500")); }
    catch (e) { $("errorLogRows").innerHTML = tableEmpty(7, e.status === 403 ? "Admin access required." : "Could not load error logs. Please refresh."); $("errorCount").textContent = "Unavailable"; }
    finally { button.disabled = false; errorsLoading = false; }
  }
  function showErrorLogs() {
    closeUserDrawer();
    $("dashboard").classList.add("hidden");
    $("systemHealth").classList.add("hidden");
    $("users").classList.add("hidden");
    $("operations").classList.add("hidden");
    $("billing").classList.add("hidden");
    $("errorLogs").classList.remove("hidden");
    $("adminSettings").classList.add("hidden");
    document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.section === "errors"));
    loadErrorLogs();
    window.scrollTo({top: 0, behavior: "smooth"});
  }
  function configLine(label, value, kind = "") { return `<div class="config-line"><span>${esc(label)}</span><b class="${esc(kind)}">${esc(value)}</b></div>`; }
  function yesNo(value) { return value ? "Configured" : "Not configured"; }
  function renderSettings(payload) {
    settingsData = payload;
    const trials = payload.trials || {}, billing = payload.billing || {}, model = payload.model || {}, maintenance = payload.maintenance || {};
    $("trialConfig").innerHTML = [
      configLine("Default account trials", trials.default_limit),
      configLine("Per-device protection", trials.device_limit_enabled ? "Enabled" : "Disabled", trials.device_limit_enabled ? "good" : "warn"),
      configLine("Machine protection", trials.machine_limit_enabled ? "Enabled · " + trials.machine_limit + " limit" : "Disabled", trials.machine_limit_enabled ? "good" : "warn"),
      configLine("Devices (free / paid)", trials.max_free_devices + " / " + trials.max_paid_devices),
    ].join("");
    $("grantTrialsLimit").value = trials.default_limit == null ? 5 : trials.default_limit;
    $("billingConfig").innerHTML = [
      configLine("Provider", billing.provider || "Not configured"),
      configLine("Checkout", yesNo(billing.checkout_configured), billing.checkout_configured ? "good" : "warn"),
      configLine("India plan", yesNo(billing.india_plan_configured), billing.india_plan_configured ? "good" : "warn"),
      configLine("International plan", yesNo(billing.international_plan_configured), billing.international_plan_configured ? "good" : "warn"),
      configLine("Webhook secret", yesNo(billing.webhook_configured), billing.webhook_configured ? "good" : "warn"),
      configLine("Latest webhook", billing.last_webhook_at ? (billing.last_webhook_type || "Event") + " · " + dateTime(billing.last_webhook_at) : "No event recorded"),
    ].join("");
    const vllm = model.vllm || {}, gpu = model.gpu || {};
    $("modelConfig").innerHTML = [
      configLine("Model", model.model || "Not configured"),
      configLine("Max concurrency", model.max_concurrency),
      configLine("Queue timeout", model.queue_timeout_seconds + " seconds"),
      configLine("Context window", number(model.context_tokens) + " tokens"),
      configLine("vLLM queue", vllm.running == null ? "Metrics unavailable" : (vllm.running || 0) + " running · " + (vllm.waiting || 0) + " waiting", vllm.running == null ? "warn" : "good"),
      configLine("GPU", gpu.available ? gpu.name + " · " + gpu.utilization + "% · " + gpu.temperature + "°C" : "nvidia-smi unavailable", gpu.available ? "good" : "warn"),
    ].join("");
    $("cacheConfig").innerHTML = [
      configLine("Output cache", maintenance.output_cache_enabled ? "Enabled" : "Disabled", maintenance.output_cache_enabled ? "good" : "warn"),
      configLine("Stored cache entries", number(maintenance.cache_entries)),
      configLine("Automatic expiry", maintenance.output_cache_ttl_days ? maintenance.output_cache_ttl_days + " days" : "Never"),
    ].join("");
    $("purgeDays").value = maintenance.output_cache_ttl_days == null ? 90 : maintenance.output_cache_ttl_days;
    $("maintenanceConfig").innerHTML = [
      configLine("Failed jobs (last 30 days)", number(maintenance.failed_jobs_last_30_days), maintenance.failed_jobs_last_30_days ? "warn" : "good"),
      configLine("Error-log action", "View or export only"),
    ].join("");
  }
  async function loadSettings() {
    if (settingsLoading) return;
    settingsLoading = true;
    const button = $("settingsRefresh");
    button.disabled = true;
    try { renderSettings(await api("/settings/status")); }
    catch (e) { ["trialConfig", "billingConfig", "modelConfig", "cacheConfig", "maintenanceConfig"].forEach(id => $(id).textContent = e.status === 403 ? "Admin access required." : "Could not load current status."); }
    finally { button.disabled = false; settingsLoading = false; }
  }
  function showSettings() {
    closeUserDrawer();
    $("dashboard").classList.add("hidden");
    $("systemHealth").classList.add("hidden");
    $("users").classList.add("hidden");
    $("operations").classList.add("hidden");
    $("billing").classList.add("hidden");
    $("errorLogs").classList.add("hidden");
    $("adminSettings").classList.remove("hidden");
    document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.section === "settings"));
    loadSettings();
    window.scrollTo({top: 0, behavior: "smooth"});
  }
  function showSystemHealth() {
    closeUserDrawer();
    $("dashboard").classList.add("hidden");
    $("users").classList.add("hidden");
    $("operations").classList.add("hidden");
    $("billing").classList.add("hidden");
    $("errorLogs").classList.add("hidden");
    $("adminSettings").classList.add("hidden");
    $("systemHealth").classList.remove("hidden");
    document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.section === "health"));
    load();
    window.scrollTo({top: 0, behavior: "smooth"});
  }
  function setActionResult(id, message, kind = "") { const target = $(id); target.textContent = message; target.className = "action-result " + kind; }
  function csvCell(value) { const text = String(value == null ? "" : value); const safe = /^[=+\-@]/.test(text) ? "'" + text : text; return '"' + safe.replaceAll('"', '""') + '"'; }
  async function downloadErrorsCsv() {
    try {
      const payload = await api("/errors?days=" + encodeURIComponent($("range").value) + "&limit=1000");
      const lines = [["User email", "Video URL", "Title", "Type", "Language", "Error message", "Started"]];
      (payload.errors || []).forEach(job => lines.push([job.email, job.video_url, job.title || job.video_id, job.kind, language(job.language), job.error_message || "Processing failed", job.started_at]));
      const blob = new Blob([lines.map(row => row.map(csvCell).join(",")).join("\r\n")], {type: "text/csv;charset=utf-8"});
      const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = "tubenotes-error-logs.csv"; link.click(); URL.revokeObjectURL(link.href);
      setActionResult("maintenanceActionResult", number(payload.total_errors) + " error logs exported.", "success");
    } catch (_) { setActionResult("maintenanceActionResult", "Could not export error logs. Please retry.", "failed"); }
  }
  function closeUserDrawer() { $("userDrawer").classList.add("hidden"); }
  function renderSubscriptionRows(subscriptions) {
    return subscriptions.length ? `<table><thead><tr><th>Provider</th><th>Status</th><th>Price</th><th>Renewal</th><th>Cancellation</th></tr></thead><tbody>${subscriptions.map(sub => `<tr><td>${esc(sub.provider)}</td><td><span class="status ${esc(String(sub.status || "").toLowerCase())}">${esc(sub.status)}</span></td><td>${esc(planText(sub).replace("/month · " + sub.status, ""))}</td><td>${esc(dateTime(sub.current_period_end))}</td><td>${sub.cancel_at_period_end ? "At period end" : "No"}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">No subscription has been created for this user.</p>`;
  }
  function renderDeviceRows(devices) {
    return devices.length ? `<table><thead><tr><th>Device</th><th>Platform</th><th>Extension</th><th>Added</th><th>Last seen</th><th>Status</th></tr></thead><tbody>${devices.map(item => `<tr><td>${esc(item.label)}</td><td>${esc(item.platform)}</td><td>${esc(item.extension_version || "—")}</td><td>${esc(dateTime(item.created_at))}</td><td>${esc(dateTime(item.last_seen_at))}</td><td><span class="status ${item.revoked ? "failed" : "success"}">${item.revoked ? "Revoked" : "Active"}</span></td></tr>`).join("")}</tbody></table>` : `<p class="muted">No registered devices.</p>`;
  }
  function renderJobRows(jobs, errorsOnly = false) {
    const rows = errorsOnly ? jobs.filter(item => item.status === "failed") : jobs;
    return rows.length ? `<table><thead><tr><th>Started</th><th>Video / title</th><th>Type</th><th>Language</th><th>Status</th><th>PDF</th><th>Time</th><th>Tokens*</th><th>Error</th></tr></thead><tbody>${rows.map(job => `<tr><td>${esc(dateTime(job.started_at))}</td><td>${job.video_url ? `<a class="video-link" target="_blank" rel="noreferrer" href="${esc(job.video_url)}">${esc(job.title || job.video_id || "YouTube video")}</a>` : esc(job.title || job.video_id || "—")}</td><td>${esc(job.kind)}</td><td>${esc(language(job.language))}</td><td><span class="status ${esc(String(job.status || "").toLowerCase())}">${esc(job.status)}</span></td><td>${job.pdf_generated ? "Yes" : "—"}</td><td>${esc(elapsed(job.duration_ms))}</td><td>${job.output_tokens == null ? "—" : number(job.output_tokens)}</td><td title="${esc(job.error_message || "")}">${esc(job.error_message || "—")}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">${errorsOnly ? "No recorded errors for this user." : "No recorded jobs for this user yet."}</p>`;
  }
  function renderUserDetail(payload) {
    const user = payload.user, counts = user.counts || {};
    $("drawerTitle").textContent = user.full_name || user.email;
    $("userProfile").innerHTML = `<div><h3>${esc(user.full_name || "No name supplied")}</h3><p>${esc(user.email)}</p><p class="muted">Signed up ${esc(dateTime(user.signup_at))} · Last login ${esc(dateTime(user.last_login_at))}</p></div><div class="profile-grid"><span><b>${esc(countryLabel(user.country))}</b>Country</span><span><b>${number(user.trials_used)} / ${number(user.trial_limit)}</b>Free trials used</span><span><b>${number(counts.videos)}</b>Videos</span><span><b>${number(counts.pdfs)}</b>PDFs</span><span><b>${number(counts.translations)}</b>Translations</span><span><b>${number(counts.active_devices)} / ${number(counts.devices)}</b>Active devices</span></div><p class="profile-plan"><b>${esc(planText(user.subscription))}</b> · ${user.is_active ? "Account active" : "Account disabled"} · ${user.email_verified ? "Email verified" : "Email not verified"}</p>`;
    $("userSubscriptions").innerHTML = renderSubscriptionRows(payload.subscriptions || []);
    $("userDevices").innerHTML = renderDeviceRows(payload.devices || []);
    $("userJobs").innerHTML = renderJobRows(payload.jobs || []);
    $("userErrors").innerHTML = renderJobRows(payload.errors || [], true);
  }
  async function openUserDetail(userId) {
    $("userDrawer").classList.remove("hidden");
    $("drawerTitle").textContent = "Loading user...";
    $("userProfile").innerHTML = `<p class="muted">Loading account details...</p>`;
    ["userSubscriptions", "userDevices", "userJobs", "userErrors"].forEach(id => $(id).innerHTML = "");
    try { renderUserDetail(await api("/users/" + encodeURIComponent(userId) + "/detail")); }
    catch (_) { $("userProfile").innerHTML = `<p class="muted">Could not load this user. Close the panel and try again.</p>`; }
  }
  async function load() {
    if (dashboardLoading) return;
    dashboardLoading = true;
    $("refresh").disabled = true;
    try { render(await api("/dashboard?days=" + encodeURIComponent($("range").value))); $("blocked").classList.add("hidden"); }
    catch (e) { $("blockedText").textContent = e.status === 403 ? "This signed-in account is not configured as an administrator." : "Sign in with the administrator account first."; $("blocked").classList.remove("hidden"); }
    finally { $("refresh").disabled = false; dashboardLoading = false; }
  }
  async function autoUpdate() {
    if (document.visibilityState !== "visible" || autoUpdateInFlight) return;
    autoUpdateInFlight = true;
    try {
      if (!$("dashboard").classList.contains("hidden")) await load();
      else if (!$("users").classList.contains("hidden")) await loadUsers();
      else if (!$("operations").classList.contains("hidden")) await loadOperations();
      else if (!$("billing").classList.contains("hidden")) await loadBillingHistory();
      else if (!$("errorLogs").classList.contains("hidden")) await loadErrorLogs();
      else if (!$("systemHealth").classList.contains("hidden")) await load();
      else if (!$("adminSettings").classList.contains("hidden") && Date.now() - lastSettingsAutoUpdate >= 15000) {
        lastSettingsAutoUpdate = Date.now();
        await loadSettings();
      }
    } finally { autoUpdateInFlight = false; }
  }
  async function init() {
    try { const session = await api("/session"); $("adminName").textContent = session.name; $("welcomeName").textContent = session.name; $("adminEmail").textContent = session.email; $("adminInitial").textContent = (session.name || "A").trim().charAt(0).toUpperCase(); } catch (_) {}
    load();
  }
  installThemeToggle();
  $("refresh").onclick = () => window.location.reload();
  $("adminSignoutBtn").onclick = signOutAdmin;
  $("range").classList.add("range-control");
  if (!$("range").querySelector('option[value="15"]')) $("range").add(new Option("Last 15 days", "15"));
  function setDashboardRange(value) {
    document.querySelectorAll(".range-control").forEach(control => { control.value = String(value); });
    load();
    if (!$("operations").classList.contains("hidden")) loadOperations();
    if (!$("billing").classList.contains("hidden")) loadBillingHistory();
    if (!$("errorLogs").classList.contains("hidden")) loadErrorLogs();
  }
  document.querySelectorAll(".range-control").forEach(control => control.onchange = () => setDashboardRange(control.value));
  $("search").oninput = () => { if (data) renderTables(data); };
  let userSearchTimer = null;
  $("usersRefresh").onclick = loadUsers;
  $("userSearch").oninput = () => { clearTimeout(userSearchTimer); userSearchTimer = setTimeout(loadUsers, 300); };
  $("userStatus").onchange = loadUsers;
  document.querySelectorAll(".metric-link").forEach(card => card.onclick = () => {
    const action = card.dataset.dashboardAction;
    if (action === "users") showUsers("all");
    else if (action === "active_today") showUsers("active_today");
    else if (action === "failed") showErrorLogs();
    else showOperations(action);
  });
  document.querySelectorAll(".payment-filter-card").forEach(card => card.onclick = () => showUsers(card.dataset.userFilter));
  $("operationsRefresh").onclick = loadOperations;
  $("operationSearch").oninput = () => { if (operationData) renderOperationRows(operationData.operations || []); };
  let billingSearchTimer = null;
  $("billingRefresh").onclick = loadBillingHistory;
  $("billingSearch").oninput = () => { clearTimeout(billingSearchTimer); billingSearchTimer = setTimeout(loadBillingHistory, 300); };
  $("billingStatus").onchange = loadBillingHistory;
  $("errorsRefresh").onclick = loadErrorLogs;
  $("errorSearch").oninput = () => { if (errorData) renderErrorRows(errorData.errors || []); };
  $("settingsRefresh").onclick = loadSettings;
  $("healthRefresh").onclick = load;
  $("settingsOpenUsers").onclick = () => showUsers("all");
  $("settingsOpenErrors").onclick = showErrorLogs;
  $("exportErrors").onclick = downloadErrorsCsv;
  $("grantTrialsForm").onsubmit = async event => {
    event.preventDefault();
    const email = $("grantTrialsEmail").value.trim();
    const trialLimit = Number($("grantTrialsLimit").value);
    if (!email || !Number.isInteger(trialLimit) || trialLimit < 0 || trialLimit > 10000) { setActionResult("trialActionResult", "Enter a valid email and a whole-number trial limit (0–10,000).", "failed"); return; }
    if (!window.confirm(`Set ${email}'s total free-trial allowance to ${trialLimit}?`)) return;
    try {
      const user = await api("/grant-trials", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({email, trial_limit: trialLimit})});
      setActionResult("trialActionResult", "Trial allowance updated for " + user.email + ".", "success");
    } catch (_) { setActionResult("trialActionResult", "Could not update trials. Check the email and retry.", "failed"); }
  };
  $("purgeCache").onclick = async () => {
    const days = Number($("purgeDays").value);
    if (!Number.isInteger(days) || days < 0 || days > 3650) { setActionResult("cacheActionResult", "Enter a whole number from 0 to 3650 days.", "failed"); return; }
    if (!window.confirm(`Permanently remove cached outputs older than ${days} days? This cannot be undone.`)) return;
    try {
      const result = await api("/cache/purge?older_than_days=" + encodeURIComponent(days), {method: "POST"});
      setActionResult("cacheActionResult", number(result.removed) + " cache entries removed.", "success"); loadSettings();
    } catch (_) { setActionResult("cacheActionResult", "Could not purge the cache. Please retry.", "failed"); }
  };
  document.querySelectorAll(".view-all-link").forEach(button => button.onclick = () => {
    if (button.dataset.viewAll === "recent") showOperations("recent");
    else if (button.dataset.viewAll === "users") showUsers("all");
    else if (button.dataset.viewAll === "jobs") showOperations("jobs");
    else if (button.dataset.viewAll === "errors") showErrorLogs();
    else if (button.dataset.viewAll === "health") showSystemHealth();
  });
  $("directoryRows").onclick = event => { const button = event.target.closest(".view-user"); if (button) openUserDetail(button.dataset.userId); };
  $("closeUserDrawer").onclick = closeUserDrawer;
  $("userDrawer").onclick = event => { if (event.target === $("userDrawer")) closeUserDrawer(); };
  document.addEventListener("keydown", event => { if (event.key === "Escape") closeUserDrawer(); });
  document.querySelectorAll(".nav-item").forEach(button => button.onclick = () => {
    document.querySelectorAll(".nav-item").forEach(b => b.classList.remove("active"));
    button.classList.add("active");
    const requested = button.dataset.section;
    const usersPage = $("users");
    const dashboardPage = $("dashboard");
    if (requested === "users") {
      showUsers("all"); return;
    }
    if (requested === "billing") {
      showBillingHistory(); return;
    }
    if (["recent", "jobs", "pdf", "translations"].includes(requested)) {
      showOperations(requested); return;
    }
    if (requested === "errors") {
      showErrorLogs(); return;
    }
    if (requested === "settings") {
      showSettings(); return;
    }
    if (requested === "health") {
      showSystemHealth(); return;
    }
    usersPage.classList.add("hidden"); dashboardPage.classList.remove("hidden");
    $("systemHealth").classList.add("hidden");
    $("operations").classList.add("hidden");
    $("billing").classList.add("hidden");
    $("errorLogs").classList.add("hidden");
    $("adminSettings").classList.add("hidden");
    const section = $(requested); if (section) section.scrollIntoView({behavior:"smooth",block:"start"});
  });
  init();
  setInterval(autoUpdate, 5000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") autoUpdate(); });
})();
