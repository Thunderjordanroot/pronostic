#!/usr/bin/env python3
"""
safe_picks.py : sélection quotidienne de pronostics "réguliers", suivi et tableau de bord.

Source de données : API-Football (https://www.api-football.com), clé dans la variable
d'environnement API_FOOTBALL_KEY.

Commandes :
  python safe_picks.py picks                      # matchs restants d'aujourd'hui
  python safe_picks.py picks --now                # en direct + à venir jusqu'à 06:00
  python safe_picks.py picks --now --until 03:00  # idem, jusqu'à 03:00
  python safe_picks.py picks --from 18:00 --to 23:59   # un créneau précis
  python safe_picks.py update                     # met à jour les résultats des pronostics passés
  python safe_picks.py dashboard                  # génère docs/index.html
  python safe_picks.py export                     # génère docs/fiche.json à m'envoyer
  python safe_picks.py daily                      # update + picks + dashboard (pour cron)
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ------------------------- CONFIG -------------------------
API_KEY = os.environ.get("API_FOOTBALL_KEY", "")
BASE = "https://v3.football.api-sports.io"
TZ_NAME = os.environ.get("TZ_LOCAL", "Africa/Lubumbashi")  # Goma = UTC+2
TZ = ZoneInfo(TZ_NAME)
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")   # historique (base SQLite)
OUT_DIR = os.path.join(HERE, "docs")    # page web publiée (GitHub Pages)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "safe_picks.db")
HIST_N = 10          # nombre de derniers matchs analysés par équipe
MIN_RATE = 0.80      # taux de réalisation minimum (80 %)
LAST_K = 3           # le pari doit être passé dans les K derniers matchs
TOP = 10             # nombre de pronostics voulus
MAX_MATCHES = 30     # matchs analysés max par lancement (2 appels API par match, cache 12 h)
CACHE_H = 12

LIVE = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT"}
FINAL = {"FT", "AET", "PEN"}
VOID = {"PST", "CANC", "ABD", "AWD", "WO", "SUSP"}
NOT_STARTED = {"NS", "TBD"}

calls = 0

# ------------------------- API -------------------------
def api(endpoint, **params):
    global calls
    if not API_KEY:
        sys.exit("Clé manquante : définis la variable d’environnement API_FOOTBALL_KEY.")
    url = f"{BASE}/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-apisports-key": API_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    calls += 1
    if data.get("errors"):
        print("Avertissement API :", data["errors"], file=sys.stderr)
    return data.get("response", [])


# ------------------------- BASE DE DONNÉES -------------------------
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS team_cache(team_id INTEGER PRIMARY KEY, ts INTEGER, js TEXT);
    CREATE TABLE IF NOT EXISTS latest(id INTEGER PRIMARY KEY, label TEXT, ts TEXT, js TEXT);
    CREATE TABLE IF NOT EXISTS picks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT, fixture_id INTEGER, kickoff INTEGER, league TEXT,
        home TEXT, away TEXT, market TEXT, label TEXT,
        rate REAL, detail TEXT, status TEXT DEFAULT 'pending',
        score TEXT, created_at INTEGER,
        UNIQUE(fixture_id, market));
    """)
    return conn


# ------------------------- MARCHÉS -------------------------
def _tot(m): return m["gf"] + m["ga"]

MATCH_MARKETS = {
    "O1.5":  ("Plus de 1,5 but",         lambda m: _tot(m) >= 2),
    "O2.5":  ("Plus de 2,5 buts",        lambda m: _tot(m) >= 3),
    "U2.5":  ("Moins de 2,5 buts",       lambda m: _tot(m) <= 2),
    "U3.5":  ("Moins de 3,5 buts",       lambda m: _tot(m) <= 3),
    "BTTS":  ("Les deux marquent",       lambda m: m["gf"] >= 1 and m["ga"] >= 1),
    "NBTTS": ("Une équipe ne marque pas", lambda m: m["gf"] == 0 or m["ga"] == 0),
    "HT0.5": ("But en 1re mi-temps",     lambda m: None if m["hf"] is None else (m["hf"] + m["ha"]) >= 1),
}


def build(key, label, series):
    """series : listes (anciens -> récents) de True/False/None. Retourne un candidat ou None."""
    valid = [[x for x in s if x is not None] for s in series]
    if any(len(v) < 6 for v in valid):
        return None
    hits = sum(sum(v) for v in valid)
    total = sum(len(v) for v in valid)
    rate = hits / total
    if rate < MIN_RATE:
        return None
    for s in series:  # les K derniers matchs de chaque équipe doivent tous valider le pari
        last = s[-LAST_K:]
        if len(last) < LAST_K or not all(x is True for x in last):
            return None
    recent5 = sum(sum(s[-5:]) for s in valid) / sum(len(s[-5:]) for s in valid)
    return {"key": key, "label": label, "rate": rate, "recent5": recent5,
            "detail": f"{hits}/{total}"}


def candidates(hh, ah, hname, aname):
    out = []
    for key, (label, fn) in MATCH_MARKETS.items():
        out.append(build(key, label, [[fn(m) for m in hh], [fn(m) for m in ah]]))
    out.append(build("HSCORE", f"{hname} marque", [[m["gf"] >= 1 for m in hh]]))
    out.append(build("ASCORE", f"{aname} marque", [[m["gf"] >= 1 for m in ah]]))
    out.append(build("1X", f"{hname} ne perd pas (1X)", [[m["gf"] >= m["ga"] for m in hh]]))
    out.append(build("X2", f"{aname} ne perd pas (X2)", [[m["gf"] >= m["ga"] for m in ah]]))
    return [c for c in out if c]


def resolve(key, hg, ag, hth, hta, status):
    """Retourne 'won', 'lost', 'pending' ou 'void' (valable en direct et à la fin)."""
    if status in VOID:
        return "void"
    if status in NOT_STARTED or hg is None or ag is None:
        return "pending"
    final = status in FINAL
    t = hg + ag
    def end(win_if_final):
        if not final:
            return "pending"
        return "won" if win_if_final else "lost"
    if key in ("O1.5", "O2.5"):
        n = 2 if key == "O1.5" else 3
        return "won" if t >= n else end(False)
    if key in ("U2.5", "U3.5"):
        n = 3 if key == "U2.5" else 4
        return "lost" if t >= n else end(True)
    if key == "BTTS":
        return "won" if hg >= 1 and ag >= 1 else end(False)
    if key == "NBTTS":
        return "lost" if hg >= 1 and ag >= 1 else end(True)
    if key == "HSCORE":
        return "won" if hg >= 1 else end(False)
    if key == "ASCORE":
        return "won" if ag >= 1 else end(False)
    if key == "1X":
        return end(hg >= ag)
    if key == "X2":
        return end(ag >= hg)
    if key == "HT0.5":
        if status == "1H":
            return "won" if t >= 1 else "pending"
        if hth is None:
            return "void" if final else "pending"
        return "won" if (hth + hta) >= 1 else "lost"
    return "pending"


# ------------------------- HISTORIQUE ÉQUIPES -------------------------
def get_history(conn, team_id):
    now = int(datetime.now().timestamp())
    row = conn.execute("SELECT ts, js FROM team_cache WHERE team_id=?", (team_id,)).fetchone()
    if row and now - row["ts"] < CACHE_H * 3600:
        return json.loads(row["js"])
    resp = api("fixtures", team=team_id, last=HIST_N)
    hist = []
    for f in sorted(resp, key=lambda f: f["fixture"]["timestamp"]):
        if f["fixture"]["status"]["short"] not in FINAL:
            continue
        is_home = f["teams"]["home"]["id"] == team_id
        gh, ga = f["goals"]["home"], f["goals"]["away"]
        if gh is None or ga is None:
            continue
        ht = f["score"]["halftime"]
        hh, ha = ht.get("home"), ht.get("away")
        if hh is None or ha is None:
            hf = hta = None
        else:
            hf, hta = (hh, ha) if is_home else (ha, hh)
        hist.append({"gf": gh if is_home else ga, "ga": ga if is_home else gh,
                     "hf": hf, "ha": hta})
    conn.execute("INSERT OR REPLACE INTO team_cache VALUES(?,?,?)", (team_id, now, json.dumps(hist)))
    conn.commit()
    return hist


# ------------------------- FENÊTRE HORAIRE -------------------------
def parse_hm(s, base):
    h, m = map(int, s.split(":"))
    return base.replace(hour=h, minute=m, second=0, microsecond=0)


def window(args):
    now = datetime.now(TZ)
    if args.now:
        end = parse_hm(args.until, now)
        if end <= now:
            end += timedelta(days=1)
        return now, end, True
    start = parse_hm(args.from_, now) if args.from_ else now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = parse_hm(args.to, now) if args.to else now.replace(hour=23, minute=59, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    return start, end, not args.from_ and not args.to


def fetch_fixtures(start, end, include_live):
    fx = {}
    for d in sorted({start.date(), end.date()}):
        for f in api("fixtures", date=d.isoformat(), timezone=TZ_NAME):
            fx[f["fixture"]["id"]] = f
    sel = []
    for f in fx.values():
        st = f["fixture"]["status"]["short"]
        ko = datetime.fromtimestamp(f["fixture"]["timestamp"], TZ)
        if st in LIVE and include_live:
            sel.append(f)
        elif st in NOT_STARTED and start <= ko <= end:
            sel.append(f)
    return sorted(sel, key=lambda f: f["fixture"]["timestamp"])


# ------------------------- PRONOSTICS -------------------------
ICON = {"won": "✅ déjà validé", "lost": "❌ déjà perdu", "pending": "⏳ en cours", "void": "—"}


def cmd_picks(args):
    conn = db()
    start, end, include_live = window(args)
    fixtures = fetch_fixtures(start, end, include_live)
    print(f"{len(fixtures)} match(s) dans la fenêtre {start:%d/%m %H:%M} → {end:%d/%m %H:%M}")
    if len(fixtures) > MAX_MATCHES:
        print(f"Analyse limitée aux {MAX_MATCHES} premiers (réglage MAX_MATCHES) pour économiser le quota API.")
        fixtures = fixtures[:MAX_MATCHES]
    found = []
    for f in fixtures:
        h, a = f["teams"]["home"], f["teams"]["away"]
        try:
            hh, ah = get_history(conn, h["id"]), get_history(conn, a["id"])
        except Exception as e:
            print("  erreur API sur", h["name"], "-", a["name"], ":", e, file=sys.stderr)
            continue
        cands = candidates(hh, ah, h["name"], a["name"])
        if not cands:
            continue
        best = max(cands, key=lambda c: (c["rate"], c["recent5"]))
        st = f["fixture"]["status"]["short"]
        ht = f["score"]["halftime"]
        state = resolve(best["key"], f["goals"]["home"], f["goals"]["away"],
                        ht.get("home"), ht.get("away"), st)
        if state == "lost":
            continue  # déjà perdu en direct : inutile de le proposer
        found.append((f, best, state))
    found.sort(key=lambda x: (x[1]["rate"], x[1]["recent5"]), reverse=True)
    found = found[:TOP]
    latest = []

    if not found:
        print("Aucun match ne remplit les critères pour cette fenêtre.")
    else:
        if len(found) < TOP:
            print(f"Seulement {len(found)} pronostic(s) répondent aux critères (sur {TOP} souhaités) : voici ceux trouvés.")
        print()
    for i, (f, b, state) in enumerate(found, 1):
        ko = datetime.fromtimestamp(f["fixture"]["timestamp"], TZ)
        st = f["fixture"]["status"]["short"]
        live = ""
        if st in LIVE:
            live = f" | DIRECT {f['goals']['home']}-{f['goals']['away']} ({f['fixture']['status'].get('elapsed')}′) {ICON[state]}"
        print(f"{i:>2}. {ko:%H:%M} [{f['league']['country']} - {f['league']['name']}] "
              f"{f['teams']['home']['name']} - {f['teams']['away']['name']}")
        print(f"    ➜ {b['label']} | réalisé {b['rate']:.0%} ({b['detail']}) + {LAST_K} derniers matchs validés{live}")
        latest.append({
            "time": f"{ko:%H:%M}", "league": f"{f['league']['country']} - {f['league']['name']}",
            "match": f"{f['teams']['home']['name']} - {f['teams']['away']['name']}",
            "label": b["label"], "rate": round(b["rate"] * 100), "detail": b["detail"],
            "live": (f"DIRECT {f['goals']['home']}-{f['goals']['away']} ({f['fixture']['status'].get('elapsed')}′) {ICON[state]}"
                     if st in LIVE else "")})
        conn.execute("""INSERT OR IGNORE INTO picks(day,fixture_id,kickoff,league,home,away,market,label,rate,detail,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                     (ko.strftime("%Y-%m-%d"), f["fixture"]["id"], f["fixture"]["timestamp"],
                      f"{f['league']['country']} - {f['league']['name']}",
                      f["teams"]["home"]["name"], f["teams"]["away"]["name"],
                      b["key"], b["label"], b["rate"], b["detail"], int(datetime.now().timestamp())))
    if getattr(args, "now", False):
        mode = f"En direct + à venir jusqu’à {end:%d/%m %H:%M}"
    elif getattr(args, "from_", None) or getattr(args, "to", None):
        mode = f"Créneau {start:%d/%m %H:%M} → {end:%d/%m %H:%M}"
    else:
        mode = "Matchs restants d’aujourd’hui"
    conn.execute("INSERT OR REPLACE INTO latest VALUES(1,?,?,?)",
                 (mode, datetime.now(TZ).strftime("%d/%m/%Y %H:%M"),
                  json.dumps({"picks": latest, "analyses": len(fixtures), "demandes": TOP})))
    conn.commit()
    print(f"\nAppels API utilisés : {calls}")


# ------------------------- MISE À JOUR DES RÉSULTATS -------------------------
def cmd_update(_args=None):
    conn = db()
    now = int(datetime.now().timestamp())
    rows = conn.execute("SELECT * FROM picks WHERE status='pending' AND kickoff < ?", (now,)).fetchall()
    ids = sorted({r["fixture_id"] for r in rows})
    done = 0
    for i in range(0, len(ids), 20):
        batch = ids[i:i + 20]
        for f in api("fixtures", ids="-".join(map(str, batch))):
            st = f["fixture"]["status"]["short"]
            if st not in FINAL and st not in VOID:
                continue
            ht = f["score"]["halftime"]
            gh, ga = f["goals"]["home"], f["goals"]["away"]
            for r in conn.execute("SELECT * FROM picks WHERE fixture_id=? AND status='pending'",
                                  (f["fixture"]["id"],)).fetchall():
                res = resolve(r["market"], gh, ga, ht.get("home"), ht.get("away"), st)
                if res in ("won", "lost", "void"):
                    conn.execute("UPDATE picks SET status=?, score=? WHERE id=?", (res, f"{gh}-{ga}", r["id"]))
                    done += 1
    conn.commit()
    print(f"{done} pronostic(s) mis à jour. Appels API : {calls}")


# ------------------------- STATS / TABLEAU DE BORD -------------------------
def stats(conn):
    q = lambda s, *a: [dict(r) for r in conn.execute(s, a).fetchall()]
    by_day = q("""SELECT day, SUM(status='won') w, SUM(status='lost') l FROM picks
                  WHERE status IN ('won','lost') GROUP BY day ORDER BY day""")
    cum_w = cum_t = 0
    for d in by_day:
        cum_w += d["w"]; cum_t += d["w"] + d["l"]
        d["rate"] = round(100 * d["w"] / (d["w"] + d["l"]), 1)
        d["cum_rate"] = round(100 * cum_w / cum_t, 1)
    by_market = q("""SELECT market, label, COUNT(*) n, SUM(status='won') w FROM picks
                     WHERE status IN ('won','lost') GROUP BY market ORDER BY n DESC""")
    for m in by_market:
        m["rate"] = round(100 * m["w"] / m["n"], 1)
    buckets = []
    for lo, hi in ((0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 1.01)):
        r = q("""SELECT COUNT(*) n, COALESCE(SUM(status='won'),0) w FROM picks
                 WHERE status IN ('won','lost') AND rate>=? AND rate<?""", lo, hi)[0]
        buckets.append({"predit": f"{int(lo*100)}-{min(int(hi*100),100)}%", "n": r["n"],
                        "realise": round(100 * r["w"] / r["n"], 1) if r["n"] else None})
    total = q("""SELECT COUNT(*) n, COALESCE(SUM(status='won'),0) w FROM picks WHERE status IN ('won','lost')""")[0]
    pend = q("SELECT COUNT(*) n FROM picks WHERE status='pending'")[0]["n"]
    recent = q("""SELECT day, league, home, away, label, rate, status, score FROM picks
                  ORDER BY kickoff DESC LIMIT 60""")
    lt = conn.execute("SELECT label, ts, js FROM latest WHERE id=1").fetchone()
    latest = ({"label": lt["label"], "ts": lt["ts"], **json.loads(lt["js"])} if lt else None)
    return {"latest": latest, "total": total["n"], "won": total["w"],
            "rate": round(100 * total["w"] / total["n"], 1) if total["n"] else None,
            "pending": pend, "by_day": by_day, "by_market": by_market,
            "calibration": buckets, "recent": recent}


HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tableau de bord des pronostics</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--card:#f3f4f6;--line:#d1d5db;--ok:#15803d;--ko:#b91c1c}
@media(prefers-color-scheme:dark){:root{--bg:#111827;--fg:#f3f4f6;--card:#1f2937;--line:#374151;--ok:#4ade80;--ko:#f87171}}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:20px;max-width:1000px;margin:auto}
.cards{display:flex;gap:12px;flex-wrap:wrap}.card{background:var(--card);border-radius:10px;padding:14px 18px;flex:1;min-width:140px}
.card b{display:block;font-size:26px}
table{width:100%;border-collapse:collapse;margin:10px 0 30px;font-size:14px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
.wrap{overflow-x:auto}.won{color:var(--ok);font-weight:600}.lost{color:var(--ko);font-weight:600}
</style></head><body>
<h1>Tableau de bord des pronostics</h1>
<p id="gen"></p>
<div class="cards" id="cards"></div>
<h2>Dernière sélection</h2><p id="lt"></p><div class="wrap"><table id="tl"></table></div>
<p><a href="fiche.json" download>Télécharger la fiche (à m’envoyer pour analyse)</a></p>
<h2>Évolution du taux de réussite</h2><canvas id="c1" height="110"></canvas>
<h2>Résultat par jour</h2><canvas id="c2" height="110"></canvas>
<h2>Par type de pari</h2><div class="wrap"><table id="tm"></table></div>
<h2>Prévu vs réalisé</h2><div class="wrap"><table id="tc"></table></div>
<h2>Derniers pronostics</h2><div class="wrap"><table id="tr"></table></div>
<script>
const D = __DATA__;
document.getElementById('gen').textContent = 'Généré le ' + D.generated;
document.getElementById('cards').innerHTML =
 `<div class="card">Taux global<b>${D.rate ?? '–'}${D.rate!==null?' %':''}</b></div>
  <div class="card">Gagnés<b>${D.won}</b></div>
  <div class="card">Perdus<b>${D.total-D.won}</b></div>
  <div class="card">En attente<b>${D.pending}</b></div>`;
const L = D.latest;
document.getElementById('lt').textContent = L ? (L.label + ' — calculée le ' + L.ts + ' — ' + L.picks.length + ' pronostic(s) sur ' + L.demandes + ' souhaités') : 'Aucune sélection pour le moment.';
if (L) document.getElementById('tl').innerHTML = '<tr><th>Heure</th><th>Match</th><th>Pari</th><th>Réalisé</th><th>Direct</th></tr>' +
 L.picks.map(p=>'<tr><td>'+p.time+'</td><td>'+p.match+'<br><small>'+p.league+'</small></td><td>'+p.label+'</td><td>'+p.rate+' % ('+p.detail+')</td><td>'+p.live+'</td></tr>').join('');
const days = D.by_day.map(d=>d.day);
new Chart(document.getElementById('c1'),{type:'line',data:{labels:days,datasets:[
 {label:'Taux cumulé (%)',data:D.by_day.map(d=>d.cum_rate),tension:.25},
 {label:'Taux du jour (%)',data:D.by_day.map(d=>d.rate),tension:.25}]},
 options:{scales:{y:{min:0,max:100}}}});
new Chart(document.getElementById('c2'),{type:'bar',data:{labels:days,datasets:[
 {label:'Gagnés',data:D.by_day.map(d=>d.w)},{label:'Perdus',data:D.by_day.map(d=>d.l)}]},
 options:{scales:{x:{stacked:true},y:{stacked:true}}}});
const row=(a,h)=>(h?'<tr>'+a.map(x=>'<th>'+x+'</th>').join('')+'</tr>':'<tr>'+a.map(x=>'<td>'+x+'</td>').join('')+'</tr>');
document.getElementById('tm').innerHTML = row(['Pari','Nb','Gagnés','Réussite'],1)+
 D.by_market.map(m=>row([m.label.replace(/^.* (marque|ne perd pas.*)$/,'$1'),m.n,m.w,m.rate+' %'])).join('');
document.getElementById('tc').innerHTML = row(['Taux prévu','Nb','Réalisé'],1)+
 D.calibration.map(c=>row([c.predit,c.n,c.realise===null?'–':c.realise+' %'])).join('');
const lab={won:'<span class=won>Gagné</span>',lost:'<span class=lost>Perdu</span>',pending:'En attente',void:'Annulé'};
document.getElementById('tr').innerHTML = row(['Jour','Match','Pari','Prévu','Score','Résultat'],1)+
 D.recent.map(r=>row([r.day,r.home+' - '+r.away,r.label,Math.round(r.rate*100)+' %',r.score||'',lab[r.status]])).join('');
</script></body></html>"""


def cmd_dashboard(_args=None):
    conn = db()
    data = stats(conn)
    data["generated"] = datetime.now(TZ).strftime("%d/%m/%Y %H:%M")
    path = os.path.join(OUT_DIR, "index.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    print("Tableau de bord écrit :", path)


def cmd_export(_args=None):
    conn = db()
    data = stats(conn)
    data["generated"] = datetime.now(TZ).strftime("%d/%m/%Y %H:%M")
    data["config"] = {"hist_n": HIST_N, "min_rate": MIN_RATE, "last_k": LAST_K, "top": TOP,
                      "max_matches": MAX_MATCHES, "timezone": TZ_NAME,
                      "markets": list(MATCH_MARKETS) + ["HSCORE", "ASCORE", "1X", "X2"]}
    data["recent"] = [dict(r) for r in conn.execute(
        """SELECT day, league, home, away, market, label, rate, detail, status, score
           FROM picks ORDER BY kickoff DESC LIMIT 300""").fetchall()]
    path = os.path.join(OUT_DIR, "fiche.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print("Fiche générée :", path, "\nEnvoie-moi ce fichier pour que j’analyse les performances.")


def cmd_daily(args):
    cmd_update()
    cmd_picks(args)
    cmd_export()
    cmd_dashboard()


# ------------------------- MAIN -------------------------
def main():
    p = argparse.ArgumentParser(description="Pronostics réguliers + suivi")
    p.add_argument("command", choices=["picks", "update", "dashboard", "export", "daily"])
    p.add_argument("--now", action="store_true", help="en direct + à venir jusqu’à --until")
    p.add_argument("--until", default="06:00", help="heure limite avec --now (défaut 06:00)")
    p.add_argument("--from", dest="from_", help="début de créneau HH:MM")
    p.add_argument("--to", help="fin de créneau HH:MM")
    args = p.parse_args()
    {"picks": cmd_picks, "update": cmd_update, "dashboard": cmd_dashboard,
     "export": cmd_export, "daily": cmd_daily}[args.command](args)


if __name__ == "__main__":
    main()
