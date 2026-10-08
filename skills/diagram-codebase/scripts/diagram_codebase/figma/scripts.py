"""`use_figma` JavaScript generators (FigJam only).

Rules from the official figma-use / figma-use-figjam skills that every script obeys:
plain JS with top-level `await` and `return` (no IIFE, no closePlugin), never
`figma.notify` or `figma.createPage`, fonts loaded before any text mutation or
reparenting of text-bearing nodes, palette colors as hex/255, section-local
coordinates after `appendChild`, and every created/mutated node id returned.

All dynamic values (ids, titles, hashes) enter the script through one
`const P = <json>;` literal, so ids are always JSON string literals and no user
text is ever spliced into code. Scripts are idempotent where it matters:
`place_section` returns an existing section already tagged with the same diagram,
run and content hash; `legend_and_index` replaces the previous legend.
"""

from __future__ import annotations

import json
from typing import Any

NAMESPACE = "diagramcodebase"
SKILL_NAMES = "figma-use,figma-use-figjam"
PAD = 48  # >= 32 px padding required by create-section guidance
TITLE_SPACE = 88  # H2 (40 px) title line plus breathing room

# Section background palette (figjam-colors), varied by row.
SECTION_FILLS = ["F5FBFF", "F8F5FF", "EBFFEE", "FFF7F0", "F1FEFD", "FFFBF0", "FFF0FA", "F9F9F9"]

# Legend sticky colors per DESIGN.md category (sticky palette from figjam-colors).
CATEGORY_STICKY = {
    "app": ("A8DAFF", "App code"),
    "data": ("B3EFBD", "Data stores"),
    "processing": ("D3BDFF", "Processing / algorithms"),
    "external": ("FFD3A8", "External services"),
    "infra": ("E6E6E6", "Infrastructure"),
    "error": ("FFB8A8", "Errors / failure paths"),
    "messaging": ("B3F4EF", "Messaging / events"),
    "context": ("FFFFFF", "Context (outside focus)"),
}

_PRELUDE = """\
const NS = "diagramcodebase";
const h = (r, g, b) => ({ r: r / 255, g: g / 255, b: b / 255 });
const hex = (s) => h(parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16));
const CHARCOAL = h(0x1e, 0x1e, 0x1e);
const FONT = { family: "Inter", style: "Medium" };
const page = figma.currentPage;
const errors = [];
const tag = (n, k) => { try { return n.getSharedPluginData(NS, k) || ""; } catch (e) { return ""; } };
const ours = (n) => n.type === "SECTION" && (tag(n, "kind") !== "" || tag(n, "diagram_id") !== "");
const box = (n) => ({ x: Math.round(n.x), y: Math.round(n.y), width: Math.round(n.width), height: Math.round(n.height) });
function textsIn(root, limit) {
  const out = [];
  const visit = (n) => {
    if (out.length >= limit) return;
    let s = "";
    try {
      if (n.type === "TEXT") s = n.characters;
      else if ("text" in n && n.text && typeof n.text.characters === "string") s = n.text.characters;
    } catch (e) { s = ""; }
    if (s && s.trim()) out.push(s.trim().slice(0, 60));
  };
  visit(root);
  if ("findAll" in root) for (const c of root.findAll(() => true)) visit(c);
  return out;
}
async function loadFontsIn(nodes) {
  const fonts = new Map();
  const add = (f) => { if (f && typeof f === "object" && f.family) fonts.set(f.family + "|" + f.style, f); };
  const visit = (n) => {
    try {
      const t = n.type === "TEXT" ? n : ("text" in n && n.text ? n.text : null);
      if (!t) return;
      if (t.characters.length) for (const f of t.getRangeAllFontNames(0, t.characters.length)) add(f);
      else add(t.fontName);
    } catch (e) { /* nodes without readable text are skipped */ }
  };
  for (const n of nodes) {
    visit(n);
    if ("findAll" in n) for (const c of n.findAll(() => true)) visit(c);
  }
  await Promise.all([...fonts.values()].map((f) => figma.loadFontAsync(f).catch(() => null)));
  return fonts.size;
}
async function makeText(chars, size, width) {
  await figma.loadFontAsync(FONT);
  const t = figma.createText();
  t.fontName = FONT;
  t.characters = chars;
  t.fontSize = size;
  t.fills = [{ type: "SOLID", color: CHARCOAL }];
  if (width) { t.resize(width, t.height); t.textAutoResize = "HEIGHT"; }
  return t;
}
function fitSection(section, pad) {
  let right = 0, bottom = 0;
  for (const c of section.children) {
    right = Math.max(right, c.x + c.width);
    bottom = Math.max(bottom, c.y + c.height);
  }
  section.resize(Math.max(right + pad, 200), Math.max(bottom + pad, 120));
}
"""

_PLACE_BODY = """\
for (const n of page.children) {
  if (ours(n) && tag(n, "diagram_id") === P.diagramId && tag(n, "content_hash") === P.contentHash && tag(n, "run_id") === P.runId) {
    return { sectionId: n.id, reused: true, alreadyPlaced: true, createdNodeIds: [], movedNodeIds: [], removedSectionIds: [],
      foreignTopLevelIds: [], texts: textsIn(n, 80), bounds: box(n), errors };
  }
}
const ignore = new Set(P.ignoreIds);
const old = P.replaceSectionId ? await figma.getNodeByIdAsync(P.replaceSectionId) : null;
const content = page.children.filter((n) => !ours(n) && !ignore.has(n.id) && n !== old);
if (content.length === 0) {
  return { sectionId: null, noContent: true, createdNodeIds: [], movedNodeIds: [], removedSectionIds: [],
    foreignTopLevelIds: page.children.filter((n) => !ours(n)).map((n) => n.id), texts: [], bounds: null, errors };
}
const mine = page.children.filter((n) => ours(n) && tag(n, "kind") !== "legend" && n !== old);
let tx = 0, ty = 0;
if (old && old.type === "SECTION") { tx = old.x; ty = old.y; }
else {
  const sameRow = mine.filter((n) => tag(n, "row") === String(P.row));
  if (sameRow.length) {
    tx = Math.max(...sameRow.map((n) => n.x + n.width)) + P.gap;
    ty = Math.min(...sameRow.map((n) => n.y));
  } else if (mine.length) {
    tx = Math.min(...mine.map((n) => n.x));
    ty = Math.max(...mine.map((n) => n.y + n.height)) + P.gap;
  }
}
await loadFontsIn(content);
const created = [], moved = [];
let section, reused = false;
if (content.length === 1 && content[0].type === "SECTION") {
  section = content[0];
  reused = true;
} else {
  const x0 = Math.min(...content.map((n) => n.x)), y0 = Math.min(...content.map((n) => n.y));
  section = figma.createSection();
  created.push(section.id);
  section.x = x0;
  section.y = y0;
  for (const n of content) {
    const ax = n.x, ay = n.y;
    try { section.appendChild(n); moved.push(n.id); }
    catch (e) { errors.push("append " + n.id + ": " + String(e)); continue; }
    try { n.x = ax - x0; n.y = ay - y0; } catch (e) { /* attached connectors follow their endpoints */ }
  }
}
const kids = section.children.slice();
if (kids.length) {
  const mx = Math.min(...kids.map((c) => c.x)), my = Math.min(...kids.map((c) => c.y));
  for (const c of kids) {
    try { c.x = c.x - mx + P.pad; c.y = c.y - my + P.pad + P.titleSpace; } catch (e) { /* connector */ }
  }
}
const title = await makeText(P.title, 40, 0);
section.appendChild(title);
title.x = P.pad;
title.y = P.pad;
title.setSharedPluginData(NS, "role", "title");
created.push(title.id);
section.name = P.title;
section.fills = [{ type: "SOLID", color: hex(P.fill) }];
fitSection(section, P.pad);
section.x = tx;
section.y = ty;
const tags = { kind: "diagram", diagram_id: P.diagramId, run_id: P.runId, content_hash: P.contentHash, row: String(P.row) };
for (const k of Object.keys(tags)) section.setSharedPluginData(NS, k, tags[k]);
const removed = [];
if (old && old.type === "SECTION" && old.id !== section.id) { removed.push(old.id); old.remove(); }
return { sectionId: section.id, reused, alreadyPlaced: false, createdNodeIds: created, movedNodeIds: moved,
  removedSectionIds: removed, foreignTopLevelIds: page.children.filter((n) => !ours(n)).map((n) => n.id),
  texts: textsIn(section, 80), bounds: box(section), errors };
"""

_LEGEND_BODY = """\
const removed = [];
for (const n of page.children.slice()) {
  if (ours(n) && tag(n, "kind") === "legend") { removed.push(n.id); n.remove(); }
}
const created = [];
const section = figma.createSection();
created.push(section.id);
section.name = "Legend and Index";
section.fills = [{ type: "SOLID", color: hex("F9F9F9") }];
let y = P.pad;
const place = (node) => { section.appendChild(node); node.x = P.pad; node.y = y; y += node.height + 32; created.push(node.id); };
place(await makeText("Legend and Index", 40, 0));
place(await makeText(P.meta, 16, 720));
const probe = figma.createSticky();
await figma.loadFontAsync(probe.text.fontName);
probe.remove();
const stickies = [];
for (const c of P.categories) {
  const s = figma.createSticky();
  s.text.characters = c.label;
  s.fills = [{ type: "SOLID", color: hex(c.color) }];
  section.appendChild(s);
  stickies.push(s);
  created.push(s.id);
}
const cols = 4, gap = 64;
for (let r = 0; r * cols < stickies.length; r++) {
  const row = stickies.slice(r * cols, r * cols + cols);
  let x = P.pad;
  for (const s of row) { s.x = x; s.y = y; x += s.width + gap; }
  y += Math.max(...row.map((s) => s.height)) + gap;
}
place(await makeText("Diagrams", 24, 0));
if (P.index) place(await makeText(P.index, 16, 720));
fitSection(section, P.pad);
const mine = page.children.filter((n) => ours(n) && tag(n, "kind") === "diagram");
const row0 = mine.filter((n) => tag(n, "row") === "0");
const anchorY = (row0.length ? row0 : mine).map((n) => n.y);
section.x = mine.length ? Math.min(...mine.map((n) => n.x)) - P.gap - section.width : 0;
section.y = anchorY.length ? Math.min(...anchorY) : 0;
section.setSharedPluginData(NS, "kind", "legend");
section.setSharedPluginData(NS, "run_id", P.runId);
return { sectionId: section.id, createdNodeIds: created, removedSectionIds: removed, bounds: box(section), errors };
"""

_DELETE_BODY = """\
const deleted = [], missing = [], skipped = [];
for (const id of P.ids) {
  const n = await figma.getNodeByIdAsync(id);
  if (!n) missing.push(id);
  else if (n.type !== "SECTION") skipped.push(id);
  else { n.remove(); deleted.push(id); }
}
return { deletedIds: deleted, missingIds: missing, skippedIds: skipped };
"""


def _params(values: dict[str, Any]) -> str:
    # ensure_ascii keeps U+2028/2029 and any non-ASCII out of the source text.
    return "const P = " + json.dumps(values, ensure_ascii=True, sort_keys=True) + ";\n"


def place_section(
    *,
    diagram_id: str,
    title: str,
    run_id: str,
    content_hash: str,
    row: int,
    ignore_ids: list[str] | None = None,
    replace_section_id: str | None = None,
    gap: int = 200,
) -> str:
    """Wrap the newest generated diagram in a tagged, titled section on the row grid."""
    params = {
        "diagramId": diagram_id,
        "title": title,
        "runId": run_id,
        "contentHash": content_hash or "",
        "row": int(row),
        "ignoreIds": sorted(ignore_ids or []),
        "replaceSectionId": replace_section_id,
        "gap": int(gap),
        "pad": PAD,
        "titleSpace": TITLE_SPACE,
        "fill": SECTION_FILLS[int(row) % len(SECTION_FILLS)],
    }
    return _params(params) + _PRELUDE + _PLACE_BODY


def legend_and_index(
    *,
    run_id: str,
    categories: list[str],
    diagrams: list[dict[str, Any]],
    repo_name: str,
    revision: str | None,
    date: str,
    gap: int = 200,
    note: str = "Evidence: see local .diagram-codebase/model.json",
) -> str:
    """Legend stickies (one per category present) plus a diagram index, left of row 0."""
    cats = [
        {"key": c, "label": CATEGORY_STICKY[c][1], "color": CATEGORY_STICKY[c][0]}
        for c in sorted(set(categories))
        if c in CATEGORY_STICKY
    ]
    rev = (revision or "unknown revision")[:10]
    lines = [
        f"{i}. {d.get('title') or d.get('id')} ({d.get('type') or 'diagram'})"
        for i, d in enumerate(diagrams, 1)
    ]
    params = {
        "runId": run_id,
        "meta": f"{repo_name} @ {rev}, generated {date}\n{note}",
        "categories": cats,
        "index": "\n".join(lines),
        "gap": int(gap),
        "pad": PAD,
    }
    return _params(params) + _PRELUDE + _LEGEND_BODY


def delete_sections(ids: list[str]) -> str:
    """Remove the given sections (and their contents); missing ids are reported, not errors."""
    return _params({"ids": sorted(set(ids))}) + _DELETE_BODY


def use_figma_params(file_key: str, code: str, description: str) -> dict[str, Any]:
    return {
        "fileKey": file_key,
        "code": code,
        "description": description,
        "skillNames": SKILL_NAMES,
    }
