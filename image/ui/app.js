// --- Element refs -------------------------------------------------
const adminBanner     = document.getElementById("adminBanner");
const adminBannerLead = document.getElementById("adminBannerLead");
const adminBannerText = document.getElementById("adminBannerText");
const destructiveTag = document.getElementById("destructiveTag");
const authBox        = document.getElementById("authBox");
const authState      = document.getElementById("authState");
const signInBtn      = document.getElementById("signInBtn");
const signOutBtn     = document.getElementById("signOutBtn");
const signInCard     = document.getElementById("signInCard");
const signInPassword = document.getElementById("signInPassword");
const signInSubmit   = document.getElementById("signInSubmit");
const signInCancel   = document.getElementById("signInCancel");
const signInError    = document.getElementById("signInError");
const stagedTag      = document.getElementById("stagedTag");
const infoCard       = document.getElementById("infoCard");
const inviteCodeEl   = document.getElementById("inviteCode");
const copyInviteBtn  = document.getElementById("copyInviteBtn");
const passwordNoteEl = document.getElementById("passwordNote");
const refreshBtn     = document.getElementById("refreshBtn");
const downloadSavesBtn = document.getElementById("downloadSavesBtn");
const playersCard    = document.getElementById("playersCard");
const playersTable   = document.getElementById("playersTable");
const playersBody    = document.getElementById("playersBody");
const playersEmpty   = document.getElementById("playersEmpty");
const worldsTable    = document.getElementById("worldsTable");
const worldsBody     = document.getElementById("worldsBody");
const worldsEmpty    = document.getElementById("worldsEmpty");
const worldUploadBtn = document.getElementById("worldUploadBtn");
const worldUploadFile= document.getElementById("worldUploadFile");
const fServerName    = document.getElementById("fServerName");
const fMaxPlayers    = document.getElementById("fMaxPlayers");
const fIsProtected   = document.getElementById("fIsProtected");
const fPassword      = document.getElementById("fPassword");
const fWorldId       = document.getElementById("fWorldId");
const fP2pAddr       = document.getElementById("fP2pAddr");
const fPSID          = document.getElementById("fPSID");
const fInvite        = document.getElementById("fInvite");
const configEditor        = document.getElementById("configEditor");
const configEditorErrors  = document.getElementById("configEditorErrors");
const configDiffBox       = document.getElementById("configDiffBox");
const configDiff          = document.getElementById("configDiff");
const configSaveBtn       = document.getElementById("configSaveBtn");
const configApplyBtn      = document.getElementById("configApplyBtn");
const configRevertBtn     = document.getElementById("configRevertBtn");
const stopServerBtn       = document.getElementById("stopServerBtn");
const worldEditorCard     = document.getElementById("worldEditorCard");
const worldEditorId       = document.getElementById("worldEditorId");
const worldEditorJson     = document.getElementById("worldEditorJson");
const worldSaveBtn        = document.getElementById("worldSaveBtn");
const worldCloseBtn       = document.getElementById("worldCloseBtn");
const fwName              = document.getElementById("fwName");
const fwPreset            = document.getElementById("fwPreset");
const fwMobHealth         = document.getElementById("fwMobHealth");
const fwMobDamage         = document.getElementById("fwMobDamage");
const fwShipHealth        = document.getElementById("fwShipHealth");
const fwShipDamage        = document.getElementById("fwShipDamage");
const fwBoarding          = document.getElementById("fwBoarding");
const fwCoopQuests        = document.getElementById("fwCoopQuests");
const fwEasyExplore       = document.getElementById("fwEasyExplore");
const backupsBody    = document.getElementById("backupsBody");
const backupsTable   = document.getElementById("backupsTable");
const backupsEmpty   = document.getElementById("backupsEmpty");
const backupCreateBtn= document.getElementById("backupCreateBtn");
const updateCard     = document.getElementById("updateCard");
const archiveInput   = document.getElementById("archiveFile");
const uploadBtn      = document.getElementById("uploadBtn");
const importTitle    = document.getElementById("importTitle");
const importBlurb    = document.getElementById("importBlurb");
const restartHint    = document.getElementById("restartHint");
const statusEl       = document.getElementById("status");

const kv = {
  serverName: document.getElementById("kvServerName"),
  files: document.getElementById("kvFiles"),
  server: document.getElementById("kvServer"),
  uptime: document.getElementById("kvUptime"),
  backendRegion: document.getElementById("kvBackendRegion"),
  worldId: document.getElementById("kvWorldId"),
  saveVersion: document.getElementById("kvSaveVersion"),
  worldCount: document.getElementById("kvWorldCount"),
  players: document.getElementById("kvPlayers"),
  cpu: document.getElementById("kvCpu"),
  mem: document.getElementById("kvMem"),
};

// --- State --------------------------------------------------------
let lastStatus = null;
let lastConfig = null;
let selectedWorldSlot = null;
let editingWorldId = null;
let editingWorldDoc = null;
let suppressSync = false;   // avoid infinite sync loops between form and raw JSON
let rawDebounceTimer = null;

// --- Auth session -------------------------------------------------
// Auth credentials persist in sessionStorage (per-tab). Cleared on
// sign-out or on 401 response. We never store the plaintext password;
// store the pre-built Basic header value instead.
const AUTH_KEY = "windrose.basicAuth";
function getAuthHeader() { return sessionStorage.getItem(AUTH_KEY) || ""; }
function setAuthHeader(v) { if (v) sessionStorage.setItem(AUTH_KEY, v); else sessionStorage.removeItem(AUTH_KEY); }
async function authFetch(input, init) {
  const hdrs = new Headers((init || {}).headers || {});
  const auth = getAuthHeader();
  if (auth && !hdrs.has("Authorization")) hdrs.set("Authorization", auth);
  const res = await fetch(input, { ...(init || {}), headers: hdrs });
  if (res.status === 401 && auth) {
    // Credentials went stale or were revoked — clear + re-render.
    setAuthHeader("");
    scheduleUiRefresh();
  }
  return res;
}
function scheduleUiRefresh() { setTimeout(loadStatus, 0); }

// --- Helpers ------------------------------------------------------
function log(msg) {
  statusEl.textContent = `[${new Date().toLocaleTimeString()}] ${msg}\n` + statusEl.textContent.slice(0, 2000);
}
function pillText(el, text, cls) { el.textContent = text; el.className = cls || ""; }
function formatUptime(s) {
  if (!s || s <= 0) return "-";
  const d=Math.floor(s/86400), h=Math.floor((s%86400)/3600), m=Math.floor((s%3600)/60), sec=s%60;
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m ${sec}s`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}
function formatBytes(b) {
  if (!b) return "-";
  const u=["B","KiB","MiB","GiB","TiB"]; let i=0, v=b;
  while (v>=1024 && i<u.length-1) { v/=1024; i++; }
  return `${v.toFixed(v>=100?0:1)} ${u[i]}`;
}
function sourceSuffix(src) {
  return src === "chart_value" ? "chart value" : src === "cgroup" ? "cgroup" : src === "host" ? "host available" : "";
}
function escapeHtml(s) { return (s||"").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c])); }
function revealable(full, shortStart = 6, shortEnd = 4) {
  const short = full.length > shortStart + shortEnd + 1 ? `${full.slice(0, shortStart)}…${full.slice(-shortEnd)}` : full;
  const safeFull = escapeHtml(full), safeShort = escapeHtml(short);
  return `<span class="reveal">
    <span class="mask-dots" data-full="${safeFull}">${safeShort}</span>
    <button type="button" class="link reveal-btn">show</button>
  </span>`;
}
function wireReveal(root) {
  root.querySelectorAll(".reveal").forEach(el => {
    const dots = el.querySelector(".mask-dots");
    const btn = el.querySelector(".reveal-btn");
    btn.addEventListener("click", () => {
      if (btn.textContent === "show") {
        dots.textContent = dots.dataset.full;
        dots.style.color = "inherit";
        btn.textContent = "hide";
      } else {
        const full = dots.dataset.full;
        const s = full.length > 10 ? `${full.slice(0,6)}…${full.slice(-4)}` : full;
        dots.textContent = s;
        dots.style.color = "#888";
        btn.textContent = "show";
      }
    });
  });
}
function renderBar(el, pct, label) {
  el.innerHTML = "";
  if (pct == null || isNaN(pct)) { el.textContent = "-"; return; }
  const bar = document.createElement("span"); bar.className = "pct-bar";
  const fill = document.createElement("span");
  fill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  if (pct > 90) bar.classList.add("err");
  else if (pct > 70) bar.classList.add("warn");
  bar.appendChild(fill);
  const lbl = document.createElement("span"); lbl.style.marginLeft = "0.5rem"; lbl.textContent = label;
  el.appendChild(bar); el.appendChild(lbl);
}

// --- Renderers ----------------------------------------------------
function renderCpu(data) {
  const usedMcpu = Math.round((data.cpuPercent || 0) * 10);
  const limitMcpu = data.cpuLimitMcpu || 0;
  if (limitMcpu <= 0) { kv.cpu.textContent = "-"; return; }
  const pct = (usedMcpu/limitMcpu) * 100;
  const limitLabel = limitMcpu >= 1000
    ? `${(limitMcpu/1000).toFixed(limitMcpu%1000===0?0:2)} cores`
    : `${limitMcpu} mcpu`;
  renderBar(kv.cpu, pct,
    `${usedMcpu} mcpu of ${limitLabel} (${pct.toFixed(1)}%, ${sourceSuffix(data.cpuLimitSource)})`);
}
function renderMem(data) {
  const used = data.rssBytes || 0, limit = data.memLimitBytes || 0;
  if (limit <= 0 || used <= 0) { kv.mem.textContent = used ? formatBytes(used) : "-"; return; }
  const pct = (used/limit) * 100;
  renderBar(kv.mem, pct,
    `${formatBytes(used)} of ${formatBytes(limit)} (${pct.toFixed(1)}%, ${sourceSuffix(data.memLimitSource)})`);
}
function renderPlayers(players) {
  playersCard.classList.remove("hidden");
  if (!players.length) {
    playersTable.classList.add("hidden"); playersEmpty.classList.remove("hidden"); return;
  }
  playersTable.classList.remove("hidden"); playersEmpty.classList.add("hidden");
  playersBody.innerHTML = "";
  players.forEach((p, i) => {
    const isPlaying = p.state === "ReadyToPlay";
    const nameMark = isPlaying ? "" : " (connecting…)";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${i+1}</td>
      <td>${escapeHtml(p.name)}${nameMark}</td>
      <td>${escapeHtml(p.state)}</td>
      <td class="mono">${escapeHtml(p.timeInGame)}</td>
      <td>${revealable(p.accountId)}</td>`;
    playersBody.appendChild(tr);
  });
  wireReveal(playersBody);
}
function renderWorlds(worlds) {
  if (!worlds.length) {
    worldsTable.classList.add("hidden"); worldsEmpty.classList.remove("hidden");
  } else {
    worldsTable.classList.remove("hidden"); worldsEmpty.classList.add("hidden");
    worldsBody.innerHTML = "";
    const activeId = lastStatus?.worldIslandId || "";
    worlds.forEach(w => {
      const active = w.islandId === activeId ? ' <span class="tag">active</span>' : "";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${revealable(w.islandId, 8, 4)}${active}</td>
        <td>${escapeHtml(w.worldName)}</td>
        <td>${escapeHtml(w.worldPresetType)}</td>
        <td class="mono">${escapeHtml(w.gameVersion)}</td>
        <td><button class="link world-edit-btn" data-id="${escapeHtml(w.islandId)}">edit</button></td>`;
      worldsBody.appendChild(tr);
    });
    wireReveal(worldsBody);
    worldsBody.querySelectorAll(".world-edit-btn").forEach(b =>
      b.addEventListener("click", () => openWorldEditor(b.dataset.id)));
  }
  // (re)populate active-world dropdown in config form
  fWorldId.innerHTML = "";
  const active = lastStatus?.worldIslandId || "";
  worlds.forEach(w => {
    const opt = document.createElement("option");
    opt.value = w.islandId;
    opt.textContent = `${w.worldName || w.islandId.slice(0,8)} (${w.worldPresetType || "?"})`;
    if (w.islandId === active) opt.selected = true;
    fWorldId.appendChild(opt);
  });
}
function renderBackups(backups) {
  if (!backups.length) {
    backupsTable.classList.add("hidden"); backupsEmpty.classList.remove("hidden"); return;
  }
  backupsTable.classList.remove("hidden"); backupsEmpty.classList.add("hidden");
  backupsBody.innerHTML = "";
  const destructive = lastStatus?.allowDestructive !== false;
  backups.forEach(b => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">${escapeHtml(b.id)}</td>
      <td>${formatBytes(b.sizeBytes)}</td>
      <td><button class="danger restore-btn" data-id="${escapeHtml(b.id)}" ${destructive ? "" : "disabled"}>Restore</button></td>`;
    backupsBody.appendChild(tr);
  });
  backupsBody.querySelectorAll(".restore-btn").forEach(b => {
    b.addEventListener("click", async () => {
      if (!confirm(`Restore backup ${b.dataset.id}? This swaps R5/Saved + identity files and restarts the server.`)) return;
      log(`restoring ${b.dataset.id}...`);
      const res = await authFetch(`/api/backups/${b.dataset.id}/restore`, { method: "POST" });
      log(`${res.ok ? "restore ok" : "restore failed"}: ${await res.text()}`);
    });
  });
}
function populateFormFromDoc(sd) {
  const p = sd.ServerDescription_Persistent || sd || {};
  suppressSync = true;
  try {
    fServerName.value = p.ServerName || "";
    fMaxPlayers.value = p.MaxPlayerCount ?? 4;
    fIsProtected.checked = !!p.IsPasswordProtected;
    fPassword.value = p.Password || "";
    fP2pAddr.value = p.P2pProxyAddress || "";
    fPSID.textContent = p.PersistentServerId || "-";
    fInvite.textContent = p.InviteCode || "-";
    if (p.WorldIslandId) {
      Array.from(fWorldId.options).forEach(o => { if (o.value === p.WorldIslandId) o.selected = true; });
    }
  } finally { suppressSync = false; }
}
function populateRawFromDoc(sd) {
  suppressSync = true;
  try { configEditor.value = JSON.stringify(sd, null, 2); }
  finally { suppressSync = false; }
}
function populateConfigForm() {
  if (!lastConfig) return;
  const sd = lastConfig.staged || lastConfig.live || {};
  populateFormFromDoc(sd);
  populateRawFromDoc(sd);
  hideConfigErrors();
  renderDiff();
}

// Myers-style LCS diff for line-level comparison. O(N*M) is fine for
// ~20-line JSON docs.
function diffLines(a, b) {
  const n = a.length, m = b.length;
  const dp = Array.from({length: n+1}, () => new Array(m+1).fill(0));
  for (let i = n-1; i >= 0; i--) {
    for (let j = m-1; j >= 0; j--) {
      if (a[i] === b[j]) dp[i][j] = dp[i+1][j+1] + 1;
      else dp[i][j] = Math.max(dp[i+1][j], dp[i][j+1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({type:"same", text:a[i]}); i++; j++; }
    else if (dp[i+1][j] >= dp[i][j+1]) { out.push({type:"del", text:a[i]}); i++; }
    else { out.push({type:"add", text:b[j]}); j++; }
  }
  while (i < n) out.push({type:"del", text:a[i++]});
  while (j < m) out.push({type:"add", text:b[j++]});
  return out;
}
function renderDiff() {
  if (!lastConfig || !lastConfig.staged) {
    configDiffBox.classList.add("hidden");
    return;
  }
  const liveLines   = JSON.stringify(lastConfig.live   || {}, null, 2).split("\n");
  const stagedLines = JSON.stringify(lastConfig.staged || {}, null, 2).split("\n");
  const parts = diffLines(liveLines, stagedLines);
  // Collapse long runs of "same" lines to keep the diff readable.
  configDiff.innerHTML = "";
  const MAX_SAME_RUN = 3;
  let sameRun = [];
  const flushSame = () => {
    if (!sameRun.length) return;
    if (sameRun.length <= MAX_SAME_RUN * 2) {
      sameRun.forEach(t => configDiff.appendChild(mk("same", "  " + t)));
    } else {
      sameRun.slice(0, MAX_SAME_RUN).forEach(t => configDiff.appendChild(mk("same", "  " + t)));
      configDiff.appendChild(mk("same", `  … (${sameRun.length - MAX_SAME_RUN * 2} unchanged lines)`));
      sameRun.slice(-MAX_SAME_RUN).forEach(t => configDiff.appendChild(mk("same", "  " + t)));
    }
    sameRun = [];
  };
  const mk = (cls, text) => {
    const s = document.createElement("span"); s.className = cls; s.textContent = text; return s;
  };
  parts.forEach(p => {
    if (p.type === "same") sameRun.push(p.text);
    else {
      flushSame();
      configDiff.appendChild(mk(p.type, (p.type === "add" ? "+ " : "- ") + p.text));
    }
  });
  flushSame();
  configDiffBox.classList.remove("hidden");
}
function buildConfigFromForm() {
  const base = lastConfig ? structuredClone(lastConfig.staged || lastConfig.live || {}) : {};
  base.ServerDescription_Persistent = base.ServerDescription_Persistent || {};
  const p = base.ServerDescription_Persistent;
  p.ServerName          = fServerName.value;
  p.MaxPlayerCount      = Number(fMaxPlayers.value) || 4;
  p.IsPasswordProtected = !!fIsProtected.checked;
  p.Password            = fPassword.value;
  p.P2pProxyAddress     = fP2pAddr.value;
  if (fWorldId.value) p.WorldIslandId = fWorldId.value;
  return base;
}
function showConfigErrors(errs) {
  if (!errs || !errs.length) return hideConfigErrors();
  configEditorErrors.textContent = "Validation errors:\n• " + errs.join("\n• ");
  configEditorErrors.classList.remove("hidden");
}
function hideConfigErrors() {
  configEditorErrors.textContent = "";
  configEditorErrors.classList.add("hidden");
}
function syncFormToRaw() {
  if (suppressSync) return;
  try {
    const doc = buildConfigFromForm();
    populateRawFromDoc(doc);
    hideConfigErrors();
  } catch (e) { /* ignore */ }
}
function syncRawToForm() {
  if (suppressSync) return;
  clearTimeout(rawDebounceTimer);
  rawDebounceTimer = setTimeout(async () => {
    const text = configEditor.value;
    let parsed;
    try { parsed = JSON.parse(text); }
    catch (e) { showConfigErrors([`invalid JSON: ${e.message}`]); return; }
    // Server-side schema validation.
    try {
      const res = await authFetch("/api/config/validate", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: text,
      });
      const v = await res.json();
      if (!v.valid) { showConfigErrors(v.errors); return; }
    } catch (e) { /* ignore transient */ }
    hideConfigErrors();
    populateFormFromDoc(parsed);
  }, 400);
}

function applyStatus(data) {
  lastStatus = data;
  kv.serverName.textContent = data.serverName || "-";
  pillText(kv.files, data.filesPresent ? "present" : "missing", data.filesPresent ? "pill-ok" : "pill-warn");
  pillText(kv.server, data.serverRunning ? "running" : "not running", data.serverRunning ? "pill-ok" : "pill-warn");
  kv.uptime.textContent = data.serverRunning ? formatUptime(data.uptimeSeconds) : "-";
  kv.backendRegion.textContent = data.backendRegion ? data.backendRegion.toUpperCase() : "-";
  kv.worldId.textContent = data.worldIslandId || "-";
  kv.saveVersion.textContent = data.saveVersion || "-";
  kv.worldCount.textContent = data.worldCount;
  kv.players.textContent = `${data.playerCount} / ${data.maxPlayerCount != null ? data.maxPlayerCount : "?"}`;
  renderCpu(data); renderMem(data);

  const destructive = data.allowDestructive !== false;
  const authed = data.authenticated === true;
  const authNeeded = data.adminAuthRequired === true;
  destructiveTag.classList.toggle("hidden", destructive);

  // Header auth box — reflects our session, not the server's.
  if (authed) {
    authState.textContent = "signed in";
    authBox.classList.add("authed");
    signInBtn.classList.add("hidden");
    signOutBtn.classList.remove("hidden");
  } else {
    authState.textContent = authNeeded ? "not signed in" : "no auth configured";
    authBox.classList.remove("authed");
    signInBtn.classList.toggle("hidden", !authNeeded);
    signOutBtn.classList.add("hidden");
  }

  // Public view: hide admin cards. Admin view: show everything unless
  // another rule hides it (e.g. worldEditorCard stays hidden until
  // Edit clicked).
  const showAdmin = authed || (authNeeded === false && destructive);
  document.querySelectorAll(".admin-only").forEach(el => {
    if (el.id === "worldEditorCard") {
      // Only hide worldEditorCard based on admin gating when not open;
      // its content flow is driven by openWorldEditor / worldCloseBtn.
      if (!showAdmin) el.classList.add("hidden");
      return;
    }
    el.classList.toggle("hidden", !showAdmin);
  });
  [uploadBtn, configSaveBtn, configApplyBtn, configRevertBtn, backupCreateBtn, worldUploadBtn, stopServerBtn, worldSaveBtn].forEach(b => {
    if (b) b.disabled = !destructive;
  });
  stagedTag.classList.toggle("hidden", !data.stagedConfigPending);

  // Adaptive admin banner — four states.
  adminBanner.classList.remove("danger", "info");
  if (authed) {
    // Authed implies destructive allowed (new semantics — we removed the
    // "auth'd but read-only" tri-state case).
    adminBannerLead.textContent = "⚠ Admin-only.";
    adminBannerText.innerHTML = "Signed in. Destructive actions (restart, upload, backup restore, world edits) are enabled.";
  } else if (!destructive) {
    adminBannerLead.textContent = "⚠ Admin-only (read-only).";
    adminBannerText.innerHTML = "No auth configured. Destructive actions are disabled. If this host is LAN-only you can leave it; otherwise set <code>UI_PASSWORD</code> (or front this with nginx basic-auth).";
  } else {
    // no auth + destructive = enableAdminWithoutPassword was set explicitly.
    adminBanner.classList.add("danger");
    adminBannerLead.textContent = "🛑 DANGER — open + writable.";
    adminBannerText.innerHTML = "No auth is configured but destructive endpoints are enabled via <code>enableAdminWithoutPassword=true</code>. Anyone who can reach this URL can wipe the server. Set <code>UI_PASSWORD</code> (or front this with nginx basic-auth) unless this is a firewalled LAN-only host.";
  }

  // Update card (collapsed by default) — tweak blurb based on state.
  if (!data.filesPresent) {
    importTitle.textContent = "Install WindowsServer Files";
    importBlurb.textContent = "No binary on the PVC. Pack WindowsServer/ from your Steam install and upload here — or let SteamCMD auto-install on next restart (source=steamcmd default).";
    uploadBtn.textContent = "Upload and start server";
    updateCard.open = true;  // expand when required
    restartHint.textContent = "";
  } else {
    importTitle.textContent = "Manual WindowsServer Update";
    uploadBtn.textContent = "Upload replacement";
    restartHint.textContent = data.serverRunning
      ? "After upload, restart the game container to load the new binary."
      : "";
  }

  const showInfo = !!data.inviteCode && data.filesPresent;
  infoCard.classList.toggle("hidden", !showInfo);
  if (showInfo) {
    inviteCodeEl.textContent = data.inviteCode;
    passwordNoteEl.textContent = data.isPasswordProtected
      ? "Password-protected: share the password out of band."
      : "Password: none.";
  }

  renderPlayers(data.players || []);
}

// --- API calls ----------------------------------------------------
async function loadStatus() {
  try {
    const res = await authFetch("/api/status", { cache: "no-store" });
    if (!res.ok) throw new Error(`status ${res.status}`);
    applyStatus(await res.json());
  } catch (err) { log("status load failed: " + err); }
}
async function loadConfig() {
  try {
    const res = await authFetch("/api/config");
    if (!res.ok) return;
    lastConfig = await res.json();
    renderWorlds(lastConfig.worlds || []);
    populateConfigForm();
  } catch (err) { log("config load failed: " + err); }
}
async function loadBackups() {
  try {
    const res = await authFetch("/api/backups");
    if (!res.ok) return;
    renderBackups((await res.json()).backups || []);
  } catch (err) { log("backups load failed: " + err); }
}

async function stageConfig(json) {
  const res = await authFetch("/api/config", {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(json),
  });
  log(`${res.ok ? "config staged" : "stage failed"}: ${await res.text()}`);
  loadStatus(); loadConfig();
}

// --- Event wiring -------------------------------------------------
refreshBtn.addEventListener("click", () => { loadStatus(); loadConfig(); loadBackups(); });
downloadSavesBtn.addEventListener("click", () => { window.location.href = "/api/saves/download"; });

uploadBtn.addEventListener("click", async () => {
  const file = archiveInput.files[0];
  if (!file) { log("select an archive first"); return; }
  log(`uploading ${file.name} (${Math.round(file.size/(1024*1024))} MiB)...`);
  uploadBtn.disabled = true;
  try {
    const res = await authFetch("/api/upload", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Filename": file.name },
      body: file,
    });
    log(`${res.ok ? "upload ok" : "upload failed"}: ${await res.text()}`);
  } catch (err) { log("upload error: " + err); }
  finally { uploadBtn.disabled = false; loadStatus(); loadBackups(); }
});

configSaveBtn.addEventListener("click", () => {
  // Prefer the raw textarea's current value (it's the most-recent
  // edit state, since the two-way sync keeps them aligned).
  let parsed;
  try { parsed = JSON.parse(configEditor.value); }
  catch (e) { showConfigErrors([`invalid JSON: ${e.message}`]); return; }
  stageConfig(parsed);
});

[fServerName, fMaxPlayers, fPassword, fP2pAddr, fWorldId].forEach(el =>
  el.addEventListener("input", syncFormToRaw));
fIsProtected.addEventListener("change", syncFormToRaw);
configEditor.addEventListener("input", syncRawToForm);

stopServerBtn.addEventListener("click", async () => {
  if (!confirm("Stop the Windrose game server? In-progress players will be disconnected. Kubelet (or Docker) will restart the container unless you scale the deployment to 0.")) return;
  const res = await authFetch("/api/server/stop", { method: "POST" });
  log(`${res.ok ? "server stop" : "server stop failed"}: ${await res.text()}`);
  setTimeout(loadStatus, 1500);
});

async function openWorldEditor(islandId) {
  try {
    const res = await authFetch(`/api/worlds/${islandId}/config`);
    if (!res.ok) { log(`load world failed: ${await res.text()}`); return; }
    const data = await res.json();
    editingWorldId = islandId;
    editingWorldDoc = data.content || {WorldDescription: {islandId, WorldName: "", WorldPresetType: "Medium", WorldSettings: {BoolParameters:{}, FloatParameters:{}, TagParameters:{}}}};
    worldEditorCard.classList.remove("hidden");
    worldEditorId.textContent = `${islandId.slice(0,8)}…${islandId.slice(-4)}`;
    const w = editingWorldDoc.WorldDescription || {};
    const s = w.WorldSettings || {};
    const fp = s.FloatParameters || {};
    const bp = s.BoolParameters  || {};
    fwName.value      = w.WorldName || "";
    fwPreset.value    = w.WorldPresetType || "Medium";
    fwMobHealth.value   = fp['{"TagName":"WDS.Parameter.MobHealthMultiplier"}']   ?? 1;
    fwMobDamage.value   = fp['{"TagName":"WDS.Parameter.MobDamageMultiplier"}']   ?? 1;
    fwShipHealth.value  = fp['{"TagName":"WDS.Parameter.ShipsHealthMultiplier"}'] ?? 1;
    fwShipDamage.value  = fp['{"TagName":"WDS.Parameter.ShipsDamageMultiplier"}'] ?? 1;
    fwBoarding.value    = fp['{"TagName":"WDS.Parameter.BoardingDifficultyMultiplier"}'] ?? 1;
    fwCoopQuests.checked  = !!bp['{"TagName":"WDS.Parameter.Coop.SharedQuests"}'];
    fwEasyExplore.checked = !!bp['{"TagName":"WDS.Parameter.EasyExplore"}'];
    worldEditorJson.value = JSON.stringify(editingWorldDoc, null, 2);
    worldEditorCard.scrollIntoView({behavior: "smooth"});
  } catch (err) { log("world editor error: " + err); }
}
function collectWorldDocFromForm() {
  const base = structuredClone(editingWorldDoc || {WorldDescription: {}});
  base.WorldDescription = base.WorldDescription || {};
  const w = base.WorldDescription;
  w.islandId        = editingWorldId;
  w.WorldName       = fwName.value;
  w.WorldPresetType = fwPreset.value;
  w.WorldSettings   = w.WorldSettings || {};
  w.WorldSettings.FloatParameters = w.WorldSettings.FloatParameters || {};
  w.WorldSettings.BoolParameters  = w.WorldSettings.BoolParameters  || {};
  const fp = w.WorldSettings.FloatParameters, bp = w.WorldSettings.BoolParameters;
  fp['{"TagName":"WDS.Parameter.MobHealthMultiplier"}']   = Number(fwMobHealth.value)  || 1;
  fp['{"TagName":"WDS.Parameter.MobDamageMultiplier"}']   = Number(fwMobDamage.value)  || 1;
  fp['{"TagName":"WDS.Parameter.ShipsHealthMultiplier"}'] = Number(fwShipHealth.value) || 1;
  fp['{"TagName":"WDS.Parameter.ShipsDamageMultiplier"}'] = Number(fwShipDamage.value) || 1;
  fp['{"TagName":"WDS.Parameter.BoardingDifficultyMultiplier"}'] = Number(fwBoarding.value) || 1;
  bp['{"TagName":"WDS.Parameter.Coop.SharedQuests"}'] = !!fwCoopQuests.checked;
  bp['{"TagName":"WDS.Parameter.EasyExplore"}']      = !!fwEasyExplore.checked;
  return base;
}
worldSaveBtn.addEventListener("click", async () => {
  if (!editingWorldId) return;
  const doc = collectWorldDocFromForm();
  const res = await authFetch(`/api/worlds/${editingWorldId}/config`, {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(doc),
  });
  log(`${res.ok ? "world saved" : "world save failed"}: ${await res.text()}`);
  loadConfig();
});
worldCloseBtn.addEventListener("click", () => {
  worldEditorCard.classList.add("hidden");
  editingWorldId = null; editingWorldDoc = null;
});
configApplyBtn.addEventListener("click", async () => {
  if (!confirm("Apply staged config and restart the server?")) return;
  const res = await authFetch("/api/config/apply", { method: "POST" });
  log(`${res.ok ? "apply ok" : "apply failed"}: ${await res.text()}`);
  loadStatus(); loadConfig();
});
configRevertBtn.addEventListener("click", async () => {
  const res = await authFetch("/api/config", { method: "DELETE" });
  log(`${res.ok ? "staged discarded" : "discard failed"}: ${await res.text()}`);
  loadStatus(); loadConfig();
});

backupCreateBtn.addEventListener("click", async () => {
  log("creating backup...");
  const res = await authFetch("/api/backups", { method: "POST" });
  log(`${res.ok ? "backup ok" : "backup failed"}: ${await res.text()}`);
  loadBackups();
});

worldUploadBtn.addEventListener("click", () => {
  const id = prompt("Island ID to upload into. Paste an existing one from the Worlds table to replace, or enter a new 32-char hex to create.");
  if (!id) return;
  if (!/^[0-9A-Fa-f]{32}$/.test(id)) { log("island ID must be 32 hex chars"); return; }
  selectedWorldSlot = id;
  worldUploadFile.click();
});
worldUploadFile.addEventListener("change", async () => {
  const file = worldUploadFile.files[0];
  if (!file || !selectedWorldSlot) return;
  log(`uploading world ${selectedWorldSlot} (${Math.round(file.size/(1024*1024))} MiB)...`);
  try {
    const res = await authFetch(`/api/worlds/${selectedWorldSlot}/upload`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Filename": file.name },
      body: file,
    });
    log(`${res.ok ? "world upload ok" : "world upload failed"}: ${await res.text()}`);
  } catch (err) { log("world upload error: " + err); }
  finally { worldUploadFile.value = ""; selectedWorldSlot = null; loadConfig(); loadBackups(); }
});

copyInviteBtn.addEventListener("click", async () => {
  const code = inviteCodeEl.textContent.trim();
  if (!code || code === "...") return;
  let copied = false;
  try {
    if (window.isSecureContext && navigator.clipboard) { await navigator.clipboard.writeText(code); copied = true; }
  } catch {}
  if (!copied) {
    const ta = document.createElement("textarea");
    ta.value = code; ta.style.position="fixed"; ta.style.opacity="0";
    document.body.appendChild(ta); ta.select();
    try { copied = document.execCommand("copy"); } catch {}
    document.body.removeChild(ta);
  }
  log(copied ? "invite copied" : "copy not permitted; select manually: " + code);
});

// --- Auth handlers ------------------------------------------------
signInBtn.addEventListener("click", () => {
  signInCard.classList.remove("hidden");
  signInError.classList.add("hidden");
  signInPassword.value = "";
  signInPassword.focus();
});
signInCancel.addEventListener("click", () => {
  signInCard.classList.add("hidden");
  signInError.classList.add("hidden");
});
async function attemptSignIn() {
  const pw = signInPassword.value;
  if (!pw) return;
  const header = "Basic " + btoa("admin:" + pw);
  // Probe a definitely-auth-gated endpoint to validate creds.
  const probe = await fetch("/api/config", { headers: { Authorization: header } });
  if (probe.ok) {
    setAuthHeader(header);
    signInCard.classList.add("hidden");
    signInError.classList.add("hidden");
    signInPassword.value = "";
    log("signed in");
    loadStatus(); loadConfig(); loadBackups();
  } else {
    signInError.textContent = probe.status === 401
      ? "Wrong password."
      : `Unexpected response: ${probe.status}`;
    signInError.classList.remove("hidden");
  }
}
signInSubmit.addEventListener("click", attemptSignIn);
signInPassword.addEventListener("keydown", e => { if (e.key === "Enter") attemptSignIn(); });
signOutBtn.addEventListener("click", () => {
  setAuthHeader("");
  log("signed out");
  loadStatus();
});

// --- Init ---------------------------------------------------------
loadStatus(); loadConfig(); loadBackups();
setInterval(loadStatus, 5000);
setInterval(loadBackups, 30000);
