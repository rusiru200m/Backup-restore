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
  $("output").textContent = output;
}
const fieldControls = {
  token: "token", name: "profileName", host: "profileHost", port: "profilePort", username: "profileUser",
  credential_id: "managedCredential", key_path: "profileKey", known_hosts_path: "profileKnownHosts", passphrase_env: "profilePassphraseEnv",
  backup_directory: "profileBackupDir", container: "profileContainer", database: "profileDatabase", database_user: "profileDbUser",
  safety_backup_directory: "profileSafetyDir", fingerprint: "verifiedFingerprint", password: "bootstrapPassword",
  source_profile_id: "sourceProfile", destination_profile_id: "destProfile", backup_filename: "backupName",
  confirmation: "confirmation", writes_paused: "writesPaused", agree_replace: "agreeReplace", role: "profileStorage",
};
const controlLabels = {
  token: "App token", profileName: "Profile name", profileHost: "SSH host", profilePort: "SSH port", profileUser: "SSH username",
  managedCredential: "SSH key", profileKey: "Private key path", profileKnownHosts: "Known hosts file", profilePassphraseEnv: "Passphrase environment variable",
  profileBackupDir: "Backup folder", profileContainer: "Docker container", profileDatabase: "Database", profileDbUser: "PostgreSQL role",
  profileSafetyDir: "Safety backup folder", verifiedFingerprint: "Verified fingerprint", bootstrapPassword: "VPS SSH password",
  sourceProfile: "Backup storage server", destProfile: "Destination server", backupName: "Backup archive", confirmation: "Database name confirmation",
  writesPaused: "Pause database writes", agreeReplace: "Confirm data replacement", profileStorage: "Server roles", profileTarget: "Restore destination role",
};
class OperationError extends Error {
  constructor(message, { code = "OPERATION_FAILED", hint = "Check the settings for this operation and try again.", fields = [], requestId = "", stage = "", safetyBackup = "", restoreCompleted = false } = {}) {
    super(message); this.code = code; this.hint = hint; this.fields = fields; this.requestId = requestId;
    this.stage = stage; this.safetyBackup = safetyBackup; this.restoreCompleted = restoreCompleted;
  }
}
function fieldId(field) {
  if ($(field)) return field;
  return fieldControls[String(field).split(".").pop()] || null;
}
function clearFieldError(id) {
  const control = $(id);
  if (!control) return;
  control.removeAttribute("aria-invalid");
  const descriptionId = id + "Error";
  const descriptions = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(value => value && value !== descriptionId);
  if (descriptions.length) control.setAttribute("aria-describedby", descriptions.join(" "));
  else control.removeAttribute("aria-describedby");
  $(descriptionId)?.remove();
}
function clearErrors() {
  document.querySelectorAll('[aria-invalid="true"]').forEach(control => clearFieldError(control.id));
  $("errorSummary").hidden = true;
  $("errorFields").replaceChildren();
}
function focusField(id) {
  const control = $(id);
  if (!control) return;
  if ($("folderDialog").open && !$("folderDialog").contains(control)) $("folderDialog").close();
  for (let parent = control.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === "DETAILS") parent.open = true;
  }
  control.focus();
  control.scrollIntoView({ block: "center", behavior: "smooth" });
}
function errorText(error) {
  return error instanceof Error ? error.message : "An unexpected error occurred.";
}
function showError(error, title, context = "") {
  const message = errorText(error);
  const hint = (error.restoreCompleted ? "Do not repeat the restore. " : "") + (error.hint || "Check the settings and controller terminal log before running the operation again.");
  const operationContext = [context, error.stage && `Stage: ${error.stage}`, error.safetyBackup && `Verified safety backup on destination: ${error.safetyBackup}`, error.restoreCompleted && "Restore completed: yes. Cleanup needs attention; keep application writes paused until verified."].filter(Boolean).join("\n");
  const references = [error.code && `Code: ${error.code}`, error.requestId && `Request ID: ${error.requestId}`].filter(Boolean).join(" · ");
  $("errorTitle").textContent = error.restoreCompleted ? "Restore completed; cleanup needs attention" : title.replace(/\.{3}$/, "") + " — failed";
  $("errorMessage").textContent = message;
  $("errorHint").textContent = `Next step: ${hint}`;
  $("errorContext").textContent = operationContext;
  $("errorReference").textContent = references;
  $("errorSummary").dataset.fieldErrors = String((error.fields || []).length > 0);
  $("errorFields").replaceChildren();
  for (const issue of error.fields || []) {
    const id = fieldId(issue.field);
    const row = document.createElement("li");
    row.dataset.fieldId = id || "";
    const label = id ? controlLabels[id] || issue.field : issue.field || "Settings";
    if (id) {
      const link = document.createElement("a");
      link.href = "#" + id;
      link.textContent = `${label}: ${issue.message}`;
      link.addEventListener("click", event => { event.preventDefault(); focusField(id); });
      row.append(link);
      const control = $(id);
      for (let parent = control.parentElement; parent; parent = parent.parentElement) {
        if (parent.tagName === "DETAILS") parent.open = true;
      }
      clearFieldError(id);
      control.setAttribute("aria-invalid", "true");
      const inline = document.createElement("span");
      inline.id = id + "Error";
      inline.className = "field-error";
      inline.textContent = issue.message;
      const descriptions = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      control.setAttribute("aria-describedby", [...descriptions, inline.id].join(" "));
      (control.closest(".folder-input") || control.closest("label") || control).after(inline);
    } else row.textContent = `${label}: ${issue.message}`;
    $("errorFields").append(row);
  }
  $("errorSummary").hidden = false;
  setStatus((error.restoreCompleted ? "Restore completed; cleanup needs attention: " : "Operation failed: ") + message, "error", [operationContext, message, `Next step: ${hint}`, references].filter(Boolean).join("\n"));
  if (!$("folderDialog").open) $("errorSummary").focus();
}
function invalid(field, message) { return { field, message }; }
function requireValid(issues) {
  if (issues.length) throw new OperationError("Some settings need attention before this operation can run.", {
    code: "VALIDATION_ERROR", hint: "Select a field below to correct it, then run the operation again.", fields: issues,
  });
}
function tokenIssues() {
  return $("token").value.trim() ? [] : [invalid("token", "Enter APP_TOKEN from the controller's .env file.")];
}
function validHost(host) {
  if (host.includes(":")) {
    if (!/^[A-Fa-f0-9:.]+$/.test(host)) return false;
    try { return new URL(`http://[${host}]/`).hostname.startsWith("["); } catch { return false; }
  }
  if (!host || host.length > 253) return false;
  if (/^[0-9.]+$/.test(host) && host.includes(".")) return host.split(".").length === 4 && host.split(".").every(part => /^(0|[1-9][0-9]{0,2})$/.test(part) && Number(part) <= 255);
  return host.replace(/\.$/, "").split(".").every(label => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(label));
}
const hasControlCharacters = value => /[\x00-\x1f\x7f]/.test(value);
function sshIssues() {
  const server = sshSettings();
  const issues = [];
  if (!validHost(server.host)) issues.push(invalid("host", "Enter a valid hostname, IPv4, or IPv6 address without ssh://, spaces, or a username. Enter the port in its separate field."));
  if (!$("profilePort").value.trim() || !Number.isInteger(server.port) || server.port < 1 || server.port > 65535) issues.push(invalid("port", "Enter a whole-number SSH port from 1 to 65535."));
  if (!/^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/.test(server.username)) issues.push(invalid("username", "Enter an SSH username starting with a letter or underscore; use letters, numbers, dots, underscores, or hyphens (maximum 64 characters)."));
  if (server.credential_id && !credentials.some(item => item.id === server.credential_id)) issues.push(invalid("credential_id", "Connect to load saved keys, then select an available SSH key."));
  if (!server.credential_id) {
    for (const [field, id] of [["key_path", "profileKey"], ["known_hosts_path", "profileKnownHosts"]]) {
      if ($(id).value.length > 1024 || hasControlCharacters($(id).value)) issues.push(invalid(field, "Use a controller file path of at most 1024 characters without control characters, or leave blank for controller defaults."));
    }
    if (server.passphrase_env && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(server.passphrase_env)) issues.push(invalid("passphrase_env", "Use an environment variable name such as PRODUCTION_SSH_PASSPHRASE, or leave blank."));
  }
  return issues;
}
function directoryIssues(id, optional = false) {
  const path = $(id).value.trim();
  return optional && !path || path.startsWith("/") && path.length <= 1024 && !hasControlCharacters(path) && !path.split("/").includes("..") ? []
    : [invalid(id, "Use an absolute Linux folder, such as /var/backups, without .. or control characters (maximum 1024 characters).")];
}
function databaseIssues(optional = false, ids = ["profileContainer", "profileDatabase", "profileDbUser"]) {
  return ids.flatMap(id => {
    const value = $(id).value.trim();
    return optional && !value || /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(value) ? []
      : [invalid(id, "Use 1–128 letters, numbers, dots, underscores, or hyphens, starting with a letter, number, or underscore.")];
  });
}
function profileIssues() {
  const issues = sshIssues();
  if (!$("profileName").value.trim() || $("profileName").value.trim().length > 120 || hasControlCharacters($("profileName").value)) issues.unshift(invalid("name", "Enter a profile name of 1–120 characters without control characters."));
  if (!$("profileStorage").checked && !$("profileTarget").checked) issues.push(invalid("role", "Select backup storage, restore destination, or both roles."));
  if ($("profileStorage").checked) issues.push(...directoryIssues("profileBackupDir"));
  if ($("profileTarget").checked) issues.push(...databaseIssues(), ...directoryIssues("profileSafetyDir", true));
  // Keep native constraints in this summary too, including inputs inside closed details.
  for (const control of $("profileForm").querySelectorAll("input,select")) {
    if (control.willValidate && !control.validity.valid && !issues.some(issue => fieldId(issue.field) === control.id)) issues.push(invalid(control.id, control.validationMessage));
  }
  return issues;
}
function selectionIssues(id, message) { return selected(id) ? [] : [invalid(id, message)]; }
function restoreIssues() {
  const source = selected("sourceProfile");
  const dest = selected("destProfile");
  const issues = [...selectionIssues("sourceProfile", "Select a saved backup storage server."), ...selectionIssues("destProfile", "Select a saved destination database.")];
  if (!$("backupName").value || !source || backupsFor !== source.id) issues.push(invalid("backupName", "Load backups for the selected storage server, then choose an archive."));
  if (!$("writesPaused").checked) issues.push(invalid("writes_paused", "Stop applications and jobs that write to the destination, then check this confirmation."));
  if (!$("agreeReplace").checked) issues.push(invalid("agree_replace", "Confirm that replacing the destination data is intended."));
  if (!dest || $("confirmation").value !== dest.destination.database) issues.push(invalid("confirmation", "Type the exact destination database name shown above."));
  return issues;
}
function sshContext() { const server = sshSettings(); return `Server: ${server.username}@${server.host || "(not set)"}:${server.port || "(not set)"}`; }
function savedContext(...ids) {
  return ids.map(id => { const profile = selected(id); return profile ? `${profile.name}: ${profile.ssh.username}@${profile.ssh.host}:${profile.ssh.port}` : `${controlLabels[id]}: not selected`; }).join(" · ");
}
function busy(on) {
  running = on;
  document.querySelectorAll("input, select, button").forEach(el => { el.disabled = on; });
  if (!on) $("deleteProfile").disabled = !$("profileEditor").value;
}
async function request(url, data, method = "POST") {
  const token = $("token").value.trim();
  requireValid(tokenIssues());
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json", "X-Admin-Token": token },
      ...(data === undefined ? {} : { body: JSON.stringify(data) }),
      cache: "no-store",
    });
  } catch {
    throw new OperationError("The browser could not reach the controller API.", {
      code: "API_UNREACHABLE", hint: "Check that the project server is running at this page's address and that your connection is available. For a restore, check the controller log and database state before deciding whether to run it again.",
    });
  }
  let result = null;
  try { result = await response.json(); } catch { /* Never display an HTML error page or raw response as trusted output. */ }
  const requestId = response.headers.get("X-Request-ID") || "";
  if (!response.ok) {
    const details = result && typeof result === "object" ? result : {};
    const structured = details.error && typeof details.error === "object" ? details.error : {};
    const fields = Array.isArray(structured.fields) ? structured.fields.filter(issue => issue && typeof issue.field === "string" && typeof issue.message === "string")
      : Array.isArray(details.detail) ? details.detail.filter(issue => issue && typeof issue === "object").map(issue => invalid((Array.isArray(issue.loc) ? issue.loc : []).filter(part => part !== "body").join("."), typeof issue.msg === "string" ? issue.msg : "Invalid value")) : [];
    const unauthorized = response.status === 401 || response.status === 403;
    throw new OperationError(typeof structured.message === "string" ? structured.message : typeof details.detail === "string" ? details.detail : `The controller returned HTTP ${response.status}.`, {
      code: typeof structured.code === "string" ? structured.code : unauthorized ? "INVALID_APP_TOKEN" : "HTTP_ERROR",
      hint: typeof structured.hint === "string" ? structured.hint : unauthorized ? "Paste the APP_TOKEN configured in the running controller's .env file, then connect again." : "Check the controller terminal log. If a proxy is in use, verify that /api requests reach this project server.",
      fields: unauthorized && !fields.length ? [invalid("token", "Use APP_TOKEN from the running controller's .env file.")] : fields,
      requestId: typeof structured.request_id === "string" ? structured.request_id : requestId,
      stage: typeof structured.stage === "string" ? structured.stage : "",
      safetyBackup: typeof structured.safety_backup_on_destination === "string" ? structured.safety_backup_on_destination : "",
      restoreCompleted: structured.restore_completed === true,
    });
  }
  if (!validResponse(url, method, result)) throw new OperationError("The controller returned an unexpected response, so this operation could not be verified.", {
    code: "INVALID_API_RESPONSE", requestId,
    hint: "Check the controller log and ensure this page and API use the same project version. For a restore, inspect the database and job log before deciding whether to run it again.",
  });
  return result;
}
function validResponse(url, method, result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return false;
  const text = value => typeof value === "string" && value.length > 0;
  const destination = value => value && text(value.container) && text(value.database) && text(value.database_user);
  const credential = value => value && text(value.id) && text(value.name) && text(value.public_key) && text(value.fingerprint);
  if (url === "/api/profiles" && method === "GET") return Array.isArray(result.items) && result.items.every(item => item && text(item.id) && text(item.revision) && text(item.name) && item.ssh && text(item.ssh.host) && text(item.ssh.username) && Number.isInteger(item.ssh.port) && (item.backup_directory === null || text(item.backup_directory)) && (item.destination === null || destination(item.destination)));
  if (url.startsWith("/api/profiles")) return method === "DELETE" ? result.ok === true : text(result.id);
  if (url === "/api/ssh/credentials") return method === "GET" ? Array.isArray(result.items) && result.items.every(credential) : credential(result);
  if (url === "/api/ssh/scan" || url === "/api/ssh/trust") return text(result.host) && Number.isInteger(result.port) && text(result.fingerprint) && (url.endsWith("scan") ? text(result.key_type) : result.ok === true);
  if (url === "/api/ssh/authorize" || url === "/api/ssh/test") return result.ok === true && text(result.message);
  if (url === "/api/ssh/directories") return text(result.path) && (result.parent === null || text(result.parent)) && Array.isArray(result.items) && result.items.every(item => item && text(item.name) && text(item.path) && typeof item.is_directory === "boolean");
  if (url === "/api/ssh/databases") return Array.isArray(result.items) && result.items.every(destination) && Number.isInteger(result.postgres_containers) && Array.isArray(result.warnings) && result.warnings.every(item => item && typeof item.container === "string" && typeof item.message === "string");
  if (url === "/api/profile-backups") return Array.isArray(result.items) && result.items.every(item => item && text(item.filename) && Number.isFinite(item.bytes) && item.bytes >= 0);
  if (url === "/api/profile-preflight") return result.ok === true && text(result.database) && text(result.message);
  if (url === "/api/profile-restore") return result.ok === true && text(result.job_id) && text(result.safety_backup_on_destination);
  return false;
}
async function perform(action, title, validate = () => [], context = "") {
  if (running) return;
  clearErrors();
  setStatus(title, "working");
  try {
    requireValid([...tokenIssues(), ...validate()]);
    busy(true);
    await action();
  }
  catch (error) { showError(error, title, typeof context === "function" ? context() : context); }
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
  clearErrors();
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
  }, "Saving server profile...", profileIssues, sshContext);
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
  const result = await request("/api/profile-backups", { source_profile_id: p.id });
  $("backupName").replaceChildren(option("", result.items.length ? "Choose an archive" : "No .dump files found"), ...result.items.map(item => option(item.filename, `${item.filename} · ${(item.bytes / 1024 / 1024).toFixed(2)} MB`)));
  backupsFor = p.id;
  summaries();
  setStatus(`Loaded ${result.items.length} backup(s) from ${p.name}`, "success", JSON.stringify(result.items, null, 2));
}, "Reading backup storage over SSH...", () => selectionIssues("sourceProfile", "Select a saved backup storage server first."), () => savedContext("sourceProfile")));
$("checkDestination").addEventListener("click", () => perform(async () => {
  const p = selected("destProfile");
  const result = await request("/api/profile-preflight", { destination_profile_id: p.id });
  setStatus(`Destination ${p.name} is reachable`, "success", JSON.stringify(result, null, 2));
}, "Checking destination SSH, Docker and PostgreSQL...", () => selectionIssues("destProfile", "Select a saved destination database first."), () => savedContext("destProfile")));
$("restore").addEventListener("click", () => perform(async () => {
  const source = selected("sourceProfile");
  const dest = selected("destProfile");
  const data = { source_profile_id: source.id, destination_profile_id: dest.id,
    source_profile_revision: source.revision, destination_profile_revision: dest.revision,
    backup_filename: $("backupName").value, confirmation: $("confirmation").value,
    writes_paused: $("writesPaused").checked, agree_replace: $("agreeReplace").checked };
  if (!window.confirm(`Replace backed-up objects in ${dest.destination.database} on ${dest.ssh.host}:${dest.ssh.port} (container ${dest.destination.container}) using ${data.backup_filename} from ${source.ssh.host}? A safety dump will be created first.`)) {
    setStatus("Restore cancelled", "neutral");
    return;
  }
  try {
    const result = await request("/api/profile-restore", data);
    setStatus("Restore completed. Verify database records before resuming writes.", "success", JSON.stringify(result, null, 2));
  } finally { resetConfirmations(); }
}, "Creating safety backup and restoring. Keep the app running...", restoreIssues, () => savedContext("sourceProfile", "destProfile")));
roleFields();

$("managedCredential").addEventListener("change", () => { credentialFields(); $("bootstrapPassword").value = ""; });
["profileHost", "profilePort", "profileUser"].forEach(id => $(id).addEventListener("input", resetEnrollment));
$("generateKey").addEventListener("click", () => perform(async () => {
  const name = $("profileName").value.trim() || $("profileHost").value.trim();
  const result = await request("/api/ssh/credentials", { name });
  await refreshCredentials(result.id);
  setStatus("SSH key generated and selected", "success", "Next: verify the VPS fingerprint, authorize this public key, test SSH access, and save the profile. The private key stays encrypted on the controller.");
}, "Generating and encrypting an SSH key...", () => {
  const name = $("profileName").value.trim() || $("profileHost").value.trim();
  return name && name.length <= 120 && !hasControlCharacters(name) ? [] : [invalid("name", "Enter a profile/key name of 1–120 characters without control characters before generating a key. Host details are optional for this step.")];
}));
$("scanHost").addEventListener("click", () => perform(async () => {
  const result = await request("/api/ssh/scan", sshSettings());
  $("hostFingerprint").textContent = `${result.host}:${result.port} · ${result.key_type} · ${result.fingerprint} (not yet verified)`;
  $("verifiedFingerprint").value = "";
  setStatus("Server fingerprint read. Verify it before trusting.", "neutral", JSON.stringify(result, null, 2));
}, "Reading the server SSH identity without sending credentials...", sshIssues, sshContext));
$("trustHost").addEventListener("click", () => perform(async () => {
  const result = await request("/api/ssh/trust", { server: sshSettings(), fingerprint: $("verifiedFingerprint").value.trim() });
  $("hostFingerprint").textContent = `Verified and saved: ${result.host}:${result.port} · ${result.fingerprint}`;
  setStatus("Verified server identity saved", "success", "The app will check this identity on every managed-key SSH connection. You can now authorize the generated public key.");
}, "Checking and saving the verified server identity...", () => [...sshIssues(), ...(/^SHA256:[A-Za-z0-9+/]{43}$/.test($("verifiedFingerprint").value.trim()) ? [] : [invalid("fingerprint", "Paste the complete SHA256 fingerprint verified through your VPS console or another trusted SSH connection.")])], sshContext));
$("authorizeKey").addEventListener("click", () => {
  const password = $("bootstrapPassword").value;
  $("bootstrapPassword").value = "";
  perform(async () => {
    const result = await request("/api/ssh/authorize", { server: sshSettings(), password });
    setStatus("Public key installed", "success", result.message);
  }, "Authorizing the public key with a one-time SSH password login...", () => [
    ...sshIssues(), ...($("managedCredential").value ? [] : [invalid("credential_id", "Generate or select an SSH key first.")]),
    ...(password ? [] : [invalid("password", "Enter this VPS account's current SSH password, or install the public key through the VPS console.")]),
  ], sshContext);
});
$("testSsh").addEventListener("click", () => perform(async () => {
  const result = await request("/api/ssh/test", sshSettings());
  setStatus("SSH key access verified", "success", `${result.message}. Save the profile to use these settings for backups and restores.`);
}, "Testing SSH key login and SFTP...", sshIssues, sshContext));
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
  clearErrors();
  const context = `${sshContext()} · Folder: ${path || "home"}`;
  folderState = null;
  $("folderList").replaceChildren();
  $("folderBreadcrumbs").replaceChildren();
  $("folderFilter").value = "";
  $("folderMessage").textContent = "Reading folders on the VPS...";
  $("folderMessage").className = "helper";
  setStatus("Browsing VPS folders...", "working", context);
  try {
    requireValid([...tokenIssues(), ...sshIssues(), ...(path !== null && (!path.startsWith("/") || path.length > 1024 || hasControlCharacters(path) || path.split("/").includes("..")) ? [invalid(folderContext.target, "Use an absolute Linux folder without .. or control characters, or choose Home folder / Server root.")] : [])]);
    busy(true);
    folderControls();
    folderState = await request("/api/ssh/directories", { server: folderContext.server, role: folderContext.role, path });
    renderFolderBreadcrumbs(folderState.path);
    renderFolderItems();
    setStatus("VPS folders loaded", "success", `${context}\nCurrent folder: ${folderState.path}`);
  } catch (error) {
    showError(error, "Browsing VPS folders...", context);
    $("folderMessage").textContent = [context, errorText(error), `Next step: ${error.hint || "Check SSH access and folder permissions, or try Server root / Home folder."}`, error.code && `Code: ${error.code}`, error.requestId && `Request ID: ${error.requestId}`].filter(Boolean).join("\n");
    $("folderMessage").className = "status error";
    $("folderMessage").focus();
  } finally {
    busy(false);
    folderControls();
  }
}
function openFolderPicker(target, role) {
  if (running) return;
  clearErrors();
  const server = sshSettings();
  try { requireValid([...tokenIssues(), ...sshIssues()]); }
  catch (error) {
    showError(error, "Browsing VPS folders...", sshContext());
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
$("folderDialog").addEventListener("close", () => {
  folderContext = null; folderState = null;
  if (!$("errorSummary").hidden) $("errorSummary").focus();
});

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
  resetDatabaseDiscovery(false);
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
    $("databaseDiscoverySummary").textContent = [`Database discovery failed on ${server.host}:${server.port}.`, errorText(error), `Next step: ${error.hint || "Check SSH/Docker access and database authentication, or use the advanced fields."}`, error.requestId && `Request ID: ${error.requestId}`].filter(Boolean).join(" ");
    throw error;
  }
}, "Finding PostgreSQL databases on the selected VPS...", () => [...sshIssues(), ...databaseIssues(true, ["profileDatabase", "profileDbUser"])], sshContext));
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
document.querySelectorAll("input,select").forEach(control => {
  const clearEditedError = () => {
    if (["profileStorage", "profileTarget", "managedCredential", "sourceProfile", "destProfile", "backupName"].includes(control.id)) {
      if (!$("errorSummary").hidden) {
        clearErrors();
        setStatus("Settings changed. Run the operation again to validate them.", "neutral");
      }
      return;
    }
    clearFieldError(control.id);
    $("errorFields").querySelectorAll("li").forEach(row => { if (row.dataset.fieldId === control.id) row.remove(); });
    if ($("errorSummary").dataset.fieldErrors === "true" && !$("errorFields").children.length && !$("errorSummary").hidden) {
      $("errorSummary").hidden = true;
      setStatus("Settings changed. Run the operation again to validate them.", "neutral");
    }
  };
  control.addEventListener("input", clearEditedError);
  control.addEventListener("change", clearEditedError);
});
