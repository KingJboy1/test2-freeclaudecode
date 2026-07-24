/* ═══════════════════════════════════════════════════════
   FREE CLAUDE CODE — Admin UI Controller
   ═══════════════════════════════════════════════════════ */
const state = {
  config: null,
  fields: new Map(),
  localStatus: new Map(),
  modelOptions: [],
  modelComboboxes: new Set(),
  activeView: "providers",
};

const MASKED_SECRET = "********";

/** Escape HTML special characters to prevent XSS via innerHTML. */
function esc(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}


const VIEW_GROUPS = [
  {
    id: "providers",
    label: "Providers",
    title: "Providers",
    sections: ["providers", "runtime"],
    containerId: "providersSections",
  },
  {
    id: "model_config",
    label: "Model Config",
    title: "Model Config",
    sections: ["models", "reasoning", "web_tools"],
    containerId: "modelConfigSections",
  },
  {
    id: "messaging",
    label: "Messaging",
    title: "Messaging",
    sections: ["messaging", "voice"],
    containerId: "messagingSections",
  },
  {
    id: "models",
    label: "My Models",
    title: "My Models",
    sections: ["user_models"],
    containerId: "userModelsSections",
  },
  {
    id: "advanced",
    label: "Advanced",
    title: "Advanced",
    sections: ["diagnostics", "smoke"],
    containerId: "advancedSections",
  },
];

const byId = (id) => document.getElementById(id);

/* ── Icons ── */
const ICONS = {
  providers: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><path d="M6 6h.01M6 18h.01"/></svg>',
  model_config: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" x2="4" y1="21" y2="14"/><line x1="4" x2="4" y1="10" y2="3"/><line x1="12" x2="12" y1="21" y2="12"/><line x1="12" x2="12" y1="8" y2="3"/><line x1="20" x2="20" y1="21" y2="16"/><line x1="20" x2="20" y1="12" y2="3"/><line x1="2" x2="6" y1="14" y2="14"/><line x1="10" x2="14" y1="8" y2="8"/><line x1="18" x2="22" y1="16" y2="16"/></svg>',
  messaging: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  models: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>',
};

/* ═══════════════════════════════════════════════════════
   TOAST SYSTEM
   ═══════════════════════════════════════════════════════ */
const toast = {
  _container: null,
  _getContainer() {
    if (!this._container) this._container = byId("toastContainer");
    return this._container;
  },
  show(message, kind = "info", duration = 4000) {
    const container = this._getContainer();
    if (!container) return;
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.textContent = message;
    container.appendChild(el);
    if (duration > 0) {
      setTimeout(() => {
        el.classList.add("toast-out");
        setTimeout(() => el.remove(), 200);
      }, duration);
    }
    return el;
  },
  success(msg, dur) { return this.show(msg, "success", dur); },
  error(msg, dur) { return this.show(msg, "error", dur || 6000); },
  info(msg, dur) { return this.show(msg, "info", dur); },
  dismiss(el) {
    el.classList.add("toast-out");
    setTimeout(() => el.remove(), 200);
  },
};

/* ═══════════════════════════════════════════════════════
   HELPERS
   ═══════════════════════════════════════════════════════ */

function sourceLabel(source) {
  const labels = {
    default: "default",
    template: "template",
    repo_env: "repo .env",
    managed_env: "",
    explicit_env_file: "FCC_ENV_FILE",
    process: "process env",
  };
  return Object.prototype.hasOwnProperty.call(labels, source) ? labels[source] : source;
}

function sourceText(field) {
  const parts = [];
  const label = sourceLabel(field.source);
  if (label) parts.push(label);
  if (field.locked) parts.push("locked");
  return parts.join(" ");
}

function statusClass(status) {
  if (["configured", "reachable", "running"].includes(status)) return "ok";
  if (["missing_key", "missing_config", "missing_url", "unknown"].includes(status)) return "warn";
  if (["offline", "error"].includes(status)) return "error";
  return "neutral";
}

function statusLabel(status) {
  const labels = {
    configured: "Ready",
    reachable: "Online",
    running: "Active",
    missing_key: "No Key",
    missing_config: "Missing Config",
    missing_url: "No URL",
    unknown: "Unknown",
    offline: "Offline",
    error: "Error",
  };
  return labels[status] || status;
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/* ═══════════════════════════════════════════════════════
   API
   ═══════════════════════════════════════════════════════ */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    cache: "no-store",
  });
  if (!response.ok) {
    let detail;
    try { const body = await response.json(); detail = body.detail || body.message; }
    catch { detail = response.statusText; }
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function load() {
  const data = await api("/admin/api/config");
  state.config = data;
  state.fields.clear();
  state.modelOptions = data.model_options || [];
  flattenFields(data.fields);

  renderNav();
  renderProviders();
  VIEW_GROUPS.forEach((view) => renderSections(view));
  void refreshLocalStatus();

  if (data.config_path) byId("configPath").textContent = data.config_path;
  const footer = document.querySelector(".sidebar-footer span:last-child");
  if (footer) footer.textContent = data.config_path ? "Config loaded" : "No config file";
  updateProviderCount();
}

function flattenFields(fields) {
  if (!fields) return;
  fields.forEach((field) => state.fields.set(field.key, field));
}

async function validate(verbose = true) {
  try {
    const data = await api("/admin/api/config/validate", {
      method: "POST",
      body: JSON.stringify({ values: changedValues() }),
    });
    if (verbose) {
      if (data.valid) toast.success("Config shape is valid");
      else toast.error((data.errors || []).join("; "));
    }
    return data;
  } catch (err) {
    toast.error(err.message);
    throw err;
  }
}

async function apply() {
  try {
    const result = await api("/admin/api/config/apply", {
      method: "POST",
      body: JSON.stringify({ values: changedValues() }),
    });
    if (!result.applied) {
      const errs = result.errors || [];
      toast.error(errs.length ? errs.join("; ") : "Validation failed — config not applied");
      return;
    }
    const restart = result.restart || {};
    if (restart.required && restart.automatic) {
      toast.success("Applied. Restarting server...");
      byId("applyButton").disabled = true;
      setTimeout(() => { window.location.href = restart.admin_url || "/admin"; }, 1600);
      return;
    }
    toast.success(restart.required ? "Applied — restart required to take effect" : "Configuration applied");
    state.fields.forEach((f) => { f.dirty = undefined; });
    updateDirtyState();
    await load();
  } catch (err) {
    toast.error(err.message);
  }
}

async function testProvider(providerId) {
  const card = document.querySelector(`[data-provider="${providerId}"]`);
  const button = card ? card.querySelector(".test-button") : null;
  if (button) { button.disabled = true; button.textContent = "Testing..."; }
  const setMeta = (text) => {
    const meta = card ? card.querySelector(".provider-meta") : null;
    if (meta) meta.textContent = text;
  };
  try {
    const result = await api(`/admin/api/providers/${providerId}/test`, {
      method: "POST",
      body: "{}",
    });
    if (result.ok) {
      const models = result.models || [];
      updateProviderStatus(providerId, "reachable");
      setMeta(`${models.length} models — ${models.slice(0, 3).join(", ") || "no models listed"}`);
      if (models.length) setModelOptions([...state.modelOptions, ...models]);
      toast.success(`${providerId}: reachable (${models.length} models)`);
    } else {
      updateProviderStatus(providerId, "offline");
      setMeta(result.error || "Test failed");
      toast.error(`${providerId}: ${result.error || "test failed"}`);
    }
  } catch (err) {
    updateProviderStatus(providerId, "offline");
    setMeta(err.message);
    toast.error(`${providerId}: ${err.message}`);
  } finally {
    if (button) { button.disabled = false; button.textContent = "Test"; }
  }
}

async function refreshLocalStatus() {
  // Best-effort reachability check for local providers (lmstudio/llamacpp/ollama).
  try {
    const result = await api("/admin/api/providers/local-status");
    (result.providers || []).forEach((provider) => {
      state.localStatus.set(provider.provider_id, provider);
      const card = document.querySelector(`[data-provider="${provider.provider_id}"]`);
      if (!card) return;
      updateProviderStatus(provider.provider_id, provider.status);
      const meta = card.querySelector(".provider-meta");
      if (meta) {
        meta.textContent = provider.status_code
          ? `${provider.base_url} — HTTP ${provider.status_code}`
          : (provider.base_url || provider.label || "");
      }
    });
  } catch (err) {
    // non-fatal: local provider checks are best-effort
  }
}

async function refreshModelOptions(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Refreshing...";
  try {
    const result = await api("/admin/api/models/refresh", { method: "POST" });
    await loadModelOptions(true);
    const failed = result.failed_providers || [];
    if (failed.length) {
      const labels = failed.map((p) => p.provider_id || p).join(", ");
      toast.info(`${state.modelOptions.length} models; ${labels} failed`);
    } else {
      toast.success(`${state.modelOptions.length} models available`);
    }
  } catch (err) {
    toast.error(`Could not refresh models: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function loadModelOptions(refresh = false) {
  const result = await api("/admin/api/models" + (refresh ? "/refresh" : ""), {
    method: refresh ? "POST" : "GET",
  });
  setModelOptions(result.models);
  return result;
}

function setModelOptions(models) {
  state.modelOptions = Array.from(
    new Set(models.filter((m) => typeof m === "string" && m.trim())),
  ).sort((a, b) => a.localeCompare(b));
  state.modelComboboxes.forEach((cb) => { if (cb.isOpen) cb.render(cb.query); });
}

/* ═══════════════════════════════════════════════════════
   RENDER: NAV
   ═══════════════════════════════════════════════════════ */

function renderNav() {
  const nav = byId("sectionNav");
  nav.innerHTML = VIEW_GROUPS.map(
    (v) => `<button class="nav-link${v.id === state.activeView ? " active" : ""}" data-view="${v.id}" aria-current="${v.id === state.activeView ? "page" : "false"}">${ICONS[v.id] || ""}<span>${esc(v.label)}</span></button>`
  ).join("");

}

function setActiveView(viewId) {
  state.activeView = viewId;
  document.querySelectorAll(".nav-link").forEach((btn) => {
    const a = btn.dataset.view === viewId;
    btn.classList.toggle("active", a);
    btn.setAttribute("aria-current", a ? "page" : "false");
  });
  document.querySelectorAll(".admin-view").forEach((v) => {
    v.classList.toggle("active", v.dataset.view === viewId);
    v.hidden = v.dataset.view !== viewId;
  });
  const group = VIEW_GROUPS.find((g) => g.id === viewId);
  if (group) {
    byId("pageTitle").textContent = group.title;
    byId("viewBadge").textContent = viewId.replace(/_/g, " ");
  }
  const sw = byId("searchWrapper");
  if (sw) sw.style.display = viewId === "providers" ? "" : "none";
}

/* ═══════════════════════════════════════════════════════
   RENDER: PROVIDERS
   ═══════════════════════════════════════════════════════ */

function renderProviders() {
  const grid = byId("providerGrid");
  const providers = state.config.provider_status || [];
  grid.innerHTML = providers.map(
    (p) => {
      const sc = statusClass(p.status);
      return `<article class="provider-card" data-provider="${p.provider_id}" data-status="${sc}" data-name="${esc(p.display_name || p.provider_id)}">
        <div class="provider-title">
          <strong>${esc(p.display_name || p.provider_id)}</strong>
          <span class="status-pill ${sc}">${statusLabel(p.status)}</span>
        </div>
        <div class="provider-meta">${esc(p.label || "")}</div>
        <button class="test-button" onclick="testProvider('${esc(p.provider_id)}')">Test</button>
      </article>`;
    }
  ).join("");
  updateProviderCount();
}

function updateProviderStatus(providerId, status) {
  const card = document.querySelector(`[data-provider="${providerId}"]`);
  if (!card) return;
  const sc = statusClass(status);
  card.dataset.status = sc;
  const pill = card.querySelector(".status-pill");
  if (pill) { pill.className = `status-pill ${sc}`; pill.textContent = statusLabel(status); }
}

function updateProviderCount() {
  const el = byId("providerCount");
  if (!el) return;
  el.textContent = `${(state.config.provider_status || []).length} configured`;
}

/* ═══════════════════════════════════════════════════════
   SEARCH / FILTER (Providers)
   ═══════════════════════════════════════════════════════ */

const filterProviders = debounce((query) => {
  const q = query.toLowerCase().trim();
  const cards = document.querySelectorAll(".provider-card");
  let visible = 0;
  cards.forEach((c) => {
    const match = !q || (c.dataset.name || "").toLowerCase().includes(q);
    c.classList.toggle("hidden-search", !match);
    if (match) visible++;
  });
  const nr = byId("noResults");
  if (nr) nr.classList.toggle("visible", q && visible === 0);
}, 150);

/* ═══════════════════════════════════════════════════════
   RENDER: SECTIONS & FIELDS
   ═══════════════════════════════════════════════════════ */

let sectionById = new Map();

function renderSections(view) {
  const container = byId(view.containerId);
  container.innerHTML = "";
  const allSections = state.config.sections || [];
  sectionById.clear();
  allSections.forEach((s) => sectionById.set(s.id, s));

  view.sections.forEach((sectionId) => {
    const section = sectionById.get(sectionId);
    const sectionFields = [];
    state.fields.forEach((field, key) => {
      if (field.section === sectionId) sectionFields.push(field);
    });
    if (!section || sectionFields.length === 0) return;

    const sectionEl = document.createElement("section");
    sectionEl.className = "settings-section";
    sectionEl.id = `section-${section.id}`;

    const heading = document.createElement("div");
    heading.className = "section-heading";
    heading.innerHTML = `<div><h3>${esc(section.label)}</h3>${section.description ? `<p>${esc(section.description)}</p>` : ""}</div>`;
    if (section.id === "models") {
      const refreshBtn = document.createElement("button");
      refreshBtn.type = "button";
      refreshBtn.className = "secondary-button";
      refreshBtn.textContent = "Refresh models";
      refreshBtn.addEventListener("click", () => refreshModelOptions(refreshBtn));
      heading.appendChild(refreshBtn);
    }
    sectionEl.appendChild(heading);

    const grid = document.createElement("div");
    grid.className = "field-grid";
    sectionFields.forEach((field) => {
      const row = renderField(field);
      if (row) grid.appendChild(row);
    });
    sectionEl.appendChild(grid);

    if (sectionFields.some((f) => f.advanced)) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "ghost-button";
      toggle.textContent = "Show advanced";
      toggle.addEventListener("click", () => {
        const showing = sectionEl.classList.toggle("show-advanced");
        toggle.textContent = showing ? "Hide advanced" : "Show advanced";
      });
      sectionEl.appendChild(toggle);
    }
    container.appendChild(sectionEl);
  });
}

function readFieldValue(input) {
  if (input.type === "checkbox") return input.checked ? "true" : "false";
  if (input.dataset.fieldType === "optional_model" && input.value.trim().toLowerCase() === "none") return "";
  if (input.dataset.secret === "true" && input.dataset.configured === "true") return input.value || MASKED_SECRET;
  return input.value;
}

function changedValues() {
  const values = {};
  document.querySelectorAll("[data-key]").forEach((input) => {
    if (input.disabled || !input.matches("input, select, textarea")) return;
    const value = readFieldValue(input);
    if (value !== input.dataset.original) values[input.dataset.key] = value;
  });
  return values;
}

function updateDirtyState() {
  const count = Object.keys(changedValues()).length;
  byId("dirtyState").textContent = count === 0 ? "No changes" : `${count} unsaved change${count === 1 ? "" : "s"}`;
  byId("applyButton").disabled = count === 0;
}

/* ═══════════════════════════════════════════════════════
   USER MODELS: list with add/delete
   ═══════════════════════════════════════════════════════ */

function renderModelRefsList(container) {
  const listEl = container.querySelector(".model-refs-list");
  const hiddenInput = container.querySelector("input[type=hidden]");
  const refs = (hiddenInput.value || "").split("\n").filter(Boolean);
  listEl.innerHTML = refs.map((ref, i) =>
    '<div class="model-refs-item" data-index="' + i + '">'
      + '<span class="model-refs-ref">' + esc(ref) + '</span>'
      + '<button type="button" class="model-refs-delbtn" title="Remove this model" onclick="deleteModelRef(this)">\u00d7</button>'
    + '</div>'
  ).join("");
}

function addModelRef(input) {
  const container = input.closest(".model-refs-container");
  const hiddenInput = container.querySelector("input[type=hidden]");
  const ref = input.value.trim();
  if (!ref) { toast.info("Enter a model ref first"); return; }
  if (!ref.includes("/")) { toast.error("Model ref must include a provider prefix, e.g. open_router/..."); return; }
  const existing = (hiddenInput.value || "").split("\n").filter(Boolean);
  if (existing.includes(ref)) { toast.info("Model already in your list"); return; }
  existing.push(ref);
  hiddenInput.value = existing.join("\n");
  input.value = "";
  renderModelRefsList(container);
  updateDirtyState();
}

function deleteModelRef(btn) {
  const item = btn.closest(".model-refs-item");
  const container = btn.closest(".model-refs-container");
  const hiddenInput = container.querySelector("input[type=hidden]");
  const refs = (hiddenInput.value || "").split("\n").filter(Boolean);
  const idx = parseInt(item.dataset.index, 10);
  refs.splice(idx, 1);
  hiddenInput.value = refs.join("\n");
  renderModelRefsList(container);
  updateDirtyState();
}

// Wire up add-model buttons globally
document.addEventListener("click", function(e) {
  const addBtn = e.target.closest(".model-refs-addbtn");
  if (addBtn) {
    const input = addBtn.closest(".model-refs-addrow").querySelector(".model-refs-input");
    if (input) addModelRef(input);
  }
});

// Allow Enter key to add model
document.addEventListener("keydown", function(e) {
  if (e.key === "Enter") {
    const input = e.target.closest(".model-refs-input");
    if (input) { e.preventDefault(); addModelRef(input); }
  }
});

function renderField(field) {
  if (field.type === "section_break") return null;
  const wrapper = document.createElement("div");
  wrapper.className = `field${field.advanced ? " advanced-field" : ""}`;
  wrapper.dataset.key = field.key;

  const label = document.createElement("label");
  label.htmlFor = `field-${field.key}`;
  const labelText = document.createElement("span");
  labelText.textContent = field.label;
  label.appendChild(labelText);

  const source = sourceText(field);
  if (source) {
    const sourceEl = document.createElement("span");
    sourceEl.className = "field-source";
    sourceEl.textContent = source;
    label.appendChild(sourceEl);
  }

  const input = inputForField(field);
  input.id = `field-${field.key}`;
  input.dataset.key = field.key;
  input.dataset.original = field.value || "";
  input.dataset.secret = field.secret ? "true" : "false";
  input.dataset.configured = field.configured ? "true" : "false";
  input.dataset.fieldType = field.type;
  input.disabled = field.locked;
  input.addEventListener("input", updateDirtyState);
  input.addEventListener("change", updateDirtyState);
  if (field.type === "optional_model") {
    input.addEventListener("blur", () => {
      if (!input.value.trim() || input.value.trim().toLowerCase() === "none") {
        input.value = "None";
        updateDirtyState();
      }
    });
  }

  const control = (field.type === "model" || field.type === "optional_model")
    ? new ModelCombobox(input, field).element
    : input;
  wrapper.append(label, control);
  if (field.description) {
    const desc = document.createElement("div");
    desc.className = "field-description";
    desc.textContent = field.description;
    wrapper.appendChild(desc);
  }
  return wrapper;
}

function inputForField(field) {
  if (field.type === "boolean") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = String(field.value).toLowerCase() === "true";
    input.dataset.original = input.checked ? "true" : "false";
    return input;
  }
  if (field.type === "select") {
    const select = document.createElement("select");
    (field.options || []).forEach((item) => {
      const opt = document.createElement("option");
      opt.value = item.value;
      opt.textContent = item.label || item.value;
      select.appendChild(opt);
    });
    select.value = field.value || (field.options?.[0]?.value) || "";
    return select;
  }
  if (field.type === "textarea") {
    const ta = document.createElement("textarea");
    ta.value = field.value || "";
    return ta;
  }
  if (field.type === "model_refs") {
    const container = document.createElement("div");
    container.className = "model-refs-container";
    container.dataset.key = field.key;
    container.dataset.original = field.value || "";
    container.dataset.fieldType = "model_refs";
    // Hidden input stores the newline-separated value for the form save flow
    const hiddenInput = document.createElement("input");
    hiddenInput.type = "hidden";
    hiddenInput.dataset.key = field.key;
    hiddenInput.dataset.original = field.value || "";
    hiddenInput.dataset.fieldType = "model_refs";
    hiddenInput.value = field.value || "";
    container.appendChild(hiddenInput);
    // Visible list
    const listEl = document.createElement("div");
    listEl.className = "model-refs-list";
    container.appendChild(listEl);
    // Add-row
    const addRow = document.createElement("div");
    addRow.className = "model-refs-addrow";
    addRow.innerHTML = '<input type="text" class="model-refs-input" placeholder="e.g. open_router/anthropic/claude-sonnet-4" /><button type="button" class="model-refs-addbtn" title="Add model ref">+</button>';
    container.appendChild(addRow);
    return container;
  }
  if (field.type === "model" || field.type === "optional_model") {
    const input = document.createElement("input");
    input.type = "text";
    input.value = field.value || (field.type === "optional_model" ? "None" : "");
    input.autocomplete = "off";
    return input;
  }
  const input = document.createElement("input");
  input.type = field.type === "number" ? "number" : "text";
  if (field.type === "secret") {
    input.type = "password";
    input.placeholder = field.configured ? "Configured - enter new value to replace" : "Not configured";
    input.value = "";
    input.autocomplete = "off";
  } else {
    input.value = field.value || "";
  }
  return input;
}

/* ═══════════════════════════════════════════════════════
   MODEL COMBOBOX
   ═══════════════════════════════════════════════════════ */

function optionEl(value, label) {
  const opt = document.createElement("option");
  opt.value = value;
  opt.textContent = label;
  return opt;
}

class ModelCombobox {
  constructor(input, field) {
    this.input = input;
    this.fieldType = field.type;
    this.activeIndex = -1;
    this.query = "";

    this.element = document.createElement("div");
    this.element.className = "model-combobox";
    this.listbox = document.createElement("div");
    this.listbox.className = "model-combobox-list";
    this.listbox.id = `model-options-${field.key}`;
    this.listbox.setAttribute("role", "listbox");
    this.listbox.hidden = true;
    this.toggle = document.createElement("button");
    this.toggle.type = "button";
    this.toggle.className = "model-combobox-toggle";
    this.toggle.disabled = input.disabled;
    this.toggle.setAttribute("aria-label", `Show ${field.label} options`);

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    for (const ctrl of [input, this.toggle]) {
      ctrl.setAttribute("aria-controls", this.listbox.id);
      ctrl.setAttribute("aria-expanded", "false");
    }

    input.addEventListener("click", () => this.open());
    input.addEventListener("input", () => this.open(input.value));
    input.addEventListener("keydown", (event) => this.handleKeydown(event));
    this.toggle.addEventListener("mousedown", (e) => e.preventDefault());
    this.toggle.addEventListener("click", () => { this.isOpen ? this.close() : this.open(); });

    this.element.append(input, this.toggle, this.listbox);
    state.modelComboboxes.add(this);
  }

  get visibleOptions() {
    return [...this.listbox.children].filter(
      (el) => el.matches(".model-combobox-option") && !el.hidden,
    );
  }

  get isOpen() { return !this.listbox.hidden; }

  open(filter) {
    this.query = String(filter || "");
    const source = state.modelOptions || [];
    const q = this.query.toLowerCase();
    const filtered = this.query
      ? source.filter((m) => m.toLowerCase().includes(q))
      : source;
    const items = (this.fieldType === "optional_model" ? ["None", ...filtered] : filtered);
    this.listbox.innerHTML = items.map(
      (v, i) => `<div class="model-combobox-option${v === this.input.value ? " active" : ""}" role="option" aria-selected="${v === this.input.value}" data-value="${v}" id="${this.listbox.id}-${i}">${v}</div>`
    ).join("");
    if (!items.length) {
      this.listbox.innerHTML = `<div class="model-combobox-option" style="color:var(--text-faint);cursor:default">No matches</div>`;
    }
    this.listbox.hidden = false;
    this.input.setAttribute("aria-expanded", "true");
    this.toggle.setAttribute("aria-expanded", "true");
    this.element.classList.add("open");
    this.activeIndex = -1;
  }

  close() {
    this.listbox.hidden = true;
    this.input.setAttribute("aria-expanded", "false");
    this.toggle.setAttribute("aria-expanded", "false");
    this.element.classList.remove("open");
    this.activeIndex = -1;
    this.input.setAttribute("aria-activedescendant", "");
  }

  setActive(index, scroll = true) {
    const options = this.visibleOptions;
    if (!options.length) return;
    this.activeIndex = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((el, i) => {
      const a = i === this.activeIndex;
      el.classList.toggle("active", a);
      el.setAttribute("aria-selected", String(a));
    });
    this.input.setAttribute("aria-activedescendant", options[this.activeIndex]?.id || "");
    if (scroll && options[this.activeIndex]) options[this.activeIndex].scrollIntoView({ block: "nearest" });
  }

  move(offset) {
    const count = this.visibleOptions.length;
    if (count) this.setActive((this.activeIndex + offset + count) % count);
  }

  select(value) {
    this.input.value = value;
    this.input.dispatchEvent(new Event("change", { bubbles: true }));
    this.close();
    this.input.focus();
  }

  handleKeydown(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (this.isOpen) this.move(event.key === "ArrowDown" ? 1 : -1);
      else { this.open(); if (event.key === "ArrowUp") this.setActive(this.visibleOptions.length - 1); }
    } else if (this.isOpen && (event.key === "Home" || event.key === "End")) {
      event.preventDefault();
      this.setActive(event.key === "Home" ? 0 : this.visibleOptions.length - 1);
    } else if (this.isOpen && event.key === "Enter") {
      const active = this.visibleOptions[this.activeIndex];
      if (active) { event.preventDefault(); this.select(active.dataset.value); }
    } else if (this.isOpen && event.key === "Escape") {
      event.preventDefault();
      this.close();
    } else if (this.isOpen && event.key === "Tab") {
      this.close();
    }
  }
}

/* ═══════════════════════════════════════════════════════
   KEYBOARD SHORTCUTS
   ═══════════════════════════════════════════════════════ */

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s") {
    e.preventDefault();
    const btn = byId("applyButton");
    if (btn && !btn.disabled) apply();
  }
});

/* ═══════════════════════════════════════════════════════
   EVENT BINDING & INIT
   ═══════════════════════════════════════════════════════ */

byId("validateButton")?.addEventListener("click", () => validate(true));
byId("applyButton")?.addEventListener("click", apply);
byId("searchInput")?.addEventListener("input", (e) => {
  if (state.activeView === "providers") filterProviders(e.target.value);
});

/* Nav click listener — attached once, not on every renderNav call. */
byId("sectionNav")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".nav-link");
  if (btn) setActiveView(btn.dataset.view);
});

document.addEventListener("pointerdown", (event) => {
  state.modelComboboxes.forEach((cb) => {
    if (cb.isOpen && !cb.element.contains(event.target)) cb.close();
  });
});

load().catch((error) => {
  toast.error(error.message);
});
