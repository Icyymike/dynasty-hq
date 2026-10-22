"""Redraft HQ data refresh (the redraft league; managed lineups, rolling waivers, kickers and defenses).

Shares the data layer and noise model with build.py, but everything is about THIS season:
start/sit, waivers, streaming, rest-of-season trades, playoff odds. Writes docs/redraft/data.json.
Set FIXTURE_DIR to run offline against saved responses.
"""
import json, os, sys, math, statistics as st, itertools, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build as B  # noqa: E402  (shared fetching, scoring, hashing)
import sources as SRC  # noqa: E402  (ESPN projections, betting lines, calibration)

ROOT = B.ROOT
RC = B.CFG.get("redraft") or {}
OUTDIR = os.path.join(ROOT, "docs", "redraft")
os.makedirs(OUTDIR, exist_ok=True)
API = B.API
get = B.get
FC_URL = "https://api.fantasycalc.com/values/current?isDynasty=false&numQbs=1&numTeams=12&ppr=1"

POS = ("QB", "RB", "WR", "TE", "K", "DEF")
SKILL = ("QB", "RB", "WR", "TE")
ELIG = {"QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
        "FLEX": {"RB", "WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}, "REC_FLEX": {"WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}}
CV = {"QB": 0.42, "RB": 0.55, "WR": 0.62, "TE": 0.65, "K": 0.5, "DEF": 0.7}


# ---------------------------------------------------------------- noise model (mirrored in the page's JavaScript)
def sidnum(sid):
    s = str(sid)
    return int(s) if s.isdigit() else 500000 + B.team_num(s)   # team defenses get their own number


def sim(m, cv, sid, team, s, w, p_miss):
    if m <= 0:
        return 0.0
    a = sidnum(sid)
    if B.h3(a, s * 37 + 9, w * 41 + 17) < p_miss:
        return 0.0
    sig = math.sqrt(math.log(1 + cv * cv))
    z = B.W_IND * B.znorm(a, s, w) + B.W_TEAM * B.team_z(team, s, w)
    return m * math.exp(sig * z - sig * sig / 2)


# ---------------------------------------------------------------- lineups (fixed slots, chosen before kickoff)
def choose(items, slots, locked=None):
    """items: [(pid, pos, value)]. Fill slots with the best eligible players (dedicated slots first, then flexes).
    Greedy is exact here because flex eligibility nests. locked: {slot index: pid} kept as is."""
    locked = locked or {}
    used = set(locked.values())
    out = [locked.get(i) for i in range(len(slots))]
    order = sorted((i for i in range(len(slots)) if i not in locked), key=lambda i: len(ELIG[slots[i]]))
    pool = sorted((x for x in items if x[0] not in used), key=lambda x: -x[2])
    for i in order:
        for x in pool:
            if x[0] not in used and x[1] in ELIG[slots[i]] and x[2] > 0:
                out[i] = x[0]; used.add(x[0]); break
    return out


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


PA_B = [(0, 0, "pts_allow_0"), (1, 6, "pts_allow_1_6"), (7, 13, "pts_allow_7_13"), (14, 20, "pts_allow_14_20"), (21, 27, "pts_allow_21_27"),
        (28, 34, "pts_allow_28_34"), (35, 999, "pts_allow_35p")]
YA_B = [(0, 99, "yds_allow_0_100"), (100, 199, "yds_allow_100_199"), (200, 299, "yds_allow_200_299"), (300, 349, "yds_allow_300_349"),
        (350, 399, "yds_allow_350_399"), (400, 449, "yds_allow_400_449"), (450, 499, "yds_allow_450_499"), (500, 549, "yds_allow_500_549"), (550, 9999, "yds_allow_550p")]


def proj_stats(s, pos):
    """Sleeper projections give one points-allowed bucket and simplified kicking buckets; turn them into expected values
    for this league's buckets so a defense projected to allow 20 isn't scored as a sure 14-20."""
    s = dict(s)
    if pos == "DEF":
        for lo, hi, k in PA_B + YA_B:
            s.pop(k, None)
        for mean, sd, bks in ((s.get("pts_allow"), 9.5, PA_B), (s.get("yds_allow"), 60.0, YA_B)):
            if mean is None:
                continue
            for lo, hi, k in bks:
                p = norm_cdf((hi + 0.5 - mean) / sd) - norm_cdf((lo - 0.5 - mean) / sd) if lo > 0 else norm_cdf((hi + 0.5 - mean) / sd)
                s[k] = p
    if pos == "K":
        if "fgm_50p" in s and "fgm_50_59" not in s:
            s["fgm_50_59"] = s["fgm_50p"]
        if "fgmiss" not in s:
            s["fgmiss"] = sum(v for k, v in s.items() if k.startswith("fgmiss_"))
    return s


def main():
    lid, me_owner = RC["league_id"], RC["owner_id"]
    SIMS = int(RC.get("sims", 600))
    state = get(f"{API}/state/nfl")
    league = get(f"{API}/league/{lid}")
    for _ in range(5):  # Sleeper issues a new league id each season: follow it forward
        st_s = str(state.get("league_season") or state.get("season") or "")
        if not st_s.isdigit() or int(st_s) <= int(league["season"]):
            break
        nxt = next((l_ for l_ in (get(f"{API}/user/{me_owner}/leagues/nfl/{int(league['season']) + 1}", required=False) or [])
                    if str(l_.get("previous_league_id")) == str(lid)), None)
        if not nxt:
            break
        lid = nxt["league_id"]; league = get(f"{API}/league/{lid}")
    season = int(league["season"])
    users = get(f"{API}/league/{lid}/users")
    rosters = get(f"{API}/league/{lid}/rosters")
    S = league.get("settings", {})
    slots = [x for x in league.get("roster_positions", []) if x in ELIG]
    n_bench = sum(1 for x in league.get("roster_positions", []) if x == "BN")
    playoff_start = int(S.get("playoff_week_start", 15)); reg_end = playoff_start - 1
    n_playoff = int(S.get("playoff_teams", 6))
    po_weeks = 3 if n_playoff > 4 else 2
    last_week = reg_end + po_weeks
    median_on = bool(S.get("league_average_match"))
    deadline = int(S.get("trade_deadline") or 0)
    score = B.Scorer(league["scoring_settings"])

    st_season = str(state.get("season"))
    cur = int(state.get("week") or 1) if st_season == str(season) else last_week + 1
    sch = get(f"https://api.sleeper.com/schedule/nfl/regular/{season}", required=False) or []
    game_state = {}
    for gm in sch if isinstance(sch, list) else []:
        if isinstance(gm, dict) and str(gm.get("week")) == str(cur):
            for side in ("home", "away"):
                if gm.get(side):
                    game_state[gm[side]] = gm.get("status") or ""
    week_started = any(v in ("in_progress", "complete") for v in game_state.values())
    done = max(0, min(cur - 1, last_week))
    fut = list(range(max(done + 1, 1), last_week + 1))
    NW = len(fut) or 1

    players_db = get(f"{API}/players/nfl", cache_hours=0.3)  # injury status lives here: keep it fresh (the second app in the same run reuses it)
    for d_ in players_db.values():
        if isinstance(d_, dict) and d_.get("position") not in POS:
            fp_ = [x for x in (d_.get("fantasy_positions") or []) if x in POS]
            if fp_:
                d_["position"] = fp_[0]

    def pinfo(pid):
        d = players_db.get(pid) or {}
        pos = d.get("position") or (d.get("fantasy_positions") or [None])[0]
        if pos == "DEF":
            return dict(n=f"{d.get('first_name', pid)} {d.get('last_name', '')}".strip() + " D/ST", p="DEF", t=pid, age=None, inj=None)
        return dict(n=d.get("full_name") or f"{d.get('first_name', '')} {d.get('last_name', '')}".strip() or pid,
                    p=pos, t=d.get("team") or "", age=d.get("age"), inj=d.get("injury_status"))

    uname = {u["user_id"]: (u.get("metadata") or {}).get("team_name") or u.get("display_name") for u in users}
    teams = []
    for r in rosters:
        rs = r.get("settings") or {}
        teams.append(dict(id=r["roster_id"], owner=r.get("owner_id"), name=uname.get(r.get("owner_id")) or f"Team {r['roster_id']}",
                          user=next((u.get("display_name") for u in users if u["user_id"] == r.get("owner_id")), ""),
                          players=[str(p) for p in (r.get("players") or [])], ir=[str(p) for p in (r.get("reserve") or [])],
                          wpos=rs.get("waiver_position"), moves=rs.get("total_moves", 0),
                          ppts=(rs.get("ppts") or 0) + (rs.get("ppts_decimal") or 0) / 100))
    me = next(t["id"] for t in teams if t["owner"] == me_owner)
    rostered = {p: t["id"] for t in teams for p in t["players"]}
    on_ir = {p for t in teams for p in t["ir"]}

    # ---------- stats this season + last season, scored in this league's format
    pts, use, ext, tm_tot = {}, {}, {}, {}
    for yr, weeks_, cache in ((season - 1, range(1, 19), True), (season, range(1, done + 1), False)):
        for wk in weeks_:
            data = get(f"{API}/stats/nfl/regular/{yr}/{wk}", cache_hours=(24 * 365 if cache else None), required=False) or {}
            for pid, s in data.items():
                if not isinstance(s, dict):
                    continue
                info = players_db.get(pid) or {}
                pos = info.get("position")
                if pos not in POS:
                    continue
                if not (s.get("gp") or s.get("gms_active") or s.get("off_snp")):
                    continue
                key = f"{yr}-{wk}"
                pts.setdefault(pid, {})[key] = score(s, pos)
                if pos in SKILL:
                    use.setdefault(pid, {})[key] = [s.get("pass_att", 0) or 0, s.get("rush_att", 0) or 0, s.get("rec_tgt", 0) or 0]
                    g_ = lambda k_: float(s.get(k_) or 0)
                    ext.setdefault(pid, {})[key] = [g_("off_snp"), g_("tm_off_snp"), g_("rec_tgt"), g_("rush_att"), g_("pass_att"),
                                                    g_("rec_rz_tgt"), g_("rush_rz_att"), g_("rec_air_yd")]
                    tt_ = tm_tot.setdefault((info.get("team") or "", key), [0.0, 0.0]); tt_[0] += g_("rec_tgt"); tt_[1] += g_("rush_att")

    # ---------- Sleeper projections, rescored (past weeks kept for the backtest)
    proj = {}
    for wk in range(1, last_week + 1):
        data = get(f"{API}/projections/nfl/regular/{season}/{wk}", cache_hours=(24 * 365 if wk <= done else 0.3 if wk == done + 1 else 3), required=False) or {}
        for pid, s in data.items():
            if not isinstance(s, dict):
                continue
            pos = (players_db.get(pid) or {}).get("position")
            if pos not in POS or (pid not in rostered and not (players_db.get(pid) or {}).get("team") and pos != "DEF"):
                continue
            v = score(proj_stats(s, pos), pos)
            if v > 0 or (pos == "DEF" and s.get("gp")):
                proj.setdefault(pid, {})[wk] = max(v, 0.1)

    # ---------- more inputs: ESPN's projections and betting lines, kept only if they make this season's misses smaller
    proj_sl = {p_: dict(v) for p_, v in proj.items()}
    es_proj, it_lines, n_es = {}, {}, 0
    try:
        es_proj, n_es = SRC.espn_weekly(season, players_db, score, lambda u, headers=None: get(u, cache_hours=3, required=False, headers=headers))
    except Exception as e:  # noqa
        print(f"WARN ESPN projections unavailable: {e}")
    try:
        it_lines = SRC.vegas_lines(season, done, last_week, lambda u, cache_hours=None: get(u, cache_hours=cache_hours, required=False))
    except Exception as e:  # noqa
        print(f"WARN betting lines unavailable: {e}")
    team_of = lambda p_: (players_db.get(p_) or {}).get("team")
    t_rows = [(pts[p_][f"{season}-{w}"], v, es_proj.get(p_, {}).get(w), it_lines.get((team_of(p_), w)))
              for p_, d_ in proj_sl.items() if (players_db.get(p_) or {}).get("position") in SKILL
              for w, v in d_.items() if w <= done and v >= 3 and f"{season}-{w}" in pts.get(p_, {})]
    src_par, src_rep = SRC.tune(t_rows)   # applied below, only if the honest week-by-week test agrees
    src_rep.update(espn_players=n_es, lines=len(it_lines))

    # ---------- redraft trade market (FantasyCalc, 12-team 1QB full PPR, rest of season)
    fc_vals, fc_trend = {}, {}
    for e in get(FC_URL, cache_hours=12, required=False) or []:
        pl = e.get("player") or {}
        if pl.get("sleeperId"):
            fc_vals[str(pl["sleeperId"])] = e.get("value", 0); fc_trend[str(pl["sleeperId"])] = e.get("trend30Day") or 0

    # ---------- matchups: results so far, current lineups, live points, data check
    weeks, sched, starters_wk, pp_wk = {}, {}, {}, {}
    live, live_pp, cur_starters = {}, {}, {}
    checks = dict(lineups=0, lineup_bad=[], scored=0, score_bad=[])
    for wk in range(1, last_week + 1):
        m = get(f"{API}/league/{lid}/matchups/{wk}", required=False) or []
        if wk <= done:
            weeks[wk] = [[x["roster_id"], x.get("matchup_id"), round(x.get("points") or 0, 2)] for x in m]
            starters_wk[wk] = {x["roster_id"]: [str(p) for p in (x.get("starters") or [])] for x in m}
            pp_wk[wk] = {x["roster_id"]: {str(k): v for k, v in (x.get("players_points") or {}).items()} for x in m}
            for x in m:
                pp = x.get("players_points") or {}
                checks["lineups"] += 1
                sp = sum(pp.get(str(p), 0) for p in (x.get("starters") or []) if p and p != "0")
                if abs(sp - (x.get("points") or 0)) > 0.06:
                    checks["lineup_bad"].append([wk, x["roster_id"], round(sp, 2), x.get("points")])
                for k_, v_ in pp.items():
                    ours = pts.get(str(k_), {}).get(f"{season}-{wk}")
                    if ours is not None:
                        checks["scored"] += 1
                        if abs(ours - v_) > 0.11:
                            checks["score_bad"].append([wk, str(k_), ours, v_])
        else:
            if wk <= reg_end:
                sched[wk] = [[x["roster_id"], x.get("matchup_id")] for x in m]
            if wk == cur:
                cur_starters = {x["roster_id"]: [str(p) for p in (x.get("starters") or [])] for x in m}
                if week_started:
                    live = {x["roster_id"]: round(x.get("points") or 0, 2) for x in m}
                    for x in m:
                        for k_, v_ in (x.get("players_points") or {}).items():
                            live_pp[str(k_)] = v_
    if not sched.get(cur) and cur <= reg_end:
        pass
    print(f"data check: {checks['lineups']} lineups, {len(checks['lineup_bad'])} off; {checks['scored']} player scores, {len(checks['score_bad'])} off")

    # ---------- transactions this season (moves, trades, recent drops)
    tx_all = []
    for rd in range(0, last_week + 2):
        for t in get(f"{API}/league/{lid}/transactions/{rd}", required=False) or []:
            if t.get("status") == "complete":
                tx_all.append(t)
    tx_all.sort(key=lambda t: -(t.get("status_updated") or t.get("created") or 0))
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    recent_drop = {}
    for t in tx_all:
        for pid, rid in (t.get("drops") or {}).items():
            if str(pid) not in rostered and now_ms - (t.get("status_updated") or 0) < 3 * 86400000:
                recent_drop.setdefault(str(pid), t.get("status_updated"))
    tnames = {t["id"]: t["name"] for t in teams}
    moves_by = {}
    for t in tx_all:
        if t.get("type") in ("free_agent", "waiver"):
            for rid in t.get("roster_ids") or []:
                moves_by[rid] = moves_by.get(rid, 0) + 1
    for t in teams:
        t["moves"] = moves_by.get(t["id"], 0)
    trades = []
    for t in tx_all:
        if t.get("type") != "trade":
            continue
        sides = {rid: dict(rid=rid, got=[]) for rid in t.get("roster_ids") or []}
        for pid, rid in (t.get("adds") or {}).items():
            if rid in sides:
                sides[rid]["got"].append(str(pid))
        trades.append(dict(ts=t.get("status_updated"), wk=t.get("leg"), sides=list(sides.values())))

    # ---------- player universe
    uni = set(rostered) | {p for wk in starters_wk.values() for l in wk.values() for p in l if p and p != "0"}
    ros_sl = lambda p_: sum(v for w_, v in proj.get(p_, {}).items() if w_ >= (fut[0] if fut else 99))
    fa_all = [p_ for p_ in proj if p_ not in rostered]
    fa_pool = []
    for ps, n_ in (("QB", 8), ("RB", 18), ("WR", 20), ("TE", 8), ("K", 8), ("DEF", 10)):
        fa_pool += sorted((p_ for p_ in fa_all if (players_db.get(p_) or {}).get("position") == ps), key=lambda p_: -ros_sl(p_))[:n_]
    uni |= set(fa_pool)

    # ---------- our model next to Sleeper's (skill positions), tuned on this season's weeks
    def g(pid, yr, upto=99):
        return sorted((int(k.split("-")[1]), v) for k, v in pts.get(pid, {}).items() if k.startswith(f"{yr}-") and int(k.split("-")[1]) < upto)

    coef = {}
    for ps in SKILL:
        X, y = [], []
        for pid, d in pts.items():
            if (players_db.get(pid) or {}).get("position") != ps:
                continue
            for k, v in d.items():
                if k.startswith(f"{season - 1}-") and k in use.get(pid, {}):
                    u = use[pid][k]; X.append([u[0], u[1], u[2], 1.0]); y.append(v)
        coef[ps] = B.lstsq(X, y) if len(X) > 30 else [0, 0, 0, 5]

    def our_mean(pid, wk, par):
        k, wr, ws, wo = par
        pos = pinfo(pid)["p"]
        cur_g = g(pid, season, wk); prev = g(pid, season - 1)
        sl_ = proj.get(pid, {})
        prior = B._mean(v for _, v in prev) if len(prev) >= 4 else (B._mean(sl_.values()) if sl_ else 5.0)
        n = len(cur_g)
        if n == 0:
            return prior
        recent = B._mean(v for _, v in cur_g[-4:]); season_avg = B._mean(v for _, v in cur_g)
        if pos in SKILL:
            c = coef[pos]
            opp = B._mean(c[0] * use[pid][f"{season}-{w}"][0] + c[1] * use[pid][f"{season}-{w}"][1] + c[2] * use[pid][f"{season}-{w}"][2] + c[3]
                          for w, _ in cur_g[-4:] if f"{season}-{w}" in use.get(pid, {})) if use.get(pid) else season_avg
        else:
            opp = season_avg
        data = (wr * recent + ws * season_avg + wo * opp) / (wr + ws + wo)
        pw = k / (k + n)
        return pw * prior + (1 - pw) * data

    grid = list(itertools.product([1, 2, 3, 4, 6], [0.2, 0.35, 0.5], [0.1, 0.25, 0.4], [0.2, 0.4, 0.6]))
    bt_pts = [(pid, w, pts[pid][f"{season}-{w}"], proj_sl[pid][w]) for pid in proj_sl for w in range(2, done + 1)
              if f"{season}-{w}" in pts.get(pid, {}) and w in proj_sl[pid] and pinfo(pid)["p"] in SKILL]
    best_par = (2, 0.2, 0.1, 0.6)
    wf = dict(n=0)
    if len(bt_pts) >= 100:
        OM = {par: [our_mean(pid, w, par) for pid, w, _, _ in bt_pts] for par in grid}
        # honest test first: every setting chosen from earlier weeks only, scored on the next week
        R_ = [dict(wk=w, a=a, sl=s, es=es_proj.get(pid, {}).get(w), it=it_lines.get((team_of(pid), w)), ev=s >= 3) for pid, w, a, s in bt_pts]
        wf, _, _ = SRC.choose(R_, OM)
        use_src = wf["pick"] in ("sources", "sources+model") and bool(src_rep.get("used"))
        if use_src:
            for p_, d_ in proj.items():
                if (players_db.get(p_) or {}).get("position") in SKILL:
                    for w in d_:
                        d_[w] = round(SRC.combine(proj_sl[p_][w], es_proj.get(p_, {}).get(w), it_lines.get((team_of(p_), w)), src_par), 2)
        else:
            src_par = dict(q=1.0, beta=0.0, ref=22.5)
        src_rep["used"] = use_src
        cons = [proj[pid][w] for pid, w, _, _ in bt_pts]
        best_par = min(OM, key=lambda k: st.mean(abs(r[2] - o) for r, o in zip(bt_pts, OM[k])))
        rows = [(r[2], c, o) for r, c, o in zip(bt_pts, cons, OM[best_par])]
        mae = lambda f: st.mean(abs(a - f(s, o)) for a, s, o in rows)
        bw = min((mae(lambda s, o, q=q: q * s + (1 - q) * o), q) for q in [i / 20 for i in range(21)])
        use_model = wf["pick"] in ("sleeper+model", "sources+model")
        backtest = dict(n=len(rows), sleeper=round(mae(lambda s, o: s), 2), ours=round(mae(lambda s, o: o), 2), blend=round(bw[0], 2),
                        w=bw[1] if use_model else 1.0, model_used=use_model)
    else:
        backtest = dict(n=len(bt_pts), sleeper=None, ours=None, blend=None, w=0.85)
    src_rep["oos"] = wf
    BW = backtest["w"]
    print(f"sources: {src_rep}")
    print(f"projection backtest: {backtest}")

    # ---------- matchups: how generous each defense is to each position (opponent-adjusted, shrunk)
    opp_tw = {}
    for gm in sch if isinstance(sch, list) else []:
        if isinstance(gm, dict) and gm.get("home") and gm.get("away") and str(gm.get("week", "")).isdigit():
            w_ = int(gm["week"]); opp_tw[(gm["home"], w_)] = gm["away"]; opp_tw[(gm["away"], w_)] = gm["home"]
    tp = {}
    for pid_, d_ in pts.items():
        inf_ = players_db.get(pid_) or {}
        ps_, tm_ = inf_.get("position"), (pid_ if inf_.get("position") == "DEF" else inf_.get("team"))
        if ps_ not in POS or not tm_:
            continue
        for wk in range(1, done + 1):
            v_ = d_.get(f"{season}-{wk}")
            if v_ is None:
                continue
            k_ = (tm_, ps_, wk)
            tp[k_] = max(tp.get(k_, 0.0), v_) if ps_ in ("QB", "K", "DEF") else tp.get(k_, 0.0) + v_
    off_avg = {}
    for (tm_, ps_, wk), v_ in tp.items():
        off_avg.setdefault((tm_, ps_), []).append(v_)
    off_avg = {k_: sum(v) / len(v) for k_, v in off_avg.items() if v}
    ratios = {}
    for (tm_, ps_, wk), v_ in tp.items():
        d_ = opp_tw.get((tm_, wk)); o_ = off_avg.get((tm_, ps_))
        if d_ and o_ and o_ > 1:
            ratios.setdefault((d_, ps_), []).append(v_ / o_)
    DEFF = {}
    for (d_, ps_), r_ in ratios.items():
        n_ = len(r_); DEFF.setdefault(d_, {})[ps_] = round(min(1.3, max(0.75, (sum(r_) + 4) / (n_ + 4))), 3)

    # ---------- how good were the pre-week numbers? Every game so far re-played with the projections for the lineups that were started.
    # The same re-play, with every player's spread scaled up or down, finds the "noise dial" that makes win chances match results.
    def backfill(mult, sims=300):
        out_ = []
        for wk in range(1, done + 1):
            rows = weeks.get(wk) or []
            means = {}
            for rid, mid, _ in rows:
                means[rid] = [(p, (proj.get(p, {}).get(wk) or 0), CV.get((players_db.get(p) or {}).get("position"), 0.6) * mult,
                               (players_db.get(p) or {}).get("team") or p) for p in starters_wk[wk].get(rid, []) if p and p != "0"]
            draws = {rid: [sum(sim(m, cv, p, tm, s_, 900 + wk, 0.03) for p, m, cv, tm in ms) for s_ in range(sims)] for rid, ms in means.items()}
            for rid, mid, sc_ in rows:
                o = next((r for r in rows if r[1] == mid and r[0] != rid), None)
                if not o or rid > o[0]:
                    continue
                wp_ = (sum(1 for a, b in zip(draws[rid], draws[o[0]]) if a > b) + 0.5) / (sims + 1)
                out_.append(dict(wk=wk, a=rid, b=o[0], wp=round(wp_, 3), pa=round(sum(m for _, m, _, _ in means[rid]), 1), pb=round(sum(m for _, m, _, _ in means[o[0]]), 1),
                                 sa=sc_, sb=o[2]))
        return out_
    cal = SRC.calibrate(lambda m_: [(g_["wk"], g_["wp"], 1 if g_["sa"] > g_["sb"] else 0) for g_ in backfill(m_, 200)]) if done else dict(m=1.0, n=0)
    CAL_M = cal["m"]
    back = backfill(CAL_M)
    print(f"calibration: {cal}")

    # ---------- per-player record
    team_byes = {}
    played = {}
    for gm in sch if isinstance(sch, list) else []:
        for side in ("home", "away"):
            if isinstance(gm, dict) and gm.get(side):
                played.setdefault(gm[side], set()).add(int(gm["week"]))
    for tm, wks in played.items():
        miss = [w for w in range(1, 19) if w not in wks]
        if len(miss) == 1:
            team_byes[tm] = miss[0]
    P = {}
    for pid in sorted(uni):
        info = pinfo(pid)
        if info["p"] not in POS:
            continue
        o = our_mean(pid, done + 1, best_par)
        out_now = info["inj"] in ("Out", "IR", "PUP", "Sus", "Doubtful")
        sl, pr = [], []
        for i, wk in enumerate(fut):
            spv = proj.get(pid, {}).get(wk)
            avail = spv is not None and spv > 0
            if i == 0 and out_now:
                avail = False
            if info["inj"] in ("IR", "PUP", "Sus") and i < 4:
                avail = False
            if not avail:
                sl.append(0); pr.append(0); continue
            q = BW if info["p"] in SKILL else 0.85
            sl.append(round(spv, 2)); pr.append(round(q * spv + (1 - q) * o, 2))
        vals = [v for _, v in g(pid, season, done + 1)] + [v for _, v in g(pid, season - 1)]
        cvb = CV.get(info["p"], 0.6)
        if len(vals) >= 5 and o > 0:
            sd = (len(vals) * st.pstdev(vals) + 6 * cvb * o) / (len(vals) + 6)
            cv = min(1.2, max(0.25, sd / max(o, 1)))
        else:
            cv = cvb
        cv = min(1.2, max(0.25, cv * CAL_M))
        tm = info["t"]
        d = dict(n=info["n"], p=info["p"], t=tm, age=info["age"], inj=info["inj"], r=rostered.get(pid, 0),
                 a=[pts.get(pid, {}).get(f"{season}-{w}") for w in range(1, done + 1)],
                 sl=sl, pr=pr, es=[round(es_proj.get(pid, {}).get(w, 0), 1) for w in fut[:4]], it=[it_lines.get((tm, w)) for w in fut[:4]],
                 sl0=[round(proj_sl.get(pid, {}).get(w, 0), 1) for w in fut[:4]], cv=round(cv, 3), bye=team_byes.get(tm), fc=fc_vals.get(pid, 0), fct=fc_trend.get(pid, 0),
                 opp=[opp_tw.get((tm, w), "") for w in fut[:4]],
                 mf=[(DEFF.get(opp_tw.get((tm, w)), {}) or {}).get(info["p"]) for w in fut[:4]])
        if pid in on_ir:
            d["ir"] = 1
        if pid not in rostered:
            d["fa"] = 1
            if pid in recent_drop:
                d["wv"] = 1
        st_ = game_state.get(tm)
        if fut and st_ in ("complete", "in_progress"):
            d["lk"] = [round(live_pp.get(pid, 0.0), 2), 0 if st_ == "complete" else 0.5]
        P[pid] = d

    # ---------- simulation draws (same hash as the page, so both show the same odds)
    VAL = {}
    for pid, d in P.items():
        rows = []
        for s_ in range(SIMS):
            row = []
            for k, wk in enumerate(fut):
                if k == 0 and d.get("lk"):
                    lk = d["lk"]
                    row.append(lk[0] + (sim(d["pr"][0] * lk[1], d["cv"], pid, d["t"], s_, wk, 0) if lk[1] > 0 else 0.0)); continue
                pm = B.P_MISS_STATUS.get(d["inj"], B.P_MISS) if k == 0 else B.P_MISS
                row.append(sim(d["pr"][k], d["cv"], pid, d["t"], s_, wk, pm))
            rows.append(row)
        VAL[pid] = rows

    def active(rid):
        return [p for p, d in P.items() if d["r"] == rid and not d.get("ir")]

    def exp_k(pid, k):
        d = P[pid]
        if k == 0 and d.get("lk"):
            return d["lk"][0] + d["pr"][0] * d["lk"][1]
        return d["pr"][k]

    def locked_of(rid):
        """This week's starters whose games have kicked off can't be moved."""
        cs = cur_starters.get(rid) or []
        return {i: p for i, p in enumerate(cs) if i < len(slots) and p in P and P[p].get("lk")}

    def lineup_k(ids, k, rid=None, keep_current=False):
        """Starters for week fut[k]. Week 0 respects locked players; keep_current keeps the manager's current
        choices except obvious holes (out, on bye, empty)."""
        items = [(p, P[p]["p"], exp_k(p, k)) for p in ids if p in P]
        if k == 0 and rid is not None:
            lock = locked_of(rid)
            if keep_current:
                cs = cur_starters.get(rid) or []
                lock = dict(lock)
                for i, p in enumerate(cs[:len(slots)]):
                    if p in P and p in ids and exp_k(p, 0) > 0 and i not in lock:
                        lock[i] = p
            return choose(items, slots, lock)
        return choose(items, slots)

    # replacement level: what a manager can stream off waivers in a given week (3rd-best free agent at the position).
    # Empty slots (byes, injuries) are filled at this level, so a pickup isn't credited just for covering a bye week.
    repl = {}
    for k, wk in enumerate(fut):
        for ps in POS:
            vs = sorted((v.get(wk, 0) for p_, v in proj.items() if p_ not in rostered and (players_db.get(p_) or {}).get("position") == ps), reverse=True)
            repl[(ps, k)] = round(vs[2] if len(vs) > 2 else (vs[-1] if vs else 0), 2)

    def fill(lu, k):
        return sum(max(repl.get((ps, k), 0) for ps in ELIG[slots[i]]) for i, p in enumerate(lu) if not p)

    def lu_pts(lu, k):
        return sum(exp_k(p, k) for p in lu if p) + fill(lu, k)

    def det_total(ids, ks=None, rid=None):
        ks = range(len(fut)) if ks is None else ks
        return sum(lu_pts(lineup_k(ids, k, rid), k) for k in ks)

    ids_by = {t["id"]: active(t["id"]) for t in teams}
    LU = {t["id"]: [lineup_k(ids_by[t["id"]], k, t["id"], keep_current=(k == 0)) for k in range(len(fut))] for t in teams}
    FILL = {r: [fill(LU[r][k], k) for k in range(len(fut))] for r in LU}

    # ---------- standings so far
    rec = {t["id"]: [0, 0, 0.0, 0.0] for t in teams}  # W, L, PF, PA
    for wk, rows in weeks.items():
        scores = [r[2] for r in rows]; med = st.median(scores) if scores else 0
        for rid, mid, sc_ in rows:
            opp = next((r[2] for r in rows if r[1] == mid and r[0] != rid), None)
            rec[rid][2] += sc_
            if opp is not None:
                rec[rid][3] += opp
                rec[rid][0 if sc_ > opp else 1] += 1
            if median_on:
                rec[rid][0 if sc_ > med else 1] += 1
    rem = [wk for wk in fut if wk <= reg_end]
    po_k = [fut.index(wk) for wk in fut if wk > reg_end]
    this_week = fut[0] if fut else None

    def week_scores(lus, s_, k):
        return {r: sum(VAL[p][s_][k] for p in lus[r][k] if p) + fill(lus[r][k], k) for r in lus}

    def run_season(lus):
        T = list(lus)
        res = {r: dict(playoffs=0, bye=0, title=0, xw=0.0, wp=0, seed=[0] * len(T)) for r in T}
        for s_ in range(SIMS):
            W = {r: rec[r][0] for r in T}; PF = {r: rec[r][2] for r in T}
            sc = {}
            for k in range(len(fut)):
                sc[k] = week_scores(lus, s_, k)
            for wk in rem:
                k = fut.index(wk); vals = sc[k]; med = st.median(vals.values())
                for r, mid in sched.get(wk, []):
                    o = next((x for x, m2 in sched[wk] if m2 == mid and x != r), None)
                    if o is not None and vals[r] > vals[o]:
                        W[r] += 1
                        if wk == this_week:
                            res[r]["wp"] += 1
                    if median_on and vals[r] > med:
                        W[r] += 1
                    PF[r] += vals[r]
            order = sorted(T, key=lambda r: (W[r], PF[r]), reverse=True)
            for i, r in enumerate(order):
                res[r]["xw"] += W[r]; res[r]["seed"][i] += 1
            seeds = order[:n_playoff]
            for i, r in enumerate(seeds):
                res[r]["playoffs"] += 1
                if i < 2 and n_playoff == 6:
                    res[r]["bye"] += 1
            psc = lambda r, i: sc[fut.index(reg_end + 1 + i)][r] if (reg_end + 1 + i) in fut else 0.0
            if n_playoff == 6 and len(po_k) >= 3:
                gg = lambda x, y, i: x if psc(x, i) >= psc(y, i) else y
                a_ = gg(seeds[2], seeds[5], 0); b_ = gg(seeds[3], seeds[4], 0)
                champ = gg(gg(seeds[0], b_, 1), gg(seeds[1], a_, 1), 2)
                res[champ]["title"] += 1
            elif n_playoff == 4 and len(po_k) >= 2:
                gg = lambda x, y, i: x if psc(x, i) >= psc(y, i) else y
                res[gg(gg(seeds[0], seeds[3], 0), gg(seeds[1], seeds[2], 0), 1)]["title"] += 1
        for r in T:
            for k2 in ("playoffs", "bye", "title", "wp"):
                res[r][k2] = round(res[r][k2] / SIMS, 3)
            res[r]["xw"] = round(res[r]["xw"] / SIMS, 2)
            res[r]["seed"] = [round(x / SIMS, 3) for x in res[r]["seed"]]
        return res

    S0 = run_season(LU) if fut else {t["id"]: dict(playoffs=0, bye=0, title=0, xw=rec[t["id"]][0], wp=0, seed=[]) for t in teams}

    # weekly projected scores and ranges
    wk_mean = {r: [lu_pts(LU[r][k], k) for k in range(len(fut))] for r in LU}
    rng = {}
    if fut:
        for r in LU:
            xs = sorted(sum(VAL[p][s_][0] for p in LU[r][0] if p) + FILL[r][0] for s_ in range(SIMS))
            rng[r] = [round(xs[int(0.1 * SIMS)], 1), round(xs[int(0.9 * SIMS)], 1)]

    # ---------- this week: start/sit for me
    def my_opp(k):
        wk = fut[k]
        pr_ = sched.get(wk) or []
        mine = next((m for r, m in pr_ if r == me), None)
        return next((r for r, m in pr_ if m == mine and r != me), None)

    lineup_now = cur_starters.get(me) or []
    startsit = None
    if fut:
        opp0 = my_opp(0)
        my_ids = ids_by[me]
        best0 = lineup_k(my_ids, 0, me)
        cur0 = LU[me][0]
        opp_sc = [sum(VAL[p][s_][0] for p in LU[opp0][0] if p) + FILL[opp0][0] for s_ in range(SIMS)] if opp0 else None

        def win_with(lu):
            if not opp_sc:
                return None
            f_ = fill(lu, 0)
            return round(sum(1 for s_ in range(SIMS) if sum(VAL[p][s_][0] for p in lu if p) + f_ > opp_sc[s_]) / SIMS, 3)

        def rng_of(p):
            xs = sorted(VAL[p][s_][0] for s_ in range(SIMS))
            return [round(xs[int(0.1 * SIMS)], 1), round(xs[int(0.5 * SIMS)], 1), round(xs[int(0.9 * SIMS)], 1)]

        # moves: who to start and who to sit (players, not slot shuffles); paired best-in with worst-out by position fit
        now_set = {p for p in lineup_now[:len(slots)] if p in P}
        ins = sorted((p for p in best0 if p and p not in now_set), key=lambda p: -exp_k(p, 0))
        outs = sorted((p for p in now_set if p not in best0), key=lambda p: exp_k(p, 0))
        moves = []
        empty_now = [slots[i] for i, p in enumerate(lineup_now[:len(slots)]) if p not in P]
        for b in ins:
            a = next((x for x in outs if P[x]["p"] == P[b]["p"]), None) or (outs[0] if outs else None)
            if a:
                outs.remove(a)
            moves.append(dict(slot=P[b]["p"], out=a, inn=b, gain=round(exp_k(b, 0) - (exp_k(a, 0) if a else 0), 1)))
        for a in outs:
            moves.append(dict(slot=P[a]["p"], out=a, inn=None, gain=0))
        # alternatives worth a look: a bench player close to a starter he could replace
        alts = []
        for b in my_ids:
            if b in best0 or exp_k(b, 0) <= 0 or P[b].get("lk"):
                continue
            for i, a in enumerate(best0):
                if not a or P[a].get("lk") or P[b]["p"] not in ELIG[slots[i]]:
                    continue
                gap = exp_k(a, 0) - exp_k(b, 0)
                if 0 <= gap <= 3:
                    alt = list(best0); alt[i] = b
                    alts.append(dict(start=a, alt=b, slot=slots[i], gap=round(gap, 1), wp_a=win_with(best0), wp_b=win_with(alt)))
        alts.sort(key=lambda x: x["gap"])
        seen_alt, alts2 = set(), []
        for x in alts:
            if x["alt"] in seen_alt:
                continue
            seen_alt.add(x["alt"]); alts2.append(x)
        startsit = dict(slots=slots, now=lineup_now[:len(slots)], best=best0, moves=moves, alts=alts2[:5], opp=opp0,
                        wp_now=win_with(cur0), wp_best=win_with(best0),
                        proj_now=round(lu_pts(cur0, 0), 1), proj_best=round(lu_pts(best0, 0), 1), raw_now=round(sum(exp_k(p, 0) for p in lineup_now[:len(slots)] if p in P), 1),
                        opp_proj=round(wk_mean[opp0][0], 1) if opp0 else None, opp_lu=LU[opp0][0] if opp0 else [],
                        rng={p: rng_of(p) for p in set(my_ids) | set(LU[opp0][0] if opp0 else []) if p},
                        empty=empty_now + [s_ for s_ in slots[len(lineup_now):]], holes=[slots[i] for i, p in enumerate(best0) if not p])

    # ---------- waivers: who helps my lineup most (rolling waivers: claims send me to the back of the line)
    my_ids = ids_by[me]
    roster_max = len(slots) + n_bench
    open_spot = len(my_ids) < roster_max
    base_tot = det_total(my_ids, rid=me) if fut else 0
    base_po = det_total(my_ids, po_k, rid=me) if po_k else 0
    base_k0 = det_total(my_ids, [0], rid=me) if fut else 0
    drop_cost = {}
    for d_ in my_ids:
        rest = [p for p in my_ids if p != d_]
        drop_cost[d_] = base_tot - det_total(rest, rid=me)
    cheap = sorted(my_ids, key=lambda p: drop_cost[p])
    # never suggest dropping the only kicker or defense if one is needed
    waivers = []
    if fut:
        for f in fa_pool:
            if f not in P or P[f]["p"] in ("K", "DEF"):   # kickers/defenses: see the streaming table
                continue
            best = None
            fpos = P[f]["p"]
            if fpos in ("K", "DEF"):   # kickers and defenses: a straight swap for the one you have
                cands = [d_ for d_ in my_ids if P[d_]["p"] == fpos] or ([None] if open_spot else cheap[:3])
            else:                      # never drop your only kicker or defense to make room for a position player
                solo = {d_ for d_ in my_ids if P[d_]["p"] in ("K", "DEF") and sum(1 for x in my_ids if P[x]["p"] == P[d_]["p"]) == 1}
                cands = ([None] if open_spot else []) + [d_ for d_ in cheap if d_ not in solo][:6]
            for d_ in cands:
                ids2 = [p for p in my_ids if p != d_] + [f]
                tot = det_total(ids2, rid=me)
                if best is None or tot > best[0]:
                    best = (tot, d_, ids2)
            if not best:
                continue
            gain = (best[0] - base_tot) / NW
            if gain <= 0.05:
                continue
            g0 = det_total(best[2], [0], rid=me) - base_k0
            gpo = (det_total(best[2], po_k, rid=me) - base_po) / len(po_k) if po_k else None
            waivers.append(dict(id=f, drop=best[1], g=round(gain, 2), g0=round(g0, 1), gpo=round(gpo, 2) if gpo is not None else None,
                                ros=round(sum(P[f]["pr"]), 1)))
        waivers.sort(key=lambda x: -(x["g"] * len(fut) + x["g0"]))
    wpos = {t["id"]: t["wpos"] for t in teams}

    # ---------- streaming kickers and defenses: best available each of the next 3 weeks
    stream = []
    for ps in ("DEF", "K"):
        mine_ps = [p for p in my_ids if P[p]["p"] == ps]
        for k in range(min(3, len(fut))):
            cur_best = max(mine_ps, key=lambda p: exp_k(p, k), default=None)
            fas = sorted((p for p in fa_pool if p in P and P[p]["p"] == ps), key=lambda p: -exp_k(p, k))[:3]
            stream.append(dict(p=ps, wk=fut[k], mine=cur_best, mine_v=round(exp_k(cur_best, k), 1) if cur_best else 0,
                               fa=[[p, round(exp_k(p, k), 1)] for p in fas]))

    # ---------- trades: rest-of-season swaps that help both lineups (deterministic projections; odds for the best)
    by_id = {t["id"]: t for t in teams}
    base_of = {t["id"]: det_total(ids_by[t["id"]], rid=t["id"]) for t in teams} if fut else {}
    trade_ideas = []
    tradeable = lambda p: P[p]["p"] in SKILL and P[p].get("fc", 0) > 0
    if fut and (not deadline or cur <= deadline):
        mine_t = [p for p in my_ids if tradeable(p)]
        for t in teams:
            if t["id"] == me:
                continue
            theirs = [p for p in ids_by[t["id"]] if tradeable(p)]
            cand = []
            gives = [(a,) for a in mine_t] + list(itertools.combinations(mine_t, 2))
            for gv in gives:
                for b in theirs:
                    fg = sorted((P[x]["fc"] for x in gv), reverse=True)
                    gv_val = fg[0] + (0.6 * fg[1] if len(fg) > 1 else 0)
                    fair = gv_val / max(P[b]["fc"], 1)
                    if fair < 0.9 or fair > 1.4:
                        continue
                    mine2 = [p for p in my_ids if p not in gv] + [b]
                    th2 = [p for p in ids_by[t["id"]] if p != b] + list(gv)
                    dm = (det_total(mine2, rid=me) - base_of[me]) / NW
                    if dm < 0.6:
                        continue
                    dt = (det_total(th2, rid=t["id"]) - base_of[t["id"]]) / NW
                    if dt < 0.2:
                        continue
                    cand.append(dict(team=t["id"], give=list(gv), get=[b], dm=round(dm, 2), dt=round(dt, 2), fair=round(fair, 2)))
            # prefer the simpler deal: drop a 2-for-1 when giving just one of those players does about as well
            cand = [c for c in cand if not (len(c["give"]) > 1 and any(len(o["give"]) == 1 and o["get"] == c["get"] and o["give"][0] in c["give"]
                                                                     and o["dm"] >= c["dm"] - 0.2 for o in cand))]
            cand.sort(key=lambda x: -(x["dm"] + 0.5 * min(x["dt"], 2)))
            seen_get = set(); keep = []
            for c in cand:
                if c["get"][0] in seen_get:
                    continue
                seen_get.add(c["get"][0]); keep.append(c)
            trade_ideas += keep[:2]
        trade_ideas.sort(key=lambda x: -(x["dm"] + 0.5 * min(x["dt"], 2)))
        trade_ideas = trade_ideas[:6]
        for ti in trade_ideas:
            lus = dict(LU)
            m2 = [p for p in my_ids if p not in ti["give"]] + ti["get"]
            t2 = [p for p in ids_by[ti["team"]] if p not in ti["get"]] + ti["give"]
            lus[me] = [lineup_k(m2, k, me) for k in range(len(fut))]
            lus[ti["team"]] = [lineup_k(t2, k, ti["team"]) for k in range(len(fut))]
            r2 = run_season(lus)
            ti["po"] = [S0[me]["playoffs"], r2[me]["playoffs"]]; ti["ti"] = [S0[me]["title"], r2[me]["title"]]
            ti["their_po"] = [S0[ti["team"]]["playoffs"], r2[ti["team"]]["playoffs"]]
    print(f"waivers {len(waivers)}, trade ideas {len(trade_ideas)}")

    # ---------- research: weekly role and what it's worth (skill positions); K/DEF weekly points only
    XF = {}
    xrow = lambda e_: [e_[4], e_[3], e_[2], e_[5], e_[6], e_[7] / 10.0, 1.0]
    for ps in SKILL:
        X_, y_ = [], []
        for pid_, d_ in ext.items():
            if (players_db.get(pid_) or {}).get("position") != ps:
                continue
            for k_, e_ in d_.items():
                if k_ in pts.get(pid_, {}) and (e_[0] > 0 or e_[2] + e_[3] + e_[4] > 0):
                    X_.append(xrow(e_)); y_.append(pts[pid_][k_])
        XF[ps] = B.lstsq(X_, y_) if len(X_) > 60 else None
    research = []
    for pid_ in set(ext) | {p for p in P if P[p]["p"] in ("K", "DEF")}:
        info_ = pinfo(pid_)
        if info_["p"] not in POS:
            continue
        d_ = ext.get(pid_, {})
        rows_ = []; mx_opp = 0
        for wk in range(1, done + 1):
            k_ = f"{season}-{wk}"; e_ = d_.get(k_)
            if info_["p"] in ("K", "DEF"):
                v_ = pts.get(pid_, {}).get(k_)
                rows_.append(None if v_ is None else [round(v_, 1)]); continue
            if not e_:
                rows_.append(None); continue
            tt_ = tm_tot.get((info_["t"] or "", k_), [0, 0])
            xf_ = sum(c * x for c, x in zip(XF[info_["p"]], xrow(e_))) if XF.get(info_["p"]) else None
            opp_ = e_[4] + e_[3] if info_["p"] == "QB" else e_[2] + e_[3]
            mx_opp = max(mx_opp, opp_)
            rows_.append([round(pts.get(pid_, {}).get(k_, 0), 1), round(e_[0] / e_[1], 3) if e_[1] else None, int(e_[2]), int(e_[3]), int(e_[4]),
                          int(e_[5] + e_[6]), round(e_[2] / tt_[0], 3) if tt_[0] else None, round(max(xf_, 0), 1) if xf_ is not None else None])
        own_ = rostered.get(pid_, 0)
        if info_["p"] in SKILL and not own_ and pid_ not in P and (not info_["t"] or mx_opp < 4):
            continue
        if all(w_ is None for w_ in rows_):
            continue
        if info_["p"] in ("K", "DEF") and pid_ not in P:
            continue
        ros_ = P[pid_]["pr"] if pid_ in P else [round(proj.get(pid_, {}).get(w, 0), 1) for w in fut]
        research.append(dict(id=pid_, n=info_["n"], p=info_["p"], t=info_["t"], r=own_, inj=info_["inj"], w=rows_,
                             pr=[round(x, 1) for x in ros_[:4]], ros=round(sum(ros_) / max(1, sum(1 for x in ros_ if x > 0)), 1),
                             fc=fc_vals.get(pid_, 0), fct=fc_trend.get(pid_, 0), ir=1 if pid_ in on_ir else 0,
                             opp=[opp_tw.get((info_["t"], w), "") for w in fut[:4]],
                             mf=[(DEFF.get(opp_tw.get((info_["t"], w)), {}) or {}).get(info_["p"]) for w in fut[:4]]))
    print(f"research rows {len(research)}")

    # ---------- lineup efficiency: points left on the bench each week (best possible lineup in hindsight vs. what was started)
    eff = {t["id"]: [] for t in teams}
    for wk in range(1, done + 1):
        for rid, pp in pp_wk.get(wk, {}).items():
            items = [(p, (players_db.get(p) or {}).get("position"), v) for p, v in pp.items() if (players_db.get(p) or {}).get("position") in POS]
            best_ = sum(v for p, _, v in items if p in set(choose(items, slots)))
            act_ = next((r[2] for r in weeks[wk] if r[0] == rid), 0)
            eff[rid].append([wk, round(act_, 2), round(best_, 2)])

    # ---------- prediction log (before kickoff) and grading
    gen = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    pred_path = os.path.join(OUTDIR, "predictions.json")
    try:
        PRED = json.load(open(pred_path))
    except Exception:  # noqa
        PRED = {}
    PRED.setdefault("weeks", {}); PRED.setdefault("odds", [])
    if fut and not week_started:
        PRED["weeks"][f"{season}-{fut[0]}"] = dict(made=gen, wp={str(r): S0[r]["wp"] for r in S0}, proj={str(r): round(wk_mean[r][0], 1) for r in wk_mean})
    day_ = gen[:10]
    row_ = dict(d=day_, wk=this_week, t={str(r): [S0[r]["playoffs"], S0[r]["title"]] for r in S0})
    if PRED["odds"] and PRED["odds"][-1].get("d") == day_:
        PRED["odds"][-1] = row_
    else:
        PRED["odds"].append(row_)
    PRED["odds"] = PRED["odds"][-200:]
    if not B.FIX:
        json.dump(PRED, open(pred_path, "w"), separators=(",", ":"))
    graded = []
    for key_, r_ in PRED["weeks"].items():
        yr_, wk_ = map(int, key_.split("-"))
        if yr_ != season or wk_ not in weeks:
            continue
        rows = weeks[wk_]; act = {x[0]: x[2] for x in rows}
        for rid, mid, sc_ in rows:
            o = next((x[0] for x in rows if x[1] == mid and x[0] != rid), None)
            if o is not None and rid < o and str(rid) in r_["wp"]:
                graded.append([wk_, r_["wp"][str(rid)], 1 if sc_ > act[o] else 0])

    # ---------- the to-do list
    todo = []
    if startsit:
        for mv in startsit["moves"]:
            if mv["inn"] and mv["gain"] >= 0.3:
                todo.append(dict(kind="lineup", text=f"Start {P[mv['inn']]['n']}" + (f" over {P[mv['out']]['n']}" if mv["out"] else f" in your empty {mv['slot']} spot"),
                                 why=f"+{mv['gain']:.1f} projected points", gain=mv["gain"]))
        for a in startsit["alts"]:
            if a["wp_b"] is not None and a["wp_a"] is not None and a["wp_b"] - a["wp_a"] >= 0.02:
                why = f"{int(a['wp_b'] * 100 + 0.5)}% vs {int(a['wp_a'] * 100 + 0.5)}% win chance"
                if P[a["start"]]["inj"] == "Questionable":
                    why += f"; {P[a['start']]['n']} is questionable"
                todo.append(dict(kind="lineup", text=f"Consider starting {P[a['alt']]['n']} over {P[a['start']]['n']}", why=why, gain=0))
    if waivers:
        w0 = waivers[0]
        if w0["g"] >= 0.8 or w0["g0"] >= 2:
            todo.append(dict(kind="waiver", text=f"Add {P[w0['id']]['n']}" + (f", drop {P[w0['drop']]['n']}" if w0["drop"] else ""),
                             why=f"+{w0['g']:.1f} points a week for the rest of the season" + (f", +{w0['g0']:.1f} this week" if w0["g0"] >= 0.5 else ""), gain=w0["g"]))
    for s_ in stream:
        if s_["wk"] == this_week and s_["fa"] and s_["fa"][0][1] - s_["mine_v"] >= 2:
            todo.append(dict(kind="stream", text=f"Stream {P[s_['fa'][0][0]]['n']} this week", why=f"{s_['fa'][0][1]:.1f} projected vs {s_['mine_v']:.1f} for yours", gain=s_["fa"][0][1] - s_["mine_v"]))
    if trade_ideas and trade_ideas[0]["dm"] >= 1.0:
        ti = trade_ideas[0]
        todo.append(dict(kind="trade", text=f"Offer {by_id[ti['team']]['name']}: " + " + ".join(P[x]["n"] for x in ti["give"]) + " for " + " + ".join(P[x]["n"] for x in ti["get"]),
                         why=f"+{ti['dm']:.1f} points a week for you, +{ti['dt']:.1f} for them", gain=ti["dm"]))

    data = dict(
        meta=dict(league=league.get("name", "").strip(), season=season, cur=cur, done=done, fut=fut, reg_end=reg_end, me=me, sims=SIMS,
                  generated=gen, started=week_started, slots=slots, n_playoff=n_playoff, deadline=deadline, median=median_on,
                  waiver=dict(type=S.get("waiver_type"), mine=wpos.get(me), order=wpos), scoring=dict(ppr=league["scoring_settings"].get("rec", 0),
                  pass_td=league["scoring_settings"].get("pass_td", 4)), checks=dict(lineups=checks["lineups"], lineup_bad=checks["lineup_bad"][:20],
                  scored=checks["scored"], score_bad=checks["score_bad"][:20]), backtest=backtest, sources=src_rep, calib=cal, value_src="FantasyCalc redraft values (12-team, 1QB, full PPR)"),
        teams=[dict(id=t["id"], name=t["name"], user=t["user"], w=rec[t["id"]][0], l=rec[t["id"]][1], pf=round(rec[t["id"]][2], 2), pa=round(rec[t["id"]][3], 2),
                    wpos=t["wpos"], moves=t["moves"], ppts=round(t["ppts"], 2)) for t in teams],
        odds={str(r): v for r, v in S0.items()}, repl={f"{ps}|{k}": v for (ps, k), v in repl.items()}, wk_mean={str(r): [round(x, 1) for x in v] for r, v in wk_mean.items()}, rng={str(r): v for r, v in rng.items()},
        lineups={str(r): v for r, v in LU.items()}, cur_starters={str(r): v for r, v in cur_starters.items()}, live={str(r): v for r, v in live.items()},
        weeks={str(k): v for k, v in weeks.items()}, sched={str(k): v for k, v in sched.items()},
        players=P, startsit=startsit, waivers=waivers[:25], stream=stream, trade_ideas=trade_ideas, trades=trades[:30],
        research=research, defense=DEFF, eff={str(r): v for r, v in eff.items()}, back=back,
        accuracy=dict(graded=graded, odds=[dict(d=o_["d"], wk=o_["wk"], po=o_["t"].get(str(me), [None, None])[0], t=o_["t"].get(str(me), [None, None])[1]) for o_ in PRED["odds"]],
                      logged=sorted(PRED["weeks"])),
        todo=todo)
    out = os.path.join(OUTDIR, "data.json")
    json.dump(data, open(out, "w"), separators=(",", ":"))
    mo = S0.get(me, {})
    print(f"wrote {out}: week {cur}, done {done}, {len(P)} players; me {rec[me][0]}-{rec[me][1]} playoffs {mo.get('playoffs')} title {mo.get('title')} wp {mo.get('wp')}")


if __name__ == "__main__":
    main()
