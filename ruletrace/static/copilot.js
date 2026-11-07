// RuleMate Copilot panel for the RuleTrace viewer.
//  * Ask: questions answered from the FULL regulation texts; every citation, number and quote is checked in code,
//    or the answer is shown as clause excerpts / "not found". Opened straight from a finding ("Ask the regulations").
//  * Read with Gemini: a picture (screenshot, sketch, photo) or a drawing page (also scanned). Gemini reads it, code
//    confirms each reading on the image; picture values are checked against the rules, drawings become a RuleTrace
//    design that the rule engine checks and shows in 3D. Gemini is optional; the bot itself uses Groq / Ollama only.
//  * Human approval wherever code cannot settle something alone (NEEDS REVIEW verdicts, Gemini readings, general
//    answers, turning a drawing into a design); every decision goes to the audit chain.
//  * Comparison charts (value vs limit, verdict breakdown, readings vs confirmed) and an Accuracy tab.
// Talks to RuleTrace only through window.RuleTrace (headers, open, boot, toast, result).
const RT = () => window.RuleTrace;
const $c = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const EXAMPLES = ["What clear width must an accessible door have?",
                  "What is the difference between SOLAS and MLC?",
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
  <div class="top"><div><b>RuleMate Copilot</b><div class="tagline">Ask anything · rule answers checked against the clause · engineer approves</div></div>
    <span class="sp"></span><button id="cpClose" aria-label="Close">×</button></div>
  <div class="tabs"><button data-tab="ask" class="on">Ask</button><button data-tab="check">Check values</button>
    <button data-tab="browse">Browse</button><button data-tab="read" title="Optional: needs GEMINI_API_KEY">Gemini <span class="opt">optional</span></button>
    <button data-tab="acc">Accuracy</button></div>
  <div class="body">
    <div class="state" id="cpState">Checking the Copilot…</div>
    <section id="tab-ask">
      <textarea id="cpQ" rows="3" placeholder="Ask anything. Rule questions are answered from the loaded regulations with checked citations; other questions get a labelled general answer."></textarea>
      <div class="ex" id="cpEx"></div>
      <div class="docs" id="cpDocs"></div>
      <div class="row"><label class="small" for="cpModel">Model</label><select id="cpModel" style="flex:1"></select></div>
      <div class="small muted" id="cpModelAcc"></div>
      <button class="primary" id="cpAsk" style="width:100%;margin-top:8px">Ask</button>
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
    <section id="tab-acc" hidden><div id="cpAcc"></div><h2>Engineer decisions</h2><div id="cpRev" class="small"></div></section>
    <section id="tab-read" hidden>
      <div class="small muted" style="margin-bottom:8px"><b>Optional.</b> Reading drawings and pictures uses Google Gemini
        (free key, GEMINI_API_KEY). It is kept apart from the bot: Ask, Check values (CSV) and Browse work without it.</div>
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
  for (const k of ["ask", "check", "browse", "read", "acc"]) $c(`#tab-${k}`).hidden = t !== k;
  if (t === "browse" && !$c("#cpBList").dataset.done) outline();
  if (t === "acc") accuracy();
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
  $c("#cpState").innerHTML = `<b>Full texts:</b> ${docs}<br>Bot: ${esc(status.provider)} · Gemini (optional, drawings only): ${status.gemini ? "ready" : "not set"}`;
  if (!$c("#cpDocs").dataset.done) {
    const boxes = status.documents.map(d => `<label><input type="checkbox" value="${esc(d.doc)}" checked> ${esc(d.doc)}</label>`).join("");
    $c("#cpDocs").innerHTML = boxes; $c("#cpCheckDocs").innerHTML = boxes;
    $c("#cpBDoc").innerHTML = `<option value="">All documents</option>` + status.documents.map(d =>
      `<option value="${esc(d.doc)}">${esc(d.doc)}: ${esc(d.title)}</option>`).join("");
    $c("#cpDocs").dataset.done = "1";
    const pct = v => v == null ? "?" : Math.round(v * 100) + "%";
    $c("#cpModel").innerHTML = (status.models || []).map(m => `<option value="${esc(m.id)}" ${m.ready ? "" : "disabled"}>${esc(m.name)}${
      m.accuracy ? ` · ${pct(m.accuracy.verified)} verified` : m.id === "auto" ? "" : " · not measured"}${m.ready ? "" : " (no key)"}</option>`).join("");
    const accLine = () => { const m = (status.models || []).find(x => x.id === $c("#cpModel").value);
      $c("#cpModelAcc").innerHTML = m?.accuracy ? `Measured on 38 test questions: ${pct(m.accuracy.verified)} of answers passed every check,
        ${pct(m.accuracy.citation_precision)} of citations support their sentence, ${pct(m.accuracy.correct_not_found)} correct "not found". Answers that fail the checks are never shown as answers.`
        : m?.id === "auto" ? "Groq's best model; the local model if Groq is unavailable. Every rule answer is checked in code, whatever the model." : ""; };
    $c("#cpModel").onchange = accLine; accLine();
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
    const a = await call("/ruletrace/copilot/ask", {method: "POST", body: {question: q, docs: docs.length ? docs : null, model: $c("#cpModel").value || null}});
    const label = {verified: "verified", excerpts: "excerpts only", not_found: "not found in the rules"}[a.status];
    $c("#cpAns").innerHTML = `<div class="ans">
      <div class="meta"><span class="badge2 ${a.status}">${label}</span><span>${esc(a.model || "no model needed")}</span>
        ${a.rejected_attempts ? `<span>${a.rejected_attempts} draft(s) rejected by the checks</span>` : ""}</div>
      <div class="txt">${withCites(a.text)}</div><div id="cpClause"></div>
      <div class="how">${a.status === "verified" ? "Every sentence cites a clause that was retrieved and supports it; every number, unit and quote is in that clause."
        : a.status === "excerpts" ? "The generated answer failed the checks, so the clause text is shown instead."
        : "Nothing in the loaded regulations answers this; the closest clauses are listed."} Click a citation to read the clause.
        ${a.status === "verified" ? ` Measured: ${ACC_HINT}.` : ""}</div></div>${generalBox(a)}`;
    wireReview($c("#cpAns"));
  } catch (e) { $c("#cpAns").innerHTML = `<div class="ans" style="color:var(--fail)">${esc(e.message)}</div>`; }
  finally { $c("#cpAsk").disabled = false; }
}
const refCls = s => s.startsWith("loaded") ? "ok" : s.startsWith("named") ? "warn" : "bad";
const ACC_HINT = "91% of rule answers pass every check, 96% of citations support their sentence (see Accuracy)";
function generalBox(a) {
  const g = a.general; if (!g) return "";
  if (!g.ok) return `<div class="ans gen"><div class="meta"><span class="badge2 general">general answer</span></div>
    <div class="small muted">${esc(g.error)}</div></div>`;
  return `<div class="ans gen"><div class="meta"><span class="badge2 general">general knowledge</span><span>${esc(g.model)}</span></div>
    <div class="warnline">${esc(g.label)}. Not checked against any rule text; accuracy not measured. Confirm in the governing rules before you use it in a design.</div>
    ${(g.warnings || []).length ? `<ul class="gwarn">${g.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    <div class="txt">${esc(g.text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</div>
    <div class="lab2">Sources named by the model, checked in code ${g.certainty ? `· model's own certainty: ${esc(g.certainty)}` : ""}</div>
    ${(g.references || []).length ? `<div class="refs">${g.references.map(r => `<div class="ref ${refCls(r.status)}">
        <div><span class="rs">${esc(r.status)}</span> <b>${r.uid ? `<span class="cite" data-uid="${esc(r.uid)}">${esc(r.source)}</span>` : esc(r.source)}</b></div>
        ${r.supports ? `<div class="small">for: ${esc(r.supports)}</div>` : ""}
        <div class="small muted">${esc(r.why)}</div>
        ${r.url ? `<div class="small"><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.url)}</a> (official site, not opened by RuleMate)</div>` : ""}
        ${r.url_dropped ? `<div class="small muted">A link to a non-official site was removed.</div>` : ""}</div>`).join("")}</div>`
      : `<div class="small rej">No source named: do not rely on this answer.</div>`}
    <div id="cpClause"></div>
    ${reviewBar("general_answer", a.question, {model: g.model}, "Engineer: is this answer usable?")}</div>`;
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

// ---------- human approval ----------
function reviewBar(kind, ref, detail, ask) {
  return `<div class="rv" data-kind="${esc(kind)}" data-ref="${esc(ref)}" data-detail="${esc(JSON.stringify(detail || {}))}">
    <span class="small">${esc(ask)}</span><input class="rvnote" placeholder="note (optional)">
    <button data-dec="approve">Approve</button><button data-dec="reject">Reject</button></div>`;
}
function wireReview(root) {
  root.querySelectorAll(".rv button[data-dec]").forEach(b => b.onclick = async e => {
    e.stopPropagation();
    const bar = b.closest(".rv");
    try { const r = await call("/ruletrace/copilot/review", {method: "POST", body: {kind: bar.dataset.kind, ref: bar.dataset.ref,
            decision: b.dataset.dec, note: bar.querySelector(".rvnote").value, detail: JSON.parse(bar.dataset.detail || "{}")}});
      bar.innerHTML = `<span class="small ${r.decision === "approve" ? "ok" : "rej"}">${r.decision === "approve" ? "✓ Approved" : "✗ Rejected"} by ${esc(r.by)} · ${esc(r.at)} · recorded in the audit log</span>`;
    } catch (err) { toast(err.message, true); }
  });
  root.querySelectorAll(".rv input").forEach(i => i.onclick = e => e.stopPropagation());
}

// ---------- comparison charts ----------
const VCOL = {PASS: "var(--pass)", FAIL: "var(--fail)", "NEEDS REVIEW": "var(--warn)", "NO RULE": "var(--unknown)", "CHECK INPUT": "var(--unknown)"};
function breakdown(counts, title) {
  const keys = Object.keys(VCOL).filter(k => counts[k]); const n = keys.reduce((a, k) => a + counts[k], 0);
  if (!n) return "";
  return `<div class="chart"><div class="ct">${esc(title)}</div><div class="stack">${keys.map(k =>
      `<span style="flex:${counts[k]};background:${VCOL[k]}" title="${esc(k)}: ${counts[k]} of ${n}"></span>`).join("")}</div>
    <div class="legend">${keys.map(k => `<span><i style="background:${VCOL[k]}"></i>${esc(k)} ${counts[k]}</span>`).join("")}</div></div>`;
}
function valueVsLimit(results) {
  const rows = results.filter(x => x.deciding && x.deciding.limit_value != null && x.deciding.value_in_limit_unit != null);
  if (!rows.length) return "";
  return `<div class="chart"><div class="ct">Design value compared with the rule limit</div>
    <div class="small muted">Bar = your value, black line = the limit (same unit). Hover for the numbers.</div>
    ${rows.map(x => { const d = x.deciding, v = d.value_in_limit_unit, l = d.limit_value;
      const top = Math.max(v, l) * 1.15 || 1, pv = Math.max(0, v) / top * 100, pl = Math.max(0, l) / top * 100;
      const diff = v - l, pct = l ? Math.round(diff / Math.abs(l) * 100) : 0;
      return `<div class="vl" title="${esc(x.parameter)}: ${v} ${esc(d.unit)} · limit ${esc(d.limit)} · ${diff >= 0 ? "+" : ""}${+diff.toFixed(2)} ${esc(d.unit)} (${pct >= 0 ? "+" : ""}${pct}%)">
        <div class="vlh"><span>${esc(x.item_id)} ${esc(x.parameter)}</span><span>${+v.toFixed(2)} vs ${esc(d.comparator)} ${l} ${esc(d.unit)}</span></div>
        <div class="track"><span class="bar" style="width:${pv}%;background:${VCOL[x.verdict] || "var(--unknown)"}"></span>
          <span class="lim" style="left:${pl}%"></span></div></div>`; }).join("")}</div>`;
}
function readingsChart(pairs, title) {  // [[label, value, max]]
  return `<div class="chart"><div class="ct">${esc(title)}</div>${pairs.map(([lab, v, max, col]) =>
    `<div class="vl" title="${esc(lab)}: ${v} of ${max}"><div class="vlh"><span>${esc(lab)}</span><span>${v} of ${max}</span></div>
      <div class="track"><span class="bar" style="width:${max ? v / max * 100 : 0}%;background:${col || "var(--teal)"}"></span></div></div>`).join("")}</div>`;
}

// ---------- accuracy ----------
async function accuracy() {
  try { const a = await call("/ruletrace/copilot/accuracy");
    const M = a.models || [], pc = v => v == null ? "–" : Math.round(v * 100) + "%";
    const best = M.length ? M.reduce((x, y) => (y.verified ?? -1) > (x.verified ?? -1) ? y : x) : null;
    const mchart = M.length ? `<div class="chart"><div class="ct">Which bot model is most accurate?</div>
      <div class="small muted">Same 38 questions for each model. Bars: answers that passed every check (verified). Hover for details.</div>
      ${M.map(m => `<div class="vl" title="${esc(m.name)}: verified ${pc(m.verified)}, citations correct ${pc(m.citation_precision)}, key numbers ${pc(m.fact_recall)}, correct 'not found' ${pc(m.correct_not_found)}, median ${m.latency_p50_s ?? "?"} s">
        <div class="vlh"><span>${m === best ? "★ " : ""}${esc(m.name)}</span><span><b>${pc(m.verified)}</b></span></div>
        <div class="track"><span class="bar" style="width:${(m.verified || 0) * 100}%;background:var(--teal)"></span></div></div>`).join("")}
      <table><tr><th>Model</th><th>Verified</th><th>Citations correct</th><th>Key numbers</th><th>"Not found" right</th><th>Median time</th></tr>
      ${M.map(m => `<tr><td>${esc(m.name)}</td><td>${pc(m.verified)}</td><td>${pc(m.citation_precision)}</td><td>${pc(m.fact_recall)}</td><td>${pc(m.correct_not_found)}</td><td>${m.latency_p50_s ?? "–"} s</td></tr>`).join("")}</table>
      ${best ? `<div class="small" style="margin-top:6px"><b>Most accurate: ${esc(best.name)}</b>. Whatever the model, an answer that fails the checks is shown as clause excerpts, never as an answer.</div>` : ""}</div>` : "";
    $c("#cpAcc").innerHTML = `<div class="small muted">Measured ${esc(a.measured)}. ${esc(a.how)}</div>` + mchart + a.groups.map(g =>
      `<div class="chart"><div class="ct">${esc(g.name)}</div><div class="small muted">${esc(g.model)}</div>${g.items.map(i => i.value == null
        ? `<div class="small warnline">${esc(i.metric)}</div>`
        : `<div class="vl" title="${esc(i.metric)}: ${Math.round(i.value * 100)}% of ${i.n}${i.note ? " · " + esc(i.note) : ""}">
          <div class="vlh"><span>${esc(i.metric)}</span><span><b>${Math.round(i.value * 100)}%</b> <span class="muted">n=${i.n}</span></span></div>
          <div class="track"><span class="bar" style="width:${i.value * 100}%;background:${i.lower_is_better ? "var(--fail)" : "var(--teal)"}"></span></div></div>`).join("")}</div>`).join("")
      + `<div class="short"><div class="lab">Read this before trusting the numbers</div><ul>${a.caveats.map(c => `<li>${esc(c)}</li>`).join("")}</ul></div>`;
  } catch (e) { $c("#cpAcc").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
  try { const r = await call("/ruletrace/copilot/reviews");
    $c("#cpRev").innerHTML = r.length ? `<table><tr><th>When</th><th>Who</th><th>What</th><th>Decision</th></tr>${r.map(x =>
      `<tr><td>${esc(x.at.replace("T", " ").slice(0, 16))}</td><td>${esc(x.by)}</td><td>${esc(x.kind)}: ${esc(x.ref)}${x.note ? `<div class="muted">${esc(x.note)}</div>` : ""}</td>
       <td class="${x.decision === "approve" ? "ok" : "rej"}">${esc(x.decision)}</td></tr>`).join("")}</table>` : `<div class="muted">No decisions yet.</div>`;
  } catch (e) { $c("#cpRev").textContent = e.message; }
}

// ---------- read with Gemini ----------
$c("#cpFile").onchange = () => { const f = $c("#cpFile").files[0]; $c("#cpPageRow").hidden = !(f && /\.pdf$/i.test(f.name)); };
let lastDrawing = null;
$c("#cpRead").onclick = async () => {
  const f = $c("#cpFile").files[0]; if (!f) return toast("Choose a picture or a PDF first", true);
  const fd = new FormData(); fd.append("file", f); fd.append("page", $c("#cpPage").value || "1");
  $c("#cpRead").disabled = true; $c("#cpOut").innerHTML = `<div class="ans muted">Gemini is reading ${esc(f.name)}; code is confirming each reading… (up to a minute)</div>`;
  try { const r = await call("/ruletrace/copilot/read", {method: "POST", body: fd});
        r.kind === "picture" ? showPicture(r) : r.kind === "drawing" ? showDrawing(r)
          : ($c("#cpOut").innerHTML = inShort(r.in_short)); }
  catch (e) { $c("#cpOut").innerHTML = `<div class="ans" style="color:var(--fail)">${esc(e.message)}</div>`; }
  finally { $c("#cpRead").disabled = false; }
};
function inShort(sm) {
  if (!sm) return "";
  return `<div class="short"><div class="lab">In short</div><b>${esc(sm.headline)}</b>
    ${sm.points?.length ? `<ul>${sm.points.map(p => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    ${sm.next?.length ? `<div class="lab" style="margin-top:6px">What to do next</div><ul>${sm.next.map(p => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}</div>`;
}
const pill = v => v ? `<span class="pill ${v === "PASS" ? "pass" : v === "FAIL" ? "fail" : v === "NO RULE" || v === "CHECK INPUT" ? "unknown" : "warn"}"
  title="${v === "NO RULE" ? "None of the loaded rule texts limits this value" : v === "NEEDS REVIEW" ? "A rule applies, but a condition must be confirmed" : v === "CHECK INPUT" ? "The value is not a number or has no unit" : ""}">${esc(v)}</span>`
  : `<span class="pill unknown">not checked</span>`;
function showPicture(r) {
  const counts = ["PASS", "FAIL", "NEEDS REVIEW"].map(v => r.items.filter(i => i.verdict === v).length);
  $c("#cpOut").innerHTML = inShort(r.in_short) + `<div class="sum"><div><b>${r.n_confirmed}/${r.n_readings}</b>values confirmed</div>
      <div><b style="color:var(--fail)">${counts[1]}</b>fail</div><div><b style="color:var(--pass)">${counts[0]}</b>pass</div></div>
    ${readingsChart([["Values Gemini read", r.n_readings, r.n_readings, "var(--unknown)"],
                     ["Confirmed on the picture by code", r.n_confirmed, r.n_readings, "var(--teal)"]], "Gemini readings vs confirmed in code")}
    ${valueVsLimit(r.items.filter(i => i.verdict).map((i, k) => ({...i, item_id: `P${String(k + 1).padStart(2, "0")}`, value: i.written})))}
    <img class="prev" src="${r.preview}" alt="">
    ${r.items.map((it, i) => `<div class="item" data-i="${i}"><div class="h"><span class="t">${esc(it.parameter)}: ${esc(it.written)}</span>${pill(it.verdict)}</div>
      <div class="small muted">${esc(it.space)} · ${it.confirmed ? `<span class="ok">confirmed on the picture</span>` : "Gemini only: not found on the picture, not used"}
        ${it.clauses.length ? " · " + it.clauses.map(esc).join(", ") : ""}</div>
      <div class="more" hidden><img src="${it.crop}" alt=""><pre>${esc(it.reason)}</pre></div>
      ${it.confirmed ? reviewBar(it.verdict === "NEEDS REVIEW" ? "verdict" : "reading", `${r.file}: ${it.parameter} ${it.written}`,
        {verdict: it.verdict, space: it.space}, it.verdict === "NEEDS REVIEW" ? "Engineer: does the condition hold?" : "Engineer: is this reading right?") : ""}</div>`).join("")}
    <div class="how">Gemini read the values; code confirmed each one is written at that spot (local OCR) and compared it with the rule. Gemini never decides PASS or FAIL.</div>`;
  document.querySelectorAll("#cpOut .item").forEach(el => el.onclick = e => { if (e.target.closest(".rv")) return;
    const m = el.querySelector(".more"); m.hidden = !m.hidden; });
  wireReview($c("#cpOut"));
}
function showDrawing(r) {
  lastDrawing = r;
  const rooms = r.items.filter(i => ["stateroom", "hospital", "stairway"].includes(i.kind));
  const enough = r.summary.cabins + (r.summary.hospital ? 1 : 0) >= 3;
  const conf = r.items.filter(i => i.confirmed);
  const cand = r.items.map((it, i) => [it, i]).filter(([it]) => it.confirmed && ["stateroom", "hospital"].includes(it.kind));
  $c("#cpOut").innerHTML = inShort(r.in_short) + readingsChart([
      ["Labels Gemini read", r.n_readings, r.n_readings, "var(--unknown)"],
      ["Confirmed on the drawing by code", r.n_confirmed, r.n_readings, "var(--teal)"],
      ["Cabins", conf.filter(i => i.kind === "stateroom").length, r.n_readings, "var(--teal)"],
      ["Cabins with printed berth count", conf.filter(i => i.kind === "stateroom" && i.berths_confirmed).length, r.n_readings, "var(--teal)"]],
      "What was read from this sheet") + `
    <img class="prev" src="${r.preview}" alt="">
    ${r.items.length ? `<details style="margin-top:8px"><summary>Show all readings (${r.n_confirmed} confirmed of ${r.n_readings})</summary>
      <table><tr><th>Name on the drawing</th><th>Room no.</th><th>Type</th><th>Berths</th><th></th></tr>
      ${r.items.map(i => `<tr><td>${esc(i.label)}</td><td>${esc(i.room_number || "")}</td><td>${esc({stateroom: "cabin", hospital: "hospital",
          stairway: "stairway", lab: "laboratory", mess: "mess", corridor: "corridor", toilet_shower: "toilet / shower"}[i.kind] || "other space")}</td>
        <td>${i.berths ?? ""}${i.berths && !i.berths_confirmed ? " ?" : ""}</td>
        <td class="${i.confirmed ? "ok" : "no"}">${i.confirmed ? "✓ on drawing" : "not confirmed"}</td></tr>`).join("")}</table>
      <div class="small muted">"?" = berths counted by Gemini but not printed on the drawing: not used.</div></details>` : ""}
    <div ${enough ? "" : "hidden"}>
    <h2 style="margin-top:14px">Check it in RuleTrace 3D</h2>
    <div class="small muted">Fill in what you know about the ship; the keel date and persons on board decide which rules apply.</div>
    <div class="ship"><input id="cpShipName" placeholder="Ship name"><select id="cpShipType" title="Type of ship">
      ${["research", "passenger", "cargo", "special_purpose", "ro_ro_passenger"].map(t => `<option>${t}</option>`).join("")}</select>
      <input id="cpLen" type="number" placeholder="Ship length in m"><input id="cpPers" type="number" placeholder="Persons on board">
      <label class="small">Keel laid <input id="cpKeel" type="date"></label>
      <label class="small"><input id="cpFull" type="checkbox"> this sheet shows all cabins of the ship</label></div>
    <h2 style="margin-top:14px">Engineer approval</h2>
    <div class="small muted">Compare each room with the drawing above. Untick any reading that is wrong; only ticked rooms are used.</div>
    <div class="appr">${cand.map(([it, i]) => `<label><input type="checkbox" class="cpKeep" value="${i}" checked>
      ${esc(it.label)} ${esc(it.room_number || "")} <span class="muted">${it.kind === "hospital" ? "hospital" : `cabin${it.berths_confirmed && it.berths ? ", " + it.berths + " berths" : ""}`}</span></label>`).join("")}</div>
    <label class="small" style="display:block;margin-top:6px"><input type="checkbox" id="cpApprove"> I have checked these readings against the drawing and approve them</label>
    <input id="cpApNote" placeholder="Note for the audit log (optional)" style="width:100%;margin-top:4px">
    <button class="primary" id="cpTo3d" style="width:100%;margin-top:8px" disabled>Approve and check in RuleTrace 3D</button></div>
    <div class="how">Page ${r.page} of ${r.pages}. Gemini only proposes; a room is used only when its name or number is found on the drawing at the place Gemini points to.</div>`;
  if (!enough) return;
  $c("#cpApprove").onchange = () => $c("#cpTo3d").disabled = !$c("#cpApprove").checked;
  $c("#cpTo3d").onclick = async () => {
    const rejected = [...document.querySelectorAll(".cpKeep")].filter(c => !c.checked).map(c => +c.value);
    const ship = {name: $c("#cpShipName").value || undefined, ship_type: $c("#cpShipType").value};
    if ($c("#cpLen").value) ship.length_m = +$c("#cpLen").value;
    if ($c("#cpPers").value) ship.persons_total = +$c("#cpPers").value;
    if ($c("#cpKeel").value) ship.keel_laid = $c("#cpKeel").value;  // date-based rules (MLC) need it
    if ($c("#cpFull").checked) ship.complete_arrangement = true;     // "is it on board?" and total-berth checks
    try { const d = await call("/ruletrace/copilot/design", {method: "POST", body: {readings_id: r.readings_id, ship, approved: true, rejected, note: $c("#cpApNote").value}});
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
  if (r.kind === "error") { $c("#cpCheckOut").innerHTML = inShort(r.in_short); return; }
  $c("#cpCheckOut").innerHTML = inShort(r.in_short) + `<div class="sum" style="grid-template-columns:repeat(4,1fr)"><div><b style="color:var(--fail)">${r.counts.FAIL}</b>fail</div>
      <div><b style="color:var(--warn)">${r.counts["NEEDS REVIEW"]}</b>review</div><div><b style="color:var(--pass)">${r.counts.PASS}</b>pass</div>
      <div><b style="color:var(--unknown)">${(r.counts["NO RULE"] || 0) + (r.counts["CHECK INPUT"] || 0)}</b>no rule / input</div></div>
    ${breakdown(r.counts, "Verdicts")}${valueVsLimit(r.results)}
    ${r.counts["NO RULE"] ? `<div class="small muted" style="margin-top:6px"><b>NO RULE</b>: none of the loaded rule texts (${esc(Array.isArray(r.docs) ? r.docs.join(", ") : r.docs)}) limits these values; add the rule (e.g. class rules, SOLAS) to check them. Not the same as NEEDS REVIEW, where a rule applies but a condition must be confirmed.</div>` : ""}
    ${r.counts["CHECK INPUT"] ? `<div class="small muted"><b>CHECK INPUT</b>: the value is not a single number or has no unit (e.g. a crane rating "4T/14M", a dimension without mm/in); correct it and check again.</div>` : ""}
    ${r.results.map((x, i) => `<div class="item" data-ci="${i}"><div class="h"><span class="t">${esc(x.parameter)}: ${esc(x.value)}</span>${pill(x.verdict)}</div>
      <div class="small muted">${esc(x.item_id)} · ${esc(x.space)}${x.deciding ? ` · ${esc(x.deciding.limit)} <span class="cite" data-uid="${esc(x.deciding.uid)}">${esc(x.deciding.clause)}</span>` : " · no matching limit"}</div>
      <div class="more" hidden>${x.crop ? `<img src="${x.crop}" alt="">` : ""}<pre>${esc(x.reason)}</pre>
        ${x.source ? `<div class="small muted">From: ${esc(x.source)}</div>` : ""}</div><div class="cl"></div>
      ${x.verdict === "NEEDS REVIEW" ? reviewBar("verdict", `${x.item_id} ${x.parameter} ${x.value}`, {limit: x.deciding?.limit, clause: x.deciding?.clause},
          "Engineer: does the condition hold?")
        : x.crop ? reviewBar("reading", `${x.item_id} ${x.parameter} ${x.value}`, {verdict: x.verdict}, "Read by Gemini. Engineer: is this reading right?") : ""}</div>`).join("")}
    ${(r.unconfirmed || []).length ? `<div class="small muted" style="margin-top:8px">Read by Gemini but not found on the page, so not checked: ${r.unconfirmed.map(u => esc(`${u.parameter} ${u.written}`)).join("; ")}</div>` : ""}
    ${(r.notes || []).length ? `<div class="small muted">${r.notes.map(esc).join(" · ")}</div>` : ""}
    <button class="aib" id="cpDl">Download results (CSV)</button>
    <div class="how">Limits come from the clause text; values are converted with pint and compared in code. NEEDS REVIEW when a limit has a condition code cannot confirm.</div>`;
  document.querySelectorAll("#cpCheckOut .item").forEach(el => el.onclick = e => {
    if (e.target.closest(".cite, .rv")) return; const m = el.querySelector(".more"); m.hidden = !m.hidden; });
  wireReview($c("#cpCheckOut"));
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
