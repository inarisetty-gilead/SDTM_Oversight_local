"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });

function msg(el, kind, html) { el.innerHTML = `<div class="msg ${kind}">${html}</div>`; }
function unlock(id) { $(id).classList.remove("locked"); }
function markDone(id) { $(id).classList.add("done"); }

function table(cols, rows, opts = {}) {
  const head = cols.map((c) => `<th class="${c.n ? "n" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map((r, i) => {
    const tds = cols.map((c) => {
      const v = c.html ? c.html(r) : esc(r[c.key] ?? "");
      return `<td class="${c.n ? "n" : ""}">${v}</td>`;
    }).join("");
    return `<tr class="${opts.click ? "clickable" : ""}" data-i="${i}">${tds}</tr>`;
  }).join("");
  return `<div class="tblwrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

const stat = (n, label, kind = "") =>
  `<div class="stat ${kind}"><b>${n}</b><span>${esc(label)}</span></div>`;

// ── file / folder picker ────────────────────────────────────────────────────
let pickTarget = null, pickMode = "dir", pickCwd = "";

async function pickLoad(path) {
  const d = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
  pickCwd = d.path;
  $("pick-cwd").textContent = d.path;
  $("pick-shortcuts").innerHTML = d.shortcuts
    .map((s) => `<button data-p="${esc(s.path)}">${esc(s.name)}</button>`).join("");
  const items = d.dirs.map((x) => `<li data-dir="${esc(x.path)}"><span class="ico">📁</span>${esc(x.name)}</li>`);
  if (pickMode === "file") {
    items.push(...d.files
      .filter((f) => /\.(xlsx|xlsm|xls)$/i.test(f.name))
      .map((f) => `<li data-file="${esc(f.path)}"><span class="ico">📄</span>${esc(f.name)}` +
        `<span class="sz">${(f.size / 1024).toFixed(0)} KB</span></li>`));
  } else {
    const n = d.files.length;
    if (n) items.push(`<li style="color:var(--mut);cursor:default">${n} data file(s) here</li>`);
  }
  $("pick-list").innerHTML = items.join("") || `<li style="color:var(--mut)">empty</li>`;
  $("pick-up").disabled = !d.parent;
  $("pick-up").dataset.p = d.parent || "";
}

function openPicker(targetId, mode) {
  pickTarget = targetId; pickMode = mode;
  $("pick-title").textContent = mode === "file" ? "Choose the spec workbook" : "Choose a folder";
  $("pick-choose").hidden = mode === "file";
  $("modal").hidden = false;
  pickLoad($(targetId).value || "");
}
function closePicker() { $("modal").hidden = true; }

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-browse]");
  if (b) { openPicker(b.dataset.browse, b.dataset.mode); return; }
  if (e.target.closest("#pick-cancel") || e.target.id === "modal") { closePicker(); return; }
  const up = e.target.closest("#pick-up");
  if (up && up.dataset.p) { pickLoad(up.dataset.p); return; }
  const sc = e.target.closest("#pick-shortcuts button");
  if (sc) { pickLoad(sc.dataset.p); return; }
  const li = e.target.closest("#pick-list li");
  if (li) {
    if (li.dataset.dir) pickLoad(li.dataset.dir);
    else if (li.dataset.file) { $(pickTarget).value = li.dataset.file; closePicker(); }
    return;
  }
  if (e.target.closest("#pick-choose")) { $(pickTarget).value = pickCwd; closePicker(); }
});

// ── job polling ─────────────────────────────────────────────────────────────
function pollJob(prefix, onDone) {
  const box = $(`${prefix}-job`), bar = $(`${prefix}-prog`), lbl = $(`${prefix}-lbl`);
  box.hidden = false;
  const timer = setInterval(async () => {
    let j;
    try { j = await api("/api/job"); } catch { return; }
    bar.value = j.percent;
    lbl.textContent = j.message + (j.total ? `  (${j.step}/${j.total})` : "");
    if (j.status === "done") {
      clearInterval(timer); box.hidden = true; onDone(null);
    } else if (j.status === "error") {
      clearInterval(timer); box.hidden = true; onDone(j);
    }
  }, 400);
}


function clearBuildAndCompare(why) {
  $("build-out").innerHTML = "";
  $("cmp-out").innerHTML = "";
  $("s-build").classList.remove("done");
  $("s-cmp").classList.remove("done", "locked");
  $("s-cmp").classList.add("locked");
  selectedDomains = null;
  $("btn-build").textContent = "Build all domains";
  if (why) msg($("build-out"), "warn", esc(why));
}

// ── step 1 · spec ───────────────────────────────────────────────────────────
$("btn-spec").onclick = async () => {
  const out = $("spec-out");
  const path = $("spec-path").value.trim();
  if (!path) return msg(out, "warn", "Choose the mapping spec workbook first.");
  out.innerHTML = `<div class="msg">reading the workbook…</div>`;
  try {
    const d = await post("/api/spec", { path });
    const skipped = d.skipped.length
      ? `<details><summary>${d.skipped.length} sheet(s) skipped</summary>` +
        `<ul>${d.skipped.map((s) => `<li><code>${esc(s.sheet)}</code> — ${esc(s.why)}</li>`).join("")}</ul></details>`
      : "";
    out.innerHTML =
      `<div class="stats">${stat(d.domains.length, "domains")}${stat(d.variables, "spec variables")}` +
      `${stat(d.codelists, "codelists")}</div>` +
      `<div class="msg ok">${esc(d.domains.join(", "))}</div>${skipped}`;
    markDone("s-spec"); unlock("s-raw");
    if (d.cleared) clearBuildAndCompare(
      "The mapping spec changed, so the previous build no longer applies. Build again.");
  } catch (e) { msg(out, "bad", esc(e.message)); }
};

// ── step 2 · raw ────────────────────────────────────────────────────────────
$("btn-raw").onclick = async () => {
  const out = $("raw-out");
  const path = $("raw-path").value.trim();
  if (!path) return msg(out, "warn", "Choose the folder holding the raw datasets.");
  out.innerHTML = `<div class="msg">reading the datasets…</div>`;
  try {
    const d = await post("/api/raw", { path });
    const bad = d.datasets.filter((x) => x.error);
    const rows = d.datasets.map((x) => ({
      name: x.name, rows: x.error ? "—" : x.rows.toLocaleString(),
      cols: x.error ? "—" : x.cols, file: x.file, note: x.error || "",
    }));
    let html =
      `<div class="stats">${stat(d.datasets.length, "raw datasets")}` +
      `${stat(d.coverage.filter((c) => c.resolved > 0).length, "domains with sources")}` +
      `${stat(d.missing.length, "missing sources", d.missing.length ? "warn" : "ok")}</div>`;
    if (d.missing.length) {
      html += `<div class="msg warn"><b>${d.missing.length} source(s) named in the spec are not in
        this folder.</b> Those variables cannot be built — resolve them or expect them in the
        “not built” list.<ul>` +
        d.missing.slice(0, 12).map((m) =>
          `<li><code>${esc(m.source)}</code> — used by ${esc(m.used_by.join(", "))}${m.count > 12 ? " …" : ""}</li>`).join("") +
        (d.missing.length > 12 ? `<li>… and ${d.missing.length - 12} more</li>` : "") + `</ul></div>`;
    } else {
      msg(out, "ok", "");
      html += `<div class="msg ok">Every <code>raw.&lt;dataset&gt;.&lt;column&gt;</code> in the spec resolves.</div>`;
    }
    if (bad.length) html += `<div class="msg bad">${bad.length} file(s) could not be read.</div>`;
    html += table([
      { key: "name", label: "Dataset" }, { key: "rows", label: "Rows", n: true },
      { key: "cols", label: "Columns", n: true }, { key: "file", label: "File" },
      { key: "note", label: "" },
    ], rows);
    out.innerHTML = html;
    markDone("s-raw"); unlock("s-build");
    window.__domains = d.coverage.map((c) => c.domain);
    if (d.cleared) clearBuildAndCompare(
      "The raw data folder changed, so the previous build no longer applies. Build again.");
    else if (d.built && d.built.length) { markDone("s-build"); unlock("s-cmp"); await renderBuild(); }
  } catch (e) { msg(out, "bad", esc(e.message)); }
};

// ── step 3 · build ──────────────────────────────────────────────────────────
let selectedDomains = null;

$("btn-build-sel").onclick = () => {
  const all = window.__domains || [];
  const cur = (selectedDomains || all).join(", ");
  const answer = prompt(`Domains to build (comma separated).\n\nAvailable: ${all.join(", ")}`, cur);
  if (answer === null) return;
  const picked = answer.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
  selectedDomains = picked.length && picked.length !== all.length ? picked : null;
  $("btn-build").textContent = selectedDomains
    ? `Build ${selectedDomains.length} domain(s)` : "Build all domains";
};

$("btn-build").onclick = async () => {
  const out = $("build-out");
  out.innerHTML = "";
  try {
    await post("/api/build", {
      domains: selectedDomains, studyid: $("studyid").value.trim(),
      fmt: $("fmt").value, include_unbuilt: $("incl-unbuilt").checked,
      name_match: parseInt($("namematch").value, 10),
    });
  } catch (e) { return msg(out, "bad", esc(e.message)); }
  $("btn-build").disabled = true;
  pollJob("build", async (err) => {
    $("btn-build").disabled = false;
    if (err) return msg(out, "bad", `${esc(err.error)}<details><summary>details</summary><pre style="font-size:11px;overflow:auto">${esc(err.detail)}</pre></details>`);
    await renderBuild();
    markDone("s-build"); unlock("s-cmp");
  });
};

async function renderBuild() {
  const out = $("build-out");
  const d = await api("/api/build/results");
  const okDoms = d.domains.filter((x) => x.ok);
  const totalRows = okDoms.reduce((a, x) => a + x.rows, 0);
  const nb = d.not_built.length;
  const failed = d.domains.filter((x) => !x.ok);

  let html = `<div class="stats">${stat(okDoms.length, "domains built")}` +
    `${stat(totalRows.toLocaleString(), "records")}` +
    `${stat(okDoms.reduce((a, x) => a + x.built, 0), "variables built")}` +
    `${(() => { const g = okDoms.reduce((a, x) => a + (x.name_matched || 0), 0);
                return g ? stat(g, "matched by name", "warn") : ""; })()}` +
    `${stat(nb, "not built", nb ? "warn" : "ok")}` +
    `${failed.length ? stat(failed.length, "failed", "bad") : ""}</div>`;

  html += table([
    { key: "domain", label: "Domain" },
    { label: "Records", n: true, html: (r) => r.ok ? r.rows.toLocaleString() : "—" },
    { label: "SUPP", n: true, html: (r) => r.supp_rows ? r.supp_rows.toLocaleString() : "" },
    { label: "Built", n: true, html: (r) => r.ok ? r.built : "—" },
    { label: "Dropped", n: true, html: (r) => r.ok ? r.dropped : "—" },
    { label: "Not built", n: true, html: (r) => r.ok
        ? (r.not_built ? `<span class="pill warn">${r.not_built}</span>` : "0") : "—" },
    { key: "base", label: "Base raw dataset" },
    { label: "Prepared", html: (r) => r.prep
        ? `<span class="pill warn">${r.prep.op === "stack" ? "stacked" : "transposed"}</span>` : "" },
    { label: "Name matched", n: true, html: (r) => r.name_matched
        ? `<span class="pill warn">${r.name_matched}</span>` : "" },
    { label: "Hand edits", n: true, html: (r) => r.edited
        ? `<span class="pill edit">${r.edited}</span>` : "" },
    { label: "", html: (r) => r.ok ? "" : `<span class="pill bad">${esc(r.error)}</span>` },
  ], d.domains, { click: true });

  const warns = d.domains.flatMap((x) => x.warnings.map((w) => `<li><b>${esc(x.domain)}</b> — ${esc(w)}</li>`));
  if (warns.length) html += `<details open><summary>${warns.length} build note(s)</summary><ul>${warns.join("")}</ul></details>`;

  const reasons = d.not_built_reasons || [];
  if (reasons.length) {
    html += `<details open><summary>Why ${nb} variable(s) were not built</summary>` +
      `<p class="hint">Coverage is limited by what the mapping spec states, not by the engine.
       Each group below is a different cause, and most have a fix.</p>` +
      table([
        { key: "reason", label: "Cause" },
        { key: "count", label: "Variables", n: true },
        { label: "For example", html: (r) => r.examples.map((x) => `<code>${esc(x)}</code>`).join(" ") },
      ], reasons) +
      `<p class="hint">Variables whose source the spec leaves unstated can often be picked up
       by <b>name matching</b> (the setting above), or mapped by hand in the domain view.</p>` +
      `</details>`;
  }
  if (nb) {
    html += `<details><summary>${nb} variable(s) not built — excluded from the comparison</summary>` +
      table([
        { key: "domain", label: "Domain" }, { key: "variable", label: "Variable" },
        { key: "why", label: "Reason" }, { key: "spec_rule", label: "Spec rule" },
      ], d.not_built) + `</details>`;
  }

  html += `<div class="actions">
      <button class="link" id="btn-open-out">open the output folder</button>
      <button class="link" onclick="window.open('/api/download?name=manifest')">download build manifest (.xlsx)</button>
      <button class="link" onclick="window.open('/api/report')">open the report</button>
    </div>
    <p class="hint">Output folder: <code>${esc(d.out_dir)}</code></p>
    <details><summary>Preview a built dataset</summary>
      <div class="pathrow" style="margin:.5rem 0">
        <select id="prev-dom">${okDoms.map((x) => `<option>${esc(x.domain)}</option>`).join("")}</select>
        <button id="btn-prev">Show first 25 records</button>
      </div><div id="prev-out"></div></details>`;
  out.innerHTML = html;

  // clicking a domain row opens its detail view
  const btbl = out.querySelector("table");
  if (btbl) {
    btbl.querySelectorAll("tbody tr").forEach((tr, i) => {
      tr.onclick = () => openDomain(d.domains[i].domain);
      tr.title = "open the domain detail";
    });
  }
  $("btn-open-out").onclick = () => api("/api/reveal?name=out_dir").catch(() => {});
  $("btn-prev").onclick = async () => {
    const dom = $("prev-dom").value;
    const p = await api(`/api/build/preview/${dom}`);
    $("prev-out").innerHTML =
      `<p class="hint">${p.nrows.toLocaleString()} record(s), ${p.columns.length} variables</p>` +
      `<div class="tblwrap"><table><thead><tr>${p.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>` +
      `<tbody>${p.rows.map((r) => `<tr>${r.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  };
}

// ── domain detail ───────────────────────────────────────────────────────────
let domState = { domain: null, data: null, filter: "all", search: "" };

async function openDomain(domain) {
  domState = { domain, data: null, filter: "all", search: "" };
  $("dom-title").textContent = domain;
  $("dom-body").innerHTML = `<p class="hint">loading…</p>`;
  $("dommodal").hidden = false;
  try {
    domState.data = await api(`/api/domain/${domain}`);
    renderDomain();
  } catch (e) {
    $("dommodal").hidden = true;
    clearBuildAndCompare(`${e.message}`);
    $("s-build").scrollIntoView({ behavior: "smooth", block: "center" });
  }
}
$("dom-close").onclick = () => { $("dommodal").hidden = true; };
$("dommodal").addEventListener("click", (e) => {
  if (e.target.id === "dommodal") $("dommodal").hidden = true;
});

function renderDomain() {
  const d = domState.data, ov = d.override || {}, dd = d.dedup || {};
  $("dom-title").textContent =
    `${d.domain} — ${d.rows.toLocaleString()} record(s) from ${d.base}` +
    (d.supp_rows ? ` · SUPP${d.domain}: ${d.supp_rows}` : "");

  let html = "";
  if (!d.ok) html += `<div class="msg bad">${esc(d.error)}</div>`;

  if (d.prep) {
    html += `<div class="msg warn"><b>Data preparation applied — ` +
      `${d.prep.op === "stack" ? "records stacked from several forms" : "wide form transposed to one record per test"}` +
      `</b><br>${esc(d.prep.note)}</div>`;
  }
  (d.warnings || []).filter((w) => !d.prep || w !== d.prep.note)
    .forEach((w) => { html += `<div class="msg">${esc(w)}</div>`; });

  const nGuessed = d.variables.filter((v) => v.method_source === "name_match"
                                        && v.status === "built").length;
  if (nGuessed) {
    html += `<div class="msg warn"><b>${nGuessed} variable(s) have no source in the mapping
      spec and were matched to a raw column by name.</b> They are built, but they are guesses.
      Agreement with the vendor on these shows the two guesses coincide — it is not evidence
      that the spec was followed. Open each one to confirm or replace the source.</div>`;
  }
  const nEdits = Object.keys(d.edits || {}).length;
  if (nEdits) {
    html += `<div class="msg warn"><b>${nEdits} mapping(s) in ${esc(d.domain)} are set by hand,
      not by the spec.</b> For those variables this build is not an independent rebuild, so the
      vendor comparison is not independent evidence either. They are listed on the
      <i>Hand Edits</i> sheet of the build manifest and in the report.
      <button class="link" id="ov-clear-edits">revert all of them to the spec</button></div>`;
  }
  html += `<div class="stats">${stat(d.counts.built, "built", "ok")}` +
    `${stat(d.counts.dropped, "dropped by spec")}` +
    `${stat(d.counts.not_built + d.counts.error, "not built",
        (d.counts.not_built + d.counts.error) ? "warn" : "ok")}` +
    `${stat(d.rows.toLocaleString(), "records")}</div>`;

  const dsOpts = ['<option value="">auto — detect from the spec</option>']
    .concat(d.datasets.map((x) => `<option ${ov.base === x ? "selected" : ""}>${esc(x)}</option>`)).join("");
  const pm = ov.prep_mode || "auto";
  html += `<div class="ctrls">
      <div class="opt"><label for="ov-base">Record source (base dataset)</label>
        <select id="ov-base" style="min-width:15rem">${dsOpts}</select></div>
      <div class="opt"><label for="ov-prep">Data preparation</label>
        <select id="ov-prep">
          <option value="auto" ${pm === "auto" ? "selected" : ""}>auto — detect stack / transpose</option>
          <option value="off" ${pm === "off" ? "selected" : ""}>off — use one raw form as-is</option>
        </select></div>
      <div class="opt"><label for="ov-sort">Sort before --SEQ</label>
        <input type="text" id="ov-sort" style="width:16rem" placeholder="e.g. USUBJID, ${esc(d.domain)}DTC"
               value="${esc((ov.sort || []).join(', '))}"></div>
      <div class="opt"><label for="ov-keys">Comparison keys</label>
        <input type="text" id="ov-keys" style="width:16rem" placeholder="auto — derived per domain"
               value="${esc((ov.keys || []).join(', '))}"></div>
      <button class="primary" id="ov-apply">Save &amp; rebuild ${esc(d.domain)}</button>
    </div>
    <div class="ctrls">
      <label class="check"><input type="checkbox" id="ov-dd" ${dd.enabled ? "checked" : ""}>
        keep only one record per group</label>
      <div class="opt"><label for="ov-ddkeys">Group by</label>
        <input type="text" id="ov-ddkeys" style="width:18rem" placeholder="e.g. USUBJID, VISITNUM"
               value="${esc((dd.keys || []).join(', '))}"></div>
      <div class="opt"><label for="ov-ddkeep">Keep</label>
        <select id="ov-ddkeep">
          <option value="first" ${dd.keep !== "last" ? "selected" : ""}>first record</option>
          <option value="last" ${dd.keep === "last" ? "selected" : ""}>last record</option>
        </select></div>
      <span class="fieldhelp">Applied after the columns are built and before --SEQ numbering.</span>
    </div>
    <div id="ov-job" hidden><progress id="ov-prog" max="100" value="0"></progress>
      <div class="joblbl" id="ov-lbl"></div></div>`;

  const counts = { all: d.variables.length };
  for (const k of ["built", "dropped", "not_built", "error"])
    counts[k] = d.variables.filter((v) => v.status === k).length;
  const chip = (k, label) =>
    `<button class="chip ${domState.filter === k ? "on" : ""}" data-f="${k}">${label} ${counts[k] ?? 0}</button>`;
  html += `<div class="filters">${chip("all", "All")}${chip("built", "Built")}` +
    `${chip("dropped", "Dropped")}${chip("not_built", "Not built")}` +
    `${counts.error ? chip("error", "Errors") : ""}` +
    `<input type="text" id="ov-search" placeholder="filter by variable, label or source"
            value="${esc(domState.search)}"></div>`;
  html += pipelineSection(d);
  html += `<div id="dom-vars"></div>`;
  $("dom-body").innerHTML = html;
  wirePipelineSection(d);

  $("dom-body").querySelectorAll(".chip").forEach((b) => {
    b.onclick = () => { domState.filter = b.dataset.f; renderDomain(); };
  });
  $("ov-search").oninput = (e) => {
    domState.search = e.target.value;
    renderVars();
    $("ov-search").focus();
  };
  $("ov-apply").onclick = applyOverride;
  const clr = $("ov-clear-edits");
  if (clr) clr.onclick = async () => {
    await api(`/api/domain/${d.domain}/edits`, { method: "DELETE" });
    await post(`/api/domain/${d.domain}/build`, {});
    afterDomainRebuild(d.domain);
  };
  renderVars();
}

function renderVars() {
  const d = domState.data;
  const q = domState.search.trim().toLowerCase();
  let rows = d.variables;
  if (domState.filter !== "all") rows = rows.filter((v) => v.status === domState.filter);
  if (q) rows = rows.filter((v) =>
    [v.variable, v.label, v.source, v.how, v.spec_input].join(" ").toLowerCase().includes(q));

  $("dom-vars").innerHTML = rows.length ? table([
    { key: "variable", label: "Variable" },
    { key: "label", label: "Label" },
    { label: "Status", html: (r) => `<span class="st-${r.status}">${r.status.replace("_", " ")}</span>` },
    { label: "How it was built", html: (r) => esc(r.how) },
    { label: "Source", html: (r) => r.source ? `<code>${esc(r.source)}</code>`
        : (r.constant ? `<code>"${esc(r.constant)}"</code>` : "") },
    { label: "Populated", n: true, html: (r) => r.populated === null ? "" : r.populated.toLocaleString() },
    { label: "Values", html: (r) => `<span class="samples">${r.samples.map((x) => `<code>${esc(x)}</code>`).join("")}</span>` },
    { key: "codelist", label: "Codelist" },
    { label: "Reason / error", html: (r) => `<span class="${r.error ? "st-error" : ""}">${esc(r.error || r.reason)}</span>` },
    { label: "Mapped by", html: (r) => (r.method_source === "edit" || r.edited)
        ? `<span class="pill edit">hand edit</span>`
        : r.method_source === "name_match"
        ? `<span class="pill warn">name match ${r.confidence}%</span>`
        : `<span class="st-dropped">spec</span>` },
    { label: "Spec row", n: true, html: (r) => r.spec_row || "" },
  ], rows, { click: true }) : `<p class="hint">no variables match</p>`;

  const t = $("dom-vars").querySelector(".tblwrap");
  if (t) t.style.maxHeight = "22rem";
  const tb = $("dom-vars").querySelector("table");
  if (tb) {
    tb.querySelectorAll("tbody tr").forEach((tr, i) => {
      if (rows[i].edited) tr.classList.add("edited");
      tr.title = "edit this mapping";
      tr.onclick = () => openEditor(rows[i].variable);
    });
  }
}

async function applyOverride() {
  const d = domState.data;
  const split = (v) => v.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean);
  $("ov-apply").disabled = true;
  try {
    await post(`/api/domain/${d.domain}/settings`, {
      base: $("ov-base").value, sort: split($("ov-sort").value),
      prep_mode: $("ov-prep").value, keys: split($("ov-keys").value),
    });
    await post(`/api/domain/${d.domain}/dedup`, {
      enabled: $("ov-dd").checked, keys: split($("ov-ddkeys").value), keep: $("ov-ddkeep").value,
    });
    await post(`/api/domain/${d.domain}/build`, {});
  } catch (e) {
    $("ov-apply").disabled = false;
    $("dom-body").insertAdjacentHTML("afterbegin", `<div class="msg bad">${esc(e.message)}</div>`);
    return;
  }
  pollJob("ov", async (err) => {
    $("ov-apply").disabled = false;
    if (err) {
      $("dom-body").insertAdjacentHTML("afterbegin", `<div class="msg bad">${esc(err.error)}</div>`);
      return;
    }
    await openDomain(d.domain);
    await renderBuild();
    // the previous comparison was against the old build — clear it rather than leave it stale
    const co = $("cmp-out");
    if (co.innerHTML) msg(co, "warn",
      `${esc(d.domain)} was rebuilt, so the earlier comparison is out of date. Run it again.`);
  });
}



// ── data-preparation pipeline ───────────────────────────────────────────────
let PREPOPS = null;
let pipeState = { steps: [], reports: [], outputs: {} };

async function loadPrepOps() {
  if (!PREPOPS) PREPOPS = await api("/api/prep/ops");
  return PREPOPS;
}

// which parameter fields each operation needs. "ds" = a dataset the pipeline can read
// (raw datasets plus every earlier step's output).
const STEP_FIELDS = {
  stack: [{ k: "datasets", t: "dslist" }],
  merge: [{ k: "inputs", t: "mergeinputs" }, { k: "on", t: "list", ph: "USUBJID" },
          { k: "how", t: "choice", options: ["left", "inner", "outer", "right"] }],
  filter: [{ k: "dataset", t: "ds" }, { k: "conds", t: "conds" }],
  select: [{ k: "dataset", t: "ds" }, { k: "columns", t: "cols" }],
  drop: [{ k: "dataset", t: "ds" }, { k: "columns", t: "cols" }],
  rename: [{ k: "dataset", t: "ds" }, { k: "renames", t: "renames" }],
  derive: [{ k: "dataset", t: "ds" }, { k: "target", t: "text", ph: "new column name" },
           { k: "else_value", t: "text", ph: "value when no rule matches" },
           { k: "rules", t: "deriverules" }],
  aggregate: [{ k: "dataset", t: "ds" }, { k: "group_by", t: "cols" },
              { k: "column", t: "col" },
              { k: "func", t: "choice", options: ["min", "max", "first", "last", "count", "sum", "mean"] },
              { k: "out_col", t: "text" }],
  date_extreme: [{ k: "sources", t: "datesources" }, { k: "group_by", t: "list", ph: "USUBJID" },
                 { k: "func", t: "choice", options: ["min", "max"] }, { k: "out_col", t: "text" }],
  sort: [{ k: "dataset", t: "ds" }, { k: "columns", t: "cols" },
         { k: "directions", t: "list", ph: "asc, desc" }],
  dedup: [{ k: "dataset", t: "ds" }, { k: "keys", t: "cols" },
          { k: "keep", t: "choice", options: ["first", "last"] }],
  split: [{ k: "dataset", t: "ds" }, { k: "branches", t: "branches" },
          { k: "other_name", t: "text", ph: "name for the remaining records" }],
  transpose_long: [{ k: "dataset", t: "ds" }, { k: "id_vars", t: "cols" },
                   { k: "value_vars", t: "cols" }, { k: "var_name", t: "text", ph: "TESTCD" },
                   { k: "value_name", t: "text", ph: "ORRES" }],
  transpose_findings: [{ k: "dataset", t: "ds" }, { k: "id_vars", t: "cols" },
                       { k: "measures", t: "json" }, { k: "testcd_col", t: "text" },
                       { k: "test_col", t: "text" }, { k: "orres_col", t: "text" },
                       { k: "orresu_col", t: "text" }],
};

function availableDatasets(uptoIndex) {
  const d = domState.data;
  const earlier = pipeState.steps.slice(0, uptoIndex)
    .map((st, i) => st.name || `prep${i + 1}`);
  return [...earlier, ...d.datasets];
}

async function renderPipeline() {
  await loadPrepOps();
  const host = $("pipe-body");
  if (!pipeState.steps.length) {
    host.innerHTML = `<p class="hint" style="padding:.8rem .85rem">No preparation steps.
      The build uses the detected record source as-is. Add a step to stack, merge, filter,
      reshape or summarise the raw data first.</p>`;
    return;
  }
  host.innerHTML = pipeState.steps.map((st, i) => {
    const opts = PREPOPS.ops.map((o) =>
      `<option value="${o.id}" ${st.op === o.id ? "selected" : ""}>${esc(o.label)}</option>`).join("");
    const rep = pipeState.reports.find((r) => r.step === i + 1);
    const out = rep
      ? (rep.ok
        ? `<div class="step-out">→ <b>${esc(rep.name)}</b>: ${rep.rows.toLocaleString()} records,
           ${rep.columns.length} columns${rep.extra_outputs?.length
             ? ` · also produced ${rep.extra_outputs.map(esc).join(", ")}` : ""}</div>`
        : `<div class="step-out bad">✗ ${esc(rep.error)}</div>`)
      : "";
    return `<div class="step-card" data-i="${i}">
      <div class="step-head">
        <span class="step-n">${i + 1}</span>
        <select data-f="op">${opts}</select>
        <input type="text" class="nm" data-f="name" value="${esc(st.name || "")}"
               placeholder="output name" title="the name later steps and mappings use">
        <span class="sp"></span>
        <button class="tiny" data-act="up" ${i === 0 ? "disabled" : ""}>↑</button>
        <button class="tiny" data-act="down" ${i === pipeState.steps.length - 1 ? "disabled" : ""}>↓</button>
        <button class="tiny" data-act="del">Remove</button>
      </div>
      <div class="step-body">${stepFields(st, i)}</div>
      ${out}</div>`;
  }).join("");
  wirePipeline();
}

function stepFields(st, idx) {
  const fields = STEP_FIELDS[st.op] || [];
  const p = st.params || {};
  const dsOpts = (sel) => ['<option value="">—</option>'].concat(
    availableDatasets(idx).map((x) =>
      `<option ${sel === x ? "selected" : ""}>${esc(x)}</option>`)).join("");

  return `<div class="grid">` + fields.map((f) => {
    const v = p[f.k];
    const id = `p-${idx}-${f.k}`;
    let input;
    if (f.t === "ds") {
      input = `<select id="${id}" data-p="${f.k}">${dsOpts(v)}</select>`;
    } else if (f.t === "dslist") {
      const chosen = Array.isArray(v) ? v : [];
      input = `<select id="${id}" data-p="${f.k}" data-multi="1" multiple size="4">` +
        availableDatasets(idx).map((x) =>
          `<option ${chosen.includes(x) ? "selected" : ""}>${esc(x)}</option>`).join("") + `</select>`;
    } else if (f.t === "choice") {
      input = `<select id="${id}" data-p="${f.k}">` + ['<option value="">—</option>'].concat(
        f.options.map((x) => `<option ${v === x ? "selected" : ""}>${esc(x)}</option>`)).join("") + `</select>`;
    } else if (f.t === "list" || f.t === "cols") {
      input = `<input type="text" id="${id}" data-p="${f.k}" data-list="1"
        placeholder="${esc(f.ph || "comma separated")}"
        value="${esc(Array.isArray(v) ? v.join(", ") : (v || ""))}">`;
    } else if (f.t === "col") {
      input = `<input type="text" id="${id}" data-p="${f.k}" value="${esc(v || "")}">`;
    } else if (["conds", "deriverules", "branches", "renames", "mergeinputs",
                "datesources", "json"].includes(f.t)) {
      input = `<textarea id="${id}" data-p="${f.k}" data-json="1" rows="3"
        placeholder="${esc(JSON_HINT[f.t] || "")}">${esc(
          v === undefined ? "" : JSON.stringify(v))}</textarea>`;
    } else {
      input = `<input type="text" id="${id}" data-p="${f.k}"
        placeholder="${esc(f.ph || "")}" value="${esc(v ?? "")}">`;
    }
    const wide = ["conds", "deriverules", "branches", "renames", "mergeinputs",
                  "datesources", "json"].includes(f.t);
    return `<div class="opt" ${wide ? 'style="grid-column:1/-1"' : ""}>
      <label>${esc(f.k)}</label>${input}
      ${JSON_HINT[f.t] ? `<div class="fieldhelp">${esc(JSON_HINT[f.t])}</div>` : ""}</div>`;
  }).join("") + `</div>`;
}

const JSON_HINT = {
  conds: '[{"column": "DSCAT", "operator": "==", "value": "PROTOCOL MILESTONE"}] — operators: ' +
         '==, !=, contains, startswith, endswith, in, notin, missing, notmissing, >, <, >=, <=',
  deriverules: '[{"conds": [{"column": "DSCAT", "operator": "==", "value": "X"}], "value": "MILESTONE"}]',
  branches: '[{"name": "milestones", "conds": [{"column": "DSCAT", "operator": "==", "value": "X"}]}]',
  renames: '[{"from": "DSSTDAT", "to": "DSSTDTC"}]',
  mergeinputs: '[{"dataset": "ae"}, {"dataset": "dm", "columns": ["SEXCD", "ARMCD"]}]',
  datesources: '[{"dataset": "ae", "date_col": "AESTDAT"}, {"dataset": "vs", "date_col": "VSDAT"}]',
  json: '[{"testcd": "SYSBP", "value_col": "SYSBP", "unit_col": "SYSBPU"}]',
};

function collectPipeline() {
  const host = $("pipe-body");
  const steps = [];
  host.querySelectorAll(".step-card").forEach((card, i) => {
    const step = { op: card.querySelector('[data-f="op"]').value,
                   name: card.querySelector('[data-f="name"]').value.trim() || `prep${i + 1}`,
                   params: {} };
    card.querySelectorAll("[data-p]").forEach((el) => {
      const k = el.dataset.p;
      let val;
      if (el.dataset.multi) {
        val = [...el.selectedOptions].map((o) => o.value);
      } else if (el.dataset.json) {
        if (!el.value.trim()) return;
        try { val = JSON.parse(el.value); }
        catch { throw new Error(`step ${i + 1}, ${k}: not valid JSON`); }
      } else if (el.dataset.list) {
        val = el.value.split(",").map((x) => x.trim()).filter(Boolean);
      } else {
        val = el.value;
      }
      if (val === "" || (Array.isArray(val) && !val.length)) return;
      step.params[k] = val;
    });
    steps.push(step);
  });
  return steps;
}

function wirePipeline() {
  const host = $("pipe-body");
  host.querySelectorAll(".step-card").forEach((card) => {
    const i = Number(card.dataset.i);
    card.querySelector('[data-f="op"]').onchange = (e) => {
      pipeState.steps = collectPipelineSafe();
      pipeState.steps[i].op = e.target.value;
      pipeState.steps[i].params = {};
      renderPipeline();
    };
    card.querySelectorAll("[data-act]").forEach((b) => {
      b.onclick = () => {
        pipeState.steps = collectPipelineSafe();
        const act = b.dataset.act;
        if (act === "del") pipeState.steps.splice(i, 1);
        if (act === "up" && i > 0)
          [pipeState.steps[i - 1], pipeState.steps[i]] = [pipeState.steps[i], pipeState.steps[i - 1]];
        if (act === "down" && i < pipeState.steps.length - 1)
          [pipeState.steps[i + 1], pipeState.steps[i]] = [pipeState.steps[i], pipeState.steps[i + 1]];
        pipeState.reports = [];
        renderPipeline();
      };
    });
    // a dataset change refreshes downstream pickers
    const dsSel = card.querySelector('[data-p="dataset"]');
    if (dsSel) dsSel.onchange = () => { pipeState.steps = collectPipelineSafe(); };
  });
}

function collectPipelineSafe() {
  try { return collectPipeline(); } catch { return pipeState.steps; }
}

function pipelineSection(d) {
  const n = (d.pipeline || []).length;
  return `<div class="pipe">
    <header>
      <h4>Prepare the data${n ? ` — ${n} step(s)` : ""}</h4>
      <button class="tiny" id="pipe-add">Add step</button>
      ${d.prep ? `<button class="tiny" id="pipe-seed">Start from the detected step</button>` : ""}
      <button class="tiny" id="pipe-preview">Preview</button>
      <button class="tiny primary" id="pipe-apply">Apply &amp; rebuild</button>
      ${n ? `<button class="tiny" id="pipe-clear">Remove all</button>` : ""}
    </header>
    <div id="pipe-body"></div>
    <div id="pipe-out" style="padding:0 .85rem .7rem"></div>
  </div>`;
}

function wirePipelineSection(d) {
  pipeState = { steps: JSON.parse(JSON.stringify(d.pipeline || [])),
                reports: d.prep_reports || [], outputs: {} };
  renderPipeline();

  $("pipe-add").onclick = () => {
    pipeState.steps = collectPipelineSafe();
    pipeState.steps.push({ op: "stack", name: `prep${pipeState.steps.length + 1}`, params: {} });
    renderPipeline();
  };
  const seed = $("pipe-seed");
  if (seed) seed.onclick = async () => {
    const r = await post(`/api/domain/${d.domain}/pipeline/from-auto`);
    pipeState.steps = r.steps;
    renderPipeline();
  };
  $("pipe-preview").onclick = async () => {
    let steps;
    try { steps = collectPipeline(); } catch (e) { return msg($("pipe-out"), "bad", esc(e.message)); }
    pipeState.steps = steps;
    $("pipe-out").innerHTML = `<p class="hint">running…</p>`;
    const r = await post(`/api/domain/${d.domain}/pipeline/preview`, { steps });
    if (!r.ok) { pipeState.reports = []; await renderPipeline();
                 return msg($("pipe-out"), "bad", esc(r.error)); }
    pipeState.reports = r.reports;
    await renderPipeline();
    const names = Object.keys(r.outputs);
    const last = r.outputs[names[names.length - 1]];
    $("pipe-out").innerHTML =
      `<div class="msg ok">${names.length} dataset(s) produced: <code>${names.map(esc).join("</code> <code>")}</code></div>` +
      `<p class="hint"><b>${esc(names[names.length - 1])}</b> — first records</p>` +
      `<div class="tblwrap" style="max-height:14rem"><table><thead><tr>` +
      last.columns.map((c) => `<th>${esc(c)}</th>`).join("") + `</tr></thead><tbody>` +
      last.sample.map((row) => `<tr>${row.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("") +
      `</tbody></table></div>`;
  };
  $("pipe-apply").onclick = async () => {
    let steps;
    try { steps = collectPipeline(); } catch (e) { return msg($("pipe-out"), "bad", esc(e.message)); }
    try {
      await post(`/api/domain/${d.domain}/pipeline`, { steps });
      await post(`/api/domain/${d.domain}/build`, {});
    } catch (e) { return msg($("pipe-out"), "bad", esc(e.message)); }
    afterDomainRebuild(d.domain);
  };
  const clr = $("pipe-clear");
  if (clr) clr.onclick = async () => {
    await post(`/api/domain/${d.domain}/pipeline`, { steps: [] });
    await post(`/api/domain/${d.domain}/build`, {});
    afterDomainRebuild(d.domain);
  };
}

// ── variable mapping editor ─────────────────────────────────────────────────
let RECIPES = null;
let colCache = {};

async function loadRecipes() {
  if (!RECIPES) RECIPES = await api("/api/recipes");
  return RECIPES;
}

async function columnsOf(dataset) {
  if (!dataset) return [];
  if (!colCache[dataset]) {
    try {
      colCache[dataset] = (await api(
        `/api/domain/${domState.domain}/columns/${encodeURIComponent(dataset)}`)).columns;
    } catch { colCache[dataset] = []; }
  }
  return colCache[dataset];
}

async function openEditor(variable) {
  const d = domState.data;
  const v = d.variables.find((x) => x.variable === variable);
  if (!v) return;
  await loadRecipes();
  const host = document.createElement("div");
  host.className = "editor";
  host.id = "editor";
  document.getElementById("editor")?.remove();
  $("dom-vars").before(host);

  const cur = {
    mtype: v.mapping_type, dataset: v.source.split(".")[0] || "", 
    column: v.source.split(".").slice(1).join(".") || "",
    value: v.constant || "", recipe: v.recipe || "", codelist: v.codelist || "",
    args: JSON.parse(JSON.stringify(v.args || {})),
  };

  const render = async () => {
    const cols = await columnsOf(cur.dataset);
    const dsOpts = ['<option value="">—</option>'].concat(
      d.datasets.map((x) => `<option ${cur.dataset === x ? "selected" : ""}>${esc(x)}</option>`)).join("");
    const colOpts = ['<option value="">—</option>'].concat(
      cols.map((x) => `<option ${cur.column === x ? "selected" : ""}>${esc(x)}</option>`)).join("");
    const mtOpts = RECIPES.mtypes.map((m) =>
      `<option value="${m}" ${cur.mtype === m ? "selected" : ""}>${m}</option>`).join("");

    let body = `<div class="grid">
      <div class="opt"><label>Mapping type</label><select id="e-mtype">${mtOpts}</select></div>`;

    if (cur.mtype === "assign") {
      body += `<div class="opt"><label>Raw dataset</label><select id="e-dataset">${dsOpts}</select></div>
        <div class="opt"><label>Column</label><select id="e-column">${colOpts}</select></div>
        <div class="opt"><label>Codelist (optional)</label>
          <input type="text" id="e-codelist" value="${esc(cur.codelist)}"></div>`;
    } else if (cur.mtype === "constant") {
      body += `<div class="opt"><label>Value</label>
        <input type="text" id="e-value" value="${esc(cur.value)}"></div>`;
    } else if (cur.mtype === "sequence") {
      body += `<div class="opt"><label>Number within</label>
        <input type="text" id="e-grp" value="${esc(cur.args.group || "USUBJID")}"></div>`;
    } else if (cur.mtype === "derived") {
      const rOpts = ['<option value="">— choose a derivation —</option>'].concat(
        RECIPES.recipes.map((r) =>
          `<option value="${r.id}" ${cur.recipe === r.id ? "selected" : ""}>${esc(r.label)}</option>`)).join("");
      body += `<div class="opt" style="grid-column:1/-1"><label>Derivation</label>
        <select id="e-recipe">${rOpts}</select></div>`;
      const rec = RECIPES.recipes.find((r) => r.id === cur.recipe);
      for (const f of (rec?.fields || [])) {
        const val = cur.args[f.k];
        const id = `e-arg-${f.k}`;
        let input;
        if (f.t === "dataset") {
          input = `<select id="${id}" data-arg="${f.k}">` + ['<option value="">—</option>'].concat(
            d.datasets.map((x) => `<option ${val === x ? "selected" : ""}>${esc(x)}</option>`)).join("") + `</select>`;
        } else if (f.t === "column") {
          const acols = await columnsOf(cur.args.dataset || cur.dataset || d.base);
          input = `<select id="${id}" data-arg="${f.k}">` + ['<option value="">—</option>'].concat(
            acols.map((x) => `<option ${val === x ? "selected" : ""}>${esc(x)}</option>`)).join("") + `</select>`;
        } else if (f.t === "sdtmvar") {
          input = `<select id="${id}" data-arg="${f.k}">` + ['<option value="">—</option>'].concat(
            d.variables.map((x) => `<option ${val === x.variable ? "selected" : ""}>${esc(x.variable)}</option>`)).join("") + `</select>`;
        } else if (f.t === "domain") {
          input = `<select id="${id}" data-arg="${f.k}">` + ['<option value="">—</option>'].concat(
            (d.built_domains || []).map((x) => `<option ${val === x ? "selected" : ""}>${esc(x)}</option>`)).join("") + `</select>`;
        } else if (f.t === "choice") {
          input = `<select id="${id}" data-arg="${f.k}">` + ['<option value="">—</option>'].concat(
            f.options.map((x) => `<option ${val === x ? "selected" : ""}>${esc(x)}</option>`)).join("") + `</select>`;
        } else if (f.t === "json") {
          input = `<textarea id="${id}" data-arg="${f.k}" data-json="1" rows="3">${esc(
            val === undefined ? "" : JSON.stringify(val))}</textarea>`;
        } else if (f.t === "list") {
          input = `<input type="text" id="${id}" data-arg="${f.k}" data-list="1"
            value="${esc(Array.isArray(val) ? val.join(", ") : (val || ""))}">`;
        } else {
          input = `<input type="text" id="${id}" data-arg="${f.k}" value="${esc(val ?? "")}">`;
        }
        body += `<div class="opt" ${f.t === "json" ? 'style="grid-column:1/-1"' : ""}>
          <label>${esc(f.k)}</label>${input}
          ${f.help ? `<div class="fieldhelp">${esc(f.help)}</div>` : ""}</div>`;
      }
    } else if (cur.mtype === "drop") {
      body += `<div class="opt" style="grid-column:1/-1"><div class="fieldhelp">
        This variable will be excluded from the built dataset.</div></div>`;
    }
    body += `</div>
      <div class="row">
        <button id="e-preview">Preview</button>
        <button class="primary" id="e-apply">Apply &amp; rebuild ${esc(d.domain)}</button>
        ${v.edited ? `<button id="e-reset">Revert to the spec</button>` : ""}
        <button id="e-cancel">Cancel</button>
      </div>
      <div id="e-out"></div>`;

    host.innerHTML =
      `<h4>${esc(v.variable)} — ${esc(v.label || "")}</h4>
       <p class="sub">spec row ${v.spec_row}: <code>${esc(v.spec_action || "—")}</code>
         ${v.spec_input ? `· inputs <code>${esc(v.spec_input)}</code>` : ""}
         ${v.spec_rule ? `<br>rule: ${esc(v.spec_rule)}` : ""}
         ${v.edited ? `<br><b>currently a hand edit.</b> ${esc(v.edit_note)}` : ""}</p>` + body;

    host.scrollIntoView({ block: "nearest" });
    wire();
  };

  const collect = () => {
    cur.mtype = $("e-mtype").value;
    if (cur.mtype === "assign") {
      cur.dataset = $("e-dataset")?.value || "";
      cur.column = $("e-column")?.value || "";
      cur.codelist = $("e-codelist")?.value || "";
    }
    if (cur.mtype === "constant") cur.value = $("e-value")?.value || "";
    if (cur.mtype === "sequence") cur.args = { group: $("e-grp")?.value || "USUBJID" };
    if (cur.mtype === "derived") {
      cur.recipe = $("e-recipe")?.value || "";
      const args = {};
      host.querySelectorAll("[data-arg]").forEach((el) => {
        const k = el.dataset.arg;
        let val = el.value;
        if (el.dataset.json) {
          if (!val.trim()) return;
          try { val = JSON.parse(val); } catch { throw new Error(`${k}: not valid JSON`); }
        } else if (el.dataset.list) {
          val = val.split(",").map((x) => x.trim()).filter(Boolean);
        }
        if (val !== "" && !(Array.isArray(val) && !val.length)) args[k] = val;
      });
      cur.args = args;
    }
    return {
      mtype: cur.mtype, dataset: cur.dataset, column: cur.column, value: cur.value,
      recipe: cur.mtype === "derived" ? cur.recipe : "", codelist: cur.codelist,
      args: cur.args || {},
    };
  };

  const wire = () => {
    $("e-mtype").onchange = () => { cur.mtype = $("e-mtype").value; render(); };
    if ($("e-dataset")) $("e-dataset").onchange = () => {
      cur.dataset = $("e-dataset").value; cur.column = ""; render();
    };
    if ($("e-recipe")) $("e-recipe").onchange = () => {
      cur.recipe = $("e-recipe").value; cur.args = {}; render();
    };
    const argDs = host.querySelector('[data-arg="dataset"]');
    if (argDs) argDs.onchange = () => { cur.args.dataset = argDs.value; render(); };

    $("e-cancel").onclick = () => host.remove();
    $("e-preview").onclick = async () => {
      let payload;
      try { payload = collect(); } catch (e) { return msg($("e-out"), "bad", esc(e.message)); }
      $("e-out").innerHTML = `<div class="previewbox">running…</div>`;
      try {
        const r = await post(`/api/domain/${d.domain}/variable/${v.variable}/preview`, payload);
        if (!r.ok) {
          $("e-out").innerHTML = `<div class="msg bad">${esc(r.error || r.reason || "not built")}</div>`;
          return;
        }
        $("e-out").innerHTML = `<div class="previewbox"><b>${esc(r.how)}</b> — ` +
          `${r.populated.toLocaleString()} of ${r.rows.toLocaleString()} records populated<br>` +
          r.samples.map((x) => `<code>${esc(x)}</code>`).join("") +
          (r.samples.length ? "" : "<i>no values</i>") + `</div>`;
      } catch (e) { msg($("e-out"), "bad", esc(e.message)); }
    };
    $("e-apply").onclick = async () => {
      let payload;
      try { payload = collect(); } catch (e) { return msg($("e-out"), "bad", esc(e.message)); }
      $("e-apply").disabled = true;
      try {
        await post(`/api/domain/${d.domain}/variable/${v.variable}`, payload);
        await post(`/api/domain/${d.domain}/build`, {});
      } catch (e) { $("e-apply").disabled = false; return msg($("e-out"), "bad", esc(e.message)); }
      afterDomainRebuild(d.domain);
    };
    if ($("e-reset")) $("e-reset").onclick = async () => {
      await api(`/api/domain/${d.domain}/variable/${v.variable}`, { method: "DELETE" });
      await post(`/api/domain/${d.domain}/build`, {});
      afterDomainRebuild(d.domain);
    };
  };

  await render();
}

function afterDomainRebuild(domain) {
  pollJob("ov", async (err) => {
    if (err) { alert(`rebuild failed: ${err.error}`); return; }
    await openDomain(domain);
    await renderBuild();
    const co = $("cmp-out");
    if (co.innerHTML) msg(co, "warn",
      `${esc(domain)} was rebuilt, so the earlier comparison is out of date. Run it again.`);
  });
}

// ── step 4 · compare ────────────────────────────────────────────────────────
$("btn-cmp").onclick = async () => {
  const out = $("cmp-out");
  const path = $("vendor-path").value.trim();
  if (!path) return msg(out, "warn", "Choose the folder holding the vendor's SDTM datasets.");
  out.innerHTML = "";
  try {
    await post("/api/compare", {
      path,
      ignore_case: $("ign-case").checked,
      numeric_tolerance: parseFloat($("numtol").value) || 1e-9,
      ignore_vars: $("ignvars").value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
    });
  } catch (e) { return msg(out, "bad", esc(e.message)); }
  $("btn-cmp").disabled = true;
  pollJob("cmp", async (err) => {
    $("btn-cmp").disabled = false;
    if (err) return msg(out, "bad", `${esc(err.error)}<details><summary>details</summary><pre style="font-size:11px;overflow:auto">${esc(err.detail)}</pre></details>`);
    await renderCompare();
    markDone("s-cmp");
  });
};

async function renderCompare() {
  const out = $("cmp-out");
  const d = await api("/api/compare/results");
  const doms = d.domains;
  const identical = doms.filter((x) => x.status === "identical").length;
  const differing = doms.filter((x) => x.status === "differences"
    && (x.value_differences || x.only_built || x.only_vendor)).length;
  const structural = doms.filter((x) => x.status === "differences"
    && !x.value_differences && !x.only_built && !x.only_vendor).length;
  const errored = doms.filter((x) => x.status === "error").length;
  const totalDiff = doms.reduce((a, x) => a + x.value_differences, 0);
  const recDiff = doms.reduce((a, x) => a + x.only_built + x.only_vendor, 0);

  // a domain whose records and values all agree, but whose variable LIST differs, is a
  // structural finding — worth flagging, but not the same as wrong data
  const structuralOnly = (x) => x.status === "differences" && !x.value_differences
    && !x.only_built && !x.only_vendor;
  const pill = (x) => x.status === "identical" ? `<span class="pill ok">identical</span>`
    : x.status === "error" ? `<span class="pill warn">${esc(x.error)}</span>`
    : structuralOnly(x) ? `<span class="pill warn">variables differ</span>`
    : `<span class="pill bad">differences</span>`;

  let html = `<div class="stats">${stat(identical, "domains identical", "ok")}` +
    `${stat(differing, "with differences", differing ? "bad" : "")}` +
    `${structural ? stat(structural, "variables differ only", "warn") : ""}` +
    `${stat(errored, "not comparable", errored ? "warn" : "")}` +
    `${stat(recDiff.toLocaleString(), "record mismatches", recDiff ? "bad" : "ok")}` +
    `${stat(totalDiff.toLocaleString(), "value differences", totalDiff ? "bad" : "ok")}</div>`;

  html += table([
    { key: "domain", label: "Domain" },
    { label: "Status", html: pill },
    { label: "Built", n: true, html: (r) => r.rows_built.toLocaleString() },
    { label: "Vendor", n: true, html: (r) => r.rows_vendor.toLocaleString() },
    { label: "Matched", n: true, html: (r) => r.status === "error" ? "—" : r.matched.toLocaleString() },
    { label: "Only built", n: true, html: (r) => r.only_built ? `<span class="pill bad">${r.only_built}</span>` : (r.status === "error" ? "—" : "0") },
    { label: "Only vendor", n: true, html: (r) => r.only_vendor ? `<span class="pill bad">${r.only_vendor}</span>` : (r.status === "error" ? "—" : "0") },
    { label: "Value diffs", n: true, html: (r) => r.value_differences ? `<span class="pill bad">${r.value_differences}</span>` : (r.status === "error" ? "—" : "0") },
  ], doms);

  for (const x of doms) {
    if (x.status !== "differences") continue;
    const notes = [x.key_note, ...x.notes].filter(Boolean)
      .map((n) => `<p class="hint">${esc(n)}</p>`).join("");
    const extra = [];
    if (x.vars_only_vendor.length) extra.push(`<div class="msg warn">Variables only in the vendor dataset: <code>${esc(x.vars_only_vendor.join(", "))}</code></div>`);
    if (x.vars_only_built.length) extra.push(`<div class="msg warn">Variables only in the built dataset: <code>${esc(x.vars_only_built.join(", "))}</code></div>`);
    if (x.not_built.length) extra.push(`<div class="msg">Not built here, so not compared: <code>${esc(x.not_built.join(", "))}</code></div>`);
    const varTbl = x.variables.length ? table([
      { key: "variable", label: "Variable" },
      { label: "Differing", n: true, html: (r) => `<span class="pill bad">${r.differing}</span>` },
      { label: "Compared", n: true, html: (r) => r.compared.toLocaleString() },
      { label: "Agreement", n: true, html: (r) => `${r.agreement}%` },
      { label: "Only built", n: true, html: (r) => r.only_built_nonblank || "" },
      { label: "Only vendor", n: true, html: (r) => r.only_vendor_nonblank || "" },
    ], x.variables) : "";
    const examples = x.variables.flatMap((v) => v.examples.slice(0, 3).map((ex) => ({
      variable: v.variable,
      record: Object.entries(ex).filter(([k]) => k !== "built" && k !== "vendor")
        .map(([k, val]) => `${k}=${val}`).join(" · "),
      built: ex.built, vendor: ex.vendor,
    })));
    const exTbl = examples.length ? table([
      { key: "variable", label: "Variable" }, { key: "record", label: "Record" },
      { label: "Built", html: (r) => `<code>${esc(r.built)}</code>` },
      { label: "Vendor", html: (r) => `<code>${esc(r.vendor)}</code>` },
    ], examples) : "";

    html += `<details open><summary>${esc(x.domain)} — matched on <code>${esc(x.keys.join(", "))}</code></summary>`
      + notes + extra.join("")
      + (x.only_built ? `<div class="msg bad">${x.only_built} record(s) built here are not in the delivery.</div>` : "")
      + (x.only_vendor ? `<div class="msg bad">${x.only_vendor} vendor record(s) were not produced by this build.</div>` : "")
      + varTbl + exTbl + `</details>`;
  }

  html += `<div class="actions">
      <button class="link" onclick="window.open('/api/download?name=comparison')">download the comparison workbook (.xlsx)</button>
      <button class="link" onclick="window.open('/api/report')">open the full report</button>
      <button class="link" id="btn-open-out2">open the output folder</button>
    </div>`;
  out.innerHTML = html;
  $("btn-open-out2").onclick = () => api("/api/reveal?name=out_dir").catch(() => {});
}

// ── boot ────────────────────────────────────────────────────────────────────
$("btn-reset").onclick = async () => {
  await post("/api/reset").catch(() => {});
  location.reload();
};

(async () => {
  try {
    const s = await api("/api/state");
    $("ver").textContent = `v${s.version} · local`;
    if (s.spec) { $("spec-path").value = s.spec; }
    if (s.raw) { $("raw-path").value = s.raw; }
    if (s.vendor) { $("vendor-path").value = s.vendor; }
    if (s.domains.length) { markDone("s-spec"); unlock("s-raw"); window.__domains = s.domains; }
    if (s.raw) { markDone("s-raw"); unlock("s-build"); }
    if (s.built.length) { markDone("s-build"); unlock("s-cmp"); await renderBuild(); }
    if (s.compared.length) { markDone("s-cmp"); await renderCompare(); }
  } catch { /* fresh session */ }
})();
