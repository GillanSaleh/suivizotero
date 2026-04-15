#!/usr/bin/env python3
"""
Serveur de suivi Zotero — http://localhost:7777
Surveille zotero.sqlite en continu et pousse les mises à jour au navigateur.
"""

import http.server, threading, json, time, shutil, sqlite3, queue, sys, tempfile, os
from datetime import datetime
from pathlib import Path

# ── IA : Groq (gratuit) ou Claude API ────────────────────────────────────────
_ia_client  = None   # client actif
_ia_backend = None   # "groq" ou "anthropic"

# Groq (prioritaire — gratuit)
try:
    from groq import Groq as _GroqClient
    _GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
    if _GROQ_KEY:
        _ia_client  = _GroqClient(api_key=_GROQ_KEY)
        _ia_backend = "groq"
except ImportError:
    pass

# Claude API (fallback si pas de clé Groq)
if not _ia_client:
    try:
        import anthropic as _anthropic_mod
        _ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
        if _ANTHROPIC_KEY:
            _ia_client  = _anthropic_mod.Anthropic(api_key=_ANTHROPIC_KEY)
            _ia_backend = "anthropic"
    except ImportError:
        pass

_HAS_IA = _ia_client is not None

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
        col_tree = _fetch_collection_tree(conn)
        annots = _fetch_annotations(conn)
        with _lock:
            _cache["items"]       = items
            _cache["col_tree"]    = col_tree
            _cache["annotations"] = annots
            _cache["updated"]     = datetime.now().strftime("%d/%m/%Y à %H:%M:%S")
    finally:
        conn.close()

def _fetch_collection_tree(conn):
    """Retourne une liste ordonnée de {id, name, parent, depth} pour l'affichage."""
    rows = conn.execute("""
        SELECT collectionID, collectionName, parentCollectionID
        FROM collections ORDER BY collectionName
    """).fetchall()
    # Construire un dict enfants
    children = {}
    all_cols = {}
    for cid, name, parent in rows:
        all_cols[cid] = {"id": cid, "name": name, "parent": parent}
        children.setdefault(parent, []).append(cid)

    # Parcours en profondeur depuis les racines
    result = []
    def walk(cid, depth):
        result.append({"id": all_cols[cid]["id"],
                        "name": all_cols[cid]["name"],
                        "depth": depth})
        for child in sorted(children.get(cid, []),
                            key=lambda x: all_cols[x]["name"]):
            walk(child, depth + 1)

    for cid in sorted(children.get(None, []), key=lambda x: all_cols[x]["name"]):
        walk(cid, 0)
    return result

def _fetch_annotations(conn):
    """Retourne {parentItemID_reel: [{"text","comment","color","page"}, ...]}"""
    att_parent = dict(conn.execute(
        "SELECT itemID, parentItemID FROM itemAttachments WHERE parentItemID IS NOT NULL"
    ).fetchall())
    result = {}
    rows = conn.execute("""
        SELECT ia.parentItemID, ia.type, ia.text, ia.comment, ia.color, ia.pageLabel
        FROM itemAnnotations ia
        WHERE (ia.text IS NOT NULL AND ia.text != '')
           OR (ia.comment IS NOT NULL AND ia.comment != '')
        ORDER BY ia.parentItemID, ia.sortIndex
    """).fetchall()
    for att_id, atype, text, comment, color, page in rows:
        parent = att_parent.get(att_id, att_id)
        result.setdefault(parent, []).append({
            "text":    text    or "",
            "comment": comment or "",
            "color":   color   or "#ffd400",
            "page":    page    or ""
        })
    return result

def _fetch_items(conn):
    rows = conn.execute("""
        SELECT i.itemID,
               MAX(CASE WHEN f.fieldName='title' THEN idv.value END),
               MAX(CASE WHEN f.fieldName='date'  THEN idv.value END),
               it.typeName,
               i.dateAdded,
               MAX(CASE WHEN f.fieldName='abstractNote' THEN idv.value END)
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
             "date": (r[2] or "")[:4], "type": r[3],
             "dateAdded": r[4] or "", "abstract": r[5] or ""} for r in rows]

def _get_last_opened(conn):
    """Retourne {parentItemID: last_opened_date} via atime des fichiers PDF."""
    zotero_storage = Path.home() / "Zotero" / "storage"
    result = {}
    rows = conn.execute("""
        SELECT i.itemID, i.key, ia.parentItemID
        FROM items i
        JOIN itemAttachments ia ON i.itemID = ia.itemID
        WHERE ia.contentType = 'application/pdf'
          AND ia.parentItemID IS NOT NULL
    """).fetchall()
    for att_id, key, parent_id in rows:
        folder = zotero_storage / key
        if not folder.exists():
            continue
        for pdf in folder.glob("*.pdf"):
            try:
                atime = os.stat(pdf).st_atime
                mtime = os.stat(pdf).st_mtime
                # Ouvert seulement si atime > mtime (accès après ajout)
                if atime > mtime + 60:
                    dt = datetime.fromtimestamp(atime).strftime("%d/%m/%Y")
                    existing = result.get(parent_id)
                    if not existing or atime > datetime.strptime(existing, "%d/%m/%Y").timestamp():
                        result[parent_id] = dt
            except Exception:
                pass
    return result

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

    # Annotations + date dernière annotation
    att_parent = dict(conn.execute(
        "SELECT itemID, parentItemID FROM itemAttachments WHERE parentItemID IS NOT NULL"
    ).fetchall())
    annots = {}
    last_annot_date = {}
    last_page = {}
    for att_id, cnt, last_date, max_phys_page in conn.execute("""
        SELECT ia.parentItemID, COUNT(*), MAX(i.dateModified),
               MAX(CASE WHEN SUBSTR(ia.sortIndex, 6, 1) = '|'
                        THEN CAST(SUBSTR(ia.sortIndex, 1, 5) AS INTEGER) + 1
                        ELSE NULL END)
        FROM itemAnnotations ia
        JOIN items i ON ia.itemID = i.itemID
        GROUP BY ia.parentItemID
    """).fetchall():
        p = att_parent.get(att_id, att_id)
        annots[p] = annots.get(p, 0) + cnt
        if last_date:
            existing = last_annot_date.get(p, "")
            if last_date > existing:
                last_annot_date[p] = last_date[:10]
        if max_phys_page:
            last_page[p] = str(max_phys_page)

    # Pages totales depuis fulltextItems
    total_pages = {}
    for att_id, tot in conn.execute("""
        SELECT fi.itemID, fi.totalPages
        FROM fulltextItems fi
        WHERE fi.totalPages > 0
    """).fetchall():
        p = att_parent.get(att_id, att_id)
        if tot and (p not in total_pages or tot > total_pages[p]):
            total_pages[p] = tot

    # Dernière ouverture des PDFs (atime)
    last_opened = _get_last_opened(conn)

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
        item["creators"]       = _fmt_creators(creators.get(iid, []))
        item["annotations"]    = annots.get(iid, 0)
        item["lastAnnotation"] = last_annot_date.get(iid, "")
        item["lastPage"]       = last_page.get(iid, "")
        item["totalPages"]     = total_pages.get(iid, 0)
        item["lastOpened"]     = last_opened.get(iid, "")
        item["collections"]    = item_cols.get(iid, [])
        if tl & TAGS_LU:              item["initialStatus"] = "lu"
        elif tl & TAGS_EN_COURS:      item["initialStatus"] = "en_cours"
        elif item["annotations"] > 0: item["initialStatus"] = "en_cours"
        elif item["lastOpened"]:      item["initialStatus"] = "consulte"
        else:                         item["initialStatus"] = "a_lire"

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
        elif self.path == "/annotations":
            self._serve_annotations()
        elif self.path == "/has-ia":
            self._serve_has_ia()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/synthese-ia":
            self._serve_synthese_ia()
        else:
            self.send_error(404)

    def _serve_has_ia(self):
        payload = json.dumps({"available": _HAS_IA, "backend": _ia_backend})
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(payload.encode())

    def _serve_synthese_ia(self):
        if not _HAS_IA:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            msg = "Aucune clé IA configurée (GROQ_API_KEY ou ANTHROPIC_API_KEY)"
            self.wfile.write(json.dumps({"error": msg}).encode())
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        ids  = set(body.get("ids", []))
        col  = body.get("col", "")

        with _lock:
            items  = [it for it in _cache["items"] if it["id"] in ids]
            annots = _cache.get("annotations", {})

        # Construction du prompt
        docs = []
        for item in items:
            anns = annots.get(item["id"], [])
            ann_lines = []
            for a in anns:
                pg  = f"[p.{a['page']}] " if a['page'] else ""
                txt = a['text'].strip()    if a['text']    else ""
                cmt = a['comment'].strip() if a['comment'] else ""
                if txt:
                    ann_lines.append(f"  • {pg}{txt}")
                if cmt:
                    ann_lines.append(f"    💬 {cmt}")
            doc = f"### {item['title']}\n"
            doc += f"Auteur(s) : {item['creators']}"
            if item['date']:
                doc += f" ({item['date']})"
            doc += "\n"
            if item['abstract']:
                doc += f"Résumé : {item['abstract']}\n"
            doc += f"\nAnnotations ({len(anns)}) :\n"
            doc += "\n".join(ann_lines) if ann_lines else "(aucune annotation)"
            docs.append(doc)

        prompt = (
            f"Tu es un assistant de recherche académique. "
            f"Voici mes notes de lecture pour {len(items)} article(s) "
            f"de la collection « {col} ».\n\n"
            + "\n\n---\n\n".join(docs)
            + "\n\n---\n\n"
            "Génère une synthèse de lecture structurée et rédigée en français. "
            "Pour chaque article, rédige un ou deux paragraphes académiques qui intègrent "
            "naturellement les passages surlignés comme illustrations des idées principales. "
            "Termine par une conclusion générale sur les thèmes transversaux de la collection. "
            "Utilise du markdown (titres ##, gras, italique)."
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            if _ia_backend == "groq":
                stream = _ia_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4096,
                    stream=True
                )
                for chunk in stream:
                    text = chunk.choices[0].delta.content or ""
                    if text:
                        data = json.dumps({"chunk": text}, ensure_ascii=False)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
            else:  # anthropic
                with _ia_client.messages.stream(
                    model="claude-opus-4-6",
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}]
                ) as stream:
                    for text in stream.text_stream:
                        data = json.dumps({"chunk": text}, ensure_ascii=False)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
            self.wfile.write(b'data: {"done":true}\n\n')
            self.wfile.flush()
        except Exception as e:
            err = json.dumps({"error": str(e)}, ensure_ascii=False)
            self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
            self.wfile.flush()

    def _serve_annotations(self):
        with _lock:
            payload = json.dumps(_cache.get("annotations", {}), ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload.encode())

    def _serve_data(self):
        with _lock:
            payload = json.dumps({
                "items":    _cache["items"],
                "col_tree": _cache.get("col_tree", []),
                "updated":  _cache["updated"]
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
.status-btn[data-s="consulte"]{background:#818cf820;color:#818cf8;border:1px solid #818cf850}
.global-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.75rem;margin-bottom:1.5rem}
.gcard{background:var(--surface);border:1px solid var(--border);border-radius:.6rem;padding:1rem}
.gcard .num{font-size:1.8rem;font-weight:700;transition:all .3s}
.gcard .lbl{font-size:.75rem;color:var(--muted);margin-top:.2rem}
.col-row{background:var(--surface);border:1px solid var(--border);border-radius:.6rem;padding:.85rem 1rem;margin-bottom:.6rem;display:flex;align-items:center;gap:1rem;cursor:pointer;transition:border-color .15s}
.col-row:hover{border-color:var(--blue)}
.col-name{font-weight:600;flex:1}
.empty{color:var(--muted);font-size:.9rem;padding:1rem 0;text-align:center}
.filter-col-btn{background:var(--surface2);border:1px solid var(--border);color:var(--muted);border-radius:.4rem;padding:.3rem .7rem;font-size:.78rem;cursor:pointer;transition:all .15s}
.filter-col-btn:hover{color:var(--text)}
.filter-col-btn.active{border-color:var(--blue);color:var(--blue)}
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
const STATUS_CYCLE = ["a_lire","consulte","en_cours","lu"];
const STATUS_LABEL = {lu:"✅ Lu", en_cours:"📖 En cours", a_lire:"⬜ À lire", consulte:"👁 Consulté"};

let ALL_ITEMS = [], COL_TREE = [], UPDATED = "";
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
  ALL_ITEMS = d.items;
  COL_TREE  = d.col_tree || [];
  UPDATED   = d.updated;
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
function colStats(items){ const s={lu:0,en_cours:0,a_lire:0,consulte:0}; items.forEach(it=>{ const k=getStatus(it); if(s[k]!==undefined) s[k]++; }); return s; }
function globalStats(){ return colStats(ALL_ITEMS); }

function getGroups(){
  const g={};
  COL_TREE.forEach(c=>g[c.name]=[]);
  g["(Sans collection)"]=[];
  ALL_ITEMS.forEach(it=>{
    if(it.collections.length) it.collections.forEach(c=>{ if(g[c]!==undefined) g[c].push(it); });
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
  const treeItems = [...COL_TREE];
  if(groups["(Sans collection)"] && groups["(Sans collection)"].length)
    treeItems.push({name:"(Sans collection)", depth:0});
  const colNames = treeItems.map(c=>c.name);

  window._colNames = colNames;
  document.getElementById("nav-collections").innerHTML = treeItems.map((col,i)=>{
    const indent = col.depth * 12;
    const prefix = col.depth > 0 ? `<span style="opacity:.4;margin-right:.2rem">└</span>` : "";
    return `<div class="nav-item ${_currentCol===col.name&&_currentView==='collection'?'active':''}"
         id="nav-col-${i}" onclick="showCollectionByIdx(${i})"
         style="padding-left:${1 + indent/16}rem">
      ${prefix}${escHtml(col.name)} <span class="count">${(groups[col.name]||[]).length}</span>
    </div>`;
  }).join("");

  document.getElementById("nav-global").className =
    "nav-item" + (_currentView==="global" ? " active" : "");

  document.getElementById("mini-stats").innerHTML = `
    <div class="mini-stat"><span><span class="dot" style="background:var(--green)"></span>Lu</span><span>${s.lu} (${pct(s.lu,tot)}%)</span></div>
    <div class="mini-stat"><span><span class="dot" style="background:var(--amber)"></span>En cours</span><span>${s.en_cours}</span></div>
    <div class="mini-stat"><span><span class="dot" style="background:var(--slate)"></span>À lire</span><span>${s.a_lire}</span></div>
  `;
}

// ── Barre de progression lecture
function renderProgress(item){
  const s = getStatus(item);
  const tot  = item.totalPages || 0;
  const last = parseInt(item.lastPage) || 0;

  if(s === "lu"){
    if(tot > 0){
      return `<span style="display:inline-flex;align-items:center;gap:.35rem;margin-left:.5rem">
        <span style="display:inline-block;width:60px;background:var(--border);border-radius:9999px;height:5px;overflow:hidden;vertical-align:middle">
          <span style="display:block;width:100%;height:100%;background:var(--green);border-radius:9999px"></span>
        </span>
        <span style="font-size:.72rem;color:var(--green)">${tot}/${tot} (100%)</span>
      </span>`;
    }
    return "";
  }

  if(!last) return "";
  if(tot > 0){
    const pct = Math.min(100, Math.round(last / tot * 100));
    return `<span style="display:inline-flex;align-items:center;gap:.35rem;margin-left:.5rem">
      <span style="display:inline-block;width:60px;background:var(--border);border-radius:9999px;height:5px;overflow:hidden;vertical-align:middle">
        <span style="display:block;width:${pct}%;height:100%;background:var(--amber);border-radius:9999px"></span>
      </span>
      <span style="font-size:.72rem;color:var(--amber)">p.${last}/${tot} (${pct}%)</span>
    </span>`;
  }
  return `<span style="font-size:.72rem;color:var(--amber);margin-left:.4rem">📄 p.${last}</span>`;
}

// ── Carte article
function renderCard(item){
  const s = getStatus(item);
  const ann = item.annotations>0 ? `<span class="item-annot">${item.annotations} annot.</span>` : "";
  const lastRead = item.lastAnnotation
    ? `<span style="font-size:.72rem;color:var(--muted);margin-left:.4rem">· annoté le ${item.lastAnnotation}</span>`
    : item.lastOpened
    ? `<span style="font-size:.72rem;color:var(--muted);margin-left:.4rem">· ouvert le ${item.lastOpened}</span>`
    : "";
  const progress = renderProgress(item);
  const openedBadge = (s==="consulte") ? `<span style="font-size:.72rem;color:#818cf8;margin-left:.4rem">👁 ouvert sans annotation</span>` : "";
  return `<div class="item-card">
    <div class="item-left">
      <div class="item-title">${escHtml(item.title)}</div>
      <div class="item-meta">${item.creators}${item.date?" · "+item.date:""} · ${item.type}${ann}${progress}${openedBadge}${lastRead}</div>
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
  _colFilter = "all";
  _colSearch  = "";
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
      <div style="font-size:.8rem;color:var(--muted);margin-top:.35rem;display:flex;align-items:center;gap:1rem">
        <span>✅ ${s.lu} lu &nbsp;·&nbsp; 📖 ${s.en_cours} en cours &nbsp;·&nbsp; 👁 ${s.consulte} consulté &nbsp;·&nbsp; ⬜ ${s.a_lire} à lire</span>
        <button onclick="markAllLu(${JSON.stringify(colName)})" style="background:#22c55e20;color:#22c55e;border:1px solid #22c55e50;border-radius:.4rem;padding:.25rem .7rem;font-size:.75rem;cursor:pointer;font-weight:600">✅ Tout marquer Lu</button>
        <button onclick="genSynthese(${JSON.stringify(colName)})" style="background:#818cf820;color:#818cf8;border:1px solid #818cf850;border-radius:.4rem;padding:.25rem .7rem;font-size:.75rem;cursor:pointer;font-weight:600">📝 Synthèse</button>
        <button id="btn-ia-${CSS.escape(colName)}" onclick="genSyntheseIA(${JSON.stringify(colName)})" style="background:#f0abfc20;color:#e879f9;border:1px solid #e879f950;border-radius:.4rem;padding:.25rem .7rem;font-size:.75rem;cursor:pointer;font-weight:600">🤖 Synthèse IA</button>
      </div>
    </div>
    <div style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.75rem;align-items:center">
      <button class="filter-col-btn active" onclick="filterCol('all',this)">Tous (${tot})</button>
      <button class="filter-col-btn" onclick="filterCol('lu',this)">✅ Lu (${s.lu})</button>
      <button class="filter-col-btn" onclick="filterCol('en_cours',this)">📖 En cours (${s.en_cours})</button>
      <button class="filter-col-btn" onclick="filterCol('consulte',this)">👁 Consulté (${s.consulte})</button>
      <button class="filter-col-btn" onclick="filterCol('a_lire',this)">⬜ À lire (${s.a_lire})</button>
      <input class="search" style="flex:1;min-width:160px" placeholder="Rechercher…" oninput="filterCards(this.value)">
    </div>
    <div id="cards-list">${items.map(renderCard).join("")||'<div class="empty">Aucun article.</div>'}</div>`;
  window._currentItems = items;
}

let _colFilter = "all";
let _colSearch  = "";

function filterCol(status, btn){
  _colFilter = status;
  document.querySelectorAll(".filter-col-btn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active");
  applyColFilters();
}

function filterCards(q){
  _colSearch = q;
  applyColFilters();
}

function applyColFilters(){
  document.querySelectorAll(".item-card").forEach((card,i)=>{
    const it = (window._currentItems||[])[i];
    if(!it){ card.style.display="none"; return; }
    const statusOk = _colFilter==="all" || getStatus(it)===_colFilter;
    const searchOk = !_colSearch || (it.title+it.creators).toLowerCase().includes(_colSearch.toLowerCase());
    card.style.display = (statusOk && searchOk) ? "" : "none";
  });
}

function markAllLu(colName){
  const groups = getGroups();
  const items = groups[colName] || [];
  items.forEach(it => saveOverride(it.id, "lu"));
  showCollection(colName);
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
  const treeItems2 = [...COL_TREE];
  if(groups["(Sans collection)"] && groups["(Sans collection)"].length)
    treeItems2.push({name:"(Sans collection)", depth:0});
  const colNames = treeItems2.map(c=>c.name);

  const colCards = treeItems2.map((col,i)=>{
    const cs = colStats(groups[col.name]||[]);
    const ct = (groups[col.name]||[]).length;
    const p  = pct(cs.lu, ct);
    const indent = col.depth * 20;
    return `<div class="col-row" style="margin-left:${indent}px" onclick="showCollectionByIdx(${i})">
      <div class="col-name">${col.depth>0?'<span style="opacity:.4;margin-right:.3rem">└</span>':''}${escHtml(col.name)}</div>
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
      <div class="gcard"><div class="num" style="color:#818cf8">${s.consulte}</div><div class="lbl">👁 Consultés</div></div>
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

// ── Couleurs Zotero → CSS
const ZOTERO_COLORS = {
  "#ffd400":"#ffd400", "#ff6666":"#ff6666", "#5fb236":"#5fb236",
  "#2ea8e5":"#2ea8e5", "#a28ae5":"#a28ae5", "#e56eee":"#e56eee",
  "#f19837":"#f19837", "#aaaaaa":"#aaaaaa"
};
function annotColor(c){ return ZOTERO_COLORS[c] || "#ffd400"; }

async function genSynthese(colName){
  showToast("Génération de la synthèse…");
  const annData = await fetch("/annotations").then(r=>r.json());
  const pool = colName
    ? (getGroups()[colName] || [])
    : ALL_ITEMS;
  const luItems = pool.filter(it => getStatus(it) === "lu");
  const titre = colName ? colName : "Toutes collections";

  if(!luItems.length){
    showToast("Aucun article Lu dans cette collection.");
    return;
  }

  const now = new Date().toLocaleDateString("fr-FR",{day:"2-digit",month:"long",year:"numeric"});

  const sections = luItems.map(item => {
    const anns = annData[item.id] || [];
    const annHtml = anns.length ? anns.map(a => {
      const col = annotColor(a.color);
      const txt = a.text ? `<blockquote style="border-left:4px solid ${col};background:${col}18;margin:.5rem 0;padding:.5rem .75rem;border-radius:0 .4rem .4rem 0;font-style:italic;color:#1e293b">${escHtml(a.text)}</blockquote>` : "";
      const cmt = a.comment ? `<div style="color:#475569;font-size:.85rem;margin:.25rem 0 .5rem 1rem">💬 ${escHtml(a.comment)}</div>` : "";
      const pg  = a.page ? `<div style="font-size:.72rem;color:#94a3b8;margin-bottom:.25rem">p. ${escHtml(a.page)}</div>` : "";
      return `<div style="margin-bottom:.75rem">${pg}${txt}${cmt}</div>`;
    }).join("") : `<p style="color:#94a3b8;font-style:italic">Aucune annotation.</p>`;

    const abstract = item.abstract
      ? `<div style="background:#f1f5f9;border-radius:.4rem;padding:.75rem;margin:.75rem 0;font-size:.88rem;color:#334155"><strong>Résumé :</strong> ${escHtml(item.abstract)}</div>`
      : "";
    const cols = item.collections.length ? `<span style="font-size:.78rem;color:#64748b">📁 ${item.collections.join(", ")}</span>` : "";

    return `<section style="background:white;border-radius:.75rem;box-shadow:0 1px 4px #0001;padding:1.5rem;margin-bottom:1.5rem;page-break-inside:avoid">
      <h2 style="font-size:1.05rem;font-weight:700;color:#0f172a;margin-bottom:.25rem">${escHtml(item.title)}</h2>
      <div style="font-size:.82rem;color:#64748b;margin-bottom:.5rem">${escHtml(item.creators)} ${item.date?"· "+item.date:""} · ${escHtml(item.type)} &nbsp;${cols}</div>
      ${abstract}
      <h3 style="font-size:.82rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8;margin:.75rem 0 .5rem">Annotations (${anns.length})</h3>
      ${annHtml}
    </section>`;
  }).join("");

  const html = `<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<title>Synthèse — ${escHtml(titre)} — ${now}</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f8fafc;color:#1e293b;max-width:860px;margin:0 auto;padding:2rem 1.5rem}
  h1{font-size:1.5rem;font-weight:800;margin-bottom:.25rem}
  .meta{color:#64748b;font-size:.85rem;margin-bottom:2rem}
  @media print{body{background:white;padding:0} .no-print{display:none}}
</style></head><body>
<div class="no-print" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1.5rem">
  <div><h1>📝 Synthèse — ${escHtml(titre)}</h1><div class="meta">Générée le ${now} · ${luItems.length} articles lus</div></div>
  <button onclick="window.print()" style="background:#0f172a;color:white;border:none;border-radius:.5rem;padding:.6rem 1.2rem;font-size:.88rem;cursor:pointer;font-weight:600">🖨 Exporter en PDF</button>
</div>
<div class="print-only" style="display:none"><h1>📝 Synthèse de lecture</h1><div class="meta">Générée le ${now} · ${luItems.length} articles lus</div></div>
${sections}
</body></html>`;

  const w = window.open("", "_blank");
  w.document.write(html);
  w.document.close();
}

// ── Synthèse IA (Claude API)
let _iaAvailable = null;
async function checkIA(){
  if(_iaAvailable !== null) return _iaAvailable;
  try{
    const r = await fetch("/has-ia");
    const d = await r.json();
    _iaAvailable = d.available;
  } catch(e){ _iaAvailable = false; }
  return _iaAvailable;
}

// Convertit le markdown simple en HTML
function mdToHtml(md){
  return md
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/^### (.+)$/gm,"<h3>$1</h3>")
    .replace(/^## (.+)$/gm,"<h2>$1</h2>")
    .replace(/^# (.+)$/gm,"<h1>$1</h1>")
    .replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>")
    .replace(/\*(.+?)\*/g,"<em>$1</em>")
    .replace(/\n\n/g,"</p><p>")
    .replace(/^/,"<p>").replace(/$/,"</p>");
}

async function genSyntheseIA(colName){
  const ok = await checkIA();
  if(!ok){
    alert("Synthèse IA non disponible.\\n\\nVérifiez que :\\n1. anthropic est installé : pip install anthropic\\n2. La variable ANTHROPIC_API_KEY est définie avant de lancer le serveur.");
    return;
  }
  const groups = getGroups();
  const pool   = colName ? (groups[colName] || []) : ALL_ITEMS;
  const luItems = pool.filter(it => getStatus(it) === "lu");
  if(!luItems.length){
    showToast("Aucun article Lu dans cette collection.");
    return;
  }

  const now = new Date().toLocaleDateString("fr-FR",{day:"2-digit",month:"long",year:"numeric"});
  const titre = colName || "Toutes collections";

  // Ouvrir la fenêtre de suite tout de suite (évite le blocage popup)
  const w = window.open("", "_blank");
  w.document.write(`<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<title>Synthèse IA — ${escHtml(titre)}</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f8fafc;color:#1e293b;max-width:860px;margin:0 auto;padding:2rem 1.5rem}
  h1{font-size:1.5rem;font-weight:800;margin-bottom:.25rem}
  h2{font-size:1.1rem;font-weight:700;margin:1.5rem 0 .5rem;color:#0f172a}
  h3{font-size:.95rem;font-weight:700;margin:1.2rem 0 .3rem;color:#1e40af}
  p{margin:.6rem 0;line-height:1.75;font-size:.95rem}
  .meta{color:#64748b;font-size:.85rem;margin-bottom:2rem}
  #status{color:#9333ea;font-size:.85rem;font-style:italic;margin-bottom:1rem}
  @media print{.no-print{display:none}}
</style></head><body>
<div class="no-print" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
  <div>
    <h1>🤖 Synthèse IA — ${escHtml(titre)}</h1>
    <div class="meta">Générée le ${now} · ${luItems.length} articles lus</div>
  </div>
  <button onclick="window.print()" style="background:#0f172a;color:white;border:none;border-radius:.5rem;padding:.6rem 1.2rem;font-size:.88rem;cursor:pointer;font-weight:600">🖨 Exporter PDF</button>
</div>
<div id="status">⏳ Génération en cours…</div>
<div id="output"></div>
</body></html>`);
  w.document.close();

  showToast("Synthèse IA en cours…");

  try{
    const resp = await fetch("/synthese-ia", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({col: colName, ids: luItems.map(it=>it.id)})
    });

    if(!resp.ok){
      const err = await resp.json();
      w.document.getElementById("status").textContent = "Erreur : " + (err.error || resp.status);
      return;
    }

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", fullText = "";
    const output = w.document.getElementById("output");
    const status = w.document.getElementById("status");

    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      buffer += decoder.decode(value, {stream:true});
      const lines = buffer.split("\\n");
      buffer = lines.pop();
      for(const line of lines){
        if(!line.startsWith("data: ")) continue;
        try{
          const msg = JSON.parse(line.slice(6));
          if(msg.chunk !== undefined){
            fullText += msg.chunk;
            output.innerHTML = mdToHtml(fullText);
          }
          if(msg.done){
            status.textContent = "✅ Synthèse générée.";
            showToast("Synthèse IA prête !");
          }
          if(msg.error){
            status.textContent = "Erreur : " + msg.error;
            showToast("Erreur IA");
          }
        } catch(e){}
      }
    }
  } catch(e){
    w.document.getElementById("status").textContent = "Erreur réseau : " + e.message;
  }
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
