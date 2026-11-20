"use strict";

const $ = id => document.getElementById(id);
let profiles = [];
let running = false;
let backupsFor = "";
let credentials = [];
let discoveredDatabases = [];
let databaseDiscoveryFor = "";
const selected = id => profiles.find(p => p.id === $(id).value);
const profileSelectionKey = "backupRestore.profileSelections";
const profileSelectIds = ["profileEditor", "sourceProfile", "destProfile"];
let profilesLoaded = false;
let editorSelectionExplicit = false;
function readProfileSelections() {
  try {
    const saved = JSON.parse(localStorage.getItem(profileSelectionKey) || "{}");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
    return Object.fromEntries(profileSelectIds.map(id => [id, typeof saved[id] === "string" ? saved[id] : ""]));
  } catch { return {}; }
}
let rememberedProfileSelections = readProfileSelections();
function rememberProfileSelections() {
  if (!profilesLoaded) return;
  rememberedProfileSelections = Object.fromEntries(profileSelectIds.map(id => [id, $(id).value]));
  try { localStorage.setItem(profileSelectionKey, JSON.stringify(rememberedProfileSelections)); }
  catch { /* Saved profiles remain available when browser storage is unavailable. */ }
}

function setStatus(message, tone = "neutral", output = "") {
  $("status").textContent = message;
  $("status").className = "status " + tone;
  if (output) $("output").textContent = output;
}
function busy(on) {
  running = on;
  document.querySelectorAll("input, select, button").forEach(el => { el.disabled = on; });
  if (!on) $("deleteProfile").disabled = !$("profileEditor").value;
}
async function request(url, data, method = "POST") {
  const token = $("token").value.trim();
  if (!token) throw new Error("Enter APP_TOKEN from your .env file first.");
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-Admin-Token": token },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
    cache: "no-store",
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (typeof result.detail === "string") throw new Error(result.detail);
    if (Array.isArray(result.detail)) throw new Error(result.detail.map(e => `${(e.loc || []).join(".")}: ${e.msg}`).join("\n"));
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  return result;
}
async function perform(action, title) {
  if (running) return;
  busy(true);
  setStatus(title, "working");
  try { await action(); }
  catch (error) { setStatus("Operation failed", "error", error.message || String(error)); }
  finally { busy(false); }
}
function option(value, label) {
  const item = document.createElement("option");
  item.value = value;
  item.textContent = label;
  return item;
}
function fillSelect(id, items, placeholder, desired) {
  const select = $(id);
  select.replaceChildren(option("", placeholder), ...items.map(p => option(p.id, `${p.name} — ${p.ssh.host}${p.destination && id === "destProfile" ? ` / ${p.destination.database}` : ""}`)));
  select.value = items.some(p => p.id === desired) ? desired : "";
}
function resetConfirmations() {
  $("writesPaused").checked = false;
  $("agreeReplace").checked = false;
  $("confirmation").value = "";
}
function invalidateBackups() {
  backupsFor = "";
  $("backupName").replaceChildren(option("", "Load backups first"));
  resetConfirmations();
}
function summaries() {
  const source = selected("sourceProfile");
  const dest = selected("destProfile");
  $("sourceSummary").textContent = source ? `${source.ssh.username}@${source.ssh.host}:${source.ssh.port} · ${source.backup_directory}` : "Select a backup storage profile.";
  $("destSummary").textContent = dest ? `${dest.ssh.username}@${dest.ssh.host}:${dest.ssh.port} · container ${dest.destination.container} · database ${dest.destination.database} · role ${dest.destination.database_user} · safety folder ${dest.destination.safety_backup_directory || "controller default"}` : "Select a destination profile.";
  $("restoreSummary").textContent = source && dest && $("backupName").value ? `${source.name}: ${$("backupName").value} → ${dest.name}: ${dest.destination.database} on ${dest.ssh.host}` : "Select a source archive and destination.";
  $("confirmation").placeholder = dest ? dest.destination.database : "Destination database name";
}
function roleFields() {
  $("storageFields").hidden = !$("profileStorage").checked;
  $("targetFields").hidden = !$("profileTarget").checked;
  $("profileBackupDir").required = $("profileStorage").checked;
  ["profileContainer", "profileDatabase", "profileDbUser"].forEach(id => { $(id).required = $("profileTarget").checked; });
}
function sshSettings() {
  const credentialId = $("managedCredential").value;
  const nullable = id => $(id).value.trim() || null;
  return { host: $("profileHost").value.trim(), port: Number($("profilePort").value),
    username: $("profileUser").value.trim(), credential_id: credentialId || null,
    key_path: credentialId ? null : nullable("profileKey"),
    known_hosts_path: credentialId ? null : nullable("profileKnownHosts"),
    passphrase_env: credentialId ? null : nullable("profilePassphraseEnv") };
}
function credentialFields() {
  const credential = credentials.find(item => item.id === $("managedCredential").value);
  $("advancedSsh").hidden = Boolean(credential);
  $("publicKeyDetails").hidden = !credential;
  $("managedPublicKey").textContent = credential?.public_key || "";
}
function resetEnrollment() {
  $("verifiedFingerprint").value = "";
  $("bootstrapPassword").value = "";
  $("hostFingerprint").textContent = "Read and verify the SSH fingerprint for this host and port before authorizing a key.";
}
async function refreshCredentials(desired = $("managedCredential").value) {
  credentials = (await request("/api/ssh/credentials", undefined, "GET")).items;
  $("managedCredential").replaceChildren(option("", "Generate a key or use advanced settings below"),
    ...credentials.map(item => option(item.id, `${item.name} · ${item.fingerprint}`)));
  $("managedCredential").value = credentials.some(item => item.id === desired) ? desired : "";
  credentialFields();
}
function editProfile() {
  const p = selected("profileEditor");
  $("profileForm").reset();
  $("managedCredential").value = p?.ssh.credential_id || "";
  if (p) {
    const values = {
      profileName: p.name, profileHost: p.ssh.host, profilePort: p.ssh.port,
      profileUser: p.ssh.username, profileKey: p.ssh.key_path,
      profileKnownHosts: p.ssh.known_hosts_path, profilePassphraseEnv: p.ssh.passphrase_env,
      profileBackupDir: p.backup_directory, profileContainer: p.destination?.container,
      profileDatabase: p.destination?.database, profileDbUser: p.destination?.database_user,
      profileSafetyDir: p.destination?.safety_backup_directory || "/var/backups/backup-restore-starter",
    };
    for (const [id, value] of Object.entries(values)) $(id).value = value ?? "";
    $("profileStorage").checked = p.backup_directory !== null;
    $("profileTarget").checked = p.destination !== null;
  }
  $("deleteProfile").disabled = running || !p;
  roleFields();
  credentialFields();
  resetEnrollment();
  resetDatabaseDiscovery(false);
  if (p?.destination) {
    discoveredDatabases = [p.destination];
    databaseDiscoveryFor = JSON.stringify(sshSettings());
    $("discoveredDatabase").append(option("0", `${p.destination.container} / ${p.destination.database} (saved)`));
    $("discoveredDatabase").value = "0";
    $("databaseDiscoverySummary").textContent = `Saved destination: ${p.destination.database} in ${p.destination.container}, role ${p.destination.database_user}. Find databases to refresh the list.`;
  }
}
async function refreshProfiles(editorId = $("profileEditor").value) {
  const firstLoad = !profilesLoaded;
  const sourceId = firstLoad ? rememberedProfileSelections.sourceProfile : $("sourceProfile").value;
  const destId = firstLoad ? rememberedProfileSelections.destProfile : $("destProfile").value;
  if (firstLoad && !editorSelectionExplicit && !editorId) editorId = rememberedProfileSelections.profileEditor;
  profiles = (await request("/api/profiles", undefined, "GET")).items;
  if (firstLoad && !editorSelectionExplicit && !profiles.some(p => p.id === editorId)) editorId = profiles[0]?.id || "";
  fillSelect("profileEditor", profiles, "New server profile", editorId);
  fillSelect("sourceProfile", profiles.filter(p => p.backup_directory !== null), "Select backup storage", sourceId);
  fillSelect("destProfile", profiles.filter(p => p.destination !== null), "Select destination database", destId);
  profilesLoaded = true;
  rememberProfileSelections();
  $("savedProfilesStatus").textContent = profiles.length
    ? `${profiles.length} saved server profile(s) loaded. Select a saved profile below to reuse or update its details.`
    : "No saved server profiles yet. Enter your server details and choose Save profile once; they will be available after restarting.";
  invalidateBackups();
  editProfile();
  summaries();
}

$("loadProfiles").addEventListener("click", () => perform(async () => {
  await refreshCredentials();
  await refreshProfiles();
  setStatus(`Loaded ${profiles.length} saved profile(s)`, "success", profiles.length ? "Your saved server details are loaded. Choose a profile to edit, or select backup storage and a destination below." : "Enter your server details and choose Save profile once. Next time, connect to load them again.");
}, "Loading saved servers..."));
$("token").addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); $("loadProfiles").click(); }
});
$("profileEditor").addEventListener("change", () => { editorSelectionExplicit = true; rememberProfileSelections(); editProfile(); });
$("newProfile").addEventListener("click", () => { editorSelectionExplicit = true; $("profileEditor").value = ""; rememberProfileSelections(); editProfile(); });
$("profileStorage").addEventListener("change", roleFields);
$("profileTarget").addEventListener("change", roleFields);
$("profileForm").addEventListener("submit", event => {
  event.preventDefault();
  const id = $("profileEditor").value;
  const nullable = field => $(field).value.trim() || null;
  const data = {
    name: $("profileName").value.trim(),
    ssh: sshSettings(),
    backup_directory: $("profileStorage").checked ? $("profileBackupDir").value.trim() : null,
    destination: $("profileTarget").checked ? { container: $("profileContainer").value.trim(), database: $("profileDatabase").value.trim(),
      database_user: $("profileDbUser").value.trim(), safety_backup_directory: nullable("profileSafetyDir") } : null,
  };
  perform(async () => {
    const result = await request(id ? `/api/profiles/${encodeURIComponent(id)}` : "/api/profiles", data, id ? "PUT" : "POST");
    await refreshProfiles(result.id);
    setStatus("Server profile saved", "success", "Server details are saved on the controller and survive app restarts. Next time, enter your app token and connect to load them again. Reload backups after configuration changes.");
  }, "Saving server profile...");
});
$("deleteProfile").addEventListener("click", () => {
  const p = selected("profileEditor");
  if (!p || !window.confirm(`Delete saved profile "${p.name}"? Remote servers and backups are unaffected.`)) return;
  perform(async () => {
    await request(`/api/profiles/${encodeURIComponent(p.id)}`, undefined, "DELETE");
    await refreshProfiles("");
    setStatus("Server profile deleted", "success");
  }, "Deleting profile...");
});
$("sourceProfile").addEventListener("change", () => { rememberProfileSelections(); invalidateBackups(); summaries(); });
$("destProfile").addEventListener("change", () => { rememberProfileSelections(); resetConfirmations(); summaries(); });
$("backupName").addEventListener("change", () => { resetConfirmations(); summaries(); });
$("loadBackups").addEventListener("click", () => perform(async () => {
  invalidateBackups();
  summaries();
  const p = selected("sourceProfile");
  if (!p) throw new Error("Select a backup storage profile first.");
  const result = await request("/api/profile-backups", { source_profile_id: p.id });
  $("backupName").replaceChildren(option("", result.items.length ? "Choose an archive" : "No .dump files found"), ...result.items.map(item => option(item.filename, `${item.filename} · ${(item.bytes / 1024 / 1024).toFixed(2)} MB`)));
  backupsFor = p.id;
  summaries();
  setStatus(`Loaded ${result.items.length} backup(s) from ${p.name}`, "success", JSON.stringify(result.items, null, 2));
}, "Reading backup storage over SSH..."));
$("checkDestination").addEventListener("click", () => perform(async () => {
  const p = selected("destProfile");
  if (!p) throw new Error("Select a destination profile first.");
  const result = await request("/api/profile-preflight", { destination_profile_id: p.id });
  setStatus(`Destination ${p.name} is reachable`, "success", JSON.stringify(result, null, 2));
}, "Checking destination SSH, Docker and PostgreSQL..."));
$("restore").addEventListener("click", () => perform(async () => {
  const source = selected("sourceProfile");
  const dest = selected("destProfile");
  if (!source || !dest || !$("backupName").value || backupsFor !== source.id) throw new Error("Select source and destination profiles, load backups, and choose an archive.");
  const data = { source_profile_id: source.id, destination_profile_id: dest.id,
    source_profile_revision: source.revision, destination_profile_revision: dest.revision,
    backup_filename: $("backupName").value, confirmation: $("confirmation").value,
    writes_paused: $("writesPaused").checked, agree_replace: $("agreeReplace").checked };
  if (!data.agree_replace || !data.writes_paused || data.confirmation !== dest.destination.database) throw new Error("Pause writes, acknowledge replacement, and type the exact destination database name.");
  if (!window.confirm(`Replace backed-up objects in ${dest.destination.database} on ${dest.ssh.host}:${dest.ssh.port} (container ${dest.destination.container}) using ${data.backup_filename} from ${source.ssh.host}? A safety dump will be created first.`)) {
    setStatus("Restore cancelled", "neutral");
    return;
  }
  try {
    const result = await request("/api/profile-restore", data);
    setStatus("Restore completed. Verify database records before resuming writes.", "success", JSON.stringify(result, null, 2));
  } finally { resetConfirmations(); }
}, "Creating safety backup and restoring. Keep the app running..."));
roleFields();

$("managedCredential").addEventListener("change", () => { credentialFields(); $("bootstrapPassword").value = ""; });
["profileHost", "profilePort", "profileUser"].forEach(id => $(id).addEventListener("input", resetEnrollment));
$("generateKey").addEventListener("click", () => perform(async () => {
  const name = $("profileName").value.trim() || $("profileHost").value.trim();
  if (!name) throw new Error("Enter a profile name or VPS host before generating a key.");
  const result = await request("/api/ssh/credentials", { name });
  await refreshCredentials(result.id);
  setStatus("SSH key generated and selected", "success", "Next: verify the VPS fingerprint, authorize this public key, test SSH access, and save the profile. The private key stays encrypted on the controller.");
}, "Generating and encrypting an SSH key..."));
$("scanHost").addEventListener("click", () => perform(async () => {
  const result = await request("/api/ssh/scan", sshSettings());
  $("hostFingerprint").textContent = `${result.host}:${result.port} · ${result.key_type} · ${result.fingerprint} (not yet verified)`;
  $("verifiedFingerprint").value = "";
  setStatus("Server fingerprint read. Verify it before trusting.", "neutral", JSON.stringify(result, null, 2));
}, "Reading the server SSH identity without sending credentials..."));
$("trustHost").addEventListener("click", () => perform(async () => {
  const result = await request("/api/ssh/trust", { server: sshSettings(), fingerprint: $("verifiedFingerprint").value.trim() });
  $("hostFingerprint").textContent = `Verified and saved: ${result.host}:${result.port} · ${result.fingerprint}`;
  setStatus("Verified server identity saved", "success", "The app will check this identity on every managed-key SSH connection. You can now authorize the generated public key.");
}, "Checking and saving the verified server identity..."));
$("authorizeKey").addEventListener("click", () => {
  const password = $("bootstrapPassword").value;
  $("bootstrapPassword").value = "";
  perform(async () => {
    if (!$("managedCredential").value) throw new Error("Generate or select an SSH key first.");
    if (!password) throw new Error("Enter this VPS account's current SSH password, or use the public-key console instructions.");
    const result = await request("/api/ssh/authorize", { server: sshSettings(), password });
    setStatus("Public key installed", "success", result.message);
  }, "Authorizing the public key with a one-time SSH password login...");
});
$("testSsh").addEventListener("click", () => perform(async () => {
  const result = await request("/api/ssh/test", sshSettings());
  setStatus("SSH key access verified", "success", `${result.message}. Save the profile to use these settings for backups and restores.`);
}, "Testing SSH key login and SFTP..."));
credentialFields();

let folderContext = null;
let folderState = null;
function folderControls() {
  $("folderUp").disabled = running || !folderState?.parent;
  $("folderSelect").disabled = running || !folderState;
}
function renderFolderItems() {
  const filter = $("folderFilter").value.toLocaleLowerCase();
  const items = (folderState?.items || []).filter(item => item.name.toLocaleLowerCase().includes(filter));
  $("folderList").replaceChildren();
  for (const item of items) {
    const row = document.createElement("li");
    const entry = document.createElement(item.is_directory ? "button" : "div");
    entry.className = "folder-entry" + (item.is_directory ? "" : " file");
    const icon = document.createElement("span");
    icon.textContent = item.is_directory ? "📁" : "📄";
    icon.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.textContent = item.name;
    entry.append(icon, label);
    if (item.is_directory) {
      entry.type = "button";
      entry.addEventListener("click", () => loadFolder(item.path));
    }
    row.append(entry);
    $("folderList").append(row);
  }
  if (folderState) {
    const count = folderState.items.filter(item => item.is_directory).length;
    const dumps = folderState.items.length - count;
    $("folderMessage").textContent = `${count} folder(s), ${dumps} backup file(s). Open a folder or select the current folder.` +
      (!items.length ? " No matching folders or backups to display." : "") +
      (folderState.truncated ? " Showing the first 500 entries; additional entries are omitted." : "");
    $("folderMessage").className = "helper";
  }
}
function renderFolderBreadcrumbs(path) {
  const nav = $("folderBreadcrumbs");
  nav.replaceChildren();
  const parts = path.split("/").filter(Boolean);
  let current = "";
  const crumb = (label, destination) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = label;
    button.addEventListener("click", () => loadFolder(destination));
    nav.append(button);
  };
  crumb("/", "/");
  for (const part of parts) {
    current += "/" + part;
    crumb(part, current);
  }
}
async function loadFolder(path = null) {
  if (running || !folderContext) return;
  busy(true);
  folderState = null;
  folderControls();
  $("folderList").replaceChildren();
  $("folderBreadcrumbs").replaceChildren();
  $("folderFilter").value = "";
  $("folderMessage").textContent = "Reading folders on the VPS...";
  $("folderMessage").className = "helper";
  try {
    folderState = await request("/api/ssh/directories", { server: folderContext.server, role: folderContext.role, path });
    renderFolderBreadcrumbs(folderState.path);
    renderFolderItems();
  } catch (error) {
    $("folderMessage").textContent = `${error.message || error} You can try Server root or Home folder. Check SSH access and folder permissions if browsing fails.`;
    $("folderMessage").className = "status error";
  } finally {
    busy(false);
    folderControls();
  }
}
function openFolderPicker(target, role) {
  if (running) return;
  const server = sshSettings();
  if (!server.host) {
    setStatus("Enter the VPS SSH host first", "error", "Configure and test SSH access before browsing folders.");
    return;
  }
  folderContext = { target, role, server };
  folderState = null;
  $("folderTitle").textContent = target === "profileBackupDir" ? "Choose the backup folder" : "Choose the safety backup folder";
  $("folderServer").textContent = `Folders on ${server.username}@${server.host}:${server.port}`;
  $("folderDialog").showModal();
  loadFolder($(target).value.trim() || null);
}
$("browseBackupDir").addEventListener("click", () => openFolderPicker("profileBackupDir", "SOURCE"));
$("browseSafetyDir").addEventListener("click", () => openFolderPicker("profileSafetyDir", "DEST"));
$("folderRoot").addEventListener("click", () => loadFolder("/"));
$("folderHome").addEventListener("click", () => loadFolder());
$("folderUp").addEventListener("click", () => { if (folderState?.parent) loadFolder(folderState.parent); });
$("folderFilter").addEventListener("input", renderFolderItems);
$("folderSelect").addEventListener("click", () => {
  if (running || !folderContext || !folderState) return;
  $(folderContext.target).value = folderState.path;
  $(folderContext.target).dispatchEvent(new Event("input", { bubbles: true }));
  setStatus("VPS folder selected", "success", `${folderState.path} on ${folderContext.server.host}. Save the profile to keep this selection.`);
  $("folderDialog").close();
});
$("folderCancel").addEventListener("click", () => $("folderDialog").close());
$("folderDialog").addEventListener("cancel", event => { if (running) event.preventDefault(); });
$("folderDialog").addEventListener("close", () => { folderContext = null; folderState = null; });

function clearDestinationInputs() {
  ["profileContainer", "profileDatabase", "profileDbUser"].forEach(id => { $(id).value = ""; });
}
function resetDatabaseDiscovery(clearConnection = true) {
  discoveredDatabases = [];
  databaseDiscoveryFor = "";
  $("discoveredDatabase").replaceChildren(option("", "Find databases first"));
  $("databaseDiscoverySummary").textContent = "Set up SSH access, then find and select a PostgreSQL database on this VPS.";
  $("databaseDiscoveryWarnings").replaceChildren();
  if (clearConnection) clearDestinationInputs();
}
$("discoverDatabases").addEventListener("click", () => perform(async () => {
  const server = sshSettings();
  const databaseUser = $("profileDbUser").value.trim() || null;
  const database = $("profileDatabase").value.trim() || null;
  resetDatabaseDiscovery();
  if (!server.host) throw new Error("Enter the VPS host and configure SSH access first.");
  $("databaseDiscoverySummary").textContent = "Checking running containers and reading PostgreSQL database names...";
  try {
    const result = await request("/api/ssh/databases", { server, database_user: databaseUser, database });
    discoveredDatabases = result.items;
    databaseDiscoveryFor = JSON.stringify(server);
    $("discoveredDatabase").replaceChildren(option("", result.items.length ? "Choose a database" : "No accessible PostgreSQL databases found"),
      ...result.items.map((item, index) => option(String(index), `${item.container} / ${item.database} (role: ${item.database_user})`)));
    for (const warning of result.warnings) {
      const row = document.createElement("li");
      row.textContent = `${warning.container}: ${warning.message}`;
      $("databaseDiscoveryWarnings").append(row);
    }
    $("databaseDiscoverySummary").textContent = `Found ${result.items.length} database(s) across ${result.postgres_containers} PostgreSQL container(s) on ${server.host}. ` +
      (result.items.length ? "Choose the database you intend to restore into." : "Check that PostgreSQL containers are running and that local database authentication permits access. You can also use the advanced fields.");
    setStatus(`Database discovery finished: ${result.items.length} found`, result.items.length ? "success" : "neutral", JSON.stringify(result, null, 2));
  } catch (error) {
    $("databaseDiscoverySummary").textContent = "Database discovery failed. Check SSH/Docker access and try again, or use the advanced fields.";
    throw error;
  }
}, "Finding PostgreSQL databases on the selected VPS..."));
$("discoveredDatabase").addEventListener("change", () => {
  if (databaseDiscoveryFor !== JSON.stringify(sshSettings())) {
    resetDatabaseDiscovery();
    return;
  }
  const item = $("discoveredDatabase").value === "" ? null : discoveredDatabases[Number($("discoveredDatabase").value)];
  clearDestinationInputs();
  if (item) {
    $("profileContainer").value = item.container;
    $("profileDatabase").value = item.database;
    $("profileDbUser").value = item.database_user;
    $("databaseDiscoverySummary").textContent = `Selected ${item.database} in container ${item.container}, using role ${item.database_user}. Save the profile, then test the destination connection.`;
  }
  resetConfirmations();
});
["profileHost", "profilePort", "profileUser", "profileKey", "profileKnownHosts", "profilePassphraseEnv"].forEach(id =>
  $(id).addEventListener("input", () => resetDatabaseDiscovery()));
$("managedCredential").addEventListener("change", () => resetDatabaseDiscovery());
$("profileTarget").addEventListener("change", () => { if (!$("profileTarget").checked) resetDatabaseDiscovery(); });
["profileContainer", "profileDatabase", "profileDbUser"].forEach(id => {
  $(id).addEventListener("input", () => {
    $("discoveredDatabase").value = "";
    $("databaseDiscoverySummary").textContent = "Using manually entered connection details. Save the profile and test the destination before restoring.";
    resetConfirmations();
  });
  $(id).addEventListener("invalid", () => { $("manualDatabaseDetails").open = true; });
});
