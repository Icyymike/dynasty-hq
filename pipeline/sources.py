"""Extra projection inputs shared by both apps, each one tested on this season's games before it's trusted.

- ESPN's weekly stat projections (free, unofficial feed), rescored in the league's own scoring.
- Betting lines: each team's implied points (from the over/under and spread), used to nudge projections
  toward shootouts and away from low-scoring games.
- Calibration: one "noise dial" that makes win chances bolder or more cautious, fit on how past games turned out.

Every function fails soft: if a feed is down the app keeps using Sleeper alone.
"""
import json, math, re, statistics as st

ESPN_PROJ = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{yr}/segments/0/leaguedefaults/3?view=kona_player_info"
ESPN_FILTER = json.dumps({"players": {"limit": 2000, "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}})
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={yr}&seasontype=2&week={wk}"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={eid}"
ESPN_STAT = {"0": "pass_att", "1": "pass_cmp", "3": "pass_yd", "4": "pass_td", "19": "pass_2pt", "20": "pass_int", "23": "rush_att",
             "24": "rush_yd", "25": "rush_td", "26": "rush_2pt", "42": "rec_yd", "43": "rec_td", "44": "rec_2pt", "53": "rec",
             "58": "rec_tgt", "72": "fum_lost"}
ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE"}
ESPN_TEAM = {"WSH": "WAS"}
SKILL = ("QB", "RB", "WR", "TE")


def _nn(t):
    t = re.sub(r"[^a-z ]", "", (t or "").lower().replace("-", " "))
    return " ".join(w for w in t.split() if w not in ("jr", "sr", "ii", "iii", "iv", "v"))


def espn_weekly(season, players_db, score, get_json):
    """{sleeper id: {week: projected points in this league's scoring}} for QB/RB/WR/TE, every week ESPN has."""
    url = ESPN_PROJ.format(yr=season)
    data = get_json(url, headers={"X-Fantasy-Filter": ESPN_FILTER})
    if not isinstance(data, dict):
        return {}, 0
    by_espn = {str(v.get("espn_id")): k for k, v in players_db.items() if isinstance(v, dict) and v.get("espn_id")}
    by_name = {}
    for k, v in players_db.items():
        if isinstance(v, dict) and v.get("position") in SKILL:
            nm = v.get("full_name") or f"{v.get('first_name', '')} {v.get('last_name', '')}"
            by_name.setdefault((_nn(nm), v["position"]), []).append((0 if v.get("team") else 1, 0 if v.get("active") else 1, k))
    out, matched = {}, 0
    for x in data.get("players") or []:
        p = x.get("player") or {}
        pos = ESPN_POS.get(p.get("defaultPositionId"))
        if not pos:
            continue
        sid = by_espn.get(str(p.get("id")))
        if not sid:
            c = sorted(by_name.get((_nn(p.get("fullName")), pos), []))
            sid = c[0][2] if c else None
        if not sid:
            continue
        matched += 1
        for s in p.get("stats") or []:
            if s.get("seasonId") == season and s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1 and s.get("scoringPeriodId"):
                stt = {ESPN_STAT[k]: v for k, v in (s.get("stats") or {}).items() if k in ESPN_STAT}
                v = score(stt, pos)
                if v > 0:
                    out.setdefault(sid, {})[int(s["scoringPeriodId"])] = v
    return out, matched


def _implied(ou, details, teams):
    m = re.match(r"([A-Z]+)\s+(-?[\d.]+)", details or "")
    if m:
        fav, x = ESPN_TEAM.get(m.group(1), m.group(1)), abs(float(m.group(2)))
    else:
        fav, x = teams[0], 0.0
    return {t: (ou + x) / 2 if t == fav else (ou - x) / 2 for t in teams}


def vegas_lines(season, done, last_week, get_json):
    """{(team, week): implied points}. Past weeks from each game's summary (kept a year), upcoming weeks from the scoreboard."""
    it = {}
    for wk in range(1, last_week + 1):
        sb = get_json(SCOREBOARD.format(yr=season, wk=wk), cache_hours=(24 * 365 if wk <= done else 2))
        for e in (sb or {}).get("events") or []:
            comp = (e.get("competitions") or [{}])[0]
            teams = [ESPN_TEAM.get(c["team"]["abbreviation"], c["team"]["abbreviation"]) for c in comp.get("competitors") or []]
            if len(teams) != 2:
                continue
            odds = comp.get("odds") or []
            if not odds and wk <= done:
                summ = get_json(SUMMARY.format(eid=e.get("id")), cache_hours=24 * 365)
                odds = (summ or {}).get("pickcenter") or []
            if odds and odds[0].get("overUnder"):
                for t, v in _implied(float(odds[0]["overUnder"]), odds[0].get("details"), teams).items():
                    it[(t, wk)] = round(v, 2)
    return it


def tune(rows):
    """rows: (actual, sleeper, espn or None, implied or None). Pick the ESPN weight and betting-line strength that
    miss least on games already played. Returns params and a plain report. Defaults to Sleeper alone."""
    if len(rows) < 150:
        return dict(q=1.0, beta=0.0, ref=22.5), dict(n=len(rows), used=False)
    its = [r[3] for r in rows if r[3]]
    ref = st.mean(its) if its else 22.5
    def pred(r, q, b):
        base = q * r[1] + (1 - q) * r[2] if r[2] else r[1]
        return base * ((r[3] / ref) ** b if r[3] else 1.0)
    mae = lambda q, b: st.mean(abs(r[0] - pred(r, q, b)) for r in rows)
    grid = [(mae(q, b), q, b) for q in (1.0, 0.75, 0.5, 0.25) for b in (0.0, 0.25, 0.5)]
    best = min(grid)
    base = mae(1.0, 0.0)
    if base - best[0] < 0.005:          # not worth it: keep it simple
        best = (base, 1.0, 0.0)
    rep = dict(n=len(rows), sleeper=round(base, 3), espn=round(st.mean(abs(r[0] - r[2]) for r in rows if r[2]), 3) if any(r[2] for r in rows) else None,
               n_espn=sum(1 for r in rows if r[2]), n_lines=sum(1 for r in rows if r[3]),
               combined=round(best[0], 3), w_sleeper=best[1], beta=best[2], used=best[1] < 1 or best[2] > 0,
               lines_only=round(mae(1.0, best[2]), 3) if best[2] else None, espn_only_blend=round(mae(best[1], 0.0), 3) if best[1] < 1 else None)
    return dict(q=best[1], beta=best[2], ref=ref), rep


def combine(sl, es, it, par):
    base = par["q"] * sl + (1 - par["q"]) * es if es else sl
    return base * ((it / par["ref"]) ** par["beta"] if it and par["beta"] else 1.0)


def calibrate(games_at):
    """games_at(m) -> [(win chance, 1 if won else 0)] re-simulated with every player's spread scaled by m.
    Finds the m whose win chances best match what happened, then pulls it most of the way back to 1 until there are
    enough games to trust it (k=80 games), and limits it to 0.8-1.25."""
    grid = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.35, 1.5]
    res = {}
    for m in grid:
        g = games_at(m)
        if not g:
            return dict(m=1.0, n=0)
        res[m] = (st.mean(-(o * math.log(max(p, 1e-4)) + (1 - o) * math.log(max(1 - p, 1e-4))) for p, o in g), g)
    m_hat = min(grid, key=lambda m: res[m][0])
    n = len(res[1.0][1])
    w = n / (n + 80)
    m = round(min(1.25, max(0.8, 1 + (m_hat - 1) * w)), 3)
    g1 = res[1.0][1]
    bins = []
    for lo, hi in ((0.5, 0.6), (0.6, 0.75), (0.75, 1.01)):
        b = [(max(p, 1 - p), o if p >= 0.5 else 1 - o) for p, o in g1 if lo <= max(p, 1 - p) < hi]
        if b:
            bins.append([lo, hi, len(b), round(st.mean(x for x, _ in b), 3), round(st.mean(y for _, y in b), 3)])
    return dict(m=m, m_hat=m_hat, n=n, weight=round(w, 2), logloss=round(res[1.0][0], 4), logloss_hat=round(res[m_hat][0], 4), bins=bins)
