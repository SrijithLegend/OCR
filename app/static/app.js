"use strict";
// All user/model text is rendered with textContent / .value only. Never innerHTML.
const $ = (id) => document.getElementById(id);
const CASES = ["minimum", "normal", "maximum", "design", "single", "other"];
const TITLES = {minimum: "Min", normal: "Normal", maximum: "Max", design: "Design", single: "Single", other: "Other"};
const ALWAYS = ["minimum", "normal", "maximum"];
let job = null, pollTimer = null;
const pending = new Set();   // in-flight saves; export waits for them

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}
const store = {
  get() { try { return localStorage.getItem("jobId"); } catch { return null; } },
  set(v) { try { v ? localStorage.setItem("jobId", v) : localStorage.removeItem("jobId"); } catch {} },
};

async function api(method, url, body) {
  const opt = {method, headers: {}};
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}
function tracked(p) { pending.add(p); const done = () => pending.delete(p); p.then(done, done); return p; }

// ---------------------------------------------------------------- upload
function showError(msg) {
  const e = $("error");
  e.textContent = msg;
  e.classList.toggle("hidden", !msg);
}
async function upload(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  showError("");
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  setView("progress", "Uploading", files.map((f) => f.name).join(", "));
  try {
    const {id} = await api("POST", "/api/jobs", fd);
    store.set(id);
    poll(id);
  } catch (e) {
    setView("drop");
    showError("Upload failed: " + e.message);
  }
}
function setView(view, title, text) {
  $("drop").classList.toggle("hidden", view !== "drop");
  $("progress").classList.toggle("hidden", view !== "progress");
  $("results").classList.toggle("hidden", view !== "results");
  $("btn-new").classList.toggle("hidden", view === "drop");
  $("btn-delete").classList.toggle("hidden", view === "drop");
  if (title) $("progress-title").textContent = title;
  if (text !== undefined) $("progress-text").textContent = text;
}
function poll(id) {
  clearTimeout(pollTimer);
  const tick = async () => {
    try {
      job = await api("GET", "/api/jobs/" + id);
    } catch (e) {
      store.set(null); setView("drop"); return;
    }
    if (job.status === "done") { setView("results"); render(); return; }
    if (job.status === "error") { setView("drop"); showError("Extraction failed: " + (job.error || "unknown error")); return; }
    setView("progress", "Reading datasheets", job.progress);
    pollTimer = setTimeout(tick, 1500);
  };
  tick();
}

// ---------------------------------------------------------------- state helpers
function sev(c) { return c.flags.some((f) => f.severity === "error") ? "error" : c.flags.some((f) => f.severity === "warning") ? "warning" : ""; }
function stateOf(c) { return c.edited ? "edited" : c.reviewed ? "reviewed" : sev(c); }
function needsReview(c) { return !c.edited && !c.reviewed && sev(c) !== ""; }
function groupRows(cells) {
  const m = new Map();
  for (const c of cells) {
    const k = [(c.section || "").toLowerCase().trim(), c.parameter.toLowerCase().trim(), c.kind].join("\u0001");
    if (!m.has(k)) m.set(k, {section: c.section || "", cells: [], kind: c.kind});
    m.get(k).cells.push(c);
  }
  return [...m.values()];
}
function decorate(input, c) {
  input.classList.remove("error", "warning", "edited", "reviewed");
  const s = c ? stateOf(c) : "";
  if (s) input.classList.add(s);
  const tips = [];
  if (c) {
    c.flags.forEach((f) => tips.push(f.severity.toUpperCase() + ": " + f.message));
    if (c.edited) tips.push("You changed this. Model value: " + (c.original_value ?? "(empty)") + (c.original_unit ? " " + c.original_unit : ""));
  }
  input.title = tips.join("\n");
}
function updateSummary() {
  let cells = 0, open = 0, edited = 0;
  for (const s of job.sheets) for (const c of s.cells) { cells++; if (needsReview(c)) open++; if (c.edited) edited++; }
  $("summary").textContent = `${job.sheets.length} sheet(s), ${cells} cells · ${open} still need review · ${edited} edited by you`;
  applyFilter();
}
function applyFilter() {
  const only = $("only-flagged").checked;
  document.querySelectorAll("tr[data-row]").forEach((tr) => {
    const open = tr.querySelector("input.cell.error, input.cell.warning");
    tr.classList.toggle("hidden", only && !open);
  });
}

// ---------------------------------------------------------------- saving
function saveCell(c, patch, input, rowEls) {
  return tracked(api("PATCH", `/api/jobs/${job.id}/cells/${c.id}`, patch).then((nc) => {
    Object.assign(c, nc);
    decorate(input, c);
    rowEls?.refresh();
    updateSummary();
  }).catch((e) => { showError("Save failed: " + e.message); }));
}

// ---------------------------------------------------------------- rendering
function cellInput(sheet, row, rowCtx, caseName) {
  const c = row.cells.find((x) => x.case === caseName);
  const input = h("input", {class: "cell", type: "text", value: c ? c.value ?? "" : "", "aria-label": TITLES[caseName]});
  input.value = c ? c.value ?? "" : "";
  decorate(input, c);
  input.addEventListener("change", async () => {
    const cur = row.cells.find((x) => x.case === caseName);
    if (cur) return saveCell(cur, {value: input.value}, input, rowCtx);
    if (!input.value.trim()) return;
    const rep = row.cells[0];
    tracked(api("POST", `/api/jobs/${job.id}/sheets/${sheet.id}/cells`, {
      parameter: rep.parameter, case: caseName, value: input.value, unit: rep.unit, section: rep.section, kind: rep.kind,
    }).then((nc) => {
      sheet.cells.push(nc); row.cells.push(nc); decorate(input, nc); rowCtx.refresh(); updateSummary();
    }).catch((e) => showError("Save failed: " + e.message)));
  });
  return input;
}
function rowTr(sheet, row, i, cols) {
  const rep = row.cells[0];
  const ctx = {refresh() { flagTxt.textContent = rowFlags(); }};
  const rowFlags = () => row.cells.flatMap((c) => (c.edited || c.reviewed) ? [] : c.flags.map((f) => f.message)).filter((m, j, a) => a.indexOf(m) === j).join(" · ");
  const flagTxt = h("div", {class: "rowflag"}, rowFlags());

  const par = h("input", {class: "cell param", type: "text", "aria-label": "Parameter"});
  par.value = rep.parameter;
  par.addEventListener("change", () => Promise.all(row.cells.map((c) => saveCell(c, {parameter: par.value}, par, ctx))));
  const unitVals = [...new Set(row.cells.map((c) => c.unit).filter(Boolean))];
  const unit = h("input", {class: "cell unit", type: "text", "aria-label": "Unit"});
  unit.value = unitVals.join(" / ");
  unit.addEventListener("change", () => Promise.all(row.cells.map((c) => saveCell(c, {unit: unit.value}, unit, ctx))));

  const tr = h("tr", {"data-row": "1"},
    h("td", {class: "rn"}, rep.row_no ?? ""),
    h("td", {}, par), h("td", {}, unit),
    cols.map((cn) => h("td", {}, cellInput(sheet, row, ctx, cn))),
    h("td", {}, h("a", {class: "page", tabindex: "0", role: "button", onclick: () => openPage(sheet, rep.page),
      onkeydown: (e) => { if (e.key === "Enter") openPage(sheet, rep.page); }}, "p" + rep.page)),
    h("td", {}, flagTxt),
    h("td", {class: "act"},
      h("button", {class: "small", type: "button", title: "Mark this row as checked against the source", onclick: () => {
        Promise.all(row.cells.map((c) => api("PATCH", `/api/jobs/${job.id}/cells/${c.id}`, {reviewed: true}).then((nc) => Object.assign(c, nc))))
          .then(() => { renderSheets(); }).catch((e) => showError("Save failed: " + e.message));
      }}, "OK"),
      " ",
      h("button", {class: "small danger", type: "button", title: "Delete this row", onclick: () => {
        if (!confirm("Delete this row?")) return;
        Promise.all(row.cells.map((c) => api("DELETE", `/api/jobs/${job.id}/cells/${c.id}`)))
          .then(() => { sheet.cells = sheet.cells.filter((c) => !row.cells.includes(c)); renderSheets(); })
          .catch((e) => showError("Delete failed: " + e.message));
      }}, "×")));
  return tr;
}
function addRow(sheet, cols) {
  const par = h("input", {class: "cell param", type: "text", placeholder: "New parameter"});
  const unit = h("input", {class: "cell unit", type: "text", placeholder: "unit"});
  const vals = cols.map((cn) => h("input", {class: "cell", type: "text", placeholder: TITLES[cn]}));
  const add = h("button", {class: "small", type: "button", onclick: async () => {
    if (!par.value.trim()) { par.focus(); return; }
    const todo = cols.map((cn, i) => ({cn, v: vals[i].value})).filter((x) => x.v.trim());
    if (!todo.length) { vals[0].focus(); return; }
    try {
      for (const {cn, v} of todo) {
        sheet.cells.push(await api("POST", `/api/jobs/${job.id}/sheets/${sheet.id}/cells`,
          {parameter: par.value, case: cn, value: v, unit: unit.value}));
      }
      renderSheets();
    } catch (e) { showError("Add failed: " + e.message); }
  }}, "Add row");
  return h("tr", {class: "addrow"}, h("td"), h("td", {}, par), h("td", {}, unit),
    vals.map((v) => h("td", {}, v)), h("td"), h("td"), h("td", {class: "act"}, add));
}
function table(sheet, cells, withAdd) {
  const used = new Set(cells.map((c) => c.case));
  const cols = CASES.filter((cn) => ALWAYS.includes(cn) || used.has(cn));
  const body = h("tbody");
  let lastSection = null;
  groupRows(cells).forEach((row, i) => {
    if (row.section !== lastSection) {
      lastSection = row.section;
      if (row.section) body.append(h("tr", {class: "section"}, h("td", {colspan: 6 + cols.length}, row.section)));
    }
    body.append(rowTr(sheet, row, i, cols));
  });
  if (withAdd) body.append(addRow(sheet, cols));
  return h("div", {class: "scroll"}, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Parameter"), h("th", {}, "Unit"),
      cols.map((cn) => h("th", {}, TITLES[cn])), h("th", {}, "Page"), h("th", {}, "Flags"), h("th", {}))),
    body));
}
function renderSheets() {
  const root = $("sheets");
  root.replaceChildren();
  for (const s of job.sheets) {
    const main = s.cells.filter((c) => c.kind !== "calculated");
    const calc = s.cells.filter((c) => c.kind === "calculated");
    const card = h("div", {class: "sheet"},
      h("div", {class: "sheet-head"},
        h("h2", {}, s.tag || s.title || "(untitled sheet)"),
        h("span", {class: "meta"}, [s.title && s.tag ? s.title : null, s.fluid ? "Fluid: " + s.fluid : null,
          s.service ? "Service: " + s.service : null, s.file_name, "pages " + (s.pages.join(", ") || "-")].filter(Boolean).join(" · "))));
    if (s.notes.length) card.append(h("div", {class: "notes"}, s.notes.map((f) => h("div", {class: "note " + f.severity}, f.severity.toUpperCase() + ": " + f.message))));
    if (!s.has_values && !main.length) card.append(h("div", {class: "notes muted"}, "No filled process-condition values found on this sheet."));
    card.append(table(s, main, true));
    if (calc.length) card.append(h("details", {class: "calc"}, h("summary", {}, `Calculated values printed in the same table (${calc.length} cells; not process conditions)`), table(s, calc, false)));
    root.append(card);
  }
  updateSummary();
}
function render() { renderSheets(); }

// ---------------------------------------------------------------- page viewer
function openPage(sheet, page) {
  const fi = sheet.file_idx;
  $("modal-title").textContent = `${sheet.file_name} · page ${page}`;
  $("modal-img").src = `/api/jobs/${job.id}/pages/${fi}/${page}.png`;
  $("modal").classList.remove("hidden");
  $("modal-close").focus();
}
function closeModal() { $("modal").classList.add("hidden"); $("modal-img").removeAttribute("src"); }

// ---------------------------------------------------------------- wiring
const drop = $("drop"), picker = $("picker");
drop.addEventListener("click", () => picker.click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); } });
picker.addEventListener("change", () => { upload(picker.files); picker.value = ""; });
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => upload(e.dataTransfer.files));
// a file dropped outside the zone must not navigate away from the page
["dragover", "drop"].forEach((t) => window.addEventListener(t, (e) => e.preventDefault()));

$("only-flagged").addEventListener("change", applyFilter);
$("modal-close").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
$("btn-new").addEventListener("click", () => { clearTimeout(pollTimer); job = null; store.set(null); showError(""); setView("drop"); });
$("btn-export").addEventListener("click", async () => {
  await Promise.allSettled([...pending]);
  window.location.href = `/api/jobs/${job.id}/export.xlsx`;
});
let armed = null;
$("btn-delete").addEventListener("click", async () => {
  const b = $("btn-delete");
  if (!armed) {
    b.textContent = "Click again to confirm";
    armed = setTimeout(() => { armed = null; b.textContent = "Delete my data"; }, 4000);
    return;
  }
  clearTimeout(armed); armed = null; b.textContent = "Delete my data";
  clearTimeout(pollTimer);
  try { if (job) await api("DELETE", "/api/jobs/" + job.id); } catch {}
  job = null; store.set(null); setView("drop");
});

(function init() {
  const id = store.get();
  if (id) { setView("progress", "Resuming", ""); poll(id); } else setView("drop");
})();
