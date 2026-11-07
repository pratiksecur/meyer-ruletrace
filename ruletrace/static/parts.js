// Detailed 3D shapes for each kind of ship part, built from its box and its properties.
// Every part is built in a local frame: X = length (ship x, forward), Y = height (ship z, up), Z = width (ship y).
// The real values from the design drive the shape (step count from the stair height, the door's clear width,
// the guard rail's lowest opening, the cabin's berths ...), so what you see is what was checked.
import * as THREE from "three";

const STEEL = 0x8a96a3, DARK = 0x5b6670, WOOD = 0xb08d63, BED = 0xe8edf2, GLASS = 0xbfd6e6;

export function mm(v, fallback) {                       // "880 mm" / "0.88 m" / 0.88 -> metres
  if (v === undefined || v === null || v === "") return fallback;
  if (typeof v === "number") return v;
  const m = String(v).match(/([\d.,]+)\s*(mm|cm|m)?\b/i);
  if (!m) return fallback;
  const n = parseFloat(m[1].replace(",", "."));
  return { mm: n / 1000, cm: n / 100, m: n }[(m[2] || "m").toLowerCase()] ?? n;
}

function mat(color, o = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness: 0.7, metalness: o.metal ?? 0.1, transparent: !!o.opacity,
    opacity: o.opacity ?? 1, depthWrite: !o.opacity, side: o.side ?? THREE.FrontSide,
    polygonOffset: !!o.opacity, polygonOffsetFactor: o.opacity ? 2 : 0, polygonOffsetUnits: o.opacity ? 2 : 0 });
}
function box(g, w, h, d, x, y, z, m, name) {         // w along X, h along Y, d along Z; (x,y,z) = min corner
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(Math.max(w, 0.01), Math.max(h, 0.01), Math.max(d, 0.01)), m);
  mesh.position.set(x + w / 2, y + h / 2, z + d / 2);
  if (name) mesh.name = name;
  g.add(mesh); return mesh;
}
function rod(g, a, b, r, m) {                          // cylinder between two local points
  const va = new THREE.Vector3(...a), vb = new THREE.Vector3(...b), len = va.distanceTo(vb);
  const c = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 10), m);
  c.position.copy(va).add(vb).multiplyScalar(0.5);
  c.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), vb.clone().sub(va).normalize());
  g.add(c); return c;
}
function edges(g, mesh, color, opacity = 0.9) {
  const e = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry, 25), new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
  e.position.copy(mesh.position); e.quaternion.copy(mesh.quaternion); g.add(e); return e;
}

// -- part builders: (group, L, H, W, props, color) ----------------------------------------------------
const B = {
  stair(g, L, H, W, p, c) {
    const n = Math.max(3, Math.min(40, Math.round(H / 0.18))), run = L / n, cw = Math.min(W, mm(p.clear_width, W * 0.85));
    const z0 = (W - cw) / 2, sm = mat(c), steel = mat(STEEL, { metal: 0.4 });
    for (let i = 0; i < n; i++) box(g, run, H * (i + 1) / n, cw, i * run, 0, z0, sm);            // treads, as many as the rise needs
    box(g, L, 0.05, 0.04, 0, H - 0.05, z0 - 0.04, steel); box(g, L, 0.05, 0.04, 0, H - 0.05, z0 + cw, steel);
    for (const z of [z0 - 0.02, z0 + cw + 0.02]) {                                                // handrails 0.9 m above the pitch line
      rod(g, [0, 0.9 + H / n, z], [L, H + 0.9, z], 0.025, steel);
      for (let i = 0; i <= 3; i++) { const x = L * i / 3; rod(g, [x, H * i / 3, z], [x, H * i / 3 + 0.9, z], 0.02, steel); }
    }
    return { dim: { axis: "z", from: z0, to: z0 + cw, at: [L / 2, H + 0.25], label: "clear width" } };
  },
  corridor(g, L, H, W, p, c) {
    const cw = Math.min(W, mm(p.clear_width, W)), z0 = (W - cw) / 2;
    const floor = box(g, L, 0.06, cw, 0, 0, z0, mat(c)); edges(g, floor, c);
    const wall = mat(GLASS, { opacity: 0.22 });
    box(g, L, H, 0.05, 0, 0, z0 - 0.05, wall); box(g, L, H, 0.05, 0, 0, z0 + cw, wall);
    for (let x = 0; x < L; x += 3) box(g, 0.8, 0.01, 0.12, x + 0.6, 0.065, z0 + cw / 2 - 0.06, mat(0xffffff));  // centre-line marks
    return { dim: { axis: "z", from: z0, to: z0 + cw, at: [Math.min(L / 2, 4), 0.3], label: "clear width" } };
  },
  door(g, L, H, W, p, c, weather) {
    const alongZ = W >= L, span = alongZ ? W : L, th = Math.max(alongZ ? L : W, 0.06);
    const cw = Math.min(span, mm(p.clear_width, span * 0.85)), s0 = (span - cw) / 2, steel = mat(DARK, { metal: 0.3 });
    const sill = weather ? mm(p.sill_height, 0.3) : mm(p.threshold_height, 0.01);
    // a box along the door span: from s along the span, length len, at height y, height h, full wall thickness
    const put = (s, len, y, h, m) => alongZ ? box(g, th, h, len, 0, y, s, m) : box(g, len, h, th, s, y, 0, m);
    put(s0 - 0.06, 0.06, 0, H, steel); put(s0 + cw, 0.06, 0, H, steel); put(s0 - 0.06, cw + 0.12, H - 0.08, 0.08, steel);   // frame
    put(s0, cw, 0, Math.max(sill, 0.01), mat(weather ? c : DARK));                                                          // threshold / sill at its real height
    const lh = H - 0.1 - sill;
    const leaf = new THREE.Mesh(new THREE.BoxGeometry(alongZ ? 0.04 : cw, lh, alongZ ? cw : 0.04), mat(c));
    const hinge = new THREE.Group();                                       // leaf opened about 35 degrees on its hinge
    hinge.position.set(alongZ ? th / 2 : s0, sill + lh / 2, alongZ ? s0 : th / 2);
    leaf.position.set(alongZ ? 0 : cw / 2, 0, alongZ ? cw / 2 : 0); hinge.add(leaf); hinge.rotation.y = alongZ ? -0.6 : 0.6; g.add(hinge);
    const handle = new THREE.Mesh(new THREE.SphereGeometry(0.035, 10, 8), mat(STEEL, { metal: 0.7 }));
    handle.position.set(alongZ ? 0.04 : cw / 2 - 0.1, 0, alongZ ? cw / 2 - 0.1 : 0.04); leaf.add(handle);
    return { dim: alongZ ? { axis: "z", from: s0, to: s0 + cw, at: [th / 2, H + 0.2], label: "clear width" }
                         : { axis: "x", from: s0, to: s0 + cw, at: [H + 0.2, th / 2], label: "clear width" },
             dimSill: { axis: weather ? "y" : null, from: 0, to: sill, at: alongZ ? [th + 0.15, s0 + cw / 2] : [s0 + cw / 2, th + 0.15], label: "sill" } };
  },
  weather_door(g, L, H, W, p, c) { return B.door(g, L, H, W, p, c, true); },
  ramp(g, L, H, W, p, c) {
    const m = String(p.running_slope || "").match(/1\s*:\s*([\d.]+)/), rise = m ? Math.min(H || L / +m[1], L / +m[1]) : H;
    const geo = new THREE.BufferGeometry(), v = [0, 0, 0, L, 0, 0, L, rise, 0, 0, 0, W, L, 0, W, L, rise, W];
    geo.setAttribute("position", new THREE.Float32BufferAttribute(v, 3));
    geo.setIndex([0, 2, 1, 3, 4, 5, 0, 3, 5, 0, 5, 2, 1, 2, 5, 1, 5, 4, 0, 1, 4, 0, 4, 3]); geo.computeVertexNormals();
    const w = new THREE.Mesh(geo, mat(c, { side: THREE.DoubleSide })); g.add(w); edges(g, w, c);
    const steel = mat(STEEL, { metal: 0.4 });
    for (const z of [0.02, W - 0.02]) rod(g, [0, 0.9, z], [L, rise + 0.9, z], 0.025, steel);
    return { dim: { axis: "slope", at: [L / 2, rise + 0.5], label: "slope " + (p.running_slope || "") } };
  },
  ventilator(g, L, H, W, p, c) {
    const r = Math.min(L, W) / 2, ch = Math.min(H, mm(p.coaming_height, H * 0.8));
    const coam = new THREE.Mesh(new THREE.CylinderGeometry(r, r, ch, 20), mat(c, { metal: 0.3 })); coam.position.set(L / 2, ch / 2, W / 2); g.add(coam);
    const head = new THREE.Mesh(new THREE.SphereGeometry(r * 1.35, 20, 10, 0, Math.PI * 2, 0, Math.PI / 2), mat(STEEL, { metal: 0.4 }));
    head.position.set(L / 2, ch + 0.08, W / 2); g.add(head);
    return { dim: { axis: "y", from: 0, to: ch, at: [L / 2 + r + 0.15, W / 2], label: "coaming" } };
  },
  air_pipe(g, L, H, W, p, c) {
    const r = Math.max(0.04, Math.min(L, W) / 2), h = mm(p.height_to_opening, H);
    rod(g, [L / 2, 0, W / 2], [L / 2, h, W / 2], r, mat(c, { metal: 0.3 }));
    const neck = new THREE.Mesh(new THREE.TorusGeometry(r * 2.2, r, 10, 20, Math.PI), mat(c, { metal: 0.3 }));
    neck.position.set(L / 2 + r * 2.2, h, W / 2); g.add(neck);
    rod(g, [L / 2 + r * 4.4, h, W / 2], [L / 2 + r * 4.4, h - 0.12, W / 2], r, mat(DARK));
    return { dim: { axis: "y", from: 0, to: h, at: [L / 2 - 0.2, W / 2], label: "to opening" } };
  },
  guard_rail(g, L, H, W, p, c) {
    const alongX = L >= W, len = alongX ? L : W, h = mm(p.height, H), low = mm(p.lowest_opening, 0.23);
    const at = (s, y) => alongX ? [s, y, W / 2] : [L / 2, y, s];
    const rail = mat(c, { metal: 0.4 }), steel = mat(STEEL, { metal: 0.4 });
    const n = Math.max(1, Math.round(len / 1.5));
    for (let i = 0; i <= n; i++) rod(g, at(len * i / n, 0), at(len * i / n, h), 0.03, steel);          // stanchions about 1.5 m apart
    const courses = [low, low + (h - low) / 2, h];
    for (const y of courses) rod(g, at(0, y), at(len, y), y === h ? 0.03 : 0.015, rail);
    return { dim: { axis: "y", from: 0, to: low, at: alongX ? [0.3, W / 2 + 0.2] : [L / 2 + 0.2, 0.3], label: "lowest opening" },
             dim2: { axis: "y", from: 0, to: h, at: alongX ? [len * 0.5, W / 2 + 0.2] : [L / 2 + 0.2, len * 0.5], label: "height" } };
  },
  cabin(g, L, H, W, p, c, kind) {
    const floor = box(g, L, 0.05, W, 0, 0, 0, mat(c)); edges(g, floor, c);
    const wall = mat(GLASS, { opacity: 0.25 });
    box(g, L, H, 0.04, 0, 0, 0, wall); box(g, L, H, 0.04, 0, 0, W - 0.04, wall); box(g, 0.04, H, W, 0, 0, 0, wall); box(g, 0.04, H, W, L - 0.04, 0, 0, wall);
    if (kind === "hospital") {
      box(g, Math.min(2, L * 0.6), 0.6, 0.9, 0.2, 0.05, 0.2, mat(BED));
      box(g, 0.25, 0.02, 0.9, L / 2 - 0.12, 0.07, W / 2 - 0.45, mat(0xd64545)); box(g, 0.9, 0.02, 0.25, L / 2 - 0.45, 0.07, W / 2 - 0.12, mat(0xd64545));
    } else if (kind === "cabin") {
      const beds = Math.max(1, +p.berths || 1);
      for (let i = 0; i < beds; i++) {                                    // one bed per berth, as on the drawing
        const bl = Math.min(2.0, L - 0.2), bw = Math.min(0.9, (W - 0.3) / beds);
        box(g, bl, 0.45, bw - 0.05, 0.1, 0.05, 0.1 + i * bw, mat(WOOD));
        box(g, bl - 0.1, 0.12, bw - 0.12, 0.15, 0.5, 0.13 + i * bw, mat(BED));
      }
      if (L > 2.4 && W > 1.6) box(g, 0.5, 0.75, 1.0, L - 0.6, 0.05, W - 1.1, mat(WOOD));   // desk
    }
    return { dim: { axis: "area", at: [L / 2, H + 0.2, W / 2], label: p.floor_area ? "floor area " + p.floor_area : "" } };
  },
  hospital(g, L, H, W, p, c) { return B.cabin(g, L, H, W, p, c, "hospital"); },
  space(g, L, H, W, p, c) { return B.cabin(g, L, H, W, p, c, "space"); },
  bulkhead(g, L, H, W, p, c) {
    const plate = box(g, L, H, W, 0, 0, 0, mat(c, { metal: 0.3 })); edges(g, plate, c);
    const thin = L <= W, span = thin ? W : L, steel = mat(STEEL, { metal: 0.4 });
    for (let s = 0.7; s < span; s += 0.7) box(g, thin ? 0.12 : 0.04, H, thin ? 0.04 : 0.12, thin ? L : s, 0, thin ? s : W, steel);   // stiffeners
    return {};
  },
  double_bottom(g, L, H, W, p, c) {
    box(g, L, 0.03, W, 0, 0, 0, mat(DARK, { metal: 0.3 }));                                  // outer bottom
    const top = box(g, L, 0.03, W, 0, H - 0.03, 0, mat(c, { opacity: 0.55 })); edges(g, top, c);  // inner bottom (tank top), at the real height
    const steel = mat(STEEL, { metal: 0.3 });
    for (let x = 0; x <= L; x += 3) box(g, 0.02, H, W, x, 0, 0, steel);                      // floors every few frames
    box(g, L, H, 0.02, 0, 0, W / 2, steel);                                                   // centre girder
    return { dim: { axis: "y", from: 0, to: H, at: [-0.4, W / 2], label: "double-bottom height" } };
  },
  diesel_engine(g, L, H, W, p, c) {
    box(g, L, H * 0.6, W, 0, 0, 0, mat(c, { metal: 0.4 }));
    const n = Math.max(4, Math.round(L / 1.4));
    for (let i = 0; i < n; i++) box(g, L / n * 0.7, H * 0.3, W * 0.5, i * L / n + L / n * 0.15, H * 0.6, W * 0.25, mat(STEEL, { metal: 0.5 }));
    rod(g, [L * 0.8, H * 0.9, W / 2], [L * 0.8, H * 1.4, W / 2], Math.min(L, W) * 0.08, mat(DARK));
    return {};
  },
  low_location_lighting(g, L, H, W, p, c) {
    const m = new THREE.MeshStandardMaterial({ color: c, emissive: 0x2a5d2a, emissiveIntensity: 0.6 });
    const h = mm(p.height_above_deck, 0.25);
    box(g, L, Math.max(H, 0.03), Math.max(W, 0.03), 0, h, 0, m);
    return { dim: { axis: "y", from: 0, to: h, at: [L / 2, -0.15], label: "above deck" } };
  },
  fire_zone(g, L, H, W, p, c, st) {
    const v = box(g, L - 0.3, H - 0.3, W - 0.3, 0.15, 0.15, 0.15, mat(c, { opacity: st === "none" || st === "pass" ? 0.07 : 0.2 }));
    v.renderOrder = 2; edges(g, v, c, 0.7);
    return { dim: { axis: "x", from: 0, to: L, at: [H + 0.5, W / 2], label: "zone length" } };
  },
};

// Build one part. Returns {object, info}. IFC parts with real triangles use those instead of a shape.
export function buildPart(el, color, status, simple = false) {
  const g = new THREE.Group();
  if (!simple && el.mesh && el.mesh.v && el.mesh.f && el.mesh.f.length > 36) {  // the model's own geometry (more than a plain box)
    const v = el.mesh.v, pos = new Float32Array(v.length);
    for (let i = 0; i < v.length; i += 3) { pos[i] = v[i]; pos[i + 1] = v[i + 2]; pos[i + 2] = v[i + 1]; }  // ship (x,y,z) -> three (x,z,y)
    const geo = new THREE.BufferGeometry(); geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setIndex(el.mesh.f); geo.computeVertexNormals();
    const m = new THREE.Mesh(geo, mat(color, { side: THREE.DoubleSide, opacity: el.type === "fire_zone" ? 0.2 : undefined }));
    g.add(m); edges(g, m, el.type === "fire_zone" ? color : 0x334155, 0.6);
    return { object: g, info: { real: true } };
  }
  const [x, y, z, lx, wy, hz] = el.box;
  const L = Math.max(lx, 0.05), W = Math.max(wy, 0.05), H = Math.max(hz, 0.05);
  const build = simple ? null : B[el.type];
  let info = {};
  if (el.type === "nav_light") {                                           // lantern on a mast down to the hull
    const lan = new THREE.Mesh(new THREE.SphereGeometry(Math.max(0.6, Math.min(1.6, 0.4 + z * 0.02)), 20, 14), mat(color));
    lan.position.set(L / 2, H / 2, W / 2); g.add(lan);
    rod(g, [L / 2, -z, W / 2], [L / 2, H / 2, W / 2], 0.25, mat(STEEL, { metal: 0.4 }));
    if (!simple) rod(g, [L / 2 - 1.2, H / 2 - 1.2, W / 2], [L / 2 + 1.2, H / 2 - 1.2, W / 2], 0.08, mat(STEEL));
    info = { dim: { axis: "y", from: -(+String(el.props.height_above_hull || "").match(/[\d.]+/)?.[0] || z), to: H / 2, at: [L / 2 + 1.8, W / 2], label: "above hull" } };
  } else if (build) info = build(g, L, H, W, el.props || {}, color, status) || {};
  else { const m = box(g, L, H, W, 0, 0, 0, mat(color)); if (status === "fail") edges(g, m, color); }
  g.position.set(x, z, y);
  return { object: g, info };
}

// A text label that always faces the camera.
export function label(text, opts = {}) {
  const pad = 10, font = `600 ${opts.size || 30}px system-ui, sans-serif`;
  const cv = document.createElement("canvas"), ctx = cv.getContext("2d");
  ctx.font = font; const w = Math.ceil(ctx.measureText(text).width) + pad * 2, h = (opts.size || 30) + pad * 2;
  cv.width = w; cv.height = h; ctx.font = font;
  ctx.fillStyle = opts.bg || "rgba(22,32,42,0.88)"; ctx.beginPath(); ctx.roundRect(0, 0, w, h, 10); ctx.fill();
  ctx.fillStyle = opts.color || "#fff"; ctx.textBaseline = "middle"; ctx.fillText(text, pad, h / 2 + 1);
  const tex = new THREE.CanvasTexture(cv); tex.colorSpace = THREE.SRGBColorSpace;
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
  const s = opts.width ? opts.width / w : (opts.scale || 0.01); sp.scale.set(w * s, h * s, 1); sp.renderOrder = 10;  // width in metres
  return sp;
}

// A dimension line with end ticks between two local points, plus its label.
export function dimension(a, b, text, color = 0x16202a, width = 1) {
  const g = new THREE.Group(), va = new THREE.Vector3(...a), vb = new THREE.Vector3(...b);
  const m = new THREE.LineBasicMaterial({ color, depthTest: false });
  const dir = vb.clone().sub(va), up = Math.abs(dir.y) > Math.abs(dir.x) + Math.abs(dir.z) ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0);
  const t = up.multiplyScalar(Math.max(0.06, dir.length() * 0.06));
  const pts = [va, vb, va.clone().sub(t), va.clone().add(t), vb.clone().sub(t), vb.clone().add(t)];
  const line = new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts), m); line.renderOrder = 9; g.add(line);
  const sp = label(text, { width, bg: color === 0xa12a2a ? "rgba(161,42,42,0.92)" : undefined });
  sp.position.copy(va).add(vb).multiplyScalar(0.5).add(new THREE.Vector3(0, 0.1 + sp.scale.y * 0.8, 0)); g.add(sp);
  return g;
}
