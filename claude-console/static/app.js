/* ============================================================
   Claude Console — frontend controller (vanilla ES, one IIFE)
   No framework, no CDN. Reads the API shapes in CONTRACT §3/§5.
   SECURITY: every untrusted string goes through textContent or
   esc(); never innerHTML of session/history/settings strings.
   ============================================================ */
(function () {
  "use strict";

  // ───────────────────── DOM helpers ─────────────────────
  const $ = (sel, root) => (root || document).querySelector(sel);

  /** Escape a string for safe interpolation into innerHTML. */
  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** Create an element with optional className/text/attrs. text is set via
   *  textContent (safe). children may be nodes. */
  function el(tag, opts, children) {
    const node = document.createElement(tag);
    opts = opts || {};
    if (opts.class) node.className = opts.class;
    if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
    if (opts.title !== undefined) node.title = String(opts.title);
    if (opts.attrs) for (const k in opts.attrs) node.setAttribute(k, opts.attrs[k]);
    if (opts.on) for (const ev in opts.on) node.addEventListener(ev, opts.on[ev]);
    if (children) for (const c of children) if (c) node.appendChild(c);
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // ───────────────────── API ─────────────────────
  async function api(path) {
    const res = await fetch(path, { headers: { "Accept": "application/json" } });
    // Server wraps scanner errors as HTTP 200 {error:...}; still parse json.
    let data;
    try { data = await res.json(); }
    catch (e) { return { error: "bad response (" + res.status + ")" }; }
    if (!res.ok && data && data.error === undefined) {
      return { error: "HTTP " + res.status };
    }
    return data;
  }

  async function apiPost(path) {
    try {
      const res = await fetch(path, { method: "POST", headers: { "Accept": "application/json" } });
      return await res.json();
    } catch (e) { return { ok: false, error: "request failed" }; }
  }

  // Transient bottom-center notification.
  function toast(msg, isError) {
    const t = el("div", { class: "toast" + (isError ? " err" : ""), text: msg });
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add("show"));
    setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 300); }, 2800);
  }

  // ───────────────────── State ─────────────────────
  const DOMAINS = [
    { id: "history",  label: "会话历史", icon: "⏲" },
    { id: "projects", label: "会话记录", icon: "▤" },
    { id: "settings", label: "Settings", icon: "⚙" },
    { id: "skills",   label: "Skills",   icon: "✦" },
    { id: "plugins",  label: "Plugins",  icon: "❒" },
    { id: "agents",   label: "Agents",   icon: "◑" },
    { id: "commands", label: "Commands", icon: "›_" },
    { id: "hooks",    label: "Hooks",    icon: "⤳" },
    { id: "mcp",      label: "MCP",      icon: "⇄" },
    { id: "plans",    label: "Plans",    icon: "❖" },
  ];

  const TITLES = {
    history:  ["会话历史", "Prompt history (history.jsonl)"],
    projects: ["会话记录", "Per-cwd transcript directories"],
    settings: ["Settings", "Active settings.json (redacted) + summary"],
    skills:   ["Skills", "Installed skills from skills/*/SKILL.md"],
    plugins:  ["Plugins", "Installed plugins & marketplaces"],
    agents:   ["Agents", "Subagent definitions from agents/*.md"],
    commands: ["Commands", "Slash commands from commands/**/*.md"],
    hooks:    ["Hooks", "Lifecycle hooks declared in settings.json"],
    mcp:      ["MCP", "Configured MCP servers (.claude.json)"],
    plans:    ["Plans", "Saved plan-mode documents (plans/*.md)"],
  };

  const cache = {};        // domain -> last payload
  let active = "history";
  let lastOverview = null; // latest /api/overview payload — feeds the nav count badges
  let ws = null;
  let wsBackoff = 1000;
  // Project keys whose row is expanded — preserved across the live re-renders
  // that fire when a watched transcript changes, so an open expansion (and its
  // fresh token totals) survives instead of collapsing.
  const expandedProjects = new Set();
  // Per-project active sub-tab (sessions | memory | agents), also preserved.
  const projectTab = {};
  // The transcript payload currently shown in the drawer (for "long screenshot"
  // sharing). Null when the drawer shows non-transcript content (a memory file).
  let currentTranscriptData = null;
  // Closure that re-opens whatever the drawer currently shows, so the ⟳ button
  // can re-fetch the latest content (e.g. a transcript whose jsonl grew).
  let currentReload = null;
  let historyState = { q: "", offset: 0, limit: 100, total: 0, items: [] };

  // ───────────────────── Theme (正常 light / 夜间 dark) ─────────────────────
  // The <head> inline script already applied the saved theme before first paint;
  // here we just keep the toggle button's icon in sync and persist on toggle.
  const THEME_KEY = "cc-theme";
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }
  function syncThemeButton() {
    const btn = $("#theme-toggle");
    if (!btn) return;
    const dark = currentTheme() === "dark";
    btn.textContent = dark ? "☀" : "🌙";              // icon = the mode you switch TO
    btn.title = dark ? "切换到正常模式（亮色）" : "切换到夜间模式（暗色）";
  }
  function toggleTheme() {
    const next = currentTheme() === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    syncThemeButton();
  }

  // ───────────────────── Boot ─────────────────────
  function init() {
    syncThemeButton();
    buildNav();
    wireChrome();
    openSocket();
    selectDomain("history", true);
    refreshOverviewCounts(); // populate nav count badges (from /api/overview)
  }

  function buildNav() {
    const nav = $("#nav");
    clear(nav);
    for (const d of DOMAINS) {
      const item = el("div", {
        class: "nav-item",
        attrs: { "data-domain": d.id },
        on: { click: () => selectDomain(d.id) },
      });
      item.appendChild(el("span", { class: "nav-ico", text: d.icon }));
      item.appendChild(el("span", { class: "nav-label", text: d.label }));
      const cnt = el("span", { class: "nav-count", text: "" });
      cnt.dataset.count = d.id;
      item.appendChild(cnt);
      nav.appendChild(item);
    }
  }

  function wireChrome() {
    $("#reload-all").addEventListener("click", () => {
      for (const k in cache) delete cache[k];
      lastOverview = null;
      for (const d of Array.from(pendingDomains)) clearPending(d);
      refreshOverviewCounts();
      loadDomain(active, true);
    });
    $("#refresh-btn").addEventListener("click", () => loadDomain(active, true));

    const themeBtn = $("#theme-toggle");
    if (themeBtn) themeBtn.addEventListener("click", toggleTheme);

    const act = $("#activity");
    $("#activity-bar").addEventListener("click", () => act.classList.toggle("collapsed"));

    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#drawer-backdrop").addEventListener("click", closeDrawer);
    const refreshDrawerBtn = $("#drawer-refresh");
    if (refreshDrawerBtn) refreshDrawerBtn.addEventListener("click", () => { if (currentReload) currentReload(); });
    const shareBtn = $("#drawer-share");   // guard: tolerate a stale cached HTML
    if (shareBtn) shareBtn.addEventListener("click", shareTranscript);
    // Automation hook for the long-screenshot renderer (harmless; lets tests
    // exercise buildTranscriptImage directly without the live-churn UI).
    window.__cc = { build: buildTranscriptImage };
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
    });
  }

  // ───────────────────── Navigation ─────────────────────
  function selectDomain(id, force) {
    if (id === active && !force && cache[id]) { return; }
    active = id;
    for (const n of document.querySelectorAll(".nav-item")) {
      n.classList.toggle("active", n.dataset.domain === id);
    }
    const t = TITLES[id] || [id, ""];
    $("#page-title").textContent = t[0];
    $("#page-subtitle").textContent = t[1];
    loadDomain(id, false);
  }

  async function loadDomain(id, force) {
    const content = $("#content");
    if (!force && cache[id]) {
      renderDomain(id, cache[id]);
      clearPending(id);
      return;
    }
    clear(content);
    content.appendChild(el("div", { class: "loading", text: "Loading " + id + "…" }));
    const data = await fetchDomain(id);
    cache[id] = data;
    if (active === id) renderDomain(id, data);
    stampUpdated();
    clearPending(id);
  }

  function fetchDomain(id) {
    switch (id) {
      case "settings": return api("/api/settings");
      case "skills":   return api("/api/skills");
      case "plugins":  return api("/api/plugins");
      case "agents":   return api("/api/agents");
      case "commands": return api("/api/commands");
      case "hooks":    return api("/api/hooks");
      case "mcp":      return api("/api/mcp");
      case "projects": return api("/api/projects");
      case "history":  return loadHistory(true);
      case "plans":    return api("/api/plans");
      default:         return Promise.resolve({ error: "unknown domain" });
    }
  }

  function stampUpdated() {
    const d = new Date();
    $("#last-updated").textContent =
      "updated " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function renderDomain(id, data) {
    const content = $("#content");
    clear(content);
    // Top-level error payload → banner, but keep rendering shell.
    if (data && data.error) content.appendChild(errorBanner(data.error));
    try {
      ({
        settings: renderSettings,
        skills:   renderSkills,
        plugins:  renderPlugins,
        agents:   (c, d) => renderAgentLike(c, d, "agents"),
        commands: (c, d) => renderAgentLike(c, d, "commands"),
        hooks:    renderHooks,
        mcp:      renderMcp,
        projects: renderProjects,
        history:  renderHistory,
        plans:    renderPlans,
      }[id] || ((c) => c.appendChild(el("div", { class: "empty-state", text: "Nothing to render." }))))(content, data || {});
    } catch (e) {
      content.appendChild(errorBanner("render failure: " + (e && e.message)));
    }
  }

  function errorBanner(msg) {
    const b = el("div", { class: "error-banner" });
    b.appendChild(el("b", { text: "error " }));
    b.appendChild(document.createTextNode(String(msg)));
    return b;
  }

  function emptyState(icon, msg) {
    const e = el("div", { class: "empty-state" });
    e.appendChild(el("span", { class: "em-ico", text: icon }));
    e.appendChild(document.createTextNode(msg));
    return e;
  }

  function sectionLabel(text) { return el("div", { class: "section-label", text }); }

  function fmtNum(n) {
    if (n === undefined || n === null) return "—";
    if (typeof n === "number") return n.toLocaleString();
    return String(n);
  }

  // Compact token count: 1234 -> "1.2K", 1500000 -> "1.5M".
  function fmtTok(n) {
    if (n === undefined || n === null || typeof n !== "number") return "0";
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K";
    return String(n);
  }

  // Human-readable breakdown for a token usage object (drives the hover title).
  function tokTitle(t) {
    if (!t || typeof t !== "object") return "no token data";
    const f = (x) => (typeof x === "number" ? x.toLocaleString() : "0");
    return "input " + f(t.input) +
      " · output " + f(t.output) +
      " · cache read " + f(t.cache_read) +
      " · cache write " + f(t.cache_creation) +
      "  (total " + f(t.total) + " across " + f(t.messages) + " replies)";
  }

  // ───────────────────── Settings ─────────────────────
  function renderSettings(content, d) {
    const sum = d.summary || {};

    // summary chips
    const chips = el("div", { class: "chip-row" });
    chips.appendChild(chip("effort", sum.effortLevel || "—"));
    chips.appendChild(chip("plugins enabled", fmtNum(sum.enabled_plugins_count)));
    const perms = sum.permissions || {};
    chips.appendChild(chip("allow", (perms.allow || []).length));
    chips.appendChild(chip("deny", (perms.deny || []).length));
    chips.appendChild(chip("ask", (perms.ask || []).length));
    content.appendChild(sectionLabel("Summary"));
    content.appendChild(chips);

    // flags
    const flags = sum.flags || {};
    const fkeys = Object.keys(flags);
    if (fkeys.length) {
      const frow = el("div", { class: "chip-row", attrs: { style: "margin-top:8px" } });
      for (const k of fkeys) {
        const b = el("span", { class: "badge " + (flags[k] ? "green" : "off") });
        b.appendChild(document.createTextNode((flags[k] ? "✓ " : "✕ ") + k));
        frow.appendChild(b);
      }
      content.appendChild(frow);
    }

    // hook events present
    const hev = Array.isArray(sum.hook_events) ? sum.hook_events : [];
    if (hev.length) {
      const hrow = el("div", { class: "chip-row", attrs: { style: "margin-top:8px" } });
      for (const e of hev) hrow.appendChild(el("span", { class: "badge accent", text: e }));
      content.appendChild(hrow);
    }

    // redacted settings JSON viewer — pretty-printed + syntax-highlighted
    content.appendChild(sectionLabel("settings.json (redacted)"));
    if (d.settings === null || d.settings === undefined) {
      content.appendChild(el("div", { class: "json-viewer", text: "// settings.json missing or unparseable" }));
    } else {
      try {
        content.appendChild(jsonViewer(d.settings, false));   // strict JSON, highlighted
      } catch (e) {
        content.appendChild(el("div", { class: "json-viewer", text: "// could not render settings" }));
      }
    }

    // ~/.claude/CLAUDE.md global instructions — rendered markdown, if present.
    const cmd = d.claude_md || {};
    if (cmd.exists && cmd.content) {
      content.appendChild(sectionLabel("CLAUDE.md (~/.claude/CLAUDE.md)"));
      const box = el("div", { class: "claude-md-box" });
      const doc = el("div", { class: "md-doc" });
      renderMarkdown(doc, cmd.content);
      box.appendChild(doc);
      if (cmd.truncated) box.appendChild(el("div", { class: "content-meta trunc", text: "truncated — showing first 512 KB" }));
      content.appendChild(box);
    }

    // files list
    const files = Array.isArray(d.files) ? d.files : [];
    if (files.length) {
      content.appendChild(sectionLabel("Settings files"));
      const fl = el("div", { class: "file-list card" });
      for (const f of files) {
        const r = el("div", { class: "file-row" + (f.is_backup ? " backup" : "") });
        const name = el("span", { class: "fname", text: f.name || "?", title: f.path || "" });
        if (f.is_backup) name.appendChild(document.createTextNode("  ·backup"));
        r.appendChild(name);
        r.appendChild(el("span", { class: "fsize", text: (f.size_h || "") + "  " + (f.mtime_iso || "") }));
        fl.appendChild(r);
      }
      content.appendChild(fl);
    }
  }

  function chip(label, value) {
    const c = el("span", { class: "chip" });
    c.appendChild(document.createTextNode(label + " "));
    c.appendChild(el("b", { text: value === undefined || value === null ? "—" : String(value) }));
    return c;
  }

  // ───────────────────── Skills ─────────────────────
  function renderSkills(content, d) {
    const items = Array.isArray(d.items) ? d.items : [];
    if (!items.length) { content.appendChild(emptyState("✦", "No skills found in skills/.")); return; }
    const grid = el("div", { class: "item-grid" });
    for (const s of items) {
      // Whole card opens the skill's SKILL.md (rendered markdown) in the drawer.
      const card = el("div", { class: "card skill-card skill-open", attrs: { role: "button", tabindex: "0" } });
      const head = el("div", { class: "skill-head" });
      head.appendChild(el("span", { class: "skill-name", text: s.name || "?" }));
      if (s.has_references) head.appendChild(el("span", { class: "badge blue", text: "refs" }));
      // Reveal the skill's folder in Finder (doesn't open the drawer).
      const reveal = el("button", { class: "path-reveal", text: "↗", attrs: { title: "在 Finder 中打开该 skill 目录", style: "margin-left:auto" } });
      reveal.addEventListener("click", (e) => { e.stopPropagation(); revealPath("skills/" + (s.key || s.name)); });
      head.appendChild(reveal);
      card.appendChild(head);

      card.appendChild(el("div", { class: "skill-desc", text: s.description || "(no description)" }));

      const meta = el("div", { class: "skill-meta" });
      const fileCount = Array.isArray(s.files) ? s.files.length : 0;
      if (fileCount) meta.appendChild(el("span", { class: "badge", text: fileCount + " file" + (fileCount === 1 ? "" : "s") }));
      if (s.mtime_iso) meta.appendChild(el("span", { class: "badge", text: shortIso(s.mtime_iso) }));
      meta.appendChild(el("span", { class: "open-hint", text: "open ↦" }));
      card.appendChild(meta);

      if (s.dir) card.appendChild(el("div", { class: "skill-path", text: s.dir }));

      const go = () => openSkill(s);
      card.addEventListener("click", go);
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
      grid.appendChild(card);
    }
    content.appendChild(grid);
  }

  // ───────────────────── Skill viewer (drawer) ─────────────────────
  // Extensions read_file_safe will serve; everything else is shown but inert.
  const VIEWABLE_EXT = /\.(md|markdown|txt|json|jsonl|js|ts|mjs|cjs|sh|toml|ya?ml|local|py|dart)$/i;
  // Noise we never list (build output / caches / compiled / binaries).
  function isNoiseFile(rel) {
    return /(^|\/)(\.pytest_cache|__pycache__|out)\//.test(rel) ||
           /\.(pyc|pyo|png|jpe?g|gif|webp|ico|pdf)$/i.test(rel) ||
           rel.split("/").pop().startsWith(".");
  }

  function openSkill(s) {
    const key = s.key || s.name;
    openText(s.name || key, s.dir || ("skills/" + key), "Loading…");
    const files = (Array.isArray(s.files) ? s.files : []).filter((f) => !isNoiseFile(f));
    renderSkillFile(key, "SKILL.md", files);
  }

  // Render one file of a skill into the drawer, with the skill's file list as a
  // clickable sidebar-style strip at the top so you can hop between files.
  async function renderSkillFile(key, file, files) {
    setDrawerReload(() => renderSkillFile(key, file, files));
    const body = $("#drawer-body");
    clear(body);
    $("#drawer-sub").textContent = "skills/" + key + "/" + file;

    // file switcher strip (SKILL.md + filtered files), current one highlighted
    const all = ["SKILL.md"].concat(files.filter((f) => f !== "SKILL.md"));
    const strip = el("div", { class: "file-strip" });
    // reveal the currently-viewed source file in Finder (selected)
    const reveal = el("span", { class: "file-chip reveal-chip", text: "↗ Finder",
      attrs: { title: "在 Finder 中显示当前文件" } });
    reveal.addEventListener("click", () => revealPath("skills/" + key + "/" + file));
    strip.appendChild(reveal);
    for (const f of all) {
      const viewable = VIEWABLE_EXT.test(f);
      const chip = el("span", {
        class: "file-chip" + (f === file ? " active" : "") + (viewable ? "" : " inert"),
        text: f, attrs: viewable ? {} : { title: "not viewable" },
      });
      if (viewable && f !== file) chip.addEventListener("click", () => renderSkillFile(key, f, files));
      strip.appendChild(chip);
    }
    body.appendChild(strip);

    const holder = el("div", { class: "skill-file-body" });
    holder.appendChild(el("div", { class: "loading", text: "Loading " + file + "…" }));
    body.appendChild(holder);

    const r = await api("/api/file?rel=" + encodeURIComponent("skills/" + key + "/" + file));
    clear(holder);
    if (r && r.error) { holder.appendChild(errorBanner(r.error)); return; }
    const txt = (r && r.content != null) ? r.content : "";
    if (/\.(md|markdown)$/i.test(file)) {
      const doc = el("div", { class: "md-doc" });
      renderMarkdown(doc, txt);
      holder.appendChild(doc);
    } else {
      // JSON / diff / plain handled by the rich-content viewer.
      renderRichContent(holder, txt, { truncated: !!(r && r.truncated), size: r && r.size });
    }
    if (r && r.truncated && /\.(md|markdown)$/i.test(file)) {
      holder.appendChild(el("div", { class: "content-meta trunc", text: "truncated — showing first 512 KB" }));
    }
  }

  // ───────────────────── Plugins ─────────────────────
  function renderPlugins(content, d) {
    const mkts = Array.isArray(d.marketplaces) ? d.marketplaces : [];
    if (mkts.length) {
      content.appendChild(sectionLabel("Marketplaces"));
      const grid = el("div", { class: "item-grid" });
      for (const m of mkts) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "skill-name", text: m.name || "?" }));
        if (m.source) card.appendChild(el("div", { class: "skill-path", text: m.source }));
        const meta = el("div", { class: "skill-meta", attrs: { style: "margin-top:8px" } });
        if (m.lastUpdated) meta.appendChild(el("span", { class: "badge", text: "updated " + shortIso(m.lastUpdated) }));
        card.appendChild(meta);
        grid.appendChild(card);
      }
      content.appendChild(grid);
    }

    const items = Array.isArray(d.items) ? d.items : [];
    content.appendChild(sectionLabel("Installed plugins (" + items.length + ")"));
    if (!items.length) { content.appendChild(emptyState("❒", "No plugins installed.")); return; }

    const tbl = el("table", { class: "tbl" });
    const thead = el("tr");
    for (const h of ["Name", "Marketplace", "Version", "Scope", "Enabled"]) thead.appendChild(el("th", { text: h }));
    const head = el("thead"); head.appendChild(thead); tbl.appendChild(head);
    const tb = el("tbody");
    for (const p of items) {
      const tr = el("tr");
      tr.appendChild(el("td", { class: "mono", text: p.name || p.key || "?", title: p.installPath || "" }));
      tr.appendChild(el("td", { class: "mono", text: p.marketplace || "—" }));
      tr.appendChild(el("td", { class: "mono", text: p.version || "—" }));
      tr.appendChild(el("td", { text: p.scope || "—" }));
      const td = el("td");
      const tg = el("span", { class: "toggle-badge" + (p.enabled ? " on" : "") });
      tg.appendChild(el("span", { class: "knob" }));
      tg.appendChild(document.createTextNode(p.enabled ? "on" : "off"));
      td.appendChild(tg);
      tr.appendChild(td);
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    content.appendChild(tbl);
  }

  // ───────────────────── Agents / Commands ─────────────────────
  function renderAgentLike(content, d, kind) {
    const items = Array.isArray(d.items) ? d.items : [];
    if (!items.length) {
      content.appendChild(emptyState(kind === "agents" ? "◑" : "›_",
        "No " + kind + " defined. Drop *.md files into " + kind + "/ to get started."));
      return;
    }
    const grid = el("div", { class: "item-grid" });
    for (const it of items) {
      const card = el("div", { class: "card skill-card" });
      card.appendChild(el("span", { class: "skill-name", text: it.name || "?" }));
      card.appendChild(el("div", { class: "skill-desc", text: it.description || "(no description)" }));
      const meta = el("div", { class: "skill-meta" });
      if (it.mtime_iso) meta.appendChild(el("span", { class: "badge", text: shortIso(it.mtime_iso) }));
      card.appendChild(meta);
      if (it.path) card.appendChild(el("div", { class: "skill-path", text: it.path }));
      grid.appendChild(card);
    }
    content.appendChild(grid);
  }

  // ───────────────────── Plans ─────────────────────
  // Plan-mode documents (plans/*.md). Newest-first list; clicking a row opens
  // the markdown rendered in the drawer (reuses renderMarkdown).
  function renderPlans(content, d) {
    const items = Array.isArray(d.items) ? d.items : [];
    if (!items.length) {
      content.appendChild(emptyState("❖", "No saved plans. Plan mode writes approved plans to plans/*.md."));
      return;
    }
    const list = el("div", { class: "plan-list" });
    for (const it of items) {
      const row = el("div", { class: "plan-row", attrs: { role: "button", tabindex: "0" } });
      row.appendChild(el("div", { class: "plan-title", text: it.title || it.name || "(untitled plan)" }));
      row.appendChild(el("div", {
        class: "plan-meta mono",
        text: (it.name || "") + "  ·  " + (it.size_h || "") +
              (it.mtime_iso ? "  ·  " + shortIso(it.mtime_iso) : ""),
      }));
      const go = () => openPlan(it);
      row.addEventListener("click", go);
      row.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
      list.appendChild(row);
    }
    content.appendChild(list);
  }

  async function openPlan(it) {
    setDrawerReload(() => openPlan(it));
    openText(it.title || it.name || "plan",
      (it.rel || "") + (it.size_h ? " · " + it.size_h : ""), "Loading…");
    const r = await api("/api/file?rel=" + encodeURIComponent(it.rel || ""));
    const body = $("#drawer-body");
    body.textContent = "";
    if (r && r.error) { body.appendChild(errorBanner(r.error)); return; }
    const txt = (r && r.content != null) ? r.content : "";
    const doc = el("div", { class: "md-doc" });
    if (looksLikeDiff(txt.trim())) doc.appendChild(renderDiff(txt));
    else renderMarkdown(doc, txt);
    body.appendChild(doc);
    if (r && r.truncated) {
      body.appendChild(el("div", { class: "content-meta trunc",
        text: "truncated — showing first 512 KB" }));
    }
  }

  // ───────────────────── Hooks ─────────────────────
  function renderHooks(content, d) {
    const events = Array.isArray(d.events) ? d.events : [];
    if (!events.length) { content.appendChild(emptyState("⤳", "No hooks configured in settings.json.")); return; }
    const acc = el("div", { class: "acc" });
    for (const ev of events) {
      const entries = Array.isArray(ev.entries) ? ev.entries : [];
      const item = el("div", { class: "acc-item" });
      const head = el("div", { class: "acc-head" });
      const left = el("span", { class: "ev-name", text: ev.event || "?" });
      head.appendChild(left);
      const right = el("span", {});
      let hookCount = 0;
      for (const e of entries) hookCount += (Array.isArray(e.hooks) ? e.hooks.length : 0);
      right.appendChild(el("span", { class: "badge accent", text: hookCount + " hook" + (hookCount === 1 ? "" : "s") }));
      right.appendChild(document.createTextNode(" "));
      const arrow = el("span", { class: "ev-arrow", text: "▸" });
      right.appendChild(arrow);
      head.appendChild(right);
      head.addEventListener("click", () => item.classList.toggle("open"));
      item.appendChild(head);

      const body = el("div", { class: "acc-body" });
      for (const e of entries) {
        const ent = el("div", { class: "hook-entry" });
        if (e.matcher !== undefined && e.matcher !== null && e.matcher !== "") {
          const m = el("div", { class: "hook-matcher" });
          m.appendChild(document.createTextNode("matcher: "));
          m.appendChild(el("b", { text: String(e.matcher) }));
          ent.appendChild(m);
        }
        const hooks = Array.isArray(e.hooks) ? e.hooks : [];
        for (const h of hooks) {
          const cmd = el("div", { class: "hook-cmd" });
          const meta = [];
          if (h.type) meta.push(h.type);
          if (h.timeout) meta.push("timeout=" + h.timeout);
          if (h.async) meta.push("async");
          // command is redacted server-side; render via textContent (safe).
          cmd.textContent = (meta.length ? "[" + meta.join(" · ") + "]\n" : "") + (h.command || "(no command)");
          ent.appendChild(cmd);
        }
        body.appendChild(ent);
      }
      item.appendChild(body);
      acc.appendChild(item);
    }
    content.appendChild(acc);
  }

  // ───────────────────── MCP ─────────────────────
  function renderMcp(content, d) {
    const servers = Array.isArray(d.servers) ? d.servers : [];
    if (d.source) content.appendChild(sectionLabel("source: " + d.source));
    if (!servers.length) { content.appendChild(emptyState("⇄", "No MCP servers configured in .claude.json.")); return; }
    const grid = el("div", { class: "item-grid" });
    for (const s of servers) {
      const card = el("div", { class: "card mcp-card" });
      const head = el("div", { class: "mcp-head" });
      head.appendChild(el("span", { class: "mcp-name", text: s.name || "?" }));
      const tr = (s.transport || "stdio");
      const trCls = tr === "stdio" ? "purple" : (tr === "sse" ? "yellow" : "blue");
      head.appendChild(el("span", { class: "badge " + trCls, text: tr }));
      if (s.needs_auth === true) head.appendChild(el("span", { class: "badge red", text: "needs auth" }));
      else if (s.needs_auth === false) head.appendChild(el("span", { class: "badge green", text: "authed" }));
      card.appendChild(head);

      // connection line
      const conn = el("div", { class: "mcp-conn" });
      if (s.command) {
        conn.appendChild(el("span", { class: "lbl", text: "cmd " }));
        let cmdStr = s.command;
        if (Array.isArray(s.args) && s.args.length) cmdStr += " " + s.args.join(" ");
        conn.appendChild(document.createTextNode(cmdStr));
      } else if (s.url) {
        conn.appendChild(el("span", { class: "lbl", text: "url " }));
        conn.appendChild(document.createTextNode(s.url));
      } else {
        conn.appendChild(document.createTextNode("(no command/url)"));
      }
      card.appendChild(conn);

      // env_keys chips (KEYS ONLY)
      const keys = Array.isArray(s.env_keys) ? s.env_keys : [];
      if (keys.length) {
        const row = el("div", { class: "chip-row" });
        for (const k of keys) row.appendChild(el("span", { class: "badge", text: "$" + k }));
        card.appendChild(row);
      }
      grid.appendChild(card);
    }
    content.appendChild(grid);
  }

  // ───────────────────── Projects ─────────────────────
  function renderProjects(content, d) {
    const items = Array.isArray(d.items) ? d.items : [];
    // Keep the History→session jump map fresh whenever projects (re)render,
    // including WS-driven updates — free, no extra fetch.
    if (Array.isArray(d.items)) projectKeySet = new Set(d.items.map((p) => p.key));
    if (!items.length) { content.appendChild(emptyState("▤", "No project transcript directories.")); return; }

    const tbl = el("table", { class: "tbl" });
    const head = el("thead"); const hr = el("tr");
    for (const h of ["Path", "Sessions", "Size", "Last activity"]) hr.appendChild(el("th", { text: h }));
    head.appendChild(hr); tbl.appendChild(head);
    const tb = el("tbody");

    for (const p of items) {
      const tr = el("tr", { class: "clickable" });
      const pathTd = projectPathCell(p, expandedProjects.has(p.key));
      const chev = pathTd.querySelector(".path-chevron");
      tr.appendChild(pathTd);
      tr.appendChild(el("td", { class: "num", text: fmtNum(p.session_count) }));
      tr.appendChild(el("td", { class: "num", text: p.size_h || "" }));
      tr.appendChild(el("td", { class: "mono", text: p.last_activity_iso ? shortIso(p.last_activity_iso) : "" }));
      tb.appendChild(tr);

      // placeholder expansion row
      const exRow = el("tr", { class: "hidden" });
      const exCell = el("td", { class: "sessions-cell", attrs: { colspan: "4" } });
      exRow.appendChild(exCell);
      tb.appendChild(exRow);

      // Render the expansion: a tab bar (Sessions / Memory / Agents) + body.
      // Re-fetches the active tab so it stays live across re-renders.
      function renderExpansion() {
        clear(exCell);
        const inner = el("div", { class: "sessions-inner" });
        const cur = projectTab[p.key] || "sessions";
        const tabs = el("div", { class: "proj-tabs" });
        for (const [id, label] of [["sessions", "Sessions"], ["memory", "Memory"], ["agents", "Agents"]]) {
          const t = el("button", { class: "proj-tab" + (id === cur ? " active" : ""), text: label });
          t.addEventListener("click", () => { projectTab[p.key] = id; renderExpansion(); });
          tabs.appendChild(t);
        }
        inner.appendChild(tabs);
        const tabBody = el("div", { class: "proj-tab-body" });
        inner.appendChild(tabBody);
        exCell.appendChild(inner);
        loadTab(cur, tabBody);
      }

      async function loadTab(tab, body) {
        clear(body);
        body.appendChild(el("div", { class: "loading", text: "Loading " + tab + "…" }));
        let data, render;
        if (tab === "memory") {
          data = await api("/api/projects/" + encodeURIComponent(p.key) + "/memory");
          render = renderMemoryInline;
        } else if (tab === "agents") {
          data = await api("/api/projects/" + encodeURIComponent(p.key) + "/subagents");
          render = renderSubagentsInline;
        } else {
          data = await api("/api/projects/" + encodeURIComponent(p.key) + "/sessions");
          render = renderSessionsInline;
        }
        // Ignore a stale response if the user switched tabs meanwhile.
        if ((projectTab[p.key] || "sessions") !== tab) return;
        clear(body);
        render(body, data, p);
      }

      tr.addEventListener("click", () => {
        if (expandedProjects.has(p.key)) {
          expandedProjects.delete(p.key);
          exRow.classList.add("hidden");
          if (chev) chev.classList.remove("open");
        } else {
          expandedProjects.add(p.key);
          exRow.classList.remove("hidden");
          if (chev) chev.classList.add("open");
          renderExpansion();
        }
      });

      // Restore an open expansion across live re-renders (with fresh data).
      if (expandedProjects.has(p.key)) {
        exRow.classList.remove("hidden");
        renderExpansion();
      }
    }
    tbl.appendChild(tb);
    content.appendChild(tbl);
  }

  // Build the prominent path cell: a chevron + dimmed parent dir + bold,
  // bright final folder (the project itself, the focal point of the row), plus
  // a "reveal in file manager" button that jumps to the real directory.
  function projectPathCell(p, expanded) {
    const full = p.real_path || p.key || "?";
    const td = el("td", { class: "path-cell", title: p.key || full });
    const wrap = el("div", { class: "path-wrap" });
    wrap.appendChild(el("span", { class: "path-chevron" + (expanded ? " open" : ""), text: "▸" }));
    const txt = el("span", { class: "path-text" });
    const idx = full.lastIndexOf("/");
    if (idx >= 0 && idx < full.length - 1) {
      txt.appendChild(el("span", { class: "path-dir", text: full.slice(0, idx + 1) }));
      txt.appendChild(el("span", { class: "path-base", text: full.slice(idx + 1) }));
    } else {
      txt.appendChild(el("span", { class: "path-base", text: full }));
    }
    wrap.appendChild(txt);

    const open = el("button", {
      class: "path-reveal", text: "↗",
      attrs: { title: "Open this folder in Finder" },
    });
    open.addEventListener("click", (e) => {
      e.stopPropagation();           // don't toggle the row expansion
      revealProject(p.key, "cwd");
    });
    wrap.appendChild(open);

    td.appendChild(wrap);
    return td;
  }

  async function revealProject(key, which) {
    const r = await apiPost("/api/reveal/" + encodeURIComponent(key) +
      (which ? "?which=" + encodeURIComponent(which) : ""));
    if (r && r.ok) toast("Opened " + (r.path || "folder"));
    else toast((r && r.error ? r.error : "could not open folder") +
      (r && r.path ? " — " + r.path : ""), true);
  }

  // Reveal a path under ~/.claude (a file is selected; a dir is opened) in Finder.
  async function revealPath(rel) {
    const r = await apiPost("/api/reveal-path?rel=" + encodeURIComponent(rel));
    if (r && r.ok) toast("Revealed " + (r.path || rel));
    else toast((r && r.error ? r.error : "could not reveal") +
      (r && r.path ? " — " + r.path : ""), true);
  }

  function renderSessionsInline(container, sd, project) {
    if (sd && sd.error) { container.appendChild(errorBanner(sd.error)); return; }
    const sessions = Array.isArray(sd.sessions) ? sd.sessions : [];
    if (!sessions.length) {
      container.appendChild(el("div", { class: "loading", text: "No sessions in this project." }));
      return;
    }

    // Project token roll-up header (sum across all sessions).
    const totals = (sd.totals && sd.totals.tokens) || null;
    if (totals) {
      const head = el("div", { class: "sessions-head" });
      head.appendChild(el("span", { text: sessions.length + " sessions" }));
      const tb = el("span", {
        class: "tok-badge total", attrs: { title: tokTitle(totals) },
      });
      tb.appendChild(el("span", { class: "tok-ico", text: "◆" }));
      tb.appendChild(document.createTextNode(fmtTok(totals.total) + " tokens"));
      head.appendChild(tb);
      container.appendChild(head);
    }

    for (const s of sessions) {
      const row = el("div", { class: "session-row" });
      const left = el("div", { class: "session-prompt" });
      left.appendChild(el("span", { class: "sid", text: (s.session_id || "").slice(0, 8) }));
      left.appendChild(document.createTextNode(s.first_prompt || s.summary || "(no prompt)"));
      row.appendChild(left);

      const meta = el("div", { class: "session-meta" });
      if (s.subagent_count > 0) {
        meta.appendChild(el("span", {
          class: "sub-badge", attrs: { title: s.subagent_count + " subagent transcript(s)" },
          text: "⛓ " + s.subagent_count,
        }));
      }
      if (s.task_count > 0) {
        const tb = el("span", {
          class: "task-badge",
          attrs: { title: s.task_count + " background task output(s) — click to view" },
          text: "⚙ " + s.task_count,
        });
        // Open the tasks list instead of the transcript; don't bubble to the row.
        tb.addEventListener("click", (e) => {
          e.stopPropagation();
          openSessionTasks(sd.project_key || project.key, s.session_id, sd.real_path || project.real_path);
        });
        meta.appendChild(tb);
      }
      const tok = s.tokens || null;
      if (tok && tok.total > 0) {
        const badge = el("span", { class: "tok-badge", attrs: { title: tokTitle(tok) } });
        badge.appendChild(el("span", { class: "tok-ico", text: "◆" }));
        badge.appendChild(document.createTextNode(fmtTok(tok.total)));
        meta.appendChild(badge);
      }
      meta.appendChild(el("span", {
        class: "session-size",
        text: (s.size_h || "") + " · " + (s.mtime_iso ? shortIso(s.mtime_iso) : ""),
      }));
      row.appendChild(meta);

      row.addEventListener("click", () => openTranscript(sd.project_key || project.key, s.session_id, s.first_prompt, sd.real_path || project.real_path, tok));
      container.appendChild(row);
    }
  }

  // ───────────────────── Project memory ─────────────────────
  function renderMemoryInline(container, md, project) {
    if (md && md.error) { container.appendChild(errorBanner(md.error)); return; }
    if (!md || !md.exists) {
      container.appendChild(el("div", { class: "loading", text: "No memory store for this project." }));
      return;
    }
    const items = Array.isArray(md.items) ? md.items : [];
    const head = el("div", { class: "sessions-head" });
    head.appendChild(el("span", { text: items.length + " memories" }));
    if (md.index) {
      const btn = el("button", { class: "mini-btn", text: "MEMORY.md" });
      btn.addEventListener("click", () => openText("MEMORY.md", md.real_path || project.key, md.index));
      head.appendChild(btn);
    }
    container.appendChild(head);

    if (!items.length) {
      container.appendChild(el("div", { class: "loading", text: "Index only — no individual memory files." }));
      return;
    }
    for (const m of items) {
      const row = el("div", { class: "mem-row" });
      const top = el("div", { class: "mem-top" });
      if (m.type) top.appendChild(el("span", { class: "mem-type " + memTypeCls(m.type), text: m.type }));
      top.appendChild(el("span", { class: "mem-name", text: m.name || m.file }));
      top.appendChild(el("span", { class: "session-size", text: (m.size_h || "") + " · " + (m.mtime_iso ? shortIso(m.mtime_iso) : "") }));
      row.appendChild(top);
      if (m.description) row.appendChild(el("div", { class: "mem-desc", text: m.description }));
      row.addEventListener("click", () => openMemory(m, md.real_path || project.key));
      container.appendChild(row);
    }
  }

  function memTypeCls(t) {
    switch (String(t)) {
      case "user": return "blue";
      case "project": return "accent";
      case "feedback": return "yellow";
      case "reference": return "purple";
      default: return "";
    }
  }

  async function openMemory(m, realPath) {
    setDrawerReload(() => openMemory(m, realPath));
    openText(m.name || m.file, realPath, "Loading…");
    const r = await api("/api/file?rel=" + encodeURIComponent(m.rel));
    const txt = (r && r.content) ? r.content : (r && r.error ? "(" + r.error + ")" : "(empty)");
    $("#drawer-body").textContent = "";
    $("#drawer-body").appendChild(el("pre", { class: "raw-text", text: txt }));
  }

  // ───────────────────── Project subagents / workflows ─────────────────────
  function renderSubagentsInline(container, ad, project) {
    if (ad && ad.error) { container.appendChild(errorBanner(ad.error)); return; }
    const wfs = Array.isArray(ad.workflows) ? ad.workflows : [];
    const tasks = Array.isArray(ad.tasks) ? ad.tasks : [];
    const scripts = Array.isArray(ad.scripts) ? ad.scripts : [];
    const c = ad.counts || {};
    if (!wfs.length && !tasks.length && !scripts.length) {
      container.appendChild(el("div", { class: "loading", text: "No subagent or workflow invocations in this project." }));
      return;
    }
    const head = el("div", { class: "sessions-head" });
    const parts = [(c.workflows || 0) + " workflows", (c.workflow_agents || 0) + " agents", (c.tasks || 0) + " tasks"];
    if (c.scripts) parts.push(c.scripts + " scripts");
    head.appendChild(el("span", { text: parts.join(" · ") }));
    container.appendChild(head);

    for (const w of wfs) {
      const card = el("div", { class: "wf-card" });
      const top = el("div", { class: "wf-top" });
      top.appendChild(el("span", { class: "wf-name", text: w.name || "workflow" }));
      top.appendChild(el("span", { class: "badge " + wfStatusCls(w.status), text: w.status || "?" }));
      const agentList = Array.isArray(w.agents) ? w.agents : [];
      top.appendChild(el("span", { class: "wf-meta", text: (w.agent_count || agentList.length) + " agents" }));
      if (w.duration_ms != null) top.appendChild(el("span", { class: "wf-meta", text: fmtDur(w.duration_ms) }));
      if (w.model) top.appendChild(el("span", { class: "wf-meta mono", text: w.model }));
      if (w.started_iso) top.appendChild(el("span", { class: "wf-meta", text: shortIso(w.started_iso) }));
      card.appendChild(top);
      if (w.summary) card.appendChild(el("div", { class: "wf-summary", text: w.summary }));

      const agentWrap = el("div", { class: "wf-agents" });
      for (const a of agentList) {
        const chip = el("button", { class: "agent-chip" });
        if (a.phase) chip.appendChild(el("span", { class: "agent-phase", text: a.phase }));
        chip.appendChild(document.createTextNode(a.label || "agent"));
        chip.addEventListener("click", () => openSubagent(project.key, a.session_id || w.session_id, a.agent_id, w.run_id, a.label));
        agentWrap.appendChild(chip);
      }
      card.appendChild(agentWrap);
      container.appendChild(card);
    }

    if (tasks.length) {
      container.appendChild(el("div", { class: "sub-section", text: "Direct Task agents" }));
      for (const t of tasks) {
        const row = el("div", { class: "session-row" });
        const left = el("div", { class: "session-prompt" });
        left.appendChild(el("span", { class: "sid", text: (t.agent_id || "").slice(0, 8) }));
        left.appendChild(document.createTextNode(t.task || "(no task prompt)"));
        row.appendChild(left);
        row.appendChild(el("div", { class: "session-meta" }, [
          el("span", { class: "session-size", text: (t.size_h || "") + " · " + (t.mtime_iso ? shortIso(t.mtime_iso) : "") }),
        ]));
        row.addEventListener("click", () => openSubagent(project.key, t.session_id, t.agent_id, "", "Task " + (t.agent_id || "").slice(0, 8)));
        container.appendChild(row);
      }
    }

    if (scripts.length) {
      container.appendChild(el("div", { class: "sub-section", text: "Workflow scripts" }));
      for (const s of scripts) {
        const row = el("div", { class: "session-row" });
        const left = el("div", { class: "session-prompt" });
        left.appendChild(el("span", { class: "sid", text: "⚙" }));
        left.appendChild(document.createTextNode(s.name || s.file || "script"));
        if (s.run_id) left.appendChild(el("span", { class: "wf-meta mono", text: "  " + s.run_id }));
        row.appendChild(left);
        row.appendChild(el("div", { class: "session-meta" }, [
          el("span", { class: "session-size", text: (s.size_h || "") + " · " + (s.mtime_iso ? shortIso(s.mtime_iso) : "") }),
        ]));
        row.addEventListener("click", () => openScript(s, project));
        container.appendChild(row);
      }
    }
  }

  // View a workflow script's source in the drawer (reuses the text viewer).
  async function openScript(s, project) {
    setDrawerReload(() => openScript(s, project));
    openText(s.name || s.file || "script",
      (project && project.real_path ? project.real_path + " · " : "") + (s.run_id || s.file || ""),
      "Loading…");
    const r = await api("/api/file?rel=" + encodeURIComponent(s.rel));
    const txt = (r && r.content) ? r.content : (r && r.error ? "(" + r.error + ")" : "(empty)");
    const body = $("#drawer-body");
    body.textContent = "";
    body.appendChild(el("pre", { class: "raw-text", text: txt }));
  }

  // ───────────────────── Session runtime task outputs ─────────────────────
  function taskKindLabel(k) {
    return k === "bash" ? "BASH" : k === "agent" ? "AGENT" : "TASK";
  }

  // Drawer: list a session's background-task output buffers (from /tmp runtime).
  async function openSessionTasks(key, sessionId, realPath) {
    setDrawerReload(() => openSessionTasks(key, sessionId, realPath));
    const drawer = $("#drawer");
    drawer.classList.remove("hidden");
    $("#drawer-backdrop").classList.remove("hidden");
    drawer.setAttribute("aria-hidden", "false");
    $("#drawer-title").textContent = "Background tasks";
    $("#drawer-sub").textContent = (realPath ? realPath + " · " : "") + (sessionId || "");
    currentTranscriptData = null;
    setShareVisible(false);
    const body = $("#drawer-body");
    clear(body);
    body.appendChild(el("div", { class: "loading", text: "Loading task outputs…" }));

    const data = await api("/api/sessions/" + encodeURIComponent(key) + "/" +
      encodeURIComponent(sessionId) + "/tasks");
    clear(body);
    if (data && data.error) { body.appendChild(errorBanner(data.error)); return; }
    const tasks = Array.isArray(data.tasks) ? data.tasks : [];
    if (!tasks.length) {
      body.appendChild(el("div", { class: "loading", text: "No background task outputs for this session." }));
      return;
    }
    const head = el("div", { class: "sessions-head" });
    head.appendChild(el("span", { text: tasks.length + " task output" + (tasks.length === 1 ? "" : "s") }));
    head.appendChild(el("span", { class: "session-size", text: data.root || "" }));
    body.appendChild(head);

    const list = el("div", { class: "task-list" });
    for (const t of tasks) {
      const row = el("div", { class: "session-row" });
      const left = el("div", { class: "session-prompt" });
      left.appendChild(el("span", { class: "task-kind " + (t.kind || "other"), text: taskKindLabel(t.kind) }));
      left.appendChild(el("span", { class: "sid", text: t.task_id || "" }));
      row.appendChild(left);
      row.appendChild(el("div", { class: "session-meta" }, [
        el("span", {
          class: "session-size",
          text: (t.size_h || "") + " · " + (t.mtime_iso ? shortIso(t.mtime_iso) : ""),
        }),
      ]));
      row.addEventListener("click", () => openTaskOutput(key, sessionId, t, realPath));
      list.appendChild(row);
    }
    body.appendChild(list);
  }

  // Drawer: one task output buffer (redacted, size-capped). Pretty-printed and
  // syntax-highlighted by detected type (JSON / diff / plain).
  async function openTaskOutput(key, sessionId, t, realPath) {
    setDrawerReload(() => openTaskOutput(key, sessionId, t, realPath));
    openText(t.task_id || "task",
      (realPath ? realPath + " · " : "") + taskKindLabel(t.kind) + " · " + (t.size_h || ""),
      "Loading…");
    const url = "/api/sessions/" + encodeURIComponent(key) + "/" +
      encodeURIComponent(sessionId) + "/tasks/" + encodeURIComponent(t.task_id);
    const r = await api(url);
    const body = $("#drawer-body");
    body.textContent = "";
    if (r && r.error) { body.appendChild(errorBanner(r.error)); return; }
    renderRichContent(body, (r && r.content != null) ? r.content : "",
      { truncated: !!(r && r.truncated), size: r && r.size });
  }

  // ─────────── Rich content viewer: pretty-print + highlight by type ───────────
  // Reusable: detects JSON (pretty-print + token highlight) and unified diffs
  // (per-line +/-/@@ coloring), else plain text. Safe by construction — every
  // value reaches the DOM via el({text}) / textContent, never innerHTML of
  // untrusted text. A small header strip shows the detected format + line count
  // (+ a truncation note when the payload was capped server-side).
  function renderRichContent(body, text, opts) {
    opts = opts || {};
    const raw = String(text == null ? "" : text);
    const trimmed = raw.trim();
    let fmt = "text";
    let contentNode = null;

    // JSON → pretty-print + highlight (parse must fully succeed; a truncated
    // buffer fails here and correctly falls through to plain text).
    if (trimmed && (trimmed[0] === "{" || trimmed[0] === "[")) {
      try {
        const obj = JSON.parse(trimmed);
        if (obj && typeof obj === "object") {
          fmt = "json";
          const pre = el("pre", { class: "raw-text code-view" });
          const frag = document.createDocumentFragment();
          appendJson(frag, obj, "");
          pre.appendChild(frag);
          contentNode = pre;
        }
      } catch (e) { /* not valid JSON */ }
    }
    if (!contentNode && looksLikeDiff(trimmed)) {
      fmt = "diff";
      contentNode = renderDiff(raw);
    }
    if (!contentNode) {
      contentNode = el("pre", { class: "raw-text", text: raw === "" ? "(empty)" : raw });
    }

    const lines = raw === "" ? 0 : raw.split("\n").length;
    const head = el("div", { class: "content-head" });
    head.appendChild(el("span", { class: "fmt-chip fmt-" + fmt, text: fmt.toUpperCase() }));
    head.appendChild(el("span", { class: "content-meta", text: fmtNum(lines) + " line" + (lines === 1 ? "" : "s") }));
    if (opts.truncated) {
      head.appendChild(el("span", {
        class: "content-meta trunc",
        text: "truncated · first 256 KB of " + (opts.size ? fmtNum(opts.size) + " B" : "file"),
      }));
    }
    body.appendChild(head);
    body.appendChild(contentNode);
  }

  function jSpan(cls, text) { return el("span", { class: "j-" + cls, text: text }); }

  // A string value worth rendering as a rich block rather than an escaped
  // one-liner: long prose or anything multi-line (markdown reports, embedded
  // diffs, logs) that would otherwise force horizontal scrolling.
  function isRichString(s) { return typeof s === "string" && (s.length > 100 || s.indexOf("\n") >= 0); }

  // Recursively append an indented, syntax-highlighted JSON value to a fragment.
  // `rich` (default true): long/multi-line STRING values render as a markdown /
  // diff block (great for workflow-result prose). Pass rich=false for a STRICT
  // JSON view (e.g. settings.json) where every string stays an inline quoted
  // literal. Returns true when it emitted a block element (so the caller drops
  // the inline trailing comma, which would otherwise dangle on its own line).
  function appendJson(frag, value, indent, rich) {
    if (rich === undefined) rich = true;
    const pad = indent + "  ";
    if (value === null) { frag.appendChild(jSpan("null", "null")); return false; }
    const ty = typeof value;
    if (ty === "number") { frag.appendChild(jSpan("num", String(value))); return false; }
    if (ty === "boolean") { frag.appendChild(jSpan("bool", String(value))); return false; }
    if (ty === "string") {
      if (rich && isRichString(value)) {
        const block = el("div", { class: "j-richstr" });
        renderRichString(block, value);
        frag.appendChild(block);
        return true;
      }
      frag.appendChild(jSpan("str", JSON.stringify(value)));
      return false;
    }
    if (Array.isArray(value)) {
      if (!value.length) { frag.appendChild(document.createTextNode("[]")); return false; }
      frag.appendChild(document.createTextNode("[\n"));
      value.forEach((item, i) => {
        frag.appendChild(document.createTextNode(pad));
        const blk = appendJson(frag, item, pad, rich);
        const sep = i < value.length - 1 ? "," : "";
        frag.appendChild(document.createTextNode(blk ? "\n" : sep + "\n"));
      });
      frag.appendChild(document.createTextNode(indent + "]"));
      return false;
    }
    if (ty === "object") {
      const keys = Object.keys(value);
      if (!keys.length) { frag.appendChild(document.createTextNode("{}")); return false; }
      frag.appendChild(document.createTextNode("{\n"));
      keys.forEach((k, i) => {
        frag.appendChild(document.createTextNode(pad));
        frag.appendChild(jSpan("key", JSON.stringify(k)));
        frag.appendChild(document.createTextNode(": "));
        const blk = appendJson(frag, value[k], pad, rich);
        const sep = i < keys.length - 1 ? "," : "";
        frag.appendChild(document.createTextNode(blk ? "\n" : sep + "\n"));
      });
      frag.appendChild(document.createTextNode(indent + "}"));
      return false;
    }
    frag.appendChild(document.createTextNode(String(value)));
    return false;
  }

  // Build a standalone highlighted JSON viewer node from a parsed object.
  // rich=false → strict JSON (no markdown-ifying of long strings).
  function jsonViewer(obj, rich) {
    const pre = el("pre", { class: "json-viewer" });
    const frag = document.createDocumentFragment();
    appendJson(frag, obj, "", rich !== false);
    pre.appendChild(frag);
    return pre;
  }

  // ─────────── Rich string → markdown / embedded-diff renderer ───────────
  // Used for rich JSON string values. Safe by construction (DOM nodes only).
  function renderRichString(container, text) {
    const t = String(text);
    if (looksLikeDiff(t.trim())) { container.appendChild(renderDiff(t)); return; }
    renderMarkdown(container, t);
  }

  // Safe markdown subset (DOM-only): fenced code, ATX headings, GFM tables,
  // nested bullet/number lists, horizontal rules, blank-line paragraphs, and
  // inline **bold** / `code` / [links](http…).
  function renderMarkdown(container, text) {
    const lines = String(text).replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    const isList = (l) => /^\s*([-*+]|\d+[.)])\s+/.test(l);
    const isHr = (l) => /^\s{0,3}([-*_])\1{2,}\s*$/.test(l) && l.indexOf("|") < 0;
    const isTableStart = (idx) => lines[idx].indexOf("|") >= 0 &&
      idx + 1 < lines.length && mdIsTableSep(lines[idx + 1]);
    const isSpecial = (l, idx) => /^\s*```/.test(l) || /^#{1,6}\s/.test(l) ||
      isList(l) || isHr(l) || isTableStart(idx);
    while (i < lines.length) {
      const ln = lines[i];

      if (/^\s*```/.test(ln)) {                       // fenced code block
        i++;
        const code = [];
        while (i < lines.length && !/^\s*```/.test(lines[i])) { code.push(lines[i]); i++; }
        i++;                                          // skip closing fence
        container.appendChild(el("pre", { class: "raw-text md-code", text: code.join("\n") }));
        continue;
      }

      const h = ln.match(/^(#{1,6})\s+(.*)$/);        // heading
      if (h) {
        const node = el("div", { class: "md-h md-h" + h[1].length });
        appendInline(node, h[2]);
        container.appendChild(node);
        i++; continue;
      }

      if (isTableStart(i)) {                          // GFM table
        const header = mdSplitRow(ln);
        const aligns = mdSplitRow(lines[i + 1]).map((c) => {
          const l = c.startsWith(":"), r = c.endsWith(":");
          return (l && r) ? "center" : r ? "right" : l ? "left" : "";
        });
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].indexOf("|") >= 0 &&
               lines[i].trim() && !/^\s*```/.test(lines[i])) {
          rows.push(mdSplitRow(lines[i])); i++;
        }
        container.appendChild(buildMdTable(header, aligns, rows));
        continue;
      }

      if (isHr(ln)) { container.appendChild(el("hr", { class: "md-hr" })); i++; continue; }

      if (isList(ln)) {                               // (possibly nested) list
        const block = [];
        while (i < lines.length && isList(lines[i])) {
          const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
          block.push({ indent: m[1].length, ordered: /\d/.test(m[2]), text: m[3] });
          i++;
        }
        container.appendChild(buildNestedList(block));
        continue;
      }

      if (!ln.trim()) { i++; continue; }              // blank → paragraph break

      const para = [ln]; i++;                         // paragraph: gather plain lines
      while (i < lines.length && lines[i].trim() && !isSpecial(lines[i], i)) {
        para.push(lines[i]); i++;
      }
      const p = el("div", { class: "md-p" });
      appendInline(p, para.join("\n"));
      container.appendChild(p);
    }
  }

  // Split a table row into trimmed cells, dropping the leading/trailing pipes.
  function mdSplitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((c) => c.trim());
  }
  // A GFM separator row: every cell is `:?-+:?` (handles `| - | - |` too).
  function mdIsTableSep(line) {
    if (!line || line.indexOf("|") < 0) return false;
    const cells = mdSplitRow(line);
    return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c));
  }
  function buildMdTable(header, aligns, rows) {
    const styleFor = (idx) => aligns[idx] ? { attrs: { style: "text-align:" + aligns[idx] } } : {};
    const table = el("table", { class: "md-table" });
    const htr = el("tr");
    header.forEach((c, idx) => { const th = el("th", styleFor(idx)); appendInline(th, c); htr.appendChild(th); });
    table.appendChild(el("thead", {}, [htr]));
    const tbody = el("tbody");
    for (const r of rows) {
      const tr = el("tr");
      for (let idx = 0; idx < header.length; idx++) {
        const td = el("td", styleFor(idx));
        appendInline(td, r[idx] !== undefined ? r[idx] : "");
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    return el("div", { class: "md-table-wrap" }, [table]);   // wrap → horizontal scroll
  }
  // Build a nested <ul>/<ol> from list items carrying their leading-indent width.
  function buildNestedList(items) {
    const root = el(items[0].ordered ? "ol" : "ul", { class: "md-list" });
    const stack = [{ indent: items[0].indent, listEl: root, lastLi: null }];
    for (const it of items) {
      let top = stack[stack.length - 1];
      if (it.indent > top.indent && top.lastLi) {
        const sub = el(it.ordered ? "ol" : "ul", { class: "md-list md-sublist" });
        top.lastLi.appendChild(sub);
        stack.push({ indent: it.indent, listEl: sub, lastLi: null });
        top = stack[stack.length - 1];
      } else {
        while (stack.length > 1 && it.indent < top.indent) { stack.pop(); top = stack[stack.length - 1]; }
      }
      const li = el("li");
      appendInline(li, it.text);
      top.listEl.appendChild(li);
      top.lastLi = li;
    }
    return root;
  }

  // Inline markdown → DOM. Only **bold**, `code`, and safe [text](http(s)://…)
  // links (fragile single * / _ italics are skipped to avoid mangling prose
  // and identifiers). href is gated to http(s) so a javascript: URI can't slip in.
  function appendInline(parent, text) {
    const s = String(text);
    const re = /(`[^`]+`)|(\*\*[^*]+?\*\*)|(\[[^\]]+\]\([^)\s]+\))/g;
    let last = 0, m;
    while ((m = re.exec(s)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(s.slice(last, m.index)));
      const tok = m[0];
      if (m[1]) parent.appendChild(el("code", { class: "md-ic", text: tok.slice(1, -1) }));
      else if (m[2]) parent.appendChild(el("strong", { class: "md-b", text: tok.slice(2, -2) }));
      else {
        const mm = tok.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
        if (mm && /^https?:\/\//i.test(mm[2])) {
          parent.appendChild(el("a", { class: "md-link", text: mm[1],
            attrs: { href: mm[2], target: "_blank", rel: "noopener noreferrer" } }));
        } else {
          parent.appendChild(document.createTextNode(tok));
        }
      }
      last = re.lastIndex;
    }
    if (last < s.length) parent.appendChild(document.createTextNode(s.slice(last)));
  }

  function looksLikeDiff(t) {
    if (!t) return false;
    if (/^diff --git /m.test(t)) return true;
    if (/^@@ .* @@/m.test(t) && /^[+-]/m.test(t)) return true;
    if (/^--- /m.test(t) && /^\+\+\+ /m.test(t)) return true;
    return false;
  }

  function renderDiff(text) {
    const pre = el("pre", { class: "raw-text diff-view" });
    const lines = String(text).split("\n");
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      let cls = "d-ctx";
      if (ln.startsWith("+++") || ln.startsWith("---")) cls = "d-file";
      else if (ln.startsWith("@@")) cls = "d-hunk";
      else if (ln.startsWith("diff ") || ln.startsWith("index ") ||
               ln.startsWith("new file") || ln.startsWith("deleted file") ||
               ln.startsWith("rename ") || ln.startsWith("similarity ")) cls = "d-meta";
      else if (ln[0] === "+") cls = "d-add";
      else if (ln[0] === "-") cls = "d-del";
      pre.appendChild(el("span", { class: cls, text: ln + (i < lines.length - 1 ? "\n" : "") }));
    }
    return pre;
  }

  function wfStatusCls(s) {
    switch (String(s)) {
      case "completed": return "green";
      case "failed": case "error": return "red";
      case "running": return "blue";
      default: return "off";
    }
  }

  function fmtDur(ms) {
    if (typeof ms !== "number") return "";
    if (ms < 1000) return ms + "ms";
    let s = Math.round(ms / 1000);          // integer seconds avoids 59.5s carry
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    s = s % 60;
    return m + "m" + (s ? " " + s + "s" : "");
  }

  async function openSubagent(key, sessionId, agentId, runId, label) {
    setDrawerReload(() => openSubagent(key, sessionId, agentId, runId, label));
    const drawer = $("#drawer");
    drawer.classList.remove("hidden");
    $("#drawer-backdrop").classList.remove("hidden");
    drawer.setAttribute("aria-hidden", "false");
    $("#drawer-title").textContent = label || ("agent " + (agentId || "").slice(0, 8));
    $("#drawer-sub").textContent = (runId ? runId + " · " : "") + (agentId || "");
    const body = $("#drawer-body");
    clear(body);
    body.appendChild(el("div", { class: "loading", text: "Loading subagent transcript…" }));
    const url = "/api/subagents/" + encodeURIComponent(key) + "/" + encodeURIComponent(sessionId) +
      "/" + encodeURIComponent(agentId) + "?run=" + encodeURIComponent(runId || "") + "&limit=2000";
    const data = await api(url);
    clear(body);
    renderTranscript(body, data);
  }

  // Lightweight text viewer reusing the drawer (memory files, MEMORY.md).
  function openText(title, sub, text) {
    const drawer = $("#drawer");
    drawer.classList.remove("hidden");
    $("#drawer-backdrop").classList.remove("hidden");
    drawer.setAttribute("aria-hidden", "false");
    $("#drawer-title").textContent = title || "";
    $("#drawer-sub").textContent = sub || "";
    currentTranscriptData = null;                 // not a transcript
    setShareVisible(false);
    const body = $("#drawer-body");
    clear(body);
    body.appendChild(el("pre", { class: "raw-text", text: text || "" }));
  }

  // ───────────────────── Transcript drawer ─────────────────────
  async function openTranscript(key, sessionId, prompt, realPath, tok0) {
    setDrawerReload(() => openTranscript(key, sessionId, prompt, realPath, tok0));
    const drawer = $("#drawer");
    const backdrop = $("#drawer-backdrop");
    drawer.classList.remove("hidden");
    backdrop.classList.remove("hidden");
    drawer.setAttribute("aria-hidden", "false");
    $("#drawer-title").textContent = (sessionId || "session");
    $("#drawer-sub").textContent = realPath || key || "";
    const body = $("#drawer-body");
    clear(body);
    body.appendChild(el("div", { class: "loading", text: "Loading transcript…" }));

    const data = await api("/api/sessions/" + encodeURIComponent(key) + "/" + encodeURIComponent(sessionId) + "?limit=2000");
    clear(body);
    renderTranscript(body, data, tok0);
  }

  // Render a transcript payload (session OR subagent) into the drawer body.
  function renderTranscript(body, data, tok0) {
    if (data && data.error) { body.appendChild(errorBanner(data.error)); return; }
    const msgs = Array.isArray(data.messages) ? data.messages : [];
    // Remember the payload so the share button can render it to a long PNG.
    currentTranscriptData = data && msgs.length ? data : null;
    setShareVisible(!!currentTranscriptData);

    // Token summary bar (authoritative whole-transcript roll-up).
    const stok = (data && data.tokens) || tok0 || null;
    if (stok && stok.total > 0) {
      const bar = el("div", { class: "tokbar" });
      bar.appendChild(el("span", { class: "tokbar-total", text: fmtTok(stok.total) + " tokens" }));
      const parts = [["in", stok.input], ["out", stok.output],
        ["cache r", stok.cache_read], ["cache w", stok.cache_creation]];
      for (const [lbl, v] of parts) {
        const seg = el("span", { class: "tokbar-seg" });
        seg.appendChild(el("span", { class: "tokbar-k", text: lbl }));
        seg.appendChild(document.createTextNode(" " + fmtTok(v || 0)));
        bar.appendChild(seg);
      }
      bar.appendChild(el("span", { class: "tokbar-seg muted", text: (stok.messages || 0) + " replies" }));
      body.appendChild(bar);
    }

    if (!msgs.length) { body.appendChild(el("div", { class: "loading", text: "Empty transcript." })); return; }

    // When the session is longer than the cap we keep the most-recent messages.
    if (data && data.truncated) {
      body.appendChild(el("div", { class: "content-meta trunc",
        text: "long session — showing the most recent " + msgs.length + " messages" }));
    }

    // Collect the genuine user questions (is_prompt) for the navigator + jump.
    const prompts = [];
    for (let i = 0; i < msgs.length; i++) if (msgs[i] && msgs[i].is_prompt) prompts.push(i);

    if (prompts.length) {
      const nav = el("div", { class: "prompt-nav" });
      nav.appendChild(el("div", { class: "prompt-nav-head", text: "❓ You asked · " + prompts.length }));
      const list = el("div", { class: "prompt-nav-list" });
      prompts.forEach((mi, n) => {
        const item = el("button", { class: "prompt-nav-item" });
        item.appendChild(el("span", { class: "prompt-nav-num", text: (n + 1) }));
        item.appendChild(document.createTextNode(oneLine(msgs[mi].text, 96)));
        item.addEventListener("click", () => jumpToMsg("tx-msg-" + mi));
        list.appendChild(item);
      });
      nav.appendChild(list);
      body.appendChild(nav);
    }

    for (let i = 0; i < msgs.length; i++) {
      const m = msgs[i];
      const role = m.role || "system";
      const isPrompt = !!m.is_prompt;
      const isNoiseUser = role === "user" && !isPrompt;   // tool result / cmd stdout
      let cls = "msg " + (role === "user" || role === "assistant" ? role : "system");
      if (isPrompt) cls += " prompt";
      else if (isNoiseUser) cls += " muted";
      const wrap = el("div", { class: cls });
      wrap.id = "tx-msg-" + i;

      const meta = el("div", { class: "msg-meta" });
      if (isPrompt) meta.appendChild(el("span", { class: "prompt-marker", text: "▶ you asked" }));
      else meta.appendChild(el("span", { class: "msg-role", text: role }));
      if (m.model) meta.appendChild(el("span", { class: "badge accent", text: m.model }));
      if (typeof m.tokens === "number" && m.tokens > 0)
        meta.appendChild(el("span", { class: "badge tok", text: "◆ " + fmtTok(m.tokens) }));
      if (m.ts_iso) meta.appendChild(el("span", { text: shortIso(m.ts_iso) }));
      wrap.appendChild(meta);

      // text rendered via textContent — never innerHTML of user/session text.
      const bubble = el("div", { class: "bubble" });
      bubble.textContent = m.text || "";
      wrap.appendChild(bubble);

      const blocks = Array.isArray(m.blocks) ? m.blocks : [];
      if (blocks.length) {
        const bt = el("div", { class: "block-tags" });
        for (const b of blocks) {
          let cls2 = "badge";
          if (typeof b === "string") {
            if (b.indexOf("tool_use") === 0) cls2 = "badge blue";
            else if (b.indexOf("tool_result") === 0) cls2 = "badge yellow";
            else if (b === "thinking") cls2 = "badge purple";
          }
          bt.appendChild(el("span", { class: cls2, text: b }));
        }
        wrap.appendChild(bt);
      }
      body.appendChild(wrap);
    }
    if (data.truncated) body.appendChild(el("div", { class: "loading", text: "Transcript truncated to first 400 records." }));
  }

  function oneLine(t, n) {
    t = String(t == null ? "" : t).replace(/\s+/g, " ").trim();
    n = n || 90;
    return t.length > n ? t.slice(0, n) + "…" : t;
  }

  function jumpToMsg(id) {
    const target = document.getElementById(id);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("flash-prompt");
    setTimeout(() => target.classList.remove("flash-prompt"), 1200);
  }

  function closeDrawer() {
    $("#drawer").classList.add("hidden");
    $("#drawer-backdrop").classList.add("hidden");
    $("#drawer").setAttribute("aria-hidden", "true");
    currentTranscriptData = null;
    setShareVisible(false);
    setDrawerReload(null);
  }

  // Register (or clear) the reload closure for the open drawer and toggle the ⟳
  // button. Each drawer opener calls this with a thunk that re-opens itself.
  function setDrawerReload(fn) {
    currentReload = (typeof fn === "function") ? fn : null;
    const b = $("#drawer-refresh");
    if (b) b.classList.toggle("hidden", !currentReload);
  }

  // ───────────────────── Long-screenshot share ─────────────────────

  function setShareVisible(show) {
    const b = $("#drawer-share");
    if (b) b.classList.toggle("hidden", !show);
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = el("a", { attrs: { href: url, download: filename } });
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { a.remove(); URL.revokeObjectURL(url); }, 1500);
  }

  // Wrap `text` to `maxW` px under `font` (mc is a measuring 2d context).
  // Splits on newlines first, then words, falling back to per-character for
  // long unbroken runs (CJK / URLs) so nothing overflows.
  function wrapLines(mc, text, maxW, font) {
    mc.font = font;
    const out = [];
    const paras = String(text == null ? "" : text).replace(/\t/g, "    ").split("\n");
    for (const para of paras) {
      if (para === "") { out.push(""); continue; }
      let line = "";
      for (const word of para.split(/(\s+)/)) {
        if (word === "") continue;
        if (mc.measureText(line + word).width <= maxW) { line += word; continue; }
        if (line) { out.push(line.replace(/\s+$/, "")); line = ""; }
        if (mc.measureText(word).width <= maxW) { line = word; continue; }
        let chunk = "";                      // break a too-long token by char
        for (const ch of word) {
          if (mc.measureText(chunk + ch).width <= maxW) chunk += ch;
          else { if (chunk) out.push(chunk); chunk = ch; }
        }
        line = chunk;
      }
      if (line) out.push(line.replace(/\s+$/, ""));
    }
    return out;
  }

  function shareTranscript() {
    const data = currentTranscriptData;
    if (!data || !Array.isArray(data.messages) || !data.messages.length) {
      toast("Open a conversation first", true);
      return;
    }
    try {
      const blob0 = buildTranscriptImage(data,
        ($("#drawer-title").textContent || "session").trim(),
        ($("#drawer-sub").textContent || "").trim());
      blob0.then((res) => {
        downloadBlob(res.blob, res.filename);
        toast("Saved " + res.filename + (res.clipped ? " (clipped to fit)" : ""));
      }).catch(() => toast("Could not render image", true));
    } catch (e) {
      toast("Could not render image", true);
    }
  }

  // Render the conversation to a tall PNG (a "long screenshot") on a canvas.
  // Returns a Promise<{blob, filename, clipped}>. Pure client-side, no deps.
  function buildTranscriptImage(data, title, sub) {
    const msgs = data.messages;
    const SCALE = 2;            // crisp on retina
    const W = 900, PAD = 26, CW = W - PAD * 2;
    const MAXH = 14000;        // logical px cap (×SCALE stays within canvas limits)
    const C = {
      bg: "#0b0e13", panel: "#13161d", panelMute: "#0f1218", line: "#242935",
      fg: "#e9eaee", dim: "#b7bdc8", mute: "#7e8593",
      accent: "#d97757", accentSoft: "rgba(217,119,87,.12)", accentLine: "rgba(217,119,87,.45)",
      user: "#5aa6e8", asst: "#54c98a",
    };
    const F = {
      brand: "700 12px ui-monospace,Menlo,monospace",
      title: "700 21px -apple-system,system-ui,Segoe UI,sans-serif",
      sub: "12px ui-monospace,Menlo,monospace",
      meta: "11.5px ui-monospace,Menlo,monospace",
      role: "700 11px ui-monospace,Menlo,monospace",
      body: "14px -apple-system,system-ui,Segoe UI,sans-serif",
      mono: "12px ui-monospace,Menlo,monospace",
      small: "11px ui-monospace,Menlo,monospace",
    };
    const mc = document.createElement("canvas").getContext("2d");

    const NOISE = ["<command-name>", "<local-command-stdout>", "<task-notification>",
      "<system-reminder>", "<bash-"];
    function classify(m) {
      if (m.is_prompt) return "prompt";
      if (m.role === "assistant") return "assistant";
      if (m.role === "user") return "noise";   // tool result / command output
      return "system";
    }
    function bodyFor(m, kind) {
      let t = String(m.text == null ? "" : m.text);
      const cap = kind === "prompt" ? 2400 : kind === "assistant" ? 1800
        : kind === "system" ? 500 : 240;
      if (t.length > cap) t = t.slice(0, cap) + " …";
      return t;
    }

    // ---- layout pass: build a flat list of draw ops, measuring heights ----
    const ops = [];
    let y = 0;
    const push = (h, draw) => { ops.push({ y, h, draw }); y += h; };

    // header block
    const titleLines = wrapLines(mc, title, CW, F.title);
    const subLines = wrapLines(mc, sub, CW, F.sub);
    const prompts = [];
    msgs.forEach((m) => { if (m.is_prompt) prompts.push(m.text || ""); });
    const tok = data.tokens || null;
    const metaTxt = [
      msgs.length + " messages",
      (data.prompt_count != null ? data.prompt_count : prompts.length) + " questions",
      tok && tok.total ? fmtTok(tok.total) + " tokens" : null,
      nowStamp(),
    ].filter(Boolean).join("   ·   ");

    // pre-wrap the "you asked" list
    const askWrapped = prompts.map((p, i) =>
      wrapLines(mc, (i + 1) + ".  " + oneLine(p, 4000), CW - 28, F.body));
    let askH = 0;
    if (prompts.length) {
      askH = 14 + 22;                 // top pad + header line
      askWrapped.forEach((ls) => { askH += ls.length * 20 + 6; });
      askH += 12;                     // bottom pad
    }
    const headH = 16 + 18 + 10 + titleLines.length * 27 + 6 +
      subLines.length * 17 + 10 + 20 + 16 + (askH ? askH + 12 : 0) + 14;

    push(headH, (ctx, top) => {
      let yy = top + 16;
      ctx.fillStyle = C.accent; ctx.font = F.brand;
      ctx.fillText("⌁ CLAUDE CONSOLE — conversation", PAD, yy + 12); yy += 18 + 10;
      ctx.fillStyle = C.fg; ctx.font = F.title;
      for (const ln of titleLines) { ctx.fillText(ln, PAD, yy + 18); yy += 27; }
      yy += 6; ctx.fillStyle = C.mute; ctx.font = F.sub;
      for (const ln of subLines) { ctx.fillText(ln, PAD, yy + 12); yy += 17; }
      yy += 10; ctx.fillStyle = C.dim; ctx.font = F.meta;
      ctx.fillText(metaTxt, PAD, yy + 12); yy += 20 + 16;
      if (askH) {
        const boxTop = yy, boxH = askH;
        roundRect(ctx, PAD, boxTop, CW, boxH, 10);
        ctx.fillStyle = C.accentSoft; ctx.fill();
        ctx.strokeStyle = C.accentLine; ctx.lineWidth = 1; ctx.stroke();
        let ay = boxTop + 14;
        ctx.fillStyle = C.accent; ctx.font = F.role;
        ctx.fillText("❓ YOU ASKED · " + prompts.length, PAD + 14, ay + 12); ay += 22;
        ctx.font = F.body;
        askWrapped.forEach((ls) => {
          ctx.fillStyle = C.dim;
          for (const ln of ls) { ctx.fillText(ln, PAD + 14, ay + 13); ay += 20; }
          ay += 6;
        });
      }
    });

    // message cards
    let clipped = false;
    for (let i = 0; i < msgs.length; i++) {
      const m = msgs[i];
      const kind = classify(m);
      const isMono = kind === "noise" || kind === "system";
      const bodyFont = isMono ? F.mono : F.body;
      const bodyLH = isMono ? 17 : 20;
      const text = bodyFor(m, kind);
      const innerW = CW - 28;
      const lines = wrapLines(mc, text, innerW, bodyFont);
      const roleH = 20;
      const cardH = 12 + roleH + 6 + lines.length * bodyLH + 14;
      const gap = 10;
      if (y + cardH + gap > MAXH) {
        clipped = true;
        const left = msgs.length - i;
        push(34, (ctx, top) => {
          ctx.fillStyle = C.mute; ctx.font = F.small;
          ctx.fillText("… " + left + " more message" + (left === 1 ? "" : "s") +
            " — open Claude Console to see the full conversation", PAD, top + 20);
        });
        break;
      }
      push(cardH + gap, (ctx, top) => {
        const x = PAD, w = CW, ct = top, h = cardH;
        // card background
        if (kind === "prompt") {
          roundRect(ctx, x, ct, w, h, 9); ctx.fillStyle = C.accentSoft; ctx.fill();
          ctx.fillStyle = C.accent; ctx.fillRect(x, ct, 3, h);
        } else if (kind === "assistant") {
          roundRect(ctx, x, ct, w, h, 9); ctx.fillStyle = C.panel; ctx.fill();
        } else {
          roundRect(ctx, x, ct, w, h, 9); ctx.fillStyle = C.panelMute; ctx.fill();
        }
        // role header
        let hx = x + 14; const hy = ct + 12;
        ctx.font = F.role;
        let label, lc;
        if (kind === "prompt") { label = "▶ YOU ASKED"; lc = C.accent; }
        else if (kind === "assistant") { label = "ASSISTANT"; lc = C.asst; }
        else if (kind === "noise") { label = "OUTPUT"; lc = C.mute; }
        else { label = "SYSTEM"; lc = C.mute; }
        ctx.fillStyle = lc; ctx.fillText(label, hx, hy + 12);
        hx += ctx.measureText(label).width + 12;
        ctx.font = F.small; ctx.fillStyle = C.mute;
        const tags = [];
        if (m.model) tags.push(m.model);
        if (typeof m.tokens === "number" && m.tokens > 0) tags.push("◆ " + fmtTok(m.tokens));
        if (m.ts_iso) tags.push(shortIso(m.ts_iso));
        if (tags.length) ctx.fillText(tags.join("   "), hx, hy + 12);
        // body
        ctx.font = bodyFont;
        ctx.fillStyle = kind === "noise" ? C.mute : (kind === "prompt" ? C.fg : C.dim);
        let by = ct + 12 + roleH + 6;
        for (const ln of lines) { ctx.fillText(ln, x + 14, by + (isMono ? 12 : 13)); by += bodyLH; }
      });
    }

    // footer
    push(30, (ctx, top) => {
      ctx.fillStyle = C.mute; ctx.font = F.small;
      ctx.fillText("Generated " + nowStamp() + " · Claude Console", PAD, top + 18);
    });

    const totalH = Math.ceil(y + 6);
    const canvas = document.createElement("canvas");
    canvas.width = W * SCALE;
    canvas.height = totalH * SCALE;
    const ctx = canvas.getContext("2d");
    ctx.scale(SCALE, SCALE);
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, totalH);
    for (const op of ops) op.draw(ctx, op.y);

    const slug = (sub || title || "session").split("/").pop()
      .replace(/[^A-Za-z0-9._-]+/g, "-").slice(0, 40) || "session";
    const filename = "claude-" + slug + "-" + ymd() + ".png";
    return new Promise((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (blob) resolve({ blob, filename, clipped });
        else reject(new Error("toBlob failed"));
      }, "image/png");
    });
  }

  function roundRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  function ymd() {
    const d = new Date();
    return d.getFullYear() + pad2(d.getMonth() + 1) + pad2(d.getDate());
  }
  function nowStamp() {
    const d = new Date();
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
      " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  // ───────────────────── History ─────────────────────
  // History → session jump. Claude Code sanitizes a cwd into a projects/<key>
  // directory by replacing every non-alphanumeric char with '-'. Verified exact
  // against the on-disk dirs (1257/1257 rows whose session still exists). We only
  // light up the jump when that key is a REAL project dir, so dead history rows
  // (session pruned or never saved to disk) stay inert instead of opening an
  // empty/error drawer.
  let projectKeySet = null;
  function cwdToProjectKey(cwd) {
    return String(cwd || "").replace(/[^A-Za-z0-9]/g, "-");
  }
  async function ensureProjectKeys() {
    if (projectKeySet) return projectKeySet;
    const data = (cache.projects && !cache.projects.error)
      ? cache.projects : await api("/api/projects");
    const items = (data && Array.isArray(data.items)) ? data.items : [];
    projectKeySet = new Set(items.map((p) => p.key));
    return projectKeySet;
  }

  async function loadHistory(reset) {
    if (reset) {
      historyState.offset = 0; historyState.items = [];
      if (!projectKeySet) await ensureProjectKeys();
    }
    const qs = "?limit=" + historyState.limit + "&offset=" + historyState.offset +
      "&q=" + encodeURIComponent(historyState.q || "");
    const data = await api("/api/history" + qs);
    if (data && !data.error) {
      historyState.total = data.total || 0;
      const items = Array.isArray(data.items) ? data.items : [];
      historyState.items = reset ? items : historyState.items.concat(items);
    }
    return data;
  }

  let histDebounce;
  function renderHistory(content, d) {
    // preserve focus/caret across re-render
    const prev = document.activeElement;
    const refocus = prev && prev.classList && prev.classList.contains("hist-input");
    const caret = refocus ? prev.selectionStart : null;

    // search bar
    const bar = el("div", { class: "hist-search" });
    const input = el("input", {
      class: "hist-input",
      attrs: { type: "text", placeholder: "Search prompt history…", value: historyState.q, spellcheck: "false" },
    });
    input.addEventListener("input", () => {
      clearTimeout(histDebounce);
      historyState.q = input.value;
      histDebounce = setTimeout(async () => {
        const data = await loadHistory(true);
        cache.history = data;
        if (active === "history") renderHistory(clearAndGet(), data);
      }, 280);
    });
    bar.appendChild(input);
    const countLbl = el("span", { class: "hist-count", text: historyState.total + " match" + (historyState.total === 1 ? "" : "es") });
    bar.appendChild(countLbl);
    content.appendChild(bar);

    if (refocus) {
      input.focus();
      try { if (caret !== null) input.setSelectionRange(caret, caret); } catch (e) {}
    }

    if (d && d.error) { content.appendChild(errorBanner(d.error)); return; }

    const items = historyState.items;
    if (!items.length) { content.appendChild(emptyState("⏲", historyState.q ? "No history matching “" + historyState.q + "”." : "No prompt history.")); return; }

    const list = el("div", { class: "hist-list" });
    for (const it of items) {
      // Jumpable only when the entry has a session id AND its cwd maps to a real
      // project dir we know about (see projectKeySet note above).
      const key = cwdToProjectKey(it.project);
      const jumpable = !!(it.session_id && projectKeySet && projectKeySet.has(key));
      const row = el("div", { class: "hist-row" + (jumpable ? " hist-row--link" : "") });
      row.appendChild(el("div", { class: "hist-time mono", text: it.ts_iso ? shortIso(it.ts_iso) : "" }));
      row.appendChild(el("div", { class: "hist-proj mono", text: it.project || "", title: it.project || "" }));
      // display is redacted server-side; render via textContent.
      row.appendChild(el("div", { class: "hist-disp", text: it.display || "" }));
      if (jumpable) {
        const sid8 = (it.session_id || "").slice(0, 8);
        row.title = "↪ Open session " + sid8 + " — view transcript";
        row.setAttribute("role", "button");
        row.setAttribute("tabindex", "0");
        // Pass the cwd as the drawer subtitle; transcript fetch carries the
        // authoritative token roll-up, so tok0 is null.
        const go = () => openTranscript(key, it.session_id, it.display, it.project, null);
        row.addEventListener("click", go);
        row.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
        });
      }
      list.appendChild(row);
    }
    content.appendChild(list);

    if (historyState.items.length < historyState.total) {
      const more = el("button", { class: "load-more", text: "Load more (" + historyState.items.length + " / " + historyState.total + ")", attrs: { type: "button" } });
      more.addEventListener("click", async () => {
        historyState.offset = historyState.items.length;
        more.textContent = "Loading…";
        const data = await loadHistory(false);
        cache.history = data;
        if (active === "history") renderHistory(clearAndGet(), data);
      });
      content.appendChild(more);
    }
  }

  // helper to re-render history into a freshly-cleared content node
  function clearAndGet() { const c = $("#content"); clear(c); return c; }

  // ───────────────────── Overview counts → nav badges ─────────────────────
  async function refreshOverviewCounts() {
    const data = await api("/api/overview");
    if (data && !data.error) { lastOverview = data; cache.overview = data; applyNavCounts(data); }
  }

  function applyNavCounts(ov) {
    if (!ov || ov.error) return;
    const c = ov.counts || {};
    const map = {
      skills: c.skills, plugins: c.plugins, agents: c.agents,
      commands: c.commands, projects: c.projects, history: c.history_entries,
      plans: c.plans,
    };
    for (const id in map) {
      const node = document.querySelector('.nav-count[data-count="' + id + '"]');
      if (node) node.textContent = (map[id] === undefined || map[id] === null) ? "" : fmtNum(map[id]);
    }
  }

  // ───────────────────── WebSocket ─────────────────────
  function setConn(open) {
    const dot = $("#conn-dot");
    dot.classList.toggle("open", !!open);
    dot.title = open ? "connected" : "disconnected";
    $("#conn-label").textContent = open ? "live" : "disconnected";
  }

  function openSocket() {
    let url;
    try {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      url = proto + "//" + location.host + "/ws";
    } catch (e) { return; }

    try { ws = new WebSocket(url); }
    catch (e) { scheduleReconnect(); return; }

    ws.addEventListener("open", () => { setConn(true); wsBackoff = 1000; });
    ws.addEventListener("close", () => { setConn(false); scheduleReconnect(); });
    ws.addEventListener("error", () => { try { ws.close(); } catch (e) {} });
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg || typeof msg !== "object") return;
      if (msg.type === "hello") {
        return;
      }
      if (msg.type === "change") handleChange(msg);
    });
  }

  function scheduleReconnect() {
    setTimeout(() => { if (!ws || ws.readyState === WebSocket.CLOSED) openSocket(); }, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 1.7, 15000);
  }

  // ── Manual reload (no auto-reload) ───────────────────────────────────────
  // Live changes NEVER re-render a panel on their own — that caused constant
  // "reloading" while you work in Claude Code with the console open. Instead a
  // change marks its domain(s) as pending (a dot on the nav item + an "updates
  // available" cue on the active panel) and invalidates its cache, so YOUR next
  // manual Refresh / Reload all / domain switch shows fresh data.
  const pendingDomains = new Set();

  function handleChange(msg) {
    const domains = Array.isArray(msg.domains) ? msg.domains : [];
    pushActivity(msg.kind, msg.path, domains);
    // NEVER auto-refetch on a live change — an actively-written session jsonl
    // fires constantly and that would flash the view. Just mark the affected
    // domains pending (nav dot + "updates available" cue + refresh-button glow);
    // the user pulls the new data manually (header ⟳ for the domain, drawer ⟳
    // for an open transcript).
    for (const dmn of domains) {
      delete cache[dmn];                 // so the manual refresh fetches fresh
      pendingDomains.add(dmn);
      markNavPending(dmn, true);
    }
    refreshUpdatesCue();
  }

  function markNavPending(domain, on) {
    const node = document.querySelector('.nav-item[data-domain="' + domain + '"]');
    if (node) node.classList.toggle("pending", !!on);
  }

  // Show the "updates available" cue (refresh-button glow + header stamp) iff the
  // ACTIVE domain has pending live changes. Derived state — always consistent.
  function refreshUpdatesCue() {
    const on = pendingDomains.has(active);
    const btn = $("#refresh-btn");
    if (btn) btn.classList.toggle("has-updates", on);
    const stamp = $("#last-updated");
    if (stamp) {
      if (on) { stamp.textContent = "updates available"; stamp.classList.add("pending"); }
      else { stamp.classList.remove("pending"); }
    }
  }

  // Clear the pending state for a domain once the user manually loads it.
  function clearPending(id) {
    pendingDomains.delete(id);
    markNavPending(id, false);
    refreshUpdatesCue();
  }

  // ───────────────────── Activity feed ─────────────────────
  let activityCount = 0;
  function pushActivity(kind, path, domains) {
    const feed = $("#activity-feed");
    const row = el("div", { class: "act-row new" });
    row.appendChild(el("span", { class: "act-time", text: nowClock() }));
    row.appendChild(el("span", { class: "act-kind " + (kind || ""), text: kind || "?" }));
    row.appendChild(el("span", { class: "act-path", text: path || "", title: path || "" }));
    row.appendChild(el("span", { class: "act-domains", text: (domains || []).join(", ") }));
    feed.insertBefore(row, feed.firstChild);
    // cap at 50 rows
    while (feed.childNodes.length > 50) feed.removeChild(feed.lastChild);
    activityCount++;
    $("#activity-count").textContent = String(activityCount);
    // remove the flash class after the animation so re-adding works later
    setTimeout(() => row.classList.remove("new"), 1100);
  }

  // ───────────────────── small utils ─────────────────────
  function nowClock() {
    const d = new Date();
    return "[" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) + "]";
  }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  /** Trim an ISO-ish string to a compact local-ish display (no parsing risk). */
  function shortIso(iso) {
    if (!iso) return "";
    const s = String(iso);
    // expected "2026-06-02T19:29:00..." → "06-02 19:29"
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    if (m) return m[2] + "-" + m[3] + " " + m[4] + ":" + m[5];
    return s.length > 19 ? s.slice(0, 19) : s;
  }

  // ───────────────────── go ─────────────────────
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
