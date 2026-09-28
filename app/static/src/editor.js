// Editor SPA bundle entry. Extracted from app/templates/editor.html and
// compiled by esbuild (see package.json build:js, Dockerfile assets stage)
// into app/static/editor.js (+ editor.css). Bundling gives esbuild a single
// deduped @codemirror/state instance and serves everything from 'self' — no
// CDN, no import map, no CSP juggling.
// ── CodeMirror 6 imports ───────────────────────────────────────────────────
import { EditorView, basicSetup } from "codemirror";
import { EditorState } from "@codemirror/state";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { languages } from "@codemirror/language-data";
import { oneDark } from "@codemirror/theme-one-dark";
import { EditorView as EV, keymap } from "@codemirror/view";
import { autocompletion, snippetCompletion, startCompletion, completionKeymap } from "@codemirror/autocomplete";
import { indentWithTab, undo, redo, toggleComment, toggleBlockComment,
         indentMore, indentLess, moveLineUp, moveLineDown, copyLineDown,
         deleteLine, selectParentSyntax } from "@codemirror/commands";
import { openSearchPanel, findNext, findPrevious, gotoLine,
         selectNextOccurrence, selectSelectionMatches } from "@codemirror/search";
import { foldCode, unfoldCode, foldAll, unfoldAll } from "@codemirror/language";
import { openLintPanel, nextDiagnostic } from "@codemirror/lint";
import * as AnsiUpNS from "ansi_up";
// ansi_up's export shape varies (CJS default vs. named `AnsiUp`) depending on
// version and how the bundler applies interop. Resolve the constructor
// tolerantly so a version bump can't reintroduce "not a constructor".
const AnsiUp = AnsiUpNS.AnsiUp ?? AnsiUpNS.default?.AnsiUp ?? AnsiUpNS.default ?? AnsiUpNS;

// Terminal deps — bundled locally and imported statically. xterm's stylesheet
// is imported here so esbuild emits it as app/static/editor.css.
import { Terminal as _XTermTerminal } from "@xterm/xterm";
import { FitAddon as _XTermFitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

// ── State ──────────────────────────────────────────────────────────────────
let cmView = null;
let currentFile = null;
const openTabs = new Map(); // path → {content, dirty, state}
const _SETTINGS_DEFAULTS = {
  theme: "dark", fontSize: 14, tabSize: 2, wordWrap: true,
  autoSave: true, autoSaveDelay: 120000, vimMode: false,
  commitMessage: "User saved.", previewFollowDebounce: 1000,
  aiProvider: "claude",
  aiClaudeModel: "",
  aiOllamaModel: "",
  aiOllamaEndpoint: "http://localhost:11434",
  inlineSystemPrompt: "You are an inline editing assistant for Quarto documents. Make targeted, precise edits. Return only the replacement text with no explanation.",
  chatSystemPrompt: "You are a writing and coding assistant for Quarto documents. Quarto is a scientific and technical publishing system built on Pandoc. Help the user write, edit, structure, and improve their Quarto documents. When showing code, use Quarto's fenced code chunk syntax (```{r}, ```{python}, etc.). Be concise. Format your responses in Markdown compatible with Quarto.",
};
// AI key config — only the has_key/server_key_active flags; provider/model/endpoint are now settings
let aiConfig = { has_key: false, server_key_active: false };
let settings = { ..._SETTINGS_DEFAULTS };
let globalSettings = { ..._SETTINGS_DEFAULTS };
let repoSettings = {};
let workspaceLoaded = false;
let userSnippets = [];
let settingsSaveTimer = null;
let autoSaveTimer = null;
let _previewStarting = false;
const _issues = [];

// ── Terminal state ─────────────────────────────────────────────────────────
let _term = null;
let _termFit = null;
let _termWs = null;
let _termConnected = false;
let _Terminal = null;
let _FitAddon = null;

// ── Built-in snippets ──────────────────────────────────────────────────────
const BUILTIN_SNIPPETS = [
  snippetCompletion("```{r}\n${code}\n```", { label: "r-chunk", detail: "R code chunk", type: "text" }),
  snippetCompletion("```{r}\n#| label: ${id}\n#| echo: ${false}\n#| fig.cap: \"${caption}\"\n\n${code}\n```",
    { label: "r-chunk-opts", detail: "R chunk with label + options", type: "text" }),
  snippetCompletion("```{python}\n${code}\n```", { label: "py-chunk", detail: "Python code chunk", type: "text" }),
  snippetCompletion("```{bash}\n${code}\n```", { label: "bash-chunk", detail: "Bash code chunk", type: "text" }),
  snippetCompletion("::: {.callout-note}\n## ${Note}\n${content}\n:::", { label: "callout-note", detail: "Note callout block", type: "text" }),
  snippetCompletion("::: {.callout-warning}\n## ${Warning}\n${content}\n:::", { label: "callout-warning", detail: "Warning callout", type: "text" }),
  snippetCompletion("::: {.callout-tip}\n## ${Tip}\n${content}\n:::", { label: "callout-tip", detail: "Tip callout", type: "text" }),
  snippetCompletion("::: {.callout-important}\n## ${Important}\n${content}\n:::", { label: "callout-important", detail: "Important callout", type: "text" }),
  snippetCompletion("::: {.callout-caution}\n## ${Caution}\n${content}\n:::", { label: "callout-caution", detail: "Caution callout", type: "text" }),
  snippetCompletion("::: {.panel-tabset}\n## ${Tab 1}\n${content 1}\n\n## ${Tab 2}\n${content 2}\n:::",
    { label: "tabset", detail: "Panel tabset (tabs)", type: "text" }),
  snippetCompletion(":::: {layout-ncol=2}\n::: {}\n${left column}\n:::\n::: {}\n${right column}\n:::\n::::",
    { label: "columns", detail: "Two-column layout", type: "text" }),
  snippetCompletion("![${caption}](${path.png}){#fig-${id} width=\"${80%}\"}",
    { label: "figure", detail: "Figure with cross-reference", type: "text" }),
  snippetCompletion("---\ntitle: \"${Title}\"\nauthor: \"${Author}\"\ndate: today\nformat:\n  html:\n    toc: true\n    code-fold: true\n    theme: ${cosmo}\n---",
    { label: "yaml-html", detail: "HTML document front matter", type: "text" }),
  snippetCompletion("---\ntitle: \"${Title}\"\nauthor: \"${Author}\"\ndate: today\nformat:\n  pdf:\n    geometry: margin=1in\n    fontsize: ${11pt}\n---",
    { label: "yaml-pdf", detail: "PDF document front matter", type: "text" }),
  snippetCompletion("---\ntitle: \"${Title}\"\nauthor: \"${Author}\"\nformat:\n  revealjs:\n    theme: ${dark}\n    slide-number: true\n---",
    { label: "yaml-revealjs", detail: "Reveal.js slides front matter", type: "text" }),
  snippetCompletion("---\ntitle: \"${Chapter Title}\"\n---",
    { label: "yaml-chapter", detail: "Quarto book chapter header", type: "text" }),
];

function allCompletions(context) {
  const word = context.matchBefore(/\S*/);
  if (!word && !context.explicit) return null;
  const userC = userSnippets.map(s =>
    snippetCompletion(s.template || "", { label: s.label, detail: s.detail || "custom snippet", type: "text" })
  );
  return {
    from: word ? word.from : context.pos,
    options: BUILTIN_SNIPPETS.concat(userC),
    validFor: /\S*/,
  };
}

// ── Editor initialisation ──────────────────────────────────────────────────
function makeExtensions() {
  return [
    basicSetup,
    markdown({ codeLanguages: languages }),
    settings.theme === "dark" ? oneDark : [],
    autocompletion({ override: [allCompletions], activateOnTyping: false }),
    keymap.of([
      indentWithTab,
      { key: "Ctrl-s", run: () => { saveCurrentFile({ commit: true }); return true; } },
      { key: "Ctrl-Space", run: startCompletion },
      { key: "Ctrl-Shift-i", run: (view) => { openInlinePromptDialog(view); return true; } },
    ]),
    EV.lineWrapping,
    EV.updateListener.of(update => {
      if (update.docChanged && currentFile) {
        markDirty(currentFile);
        scheduleAutoSave();
      }
    }),
    EditorState.tabSize.of(settings.tabSize),
    ...(settings.wordWrap ? [] : [EV.lineWrapping]),
  ].flat();
}

function initEditor(content = "") {
  const host = document.getElementById("cm-host");
  if (cmView) {
    cmView.destroy();
    cmView = null;
  }
  cmView = new EditorView({
    state: EditorState.create({ doc: content, extensions: makeExtensions() }),
    parent: host,
  });
}

function rebuildEditor() {
  const content = cmView ? cmView.state.doc.toString() : "";
  const sel = cmView ? cmView.state.selection : null;
  initEditor(content);
  if (sel && cmView) {
    try { cmView.dispatch({ selection: sel }); } catch (_) {}
  }
}

// ── Tabs ───────────────────────────────────────────────────────────────────
function renderTabs() {
  const bar = document.getElementById("tabs-bar");
  bar.innerHTML = "";
  for (const [path, tab] of openTabs) {
    const name = path.split("/").pop();
    const div = document.createElement("div");
    div.className = "tab" + (path === currentFile ? " active" : "");
    div.innerHTML = `<span class="name">${escapeHtml(name)}</span>
      <span class="dirty">${tab.dirty ? "●" : ""}</span>
      <span class="close-btn" data-path="${escapeHtml(path)}" title="Close">✕</span>`;
    div.querySelector(".name").addEventListener("click", () => switchToFile(path));
    div.querySelector(".close-btn").addEventListener("click", e => {
      e.stopPropagation(); closeTab(path);
    });
    bar.appendChild(div);
  }
  document.getElementById("cm-host").style.display = openTabs.size ? "flex" : "none";
  const welcome = document.getElementById("welcome-pane");
  if (welcome) welcome.style.display = openTabs.size ? "none" : "block";
}

function markDirty(path) {
  const tab = openTabs.get(path);
  if (tab && !tab.dirty) { tab.dirty = true; renderTabs(); }
}

function switchToFile(path) {
  if (currentFile === path) return;
  if (currentFile && cmView) {
    openTabs.get(currentFile).state = cmView.state;
  }
  currentFile = path;
  const tab = openTabs.get(path);
  if (tab.state) {
    if (!cmView) initEditor("");
    cmView.setState(tab.state);
  } else {
    initEditor(tab.content);
    openTabs.get(path).state = cmView.state;
  }
  renderTabs();
  if (path.endsWith(".qmd")) {
    followQmdFile = path;
    previewRawFile = null;
    if (_previewActive && document.getElementById("preview-target").value === "follow") {
      clearTimeout(followRestartTimer);
      const delay = settings.previewFollowDebounce;
      if (delay > 0) {
        followRestartTimer = setTimeout(() => startOrRestartPreview(path), delay);
      }
    }
  }
}

function closeTab(path) {
  openTabs.delete(path);
  if (currentFile === path) {
    currentFile = openTabs.size ? [...openTabs.keys()].pop() : null;
    if (currentFile) switchToFile(currentFile);
    else if (cmView) { cmView.destroy(); cmView = null; }
  }
  renderTabs();
}

// ── File browser ───────────────────────────────────────────────────────────
async function loadFileTree() {
  try {
    const res = await fetch("/api/files");
    if (!res.ok) {
      reportIssue("File tree failed: " + res.statusText);
      return;
    }
    const { tree } = await res.json();
    window._lastTree = tree;
    renderTree(tree, document.getElementById("file-tree"));
    populatePreviewTargetDropdown();
  } catch (e) {
    reportIssue("File tree error: " + e.message);
  }
}

function flattenQmdFiles(nodes, prefix = "") {
  const result = [];
  for (const node of nodes) {
    const path = prefix ? `${prefix}/${node.name}` : node.name;
    if (node.type === "dir") {
      result.push(...flattenQmdFiles(node.children || [], path));
    } else if (node.name.endsWith(".qmd")) {
      result.push(path);
    }
  }
  return result;
}

function populatePreviewTargetDropdown() {
  const select = document.getElementById("preview-target");
  const currentValue = select.value;
  const files = flattenQmdFiles(window._lastTree || []);

  select.innerHTML = '<option value="">Project</option><option value="follow">Follow active file</option>';
  for (const file of files) {
    const option = document.createElement("option");
    option.value = file;
    option.textContent = file;
    select.appendChild(option);
  }

  if (files.includes(currentValue)) {
    select.value = currentValue;
  } else {
    select.value = "";
  }
}

function renderTree(nodes, container) {
  container.innerHTML = "";
  if (!nodes.length) {
    container.innerHTML = '<div style="padding:0.5rem 0.7rem;color:var(--text2);font-size:0.74rem">Empty</div>';
    return;
  }
  for (const node of nodes) {
    if (node.type === "dir") {
      const wrapper = document.createElement("div");
      wrapper.className = "tree-dir";
      const header = document.createElement("div");
      header.className = "tree-item" + (node.ignored ? " ignored" : "");
      header.tabIndex = 0;
      header.dataset.path = node.path;
      header.dataset.type = "dir";
      header.innerHTML = `<span class="icon">▸</span><span class="name${node.ignored ? " ignored" : ""}">${escapeHtml(node.name)}/</span>`;
      const children = document.createElement("div");
      children.className = "tree-children hidden";
      renderTree(node.children || [], children);
      header.addEventListener("click", () => {
        const collapsed = children.classList.toggle("hidden");
        header.querySelector(".icon").textContent = collapsed ? "▸" : "▾";
      });
      header.addEventListener("contextmenu", (e) => showFileContextMenu(e, node));
      wrapper.appendChild(header);
      wrapper.appendChild(children);
      container.appendChild(wrapper);
    } else {
      const item = document.createElement("div");
      const ext = node.name.slice(node.name.lastIndexOf(".")).toLowerCase();
      const isRawFile = RENDERABLE_EXTS.includes(ext);
      item.className = "tree-item" + (node.path === currentFile || node.path === previewRawFile ? " active" : "") + (node.ignored ? " ignored" : "");
      item.tabIndex = 0;
      item.dataset.path = node.path;
      item.dataset.type = "file";
      item.innerHTML = `<span class="icon">📄</span><span class="name${node.ignored ? " ignored" : ""}">${escapeHtml(node.name)}</span>`;
      if (isRawFile) {
        item.addEventListener("click", () => selectPreviewRawFile(node.path));
      } else {
        item.addEventListener("click", () => openFile(node.path));
      }
      item.addEventListener("contextmenu", (e) => showFileContextMenu(e, node));
      container.appendChild(item);
    }
  }
}

// ── File tree context menu ──────────────────────────────────────────────────
let _contextTarget = null;

function showFileContextMenu(e, node) {
  e.preventDefault();
  e.stopPropagation();
  _contextTarget = node;
  const menu = document.getElementById("fileContextMenu");
  menu.style.left = e.clientX + "px";
  menu.style.top = e.clientY + "px";
  menu.classList.add("open");
}

function hideFileContextMenu() {
  document.getElementById("fileContextMenu").classList.remove("open");
  _contextTarget = null;
}

document.addEventListener("click", () => hideFileContextMenu());
document.addEventListener("contextmenu", (e) => {
  if (!e.target.closest(".tree-item")) hideFileContextMenu();
});

window.addContextTargetToGitignore = async () => {
  const node = _contextTarget;
  hideFileContextMenu();
  if (!node) return;
  const res = await fetch("/api/workspace/gitignore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: node.path, is_dir: node.type === "dir" }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    alert("Failed to update .gitignore: " + (d.detail || res.statusText));
    return;
  }
  const data = await res.json();
  if (data.untracked) {
    alert(`"${node.path}" was already tracked by git and has been removed from version control. It will be deleted from the GitHub repo on your next sync.`);
  }
  await loadFileTree();
};

window.deleteContextTarget = async () => {
  const node = _contextTarget;
  hideFileContextMenu();
  if (!node) return;
  const isDir = node.type === "dir";
  if (!confirm(`Delete ${isDir ? "folder" : "file"} "${node.path}"? This removes it from the working tree and stages the deletion in git.`)) {
    return;
  }
  const res = await fetch("/api/workspace/file/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: node.path, is_dir: isDir }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    alert("Failed to delete: " + (d.detail || res.statusText));
    return;
  }
  const prefix = node.path + "/";
  for (const path of [...openTabs.keys()]) {
    if (path === node.path || (isDir && path.startsWith(prefix))) {
      closeTab(path);
    }
  }
  await loadFileTree();
};

async function openFile(path) {
  if (openTabs.has(path)) { switchToFile(path); return; }
  const res = await fetch(`/api/file?path=${encodeURIComponent(path)}`);
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    reportIssue("Cannot open " + path + ": " + extractDetail(d, res.statusText));
    return;
  }
  const { content } = await res.json();
  openTabs.set(path, { content, dirty: false, state: null });
  switchToFile(path);
  await loadFileTree();
}

async function saveCurrentFile({ commit = false } = {}) {
  if (!currentFile || !cmView) return;
  const content = cmView.state.doc.toString();
  const res = await fetch("/api/file", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: currentFile, content }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    reportIssue("Save failed: " + extractDetail(d, res.statusText));
    return;
  }
  const tab = openTabs.get(currentFile);
  if (tab) { tab.dirty = false; tab.content = content; }
  renderTabs();
  if (commit) {
    const indicator = document.getElementById("save-indicator");
    if (indicator) indicator.textContent = "Pushing…";
    const syncRes = await fetch("/api/workspace/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: settings.commitMessage || "User saved." }),
    });
    if (!syncRes.ok) {
      const d = await syncRes.json().catch(() => ({}));
      reportIssue("Push failed: " + extractDetail(d, syncRes.statusText));
    }
    if (indicator) indicator.textContent = "Saved & pushed";
  }
  flashIndicator("save-indicator");
}

function scheduleAutoSave() {
  if (!settings.autoSave) return;
  clearTimeout(autoSaveTimer);
  autoSaveTimer = setTimeout(() => saveCurrentFile({ commit: false }), settings.autoSaveDelay);
}

// ── Sidebar / panels ───────────────────────────────────────────────────────
window.toggleSidebar = () => {
  const collapsed = document.getElementById("sidebar").classList.toggle("collapsed");
  document.getElementById("resize-left").classList.toggle("hidden");
  document.getElementById("edge-toggle-left").classList.toggle("visible", collapsed);
  document.getElementById("sidebar-backdrop").classList.toggle("visible", !collapsed);
  localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0");
  updateViewMenuChecks();
};
window.toggleRightPanel = () => {
  const collapsed = document.getElementById("right-panel").classList.toggle("collapsed");
  document.getElementById("resize-right").classList.toggle("hidden");
  document.getElementById("edge-toggle-right").classList.toggle("visible", collapsed);
  document.getElementById("right-panel-backdrop").classList.toggle("visible", !collapsed);
  localStorage.setItem("right-panel-collapsed", collapsed ? "1" : "0");
  updateViewMenuChecks();
};

function updateViewMenuChecks() {
  document.getElementById("check-sidebar").classList.toggle(
    "visible", !document.getElementById("sidebar").classList.contains("collapsed"));
  document.getElementById("check-rightpanel").classList.toggle(
    "visible", !document.getElementById("right-panel").classList.contains("collapsed"));
}

// ── Compact layout (tablet and below) ───────────────────────────────────────
// Single breakpoint listener today; a future phone-specific pass can add a
// second matchMedia() alongside this one rather than reworking it — see the
// matching comment in input.css above the compact-mode media query.
const COMPACT_MEDIA = window.matchMedia("(max-width: 1024px)");
function enterCompactMode() {
  // Default to collapsed on first visit (scroll-free first paint on a
  // tablet); afterwards always honor whatever the user last chose.
  const wantCollapsed = (key) => {
    const pref = localStorage.getItem(key);
    return pref === null ? true : pref === "1";
  };
  if (wantCollapsed("sidebar-collapsed") !==
      document.getElementById("sidebar").classList.contains("collapsed")) {
    toggleSidebar();
  }
  if (wantCollapsed("right-panel-collapsed") !==
      document.getElementById("right-panel").classList.contains("collapsed")) {
    toggleRightPanel();
  }
}
COMPACT_MEDIA.addEventListener("change", e => { if (e.matches) enterCompactMode(); });
if (COMPACT_MEDIA.matches) enterCompactMode();

// ── Edit menu / editor commands ───────────────────────────────────────────
const isMac = /Mac|iPhone|iPad/.test(navigator.platform);

function fmtKey(spec) {
  const raw = (isMac && spec.mac) ? spec.mac : spec.key;
  const parts = raw.split("-");
  const last = parts.pop();
  const arrows = { ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→" };
  const lastDisplay = arrows[last] || (/^[a-z]$/.test(last) ? last.toUpperCase() : last);
  const modMap = isMac
    ? { Mod: "⌘", Cmd: "⌘", Ctrl: "⌃", Alt: "⌥", Shift: "⇧" }
    : { Mod: "Ctrl", Cmd: "Ctrl", Ctrl: "Ctrl", Alt: "Alt", Shift: "Shift" };
  const mods = parts.map(p => modMap[p] || p);
  return isMac ? mods.join("") + lastDisplay : mods.concat(lastDisplay).join("+");
}

const EDIT_COMMAND_GROUPS = [
  { label: "History", items: [
    { label: "Undo", desc: "Undo the last change", cmd: undo, key: "Mod-z" },
    { label: "Redo", desc: "Redo the last undone change", cmd: redo, key: "Mod-y", mac: "Mod-Shift-z" },
  ]},
  { label: "Find & search", items: [
    { label: "Find / Replace", desc: "Open the search and replace panel", cmd: openSearchPanel, key: "Mod-f" },
    { label: "Find next", desc: "Jump to the next search match", cmd: findNext, key: "Mod-g" },
    { label: "Find previous", desc: "Jump to the previous search match", cmd: findPrevious, key: "Shift-Mod-g" },
    { label: "Go to line", desc: "Jump to a specific line number", cmd: gotoLine, key: "Mod-Alt-g" },
  ]},
  { label: "Selection", items: [
    { label: "Select next occurrence", desc: "Add the next match of the current selection to it", cmd: selectNextOccurrence, key: "Mod-d" },
    { label: "Select all occurrences", desc: "Select every instance of the current selection", cmd: selectSelectionMatches, key: "Mod-Shift-l" },
    { label: "Expand selection", desc: "Expand the selection to the enclosing syntax node", cmd: selectParentSyntax, key: "Mod-i" },
  ]},
  { label: "Editing", items: [
    { label: "Toggle comment", desc: "Comment or uncomment the selected lines", cmd: toggleComment, key: "Mod-/" },
    { label: "Toggle block comment", desc: "Wrap or unwrap the selection in a block comment", cmd: toggleBlockComment, key: "Shift-Alt-a" },
    { label: "Indent more", desc: "Increase the indentation of the selected lines", cmd: indentMore, key: "Mod-]" },
    { label: "Indent less", desc: "Decrease the indentation of the selected lines", cmd: indentLess, key: "Mod-[" },
    { label: "Move line up", desc: "Move the current line(s) up", cmd: moveLineUp, key: "Alt-ArrowUp" },
    { label: "Move line down", desc: "Move the current line(s) down", cmd: moveLineDown, key: "Alt-ArrowDown" },
    { label: "Duplicate line", desc: "Copy the current line(s) below", cmd: copyLineDown, key: "Shift-Alt-ArrowDown" },
    { label: "Delete line", desc: "Delete the current line(s)", cmd: deleteLine, key: "Shift-Mod-k" },
  ]},
  { label: "Code folding", items: [
    { label: "Fold code", desc: "Fold the code block at the cursor", cmd: foldCode, key: "Ctrl-Shift-[", mac: "Cmd-Alt-[" },
    { label: "Unfold code", desc: "Unfold the code block at the cursor", cmd: unfoldCode, key: "Ctrl-Shift-]", mac: "Cmd-Alt-]" },
    { label: "Fold all", desc: "Fold all foldable regions", cmd: foldAll, key: "Ctrl-Alt-[" },
    { label: "Unfold all", desc: "Unfold all folded regions", cmd: unfoldAll, key: "Ctrl-Alt-]" },
  ]},
  { label: "Linting", items: [
    { label: "Open lint panel", desc: "Show the panel listing diagnostics", cmd: openLintPanel, key: "Mod-Shift-m" },
    { label: "Next diagnostic", desc: "Jump to the next lint diagnostic", cmd: nextDiagnostic, key: "F8" },
  ]},
];

const _editCommandMap = {};

function renderEditMenu() {
  const container = document.getElementById("editMenuItems");
  let idx = 0;
  EDIT_COMMAND_GROUPS.forEach((group) => {
    const groupEl = document.createElement("div");
    groupEl.className = "menu-tree-group";

    const header = document.createElement("button");
    header.className = "dropdown-item menu-tree-header";
    header.innerHTML = `<span class="menu-tree-arrow">▸</span><span>${group.label}</span>`;

    const children = document.createElement("div");
    children.className = "menu-tree-children";

    group.items.forEach(item => {
      const id = "ec" + (idx++);
      _editCommandMap[id] = item.cmd;
      const btn = document.createElement("button");
      btn.className = "dropdown-item menu-tree-leaf";
      btn.innerHTML = `<span>${item.label}</span><span class="shortcut">${fmtKey(item)}</span>`;
      btn.addEventListener("click", () => runEditorCommand(id));
      children.appendChild(btn);
    });

    header.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = children.classList.toggle("open");
      header.querySelector(".menu-tree-arrow").textContent = isOpen ? "▾" : "▸";
    });

    groupEl.appendChild(header);
    groupEl.appendChild(children);
    container.appendChild(groupEl);
  });
}

window.runEditorCommand = (id) => {
  document.getElementById("viewMenu").classList.remove("open");
  if (!cmView) return;
  const cmd = _editCommandMap[id];
  if (cmd) cmd(cmView);
  cmView.focus();
};

// ── App-level keybindings ─────────────────────────────────────────────────
// Single source of truth for both the keydown handler and the Quick Help dialog.
// eKey: the value of e.key when Ctrl is held; eKeyAlt: alternate e.key some
// browsers report for the same physical key (e.g. Shift+Period → ">" or ".").
// shift: whether Shift must also be held.
const APP_KEYBINDINGS = [
  { label: "Panels & navigation", items: [
    { key: "Ctrl-'",       eKey: "'",  shift: false, label: "Preview",            desc: "Switch to the Preview panel",                         action: () => switchPanel("preview") },
    { key: "Ctrl->",       eKey: ">",  shift: true,  label: "AI",                 desc: "Switch to the AI panel and focus the input",          action: () => { switchPanel("ai"); document.getElementById("ai-input")?.focus(); }, eKeyAlt: "." },
    { key: "Ctrl-;",       eKey: ";",  shift: false, label: "Terminal",           desc: "Switch to the Terminal panel",                        action: () => { switchPanel("terminal"); initTerminal(); } },
    { key: "Ctrl-:",       eKey: ":",  shift: true,  label: "Logs",               desc: "Switch to the Logs panel",                            action: () => switchPanel("logs") },
    { key: "Ctrl-,",       eKey: ",",  shift: false, label: "Settings",           desc: "Switch to the Settings panel",                        action: () => switchPanel("settings") },
    { key: "Ctrl-.",       eKey: ".",  shift: false, label: "Focus editor",       desc: "Move keyboard focus to the editor",                   action: () => cmView?.focus() },
    { key: "Ctrl-l",       eKey: "l",  shift: false, label: "Focus file pane",    desc: "Move focus to the file browser, expanding if hidden", action: () => { if (document.getElementById("sidebar").classList.contains("collapsed")) toggleSidebar(); (document.querySelector(".tree-item.active") || document.querySelector(".tree-item"))?.focus(); } },
    { key: "Ctrl-h",       eKey: "h",  shift: false, label: "Toggle file pane",   desc: "Show or hide the file browser",                       action: () => toggleSidebar() },
    { key: "Ctrl-Shift-H", eKey: "H",  shift: true,  label: "Toggle right pane",  desc: "Show or hide the right panel",                        action: () => toggleRightPanel() },
    { key: "Ctrl-?",       eKey: "?",  shift: true,  label: "Quick help",         desc: "Open this keybinding reference",                      action: () => openQuickHelp() },
  ]},
];

window.openQuickHelp = () => {
  document.getElementById("viewMenu").classList.remove("open");
  const content = document.getElementById("quickHelpContent");
  if (!content.dataset.rendered) {
    let html = "";
    APP_KEYBINDINGS.forEach(group => {
      html += `<h4>${group.label}</h4><table>`;
      group.items.forEach(item => {
        html += `<tr><td class="key">${fmtKey(item)}</td><td>${item.label}</td><td class="desc">${item.desc}</td></tr>`;
      });
      html += `</table>`;
    });
    EDIT_COMMAND_GROUPS.forEach(group => {
      html += `<h4>${group.label}</h4><table>`;
      group.items.forEach(item => {
        html += `<tr><td class="key">${fmtKey(item)}</td><td>${item.label}</td><td class="desc">${item.desc}</td></tr>`;
      });
      html += `</table>`;
    });
    content.innerHTML = html;
    content.dataset.rendered = "1";
  }
  document.getElementById("quickHelpDialog").showModal();
};

window.toggleViewMenu = (e) => {
  e.stopPropagation();
  document.getElementById("viewMenu").classList.toggle("open");
};
document.addEventListener("click", (e) => {
  const menu = document.getElementById("viewMenu");
  if (menu.classList.contains("open") && !e.target.closest("#viewMenuWrapper")) {
    menu.classList.remove("open");
  }
});

window.switchPanel = (name) => {
  document.querySelectorAll(".panel-tab").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".panel-pane").forEach(p => p.classList.remove("active"));
  const btn = [...document.querySelectorAll(".panel-tab")].find(b =>
    b.textContent.trim().toLowerCase() === name.toLowerCase() ||
    b.getAttribute("onclick")?.includes(`'${name}'`)
  );
  if (btn) btn.classList.add("active");
  const pane = document.getElementById(`panel-${name}`);
  if (pane) pane.classList.add("active");
  if (name === "terminal") initTerminal();
};

// ── Preview ────────────────────────────────────────────────────────────────
let _previewActive = false;
let followQmdFile = null;
let previewRawFile = null;
let followRestartTimer = null;
const RENDERABLE_EXTS = [".pdf", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"];

function resolveTarget() {
  const value = document.getElementById("preview-target").value;
  if (value === "") return null;
  if (value === "follow") return followQmdFile;
  return value;
}

async function startOrRestartPreview(target) {
  const btn = document.getElementById("preview-start-btn");
  const sbStatus = document.getElementById("sb-preview-status");
  btn.disabled = true;
  _previewStarting = true;
  if (sbStatus) { sbStatus.textContent = "● Starting"; sbStatus.className = "sb-preview-status starting"; }
  try {
    const res = await fetch("/api/preview/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target })
    });
    _previewStarting = false;
    if (res.ok) {
      const data = await res.json();
      const path = data.path || "/";
      const iframeSrc = "/api/preview/" + path.slice(1);
      document.getElementById("preview-frame").src = iframeSrc;
    } else {
      const d = await res.json().catch(() => ({}));
      reportIssue("Preview failed: " + extractDetail(d, res.statusText));
      if (sbStatus) { sbStatus.textContent = "● Stopped"; sbStatus.className = "sb-preview-status stopped"; }
    }
  } catch (e) {
    _previewStarting = false;
    reportIssue("Preview error: " + e.message);
    if (sbStatus) { sbStatus.textContent = "● Stopped"; sbStatus.className = "sb-preview-status stopped"; }
  } finally {
    _previewStarting = false;
    btn.disabled = false;
  }
}

window.loadPreview = () => {
  _previewActive = true;
  startOrRestartPreview(resolveTarget());
  const btn = document.getElementById("preview-start-btn");
  btn.textContent = "↻ Re-start preview";
  btn.onclick = restartPreview;
  populatePreviewTargetDropdown();
};

window.reloadPreview = () => {
  const f = document.getElementById("preview-frame");
  f.src = f.src;
};

window.restartPreview = async () => {
  await startOrRestartPreview(resolveTarget());
};

window.onPreviewTargetChange = async () => {
  if (!_previewActive) return;
  const target = resolveTarget();
  if (target === "follow" && previewRawFile) {
    document.getElementById("preview-frame").src = "/api/file/raw?path=" + encodeURIComponent(previewRawFile);
  } else {
    await startOrRestartPreview(target);
  }
};

function selectPreviewRawFile(path) {
  previewRawFile = path;
  followQmdFile = null;
  if (_previewActive && document.getElementById("preview-target").value === "follow") {
    document.getElementById("preview-frame").src = "/api/file/raw?path=" + encodeURIComponent(path);
  }
  renderTree(window._lastTree || [], document.getElementById("file-tree"));
}

// Guard: if quarto's live-reload navigates the iframe outside /api/preview/,
// redirect it back so the editor UI never appears inside the pane.
document.getElementById("preview-frame").addEventListener("load", function () {
  if (!_previewActive) return;
  try {
    const loc = this.contentWindow.location;
    if (loc.href === "about:blank") return;
    if (!loc.pathname.startsWith("/api/preview/") && !loc.pathname.startsWith("/api/file/raw")) {
      // Navigation escaped the proxy (e.g. quarto sent a reload path without
      // the /api/preview/ prefix).  Fall back to the proxy root so we never
      // show the editor UI or a 404 inside the preview pane.
      this.src = "/api/preview/";
    }
  } catch (_) {
    // Cross-origin frame — can't inspect, leave it alone
  }
});

// Quarto's live-reload navigates the preview iframe whenever a file is
// saved (including autosaves), and that navigation steals focus from
// whatever the user was working in (typically the editor). Track the last
// focused element outside the iframe and restore focus to it once the
// reload completes.
let lastFocusedElement = null;
document.addEventListener("focusin", (e) => {
  if (e.target !== document.getElementById("preview-frame")) {
    lastFocusedElement = e.target;
  }
});
document.getElementById("preview-frame").addEventListener("load", () => {
  const frame = document.getElementById("preview-frame");
  if (document.activeElement === frame && lastFocusedElement) {
    const toFocus = lastFocusedElement;
    setTimeout(() => { if (document.contains(toFocus)) toFocus.focus(); }, 0);
  }
});

// ── Workspace ops ──────────────────────────────────────────────────────────
async function loadRepos() {
  try {
    const res = await fetch("/api/repos");
    if (!res.ok) return;
    const { repos } = await res.json();
    const dl = document.getElementById("repo-suggestions");
    dl.innerHTML = repos.map(r => `<option value="${escapeHtml(r)}">`).join("");
  } catch (_) {}
}

window.loadWorkspace = async () => {
  const owner = document.getElementById("repoOwner").value.trim();
  const name = document.getElementById("repoName").value.trim();
  if (!owner || !name) { alert("Enter both owner and repo name."); return; }
  const res = await fetch("/api/workspace/load", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_owner: owner, repo_name: name }),
  });
  if (res.ok) {
    document.getElementById("loadDialog").close();
    location.reload();
  } else if (res.status === 409) {
    const d = await res.json().catch(() => ({}));
    const files = (d.detail && d.detail.files) || [];
    document.getElementById("conflictFileList").textContent =
      files.length ? files.join("\n") : "(no file list available)";
    document.getElementById("loadDialog").close();
    document.getElementById("conflictDialog").showModal();
  } else {
    const d = await res.json().catch(() => ({}));
    alert("Load failed: " + (d.detail || res.statusText));
  }
};

window.downloadAndDiscard = async () => {
  const a = document.createElement("a");
  a.href = "/api/workspace/changes.zip";
  a.download = "";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => discardLocal(), 500);
};

window.discardLocal = async () => {
  document.getElementById("conflictDialog").close();
  const res = await fetch("/api/workspace/discard", { method: "POST" });
  if (!res.ok) { alert("Failed to discard local changes."); return; }
  const owner = document.getElementById("repoOwner").value.trim();
  const name  = document.getElementById("repoName").value.trim();
  const res2 = await fetch("/api/workspace/load", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_owner: owner, repo_name: name }),
  });
  if (res2.ok) { location.reload(); }
  else {
    const d = await res2.json().catch(() => ({}));
    alert("Load failed after discard: " + (d.detail || res2.statusText));
  }
};

window.syncWorkspace = async () => {
  const msg = document.getElementById("commitMsg").value.trim() || "Sync from quarto-ed";
  const res = await fetch("/api/workspace/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: msg }),
  });
  document.getElementById("syncDialog").close();
  if (res.ok) { alert("Pushed to GitHub successfully."); }
  else {
    const d = await res.json().catch(() => ({}));
    if (res.status === 409 && d.detail?.type === "merge_conflict") {
      alert("Sync failed: merge conflict.\n\n" + (d.detail.message || "").trim());
    } else {
      alert("Sync failed: " + extractDetail(d, res.statusText));
    }
  }
};

window.newFilePrompt = async () => {
  const path = prompt("New file path (relative to repo root):");
  if (!path) return;
  const res = await fetch("/api/file/create", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (res.ok) { await loadFileTree(); await openFile(path); }
  else { const d = await res.json().catch(() => ({})); alert(d.detail || "Create failed"); }
};

// ── AI chat ────────────────────────────────────────────────────────────────
window.aiKeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendAIMessage(); }
};

function _makeAIMsgWrap(msgEl, rawText) {
  const wrap = document.createElement("div");
  wrap.className = "ai-msg-wrap";
  if (rawText !== undefined) wrap.dataset.text = rawText;
  const btn = document.createElement("button");
  btn.className = "ai-copy-btn";
  btn.textContent = "Copy";
  btn.onclick = () => copyAIMsg(btn);
  wrap.appendChild(msgEl);
  wrap.appendChild(btn);
  return wrap;
}

window.copyAIMsg = (btn) => {
  const wrap = btn.closest(".ai-msg-wrap");
  const text = wrap.dataset.text ?? wrap.querySelector(".ai-msg").textContent;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = "✓ Copied";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("copied"); }, 1500);
  }).catch(() => {});
};

window.sendAIMessage = async () => {
  const input = document.getElementById("ai-input");
  const msg = input.value.trim();
  if (!msg) return;
  input.value = "";

  const msgs = document.getElementById("ai-messages");

  // User bubble
  const userEl = document.createElement("div");
  userEl.className = "ai-msg user";
  userEl.textContent = "You: " + msg;
  msgs.appendChild(_makeAIMsgWrap(userEl, msg));

  // Assistant bubble
  const reply = document.createElement("div");
  reply.className = "ai-msg assistant";
  const replyWrap = _makeAIMsgWrap(reply);
  msgs.appendChild(replyWrap);

  const scrollToBottom = () => { msgs.scrollTop = msgs.scrollHeight; };
  scrollToBottom();

  const includeCtx = document.getElementById("ai-context")?.checked;
  const context = includeCtx && currentFile && cmView
    ? cmView.state.doc.toString() : null;

  const systemPrompt = settings.chatSystemPrompt || _SETTINGS_DEFAULTS.chatSystemPrompt;
  const provider = settings.aiProvider || "claude";

  try {
    if (provider === "ollama") {
      await _streamOllama(
        settings.aiOllamaEndpoint || "http://localhost:11434",
        settings.aiOllamaModel || "llama3",
        systemPrompt,
        context ? `<document>\n${context}\n</document>\n\n${msg}` : msg,
        (chunk) => { reply.textContent += chunk; scrollToBottom(); },
      );
    } else {
      const model = settings.aiClaudeModel || null;
      const res = await fetch("/api/ai/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg, context, system_prompt: systemPrompt, provider, model }),
      });
      if (!res.ok) { reply.textContent = "Error: " + res.statusText; return; }
      await _streamAnthropic(res, (chunk) => { reply.textContent += chunk; scrollToBottom(); });
    }
  } catch (e) {
    reply.textContent = "Error: " + e.message;
  }
};

async function _streamAnthropic(res, onChunk) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (data === "[DONE]") continue;
      try {
        const obj = JSON.parse(data);
        if (obj.type === "content_block_delta" && obj.delta?.type === "text_delta") {
          onChunk(obj.delta.text);
        }
      } catch (_) {}
    }
  }
}

async function _streamOllama(endpoint, model, systemPrompt, userContent, onChunk) {
  const base = endpoint.replace(/\/$/, "");
  const res = await fetch(`${base}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model,
      stream: true,
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user", content: userContent },
      ],
    }),
  });
  if (!res.ok) throw new Error(`Ollama error ${res.status}: ${res.statusText}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (data === "[DONE]") continue;
      try {
        const obj = JSON.parse(data);
        const chunk = obj.choices?.[0]?.delta?.content;
        if (chunk) onChunk(chunk);
      } catch (_) {}
    }
  }
}

// ── Settings ───────────────────────────────────────────────────────────────
async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    if (!res.ok) {
      reportIssue("Settings load failed: " + res.statusText);
      return;
    }
    const data = await res.json();
    globalSettings = { ..._SETTINGS_DEFAULTS, ...(data.global_settings || data.settings) };
    repoSettings = data.repo_settings || {};
    workspaceLoaded = data.workspace_loaded || false;
    settings = { ...globalSettings, ...repoSettings };
    userSnippets = data.snippets || [];
    applySettings();
    renderSnippetsEditor();
    renderSettingScopes();
    if (!data.repo_exists) {
      const name = `${window._tpl.username}-quarto-ed-settings`;
      document.getElementById("settings-repo-name").textContent = name;
      document.getElementById("settingsRepoDialog").showModal();
    }
  } catch (e) {
    reportIssue("Settings error: " + e.message);
  }
}

function applySettings() {
  document.getElementById("s-theme").value = settings.theme;
  document.getElementById("s-fontSize").value = settings.fontSize;
  document.getElementById("font-size-val").textContent = settings.fontSize;
  document.getElementById("s-tabSize").value = String(settings.tabSize);
  document.getElementById("s-wordWrap").checked = settings.wordWrap;
  document.getElementById("s-autoSave").checked = settings.autoSave;
  document.getElementById("s-autoSaveDelay").value = settings.autoSaveDelay / 60000;
  document.getElementById("s-vimMode").checked = settings.vimMode;
  document.getElementById("s-commitMessage").value = settings.commitMessage ?? "User saved.";
  document.getElementById("s-previewFollowDebounce").value = settings.previewFollowDebounce;
  document.getElementById("s-chatSystemPrompt").value = settings.chatSystemPrompt ?? _SETTINGS_DEFAULTS.chatSystemPrompt;
  document.getElementById("s-inlineSystemPrompt").value = settings.inlineSystemPrompt ?? _SETTINGS_DEFAULTS.inlineSystemPrompt;

  // AI settings (provider/model/endpoint live in settings now)
  const provider = settings.aiProvider || "claude";
  document.getElementById("s-ai-provider").value = provider;
  document.getElementById("s-ai-claude-model").value = settings.aiClaudeModel || "";
  document.getElementById("s-ai-ollama-model").value = settings.aiOllamaModel || "";
  document.getElementById("s-ai-ollama-endpoint").value = settings.aiOllamaEndpoint || "http://localhost:11434";
  const isOllama = provider === "ollama";
  document.getElementById("ai-claude-fields").style.display = isOllama ? "none" : "";
  document.getElementById("ai-ollama-fields").style.display = isOllama ? "" : "none";
  _updateOllamaRepoWarning();

  document.body.className = settings.theme === "light" ? "light" : "";
  document.getElementById("cm-host").style.fontSize = settings.fontSize + "px";

  // Refresh AI panel visibility — provider change can unlock the chat UI
  _applyAIConfig();
}

window.onSettingChange = () => {
  const raw = {
    theme: document.getElementById("s-theme").value,
    fontSize: parseInt(document.getElementById("s-fontSize").value),
    tabSize: parseInt(document.getElementById("s-tabSize").value),
    wordWrap: document.getElementById("s-wordWrap").checked,
    autoSave: document.getElementById("s-autoSave").checked,
    autoSaveDelay: Math.round(parseFloat(document.getElementById("s-autoSaveDelay").value) * 60000),
    vimMode: document.getElementById("s-vimMode").checked,
    commitMessage: document.getElementById("s-commitMessage").value || "User saved.",
    previewFollowDebounce: parseInt(document.getElementById("s-previewFollowDebounce").value, 10),
    aiProvider: document.getElementById("s-ai-provider").value,
    aiClaudeModel: document.getElementById("s-ai-claude-model").value.trim(),
    aiOllamaModel: document.getElementById("s-ai-ollama-model").value.trim(),
    aiOllamaEndpoint: document.getElementById("s-ai-ollama-endpoint").value.trim() || "http://localhost:11434",
    chatSystemPrompt: document.getElementById("s-chatSystemPrompt").value || _SETTINGS_DEFAULTS.chatSystemPrompt,
    inlineSystemPrompt: document.getElementById("s-inlineSystemPrompt").value || _SETTINGS_DEFAULTS.inlineSystemPrompt,
  };
  for (const [key, val] of Object.entries(raw)) {
    if (key in repoSettings) repoSettings[key] = val;
    else globalSettings[key] = val;
  }
  settings = { ..._SETTINGS_DEFAULTS, ...globalSettings, ...repoSettings };
  applySettings();
  clearTimeout(settingsSaveTimer);
  settingsSaveTimer = setTimeout(persistBothSettings, 1500);
};

async function persistSettings() {
  const snippets = collectSnippets();
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: globalSettings, snippets }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    reportIssue("Settings save failed: " + extractDetail(d, res.statusText));
  } else {
    flashIndicator("settings-saved");
  }
}

async function persistRepoSettings() {
  if (!workspaceLoaded) return;
  const res = await fetch("/api/settings/repo", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: repoSettings }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    reportIssue("Repo settings save failed: " + extractDetail(d, res.statusText));
  } else {
    flashIndicator("settings-saved");
  }
}

async function persistBothSettings() {
  const tasks = [persistSettings()];
  if (Object.keys(repoSettings).length > 0) tasks.push(persistRepoSettings());
  await Promise.all(tasks);
}

window.setRepoOverride = async (key) => {
  clearTimeout(settingsSaveTimer);
  repoSettings[key] = settings[key];
  renderSettingScopes();
  try { await persistRepoSettings(); } catch (e) { reportIssue("Repo settings save failed: " + e.message); }
};

window.clearRepoOverride = async (key) => {
  delete repoSettings[key];
  settings[key] = globalSettings[key];
  applySettings();
  renderSettingScopes();
  try { await persistRepoSettings(); } catch (e) { reportIssue("Repo settings save failed: " + e.message); }
};

function renderSettingScopes() {
  const note = document.getElementById("settings-repo-note");
  if (note) note.style.display = workspaceLoaded ? "" : "none";
  for (const key of Object.keys(_SETTINGS_DEFAULTS)) {
    const el = document.getElementById(`scope-${key}`);
    if (!el) continue;
    if (!workspaceLoaded) { el.innerHTML = ""; continue; }
    if (key in repoSettings) {
      el.innerHTML = `<span class="scope-badge-repo">repo</span><button class="scope-clear-btn" onclick="clearRepoOverride('${key}')" title="Remove repo override">×</button>`;
    } else {
      el.innerHTML = `<button class="scope-set-btn" onclick="setRepoOverride('${key}')" title="Pin this setting to the current repo">+ repo</button>`;
    }
  }
  _updateOllamaRepoWarning();
}

window.createSettingsRepo = async () => {
  document.getElementById("settingsRepoDialog").close();
  const res = await fetch("/api/settings/repo/create", { method: "POST" });
  if (!res.ok) alert("Failed to create settings repo");
  else await loadSettings();
};

// ── AI config ──────────────────────────────────────────────────────────────

async function loadAIConfig() {
  try {
    const hint = document.getElementById("ollama-cors-hint");
    if (hint) hint.textContent = `OLLAMA_ORIGINS=${window.location.origin}`;
    const res = await fetch("/api/ai/config");
    if (!res.ok) return;
    aiConfig = await res.json();
    _applyAIConfig();
  } catch (_) {}
}

function _applyAIConfig() {
  // Key status
  const keyStatus = document.getElementById("ai-key-status");
  keyStatus.textContent = aiConfig.has_key ? "✓ Key saved" : "No key stored";
  keyStatus.style.color = aiConfig.has_key ? "var(--green)" : "var(--text2)";

  const serverNote = document.getElementById("ai-server-key-note");
  if (serverNote) serverNote.style.display = aiConfig.server_key_active ? "" : "none";

  // Show/hide chat UI vs configure prompt
  const hasAI = aiConfig.has_key || aiConfig.server_key_active || (settings.aiProvider === "ollama");
  document.getElementById("ai-no-config").style.display = hasAI ? "none" : "";
  document.getElementById("ai-chat-ui").style.display = hasAI ? "flex" : "none";
}

function _updateOllamaRepoWarning() {
  const isOllama = settings.aiProvider === "ollama";
  const endpointIsRepo = "aiOllamaEndpoint" in repoSettings;
  const show = isOllama && endpointIsRepo;
  const endpoint = settings.aiOllamaEndpoint || "";

  for (const [warnId, urlId] of [
    ["ollama-repo-endpoint-warning", "ollama-repo-endpoint-url"],
    ["inline-ollama-repo-warning", "inline-ollama-repo-endpoint-url"],
  ]) {
    const warn = document.getElementById(warnId);
    const urlEl = document.getElementById(urlId);
    if (!warn) continue;
    warn.style.display = show ? "" : "none";
    if (urlEl) urlEl.textContent = endpoint;
  }
}

window.onAIProviderChange = () => {
  // Update the settings object immediately so _updateOllamaRepoWarning has the right provider
  onSettingChange();
};

window.saveAIKey = async () => {
  const key = document.getElementById("ai-key-input").value.trim();
  if (!key) return;
  const provider = document.getElementById("s-ai-provider").value;
  const res = await fetch("/api/ai/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, api_key: key }),
  });
  if (res.ok) {
    document.getElementById("ai-key-input").value = "";
    await loadAIConfig();
  } else {
    reportIssue("Failed to save AI key");
  }
};

window.clearAIKey = async () => {
  const res = await fetch("/api/ai/config", { method: "DELETE" });
  if (res.ok) await loadAIConfig();
};

// ── Inline AI prompt ───────────────────────────────────────────────────────

let _inlineSelection = null;   // { from, to } CodeMirror range
let _inlineResponse = null;    // last AI response text

function openInlinePromptDialog(view) {
  const sel = view.state.selection.main;
  const selectedText = sel.empty ? "" : view.state.sliceDoc(sel.from, sel.to);
  _inlineSelection = sel.empty ? null : { from: sel.from, to: sel.to };
  _inlineResponse = null;

  const selBlock = document.getElementById("inline-selection-block");
  const selText = document.getElementById("inline-selection-text");
  selBlock.style.display = selectedText ? "" : "none";
  selText.textContent = selectedText;

  document.getElementById("inline-response-block").style.display = "none";
  document.getElementById("inline-response-text").textContent = "";
  document.getElementById("inline-status").textContent = "";
  document.getElementById("inline-prompt-input").value = "";
  ["inline-replace-btn", "inline-insert-btn", "inline-copy-btn"].forEach(id => {
    document.getElementById(id).style.display = "none";
  });
  document.getElementById("inline-send-btn").style.display = "";

  document.getElementById("inlineAIDialog").showModal();
  setTimeout(() => document.getElementById("inline-prompt-input").focus(), 50);
}

window.inlinePromptKeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendInlinePrompt(); }
};

window.sendInlinePrompt = async () => {
  const prompt = document.getElementById("inline-prompt-input").value.trim();
  if (!prompt) return;

  const status = document.getElementById("inline-status");
  const responseBlock = document.getElementById("inline-response-block");
  const responseText = document.getElementById("inline-response-text");
  status.textContent = "Thinking…";
  responseBlock.style.display = "none";
  document.getElementById("inline-send-btn").style.display = "none";

  const selection = document.getElementById("inline-selection-text").textContent || null;
  const systemPrompt = settings.inlineSystemPrompt || _SETTINGS_DEFAULTS.inlineSystemPrompt;
  const provider = settings.aiProvider || "claude";

  try {
    let text = "";
    if (provider === "ollama") {
      const base = (settings.aiOllamaEndpoint || "http://localhost:11434").replace(/\/$/, "");
      const userContent = selection ? `${prompt}\n\n<selection>\n${selection}\n</selection>` : prompt;
      const res = await fetch(`${base}/v1/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: settings.aiOllamaModel || "llama3",
          messages: [
            { role: "system", content: systemPrompt },
            { role: "user", content: userContent },
          ],
        }),
      });
      if (!res.ok) throw new Error(`Ollama error ${res.status}`);
      const data = await res.json();
      text = data.choices?.[0]?.message?.content || "";
    } else {
      const model = settings.aiClaudeModel || null;
      const res = await fetch("/api/ai/inline", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, selection, system_prompt: systemPrompt, provider, model }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || res.statusText);
      }
      const data = await res.json();
      text = data.text || "";
    }

    _inlineResponse = text;
    responseText.textContent = text;
    responseBlock.style.display = "";
    status.textContent = "";

    const hasSelection = _inlineSelection !== null;
    document.getElementById("inline-replace-btn").style.display = hasSelection ? "" : "none";
    document.getElementById("inline-insert-btn").style.display = "";
    document.getElementById("inline-copy-btn").style.display = "";
  } catch (e) {
    status.textContent = "Error: " + e.message;
    document.getElementById("inline-send-btn").style.display = "";
  }
};

window.applyInlineResponse = (action) => {
  if (!_inlineResponse || !cmView) return;
  document.getElementById("inlineAIDialog").close();

  if (action === "copy") {
    navigator.clipboard.writeText(_inlineResponse).catch(() => {});
    return;
  }

  const text = _inlineResponse;
  if (action === "replace" && _inlineSelection) {
    cmView.dispatch({
      changes: { from: _inlineSelection.from, to: _inlineSelection.to, insert: text },
    });
  } else {
    // insert after selection (or at cursor if no selection)
    const pos = _inlineSelection ? _inlineSelection.to : cmView.state.selection.main.to;
    cmView.dispatch({
      changes: { from: pos, to: pos, insert: "\n" + text },
    });
  }
  cmView.focus();
};

// ── Snippets editor ────────────────────────────────────────────────────────
function renderSnippetsEditor() {
  const container = document.getElementById("snippets-editor");
  container.innerHTML = "";
  userSnippets.forEach((s, i) => {
    const div = document.createElement("div");
    div.className = "snippet-item";
    div.innerHTML = `
      <button class="snippet-del" onclick="deleteSnippet(${i})">✕ Remove</button>
      <div><strong style="font-size:0.68rem">Label</strong>
        <input type="text" class="snip-label" value="${escapeHtml(s.label || "")}" placeholder="trigger-word" oninput="onSettingChange()">
      </div>
      <div><strong style="font-size:0.68rem">Description</strong>
        <input type="text" class="snip-detail" value="${escapeHtml(s.detail || "")}" placeholder="description" oninput="onSettingChange()">
      </div>
      <div><strong style="font-size:0.68rem">Template (use #{placeholder} for tabstops)</strong>
        <textarea class="snip-template" rows="3" oninput="onSettingChange()">${escapeHtml(s.template || "")}</textarea>
      </div>`;
    container.appendChild(div);
  });
}

window.addSnippet = () => {
  userSnippets.push({ label: "", detail: "", template: "" });
  renderSnippetsEditor();
  onSettingChange();
};

window.deleteSnippet = (i) => {
  userSnippets.splice(i, 1);
  renderSnippetsEditor();
  onSettingChange();
};

function collectSnippets() {
  const items = document.querySelectorAll(".snippet-item");
  return [...items].map(el => ({
    label: el.querySelector(".snip-label")?.value || "",
    detail: el.querySelector(".snip-detail")?.value || "",
    template: el.querySelector(".snip-template")?.value || "",
  })).filter(s => s.label);
}

// ── Utilities ──────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
                  .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

function extractDetail(d, fallback) {
  if (typeof d.detail === "string") return d.detail;
  if (d.detail) return JSON.stringify(d.detail);
  return fallback;
}

function reportIssue(msg) {
  _issues.unshift({ time: new Date().toLocaleTimeString(), msg });
  const btn = document.getElementById("sb-issues-btn");
  const cnt = document.getElementById("sb-issues-count");
  if (btn) btn.style.display = "";
  if (cnt) cnt.textContent = _issues.length;
}

window.showIssuesDialog = () => {
  const list = document.getElementById("issues-list");
  list.innerHTML = _issues.map(i =>
    `<div class="issue-item">` +
    `<div class="issue-header"><span class="issue-time">${escapeHtml(i.time)}</span></div>` +
    `<div class="issue-msg">${_ansiUp.ansi_to_html(i.msg)}</div>` +
    `</div>`
  ).join("");
  document.getElementById("issuesDialog").showModal();
};

window.clearIssues = () => {
  _issues.length = 0;
  const btn = document.getElementById("sb-issues-btn");
  if (btn) btn.style.display = "none";
  document.getElementById("issuesDialog").close();
};

function flashIndicator(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.opacity = "1";
  setTimeout(() => { el.style.opacity = "0"; }, 2000);
}

// ── Auth ───────────────────────────────────────────────────────────────────
window.logout = async () => {
  await fetch("/api/session", { method: "DELETE" });
  location.href = "/login";
};

// ── Pane resize ────────────────────────────────────────────────────────────
function initResize(handleId, fixedId, storageKey, side) {
  // side: "left" means fixedEl is left of editor, "right" means right of editor
  const handle = document.getElementById(handleId);
  const fixedEl = document.getElementById(fixedId);
  if (!handle || !fixedEl) return;

  const saved = parseInt(localStorage.getItem(storageKey));
  if (saved && saved > 0) { fixedEl.style.width = saved + "px"; fixedEl.style.minWidth = saved + "px"; }

  function beginDrag(startX) {
    const startW = fixedEl.getBoundingClientRect().width;
    handle.classList.add("dragging");

    function apply(clientX) {
      const dx = clientX - startX;
      const newW = Math.max(100, side === "left" ? startW + dx : startW - dx);
      fixedEl.style.width = newW + "px";
      fixedEl.style.minWidth = newW + "px";
      localStorage.setItem(storageKey, newW);
    }
    function end() {
      handle.classList.remove("dragging");
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      document.removeEventListener("touchmove", onTouchMove);
      document.removeEventListener("touchend", onTouchEnd);
    }
    function onMouseMove(e) { apply(e.clientX); }
    function onMouseUp() { end(); }
    function onTouchMove(e) { apply(e.touches[0].clientX); }
    function onTouchEnd() { end(); }

    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    document.addEventListener("touchmove", onTouchMove, { passive: false });
    document.addEventListener("touchend", onTouchEnd);
  }

  handle.addEventListener("mousedown", e => { e.preventDefault(); beginDrag(e.clientX); });
  handle.addEventListener("touchstart", e => { e.preventDefault(); beginDrag(e.touches[0].clientX); }, { passive: false });
}

// ── Logs ───────────────────────────────────────────────────────────────────
let _logsPoller = null;
let _logsLineCount = 0;
const _ansiUp = new AnsiUp();
_ansiUp.escape_for_html = true;

async function pollLogs() {
  try {
    const res = await fetch("/api/preview/logs");
    if (!res.ok) return;
    const { lines, running } = await res.json();
    if (!_previewStarting) {
      const sbStatus = document.getElementById("sb-preview-status");
      if (sbStatus) {
        sbStatus.textContent = running ? "● Running" : "● Stopped";
        sbStatus.className = "sb-preview-status " + (running ? "running" : "stopped");
      }
    }
    if (lines.length !== _logsLineCount) {
      _logsLineCount = lines.length;
      const out = document.getElementById("logs-output");
      if (out) {
        out.innerHTML = _ansiUp.ansi_to_html(lines.join("\n"));
        out.scrollTop = out.scrollHeight;
      }
    }
  } catch (_) {}
}

function startLogsPolling() {
  if (_logsPoller) return;
  pollLogs();
  _logsPoller = setInterval(pollLogs, 2000);
}

function stopLogsPolling() {
  clearInterval(_logsPoller);
  _logsPoller = null;
}

window.clearLogs = () => {
  _logsLineCount = 0;
  const out = document.getElementById("logs-output");
  if (out) out.innerHTML = "";
};


// ── Boot ───────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  const host = document.getElementById("cm-host");
  host.style.cssText = "flex:1;overflow:hidden;display:none;";

  initResize("resize-left", "sidebar", "pane-left", "left");
  initResize("resize-right", "right-panel", "pane-right", "right");
  updateViewMenuChecks();
  renderEditMenu();

  await loadSettings();
  await loadAIConfig();
  loadRepos();

  if (window._tpl.has_workspace) {
    await loadFileTree();
    startLogsPolling();
  }
});

// ── Terminal ────────────────────────────────────────────────────────────────

async function _loadTermDeps() {
  // xterm is bundled (imported statically at the top); just wire the classes
  // through. Kept async so the terminal call sites don't need to change.
  if (_Terminal) return;
  _Terminal = _XTermTerminal;
  _FitAddon = _XTermFitAddon;
}

function _connectTerminalWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/terminal/ws`);
  ws.binaryType = "arraybuffer";
  _termWs = ws;

  ws.onopen = () => {
    _termConnected = true;
    if (_termFit) {
      _termFit.fit();
      const dims = _termFit.proposeDimensions();
      if (dims) ws.send(JSON.stringify({ type: "resize", cols: dims.cols, rows: dims.rows }));
    }
    _term?.focus();
  };

  ws.onmessage = (e) => {
    if (!_term) return;
    if (e.data instanceof ArrayBuffer) {
      _term.write(new Uint8Array(e.data));
    } else {
      _term.write(e.data);
    }
  };

  ws.onclose = () => {
    _termConnected = false;
    _term?.write("\r\n\x1b[2m[disconnected — click ↺ to reconnect]\x1b[0m\r\n");
  };

  ws.onerror = () => {
    _term?.write("\r\n\x1b[31m[connection error]\x1b[0m\r\n");
  };
}

window.initTerminal = async () => {
  if (!workspaceLoaded) {
    const el = document.getElementById("terminal-container");
    if (el && !el.dataset.msgShown) {
      el.innerHTML = '<div style="padding:0.75rem;color:var(--text2);font-size:0.8rem">Load a workspace first.</div>';
      el.dataset.msgShown = "1";
    }
    return;
  }

  if (_term && _termWs && _termWs.readyState === WebSocket.OPEN) {
    _termFit?.fit();
    _term.focus();
    return;
  }

  if (_term && (!_termWs || _termWs.readyState > WebSocket.OPEN)) {
    _connectTerminalWS();
    return;
  }

  await _loadTermDeps();

  const container = document.getElementById("terminal-container");
  container.innerHTML = "";
  delete container.dataset.msgShown;

  _term = new _Terminal({
    cursorBlink: true,
    fontFamily: '"JetBrains Mono", "Fira Code", monospace',
    fontSize: 13,
    theme: { background: "#1a1a1a", foreground: "#d4d4d4", cursor: "#e8974a" },
    convertEol: true,
  });
  _termFit = new _FitAddon();
  _term.loadAddon(_termFit);
  _term.open(container);
  _termFit.fit();

  _term.onData((data) => {
    if (_termConnected && _termWs) {
      _termWs.send(new TextEncoder().encode(data).buffer);
    }
  });

  _term.onResize(({ cols, rows }) => {
    if (_termConnected && _termWs) {
      _termWs.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });

  new ResizeObserver(() => { _termFit?.fit(); }).observe(container);

  _connectTerminalWS();
};

window.restartTerminal = async () => {
  if (_termWs) { _termWs.close(); _termWs = null; }
  if (_term) { _term.dispose(); _term = null; _termFit = null; }
  _termConnected = false;
  const container = document.getElementById("terminal-container");
  if (container) { container.innerHTML = ""; delete container.dataset.msgShown; }
  await initTerminal();
};

// ── Keybindings ─────────────────────────────────────────────────────────────

document.addEventListener("keydown", (e) => {
  if (!e.ctrlKey || e.altKey || e.metaKey) return;
  for (const group of APP_KEYBINDINGS) {
    for (const binding of group.items) {
      if (!!binding.shift !== e.shiftKey) continue;
      // if (e.key !== binding.eKey && e.key !== (binding.eKeyAlt ?? null)) continue;
      if (e.key !== binding.eKey) continue;
      e.preventDefault();
      e.stopPropagation();
      binding.action();
      return;
    }
  }
}, true);

document.addEventListener("keydown", (e) => {
  if (e.key !== "Delete") return;
  const el = document.activeElement;
  if (!el?.classList.contains("tree-item") || !el.dataset.path) return;
  e.preventDefault();
  _contextTarget = { path: el.dataset.path, type: el.dataset.type };
  deleteContextTarget();
}, true);
