// Compliance report for one design check: a printable page (the browser's "Save as PDF" makes the PDF).
// Built from the check result the viewer already has, so the report is exactly what the engineer saw.

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const LABEL = {fail: "Fails", warn: "Warning", unknown: "Can't check", pass: "Passes"};

export function reportHtml(r, meta) {
  const s = r.ship, c = r.counts, by = k => r.findings.filter(f => f.status === k);
  const rv = meta.reviews || {findings: {}, signoff: {}}, so = rv.signoff || {};
  const when = new Date().toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
  const row = f => `<tr class="${f.status}">
      <td><span class="st ${f.status}">${LABEL[f.status]}</span></td>
      <td><b>${esc(f.title)}</b><div class="m">${esc(f.ref.startsWith(f.regime.split(" ")[0]) ? f.ref : f.regime + " " + f.ref)}${f.verified ? "" : " · value not yet verified against the official text"}</div>
        ${f.quote ? `<div class="q">"${esc(f.quote)}"</div>` : ""}</td>
      <td>${esc(f.element_name)}<div class="m">${esc(f.element_id || "whole ship")}${f.deck ? " · " + esc(f.deck) : ""}</div></td>
      <td>${esc(f.required || "–")}</td><td><b>${esc(f.actual ?? "not given")}</b><div class="m">${esc(f.measured || "")}</div></td>
      <td>${esc(f.status === "pass" ? "" : (f.fix || f.message || ""))}${f.status !== "pass" && f.why_rule ? `<div class="m">Why: ${esc(f.why_rule)}</div>` : ""}
        ${f.status !== "pass" ? decision(f) : ""}</td></tr>`;
  const decision = f => { const d = rv.findings[`${f.rule_id}|${f.element_id || ""}`];
    return d ? `<div class="dec ${d.decision}"><b>${d.decision === "confirm" ? "Confirmed: must be fixed" : "Dismissed: " + esc((rv.reasons || {})[d.reason_code] || d.reason_code)}</b>
      · ${esc(d.by)}, ${esc(String(d.at).slice(0, 16).replace("T", " "))}${d.reason ? `<br>"${esc(d.reason)}"` : ""}</div>` : `<div class="dec none">Not reviewed by an engineer</div>`; };
  const section = (title, list) => list.length ? `<h2>${title} (${list.length})</h2>
    <table style="table-layout:fixed"><colgroup><col style="width:8%"><col style="width:29%"><col style="width:15%"><col style="width:11%"><col style="width:11%"><col style="width:26%"></colgroup>
    <thead><tr><th>Result</th><th>Rule</th><th>Part</th><th>Required</th><th>Design</th><th>What to do</th></tr></thead>
    <tbody>${list.map(row).join("")}</tbody></table>` : "";
  const it = r.interpretation || {};
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Compliance report: ${esc(s.name)}</title><style>
  @page{size:A4 landscape;margin:12mm}*{box-sizing:border-box}body{font:11px/1.4 system-ui,-apple-system,"Segoe UI",Arial,sans-serif;color:#16202a;margin:0 auto;max-width:1100px;padding:16px}
  h1{font-size:20px;margin:0}h2{font-size:13px;margin:18px 0 6px;text-transform:uppercase;letter-spacing:.04em;color:#0b2545}
  .top{display:flex;justify-content:space-between;gap:20px;border-bottom:2px solid #0b2545;padding-bottom:10px}
  .m{color:#5d6975;font-size:10px}.q{font-style:italic;color:#384552;font-size:10px;margin-top:2px}
  .kv{display:grid;grid-template-columns:auto 1fr;gap:1px 10px;font-size:11px}.kv span{color:#5d6975}
  .counts{display:flex;gap:10px;margin:12px 0}.counts div{border:1px solid #dde3e8;border-radius:6px;padding:6px 12px;text-align:center}.counts b{display:block;font-size:18px}
  table{width:100%;border-collapse:collapse;page-break-inside:auto}tr{page-break-inside:avoid}th{text-align:left;font-size:10px;color:#5d6975;border-bottom:1px solid #b8c4ce;padding:4px}
  td{border-bottom:1px solid #e5e9ed;padding:5px 4px;vertical-align:top}
  .st{display:inline-block;border-radius:9px;padding:1px 7px;color:#fff;font-size:9.5px;white-space:nowrap}.st.fail{background:#d64545}.st.warn{background:#d88b16}.st.unknown{background:#6b7c93}.st.pass{background:#2e8b57}
  .dec{margin-top:4px;font-size:10px;padding:3px 5px;border-radius:4px;background:#f1f3f5}.dec.confirm{background:#e8f5ee}.dec.none{background:#fdf3e3;color:#8a5a0b}
  .sign{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-top:22px}.sign div{border-top:1px solid #16202a;padding-top:4px;font-size:10px;color:#5d6975;height:46px}
  .bar{position:sticky;top:0;background:#fff;padding:8px 0;margin-bottom:8px;border-bottom:1px solid #dde3e8}.bar button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid #138a8a;background:#138a8a;color:#fff;cursor:pointer}
  @media print{.bar{display:none}body{padding:0}}
  </style></head><body>
  <div class="bar"><button onclick="window.print()">Save as PDF / print</button> <span class="m">Choose "Save as PDF" as the printer.</span></div>
  <div class="top"><div><h1>Design compliance report</h1><div style="font-size:14px;margin-top:2px">${esc(s.name)}</div>
      <div class="m">${esc(meta.title || r.filename)} · checked ${esc(when)} by ${esc(meta.user)}</div></div>
    <div class="kv"><span>Design file</span><b>${esc(r.filename)}</b><span>Design fingerprint</span><span>${esc(r.design_sha256)}</span>
      <span>Rule pack</span><span>${esc(r.rules_sha256)}</span><span>Check history</span><b>entry #${esc(r.audit_seq)}${meta.chain ? (meta.chain.ok ? " · record intact" : " · RECORD CHANGED") : ""}</b></div></div>
  <h2>Ship</h2><div class="kv"><span>Type</span><b>${esc(s.ship_type.replace("_", " "))}</b><span>Size</span><b>${s.length_m} × ${s.breadth_m} m${s.gross_tonnage ? " · " + Number(s.gross_tonnage).toLocaleString() + " GT" : ""}</b>
    <span>On board</span><b>${s.passengers} passengers · ${s.persons_total || "?"} persons</b><span>Keel laid</span><b>${esc(s.keel_laid || "not given")}</b>
    <span>Flag / areas</span><b>${esc(s.flag || "–")} · ${esc((s.operating_areas || []).join(", ") || "no special areas")}</b>
    ${Object.keys(r.what_if || {}).length ? `<span>What-if</span><b>${esc(JSON.stringify(r.what_if))}</b>` : ""}</div>
  ${it.scale ? `<h2>How the drawing was read</h2><div class="kv"><span>Scale</span><b>${esc(it.scale)}</b><span>Decks</span><b>${esc((it.decks || []).map(d => `${d.deck} (${d.height_m ?? "?"} m)`).join(", "))}</b>
    <span>Found</span><b>${esc(Object.entries(it.found || {}).filter(([, v]) => v).map(([k, v]) => `${v} ${k}`).join(" · "))}</b></div>
    ${(it.notes || []).map(n => `<div class="m">${esc(n)}</div>`).join("")}` : ""}
  <div class="counts"><div><b style="color:#d64545">${c.fail}</b>fail</div><div><b style="color:#d88b16">${c.warn}</b>warning</div>
    <div><b style="color:#6b7c93">${c.unknown}</b>can't check</div><div><b style="color:#2e8b57">${c.pass}</b>pass</div>
    <div><b>${r.rules_not_applicable.length}</b>rules not applicable</div></div>
  ${section("Fails", by("fail"))}${section("Warnings", by("warn"))}${section("Can't check (information missing)", by("unknown"))}${section("Passes", by("pass"))}
  <h2>Rules that don't apply to this ship (${r.rules_not_applicable.length})</h2>
  <table><tbody>${r.rules_not_applicable.map(x => `<tr><td><b>${esc(x.title)}</b><div class="m">${esc(x.regime)} ${esc(x.ref)}</div></td><td>${esc(x.reasons.join("; "))}</td></tr>`).join("")}</tbody></table>
  <div class="m" style="margin-top:12px">Automated pre-check by RuleTrace. Values marked "not yet verified" must be confirmed against the official rule text.
    This report supports, and does not replace, the engineer's and the classification society's review.</div>
  <div class="sign"><div>Prepared by (design engineer)<br><b style="color:#16202a">${esc(so.prepared_by || "")}</b> ${esc(String(so.prepared_at || "").slice(0, 16).replace("T", " "))}</div>
    <div>Approved by (compliance lead)<br><b style="color:#16202a">${esc(so.approved_by || "")}</b> ${esc(String(so.approved_at || "").slice(0, 16).replace("T", " "))}
      ${so.stage === "approved" ? "" : `<br><span style="color:#a12a2a">${so.stage === "prepared" ? "Waiting for approval" : "Not yet signed off"}</span>`}</div>
    <div>Class / flag remarks</div></div>
  </body></html>`;
}
