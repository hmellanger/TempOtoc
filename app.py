import ctypes
import ctypes.wintypes
import json
import sys
import threading
import time
import winreg
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

IDLE_THRESHOLD = 300   # 5 minutes d'inactivite = fin d'activite
POLL_SECONDS = 10      # frequence de sondage
PORT = 8765

if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).parent
else:
    BASE = Path(__file__).resolve().parent
LOG_FILE = BASE / "activity_log.jsonl"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Tempotoc"


def autostart_enabled():
    """True si Tempotoc est inscrit dans demarrage Windows (cle Run)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(on):
    """Ajoute/supprime l'entree de demarrage automatique Windows."""
    exe = str(BASE / "Tempotoc.exe")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, exe)
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass
    return on


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def schedule_shutdown(delay=3.0):
    # Fermeture propre planifiee : le serveur reste vivant `delay` secondes
    # (le dashboard en profite pour se recharger et afficher la periode
    # finalisee), puis WM_QUIT a la fenetre cachee qui enregistre le "end"
    # de la session en cours et termine le process proprement.
    def fire():
        time.sleep(delay)
        hwnd = getattr(main, "_hwnd", None)
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0012, 0, 0)
    threading.Thread(target=fire, daemon=True).start()

def idle_seconds():
    """Secondes depuis la derniere entree clavier/souris."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0


LOG_LOCK = threading.Lock()


def log_event(etype, ts, comment=""):
    ev = {"type": etype, "ts": ts.strftime("%Y-%m-%dT%H:%M:%S")}
    if comment:
        ev["comment"] = comment
    with LOG_LOCK:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")


def edit_comment(ts_str, etype, comment):
    """Edite le commentaire d'un evenement 'start'/'end'/'note' du journal.
    ts_str : horodatage ISO exact de l'evenement (YYYY-MM-DDTHH:MM:SS).
    Commentaire vide = supprime le commentaire. Retourne True si modifie."""
    with LOG_LOCK:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        out, found = [], False
        for line in lines:
            try:
                ev = json.loads(line)
            except Exception:
                out.append(line)
                continue
            if ev.get("ts") == ts_str and ev.get("type") == etype:
                if comment:
                    ev["comment"] = comment
                else:
                    ev.pop("comment", None)
                out.append(json.dumps(ev))
                found = True
            else:
                out.append(line)
        if found:
            tmp = LOG_FILE.with_name(LOG_FILE.name + ".tmp")
            tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
            tmp.replace(LOG_FILE)
        return found


def delete_session(ts_str):
    """Supprime la periode d'activite demarrant a ts_str : l'evenement 'start'
    et son 'end' apparie (le prochain 'end' qui suit dans le journal). Supprime
    aussi une note de ts_str, ou l'evenement 'end' de ts_str (la ligne
    d'inactivite : la fin de periode est retiree, la session redevient ouverte).
    Retourne True si supprime."""
    with LOG_LOCK:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        evs = []
        for line in lines:
            try:
                evs.append(json.loads(line))
            except Exception:
                evs.append(None)  # ligne abimee : conservee telle quelle
        drop = set()
        for i, ev in enumerate(evs):
            if ev and ev.get("ts") == ts_str and ev.get("type") in ("start", "note", "end"):
                drop.add(i)
                if ev.get("type") == "start":
                    # 'end' apparie = prochain 'end' qui suit dans le journal
                    for j in range(i + 1, len(evs)):
                        if not evs[j]:
                            continue
                        if evs[j].get("type") == "end":
                            drop.add(j)
                            break
                        if evs[j].get("type") == "start":
                            break  # session ouverte : pas d'end apparie
        if not drop:
            return False
        out = [json.dumps(e) if e else lines[i]
               for i, e in enumerate(evs) if i not in drop]
        tmp = LOG_FILE.with_name(LOG_FILE.name + ".tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        tmp.replace(LOG_FILE)
        return True


def edit_time(ts_str, etype, new_ts_str):
    """Edite l'horodatage d'un evenement 'start'/'end' du journal.
    ts_str : horodatage ISO exact de l'evenement a modifier.
    new_ts_str : nouvel horodatage ISO (YYYY-MM-DDTHH:MM:SS).
    Pour 'start', l''end' apparie est decale du meme delta (duree conservee).
    Refuse : horodatage futur, 'start' >= son 'end', 'end' <= son 'start',
    et tout decalage qui ferait chevaucher la session voisine (le pairing
    start->end du journal serait casse). Retourne True si modifie."""
    try:
        new_ts = datetime.strptime(new_ts_str, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return False
    now = datetime.now()
    if new_ts > now:
        return False  # pas de date future
    with LOG_LOCK:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        evs = []
        for line in lines:
            try:
                evs.append(json.loads(line))
            except Exception:
                evs.append(None)  # ligne abimee : conservee telle quelle
        idx = None
        for i, ev in enumerate(evs):
            if ev and ev.get("ts") == ts_str and ev.get("type") == etype:
                idx = i
                break
        if idx is None:
            return False
        try:
            old = datetime.strptime(evs[idx]["ts"], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return False
        delta = new_ts - old
        if etype == "start":
            # 'end' apparie = prochain 'end' qui suit
            pair = None
            for j in range(idx + 1, len(evs)):
                if not evs[j]:
                    continue
                if evs[j].get("type") == "end":
                    pair = j
                    break
                if evs[j].get("type") == "start":
                    break  # session ouverte : pas d'end apparie
            if pair is not None:
                try:
                    new_end = datetime.strptime(
                        evs[pair]["ts"], "%Y-%m-%dT%H:%M:%S") + delta
                except Exception:
                    return False
                if new_end <= new_ts or new_end > now:
                    return False
                evs[pair]["ts"] = new_end.strftime("%Y-%m-%dT%H:%M:%S")
            # ne pas chevaucher la fin de la session precedente
            for j in range(idx - 1, -1, -1):
                if not evs[j]:
                    continue
                if evs[j].get("type") == "end":
                    try:
                        prev_end = datetime.strptime(
                            evs[j]["ts"], "%Y-%m-%dT%H:%M:%S")
                    except Exception:
                        break
                    if new_ts <= prev_end:
                        return False
                    break
                if evs[j].get("type") == "start":
                    break
            evs[idx]["ts"] = new_ts.strftime("%Y-%m-%dT%H:%M:%S")
        elif etype == "end":
            # 'start' apparie = dernier 'start' qui precede
            pair = None
            for j in range(idx - 1, -1, -1):
                if not evs[j]:
                    continue
                if evs[j].get("type") == "start":
                    pair = j
                    break
            if pair is not None:
                try:
                    st = datetime.strptime(evs[pair]["ts"], "%Y-%m-%dT%H:%M:%S")
                except Exception:
                    return False
                if new_ts <= st:
                    return False
            # ne pas chevaucher le debut de la session suivante
            for j in range(idx + 1, len(evs)):
                if not evs[j]:
                    continue
                if evs[j].get("type") == "start":
                    try:
                        next_start = datetime.strptime(
                            evs[j]["ts"], "%Y-%m-%dT%H:%M:%S")
                    except Exception:
                        break
                    if new_ts >= next_start:
                        return False
                    break
            evs[idx]["ts"] = new_ts.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            return False
        out = [json.dumps(e) if e else lines[i]
               for i, e in enumerate(evs)]
        tmp = LOG_FILE.with_name(LOG_FILE.name + ".tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        tmp.replace(LOG_FILE)
        return True


def load_sessions():
    """Paire les evenements start/end en sessions. Une session ouverte a end=None.
    Chaque session = (start, end, start_comment, end_comment) :
    - start_comment : commentaire de l'evenement 'start' (justifie la reprise,
      ex. changement de dossier) ;
    - end_comment : commentaire de l'evenement 'end' (justifie l'arret, ex.
      pause déjeuner — il explique la periode d'inactivite qui suit).
    Les evenements 'note' (commentaire hors session) sont retournes separement
    via load_notes()."""
    sessions = []
    cur = None
    cur_cmt = ""
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                ts = datetime.fromisoformat(ev["ts"])
            except Exception:
                continue
            if ev["type"] == "start":
                if cur is not None:
                    # start suivant un start non ferme (ex. 'end' supprime) :
                    # la session precedente est ouverte (end=None).
                    sessions.append((cur, None, cur_cmt, ""))
                cur = ts
                cur_cmt = ev.get("comment", "")
            elif ev["type"] == "end":
                if cur is not None:
                    sessions.append((cur, ts, cur_cmt, ev.get("comment", "")))
                    cur = None
                    cur_cmt = ""
    if cur is not None:
        sessions.append((cur, None, cur_cmt, ""))
    return sessions


def load_notes():
    """Commentaires hors session (evenements 'note') : (ts, texte)."""
    notes = []
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                ts = datetime.fromisoformat(ev["ts"])
            except Exception:
                continue
            if ev["type"] == "note" and ev.get("comment"):
                notes.append((ts, ev["comment"]))
    return notes


def last_start_comment():
    """Commentaire de DEBUT de la derniere periode d'activite (qu'il soit vide
    ou non). Sert de pre-remplissage automatique : journalise sur le 'start'
    quand la reprise est detectee toute seule (clavier/souris apres une periode
    d'inactivite)."""
    sessions = load_sessions()
    if not sessions:
        return ""
    s, e, sc, ec = sessions[-1]
    return sc or ""


def last_end_comment():
    """Commentaire de fin de la derniere periode d'activite (session fermee).
    Sert de reprise automatique : pre-rempli dans la popup Demarrer/Arreter
    et journalise sur le 'start' quand la reprise est detectee toute seule."""
    sessions = load_sessions()
    for s, e, sc, ec in reversed(sessions):
        if e is not None and ec:
            return ec
    notes = load_notes()
    if notes:
        return notes[-1][1]
    return ""


def overlap(session, d0, d1):
    s, e = session[0], session[1]
    if e is None:
        e = datetime.now()
    a = max(s, d0)
    b = min(e, d1)
    return max(0.0, (b - a).total_seconds())


def day_range(d):
    d0 = datetime(d.year, d.month, d.day)
    return d0, d0 + timedelta(days=1)


def build_stats(period, anchor, filt=""):
    sessions = load_sessions()
    # Filtre par commentaire de DEBUT : ne garde que les periodes dont le
    # commentaire de debut contient le texte saisi (insensible a la casse,
    # n'importe ou dans le commentaire). Filtre vide = aucune restriction.
    if filt:
        needle = filt.lower()
        sessions = [s for s in sessions if s[2] and needle in s[2].lower()]
    items = []
    if period == "year":
        y = anchor.year
        for m in range(1, 13):
            d = datetime(y, m, 1)
            total = 0.0
            while d.month == m:
                total += sum(overlap(s, *day_range(d)) for s in sessions)
                d += timedelta(days=1)
            items.append({"date": datetime(y, m, 1).strftime("%Y-%m-01"),
                          "label": MONTHS[m - 1], "value": total})
    elif period == "month":
        y, m = anchor.year, anchor.month
        d = datetime(y, m, 1)
        while d.month == m:
            total = sum(overlap(s, *day_range(d)) for s in sessions)
            items.append({"date": d.strftime("%Y-%m-%d"),
                          "label": f"{d.day:02d}", "value": total})
            d += timedelta(days=1)
    elif period == "week":
        d = anchor - timedelta(days=anchor.weekday())  # lundi
        for i in range(7):
            total = sum(overlap(s, *day_range(d)) for s in sessions)
            items.append({"date": d.strftime("%Y-%m-%d"),
                          "label": WEEKDAYS[i], "value": total})
            d += timedelta(days=1)
    elif period == "day":
        d0, d1 = day_range(anchor)
        for s, e, sc, ec in sessions:
            open_session = e is None
            if open_session:
                e = datetime.now()
            a, b = max(s, d0), min(e, d1)
            if b > a:
                items.append({"start": a.strftime("%H:%M"),
                              "end": e.strftime("%H:%M"),
                              "open": open_session,
                              "start_full": a.strftime("%Y-%m-%d %H:%M:%S"),
                              "end_full": b.strftime("%Y-%m-%d %H:%M:%S"),
                              # horodatages ISO EXACTS des evenements start/end
                              # (pour l'edition des commentaires dans le journal)
                              "start_ts": s.strftime("%Y-%m-%dT%H:%M:%S"),
                              "end_ts": e.strftime("%Y-%m-%dT%H:%M:%S"),
                              "value": (b - a).total_seconds(),
                              "start_comment": sc,
                              "end_comment": ec})
        for ts, txt in load_notes():
            if d0 <= ts < d1:
                items.append({"note": True,
                              "ts_full": ts.strftime("%Y-%m-%d %H:%M:%S"),
                              "ts_iso": ts.strftime("%Y-%m-%dT%H:%M:%S"),
                              "comment": txt})
        items.sort(key=lambda i: i["start_full"] if "start_full" in i else i["ts_full"])
    elif period == "planning":
        # Planning de la semaine : 7 jours (lundi -> dimanche), chacun avec
        # la liste des periodes (activite) qui le chevauchent, horodatage
        # complet pour placer les rectangles sur l'axe des heures.
        d = anchor - timedelta(days=anchor.weekday())  # lundi
        for i in range(7):
            d0, d1 = day_range(d)
            day_sessions = []
            for s, e, sc, ec in sessions:
                open_session = e is None
                if open_session:
                    e = datetime.now()
                a, b = max(s, d0), min(e, d1)
                if b > a:
                    day_sessions.append({
                        "start_full": a.strftime("%Y-%m-%d %H:%M:%S"),
                        "end_full": b.strftime("%Y-%m-%d %H:%M:%S"),
                        "start": a.strftime("%H:%M"),
                        "end": e.strftime("%H:%M"),
                        "open": open_session,
                        "value": (b - a).total_seconds(),
                        "start_comment": sc,
                        "end_comment": ec,
                    })
            items.append({"date": d.strftime("%Y-%m-%d"),
                          "label": WEEKDAYS[i], "sessions": day_sessions})
            d += timedelta(days=1)
    return items


MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DASHBOARD = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>TempOtoc — Work time analysis</title>
<style>
 :root{
  --bg:#0d1117;--panel:#161b22;--panel2:#1c2128;--border:#2d333b;
  --text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--green:#7ee787;
  --amber:#e8a838;--sel:#1f6feb;--barEmpty:#30363d;--dur:#e6edf3;
  --accentDark:#2f6bb0;--greenDark:#3f9e5f;
  --shadow:0 8px 24px rgba(0,0,0,.35);--btn:#2d333b;--btnHover:#3a424d}
 body.light{
  --bg:#f6f8fa;--panel:#ffffff;--panel2:#eef1f5;--border:#d0d7de;
  --text:#1f2328;--muted:#5b6672;--accent:#2563eb;--green:#1a7f37;
  --amber:#b45309;--sel:#2563eb;--barEmpty:#d0d7de;--dur:#1f2328;
  --accentDark:#1e50a0;--greenDark:#156029;
  --shadow:0 8px 24px rgba(20,30,40,.08);--btn:#eaeef2;--btnHover:#dfe4ea}
 body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);
  margin:0;padding:24px;transition:background .25s,color .25s}
 h1{font-size:22px;margin:14px 0;font-weight:600;letter-spacing:.3px}
 h1 .logo{height:42px;width:auto;vertical-align:-14px;margin-right:10px}
 h1 span{font-size:14px;color:var(--amber);font-weight:normal}
 h1 .brand{font-family:'Segoe Script','Comic Sans MS',cursive;font-size:30px;
   font-weight:700;color:var(--amber);letter-spacing:1px}
 .bar{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
 button{background:var(--btn);color:var(--text);border:1px solid var(--border);border-radius:8px;
  padding:7px 13px;cursor:pointer;font-size:14px;transition:background .15s,transform .1s}
 button:hover{background:var(--btnHover)}
 button:active{transform:scale(.97)}
 #filt{background:var(--panel2);color:var(--text);border:1px solid var(--border);
  border-radius:8px;padding:7px 12px;font-size:14px;width:190px;outline:none}
 #filt:focus{border-color:var(--accent)}
 #filt::placeholder{color:var(--muted)}
 button.sel{background:var(--sel);border-color:var(--sel);color:#fff}
 #periodLabel{font-size:16px;font-weight:bold;min-width:160px}
 #total{color:var(--green);font-weight:bold}
 canvas{background:var(--panel);border:1px solid var(--border);border-radius:12px;
  margin-top:8px;box-shadow:var(--shadow)}
 #dayview{margin-top:14px}
 table{border-collapse:collapse;width:100%;max-width:700px;background:var(--panel);
  border:1px solid var(--border);border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}
 td,th{padding:8px 12px;border-bottom:1px solid var(--border);text-align:left;font-size:14px}
 th{color:var(--muted);background:var(--panel2);font-weight:600}
 tr:last-child td{border-bottom:none}
 tr:hover td{background:var(--panel2)}
 .hint{color:var(--muted);font-size:13px;margin-top:10px}
 a{color:var(--accent)}
 #bToggle,#bTheme{display:inline-flex;align-items:center;justify-content:center;width:44px;min-width:44px;padding:6px}
 .bars{display:inline-flex;gap:4px}
 .bars i{width:5px;height:14px;background:currentColor;border-radius:1px}
 .tri{width:0;height:0;border-left:14px solid currentColor;border-top:8px solid transparent;border-bottom:8px solid transparent}
 .modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10}
 .modal.open{display:flex;align-items:center;justify-content:center}
 .popup{background:var(--panel);border:1px solid var(--border);border-radius:14px;
  padding:18px 22px;min-width:380px;max-width:520px;box-shadow:var(--shadow)}
 .popup h2{margin:0 0 14px;font-size:16px}
 .srow{display:flex;align-items:center;justify-content:space-between;gap:14px;
  padding:10px 0;border-top:1px solid var(--border);font-size:14px}
 .toggle{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden}
 .toggle button{border:none;border-radius:0;padding:6px 14px;cursor:pointer;
  background:var(--btn);color:var(--muted);font-size:13px}
 .toggle button.on{background:var(--sel);color:#fff;font-weight:bold}
 .toggle button.off.on{background:#6e2b2b}
 .closeX{float:right;cursor:pointer;color:var(--muted);font-size:18px;margin:-6px -6px 0 0}
 .noteRow td{color:var(--amber);font-style:italic;border-bottom:1px dashed var(--border)}
 .inactRow td{color:var(--muted);font-style:italic;background:var(--panel2);
  border-bottom:1px dashed var(--border)}
 .cmtCell{cursor:text}
 .cmtCell:hover{color:var(--accent)}
 .delBtn{background:none;border:none;color:var(--muted);cursor:pointer;
  font-size:14px;line-height:1;padding:2px 6px;border-radius:4px}
 .delBtn:hover{color:#e05252;background:var(--panel2)}
 input.cmtEdit{width:100%;background:var(--panel2);color:var(--text);
  border:1px solid var(--accent);border-radius:6px;padding:4px 6px;
  font-size:14px;font-family:inherit;font-style:normal}
 .inprog{color:var(--bar);font-weight:600}
 .timeCell{cursor:pointer}
 .timeCell:hover{color:var(--accent)}
 input.timeEdit{width:100%;min-width:0;background:var(--panel2);color:var(--text);
  border:1px solid var(--accent);border-radius:6px;padding:4px 6px;
  font-size:14px;font-family:inherit;font-style:normal}
 .endCell{display:flex;align-items:center;gap:4px}
 .delEndBtn{background:var(--panel2);border:1px solid var(--border);color:var(--text);
  cursor:pointer;font-size:12px;font-weight:600;padding:2px 6px;border-radius:6px}
 .delEndBtn:hover{background:var(--btn);border-color:var(--accent)}
 textarea{width:100%;background:var(--panel2);color:var(--text);border:1px solid var(--border);
  border-radius:8px;padding:8px;font-size:14px;font-family:inherit;resize:vertical}
 #tip{display:none;position:fixed;background:var(--panel2);border:1px solid var(--border);
  border-radius:8px;padding:6px 10px;font-size:13px;max-width:320px;z-index:20;
  color:var(--text);pointer-events:none;box-shadow:var(--shadow);white-space:pre-line}
/* Vue Planning : grille 7 jours x 24h, axe des heures a gauche */
.planGrid{display:flex;margin-top:10px;user-select:none}
.planTime{width:52px;flex:none}
.planAxis{position:relative}
.planHour{position:absolute;right:6px;transform:translateY(-50%);font-size:11px;color:var(--muted);line-height:1}
.planDay{flex:1;min-width:0}
.planHead{font-size:12px;font-weight:600;text-align:center;padding:4px 0;color:var(--text)}
.planCol{position:relative;border-left:1px solid var(--border);background:var(--panel);cursor:pointer}
.planLine{position:absolute;left:0;right:0;height:1px;background:var(--border);opacity:.5}
.planHalf{opacity:.22}
.planRect{position:absolute;left:2px;right:2px;background:var(--accent);color:#fff;border-radius:4px;
  font-size:10px;line-height:1.2;padding:2px 4px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;
  box-sizing:border-box;pointer-events:auto;cursor:default}
.planOpen{background:var(--green);color:#0d1117}
.planDark{background:var(--accentDark)}
.planOpen.planDark{background:var(--greenDark)}
</style></head><body>
<h1><img class="logo" src="/logo.png" alt="TempOtoc" />&mdash; Dashboard</h1>
<div class="bar">
  <button id="bYear" onclick="setPeriod('year')">Year</button>
  <button id="bMonth" onclick="setPeriod('month')">Month</button>
  <button id="bWeek" onclick="setPeriod('week')">Week</button>
  <button id="bPlanning" onclick="setPeriod('planning')">Planning</button>
  <button id="bDay" onclick="setPeriod('day')">Day</button>
  <button onclick="shift(-1)">&larr;</button>
  <button onclick="shift(1)">&rarr;</button>
  <button onclick="goToday()">Today</button>
  <input id="filt" type="text" placeholder="Filter start comment…"
   title="Filters periods whose start comment contains this text"
   oninput="setFilt(this.value)" />
  <button onclick="openSettings()" title="Settings" aria-label="Settings"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.44-.3-.61-.12l-2.39 1.43c-.5-.35-1.05-.64-1.64-.86l-.36-2.54c-.05-.41-.37-.7-.78-.7h-3.84c-.41 0-.74.29-.79.7l-.36 2.54c-.59.22-1.13.51-1.64.86l-2.39-1.43c-.17-.18-.47-.1-.61.12L2.74 9.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.44.3.61.12l2.39-1.43c.5.35 1.05.64 1.64.86l.36 2.54c.05.41.37.7.78.7h3.84c.41 0 .74-.29.79-.7l.36-2.54c.59-.22 1.13-.51 1.64-.86l2.39 1.43c.17.18.47.1.61-.12l1.92-3.32c.12-.21.08-.47-.12-.61l-2.03-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg></button>
  <button id="bToggle" onclick="toggleMeasure()" title="Stop measuring" style="background:#6e2b2b;border-color:#8a3a3a;color:#fff"><span class="bars"><i></i><i></i></span></button>
  <span id="stateLabel"></span>
  <span id="periodLabel"></span>
  <span id="total"></span>
  <button id="bTheme" onclick="toggleTheme()" title="Day / night mode" aria-label="Day / night mode"><svg id="icoTheme" viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></button>
</div>
<canvas id="chart" width="1100" height="320"></canvas>
<div id="dayview"></div>
<div id="hint" class="hint">Click a bar to explore the lower level (year → month → day).</div>
<div id="settingsModal" class="modal">
 <div class="popup">
  <span class="closeX" onclick="closeSettings()" title="Close">&times;</span>
  <h2>Settings</h2>
  <div class="srow">
   <span>Launch at Windows startup</span>
   <span class="toggle">
    <button id="tYes" onclick="setAutostart(true)">Yes</button>
    <button id="tNo" onclick="setAutostart(false)">No</button>
   </span>
  </div>
  <div id="autostartMsg" class="hint"></div>
  <div class="srow">
   <button id="killBtn" onclick="killProcess()">Kill process</button>
  </div>
  <div id="killMsg" class="hint"></div>
 </div>
</div>
<div id="reasonModal" class="modal">
 <div class="popup">
  <h2 id="reasonTitle"></h2>
  <textarea id="reasonText" rows="3" placeholder="e.g. lunch break, switching project…"></textarea>
  <div class="srow">
   <button onclick="submitReason(true)">Confirm</button>
   <button onclick="submitReason(false)">Cancel</button>
  </div>
  <div id="reasonMsg" class="hint"></div>
 </div>
</div>
<div id="tip"></div>
<script>
const MONTHS=["January","February","March","April","May","June","July",
 "August","September","October","November","December"];
const S={period:"year",anchor:new Date().toISOString().slice(0,10),filt:""};
const cv=document.getElementById("chart"),ctx=cv.getContext("2d");
let rects=[];
function fmt(sec){const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60);
 return h+"h "+String(m).padStart(2,"0")+"min";}
function fmtHM(sec){const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60);
 return String(h).padStart(2,"0")+":"+String(m).padStart(2,"0");}
function setPeriod(p){S.period=p;try{localStorage.setItem("period",p);}catch(e){}load();}
function setFilt(v){
 // Filtre par commentaire de debut : vide = aucune restriction. Meme
 // filtre applique a la liste et aux stats (le serveur filtre les sessions).
 S.filt=v;try{localStorage.setItem("filt",v);}catch(e){}load();}
function goToday(){S.anchor=new Date().toISOString().slice(0,10);load();}
function toggleMeasure(){
 // Ouvre la popup de raison : commentaire optionnel justifiant l'arret
 // (ex. pause dejeuner) ou la reprise (ex. changement de dossier).
 // Le commentaire de DEBUT de la derniere periode d'activite est pre-rempli
 // pour Demarrer ; le popup Arreter n'est pas pre-rempli.
 const measuring=document.getElementById("bToggle").title.startsWith("Stop");
 document.getElementById("reasonTitle").textContent=
  measuring?"Stop measuring — reason (optional)"
           :"Start measuring — reason (optional)";
 document.getElementById("reasonText").value="";
 document.getElementById("reasonMsg").textContent="";
 document.getElementById("reasonModal").classList.add("open");
 if(!measuring){
  fetch("/api/lastcomment?mode=start").then(r=>r.json()).then(d=>{
   // ne pre-remplit que si l'utilisateur n'a rien tape entre-temps
   if(document.getElementById("reasonModal").classList.contains("open")&&
      !document.getElementById("reasonText").value)
    document.getElementById("reasonText").value=d.comment||"";
  }).catch(()=>{});
 }
}
function submitReason(ok){
 const modal=document.getElementById("reasonModal");
 modal.classList.remove("open");
 if(!ok)return;
 const c=document.getElementById("reasonText").value;
 fetch("/api/toggle",{method:"POST",body:JSON.stringify({comment:c})})
  .then(r=>r.json()).then(d=>{
   setBtn(d.measuring);
   load();
  }).catch(()=>{});
}
function setBtn(measuring){
 const b=document.getElementById("bToggle");
 const l=document.getElementById("stateLabel");
 if(measuring){
  b.innerHTML='<span class="bars"><i></i><i></i></span>';  /* deux barres verticales = pause */
  b.title="Stop measuring";
  b.style.background="#6e2b2b";b.style.borderColor="#8a3a3a";b.style.color="#fff";
  l.textContent="";
 }else{
  b.innerHTML='<span class="tri"></span>';  /* triangle vers la droite = lecture/reprise */
  b.title="Start measuring";
  b.style.background="#1d4a2b";b.style.borderColor="#2e7d46";b.style.color="#fff";
  l.textContent="— Measuring paused";
 }
}
function openSettings(){
 document.getElementById("settingsModal").classList.add("open");
 fetch("/api/autostart").then(r=>r.json()).then(d=>{
  showAutostart(d.enabled,"");
 }).catch(()=>{});
}
function closeSettings(){
 document.getElementById("settingsModal").classList.remove("open");
}
function showAutostart(on,msg){
 document.getElementById("tYes").className=on?"on":"";
 document.getElementById("tNo").className=on?"off":"on";
 document.getElementById("autostartMsg").textContent=msg;
}
function setAutostart(on){
 fetch("/api/autostart",{method:"POST",body:JSON.stringify({on:on})})
  .then(r=>r.json()).then(d=>{
   showAutostart(d.enabled, d.ok?
    (d.enabled?"TempOtoc will launch at Windows startup."
             :"TempOtoc will no longer launch at startup.")
    :("Error: "+(d.error||"unknown")));
  }).catch(()=>showAutostart(null,"Connection error"));
}
function killProcess(){
 if(!confirm("Close TempOtoc? The current session end time is saved, then the process exits."))return;
 document.getElementById("killMsg").textContent="Shutting down cleanly…";
 fetch("/api/shutdown",{method:"POST"})
  .then(()=>{closeSettings();document.getElementById("killMsg").textContent=
    "TempOtoc is closing. Reloading the dashboard to show the last period…";}
  ).catch(()=>document.getElementById("killMsg").textContent=
    "TempOtoc is not responding.");
 // L'exe enregistre l'heure de fin puis reste vivant 3 s : on recharge a
 // 1,5 s pour afficher la periode finalisee, et la page garde cet affichage
 // quand le serveur meurt ensuite.
 setTimeout(()=>location.reload(),1500);
}
function shift(n){const d=new Date(S.anchor+"T12:00:00");
 if(S.period=="year")d.setFullYear(d.getFullYear()+n);
 if(S.period=="month")d.setMonth(d.getMonth()+n);
 if(S.period=="week"||S.period=="planning")d.setDate(d.getDate()+7*n);
 if(S.period=="day")d.setDate(d.getDate()+n);
 S.anchor=d.toISOString().slice(0,10);load();}
function load(){
 // L'onglet utilise est memorise (localStorage) : il est restaure par defaut
 // quand le dashboard est relance.
 try{localStorage.setItem("period",S.period);}catch(e){}
 ["bYear","bMonth","bWeek","bPlanning","bDay"].forEach(id=>document.getElementById(id).className="");
 document.getElementById("b"+S.period[0].toUpperCase()+S.period.slice(1)).className="sel";
 const u="/api/stats?period="+S.period+"&date="+S.anchor+"&filt="+encodeURIComponent(S.filt);
 fetch(u).then(r=>r.json()).then(render).catch(()=>{});
 fetch("/api/state").then(r=>r.json()).then(d=>setBtn(d.measuring)).catch(()=>{});
}
function render(items){
 rects=[];ctx.clearRect(0,0,cv.width,cv.height);
 // En vue Planning, le canvas est vide : on le masque pour liberer toute
 // la hauteur d'ecran au profit de la grille.
 cv.style.display=(S.period=="planning")?"none":"block";
 document.getElementById("hint").style.display=(S.period=="planning")?"none":"block";
 const T=themeColors();
 const d=new Date(S.anchor+"T12:00:00");
 let label="";
 if(S.period==="year")label="Year "+d.getFullYear();
 if(S.period==="month")label=MONTHS[d.getMonth()]+" "+d.getFullYear();
 if(S.period=="week"||S.period=="planning"){const m=new Date(d);m.setDate(m.getDate()+6);
  label="Week of "+d.toLocaleDateString("en")+" to "+m.toLocaleDateString("en");}
 if(S.period=="day")label=d.toLocaleDateString("en",{weekday:"long",day:"numeric",month:"long",year:"numeric"});
 document.getElementById("periodLabel").textContent=label;
 const total=S.period=="planning"
  ?items.reduce((a,b)=>a+b.sessions.reduce((x,s)=>x+s.value,0),0)
  :items.reduce((a,b)=>a+b.value,0);
 document.getElementById("total").textContent="Total: "+fmt(total);
 const dv=document.getElementById("dayview");dv.innerHTML="";
 if(S.period=="planning"){renderPlanning(items,dv);return;}
 if(S.period=="day"){renderDayBars(items);renderDay(items,dv);return;}
 const n=items.length,pad=40,bw=Math.min(90,(cv.width-2*pad)/n-6);
 const max=Math.max(...items.map(i=>i.value),60);
 items.forEach((it,i)=>{
  const x=pad+i*(bw+6),h=(it.value/max)*(cv.height-70);
  const y=cv.height-40-h;
  ctx.fillStyle=it.value>0?T.bar:T.empty;
  ctx.fillRect(x,y,bw,h);
  ctx.fillStyle=T.label;ctx.font="11px sans-serif";ctx.textAlign="center";
  ctx.fillText(it.label,x+bw/2,cv.height-24);
  if(it.value>0){ctx.fillStyle=T.dur;ctx.fillText(fmt(it.value),x+bw/2,y-6);}
  rects.push({x,y,w:bw,h,cv:cv.height-40,item:it});
 });
}
function themeColors(){
 const light=document.body.classList.contains("light");
 return light
  ?{bar:"#2563eb",empty:"#d0d7de",label:"#5b6672",dur:"#1f2328",amber:"#b45309",grey:"#c9d1d9"}
  :{bar:"#58a6ff",empty:"#30363d",label:"#8b949e",dur:"#e6edf3",amber:"#e8a838",grey:"#30363d"};
}
function toggleTheme(){
 const light=document.body.classList.toggle("light");
 try{localStorage.setItem("theme",light?"light":"dark");}catch(e){}
 document.getElementById("icoTheme").innerHTML=light
  ?'<circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4" stroke="currentColor" stroke-width="2" fill="none"/>'
  :'<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';
 load();  // re-charge pour redessiner les canvas avec les couleurs du theme
}
function renderDayBars(items){
 // 24 barres verticales : temps d'activite cumule dans chaque heure
 const T=themeColors();
 const hours=new Array(24).fill(0);
 items.forEach(s=>{
  if(s.note)return;
  const a=new Date(s.start_full),b=new Date(s.end_full);
  for(let h=0;h<24;h++){
   const h0=new Date(a.getFullYear(),a.getMonth(),a.getDate(),h,0,0);
   const h1=new Date(h0.getTime()+3600000);
   const m=Math.max(a,h0),n=Math.min(b,h1);
   if(n>m)hours[h]+=(n-m)/1000; // secondes
  }
 });
 const n=24,pad=40,bw=Math.min(90,(cv.width-2*pad)/n-6);
 const max=Math.max(...hours,60);
 hours.forEach((v,i)=>{
  const x=pad+i*(bw+6),hgt=(v/max)*(cv.height-70);
  const y=cv.height-40-hgt;
  ctx.fillStyle=v>0?T.bar:T.empty;
  ctx.fillRect(x,y,bw,hgt);
  ctx.fillStyle=T.label;ctx.font="11px sans-serif";ctx.textAlign="center";
  ctx.fillText(i+"h",x+bw/2,cv.height-24);
  if(v>0){ctx.fillStyle=T.dur;ctx.fillText(fmt(v),x+bw/2,y-6);}
 });
}
function renderPlanning(items,dv){
 // Planning de la semaine : axe des heures (00h-23h, pas de 1/2h) a gauche,
 // 7 colonnes (lundi -> dimanche). Chaque periode = rectangle de son heure
 // de debut a son heure de fin, avec le commentaire de debut a l'interieur.
 // Survol du rectangle -> tooltip (debut, fin, duree, commentaire).
 // Clic sur une colonne -> vue Jour de ce jour.
 // SLOT est calcule dynamiquement pour que la grille remplisse toute la
 // hauteur d'ecran disponible (sous la barre d'outils). SLOT = pixels par
 // heure ; la journee = 24 SLOT.
 const top=dv.getBoundingClientRect().top;
 const avail=window.innerHeight-top-16;  // marge du bas
 const SLOT=Math.max(20,Math.floor(avail/24)),H=24*SLOT;
 let html='<div class="planGrid"><div class="planTime">';
 html+='<div class="planHead">&nbsp;</div>';
 html+='<div class="planAxis" style="height:'+H+'px">';
 for(let h=0;h<24;h++)html+='<div class="planHour" style="top:'+(h*SLOT)+'px">'+h+"h</div>";
 html+="</div>";
 html+="</div>";
 // Table de couleurs par hash : hash(3 premieres + 3 dernieres lettres du
 // commentaire de debut) -> RGB. Les commentaires distincts sont tries par
 // duree cumulee decroissante sur toute la semaine ; les plus longs
 // prennent les premieres couleurs (bleu -> bleu-vert -> vert). Les 12
 // premiers hashes recoivent la palette, au-dela la couleur "grab all".
 const PALETTE=[[0,0,64],[0,64,64],[0,64,0],[0,0,128],[0,128,128],[0,128,0],
  [0,0,192],[0,192,192],[0,192,0]];
 const GRAB=[128,192,192];
 function hashKey(c){const k=c.slice(0,3)+c.slice(-3);let h=0;
  for(let i=0;i<k.length;i++)h=(h*31+k.charCodeAt(i))>>>0;return h;}
 const durSum={};
 items.forEach(d=>d.sessions.forEach(s=>{
  const k=hashKey(s.start_comment||"");
  durSum[k]=(durSum[k]||0)+s.value;
 }));
 const color={};
 Object.keys(durSum).sort((a,b)=>durSum[b]-durSum[a]).forEach((k,i)=>{
  color[k]=i<PALETTE.length?PALETTE[i]:GRAB;
 });
 items.forEach(day=>{
  html+='<div class="planDay"><div class="planHead">'+day.label+"</div>";
  html+='<div class="planCol" style="height:'+H+'px" data-date="'+day.date+'" title="Click to open this day">';
  for(let h=1;h<24;h++)html+='<div class="planLine" style="top:'+(h*SLOT)+'px"></div>';
  for(let h=0.5;h<24;h+=1)html+='<div class="planLine planHalf" style="top:'+(h*SLOT)+'px"></div>';
  // rectangles : empilement en couloirs si des periodes se chevauchent
  const ss=day.sessions.slice().sort((a,b)=>a.start_full<b.start_full?-1:1);
  const lanes=[];  // fin de la derniere periode de chaque couloir
  ss.forEach(s=>{
   let li=lanes.findIndex(end=>end<=s.start_full);
   if(li<0){li=lanes.length;lanes.push(0);}
   lanes[li]=s.end_full;
   s._lane=li;
  });
  const nl=Math.max(lanes.length,1);
  ss.forEach(s=>{
   const a=new Date(s.start_full),b=new Date(s.end_full);
   const top=(a.getHours()+a.getMinutes()/60)*SLOT;
   const hgt=Math.max((b-a)/1000/60/60*SLOT,10);
   const left=(s._lane/nl*100)+"%",width=(100/nl)+"%";
   const col=color[hashKey(s.start_comment||"")]||GRAB;
   const rgb="rgb("+col[0]+","+col[1]+","+col[2]+")";
   const dur=fmtHM(s.value);
   const tipTxt=s.start+"->"+(s.open?"in progress":s.end)+" ("+dur+") "+(s.start_comment||"");
   html+='<div class="planRect" style="top:'+top+
    'px;height:'+hgt+'px;left:calc('+left+" + 2px);width:calc("+width+' - 4px);background:'+rgb+';"'+
    ' data-tip="'+esc(tipTxt)+'">'+esc(s.start_comment||s.start)+"</div>";
  });
  html+="</div></div>";
 });
 html+="</div>";
 dv.innerHTML=html;
 // tooltip au survol des rectangles
 const tip=document.getElementById("tip");
 dv.querySelectorAll(".planRect").forEach(el=>{
  el.addEventListener("mousemove",e=>{
   tip.textContent=el.dataset.tip;tip.style.display="block";
   tip.style.left=(e.clientX+12)+"px";tip.style.top=(e.clientY-30)+"px";
  });
  el.addEventListener("mouseleave",()=>{tip.style.display="none";});
 });
 // clic sur une colonne de jour -> vue Jour
 dv.querySelectorAll(".planCol").forEach(el=>{
  el.addEventListener("click",e=>{
   if(e.target.closest(".planRect"))return;
   S.period="day";S.anchor=el.dataset.date;load();
  });
 });
}
function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
// Edition d'un commentaire dans la liste du jour : clic sur la cellule ->
// champ de saisie. Entree ou clic hors cellule = enregistre dans le journal
// (POST /api/edit_comment) puis re-charge la liste. Echap = annule.
let editing=false;
// Suppression d'une periode : clic sur ✕ -> confirmation -> POST
// /api/delete_session (supprime le 'start' et son 'end' apparie) puis re-charge.
function delRow(ts){
 if(editing)return;
 if(!confirm("Delete this period from the journal?"))return;
 fetch("/api/delete_session",{method:"POST",
  body:JSON.stringify({ts:ts})}).then(()=>load());
}
// Suppression de la ligne d'inactivite : retire l'evenement 'end' du journal
// (la fin de periode). La periode d'activite correspondante redevient ouverte.
function delEnd(ts){
 if(editing)return;
 if(!confirm("Delete the period end (inactivity row) from the journal?"))return;
 fetch("/api/delete_session",{method:"POST",
  body:JSON.stringify({ts:ts})}).then(()=>load());
}
function editCmt(td){
 if(editing)return;
 editing=true;
 const old=td.textContent;
 const inp=document.createElement("input");
 inp.type="text";inp.value=old;inp.className="cmtEdit";
 td.textContent="";td.appendChild(inp);inp.focus();
 const done=(save)=>{
  inp.remove();editing=false;
  if(save){
   fetch("/api/edit_comment",{method:"POST",
    body:JSON.stringify({ts:td.dataset.ts,type:td.dataset.type,comment:inp.value})})
    .then(()=>load());
  }else{
   td.textContent=old;
  }
 };
 inp.addEventListener("keydown",e=>{
  if(e.key==="Enter"){e.preventDefault();done(true);}
  if(e.key==="Escape"){e.preventDefault();done(false);}
 });
 inp.addEventListener("blur",()=>done(true));
}
// Edition des heures de debut/fin dans la liste du jour : clic sur la cellule
// d'heure -> champ date+heure complet. Entree = enregistre (POST
// /api/edit_time), Echap = annule, blur = enregistre. Le serveur refuse:
// horodatage futur, start>=end, chevauchement avec la periode voisine
// (pairing du journal casse).
function editTime(td){
 if(editing)return;
 editing=true;
 const oldFull=td.dataset.full;  // 'YYYY-MM-DD HH:MM:SS'
 const oldText=td.textContent;
 const inp=document.createElement("input");
 inp.type="datetime-local";
 inp.className="timeEdit";
 inp.value=oldFull.replace(" ","T").slice(0,16);
 td.textContent="";td.appendChild(inp);inp.focus();
 const done=(save)=>{
  inp.remove();editing=false;
  if(!save){td.textContent=oldText;return;}
  const nv=inp.value;
  if(!nv){td.textContent=oldText;return;}
  const ts=nv.length===16?nv+":00":nv;
  if(ts===oldFull.replace(" ","T")){td.textContent=oldText;return;}
  fetch("/api/edit_time",{method:"POST",
   body:JSON.stringify({ts:td.dataset.ts,type:td.dataset.type,new_ts:ts})})
   .then(r=>r.json()).then(d=>{
    if(!d.ok)alert("Shift refused: future time, overlap with a neighboring period, or pairing constraint.");
    load();
   }).catch(()=>{});
 };
 inp.addEventListener("keydown",e=>{
  if(e.key==="Enter"){e.preventDefault();done(true);}
  if(e.key==="Escape"){e.preventDefault();done(false);}
 });
 inp.addEventListener("blur",()=>done(true));
}
function renderDay(items,dv){
 // frise 24h d'abord, liste détaillée des périodes APRÈS la frise
 let html='<canvas id="tl" width="1100" height="60"></canvas>';
 dv.innerHTML=html;
 const tl=document.getElementById("tl");
 const t=tl.getContext("2d");
 const T=themeColors();
 t.fillStyle=T.label;t.font="10px sans-serif";
 for(let h=0;h<=24;h+=2){const x=h/24*tl.width;
  t.fillRect(x,0,1,8);t.fillText(h+"h",x+2,20);}
 const tlSegs=[];  // segments de la frise pour les tooltips au survol
 items.forEach(s=>{
  if(s.note){
   const a=new Date(s.ts_full);
   const x=(a.getHours()+a.getMinutes()/60)/24*tl.width;
   t.fillStyle=T.amber;t.fillRect(x-1,28,2,20);
   tlSegs.push({x0:x-3,x1:x+3,text:s.comment});
   return;
  }
  const a=new Date(s.start_full),b=new Date(s.end_full);
  const x0=(a.getHours()+a.getMinutes()/60)/24*tl.width;
  const x1=Math.max((b.getHours()+b.getMinutes()/60)/24*tl.width,x0+2);
  t.fillStyle=T.bar;t.fillRect(x0,28,x1-x0,20);
  const tips=[];
  if(s.start_comment)tips.push(s.start_comment);
  if(s.end_comment)tips.push("End: "+s.end_comment);
  if(tips.length)tlSegs.push({x0:x0,x1:x1,text:tips.join(" — ")});
  // periode d'inactivite avant cette session : commentaire de fin de la
  // session precedente (explique l'arret) -> zone grise entre les deux
  const prev=items.filter(i=>!i.note&&i.end_full&&i.end_full<=s.start_full).pop();
  if(prev&&prev.end_comment){
   const pe=new Date(prev.end_full);
   const px0=(pe.getHours()+pe.getMinutes()/60)/24*tl.width;
   t.fillStyle=T.grey;t.fillRect(px0,28,Math.max(x0-px0,2),20);
   tlSegs.push({x0:px0,x1:x0,text:"Idle: "+prev.end_comment});
  }
 });
 const tip=document.getElementById("tip");
 tl.addEventListener("mousemove",e=>{
  const r=tl.getBoundingClientRect(),x=e.clientX-r.left;
  const seg=tlSegs.find(s=>x>=s.x0&&x<=s.x1);
  if(seg){tip.textContent=seg.text;tip.style.display="block";
   tip.style.left=(e.clientX+12)+"px";tip.style.top=(e.clientY-30)+"px";}
  else tip.style.display="none";
 });
 tl.addEventListener("mouseleave",()=>{tip.style.display="none";});
 // liste détaillée, après la frise
 let tb="<table><tr><th>Start</th><th>End</th><th>Duration</th><th>Comment</th><th></th></tr>";
 // croix de suppression de l'heure de fin : UNIQUEMENT sur la derniere
 // periode commencee ET terminee. Si la derniere periode de la liste est
 // ouverte (session en cours), elle n'est pas terminee -> pas de croix.
 const lastIdx=items.reduce((acc,s,j)=>(!s.note?j:acc),-1);
 const showEndDel=lastIdx>=0&&!items[lastIdx].open;
 items.forEach((s,i)=>{
  if(s.note){tb+='<tr class="noteRow"><td colspan="4" class="cmtCell" data-ts="'+s.ts_iso+
      '" data-type="note" title="Click to edit" onclick="editCmt(this)">'+esc(s.comment)+"</td>"+
      '<td><button class="delBtn" title="Delete note" onclick="delRow(\''+s.ts_iso+'\')">✕</button></td></tr>';return;}
  // commentaire de fin : affiche sur la ligne d'inactivite en dessous, pas ici
  const cmt=s.start_comment?esc(s.start_comment):"";
  const endTxt=s.open?"<span class=\"inprog\">In progress</span>":s.end;
  // cellules d'heure editeables (clic -> champ date+heure complet). Les
  // horodatages exacts des evenements (start_ts/end_ts) servent de base a
  // l'edition. La derniere periode fermee porte une croix de suppression de
  // son heure de fin (delEnd -> l'evenement 'end' est retire du journal).
  const startCell='<td class="timeCell" data-ts="'+s.start_ts+'" data-type="start"'+
    ' data-full="'+s.start_ts.replace("T"," ")+'" title="Click to edit start time"'+
    ' onclick="editTime(this)">'+s.start+"</td>";
  const endCell=s.open?'<td>'+endTxt+"</td>":
    '<td class="timeCell" data-ts="'+s.end_ts+'" data-type="end"'+
    ' data-full="'+s.end_ts.replace("T"," ")+'" title="Click to edit end time"'+
    ' onclick="editTime(this)">'+endTxt+
    // croix de suppression de l'heure de fin : uniquement sur la derniere
    // periode commencee ET terminee (pas si la session est en cours).
    (i===lastIdx&&showEndDel?'<button class="delEndBtn" title="Delete end time"'+
      ' onclick="event.stopPropagation();delEnd(\''+s.end_ts+'\')">✕</button>':"")+
    "</td>";
  tb+="<tr>"+startCell+endCell+"<td>"+fmt(s.value)+
      '</td><td class="cmtCell" data-ts="'+s.start_ts+'" data-type="start" title="Click to edit" onclick="editCmt(this)">'+cmt+"</td>"+
      '<td><button class="delBtn" title="Delete period" onclick="delRow(\''+s.start_ts+'\')">✕</button></td></tr>';
  // periode d'inactivite qui suit la periode : duree + commentaire de fin
  // (uniquement si commentaire). Duree = entre la fin de cette periode et le
  // debut de la suivante, ou jusqu'a maintenant si c'est la derniere du jour.
  if(s.end_comment){
   let nx=null;
   for(let j=i+1;j<items.length;j++){if(!items[j].note){nx=items[j];break;}}
   let sec=0;
   if(nx)sec=(new Date(nx.start_full)-new Date(s.end_full))/1000;
   else{const now=new Date();
    // date locale d'aujourd'hui, zero-paddee (pas toISOString, qui est
    // en UTC et changerait de jour le soir)
    const p=n=>String(n).padStart(2,"0");
    const today=now.getFullYear()+"-"+p(now.getMonth()+1)+"-"+p(now.getDate());
    if(s.end_full.slice(0,10)===today)sec=(now-new Date(s.end_full))/1000;}
   if(sec>0)tb+='<tr class="inactRow"><td></td><td></td><td>'+fmt(sec)+
       '</td><td class="cmtCell" data-ts="'+s.end_ts+'" data-type="end" title="Click to edit" onclick="editCmt(this)">'+esc(s.end_comment)+"</td>"+
       '<td><button class="delBtn" title="Delete period end" onclick="delEnd(\''+s.end_ts+'\')">✕</button></td></tr>';
  }
 });
 tb+="</table>";
 dv.insertAdjacentHTML("beforeend",tb);
}
cv.addEventListener("click",e=>{
 const r=cv.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;
 for(const rc of rects){if(x>=rc.x&&x<=rc.x+rc.w&&y>=rc.y&&y<=rc.cv){
  const it=rc.item;
  if(S.period==="year"){S.period="month";S.anchor=it.date;}
  else if(S.period==="month"||S.period==="week"){S.period="day";S.anchor=it.date;}
  load();return;}}
});
// Applique le theme : mode JOUR par defaut, le choix de l'utilisateur est
// sauvegarde (localStorage) et restaure au rechargement.
(function(){
 let th=null;try{th=localStorage.getItem("theme");}catch(e){}
 if(th!=="dark"){document.body.classList.add("light");
  document.getElementById("icoTheme").innerHTML='<circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4" stroke="currentColor" stroke-width="2" fill="none"/>';}
})();
// Restaure le dernier onglet utilise (localStorage) : il est affiche par
// defaut quand le dashboard est relance.
(function(){
 let p=null;try{p=localStorage.getItem("period");}catch(e){}
 if(p==="year"||p==="month"||p==="week"||p==="planning"||p==="day")S.period=p;
 // Restaure le filtre de commentaire de debut (localStorage) : il est
 // applique par defaut quand le dashboard est relance.
 let f=null;try{f=localStorage.getItem("filt");}catch(e){}
 if(f){S.filt=f;document.getElementById("filt").value=f;}
})();
load();
// Rafraichissement automatique : le collecteur sonde toutes les 10 s,
// le dashboard se re-charge toutes les 60 s sans F5. Suspendu pendant
// l'edition d'un commentaire (la liste re-chargee effacerait le champ).
setInterval(()=>{if(!editing)load();},60000);
// Redimensionnement de la fenetre : la vue Planning se re-render pour
// recalculer SLOT et remplir la nouvelle hauteur d'ecran disponible.
let rsT;window.addEventListener("resize",()=>{
 if(S.period=="planning"){clearTimeout(rsT);rsT=setTimeout(load,120);}
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = DASHBOARD.encode("utf-8")
        elif parsed.path == "/logo.png":
            p = BASE / "logo.png"
            if p.exists():
                body = p.read_bytes()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return
        elif parsed.path == "/api/stats":
            q = parse_qs(parsed.query)
            period = q.get("period", ["year"])[0]
            date = q.get("date", [""])[0]
            filt = q.get("filt", [""])[0]
            try:
                anchor = datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                anchor = datetime.now()
            body = json.dumps(build_stats(period, anchor, filt)).encode("utf-8")
        elif parsed.path == "/api/state":
            body = json.dumps({"measuring": MEASURING}).encode("utf-8")
        elif parsed.path == "/api/lastcomment":
            q = parse_qs(parsed.query)
            mode = q.get("mode", ["end"])[0]
            comment = last_start_comment() if mode == "start" else last_end_comment()
            body = json.dumps({"comment": comment}).encode("utf-8")
        elif parsed.path == "/api/autostart":
            body = json.dumps({"enabled": autostart_enabled()}).encode("utf-8")
        elif parsed.path == "/api/shutdown":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            # Finalise la session en cours (heure de fin enregistree tout de
            # suite), puis arret propre planifie a 3 s : le dashboard a le
            # temps de se recharger (serveur vivant) avec la periode finalisee.
            if STATE:
                log_event("end", datetime.now())
            schedule_shutdown(3.0)
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        ctype = "text/html; charset=utf-8" if parsed.path == "/" else "application/json"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/autostart":
            length = int(self.headers.get("Content-Length", 0))
            try:
                on = bool(json.loads(self.rfile.read(length).decode("utf-8")).get("on"))
                set_autostart(on)
                result = {"ok": True, "enabled": autostart_enabled()}
            except Exception as e:
                result = {"ok": False, "enabled": autostart_enabled(),
                          "error": str(e)}
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/toggle":
            length = int(self.headers.get("Content-Length", 0))
            comment = ""
            if length:
                try:
                    comment = json.loads(
                        self.rfile.read(length).decode("utf-8")).get("comment", "")
                except Exception:
                    pass
            measuring = toggle_measure(comment)
            body = json.dumps({"measuring": measuring}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/edit_comment":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                ok = edit_comment(req.get("ts", ""), req.get("type", ""),
                                  req.get("comment", ""))
                result = {"ok": ok}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/edit_time":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                ok = edit_time(req.get("ts", ""), req.get("type", ""),
                               req.get("new_ts", ""))
                result = {"ok": ok}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/delete_session":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                ok = delete_session(req.get("ts", ""))
                result = {"ok": ok}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/shutdown":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            # Finalise la session en cours (heure de fin enregistree tout de
            # suite), puis arret propre planifie a 3 s : le dashboard a le
            # temps de se recharger (serveur vivant) avec la periode finalisee.
            if STATE:
                log_event("end", datetime.now())
            schedule_shutdown(3.0)
        else:
            self.send_response(404)
            self.end_headers()


STATE = False
MEASURING = True


def toggle_measure(comment=""):
    # Pause/reprise de la mesure. En pause : plus d'evenements, la session
    # ouverte est fermee a l'heure courante (le commentaire justifie l'arret :
    # il sera affiche entre la session et la periode d'inactivite suivante).
    # A la reprise : nouvelle session si une activite est en cours ; le
    # commentaire justifie la reprise (ex. changement de dossier).
    # Si la session est deja fermee (arret par inactivite) ou si la reprise
    # ne demarre pas (inactivite), le commentaire est journalise en 'note'.
    global STATE, MEASURING
    comment = (comment or "").strip()
    if MEASURING:
        if STATE:
            log_event("end", datetime.now(), comment)
        elif comment:
            log_event("note", datetime.now(), comment)
        STATE = False
        MEASURING = False
    else:
        MEASURING = True
        if idle_seconds() < IDLE_THRESHOLD:
            log_event("start", datetime.now(), comment)
            STATE = True
        elif comment:
            log_event("note", datetime.now(), comment)
    return MEASURING


def safe_print(*a):
    # En mode --noconsole, stdout n'existe pas : ne pas crasher.
    if sys.stdout is not None:
        print(*a)


def run_msg_window():
    # Fenetre de messages cachee (hwnd=None) : recoit WM_ENDSESSION
    # (logoff / arret du PC) et WM_QUIT (arret demande par le dashboard).
    # En mode sans console, c'est le canal d'arret propre : il enregistre
    # le "end" puis termine le process.
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong,  # LRESULT (LONG_PTR)
                                 ctypes.wintypes.HWND,
                                 ctypes.wintypes.UINT,
                                 ctypes.wintypes.WPARAM,
                                 ctypes.wintypes.LPARAM)
    ctypes.windll.user32.DefWindowProcW.restype = ctypes.c_longlong
    ctypes.windll.user32.DefWindowProcW.argtypes = [
        ctypes.wintypes.HWND, ctypes.wintypes.UINT,
        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == 0x0016:  # WM_ENDSESSION (wparam != 0 = session arretee)
            if wparam:
                if STATE:
                    log_event("end", datetime.now())
                ctypes.windll.kernel32.ExitProcess(0)
            return 1
        if msg == 0x0012:  # WM_QUIT : arret propre demande
            if STATE:
                log_event("end", datetime.now())
            ctypes.windll.kernel32.ExitProcess(0)
            return 1
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wnd_proc)
    main._wndproc = proc  # referencer pour eviter le GC

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.wintypes.UINT),
                    ("style", ctypes.c_uint),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.wintypes.HINSTANCE),
                    ("hIcon", ctypes.wintypes.HICON),
                    ("hCursor", ctypes.wintypes.HANDLE),
                    ("hBrush", ctypes.wintypes.HANDLE),
                    ("lpszMenuName", ctypes.wintypes.LPCWSTR),
                    ("lpszClassName", ctypes.wintypes.LPCWSTR),
                    ("hIconSm", ctypes.wintypes.HICON)]

    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(wc)
    wc.lpfnWndProc = proc
    wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
    wc.lpszClassName = "TempotocMsg"
    if not ctypes.windll.user32.RegisterClassExW(ctypes.byref(wc)):
        return
    hwnd = ctypes.windll.user32.CreateWindowExW(
        0, "TempotocMsg", "TempOtoc", 0,
        0, 0, 0, 0, None, None, None, None)
    if not hwnd:
        return
    main._hwnd = hwnd
    msg = ctypes.wintypes.MSG()
    while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
        ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
        ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
    # GetMessageW retourne 0 sur WM_QUIT (arret demande par le dashboard)
    # sans dispatcher le message : on enregistre le "end" ici.
    if STATE:
        log_event("end", datetime.now())
    ctypes.windll.kernel32.ExitProcess(0)


def poll_loop():
    global STATE
    while True:
        time.sleep(POLL_SECONDS)
        if not MEASURING:
            continue
        idle = idle_seconds()
        now = datetime.now()
        if STATE and idle >= IDLE_THRESHOLD:
            # derniere entree = now - idle (<= H-5min, plus precis)
            end_ts = now - timedelta(seconds=idle)
            log_event("end", end_ts)
            STATE = False
            safe_print("[%s] INACTIVITE detectee, fin enregistree a %s"
                       % (now.strftime("%H:%M:%S"), end_ts.strftime("%H:%M:%S")))
        elif not STATE and idle < IDLE_THRESHOLD:
            # Reprise automatique : le commentaire de DEBUT de la derniere
            # periode d'activite est repris et enregistre sur le 'start'.
            log_event("start", now, last_start_comment())
            STATE = True
            safe_print("[%s] REPRISE d'activite" % now.strftime("%H:%M:%S"))


def main():
    global STATE
    STATE = idle_seconds() < IDLE_THRESHOLD
    if STATE:
        log_event("start", datetime.now())

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()

    # Thread principal : boucle de messages de la fenetre cachee. En mode
    # sans console, aucune fenetre n'est creee : le process est completement
    # masque. La boucle recoit WM_ENDSESSION (logoff / arret du PC) et
    # WM_QUIT (arret demande par le dashboard).
    run_msg_window()


if __name__ == "__main__":
    main()
