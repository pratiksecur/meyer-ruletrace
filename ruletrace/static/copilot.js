// RuleMate Copilot panel for the RuleTrace viewer.
//  * Ask: questions answered from the FULL regulation texts; every citation, number and quote is checked in code,
//    or the answer is shown as clause excerpts / "not found". Opened straight from a finding ("Ask the regulations").
//  * Read with Gemini: a picture (screenshot, sketch, photo) or a drawing page (also scanned). Gemini reads it, code
//    confirms each reading on the image; picture values are checked against the rules, drawings become a RuleTrace
//    design that the rule engine checks and shows in 3D.
// Talks to RuleTrace only through window.RuleTrace (headers, open, boot, toast, result).
const RT = () => window.RuleTrace;
const $c = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const EXAMPLES = ["What clear width must an accessible door have?",
                  "How high must guard rails be on a research vessel?",
                  "When does a research vessel need a hospital space?",
                  "What turning space does a wheelchair need?"];

async function call(path, opts = {}) {
  const body = opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body;
  const r = await fetch(path, {...opts, body, headers: {...(body && !(opts.body instanceof FormData)
    ? {"Content-Type": "application/json"} : {}), ...RT().headers()}});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${r.status} ${r.statusText}`);
  return data;
}
const toast = (m, err) => RT()?.toast ? RT().toast(m, err) : console.log(m);

// ---------- panel ----------
document.body.insertAdjacentHTML("beforeend", `
<aside class="cp" id="cp" aria-label="RuleMate Copilot">
  <div class="top"><div><b>RuleMate Copilot</b><div class="tagline">Gemini reads, code checks: every answer traceable to a clause</div></div>
    <span class="sp"></span><button id="cpClose" aria-label="Close">×</button></div>
  <div class="tabs"><button data-tab="ask" class="on">Ask</button><button data-tab="check">Check values</button>
    <button data-tab="browse">Browse</button><button data-tab="read">Gemini</button></div>
  <div class="body">
    <div class="state" id="cpState">Checking the Copilot…</div>
    <section id="tab-ask">
      <textarea id="cpQ" rows="3" placeholder="Ask about the regulations, e.g. what clear width must an accessible door have?"></textarea>
      <div class="ex" id="cpEx"></div>
      <div class="docs" id="cpDocs"></div>
      <button class="primary" id="cpAsk" style="width:100%">Ask</button>
      <div id="cpAns"></div>
    </section>
    <section id="tab-check" hidden>
      <div class="small muted">Design values against limits taken from the full regulation texts. Units are converted
        in code (mm, in, ft, 2'-6"); PASS / FAIL / NEEDS REVIEW is computed, never written by a model.</div>
      <div class="docs" id="cpCheckDocs"></div>
      <div class="row"><input type="file" id="cpCsv" accept=".csv,.png,.jpg,.jpeg,.webp,.pdf" style="flex:1">
        <button id="cpExample" title="Accessible stateroom, synthetic values with planted defects">Example</button></div>
      <div class="row" id="cpCPageRow" hidden><label class="small">PDF page</label><input type="number" id="cpCPage" min="1" value="1"></div>
      <div class="small muted">A <b>CSV</b> (item_id, space, parameter, value, unit, source), a <b>picture</b> or a
        <b>drawing page</b> (PDF): Gemini reads the written dimensions, code confirms each one on the page and measures
        stair angles from the PDF's lines; then every value is checked.</div>
      <div class="ship" style="margin-top:10px"><input id="cpVSpace" placeholder="Space (e.g. Accessible cabin bathroom)">
        <input id="cpVParam" placeholder="Parameter (e.g. door clear width)">
        <input id="cpVValue" placeholder="Value (30, 2'-6&quot;, 1500)"><input id="cpVUnit" placeholder="Unit (in, mm)"></div>
      <div class="row"><button id="cpVAdd" style="flex:1">Add value</button>
        <button class="primary" id="cpRunCheck" style="flex:2">Run check</button></div>
      <div id="cpVList" class="small"></div>
      <div id="cpCheckOut"></div>
    </section>
    <section id="tab-browse" hidden>
      <div class="row"><select id="cpBDoc" style="flex:1"></select></div>
      <div class="row"><input id="cpBQ" placeholder="Search the full texts, or type a clause number" style="flex:1">
        <button id="cpBGo">Search</button></div>
      <div id="cpBClause"></div>
      <div id="cpBList" class="small"></div>
    </section>
    <section id="tab-read" hidden>
      <div class="drop">Drop in a <b>picture</b> (screenshot from the design tool, sketch, photo with dimensions) or a
        <b>drawing</b> (PDF page, scanned drawings too). Gemini reads it; code confirms every reading on the image.</div>
      <input type="file" id="cpFile" accept=".png,.jpg,.jpeg,.webp,.pdf" style="width:100%;margin-top:8px">
      <div class="row" id="cpPageRow" hidden><label class="small">Page</label><input type="number" id="cpPage" min="1" value="1"></div>
      <button class="primary" id="cpRead" style="width:100%;margin-top:8px">Read with Gemini</button>
      <div id="cpOut"></div>
    </section>
  </div>
</aside>`);
const panel = $c("#cp");
const openPanel = (tab) => { panel.classList.add("open"); if (tab) showTab(tab); refresh(); };
$c("#cpClose").onclick = () => panel.classList.remove("open");
function showTab(t) {
  document.querySelectorAll(".cp .tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  for (const k of ["ask", "check", "browse", "read"]) $c(`#tab-${k}`).hidden = t !== k;
  if (t === "browse" && !$c("#cpBList").dataset.done) outline();
}
document.querySelectorAll(".cp .tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
$c("#cpEx").innerHTML = EXAMPLES.map(q => `<button data-q="${esc(q)}">${esc(q)}</button>`).join("");
$c("#cpEx").onclick = e => { const q = e.target.dataset?.q; if (q) { $c("#cpQ").value = q; ask(); } };

// header button
const head = document.querySelector("header");
head?.insertAdjacentHTML("beforeend", `<button class="cpbtn" id="cpOpen" title="Ask the regulations, read drawings and pictures with Gemini">✦ Copilot</button>`);
$c("#cpOpen").onclick = () => panel.classList.contains("open") ? panel.classList.remove("open") : openPanel();

// ---------- status / index ----------
let status = null;
async function refresh() {
  try { status = await call("/ruletrace/copilot/status"); } catch (e) { $c("#cpState").textContent = e.message; return; }
  if (!status.installed) { $c("#cpState").innerHTML = esc(status.message); return; }
  if (!status.index) {
    $c("#cpState").innerHTML = `The regulation texts are not indexed yet. <button class="aib" id="cpBuild">Build the index (about 30 s)</button>`;
    $c("#cpBuild").onclick = build; return;
  }
  const docs = status.documents.map(d => `${esc(d.doc)} (${d.clauses} clauses)`).join(" · ");
  $c("#cpState").innerHTML = `<b>Full texts:</b> ${docs}<br>Answers: ${esc(status.provider)} · Gemini ${status.gemini ? "ready" : "not set (GEMINI_API_KEY)"}`;
  if (!$c("#cpDocs").dataset.done) {
    const boxes = status.documents.map(d => `<label><input type="checkbox" value="${esc(d.doc)}" checked> ${esc(d.doc)}</label>`).join("");
    $c("#cpDocs").innerHTML = boxes; $c("#cpCheckDocs").innerHTML = boxes;
    $c("#cpBDoc").innerHTML = `<option value="">All documents</option>` + status.documents.map(d =>
      `<option value="${esc(d.doc)}">${esc(d.doc)}: ${esc(d.title)}</option>`).join("");
    $c("#cpDocs").dataset.done = "1";
  }
}
async function build() {
  $c("#cpState").textContent = "Building the clause index of the full regulation texts…";
  try { const r = await call("/ruletrace/copilot/index", {method: "POST"});
        toast(`Indexed ${r.clauses} clauses (${r.documents.join(", ")})`); } catch (e) { toast(e.message, true); }
  refresh();
}

// ---------- ask ----------
const CITE = /\[([^\[\]§]+?)\s*§\s*((?:[^\[\]]|\[[^\[\]]*\])+?)\]/g;
function withCites(text) {
  // light Markdown from the answer text: **bold** and "> quoted clause" lines
  const md = esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/^&gt; ?(.*)$/gm, "<blockquote>$1</blockquote>").replace(/<\/blockquote>\n/g, "</blockquote>");
  return md.replace(CITE, (m, doc, cid) => `<span class="cite" data-uid="${esc(doc.trim())}::${esc(cid.trim())}">${esc(doc.trim())} § ${esc(cid.trim())}</span>`);
}
async function ask() {
  const q = $c("#cpQ").value.trim(); if (q.length < 3) return toast("Type a question first", true);
  const docs = [...document.querySelectorAll("#cpDocs input:checked")].map(i => i.value);
  $c("#cpAsk").disabled = true; $c("#cpAns").innerHTML = `<div class="ans muted">Searching the regulations…</div>`;
  try {
    const a = await call("/ruletrace/copilot/ask", {method: "POST", body: {question: q, docs: docs.length ? docs : null}});
    const label = {verified: "verified", excerpts: "excerpts only", not_found: "not found in the rules"}[a.status];
    $c("#cpAns").innerHTML = `<div class="ans">
      <div class="meta"><span class="badge2 ${a.status}">${label}</span><span>${esc(a.model || "no model needed")}</span>
        ${a.rejected_attempts ? `<span>${a.rejected_attempts} draft(s) rejected by the checks</span>` : ""}</div>
      <div class="txt">${withCites(a.text)}</div><div id="cpClause"></div>
      <div class="how">${a.status === "verified" ? "Every sentence cites a clause that was retrieved and supports it; every number, unit and quote is in that clause."
        : a.status === "excerpts" ? "The generated answer failed the checks, so the clause text is shown instead."
        : "Nothing in the loaded regulations answers this; the closest clauses are listed."} Click a citation to read the clause.</div></div>`;
  } catch (e) { $c("#cpAns").innerHTML = `<div class="ans" style="color:var(--fail)">${esc(e.message)}</div>`; }
  finally { $c("#cpAsk").disabled = false; }
}
$c("#cpAsk").onclick = ask;
$c("#cpQ").addEventListener("keydown", e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask(); });
document.addEventListener("click", async e => {
  const c = e.target.closest?.(".cite"); if (!c) return;
  try { const k = await call(`/ruletrace/copilot/clause/${encodeURIComponent(c.dataset.uid)}`);
    const box = c.closest(".item")?.querySelector(".cl") || c.closest(".ans")?.querySelector("#cpClause") || $c("#cpClause");
    box.innerHTML = `<div class="clause"><div class="h">${esc(k.doc)} § ${esc(k.clause_id)} ${esc(k.title || "")}</div>${esc(k.text)}</div>`;
  } catch (err) { toast(err.message, true); }
});

// "Ask the regulations" from a RuleTrace finding
document.addEventListener("click", e => {
  const b = e.target.closest?.("[data-ask-rule]"); if (!b) return;
  e.stopPropagation(); e.preventDefault();
  const f = (RT()?.result?.findings || []).find(x => x.rule_id === b.dataset.askRule);
  if (!f) return;
  $c("#cpQ").value = `What does ${f.regime} ${f.ref} require? (${f.title})`;
  openPanel("ask"); ask();
}, true);

// ---------- read with Gemini ----------
$c("#cpFile").onchange = () => { const f = $c("#cpFile").files[0]; $c("#cpPageRow").hidden = !(f && /\.pdf$/i.test(f.name)); };
let lastDrawing = null;
$c("#cpRead").onclick = async () => {
  const f = $c("#cpFile").files[0]; if (!f) return toast("Choose a picture or a PDF first", true);
  const fd = new FormData(); fd.append("file", f); fd.append("page", $c("#cpPage").value || "1");
  $c("#cpRead").disabled = true; $c("#cpOut").innerHTML = `<div class="ans muted">Gemini is reading ${esc(f.name)}; code is confirming each reading… (up to a minute)</div>`;
  try { const r = await call("/ruletrace/copilot/read", {method: "POST", body: fd});
        r.kind === "picture" ? showPicture(r) : showDrawing(r); }
  catch (e) { $c("#cpOut").innerHTML = `<div class="ans" style="color:var(--fail)">${esc(e.message)}</div>`; }
  finally { $c("#cpRead").disabled = false; }
};
const pill = v => v ? `<span class="pill ${v === "PASS" ? "pass" : v === "FAIL" ? "fail" : v === "NO RULE" || v === "CHECK INPUT" ? "unknown" : "warn"}"
  title="${v === "NO RULE" ? "None of the loaded rule texts limits this value" : v === "NEEDS REVIEW" ? "A rule applies, but a condition must be confirmed" : v === "CHECK INPUT" ? "The value is not a number or has no unit" : ""}">${esc(v)}</span>`
  : `<span class="pill unknown">not checked</span>`;
function showPicture(r) {
  const counts = ["PASS", "FAIL", "NEEDS REVIEW"].map(v => r.items.filter(i => i.verdict === v).length);
  $c("#cpOut").innerHTML = `<div class="sum"><div><b>${r.n_confirmed}/${r.n_readings}</b>values confirmed</div>
      <div><b style="color:var(--fail)">${counts[1]}</b>fail</div><div><b style="color:var(--pass)">${counts[0]}</b>pass</div></div>
    <img class="prev" src="${r.preview}" alt="">
    ${r.items.map((it, i) => `<div class="item" data-i="${i}"><div class="h"><span class="t">${esc(it.parameter)}: ${esc(it.written)}</span>${pill(it.verdict)}</div>
      <div class="small muted">${esc(it.space)} · ${it.confirmed ? `<span class="ok">confirmed on the picture</span>` : "Gemini only: not found on the picture, not used"}
        ${it.clauses.length ? " · " + it.clauses.map(esc).join(", ") : ""}</div>
      <div class="more" hidden><img src="${it.crop}" alt=""><pre>${esc(it.reason)}</pre></div></div>`).join("")}
    <div class="how">Gemini read the values; code confirmed each one is written at that spot (local OCR) and compared it with the rule. Gemini never decides PASS or FAIL.</div>`;
  document.querySelectorAll("#cpOut .item").forEach(el => el.onclick = () => { const m = el.querySelector(".more"); m.hidden = !m.hidden; });
}
function showDrawing(r) {
  lastDrawing = r;
  const rooms = r.items.filter(i => ["stateroom", "hospital", "stairway"].includes(i.kind));
  $c("#cpOut").innerHTML = `<div class="sum"><div><b>${r.n_confirmed}/${r.n_readings}</b>readings confirmed</div>
      <div><b>${r.summary.cabins}</b>cabins (${r.summary.berths_confirmed} berths stated)</div><div><b>${r.summary.hospital ? "yes" : "no"}</b>hospital</div></div>
    <img class="prev" src="${r.preview}" alt="">
    <table><tr><th>Label</th><th>No.</th><th>Kind</th><th>Berths</th><th></th></tr>
      ${rooms.map(i => `<tr><td>${esc(i.label)}</td><td>${esc(i.room_number || "")}</td><td>${esc(i.kind)}</td>
        <td>${i.berths ?? ""}${i.berths && !i.berths_confirmed ? " ?" : ""}</td>
        <td class="${i.confirmed ? "ok" : "no"}">${i.confirmed ? "✓" : "Gemini only"}</td></tr>`).join("")}</table>
    <h2 style="margin-top:14px">Check it in RuleTrace</h2>
    <div class="small muted">The confirmed cabins and hospital become a RuleTrace design: the rule engine checks them (MLC, consistency) and shows them in 3D. Berths count only where the drawing states them; "?" = Gemini's count, not used.</div>
    <div class="ship"><input id="cpShipName" placeholder="Ship name"><select id="cpShipType">
      ${["research", "passenger", "cargo", "special_purpose", "ro_ro_passenger"].map(t => `<option>${t}</option>`).join("")}</select>
      <input id="cpLen" type="number" placeholder="Length m (for positions)"><input id="cpPers" type="number" placeholder="Persons on board">
      <label class="small">Keel laid <input id="cpKeel" type="date"></label>
      <label class="small"><input id="cpFull" type="checkbox"> this sheet shows the complete accommodation</label></div>
    <button class="primary" id="cpTo3d" style="width:100%;margin-top:8px">Check in RuleTrace 3D</button>
    <div class="how">Page ${r.page} of ${r.pages}. Gemini only proposes; a room is used only when its label or number is found on the drawing at the position Gemini gives.</div>`;
  $c("#cpTo3d").onclick = async () => {
    const ship = {name: $c("#cpShipName").value || undefined, ship_type: $c("#cpShipType").value};
    if ($c("#cpLen").value) ship.length_m = +$c("#cpLen").value;
    if ($c("#cpPers").value) ship.persons_total = +$c("#cpPers").value;
    if ($c("#cpKeel").value) ship.keel_laid = $c("#cpKeel").value;  // date-based rules (MLC) need it
    if ($c("#cpFull").checked) ship.complete_arrangement = true;     // "is it on board?" and total-berth checks
    try { const d = await call("/ruletrace/copilot/design", {method: "POST", body: {readings_id: r.readings_id, ship}});
      toast(`${d.elements} rooms sent to RuleTrace`); await RT().boot(); await RT().open(d.id); panel.classList.remove("open");
    } catch (e) { toast(e.message, true); }
  };
}

// ---------- check values ----------
const vrows = [];
function drawRows() {
  $c("#cpVList").innerHTML = vrows.length ? `<table><tr><th>Space</th><th>Parameter</th><th>Value</th><th></th></tr>${vrows.map((r, i) =>
    `<tr><td>${esc(r.space)}</td><td>${esc(r.parameter)}</td><td>${esc(r.value)} ${esc(r.unit)}</td>
     <td><button data-del="${i}" style="padding:0 6px">×</button></td></tr>`).join("")}</table>` : "";
  document.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { vrows.splice(+b.dataset.del, 1); drawRows(); });
}
$c("#cpVAdd").onclick = () => {
  const r = {space: $c("#cpVSpace").value.trim(), parameter: $c("#cpVParam").value.trim(),
             value: $c("#cpVValue").value.trim(), unit: $c("#cpVUnit").value.trim()};
  if (!r.parameter || !r.value) return toast("Give at least a parameter and a value", true);
  vrows.push(r); ["#cpVParam", "#cpVValue"].forEach(k => $c(k).value = ""); drawRows();
};
const checkDocs = () => [...document.querySelectorAll("#cpCheckDocs input:checked")].map(i => i.value);
let lastCheck = null;
async function runCheck(csvFile) {
  $c("#cpRunCheck").disabled = true; $c("#cpCheckOut").innerHTML = `<div class="ans muted">Checking…</div>`;
  try {
    let r;
    if (csvFile) { const fd = new FormData(); fd.append("file", csvFile); fd.append("docs", checkDocs().join(","));
      fd.append("page", $c("#cpCPage").value || "1");
      $c("#cpCheckOut").innerHTML = `<div class="ans muted">${/\.csv$/i.test(csvFile.name) ? "Checking…"
        : "Reading the values (Gemini, then confirmed by OCR) and checking… up to a minute"}</div>`;
      r = await call("/ruletrace/copilot/check/file", {method: "POST", body: fd}); }
    else { if (!vrows.length) throw new Error("Add a value or choose a CSV first");
      r = await call("/ruletrace/copilot/check", {method: "POST", body: {rows: vrows, docs: checkDocs()}}); }
    lastCheck = r; showCheck(r);
  } catch (e) { $c("#cpCheckOut").innerHTML = `<div class="ans" style="color:var(--fail)">${esc(e.message)}</div>`; }
  finally { $c("#cpRunCheck").disabled = false; }
}
$c("#cpRunCheck").onclick = () => runCheck($c("#cpCsv").files[0]);
$c("#cpCsv").onchange = () => { const f = $c("#cpCsv").files[0]; $c("#cpCPageRow").hidden = !(f && /\.pdf$/i.test(f.name)); };
$c("#cpExample").onclick = async () => {
  const r = await fetch("/ruletrace/copilot/example.csv", {headers: RT().headers()});
  if (!r.ok) return toast("Build the index first", true);
  runCheck(new File([await r.text()], "accessible_stateroom_example.csv", {type: "text/csv"}));
};
function showCheck(r) {
  $c("#cpCheckOut").innerHTML = `<div class="sum" style="grid-template-columns:repeat(4,1fr)"><div><b style="color:var(--fail)">${r.counts.FAIL}</b>fail</div>
      <div><b style="color:var(--warn)">${r.counts["NEEDS REVIEW"]}</b>review</div><div><b style="color:var(--pass)">${r.counts.PASS}</b>pass</div>
      <div><b style="color:var(--unknown)">${(r.counts["NO RULE"] || 0) + (r.counts["CHECK INPUT"] || 0)}</b>no rule / input</div></div>
    ${r.counts["NO RULE"] ? `<div class="small muted" style="margin-top:6px"><b>NO RULE</b>: none of the loaded rule texts (${esc(Array.isArray(r.docs) ? r.docs.join(", ") : r.docs)}) limits these values; add the rule (e.g. class rules, SOLAS) to check them. Not the same as NEEDS REVIEW, where a rule applies but a condition must be confirmed.</div>` : ""}
    ${r.counts["CHECK INPUT"] ? `<div class="small muted"><b>CHECK INPUT</b>: the value is not a single number or has no unit (e.g. a crane rating "4T/14M", a dimension without mm/in); correct it and check again.</div>` : ""}
    ${r.results.map((x, i) => `<div class="item" data-ci="${i}"><div class="h"><span class="t">${esc(x.parameter)}: ${esc(x.value)}</span>${pill(x.verdict)}</div>
      <div class="small muted">${esc(x.item_id)} · ${esc(x.space)}${x.deciding ? ` · ${esc(x.deciding.limit)} <span class="cite" data-uid="${esc(x.deciding.uid)}">${esc(x.deciding.clause)}</span>` : " · no matching limit"}</div>
      <div class="more" hidden>${x.crop ? `<img src="${x.crop}" alt="">` : ""}<pre>${esc(x.reason)}</pre>
        ${x.source ? `<div class="small muted">From: ${esc(x.source)}</div>` : ""}</div><div class="cl"></div></div>`).join("")}
    ${(r.unconfirmed || []).length ? `<div class="small muted" style="margin-top:8px">Read by Gemini but not found on the page, so not checked: ${r.unconfirmed.map(u => esc(`${u.parameter} ${u.written}`)).join("; ")}</div>` : ""}
    ${(r.notes || []).length ? `<div class="small muted">${r.notes.map(esc).join(" · ")}</div>` : ""}
    <button class="aib" id="cpDl">Download results (CSV)</button>
    <div class="how">Limits come from the clause text; values are converted with pint and compared in code. NEEDS REVIEW when a limit has a condition code cannot confirm.</div>`;
  document.querySelectorAll("#cpCheckOut .item").forEach(el => el.onclick = e => {
    if (e.target.closest(".cite")) return; const m = el.querySelector(".more"); m.hidden = !m.hidden; });
  $c("#cpDl").onclick = () => {
    const q = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const csv = ["item_id,space,parameter,value,verdict,deciding_limit,clause", ...r.results.map(x =>
      [x.item_id, x.space, x.parameter, x.value, x.verdict, x.deciding?.limit, x.deciding?.clause].map(q).join(","))].join("\n");
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], {type: "text/csv"}));
    a.download = "copilot_check.csv"; a.click();
  };
}

// ---------- browse ----------
async function outline() {
  const doc = $c("#cpBDoc").value;
  try { const r = await call(`/ruletrace/copilot/browse?doc=${encodeURIComponent(doc)}`);
    $c("#cpBList").dataset.done = "1";
    $c("#cpBList").innerHTML = `<div class="muted" style="margin:8px 0">${r.items.length} clauses${doc ? "" : " in all documents"}. Click one to read it.</div>` +
      r.items.map(x => `<div class="bl" data-uid="${esc(x.uid)}" style="padding:3px 0 3px ${Math.min(x.depth, 5) * 12}px;cursor:pointer;${x.kind === "heading" ? "font-weight:600" : ""}">
        <span class="muted">${doc ? "" : esc(x.doc) + " "}</span>${esc(x.clause_id)} ${esc(x.title || x.snippet)}</div>`).join("");
  } catch (e) { $c("#cpBList").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}
async function search() {
  const q = $c("#cpBQ").value.trim(); if (!q) return outline();
  const doc = $c("#cpBDoc").value;
  try { const r = await call(`/ruletrace/copilot/browse?doc=${encodeURIComponent(doc)}&q=${encodeURIComponent(q)}`);
    $c("#cpBList").innerHTML = `<div class="muted" style="margin:8px 0">${r.items.length} matches (retrieval confidence: ${esc(r.confidence)})</div>` +
      r.items.map(x => `<div class="item bl" data-uid="${esc(x.uid)}"><div class="h"><span class="t">${esc(x.doc)} § ${esc(x.clause_id)} ${esc(x.title)}</span>
        <span class="small muted">${esc(x.how)} ${x.score}</span></div><div class="small muted">${esc(x.snippet)}</div></div>`).join("");
  } catch (e) { toast(e.message, true); }
}
async function showClause(uid) {
  try { const k = await call(`/ruletrace/copilot/clause/${encodeURIComponent(uid)}`);
    const link = x => `<span class="cite bln" data-uid="${esc(x.uid)}">${esc(x.clause_id)}</span>`;
    $c("#cpBClause").innerHTML = `<div class="clause"><div class="small muted">${esc(k.path.join(" › "))}</div>
      <div class="h">${esc(k.doc)} § ${esc(k.clause_id)} ${esc(k.title || "")}${k.page ? ` <span class="muted">p.${k.page}</span>` : ""}</div>
      <div style="white-space:pre-wrap">${esc(k.text)}</div>
      ${k.parent ? `<div class="small" style="margin-top:6px">Part of: ${link(k.parent)}</div>` : ""}
      ${k.children.length ? `<div class="small">Sub-clauses: ${k.children.map(link).join(" ")}</div>` : ""}
      ${k.refs.length ? `<div class="small">Refers to: ${k.refs.map(link).join(" ")}</div>` : ""}
      ${k.referenced_by.length ? `<div class="small">Referred to by: ${k.referenced_by.map(link).join(" ")}</div>` : ""}
      <button class="aib" id="cpAskThis">Ask about this clause</button></div>`;
    $c("#cpAskThis").onclick = () => { $c("#cpQ").value = `What does ${k.doc} ${k.clause_id} require?`; showTab("ask"); ask(); };
    $c("#cpBClause").scrollIntoView({block: "nearest"});
  } catch (e) { toast(e.message, true); }
}
$c("#cpBGo").onclick = search;
$c("#cpBQ").addEventListener("keydown", e => { if (e.key === "Enter") search(); });
$c("#cpBDoc").onchange = () => ($c("#cpBQ").value.trim() ? search() : outline());
document.addEventListener("click", e => {
  const b = e.target.closest?.("#tab-browse .bl, #tab-browse .bln"); if (!b) return;
  e.stopPropagation(); showClause(b.dataset.uid);
}, true);
