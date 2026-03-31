#!/usr/bin/env python3
"""
Serveur de suivi Zotero — http://localhost:7777
Surveille zotero.sqlite en continu et pousse les mises à jour au navigateur.
"""

import http.server, threading, json, time, shutil, sqlite3, queue, sys, tempfile
from datetime import datetime
from pathlib import Path

ZOTERO_DB = Path.home() / "Zotero" / "zotero.sqlite"
TEMP_DB   = Path(tempfile.gettempdir()) / "zotero_live.sqlite"
PORT      = 7777

TAGS_LU       = {"lu", "read", "lue", "terminé", "termine"}
TAGS_EN_COURS = {"en cours", "reading", "en lecture", "en-cours", "in progress"}

# ── État partagé ─────────────────────────────────────────────────────────────
_lock        = threading.Lock()
_cache       = {"items": [], "collections": [], "updated": ""}
_subscribers = []   # files d'attente SSE

def notify_all():
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait("update")
        except Exception:
            dead.append(q)
    for q in dead:
        try: _subscribers.remove(q)
        except ValueError: pass

# ── Lecture Zotero ────────────────────────────────────────────────────────────
def load_data():
    if not ZOTERO_DB.exists():
        return
    try:
        shutil.copy2(ZOTERO_DB, TEMP_DB)
    except Exception:
        return

    conn = sqlite3.connect(TEMP_DB)
    try:
        items = _fetch_items(conn)
        _enrich(conn, items)
        cols = sorted({c for it in items for c in it["collections"]})
        with _lock:
            _cache["items"]      = items
            _cache["collections"] = cols
            _cache["updated"]    = datetime.now().strftime("%d/%m/%Y à %H:%M:%S")
    finally:
        conn.close()

def _fetch_items(conn):
    rows = conn.execute("""
        SELECT i.itemID,
               MAX(CASE WHEN f.fieldName='title' THEN idv.value END),
               MAX(CASE WHEN f.fieldName='date'  THEN idv.value END),
               it.typeName
        FROM items i
        JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
        LEFT JOIN itemData id ON i.itemID = id.itemID
        LEFT JOIN itemDataValues idv ON id.valueID = idv.valueID
        LEFT JOIN fields f ON id.fieldID = f.fieldID
        WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
          AND it.typeName NOT IN ('annotation','attachment','note')
        GROUP BY i.itemID ORDER BY 2
    """).fetchall()
    return [{"id": r[0], "title": r[1] or "Sans titre",
             "date": (r[2] or "")[:4], "type": r[3]} for r in rows]

def _enrich(conn, items):
    # Créateurs
    creators = {}
    for iid, last, first in conn.execute("""
        SELECT ic.itemID, c.lastName, c.firstName
        FROM itemCreators ic JOIN creators c ON ic.creatorID = c.creatorID
        ORDER BY ic.orderIndex
    """).fetchall():
        name = (last or "") + (", " + first if first and last else first or "")
        creators.setdefault(iid, []).append(name)

    # Annotations
    att_parent = dict(conn.execute(
        "SELECT itemID, parentItemID FROM itemAttachments WHERE parentItemID IS NOT NULL"
    ).fetchall())
    annots = {}
    for att_id, cnt in conn.execute(
        "SELECT parentItemID, COUNT(*) FROM itemAnnotations GROUP BY parentItemID"
    ).fetchall():
        p = att_parent.get(att_id, att_id)
        annots[p] = annots.get(p, 0) + cnt

    # Collections
    item_cols = {}
    for iid, col in conn.execute("""
        SELECT ci.itemID, c.collectionName
        FROM collectionItems ci JOIN collections c ON ci.collectionID = c.collectionID
    """).fetchall():
        item_cols.setdefault(iid, []).append(col)

    # Tags
    tags_map = {}
    for iid, tag in conn.execute(
        "SELECT it.itemID, t.name FROM itemTags it JOIN tags t ON it.tagID = t.tagID"
    ).fetchall():
        tags_map.setdefault(iid, []).append(tag)

    for item in items:
        iid  = item["id"]
        tags = tags_map.get(iid, [])
        tl   = {t.lower() for t in tags}
        item["creators"]     = _fmt_creators(creators.get(iid, []))
        item["annotations"]  = annots.get(iid, 0)
        item["collections"]  = item_cols.get(iid, [])
        if tl & TAGS_LU:            item["initialStatus"] = "lu"
        elif tl & TAGS_EN_COURS:    item["initialStatus"] = "en_cours"
        elif item["annotations"] > 0: item["initialStatus"] = "lu"
        else:                        item["initialStatus"] = "a_lire"

def _fmt_creators(lst):
    if not lst: return "—"
    if len(lst) <= 2: return "; ".join(lst)
    return lst[0] + " et al."

# ── Surveillance fichier ──────────────────────────────────────────────────────
def watch_loop():
    last_mtime = 0
    print(f"  Surveillance de {ZOTERO_DB}")
    while True:
        try:
            mtime = ZOTERO_DB.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                load_data()
                with _lock:
                    n = len(_cache["items"])
                    ts = _cache["updated"]
                print(f"  [{ts}] {n} références chargées")
                notify_all()
        except Exception as e:
            print(f"  Erreur : {e}")
        time.sleep(3)

# ── Serveur HTTP ──────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence les logs de requêtes

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/data":
            self._serve_data()
        elif self.path == "/events":
            self._serve_sse()
        else:
            self.send_error(404)

    def _serve_data(self):
        with _lock:
            payload = json.dumps({
                "items": _cache["items"],
                "collections": _cache["collections"],
                "updated": _cache["updated"]
            }, ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload.encode())

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = queue.Queue()
        _subscribers.append(q)
        try:
            # Envoie un ping initial
            self.wfile.write(b"data: connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=20)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    # keepalive
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try: _subscribers.remove(q)
            except ValueError: pass

    def _serve_html(self):
        html = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(html))
        self.end_headers()
        self.wfile.write(html)

# ── Page HTML ─────────────────────────────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Suivi Zotero</title>
<style>
:root {
  --bg:#0f172a; --surface:#1e293b; --surface2:#263347;
  --border:#334155; --text:#e2e8f0; --muted:#94a3b8;
  --green:#22c55e; --amber:#f59e0b; --slate:#94a3b8; --blue:#38bdf8;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}
.layout{display:flex;height:100vh;overflow:hidden}
.sidebar{width:240px;min-width:240px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto}
.content{flex:1;overflow-y:auto;padding:1.5rem 2rem}
.sidebar-header{padding:1.25rem 1rem .75rem;border-bottom:1px solid var(--border)}
.sidebar-header h1{font-size:1rem;font-weight:700}
.sidebar-header .sub{color:var(--muted);font-size:.75rem;margin-top:.2rem}
.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#94a3b8;margin-right:.35rem;transition:background .3s}
.live-dot.on{background:#22c55e;box-shadow:0 0 4px #22c55e}
.nav-item{padding:.55rem 1rem;cursor:pointer;border-left:3px solid transparent;transition:all .15s;font-size:.85rem;color:var(--muted)}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{border-left-color:var(--blue);color:var(--blue);background:var(--surface2)}
.nav-item .count{float:right;font-size:.75rem;opacity:.7}
.nav-section{padding:.5rem 1rem .2rem;font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);opacity:.6;margin-top:.5rem}
.mini-stats{padding:.75rem 1rem;border-top:1px solid var(--border);margin-top:auto}
.mini-stat{display:flex;justify-content:space-between;align-items:center;padding:.25rem 0;font-size:.8rem}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:.4rem}
.col-header{margin-bottom:1.25rem}
.col-header h2{font-size:1.2rem;font-weight:700;margin-bottom:.4rem}
.progress-row{display:flex;align-items:center;gap:.75rem}
.bar-wrap{flex:1;background:var(--border);border-radius:9999px;height:6px;overflow:hidden}
.bar-fill{height:100%;border-radius:9999px;transition:width .5s}
.prog-label{font-size:.8rem;color:var(--muted);white-space:nowrap}
.search-wrap{margin-bottom:1rem}
.search{width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:.5rem;padding:.45rem .8rem;font-size:.85rem;outline:none}
.search:focus{border-color:var(--blue)}
.item-card{background:var(--surface);border:1px solid var(--border);border-radius:.6rem;padding:.85rem 1rem;margin-bottom:.6rem;display:flex;gap:.85rem;align-items:flex-start;transition:border-color .15s}
.item-card:hover{border-color:var(--blue)}
.item-card.new-item{animation:flash .8s ease-out}
@keyframes flash{0%{border-color:var(--blue);background:#1e3a5f}100%{border-color:var(--border);background:var(--surface)}}
.item-left{flex:1;min-width:0}
.item-title{font-weight:600;font-size:.9rem;margin-bottom:.2rem;line-height:1.35}
.item-meta{font-size:.78rem;color:var(--muted)}
.item-annot{display:inline-block;background:#1e3a5f;color:var(--blue);border-radius:.2rem;padding:.05rem .35rem;font-size:.72rem;font-weight:600;margin-left:.4rem}
.item-right{flex-shrink:0;padding-top:.1rem}
.status-btn{border:none;cursor:pointer;border-radius:9999px;padding:.28rem .75rem;font-size:.75rem;font-weight:700;transition:all .15s;white-space:nowrap}
.status-btn:hover{opacity:.85;transform:scale(1.04)}
.status-btn[data-s="lu"]      {background:#22c55e20;color:#22c55e;border:1px solid #22c55e50}
.status-btn[data-s="en_cours"]{background:#f59e0b20;color:#f59e0b;border:1px solid #f59e0b50}
.status-btn[data-s="a_lire"]  {background:#94a3b820;color:#94a3b8;border:1px solid #94a3b850}
.global-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.75rem;margin-bottom:1.5rem}
.gcard{background:var(--surface);border:1px solid var(--border);border-radius:.6rem;padding:1rem}
.gcard .num{font-size:1.8rem;font-weight:700;transition:all .3s}
.gcard .lbl{font-size:.75rem;color:var(--muted);margin-top:.2rem}
.col-row{background:var(--surface);border:1px solid var(--border);border-radius:.6rem;padding:.85rem 1rem;margin-bottom:.6rem;display:flex;align-items:center;gap:1rem;cursor:pointer;transition:border-color .15s}
.col-row:hover{border-color:var(--blue)}
.col-name{font-weight:600;flex:1}
.empty{color:var(--muted);font-size:.9rem;padding:1rem 0;text-align:center}
.toast{position:fixed;bottom:1.5rem;right:1.5rem;background:#1e3a5f;color:var(--blue);border:1px solid var(--blue);border-radius:.5rem;padding:.6rem 1rem;font-size:.82rem;opacity:0;transition:opacity .3s;pointer-events:none;z-index:99}
.toast.show{opacity:1}
</style>
</head>
<body>
<div class="layout">
<nav class="sidebar">
  <div class="sidebar-header">
    <h1>📚 Suivi Zotero</h1>
    <div class="sub"><span class="live-dot" id="live-dot"></span><span id="live-label">Connexion…</span></div>
  </div>
  <div class="nav-section">Vue</div>
  <div class="nav-item active" id="nav-global" onclick="showGlobal()">Vue globale <span class="count" id="cnt-global">—</span></div>
  <div class="nav-section">Collections</div>
  <div id="nav-collections"></div>
  <div class="mini-stats" id="mini-stats"></div>
</nav>
<main class="content" id="content"><div class="empty" style="margin-top:3rem">Chargement…</div></main>
</div>
<div class="toast" id="toast"></div>

<script>
const LS_KEY = "zotero_statuts";
const STATUS_CYCLE = ["a_lire","en_cours","lu"];
const STATUS_LABEL = {lu:"✅ Lu", en_cours:"📖 En cours", a_lire:"⬜ À lire"};

let ALL_ITEMS = [], COLLECTIONS = [], UPDATED = "";
let _currentView = "global";
let _currentCol  = null;

// ── localStorage statuts
function loadOverrides(){ try{ return JSON.parse(localStorage.getItem(LS_KEY)||"{}"); }catch{ return {}; } }
function saveOverride(id,s){ const d=loadOverrides(); d[id]=s; localStorage.setItem(LS_KEY,JSON.stringify(d)); }
function getStatus(item){ const o=loadOverrides(); return o[item.id]!==undefined ? o[item.id] : item.initialStatus; }

function cycleStatus(id, btn){
  const next = STATUS_CYCLE[(STATUS_CYCLE.indexOf(btn.dataset.s)+1)%STATUS_CYCLE.length];
  saveOverride(id, next);
  btn.dataset.s = next;
  btn.textContent = STATUS_LABEL[next];
  updateSidebar();
  if(_currentView==="global") refreshGlobalCounts();
}

// ── Chargement données
async function fetchData(){
  const r = await fetch("/data");
  const d = await r.json();
  ALL_ITEMS   = d.items;
  COLLECTIONS = d.collections;
  UPDATED     = d.updated;
}

// ── SSE : écoute les changements Zotero
function connectSSE(){
  const es = new EventSource("/events");
  const dot = document.getElementById("live-dot");
  const lbl = document.getElementById("live-label");

  es.onopen = () => {
    dot.className = "live-dot on";
    lbl.textContent = "En direct";
  };
  es.onmessage = async (e) => {
    if(e.data === "update"){
      await fetchData();
      updateSidebar();
      if(_currentView==="global") showGlobal();
      else if(_currentView==="collection") showCollection(_currentCol);
      showToast("Zotero mis à jour · " + UPDATED);
    }
  };
  es.onerror = () => {
    dot.className = "live-dot";
    lbl.textContent = "Reconnexion…";
    setTimeout(connectSSE, 3000);
    es.close();
  };
}

function showToast(msg){
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(()=>t.classList.remove("show"), 3000);
}

// ── Stats
function pct(n,t){ return t ? Math.round(100*n/t) : 0; }
function colStats(items){ const s={lu:0,en_cours:0,a_lire:0}; items.forEach(it=>s[getStatus(it)]++); return s; }
function globalStats(){ return colStats(ALL_ITEMS); }

function getGroups(){
  const g={};
  COLLECTIONS.forEach(c=>g[c]=[]);
  g["(Sans collection)"]=[];
  ALL_ITEMS.forEach(it=>{
    if(it.collections.length) it.collections.forEach(c=>{ if(g[c]) g[c].push(it); });
    else g["(Sans collection)"].push(it);
  });
  if(!g["(Sans collection)"].length) delete g["(Sans collection)"];
  return g;
}

// ── Sidebar
function updateSidebar(){
  const s = globalStats();
  const tot = ALL_ITEMS.length;
  document.getElementById("cnt-global").textContent = tot;

  const groups = getGroups();
  const colNames = [...COLLECTIONS, ...(groups["(Sans collection)"] ? ["(Sans collection)"] : [])];

  window._colNames = colNames;
  document.getElementById("nav-collections").innerHTML = colNames.map((col,i)=>`
    <div class="nav-item ${_currentCol===col&&_currentView==='collection'?'active':''}"
         id="nav-col-${i}" onclick="showCollectionByIdx(${i})">
      ${escHtml(col)} <span class="count">${(groups[col]||[]).length}</span>
    </div>`).join("");

  document.getElementById("nav-global").className =
    "nav-item" + (_currentView==="global" ? " active" : "");

  document.getElementById("mini-stats").innerHTML = `
    <div class="mini-stat"><span><span class="dot" style="background:var(--green)"></span>Lu</span><span>${s.lu} (${pct(s.lu,tot)}%)</span></div>
    <div class="mini-stat"><span><span class="dot" style="background:var(--amber)"></span>En cours</span><span>${s.en_cours}</span></div>
    <div class="mini-stat"><span><span class="dot" style="background:var(--slate)"></span>À lire</span><span>${s.a_lire}</span></div>
  `;
}

// ── Carte article
function renderCard(item){
  const s = getStatus(item);
  const ann = item.annotations>0 ? `<span class="item-annot">${item.annotations} annot.</span>` : "";
  return `<div class="item-card">
    <div class="item-left">
      <div class="item-title">${escHtml(item.title)}</div>
      <div class="item-meta">${item.creators}${item.date?" · "+item.date:""} · ${item.type}${ann}</div>
    </div>
    <div class="item-right">
      <button class="status-btn" data-s="${s}" onclick="cycleStatus(${item.id},this)">${STATUS_LABEL[s]}</button>
    </div>
  </div>`;
}

// ── Navigation par index (évite les problèmes de guillemets dans les onclick)
function showCollectionByIdx(i){
  const col = (window._colNames || [])[i];
  if(col) showCollection(col);
}

// ── Vue collection
function showCollection(colName){
  _currentView = "collection";
  _currentCol  = colName;
  updateSidebar();
  const groups = getGroups();
  const items  = groups[colName] || [];
  const s = colStats(items);
  const tot = items.length;
  const luPct = pct(s.lu, tot);

  document.getElementById("content").innerHTML = `
    <div class="col-header">
      <h2>${escHtml(colName)}</h2>
      <div class="progress-row">
        <div class="bar-wrap"><div class="bar-fill" style="width:${luPct}%;background:var(--green)"></div></div>
        <div class="prog-label">${s.lu}/${tot} lus (${luPct}%)</div>
      </div>
      <div style="font-size:.8rem;color:var(--muted);margin-top:.35rem">
        ✅ ${s.lu} lu &nbsp;·&nbsp; 📖 ${s.en_cours} en cours &nbsp;·&nbsp; ⬜ ${s.a_lire} à lire
      </div>
    </div>
    <div class="search-wrap">
      <input class="search" placeholder="Rechercher…" oninput="filterCards(this.value, event.target)">
    </div>
    <div id="cards-list">${items.map(renderCard).join("")||'<div class="empty">Aucun article.</div>'}</div>`;
  window._currentItems = items;
}

function filterCards(q, input){
  const ql = q.toLowerCase();
  document.querySelectorAll(".item-card").forEach((card,i)=>{
    const it = window._currentItems[i];
    card.style.display = (!q||(it.title+it.creators).toLowerCase().includes(ql)) ? "" : "none";
  });
}

// ── Vue globale
function showGlobal(){
  _currentView = "global";
  _currentCol  = null;
  updateSidebar();
  const s = globalStats();
  const tot = ALL_ITEMS.length;
  const groups = getGroups();
  const colNames = [...COLLECTIONS, ...(groups["(Sans collection)"] ? ["(Sans collection)"] : [])];

  const colCards = colNames.map(col=>{
    const cs = colStats(groups[col]||[]);
    const ct = (groups[col]||[]).length;
    const p  = pct(cs.lu, ct);
    return `<div class="col-row" onclick="showCollectionByIdx(${colNames.indexOf(col)})">
      <div class="col-name">${escHtml(col)}</div>
      <div style="flex:1;max-width:120px">
        <div class="bar-wrap"><div class="bar-fill" style="width:${p}%;background:var(--green)"></div></div>
      </div>
      <div style="font-size:.8rem;color:var(--muted)">${cs.lu}/${ct} &nbsp;→</div>
    </div>`;
  }).join("");

  document.getElementById("content").innerHTML = `
    <div class="global-cards">
      <div class="gcard"><div class="num">${tot}</div><div class="lbl">📚 Total</div></div>
      <div class="gcard"><div class="num" style="color:var(--green)">${s.lu}</div><div class="lbl">✅ Lus (${pct(s.lu,tot)}%)</div></div>
      <div class="gcard"><div class="num" style="color:var(--amber)">${s.en_cours}</div><div class="lbl">📖 En cours</div></div>
      <div class="gcard"><div class="num" style="color:var(--slate)">${s.a_lire}</div><div class="lbl">⬜ À lire</div></div>
    </div>${colCards}`;
}

function refreshGlobalCounts(){
  // Re-render la vue globale sans perdre la position de scroll
  showGlobal();
}

function escHtml(s){
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// ── Init
(async()=>{
  await fetchData();
  updateSidebar();
  showGlobal();
  connectSSE();
})();
</script>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not ZOTERO_DB.exists():
        sys.exit(f"Base Zotero introuvable : {ZOTERO_DB}")

    print(f"\n📚 Serveur de suivi Zotero")
    print(f"   Ouvrez votre navigateur sur : http://localhost:{PORT}")
    print(f"   (Ctrl+C pour arrêter)\n")

    # Chargement initial
    load_data()
    with _lock:
        print(f"  {len(_cache['items'])} références chargées au démarrage")

    # Thread de surveillance
    t = threading.Thread(target=watch_loop, daemon=True)
    t.start()

    # Serveur HTTP
    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Serveur arrêté.")
