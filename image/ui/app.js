// --- Element refs -------------------------------------------------
const adminBanner     = document.getElementById("adminBanner");
const adminBannerLead = document.getElementById("adminBannerLead");
const adminBannerText = document.getElementById("adminBannerText");
const destructiveTag = document.getElementById("destructiveTag");
const appTitle        = document.getElementById("appTitle");
const themeToggleBtn  = document.getElementById("themeToggleBtn");
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
const configRevertBtn     = document.getElementById("configRevertBtn");
const restartServerBtn    = document.getElementById("restartServerBtn");
const discardAllStagedBtn = document.getElementById("discardAllStagedBtn");
const worldsStagedTag     = document.getElementById("worldsStagedTag");
const worldEditorInline   = document.getElementById("worldEditorInline");
const worldEditorId       = document.getElementById("worldEditorId");
const worldStagedTag      = document.getElementById("worldStagedTag");
const worldEditorJson     = document.getElementById("worldEditorJson");
const worldConfigDiffBox  = document.getElementById("worldConfigDiffBox");
const worldConfigDiff     = document.getElementById("worldConfigDiff");
const worldStageBtn       = document.getElementById("worldStageBtn");
const worldDiscardBtn     = document.getElementById("worldDiscardBtn");
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
const ipBinaryState     = document.getElementById("ipBinaryState");
const ipBinaryMd5       = document.getElementById("ipBinaryMd5");
const ipEnvRequested    = document.getElementById("ipEnvRequested");
const ipOverride        = document.getElementById("ipOverride");
const ipEffective       = document.getElementById("ipEffective");
const ipNeedsRestart    = document.getElementById("ipNeedsRestart");
const ipEnableBtn       = document.getElementById("ipEnableBtn");
const ipDisableBtn      = document.getElementById("ipDisableBtn");
const ipAutoBtn         = document.getElementById("ipAutoBtn");
const ipApplyRestartBtn = document.getElementById("ipApplyRestartBtn");

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
let editingWorldDoc = null;   // current edit buffer (form-driven)
let editingWorldLive = null;  // on-disk live doc — used for diff
let suppressSync = false;   // avoid infinite sync loops between form and raw JSON
let rawDebounceTimer = null;

// --- FGameplayTag key helpers -------------------------------------
// World gametag keys are JSON-stringified dicts like
// '{"TagName": "WDS.Parameter.MobHealthMultiplier"}'. The game writes
// them with a space after the colon; JSON.stringify in JS emits them
// without. Matching on the raw string therefore silently fails, which
// was the "duplicate keys accumulating on every save" bug. These
// helpers read/write via parsed TagName so whitespace never matters,
// and always emit the canonical (with-space) form when writing.
function getTagValue(section, tagName) {
  if (!section || typeof section !== "object") return undefined;
  for (const [k, v] of Object.entries(section)) {
    try {
      const obj = JSON.parse(k);
      if (obj && obj.TagName === tagName) return v;
    } catch { /* non-JSON key; skip */ }
  }
  return undefined;
}
function setTagValue(section, tagName, value) {
  // Evict any existing keys that point at this tag, regardless of
  // whitespace — prevents duplicates from snowballing across saves.
  for (const k of Object.keys(section)) {
    try {
      const obj = JSON.parse(k);
      if (obj && obj.TagName === tagName) delete section[k];
    } catch { /* non-JSON key; skip */ }
  }
  // Canonical form: matches Python's json.dumps({"TagName": ...}).
  section[`{"TagName": "${tagName}"}`] = value;
}

// Stable serialization for the world editor's raw JSON view / diff.
// Without this, setTagValue's delete+insert reshuffles the textarea
// every time a form field changes — the edited tag pops to the
// bottom of its section, making diffs noisy. Server-side normalize
// sorts too, so initial load and edited-and-resaved land on the
// same order.
function stableStringifyWorld(doc) {
  if (!doc) return "";
  const clone = structuredClone(doc);
  const settings = clone?.WorldDescription?.WorldSettings;
  if (settings && typeof settings === "object") {
    for (const sec of ["BoolParameters", "FloatParameters", "TagParameters"]) {
      const obj = settings[sec];
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        settings[sec] = Object.fromEntries(
          Object.entries(obj).sort(([a], [b]) => a.localeCompare(b))
        );
      }
    }
  }
  return JSON.stringify(clone, null, 2);
}

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
  // Marker header so the server can suppress WWW-Authenticate on 401 —
  // otherwise the browser pops up its native Basic Auth modal the first
  // time the page loads and any unauth'd admin fetch 401s. Also pins
  // credentials: "omit" so the browser's per-origin basic-auth cache
  // cannot leak creds we didn't explicitly put on the request (which
  // would survive setAuthHeader("") and break sign-out).
  hdrs.set("X-Requested-With", "XMLHttpRequest");
  const res = await fetch(input, { ...(init || {}), headers: hdrs, credentials: "omit" });
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
function parseBackupId(id) {
  // "20260418T174447Z" → "2026-04-18T17:44:47Z"
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(id || "");
  return m ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z` : (id || "");
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
  // Public /api/status strips accountId from players[], so a connected
  // player on an unauth'd load arrives here as undefined. Render a
  // dash and bail rather than blowing up the whole status render.
  if (full == null || full === "") return "-";
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
  const anyStaged = worlds.some(w => w.staged);
  worldsStagedTag.classList.toggle("hidden", !anyStaged);
  if (!worlds.length) {
    worldsTable.classList.add("hidden"); worldsEmpty.classList.remove("hidden");
  } else {
    worldsTable.classList.remove("hidden"); worldsEmpty.classList.add("hidden");
    worldsBody.innerHTML = "";
    const activeId = lastStatus?.worldIslandId || "";
    worlds.forEach(w => {
      const active = w.islandId === activeId ? ' <span class="tag">active</span>' : "";
      const staged = w.staged ? ' <span class="tag">staged</span>' : "";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${revealable(w.islandId, 8, 4)}${active}${staged}</td>
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
    // Backup IDs are compact UTC stamps like "20260418T174447Z".
    // Prefer the server's createdAt (already ISO) and fall back to
    // parsing the id so older rows still render readably.
    const display = b.createdAt
      ? new Date(b.createdAt).toISOString().replace(/\.\d+Z$/, "Z")
      : parseBackupId(b.id);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono" title="${escapeHtml(b.id)}">${escapeHtml(display)}</td>
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
  // another rule hides it (e.g. worldEditorInline stays hidden until
  // Edit clicked).
  const showAdmin = authed || (authNeeded === false && destructive);
  document.querySelectorAll(".admin-only").forEach(el => {
    if (el.id === "worldEditorInline" || el.id === "discardAllStagedBtn") {
      // These are driven by their own state — leave them hidden on
      // sign-out but never force them visible on sign-in. Their show
      // logic runs separately (openWorldEditor, anyStaged check).
      if (!showAdmin) el.classList.add("hidden");
      return;
    }
    el.classList.toggle("hidden", !showAdmin);
  });
  // Fire admin data fetches once per transition into admin view.
  // Prevents unauth'd page loads from 401-spamming /api/config and
  // /api/backups (which otherwise triggers the browser's native auth
  // modal before our sign-in button is even clicked).
  if (showAdmin && !window._adminHydrated) {
    window._adminHydrated = true;
    loadConfig(); loadBackups(); loadIdlePatch();
  } else if (!showAdmin) {
    window._adminHydrated = false;
  }
  [uploadBtn, configSaveBtn, configRevertBtn, backupCreateBtn, worldUploadBtn,
   restartServerBtn, discardAllStagedBtn, worldStageBtn, worldDiscardBtn,
   ipEnableBtn, ipDisableBtn, ipAutoBtn, ipApplyRestartBtn].forEach(b => {
    if (b) b.disabled = !destructive;
  });
  stagedTag.classList.toggle("hidden", !data.stagedConfigPending);

  // Global restart button — label flips to "Apply + restart" whenever
  // ANY staged changes exist (server config or per-world). Keep
  // "Discard all staged" visible in the same window.
  const stagedWorldCount = Array.isArray(data.stagedWorlds) ? data.stagedWorlds.length : 0;
  const anyStaged = !!data.stagedConfigPending || stagedWorldCount > 0;
  restartServerBtn.textContent = anyStaged ? "Apply + restart" : "Restart server";
  discardAllStagedBtn.classList.toggle("hidden", !anyStaged);

  // H1 flips between a neutral "Status" title in the public view and
  // the full "Admin Console" label once signed in — so anons see a
  // status dashboard rather than an admin console they can't use.
  appTitle.textContent = showAdmin
    ? "Windrose Self-Hosted Admin Console"
    : "Windrose Self-Hosted";

  // Adaptive admin banner. Only surfaces when the state actually
  // warrants an operator warning — a plain "not-signed-in yet" page
  // doesn't need one (the sign-in button is right there).
  adminBanner.classList.remove("danger", "info");
  if (authed) {
    adminBanner.classList.remove("hidden");
    adminBannerLead.textContent = "⚠ Admin-only.";
    adminBannerText.innerHTML = "Signed in. Destructive actions (restart, upload, backup restore, world edits) are enabled.";
  } else if (!authNeeded && !destructive) {
    // Auth disabled + destructive disabled: read-only admin console.
    adminBanner.classList.remove("hidden");
    adminBannerLead.textContent = "⚠ Admin-only (read-only).";
    adminBannerText.innerHTML = "No auth configured. Destructive actions are disabled. If this host is LAN-only you can leave it; otherwise set <code>UI_PASSWORD</code> (or front this with nginx basic-auth).";
  } else if (!authNeeded && destructive) {
    // enableAdminWithoutPassword explicitly set — loud DANGER warning.
    adminBanner.classList.remove("hidden");
    adminBanner.classList.add("danger");
    adminBannerLead.textContent = "🛑 DANGER — open + writable.";
    adminBannerText.innerHTML = "No auth is configured but destructive endpoints are enabled via <code>enableAdminWithoutPassword=true</code>. Anyone who can reach this URL can wipe the server. Set <code>UI_PASSWORD</code> (or front this with nginx basic-auth) unless this is a firewalled LAN-only host.";
  } else {
    // Public view with auth required (authNeeded && !authed) — user just
    // needs to click Sign in. No operator warning warranted.
    adminBanner.classList.add("hidden");
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

async function loadIdlePatch() {
  try {
    const res = await authFetch("/api/idle-cpu-patch");
    if (!res.ok) return;
    renderIdlePatch(await res.json());
  } catch (err) { log("idle-patch load failed: " + err); }
}

function renderIdlePatch(s) {
  const stateLabel = {
    unpatched: "unpatched",
    patched:   "patched",
    corrupt:   "corrupt (restore from backup)",
    missing:   "binary missing",
    inapplicable: "signature not found in this build",
    unknown:   "unknown",
  }[s.binaryState] || s.binaryState || "-";
  ipBinaryState.textContent = stateLabel + (s.binaryReason ? ` — ${s.binaryReason}` : "");
  ipBinaryMd5.textContent   = s.binaryMd5 || "-";
  ipEnvRequested.textContent = s.envRequested ? "WINDROSE_PATCH_IDLE_CPU=1" : "WINDROSE_PATCH_IDLE_CPU=0 (or unset)";
  ipOverride.textContent = s.override === "auto" ? "auto (follow env)" : s.override;
  ipEffective.textContent = s.effectiveOn ? "ON (patch will be applied)" : "OFF (patch will be reverted)";
  ipNeedsRestart.classList.toggle("hidden", !s.needsRestart);
  ipApplyRestartBtn.classList.toggle("hidden", !s.needsRestart);
  // Disable whichever button matches the current override to nudge intent.
  ipEnableBtn.disabled  = s.override === "enabled";
  ipDisableBtn.disabled = s.override === "disabled";
  ipAutoBtn.disabled    = s.override === "auto";
}

async function setIdlePatchOverride(value, restart = false) {
  log(`idle-patch override -> ${value}${restart ? " + restart" : ""}...`);
  const res = await authFetch("/api/idle-cpu-patch", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ override: value, restart }),
  });
  if (!res.ok) { log(`idle-patch update failed: ${await res.text()}`); return; }
  const s = await res.json();
  renderIdlePatch(s);
  if (s.restartRequested) log("restart signaled to game container");
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
refreshBtn.addEventListener("click", () => { loadStatus(); loadConfig(); loadBackups(); loadIdlePatch(); });
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

// Global restart button — dynamic label (managed in applyStatus).
// When staged changes exist anywhere, it hits /api/config/apply which
// swaps server + per-world staged files in before killing the game.
// Otherwise it just signals SIGTERM and lets the supervisor bring the
// game back up.
restartServerBtn.addEventListener("click", async () => {
  const hasStaged = !!(lastStatus?.stagedConfigPending ||
    (Array.isArray(lastStatus?.stagedWorlds) && lastStatus.stagedWorlds.length));
  const prompt = hasStaged
    ? "Apply staged changes and restart the server? In-progress players will be disconnected."
    : "Restart the game server? In-progress players will be disconnected.";
  if (!confirm(prompt)) return;
  const url = hasStaged ? "/api/config/apply" : "/api/server/restart";
  const res = await authFetch(url, { method: "POST" });
  log(`${res.ok ? "ok" : "failed"}: ${await res.text()}`);
  setTimeout(() => { loadStatus(); loadConfig(); }, 1500);
});
discardAllStagedBtn.addEventListener("click", async () => {
  if (!confirm("Discard all staged changes (server config + every world)?")) return;
  // Server staging first (if any)...
  if (lastStatus?.stagedConfigPending) {
    await authFetch("/api/config", { method: "DELETE" });
  }
  // ...then each staged world.
  for (const id of (lastStatus?.stagedWorlds || [])) {
    await authFetch(`/api/worlds/${id}/config`, { method: "DELETE" });
  }
  log("all staged discarded");
  loadStatus(); loadConfig();
});

async function openWorldEditor(islandId) {
  try {
    const res = await authFetch(`/api/worlds/${islandId}/config`);
    if (!res.ok) { log(`load world failed: ${await res.text()}`); return; }
    const data = await res.json();
    editingWorldId = islandId;
    editingWorldLive = data.live || null;
    // Edit buffer starts from staged (if present) so the user picks up
    // where they left off — otherwise from live. Both are normalized
    // server-side before we receive them, so no duplicate tag keys.
    const source = data.staged || data.live || {WorldDescription: {islandId, WorldName: "", WorldPresetType: "Medium", WorldSettings: {BoolParameters:{}, FloatParameters:{}, TagParameters:{}}}};
    editingWorldDoc = structuredClone(source);
    worldEditorInline.classList.remove("hidden");
    worldEditorId.textContent = `${islandId.slice(0,8)}…${islandId.slice(-4)}`;
    worldStagedTag.classList.toggle("hidden", !data.staged);
    populateWorldFormFromDoc(editingWorldDoc);
    worldEditorJson.value = stableStringifyWorld(editingWorldDoc);
    renderWorldDiff();
    worldEditorInline.scrollIntoView({behavior: "smooth", block: "nearest"});
  } catch (err) { log("world editor error: " + err); }
}
function populateWorldFormFromDoc(doc) {
  const w  = doc?.WorldDescription || {};
  const s  = w.WorldSettings || {};
  const fp = s.FloatParameters || {};
  const bp = s.BoolParameters  || {};
  fwName.value      = w.WorldName || "";
  fwPreset.value    = w.WorldPresetType || "Medium";
  fwMobHealth.value  = getTagValue(fp, "WDS.Parameter.MobHealthMultiplier")   ?? 1;
  fwMobDamage.value  = getTagValue(fp, "WDS.Parameter.MobDamageMultiplier")   ?? 1;
  fwShipHealth.value = getTagValue(fp, "WDS.Parameter.ShipsHealthMultiplier") ?? 1;
  fwShipDamage.value = getTagValue(fp, "WDS.Parameter.ShipsDamageMultiplier") ?? 1;
  fwBoarding.value   = getTagValue(fp, "WDS.Parameter.BoardingDifficultyMultiplier") ?? 1;
  fwCoopQuests.checked  = !!getTagValue(bp, "WDS.Parameter.Coop.SharedQuests");
  fwEasyExplore.checked = !!getTagValue(bp, "WDS.Parameter.EasyExplore");
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
  setTagValue(fp, "WDS.Parameter.MobHealthMultiplier",   Number(fwMobHealth.value)  || 1);
  setTagValue(fp, "WDS.Parameter.MobDamageMultiplier",   Number(fwMobDamage.value)  || 1);
  setTagValue(fp, "WDS.Parameter.ShipsHealthMultiplier", Number(fwShipHealth.value) || 1);
  setTagValue(fp, "WDS.Parameter.ShipsDamageMultiplier", Number(fwShipDamage.value) || 1);
  setTagValue(fp, "WDS.Parameter.BoardingDifficultyMultiplier", Number(fwBoarding.value) || 1);
  setTagValue(bp, "WDS.Parameter.Coop.SharedQuests", !!fwCoopQuests.checked);
  setTagValue(bp, "WDS.Parameter.EasyExplore",       !!fwEasyExplore.checked);
  return base;
}
function renderWorldDiff() {
  if (!editingWorldLive || !editingWorldDoc) {
    worldConfigDiffBox.classList.add("hidden"); return;
  }
  const a = stableStringifyWorld(editingWorldLive);
  const b = stableStringifyWorld(editingWorldDoc);
  if (a === b) { worldConfigDiffBox.classList.add("hidden"); return; }
  const lines = diffLines(a.split("\n"), b.split("\n"));
  worldConfigDiff.innerHTML = lines.map(([tag, text]) => {
    const cls = tag === "+" ? "add" : tag === "-" ? "del" : "same";
    return `<span class="${cls}">${tag} ${escapeHtml(text)}</span>`;
  }).join("");
  worldConfigDiffBox.classList.remove("hidden");
}
// Keep raw JSON + form in sync. Field edits rebuild the full doc from
// the form, then re-render the diff so "what's about to be staged"
// tracks live.
[fwName, fwPreset, fwMobHealth, fwMobDamage, fwShipHealth, fwShipDamage,
 fwBoarding, fwCoopQuests, fwEasyExplore].forEach(el =>
  el.addEventListener("input", () => {
    if (!editingWorldId) return;
    editingWorldDoc = collectWorldDocFromForm();
    worldEditorJson.value = stableStringifyWorld(editingWorldDoc);
    renderWorldDiff();
  }));
worldEditorJson.addEventListener("input", () => {
  if (!editingWorldId) return;
  try {
    editingWorldDoc = JSON.parse(worldEditorJson.value);
    populateWorldFormFromDoc(editingWorldDoc);
    renderWorldDiff();
  } catch { /* wait for valid JSON */ }
});
worldStageBtn.addEventListener("click", async () => {
  if (!editingWorldId) return;
  const doc = collectWorldDocFromForm();
  const res = await authFetch(`/api/worlds/${editingWorldId}/config`, {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(doc),
  });
  log(`${res.ok ? "world staged" : "world stage failed"}: ${await res.text()}`);
  loadStatus(); loadConfig();
});
worldDiscardBtn.addEventListener("click", async () => {
  if (!editingWorldId) return;
  if (!confirm("Discard this world's staged changes?")) return;
  const res = await authFetch(`/api/worlds/${editingWorldId}/config`, { method: "DELETE" });
  log(`${res.ok ? "world staged discarded" : "world discard failed"}: ${await res.text()}`);
  // Reload the world so the form reverts to live.
  if (editingWorldId) openWorldEditor(editingWorldId);
  loadStatus(); loadConfig();
});
worldCloseBtn.addEventListener("click", () => {
  worldEditorInline.classList.add("hidden");
  editingWorldId = null; editingWorldDoc = null; editingWorldLive = null;
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

ipEnableBtn.addEventListener("click", () => setIdlePatchOverride("enabled"));
ipDisableBtn.addEventListener("click", () => setIdlePatchOverride("disabled"));
ipAutoBtn.addEventListener("click", () => setIdlePatchOverride(null));
ipApplyRestartBtn.addEventListener("click", async () => {
  if (!confirm("Restart the game container now so the patch change takes effect?")) return;
  // Re-send the current override to trigger the server-side restart flow.
  const cur = ipOverride.textContent.trim();
  const val = cur.startsWith("auto") ? null : cur;
  await setIdlePatchOverride(val, /*restart=*/true);
  loadStatus();
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

// --- Theme toggle -------------------------------------------------
// Inline script in index.html's <head> applies the stored preference
// BEFORE first paint. This just handles click-to-cycle + glyph update.
// Three-state cycle: auto (follow OS) → light → dark → auto.
function currentThemeMode() {
  const stored = localStorage.getItem("windrose.theme");
  return stored === "light" || stored === "dark" ? stored : "auto";
}
function updateThemeToggleGlyph() {
  const mode = currentThemeMode();
  // ◐ (auto/OS) — ☀ (forced light) — ☾ (forced dark)
  themeToggleBtn.textContent = mode === "light" ? "☀" : mode === "dark" ? "☾" : "◐";
  themeToggleBtn.title = `Theme: ${mode} (click to cycle)`;
}
themeToggleBtn.addEventListener("click", () => {
  const next = { auto: "light", light: "dark", dark: "auto" }[currentThemeMode()];
  if (next === "auto") {
    localStorage.removeItem("windrose.theme");
    document.documentElement.removeAttribute("data-theme");
  } else {
    localStorage.setItem("windrose.theme", next);
    document.documentElement.setAttribute("data-theme", next);
  }
  updateThemeToggleGlyph();
});
updateThemeToggleGlyph();

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
  // Probe a definitely-auth-gated endpoint to validate creds. Pass the
  // XHR marker so the server suppresses WWW-Authenticate — otherwise a
  // wrong password here would pop up the browser's native modal.
  const probe = await fetch("/api/config", {
    headers: { Authorization: header, "X-Requested-With": "XMLHttpRequest" },
    credentials: "omit",
  });
  if (probe.ok) {
    setAuthHeader(header);
    signInCard.classList.add("hidden");
    signInError.classList.add("hidden");
    signInPassword.value = "";
    log("signed in");
    // applyStatus will fire loadConfig/loadBackups once it sees
    // showAdmin transition true.
    loadStatus();
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
// Only fetch the public status on boot. Admin endpoints get fetched
// the first time applyStatus() sees we have admin access — otherwise
// an unauth'd page load would 401-spam /api/config and /api/backups.
loadStatus();
setInterval(loadStatus, 5000);
setInterval(() => { if (window._adminHydrated) loadBackups(); }, 30000);
setInterval(() => { if (window._adminHydrated) loadIdlePatch(); }, 30000);
