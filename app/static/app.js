"use strict";

const $ = id => document.getElementById(id);
let profiles = [];
let running = false;
let backupsFor = "";
const selected = id => profiles.find(p => p.id === $(id).value);

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
function editProfile() {
  const p = selected("profileEditor");
  $("profileForm").reset();
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
}
async function refreshProfiles(editorId = $("profileEditor").value) {
  const sourceId = $("sourceProfile").value;
  const destId = $("destProfile").value;
  profiles = (await request("/api/profiles", undefined, "GET")).items;
  fillSelect("profileEditor", profiles, "New server profile", editorId);
  fillSelect("sourceProfile", profiles.filter(p => p.backup_directory !== null), "Select backup storage", sourceId);
  fillSelect("destProfile", profiles.filter(p => p.destination !== null), "Select destination database", destId);
  invalidateBackups();
  editProfile();
  summaries();
}

$("loadProfiles").addEventListener("click", () => perform(async () => {
  await refreshProfiles();
  setStatus(`Loaded ${profiles.length} saved profile(s)`, "success", profiles.length ? "Choose or edit a profile, then select backup storage and a destination below." : "Create your first backup storage or destination profile above.");
}, "Loading saved servers..."));
$("profileEditor").addEventListener("change", editProfile);
$("newProfile").addEventListener("click", () => { $("profileEditor").value = ""; editProfile(); });
$("profileStorage").addEventListener("change", roleFields);
$("profileTarget").addEventListener("change", roleFields);
$("profileForm").addEventListener("submit", event => {
  event.preventDefault();
  const id = $("profileEditor").value;
  const nullable = field => $(field).value.trim() || null;
  const data = {
    name: $("profileName").value.trim(),
    ssh: { host: $("profileHost").value.trim(), port: Number($("profilePort").value), username: $("profileUser").value.trim(),
      key_path: nullable("profileKey"), known_hosts_path: nullable("profileKnownHosts"), passphrase_env: nullable("profilePassphraseEnv") },
    backup_directory: $("profileStorage").checked ? $("profileBackupDir").value.trim() : null,
    destination: $("profileTarget").checked ? { container: $("profileContainer").value.trim(), database: $("profileDatabase").value.trim(),
      database_user: $("profileDbUser").value.trim(), safety_backup_directory: nullable("profileSafetyDir") } : null,
  };
  perform(async () => {
    const result = await request(id ? `/api/profiles/${encodeURIComponent(id)}` : "/api/profiles", data, id ? "PUT" : "POST");
    await refreshProfiles(result.id);
    setStatus("Server profile saved", "success", "The profile will be available after restarting the app. Reload backups after configuration changes.");
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
$("sourceProfile").addEventListener("change", () => { invalidateBackups(); summaries(); });
$("destProfile").addEventListener("change", () => { resetConfirmations(); summaries(); });
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
