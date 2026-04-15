#!/usr/bin/env python3
"""
Suivi de lecture Zotero — Dashboard interactif par collection.
Cliquez sur le statut d'un article pour le changer. Les changements
sont sauvegardés dans le navigateur (localStorage).
"""

import sqlite3, shutil, sys, json
from datetime import datetime
from pathlib import Path

ZOTERO_DB = Path.home() / "Zotero" / "zotero.sqlite"
TEMP_DB   = Path("/tmp/zotero_suivi_copy.sqlite")
OUTPUT    = Path(__file__).parent / "dashboard.html"

TAGS_LU       = {"lu", "read", "lue", "terminé", "termine"}
TAGS_EN_COURS = {"en cours", "reading", "en lecture", "en-cours", "in progress"}

def get_db():
    if not ZOTERO_DB.exists():
        sys.exit(f"Base Zotero introuvable : {ZOTERO_DB}")
    shutil.copy2(ZOTERO_DB, TEMP_DB)
    return sqlite3.connect(TEMP_DB)

def fetch_items(conn):
    rows = conn.execute("""
        SELECT i.itemID,
               MAX(CASE WHEN f.fieldName='title' THEN idv.value END) AS title,
               MAX(CASE WHEN f.fieldName='date'  THEN idv.value END) AS date,
               it.typeName
        FROM items i
        JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
        LEFT JOIN itemData id ON i.itemID = id.itemID
        LEFT JOIN itemDataValues idv ON id.valueID = idv.valueID
        LEFT JOIN fields f ON id.fieldID = f.fieldID
        WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
          AND it.typeName NOT IN ('annotation','attachment','note')
        GROUP BY i.itemID
        ORDER BY title
    """).fetchall()
    return [{"id": r[0], "title": r[1] or "Sans titre", "date": (r[2] or "")[:4], "type": r[3]} for r in rows]

def fetch_creators(conn):
    d = {}
    for iid, last, first in conn.execute("""
        SELECT ic.itemID, c.lastName, c.firstName
        FROM itemCreators ic JOIN creators c ON ic.creatorID = c.creatorID
        ORDER BY ic.orderIndex
    """).fetchall():
        name = (last or "") + (", " + first if first and last else first or "")
        d.setdefault(iid, []).append(name)
    return {k: "; ".join(v[:2]) + (" et al." if len(v) > 2 else "") for k, v in d.items()}

def fetch_annotations(conn):
    att_parent = dict(conn.execute(
        "SELECT itemID, parentItemID FROM itemAttachments WHERE parentItemID IS NOT NULL"
    ).fetchall())
    counts = {}
    for att_id, cnt in conn.execute(
        "SELECT parentItemID, COUNT(*) FROM itemAnnotations GROUP BY parentItemID"
    ).fetchall():
        parent = att_parent.get(att_id, att_id)
        counts[parent] = counts.get(parent, 0) + cnt
    return counts

def fetch_collections(conn):
    # Retourne {itemID: [colName, ...]} + liste ordonnée des collections
    rows = conn.execute("""
        SELECT ci.itemID, c.collectionName
        FROM collectionItems ci JOIN collections c ON ci.collectionID = c.collectionID
        ORDER BY c.collectionName
    """).fetchall()
    item_cols = {}
    all_cols = set()
    for iid, col in rows:
        item_cols.setdefault(iid, []).append(col)
        all_cols.add(col)
    return item_cols, sorted(all_cols)

def fetch_tags(conn):
    d = {}
    for iid, tag in conn.execute(
        "SELECT it.itemID, t.name FROM itemTags it JOIN tags t ON it.tagID = t.tagID"
    ).fetchall():
        d.setdefault(iid, []).append(tag)
    return d

def initial_status(tags, annotations):
    tl = {t.lower() for t in tags}
    if tl & TAGS_LU:       return "lu"
    if tl & TAGS_EN_COURS: return "en_cours"
    if annotations >= 1:   return "lu"
    return "a_lire"

def build_data(conn):
    items      = fetch_items(conn)
    creators   = fetch_creators(conn)
    annots     = fetch_annotations(conn)
    item_cols, all_cols = fetch_collections(conn)
    tags_map   = fetch_tags(conn)

    for item in items:
        iid = item["id"]
        tags = tags_map.get(iid, [])
        item["creators"]     = creators.get(iid, "—")
        item["annotations"]  = annots.get(iid, 0)
        item["collections"]  = item_cols.get(iid, [])
        item["initialStatus"] = initial_status(tags, item["annotations"])

    return items, all_cols

def render_html(items, all_cols):
    now   = datetime.now().strftime("%d/%m/%Y à %H:%M")
    total = len(items)

    # Group items by collection
    groups = {col: [] for col in all_cols}
    groups["(Sans collection)"] = []
    for item in items:
        if item["collections"]:
            for col in item["collections"]:
                groups[col].append(item)
        else:
            groups["(Sans collection)"].append(item)

    items_json = json.dumps(items, ensure_ascii=False)
    groups_order = all_cols + (["(Sans collection)"] if groups["(Sans collection)"] else [])

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Suivi de lecture Zotero</title>
<style>
:root {{
  --bg:#0f172a; --surface:#1e293b; --surface2:#263347;
  --border:#334155; --text:#e2e8f0; --muted:#94a3b8;
  --green:#22c55e; --amber:#f59e0b; --slate:#94a3b8; --blue:#38bdf8;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;min-height:100vh}}
a{{color:inherit;text-decoration:none}}

/* Layout */
.layout{{display:flex;height:100vh;overflow:hidden}}
.sidebar{{width:240px;min-width:240px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto}}
.content{{flex:1;overflow-y:auto;padding:1.5rem 2rem}}

/* Sidebar */
.sidebar-header{{padding:1.25rem 1rem .75rem;border-bottom:1px solid var(--border)}}
.sidebar-header h1{{font-size:1rem;font-weight:700}}
.sidebar-header .sub{{color:var(--muted);font-size:.75rem;margin-top:.2rem}}
.nav-item{{padding:.55rem 1rem;cursor:pointer;border-left:3px solid transparent;transition:all .15s;font-size:.85rem;color:var(--muted)}}
.nav-item:hover{{background:var(--surface2);color:var(--text)}}
.nav-item.active{{border-left-color:var(--blue);color:var(--blue);background:var(--surface2)}}
.nav-item .count{{float:right;font-size:.75rem;opacity:.7}}
.nav-section{{padding:.5rem 1rem .2rem;font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);opacity:.6;margin-top:.5rem}}

/* Stats mini */
.mini-stats{{padding:.75rem 1rem;border-top:1px solid var(--border);margin-top:auto}}
.mini-stat{{display:flex;justify-content:space-between;align-items:center;padding:.25rem 0;font-size:.8rem}}
.mini-stat .label{{color:var(--muted)}}
.dot{{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:.4rem}}

/* Content header */
.col-header{{margin-bottom:1.25rem}}
.col-header h2{{font-size:1.2rem;font-weight:700;margin-bottom:.4rem}}
.progress-row{{display:flex;align-items:center;gap:.75rem}}
.bar-wrap{{flex:1;background:var(--border);border-radius:9999px;height:6px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:9999px;transition:width .4s}}
.prog-label{{font-size:.8rem;color:var(--muted);white-space:nowrap}}

/* Search */
.search-wrap{{margin-bottom:1rem}}
.search{{width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:.5rem;padding:.45rem .8rem;font-size:.85rem;outline:none}}
.search:focus{{border-color:var(--blue)}}

/* Item cards */
.item-card{{background:var(--surface);border:1px solid var(--border);border-radius:.6rem;padding:.85rem 1rem;margin-bottom:.6rem;display:flex;gap:.85rem;align-items:flex-start;transition:border-color .15s}}
.item-card:hover{{border-color:var(--blue)}}
.item-left{{flex:1;min-width:0}}
.item-title{{font-weight:600;font-size:.9rem;margin-bottom:.2rem;line-height:1.35}}
.item-meta{{font-size:.78rem;color:var(--muted)}}
.item-annot{{display:inline-block;background:#1e3a5f;color:var(--blue);border-radius:.2rem;padding:.05rem .35rem;font-size:.72rem;font-weight:600;margin-left:.4rem}}
.item-right{{flex-shrink:0;padding-top:.1rem}}

/* Status button — cliquable */
.status-btn{{border:none;cursor:pointer;border-radius:9999px;padding:.28rem .75rem;font-size:.75rem;font-weight:700;transition:all .15s;white-space:nowrap}}
.status-btn:hover{{opacity:.85;transform:scale(1.04)}}
.status-btn[data-s="lu"]      {{background:#22c55e20;color:#22c55e;border:1px solid #22c55e50}}
.status-btn[data-s="en_cours"]{{background:#f59e0b20;color:#f59e0b;border:1px solid #f59e0b50}}
.status-btn[data-s="a_lire"]  {{background:#94a3b820;color:#94a3b8;border:1px solid #94a3b850}}

/* Vue globale */
.global-cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.75rem;margin-bottom:1.5rem}}
.gcard{{background:var(--surface);border:1px solid var(--border);border-radius:.6rem;padding:1rem}}
.gcard .num{{font-size:1.8rem;font-weight:700}}
.gcard .lbl{{font-size:.75rem;color:var(--muted);margin-top:.2rem}}
.col-row{{background:var(--surface);border:1px solid var(--border);border-radius:.6rem;padding:.85rem 1rem;margin-bottom:.6rem;display:flex;align-items:center;gap:1rem;cursor:pointer;transition:border-color .15s}}
.col-row:hover{{border-color:var(--blue)}}
.col-name{{font-weight:600;flex:1}}
.col-nums{{display:flex;gap:.75rem;font-size:.8rem}}
.col-num span{{color:var(--muted)}}

/* Empty */
.empty{{color:var(--muted);font-size:.9rem;padding:1rem 0;text-align:center}}
</style>
</head>
<body>
<div class="layout">

<!-- Barre latérale -->
<nav class="sidebar">
  <div class="sidebar-header">
    <h1>📚 Suivi Zotero</h1>
    <div class="sub">Mis à jour le {now}</div>
  </div>

  <div class="nav-section">Vue</div>
  <div class="nav-item active" onclick="showGlobal(this)">Vue globale <span class="count">{total}</span></div>

  <div class="nav-section">Collections</div>
  {"".join(f'<div class="nav-item" data-col-idx="{i}" onclick="showCollection(this, {json.dumps(col)})">{col} <span class="count" id="cnt-{i}">…</span></div>' for i, col in enumerate(groups_order))}

  <div class="mini-stats" id="mini-stats"></div>
</nav>

<!-- Contenu principal -->
<main class="content" id="content"></main>
</div>

<script>
const ALL_ITEMS = {items_json};
const GROUPS_ORDER = {json.dumps(groups_order, ensure_ascii=False)};

// ── localStorage pour stocker les statuts modifiés par l'utilisateur
const LS_KEY = "zotero_statuts";
function loadOverrides() {{
  try {{ return JSON.parse(localStorage.getItem(LS_KEY) || "{{}}"); }}
  catch {{ return {{}}; }}
}}
function saveOverride(id, status) {{
  const d = loadOverrides();
  d[id] = status;
  localStorage.setItem(LS_KEY, JSON.stringify(d));
}}

function getStatus(item) {{
  const overrides = loadOverrides();
  return overrides[item.id] !== undefined ? overrides[item.id] : item.initialStatus;
}}

const STATUS_CYCLE = ["a_lire", "en_cours", "lu"];
const STATUS_LABEL = {{ lu: "✅ Lu", en_cours: "📖 En cours", a_lire: "⬜ À lire" }};

function cycleStatus(id, btn) {{
  const cur = btn.dataset.s;
  const next = STATUS_CYCLE[(STATUS_CYCLE.indexOf(cur) + 1) % STATUS_CYCLE.length];
  saveOverride(id, next);
  btn.dataset.s = next;
  btn.textContent = STATUS_LABEL[next];
  updateSidebar();
  // Si on est sur la vue globale, mettre à jour les compteurs de collection
  if (document.getElementById("view-global")) updateGlobalColCounts();
}}

function pct(n, t) {{ return t ? Math.round(100 * n / t) : 0; }}

function colStats(items) {{
  const s = {{ lu: 0, en_cours: 0, a_lire: 0 }};
  items.forEach(it => s[getStatus(it)]++);
  return s;
}}

function globalStats() {{
  return colStats(ALL_ITEMS);
}}

// ── Items groupés par collection (recalculé à chaque appel car statuts peuvent changer)
function getGroups() {{
  const g = {{}};
  GROUPS_ORDER.forEach(col => g[col] = []);
  ALL_ITEMS.forEach(item => {{
    if (item.collections.length) {{
      item.collections.forEach(col => {{ if (g[col]) g[col].push(item); }});
    }} else {{
      if (g["(Sans collection)"]) g["(Sans collection)"].push(item);
    }}
  }});
  return g;
}}

// ── Carte d'un item
function renderCard(item) {{
  const s = getStatus(item);
  const annotBadge = item.annotations > 0
    ? `<span class="item-annot">${{item.annotations}} annot.</span>` : "";
  return `<div class="item-card">
    <div class="item-left">
      <div class="item-title">${{escHtml(item.title)}}</div>
      <div class="item-meta">${{item.creators}} ${{item.date ? "· " + item.date : ""}} · ${{item.type}}${{annotBadge}}</div>
    </div>
    <div class="item-right">
      <button class="status-btn" data-s="${{s}}" onclick="cycleStatus(${{item.id}}, this)">${{STATUS_LABEL[s]}}</button>
    </div>
  </div>`;
}}

function escHtml(s) {{
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}}

// ── Navigation vers une collection par index (depuis la vue globale)
function navigateToCollection(idx) {{
  const navItems = document.querySelectorAll(".nav-item[data-col-idx]");
  const navEl = document.querySelector(`.nav-item[data-col-idx="${{idx}}"]`);
  showCollection(navEl, GROUPS_ORDER[idx]);
}}

// ── Vue d'une collection
function showCollection(navEl, colName) {{
  setActive(navEl);
  const groups = getGroups();
  const items  = groups[colName] || [];
  const s = colStats(items);
  const tot = items.length;
  const luPct = pct(s.lu, tot);

  let html = `<div class="col-header">
    <h2>${{escHtml(colName)}}</h2>
    <div class="progress-row">
      <div class="bar-wrap"><div class="bar-fill" style="width:${{luPct}}%;background:var(--green)"></div></div>
      <div class="prog-label">${{s.lu}}/${{tot}} lus (${{luPct}}%)</div>
    </div>
    <div style="font-size:.8rem;color:var(--muted);margin-top:.35rem">
      ✅ ${{s.lu}} lu &nbsp;·&nbsp; 📖 ${{s.en_cours}} en cours &nbsp;·&nbsp; ⬜ ${{s.a_lire}} à lire
    </div>
  </div>
  <div class="search-wrap">
    <input class="search" id="search" placeholder="Rechercher…" oninput="filterCards(this.value)">
  </div>
  <div id="cards-list">
    ${{items.map(renderCard).join("") || '<div class="empty">Aucun article dans cette collection.</div>'}}
  </div>`;

  document.getElementById("content").innerHTML = html;
  window._currentItems = items;
}}

function filterCards(q) {{
  const ql = q.toLowerCase();
  document.querySelectorAll(".item-card").forEach((card, i) => {{
    const item = window._currentItems[i];
    const match = !q || (item.title+item.creators).toLowerCase().includes(ql);
    card.style.display = match ? "" : "none";
  }});
}}

// ── Vue globale
function showGlobal(navEl) {{
  setActive(navEl);
  const s  = globalStats();
  const tot = ALL_ITEMS.length;
  const groups = getGroups();

  const colCards = GROUPS_ORDER.map((col, idx) => {{
    const cs = colStats(groups[col]);
    const ct = groups[col].length;
    const p = pct(cs.lu, ct);
    return `<div class="col-row" onclick="navigateToCollection(${{idx}})">
      <div class="col-name">${{escHtml(col)}}</div>
      <div style="flex:1;max-width:120px">
        <div class="bar-wrap"><div class="bar-fill" style="width:${{p}}%;background:var(--green)"></div></div>
      </div>
      <div class="col-nums">
        <div><span style="color:var(--green)">${{cs.lu}}</span>/${{ct}} &nbsp;→</div>
      </div>
    </div>`;
  }}).join("");

  document.getElementById("content").innerHTML = `<div id="view-global">
    <div class="global-cards">
      <div class="gcard"><div class="num">${{tot}}</div><div class="lbl">📚 Total</div></div>
      <div class="gcard"><div class="num" style="color:var(--green)" id="g-lu">${{s.lu}}</div><div class="lbl">✅ Lus (${{pct(s.lu,tot)}}%)</div></div>
      <div class="gcard"><div class="num" style="color:var(--amber)" id="g-ec">${{s.en_cours}}</div><div class="lbl">📖 En cours</div></div>
      <div class="gcard"><div class="num" style="color:var(--slate)" id="g-al">${{s.a_lire}}</div><div class="lbl">⬜ À lire</div></div>
    </div>
    ${{colCards}}
  </div>`;
}}

function updateGlobalColCounts() {{
  const s = globalStats();
  const tot = ALL_ITEMS.length;
  const el = (id) => document.getElementById(id);
  if (el("g-lu"))  el("g-lu").textContent = s.lu;
  if (el("g-ec"))  el("g-ec").textContent = s.en_cours;
  if (el("g-al"))  el("g-al").textContent = s.a_lire;
}}

function updateSidebar() {{
  const groups = getGroups();
  GROUPS_ORDER.forEach((col, i) => {{
    const el = document.getElementById("cnt-" + i);
    if (el) el.textContent = groups[col].length;
  }});
  const s = globalStats();
  const tot = ALL_ITEMS.length;
  document.getElementById("mini-stats").innerHTML = `
    <div class="mini-stat"><span><span class="dot" style="background:var(--green)"></span>Lu</span><span>${{s.lu}} (${{pct(s.lu,tot)}}%)</span></div>
    <div class="mini-stat"><span><span class="dot" style="background:var(--amber)"></span>En cours</span><span>${{s.en_cours}}</span></div>
    <div class="mini-stat"><span><span class="dot" style="background:var(--slate)"></span>À lire</span><span>${{s.a_lire}}</span></div>
  `;
}}

function setActive(el) {{
  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
  el.classList.add("active");
}}

// Init
updateSidebar();
showGlobal(document.querySelector(".nav-item.active"));
</script>
</body>
</html>"""

def main():
    print("Connexion à la base Zotero...")
    conn = get_db()
    print("Chargement des données...")
    items, all_cols = build_data(conn)
    conn.close()
    print(f"  {len(items)} références · {len(all_cols)} collections")
    html = render_html(items, all_cols)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"  Dashboard : {OUTPUT}")
    print(f"  → open '{OUTPUT}'")

if __name__ == "__main__":
    main()
