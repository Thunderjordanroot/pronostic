#!/usr/bin/env python3
"""
fc_picks.py : version "Football Charts seule" (pas de direct).
Réutilise la logique de safe_picks.py (marchés, base de données, tableau de bord, fiche).

Commandes :
  python fc_picks.py probe        # affiche un échantillon des vraies données (à m'envoyer)
  python fc_picks.py picks        # matchs à venir aujourd'hui
  python fc_picks.py picks --now [--until 06:00]
  python fc_picks.py picks --from 18:00 --to 23:59
  python fc_picks.py update       # règle les pronostics passés avec les résultats
  python fc_picks.py dashboard | export
Clé : variable d'environnement FC_API_KEY.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
import functools
from datetime import datetime, timedelta, timezone

import safe_picks as sp

print = functools.partial(print, flush=True)  # affichage immédiat dans le journal

KEY = os.environ.get("FC_API_KEY", "")
BASE = "https://footballcharts-backend.onrender.com/api/v1"
UTC = timezone.utc
calls = 0
_last = 0.0

# mention obligatoire de la source
sp.HTML = sp.HTML.replace("</body>", "<p><small>Data by football-charts.com</small></p></body>")


# ------------------------- appel API (max ~55 requêtes/minute) -------------------------
def fc(path, **params):
    global calls, _last
    if not KEY:
        sys.exit("Clé manquante : secret FC_API_KEY.")
    wait = 1.1 - (time.time() - _last)
    if wait > 0:
        time.sleep(wait)
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = BASE + path + ("?" + q if q else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}",
                                               "User-Agent": "pronostic-bot/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            _last = time.time()
            calls += 1
            return data
        except urllib.error.HTTPError as e:
            _last = time.time()
            if e.code == 429 and attempt < 2:
                time.sleep(20)
                continue
            print(f"  HTTP {e.code} sur {url}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"  erreur réseau sur {url} : {e}", file=sys.stderr)
            time.sleep(3)
    return None


# ------------------------- lecture tolérante des réponses -------------------------
def items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("matches", "results", "fixtures", "leagues", "data", "items", "teams"):
            if isinstance(data.get(k), list):
                return data[k]
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def g(d, *names, default=None):
    if not isinstance(d, dict):
        return default
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v is not None:
            return v
    return default


def tname(v):
    if isinstance(v, dict):
        return g(v, "name", "team", "short_name", "slug")
    return v


def norm(s):
    return re.sub(r"\W+", "", str(s).lower())


def pair(v):
    if v is None:
        return None
    if isinstance(v, str):
        m = re.match(r"^\s*(\d+)\s*[:\-–]\s*(\d+)\s*$", v)
        return (int(m.group(1)), int(m.group(2))) if m else None
    if isinstance(v, dict):
        try:
            return int(g(v, "home", "h")), int(g(v, "away", "a"))
        except (TypeError, ValueError):
            return None
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return int(v[0]), int(v[1])
        except (TypeError, ValueError):
            return None
    return None


def when(it):
    """Retourne (datetime UTC, date_seule) ou (None, False)."""
    v = g(it, "kickoff_utc", "kickoff", "datetime", "utc_date", "start_time", "date")
    t = g(it, "time", "kickoff_time")
    if not v:
        return None, False
    s = str(v).strip().replace("Z", "+00:00")
    date_only = len(s) == 10 and not t
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None, False
    if dt.tzinfo is None:
        if t and re.match(r"^\d{1,2}:\d{2}", str(t)):
            h, m = str(t)[:5].split(":")
            dt = dt.replace(hour=int(h), minute=int(m))
            date_only = False
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC), date_only


def teams_of(it):
    home = tname(g(it, "home_team", "home", "hometeam", "team_home"))
    away = tname(g(it, "away_team", "away", "awayteam", "team_away"))
    return home, away


def result_of(r):
    """(dt, home, away, (hg, ag), (hth, hta) ou None) ou None."""
    home, away = teams_of(r)
    sc = g(r, "score", "ft", "ft_score", "full_time", "result")
    ht = g(r, "ht", "ht_score", "halftime", "half_time", "halftime_score", "score_ht")
    if isinstance(sc, dict) and pair(sc) is None:
        ht = ht or g(sc, "ht", "half_time", "halftime")
        sc = g(sc, "ft", "full_time", "fulltime")
    sc = pair(sc)
    if not home or not away or sc is None:
        return None
    dt, _ = when(r)
    return dt, home, away, sc, pair(ht)


# ------------------------- ligues et historiques -------------------------
def leagues():
    out = []
    for l in items(fc("/leagues/")):
        key = g(l, "key", "slug", "id", "code")
        if key is None:
            continue
        seasons = []
        for s in (g(l, "seasons", default=[]) or []):
            v = s if isinstance(s, (str, int)) else g(s, "season", "key", "id", "name")
            if v is not None:
                seasons.append(str(v))
        out.append({"key": str(key), "name": str(g(l, "name", default=key)),
                    "seasons": sorted(seasons, reverse=True)})
    return out


_hist_cache = {}


def league_history(lg):
    """{nom normalisé: [entrées chronologiques]} pour une ligue (saison courante + précédente)."""
    if lg["key"] in _hist_cache:
        return _hist_cache[lg["key"]]
    rows = []
    seasons = [None] + lg["seasons"][1:2]   # None = saison par défaut ; puis la précédente
    for s in seasons:
        rows += items(fc(f"/leagues/{lg['key']}/results/", season=s))
    seen, hist = set(), {}
    for r in rows:
        res = result_of(r)
        if not res:
            continue
        dt, home, away, (hg, ag), ht = res
        k = (str(dt), norm(home), norm(away))
        if k in seen:
            continue
        seen.add(k)
        hth, hta = ht if ht else (None, None)
        hist.setdefault(norm(home), []).append((dt, {"gf": hg, "ga": ag, "hf": hth, "ha": hta}))
        hist.setdefault(norm(away), []).append((dt, {"gf": ag, "ga": hg, "hf": hta, "ha": hth}))
    floor = datetime.min.replace(tzinfo=UTC)
    for t in hist:
        hist[t].sort(key=lambda x: x[0] or floor)
        hist[t] = [e for _, e in hist[t]][-sp.HIST_N:]
    _hist_cache[lg["key"]] = hist
    return hist


def ensure_column(conn):
    try:
        conn.execute("ALTER TABLE picks ADD COLUMN lg TEXT")
        conn.commit()
    except Exception:
        pass


# ------------------------- PICKS -------------------------
def cmd_picks(args):
    conn = sp.db()
    ensure_column(conn)
    start, end, _ = sp.window(args)
    now = datetime.now(sp.TZ)
    start = max(start, now)  # on ne propose pas de matchs déjà commencés (pas de direct ici)
    lgs = leagues()
    print(f"{len(lgs)} ligues. Recherche des matchs du {start:%d/%m %H:%M} au {end:%d/%m %H:%M}…")

    todo = []
    for li, l in enumerate(lgs, 1):
        if li % 10 == 0 or li == len(lgs):
            print(f"  ligues vérifiées : {li}/{len(lgs)} — {len(todo)} match(s) dans la fenêtre")
        for it in items(fc(f"/leagues/{l['key']}/fixtures/")):
            home, away = teams_of(it)
            dt, date_only = when(it)
            if not home or not away or dt is None:
                continue
            if date_only:
                ok = start.astimezone(UTC).date() <= dt.date() <= end.astimezone(UTC).date()
            else:
                ok = start <= dt <= end
            if ok:
                todo.append((l, home, away, dt))
    todo.sort(key=lambda x: x[3])
    print(f"{len(todo)} match(s) dans la fenêtre.")

    found, unknown = [], []
    for n, (l, home, away, dt) in enumerate(todo, 1):
        print(f"[{n}/{len(todo)}] analyse : {home} - {away} ({l['name']})")
        hist = league_history(l)
        hh, ah = hist.get(norm(home)), hist.get(norm(away))
        if not hh or not ah:
            unknown.append(f"{home} - {away} ({l['key']})")
            continue
        cands = sp.candidates(hh, ah, home, away)
        if not cands:
            continue
        best = max(cands, key=lambda c: (c["rate"], c["recent5"]))
        found.append((l, home, away, dt, best))
        print(f"   ➜ TROUVÉ n°{len(found)} : {home} - {away} | {best['label']} ({best['rate']:.0%})")
    found.sort(key=lambda x: (x[4]["rate"], x[4]["recent5"]), reverse=True)
    found = found[:sp.TOP]

    print("\n===== LISTE FINALE (triée par fiabilité) =====")
    if not found:
        print("Aucun match ne remplit les critères pour cette fenêtre.")
    elif len(found) < sp.TOP:
        print(f"Seulement {len(found)} pronostic(s) répondent aux critères (sur {sp.TOP}) : voici ceux trouvés.\n")
    latest = []
    for i, (l, home, away, dt, b) in enumerate(found, 1):
        ko = dt.astimezone(sp.TZ)
        print(f"{i:>2}. {ko:%d/%m %H:%M} [{l['name']}] {home} - {away}")
        print(f"    ➜ {b['label']} | réalisé {b['rate']:.0%} ({b['detail']}) + {sp.LAST_K} derniers matchs validés")
        latest.append({"time": f"{ko:%H:%M}", "league": l["name"], "match": f"{home} - {away}",
                       "label": b["label"], "rate": round(b["rate"] * 100), "detail": b["detail"], "live": ""})
        fid = zlib.crc32(f"{l['key']}|{norm(home)}|{norm(away)}|{dt:%Y-%m-%d}".encode())
        conn.execute("""INSERT OR IGNORE INTO picks(day,fixture_id,kickoff,league,home,away,market,label,rate,detail,created_at,lg)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (ko.strftime("%Y-%m-%d"), fid, int(dt.timestamp()), l["name"], home, away,
                      b["key"], b["label"], b["rate"], b["detail"], int(time.time()), l["key"]))
    if unknown:
        print(f"\n{len(unknown)} match(s) ignoré(s) faute d’historique reconnu, par exemple : " + "; ".join(unknown[:8]))

    if args.now:
        mode = f"À venir jusqu’à {end:%d/%m %H:%M} (sans direct)"
    elif args.from_ or args.to:
        mode = f"Créneau {start:%d/%m %H:%M} → {end:%d/%m %H:%M}"
    else:
        mode = "Matchs à venir aujourd’hui (sans direct)"
    conn.execute("INSERT OR REPLACE INTO latest VALUES(1,?,?,?)",
                 (mode, datetime.now(sp.TZ).strftime("%d/%m/%Y %H:%M"),
                  json.dumps({"picks": latest, "analyses": len(todo), "demandes": sp.TOP})))
    conn.commit()
    print(f"\nRequêtes utilisées : {calls}")


# ------------------------- UPDATE -------------------------
def cmd_update(_args=None):
    conn = sp.db()
    ensure_column(conn)
    rows = conn.execute("""SELECT * FROM picks WHERE status='pending' AND lg IS NOT NULL
                           AND kickoff < ?""", (int(time.time()) - 3 * 3600,)).fetchall()
    by_lg = {}
    for r in rows:
        by_lg.setdefault(r["lg"], []).append(r)
    done = 0
    for lg, picks in by_lg.items():
        index = {}
        for row in items(fc(f"/leagues/{lg}/results/")):
            res = result_of(row)
            if res and res[0]:
                dt, home, away, sc, ht = res
                index.setdefault((norm(home), norm(away)), []).append((dt, sc, ht))
        for p in picks:
            for dt, (hg, ag), ht in index.get((norm(p["home"]), norm(p["away"])), []):
                if abs(dt.timestamp() - p["kickoff"]) < 36 * 3600:
                    hth, hta = ht if ht else (None, None)
                    res = sp.resolve(p["market"], hg, ag, hth, hta, "FT")
                    if res in ("won", "lost", "void"):
                        conn.execute("UPDATE picks SET status=?, score=? WHERE id=?",
                                     (res, f"{hg}-{ag}", p["id"]))
                        done += 1
                    break
    conn.commit()
    print(f"{done} pronostic(s) mis à jour. Requêtes : {calls}")


# ------------------------- PROBE -------------------------
def cmd_probe(_args=None):
    def show(title, data, n=1500):
        print("=" * 12, title)
        s = json.dumps(data, ensure_ascii=False, indent=1)
        print(s[:n] + ("\n…[tronqué]" if len(s) > n else ""))
    show("GET /api/v1/", fc("/"), 1200)
    raw = fc("/leagues/")
    show("GET /leagues/ (début)", raw, 1500)
    lgs = leagues()
    print(f"\n{len(lgs)} ligues détectées. Exemples :", [(l['key'], l['seasons'][:3]) for l in lgs[:5]])
    if not lgs:
        return
    l = next((x for x in lgs if x["key"] == "premier"), lgs[0])
    res = fc(f"/leagues/{l['key']}/results/")
    rows = items(res)
    show(f"GET /leagues/{l['key']}/results/ : {len(rows)} lignes, 2 premières", rows[:2], 1800)
    ok = sum(1 for r in rows if result_of(r))
    print(f"Lignes de résultats lues correctement : {ok}/{len(rows)}")
    fx = fc(f"/leagues/{l['key']}/fixtures/")
    frows = items(fx)
    show(f"GET /leagues/{l['key']}/fixtures/ : {len(frows)} lignes, 2 premières", frows[:2], 1800)
    okf = sum(1 for r in frows if teams_of(r)[0] and when(r)[0])
    print(f"Matchs à venir lus correctement : {okf}/{len(frows)}")
    hist = league_history(l)
    print(f"Équipes avec historique : {len(hist)} ; exemple :", {k: len(v) for k, v in list(hist.items())[:5]})
    print(f"\nRequêtes utilisées : {calls}")


# ------------------------- MAIN -------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["probe", "picks", "update", "dashboard", "export"])
    p.add_argument("--now", action="store_true")
    p.add_argument("--until", default="06:00")
    p.add_argument("--from", dest="from_")
    p.add_argument("--to")
    a = p.parse_args()
    {"probe": cmd_probe, "picks": cmd_picks, "update": cmd_update,
     "dashboard": sp.cmd_dashboard, "export": sp.cmd_export}[a.command](a)


if __name__ == "__main__":
    main()
