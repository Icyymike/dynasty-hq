"""Futures: where dynasty values are headed, sleeper screens, contract watch, offseason risk and sell windows,
with an honest, continuously graded track record.

Data (all open): DynastyProcess weekly dynasty values (superflex) back to 2019; nflverse weekly stats, snap counts,
play-by-play (red-zone usage), players, draft picks, schedules (coaches, starting QBs) and OverTheCap contracts;
plus the app's own daily archive (Sleeper depth charts and injuries) for live-only screens.

Two models, both predicting each player's value change *compared with players at a similar value* (rank bands of 20):
  in-season (weeks 3-17): 4 weeks, 8 weeks and next August ahead, from usage, production vs price, age, contract,
    role quality (air yards, red zone) and team context;
  offseason (Jan 10 - Aug 1): value at the end of August, from last season, contract status and offseason events
    (changed teams, team drafted a player at his position, new starting QB).
Each is ridge regression, optionally averaged with gradient boosting; feature groups and the ensemble are kept only
if they improve out-of-sample results. Tested walk-forward by season against no-change, momentum and age-curve
rules; used only if it beats them. Calls are shrunk per value tier to match what really happened, and turned into
odds of gaining or losing 25%+ from the out-of-sample misses. Retrained weekly; weekly calls are logged and graded.

Needs pandas, numpy, pyarrow, scikit-learn (the futures workflow installs them).
Writes docs/futures.json, docs/futures_model.json, docs/futures_log.json.
"""
import os, re, sys, json, time, math, glob, pickle, datetime, subprocess, urllib.request
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
CACHE = os.path.join(ROOT, "cache", "futures")
os.makedirs(os.path.join(CACHE, "dp"), exist_ok=True)
NV = "https://github.com/nflverse/nflverse-data/releases/download"
DPRAW = "https://raw.githubusercontent.com/dynastyprocess/data/master/files"
FIRST = 2019
VMIN = 100     # DynastyProcess superflex scale: ~200 players a week clear 100 (rank 100 is ~900)
HORIZONS = {"p4": 28, "p8": 56, "aug": None}
TIERS = ["top24", "25-60", "61-120", "121+"]
OLD = {"RB": 28, "WR": 30, "TE": 30, "QB": 33}
UP, DOWN = math.log(1.25), math.log(0.8)
TEAM = {"JAC": "JAX", "LAR": "LA", "STL": "LA", "KCC": "KC", "KAN": "KC", "GBP": "GB", "GNB": "GB", "LVR": "LV", "OAK": "LV",
        "NEP": "NE", "NWE": "NE", "NOS": "NO", "NOR": "NO", "SFO": "SF", "TBB": "TB", "TAM": "TB", "SD": "LAC", "SDG": "LAC", "WSH": "WAS", "FA": None}
tnorm = lambda t: TEAM.get(t, t) if isinstance(t, str) else None
TODAY = pd.Timestamp(datetime.date.today())
log = lambda *a: print("[futures]", *a, flush=True)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fetch(url, name, max_age_h):
    p = os.path.join(CACHE, name)
    if os.path.exists(p) and (max_age_h is None or time.time() - os.path.getmtime(p) < max_age_h * 3600):
        return p
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dynasty-hq/1.0"})
            with urllib.request.urlopen(req, timeout=180) as r, open(p + ".tmp", "wb") as f:
                f.write(r.read())
            os.replace(p + ".tmp", p)
            return p
        except Exception as e:  # noqa
            log("retry", url, e); time.sleep(3 * (i + 1))
    if os.path.exists(p):
        return p
    raise RuntimeError(f"could not download {url}")


def season_of(d):
    """NFL season a date belongs to for in-season purposes (Jan-Feb belong to the previous season)."""
    return d.year if d.month >= 3 else d.year - 1


def week1(y):
    lab = datetime.date(y, 9, 1)
    lab += datetime.timedelta(days=(0 - lab.weekday()) % 7)  # Labor Day
    return pd.Timestamp(lab + datetime.timedelta(days=3))


def week_done(d):
    y = season_of(d)
    w1 = week1(y)
    if d < w1 + pd.Timedelta(days=5):
        return y, 0
    days = (d - w1).days
    return y, min(days // 7 + (1 if days % 7 >= 5 else 0), 18 if y >= 2021 else 17)


def offseason(d):
    """Offseason window (Jan 10 - Aug 1): returns the season just finished, else None."""
    if (d.month == 1 and d.day >= 10) or 2 <= d.month <= 7 or (d.month == 8 and d.day == 1):
        return d.year - 1
    return None


# ---------------------------------------------------------------- dynasty value history
def _has(g, ref):
    return subprocess.run(["git", "-C", g, "rev-parse", "--verify", "-q", ref], capture_output=True).returncode == 0


def dp_history():
    g = os.path.join(CACHE, "dpgit")
    if not os.path.isdir(os.path.join(g, ".git")):
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", "https://github.com/dynastyprocess/data.git", g], check=True)
    else:
        subprocess.run(["git", "-C", g, "fetch", "-q", "origin"], check=False)
    ref = "origin/HEAD" if _has(g, "origin/HEAD") else ("origin/master" if _has(g, "origin/master") else "HEAD")
    out = subprocess.run(["git", "-C", g, "log", ref, f"--since={FIRST}-08-01", "--format=%h %ad", "--date=short", "--", "files/values.csv"],
                         capture_output=True, text=True).stdout.split("\n")
    seen = {}
    for line in out:
        if line.strip():
            h, d = line.split()
            seen.setdefault(datetime.date.fromisoformat(d).isocalendar()[:2], (h, d))
    for h, d in seen.values():
        p = os.path.join(CACHE, "dp", f"{d}.csv")
        if not os.path.exists(p) or os.path.getsize(p) < 5000:
            with open(p, "w") as f:
                subprocess.run(["git", "-C", g, "show", f"{h}:files/values.csv"], stdout=f, stderr=subprocess.DEVNULL)
    return fetch(f"{DPRAW}/values.csv", "values_now.csv", 6)


norm = lambda s: re.sub(r"[^a-z]", "", re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", str(s).lower()))


def load_values(ids, cur_path):
    fp2sl = ids.dropna(subset=["fantasypros_id", "sleeper_id"]).set_index("fantasypros_id")["sleeper_id"].to_dict()
    nm2sl = {}
    for r in ids.dropna(subset=["sleeper_id"]).itertuples():
        nm2sl.setdefault((norm(r.merge_name), r.position), r.sleeper_id)
    files = [(f[:-4], os.path.join(CACHE, "dp", f)) for f in sorted(os.listdir(os.path.join(CACHE, "dp"))) if f.endswith(".csv")]
    files.append((TODAY.strftime("%Y-%m-%d"), cur_path))
    rows = []
    for d, p in files:
        try:
            t = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
        except Exception:  # noqa
            continue
        t.columns = [c.strip() for c in t.columns]
        v, nmc = ("value_2qb", "player") if "value_2qb" in t.columns else ("2QB Value", "mergename")
        if v not in t.columns:
            continue
        t = t.rename(columns={v: "v", nmc: "name"})
        t = t[t["pos"].isin(["QB", "RB", "WR", "TE"])].copy()
        fp = t["fp_id"] if "fp_id" in t.columns else pd.Series([None] * len(t), index=t.index)
        t["sid"] = [fp2sl.get(str(f)) or nm2sl.get((norm(n), p_)) for f, n, p_ in zip(fp, t["name"], t["pos"])]
        t["date"] = pd.Timestamp(d)
        t["v"] = pd.to_numeric(t["v"], errors="coerce")
        t["age_dp"] = pd.to_numeric(t["age"], errors="coerce")
        t["team"] = t["team"].map(tnorm)
        rows.append(t.dropna(subset=["sid", "v"])[["sid", "date", "pos", "name", "team", "v", "age_dp"]])
    return pd.concat(rows).drop_duplicates(["sid", "date"], keep="last")


# ---------------------------------------------------------------- usage, bio, contracts, events
def load_usage(ids, last_season):
    g2sl = ids.dropna(subset=["gsis_id", "sleeper_id"]).set_index("gsis_id")["sleeper_id"].to_dict()
    pfr2sl = ids.dropna(subset=["pfr_id", "sleeper_id"]).set_index("pfr_id")["sleeper_id"].to_dict()
    st, sn, rz, trz = [], [], [], []
    for y in range(FIRST, last_season + 1):
        age = 12 if y >= last_season else None
        try:
            s = pd.read_csv(fetch(f"{NV}/stats_player/stats_player_week_{y}.csv", f"stats_{y}.csv", age), low_memory=False)
        except Exception as e:  # noqa
            log("no stats", y, e); continue
        s = s[(s["season_type"] == "REG")].copy()
        s["team"] = s["team"].map(tnorm)
        tm = s.groupby(["team", "week"]).agg(tm_tgt=("targets", "sum"), tm_car=("carries", "sum"), tm_att=("attempts", "sum"), tm_pepa=("passing_epa", "sum"))
        s = s[s["position"].isin(["QB", "RB", "WR", "TE"])].copy()
        s["sid"] = s["player_id"].map(g2sl)
        s = s.join(tm, on=["team", "week"])
        st.append(s[["sid", "season", "week", "team", "fantasy_points_ppr", "targets", "carries", "tm_tgt", "tm_car", "tm_att", "tm_pepa",
                     "air_yards_share", "wopr"]])
        try:
            n = pd.read_csv(fetch(f"{NV}/snap_counts/snap_counts_{y}.csv", f"snaps_{y}.csv", age), low_memory=False)
            n = n[(n["game_type"] == "REG") & n["position"].isin(["QB", "RB", "WR", "TE"])].copy()
            n["sid"] = n["pfr_player_id"].map(pfr2sl)
            sn.append(n[["sid", "season", "week", "offense_pct"]])
        except Exception as e:  # noqa
            log("no snaps", y, e)
        try:  # red-zone opportunities (targets + carries inside the 20)
            pb = pd.read_parquet(fetch(f"{NV}/pbp/play_by_play_{y}.parquet", f"pbp_{y}.parquet", age),
                                 columns=["season", "week", "season_type", "posteam", "yardline_100", "rusher_player_id", "receiver_player_id"])
            pb = pb[(pb.season_type == "REG") & (pb.yardline_100 <= 20)]
            pb = pb.assign(pid=pb.rusher_player_id.fillna(pb.receiver_player_id)).dropna(subset=["pid"])
            pb["posteam"] = pb.posteam.map(tnorm)
            a = pb.groupby(["pid", "season", "week"]).size().rename("rz").reset_index()
            a["sid"] = a.pid.map(g2sl)
            rz.append(a.dropna(subset=["sid"])[["sid", "season", "week", "rz"]])
            trz.append(pb.groupby(["posteam", "season", "week"]).size().rename("tm_rz").reset_index().rename(columns={"posteam": "team"}))
        except Exception as e:  # noqa
            log("no pbp", y, e)
    S = pd.concat(st).dropna(subset=["sid"])
    N = pd.concat(sn).dropna(subset=["sid"]).drop_duplicates(["sid", "season", "week"])
    S = S.merge(N, on=["sid", "season", "week"], how="left")
    if rz:
        S = S.merge(pd.concat(rz).drop_duplicates(["sid", "season", "week"]), on=["sid", "season", "week"], how="left")
        S = S.merge(pd.concat(trz), on=["team", "season", "week"], how="left")
    else:
        S["rz"], S["tm_rz"] = np.nan, np.nan
    S["rz"] = S["rz"].fillna(0)
    return S


def load_bio_contracts(ids):
    g2sl = ids.dropna(subset=["gsis_id", "sleeper_id"]).set_index("gsis_id")["sleeper_id"].to_dict()
    P = pd.read_csv(fetch(f"{NV}/players/players.csv", "players.csv", 24 * 7), dtype=str)
    P["sid"] = P["gsis_id"].map(g2sl)
    bio = P.dropna(subset=["sid"]).drop_duplicates("sid").set_index("sid")
    C = pd.read_parquet(fetch(f"{NV}/contracts/historical_contracts.parquet", "contracts.parquet", 24 * 3))
    otc2sl = P.dropna(subset=["otc_id", "sid"]).set_index("otc_id")["sid"].to_dict()
    C["sid"] = C["gsis_id"].map(g2sl)
    C.loc[C["sid"].isna(), "sid"] = C.loc[C["sid"].isna(), "otc_id"].astype(str).map(otc2sl)
    C = C.dropna(subset=["sid"])
    contracts = {}
    for r in C.itertuples():
        ys = pd.to_numeric(r.year_signed, errors="coerce")
        if pd.isna(ys):
            continue
        end = None
        try:  # last season with a real salary (void years carry only prorated bonus)
            yrs = [int(h["year"]) for h in (r.season_history if r.season_history is not None else []) if str(h.get("year", "")).isdigit()
                   and (h.get("base_salary") or 0) > 0 and (h.get("cap_number") or 0) > 0]
            end = max(yrs) if yrs else None
        except Exception:  # noqa
            end = None
        if end is None and not pd.isna(r.years):
            end = int(ys + r.years - 1)
        if end is None:
            continue
        contracts.setdefault(r.sid, []).append(dict(signed=int(ys), end=int(end), apy=float(r.apy or 0), cap=float(r.apy_cap_pct or 0), team=r.team))
    return bio, contracts


def contract_at(contracts, sid, season):
    cs = [c for c in contracts.get(sid, []) if c["signed"] <= season]
    if not cs:
        return None
    c = max(cs, key=lambda c: (c["signed"], c["end"]))
    return c if c["end"] >= season else None


def load_events(ids):
    """Draft picks by team/position/year, head coach and primary starting QB by team-season."""
    g2sl = ids.dropna(subset=["gsis_id", "sleeper_id"]).set_index("gsis_id")["sleeper_id"].to_dict()
    dr = pd.read_csv(fetch(f"{NV}/draft_picks/draft_picks.csv", "draft_picks.csv", 24 * 7), low_memory=False)
    dr = dr[dr.season >= FIRST]
    draft = {}
    for r in dr.itertuples():
        if r.position in ("QB", "RB", "WR", "TE"):
            k = (int(r.season), tnorm(r.team), r.position)
            draft[k] = min(draft.get(k, 9), int(r.round))
    g = pd.read_csv(fetch("https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv", "games.csv", 24 * 3), low_memory=False)
    g = g[(g.season >= FIRST - 1) & (g.game_type == "REG")]
    rows = pd.concat([g[["season", "week", "home_team", "home_coach", "home_qb_id"]].set_axis(["season", "week", "team", "coach", "qb"], axis=1),
                      g[["season", "week", "away_team", "away_coach", "away_qb_id"]].set_axis(["season", "week", "team", "coach", "qb"], axis=1)])
    rows["team"] = rows.team.map(tnorm)
    played = rows.dropna(subset=["qb"])
    coach = played.sort_values("week").groupby(["team", "season"]).coach.last().to_dict()
    coach_first = played.sort_values("week").groupby(["team", "season"]).coach.first().to_dict()
    qb = played.groupby(["team", "season"]).qb.agg(lambda s: s.value_counts().index[0]).map(lambda x: g2sl.get(x)).to_dict()
    return draft, coach, coach_first, qb


# ---------------------------------------------------------------- rows
def _vindex(V):
    Vp = V.pivot_table(index="date", columns="sid", values="v")
    alld = list(Vp.index)
    return {d: Vp.loc[d] for d in alld}, alld


def _near(alld, d, days):
    t = d + pd.Timedelta(days=days)
    c = [x for x in alld if abs((x - t).days) <= 4]
    return min(c, key=lambda x: abs((x - t).days)) if c else None


def _aug(alld, year):
    c = [x for x in alld if x.year == year and x.month == 8]
    return c[-1] if c else None


def _stat_agg(g):
    tm_att, tm_car = g["tm_att"].sum(), g["tm_car"].sum()
    return dict(ppg=g["fantasy_points_ppr"].mean(), snap=g["offense_pct"].mean(),
                tgt_sh=g["targets"].sum() / max(g["tm_tgt"].sum(), 1), car_sh=g["carries"].sum() / max(tm_car, 1),
                air_sh=g["air_yards_share"].mean(), wopr=g["wopr"].mean(), rz_sh=g["rz"].sum() / max(g["tm_rz"].sum(), 1),
                tm_pass=tm_att / max(tm_att + tm_car, 1), tm_qbepa=g["tm_pepa"].sum() / max(tm_att, 1))


def _bio(bio, sid, y):
    b = bio.loc[sid] if sid in bio.index else None
    return dict(exp=(y - float(b["rookie_season"])) if b is not None and str(b["rookie_season"]) not in ("nan", "") else np.nan,
                draft_rd=float(b["draft_round"]) if b is not None and str(b["draft_round"]) not in ("nan", "") else 8.0)


def build_rows(V, S, bio, contracts, dates, VI, live=False):
    """In-season rows (weeks 3-17)."""
    Vi, alld = VI
    Sg = {k: g for k, g in S.groupby("season")}
    out = []
    for date in dates:
        y, w = week_done(date)
        s = Sg.get(y)
        if live and s is not None:
            w = int(s["week"].max())
        if s is None or w < 3 or (not live and w > 17):
            continue
        s = s[s["week"] <= w]
        agg = {k: g.sort_values("week") for k, g in s.groupby("sid")}
        snap = V[V["date"] == date]
        dm4, dm12 = _near(alld, date, -28), _near(alld, date, -84)
        tgt = {} if live else {h: (_near(alld, date, dd) if dd else _aug(alld, y + 1)) for h, dd in HORIZONS.items()}
        for r in snap.itertuples():
            g = agg.get(r.sid)
            if g is None or r.v < VMIN:
                continue
            l3 = g[g["week"] > w - 3]; early = g[g["week"] <= w - 3]
            c = contract_at(contracts, r.sid, y)
            row = dict(sid=r.sid, name=r.name, date=date, season=y, week=w, pos=r.pos, v=r.v, age=r.age_dp, team=g["team"].values[-1],
                       gp_share=len(g) / w, ppg_l3=l3["fantasy_points_ppr"].mean() if len(l3) else np.nan,
                       ppg_early=early["fantasy_points_ppr"].mean() if len(early) else np.nan, miss3=3 - len(l3),
                       snap_l3=l3["offense_pct"].mean() if len(l3) else np.nan, snap_early=early["offense_pct"].mean() if len(early) else np.nan,
                       cl=(c["end"] - y) if c else np.nan, c_end=c["end"] if c else np.nan,
                       v_m4=Vi[dm4].get(r.sid, np.nan) if dm4 is not None else np.nan,
                       v_m12=Vi[dm12].get(r.sid, np.nan) if dm12 is not None else np.nan, **_stat_agg(g), **_bio(bio, r.sid, y))
            for h, d_ in tgt.items():
                row["v_" + h] = Vi[d_].get(r.sid, np.nan) if d_ is not None else np.nan
            out.append(row)
    return pd.DataFrame(out)


def build_off_rows(V, S, bio, contracts, dates, VI, EV, live=False):
    """Offseason rows (Jan 10 - Aug 1): last season's full usage, contract status and the offseason events known by then."""
    Vi, alld = VI
    draft, coach, coach_first, qb = EV
    Sg = {k: g for k, g in S.groupby("season")}
    out = []
    for date in dates:
        ps = offseason(date)
        s = Sg.get(ps) if ps else None
        if s is None:
            continue
        Y = ps + 1
        agg = {k: g.sort_values("week") for k, g in s.groupby("sid")}
        snap = V[V["date"] == date]
        # each team's most valuable QB today (known at the date), to spot a new starter
        qbs = snap[snap.pos == "QB"].sort_values("v", ascending=False).drop_duplicates("team").set_index("team")["sid"].to_dict()
        dm4, dm12 = _near(alld, date, -28), _near(alld, date, -84)
        ta = None if live else _aug(alld, Y)
        nweeks = int(s["week"].max())
        for r in snap.itertuples():
            g = agg.get(r.sid)
            if g is None or r.v < VMIN:
                continue
            last_team = g["team"].values[-1]
            team = r.team or last_team
            c = contract_at(contracts, r.sid, ps)
            drafted = draft.get((Y, team, r.pos), 9) if date >= pd.Timestamp(Y, 5, 1) else 9
            row = dict(sid=r.sid, name=r.name, date=date, season=Y, pos=r.pos, v=r.v, age=r.age_dp, team=team,
                       gp_share=len(g) / nweeks, cl=(c["end"] - ps) if c else np.nan, c_end=c["end"] if c else np.nan,
                       moved=float(bool(r.team) and team != last_team), comp12=float(drafted <= 2), comp3=float(drafted == 3),
                       coach_new=float(date >= pd.Timestamp(Y, 2, 10) and (team, Y) in coach_first and coach_first.get((team, Y)) != coach.get((team, ps))),
                       qb_new=float(r.pos != "QB" and qbs.get(team) is not None and qb.get((last_team, ps)) is not None and qbs.get(team) != qb.get((last_team, ps))),
                       days=((_aug(alld, Y) or pd.Timestamp(Y, 8, 28)) - date).days / 100,
                       v_m4=Vi[dm4].get(r.sid, np.nan) if dm4 is not None else np.nan,
                       v_m12=Vi[dm12].get(r.sid, np.nan) if dm12 is not None else np.nan, **_stat_agg(g), **_bio(bio, r.sid, ps))
            if not live:
                row["v_aug"] = Vi[ta].get(r.sid, np.nan) if ta is not None else np.nan
            out.append(row)
    return pd.DataFrame(out)


# ---------------------------------------------------------------- features
BASE_F = ["lv", "mom", "mom12", "res", "form", "dsnap", "snap", "tgt_sh", "car_sh", "perf_gap", "age_c", "age2", "young",
          "cl0", "cl_na", "rd1", "udfa", "gp_share", "miss3", "isQB", "isRB", "isTE", "rb_age", "qb_age", "exp"]
ROLE_F = ["air_sh", "wopr", "rz_sh"]
TEAM_F = ["tm_pass", "tm_qbepa"]
OFF_BASE = ["lv", "mom", "mom12", "snap", "tgt_sh", "car_sh", "perf_gap", "age_c", "age2", "young", "cl0", "cl_na", "rd1", "udfa",
            "gp_share", "isQB", "isRB", "isTE", "rb_age", "qb_age", "exp", "days"] + ROLE_F
EVENT_F = ["moved", "comp12", "comp3", "qb_new", "moved_rb"]
ALL_F = sorted(set(BASE_F + ROLE_F + TEAM_F + OFF_BASE + EVENT_F))


def featurize(X):
    X = X[X.v >= VMIN].copy()
    for c in ("ppg_l3", "ppg_early", "snap_l3", "snap_early", "miss3", "moved", "comp12", "comp3", "qb_new", "days", "coach_new"):
        if c not in X:
            X[c] = np.nan
    X["lv"] = np.log(X.v)
    X["mom"] = np.log(X.v / X.v_m4).clip(-.7, .7)
    X["mom12"] = np.log(X.v / X.v_m12).clip(-1, 1)
    X["res"] = X.ppg_l3.fillna(X.ppg) - X.ppg
    X["form"] = (X.ppg_l3 - X.ppg_early).fillna(0)
    X["dsnap"] = (X.snap_l3 - X.snap_early).fillna(0)

    def resid(g):
        if len(g) > 10:
            a, b = np.polyfit(g.lv, g.ppg.fillna(0), 1)
        else:
            a, b = 0, g.ppg.mean()
        return g.ppg - (a * g.lv + b)
    X["perf_gap"] = X.groupby(["date", "pos"], group_keys=False)[["lv", "ppg"]].apply(resid)
    X["age_c"] = (X.age - 26) / 4; X["age2"] = X.age_c ** 2
    X["young"] = (X.age < 24.5).astype(float)
    X["cl0"] = (X.cl == 0).astype(float); X["cl_na"] = X.cl.isna().astype(float)
    X["rd1"] = (X.draft_rd == 1).astype(float); X["udfa"] = (X.draft_rd >= 8).astype(float)
    for p in ("QB", "RB", "TE"):
        X["is" + p] = (X.pos == p).astype(float)
    X["rb_age"] = X.isRB * X.age_c; X["qb_age"] = X.isQB * X.age_c
    X["moved_rb"] = X.moved.fillna(0) * X.isRB
    X[ALL_F] = X[ALL_F].astype(float).fillna(0)
    X["rank"] = X.groupby("date").v.rank(ascending=False, method="first")
    X["band"] = ((X["rank"] - 1) // 20).clip(upper=9)
    X["tier"] = pd.cut(X["rank"], [0, 24, 60, 120, 10 ** 6], labels=TIERS).astype(str)
    for h in HORIZONS:  # target: change vs players at a similar value that day
        if "v_" + h in X:
            r = np.log(X["v_" + h] / X.v)
            X["y_" + h] = (r - r.groupby([X.date, X.band]).transform("median")).clip(-1.5, 1.5)
    X["rk"] = X.groupby(["date", "team", "pos"]).snap.rank(ascending=False, method="first")
    top = X[X.rk == 1][["date", "team", "pos", "age", "cl", "sid"]].rename(columns={"age": "s_age", "cl": "s_cl", "sid": "s_sid"})
    X = X.merge(top, on=["date", "team", "pos"], how="left")
    X["s_old"] = [bool(a >= OLD[p]) if not pd.isna(a) else False for a, p in zip(X.s_age, X.pos)]
    return X


SCREENS = {
    "rising": ("Rising role", "Under 25, snap share up 15+ points over the last 3 games, outside the top 25",
               lambda X: (X.age < 25) & (X.dsnap >= .15) & (X.v < 5500)),
    "open": ("Open role", "Under 26, second on his team's depth chart at his position, and the starter is old or in his contract's final year",
             lambda X: (X.rk == 2) & (X.age < 26) & ((X.s_cl == 0) | X.s_old) & (X.pos != "QB")),
    "outproducing": ("Outproducing his price", "Under 26, playing most games, scoring 4+ points a game more than players at his value",
                     lambda X: (X.age < 26) & (X.perf_gap > 4) & (X.gp_share > .7)),
    "contract_year": ("Contract year starter", "26 or older, starting, in the final year of his contract",
                      lambda X: (X.cl == 0) & (X.rk == 1) & (X.age >= 26)),
    "aging_rb": ("Aging RB", "Running back 27 or older, still valued as a top-100 player",
                 lambda X: (X.pos == "RB") & (X.age >= 27) & (X.v > 800)),
}
LIVE_SCREENS = {"starter_out": ("Next man up", "Second on his team's Sleeper depth chart and the starter is out (IR, out, PUP or suspended). New: no history yet, graded live")}


# ---------------------------------------------------------------- model machinery
def ridge_fit(X, y, alpha):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    b = np.linalg.solve(Z.T @ Z + alpha * np.eye(Z.shape[1]), Z.T @ (y - y.mean()))
    return dict(mu=mu.tolist(), sd=sd.tolist(), b=b.tolist(), c=float(y.mean()))


def ridge_pred(m, X):
    return ((X - np.array(m["mu"])) / np.array(m["sd"])) @ np.array(m["b"]) + m["c"]


def gbm_fit(X, y):
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(max_iter=250, learning_rate=0.04, max_depth=4, min_samples_leaf=80, l2_regularization=1.0, random_state=0).fit(X, y)


def spearman(a, b):
    ra, rb = pd.Series(a).rank().values, pd.Series(b).rank().values
    c = float(np.corrcoef(ra, rb)[0, 1])
    return 0.0 if math.isnan(c) else c


def score_preds(d, p, y):
    """Mean per-snapshot rank correlation, top-minus-bottom fifth spread, RMSE; per independent weekly snapshot."""
    m = pd.DataFrame({"date": d.date.values, "p": p, "y": d[y].values}).dropna()
    sp, spr = [], []
    for _, g in m.groupby("date"):
        if len(g) < 25:
            continue
        sp.append(spearman(g.p, g.y))
        q = pd.qcut(g.p.rank(method="first"), 5, labels=False)
        spr.append(g.y[q == 4].mean() - g.y[q == 0].mean())
    return dict(sp=round(float(np.mean(sp)), 3) if sp else 0.0, spread=round(float(np.mean(spr)) * 100, 1) if spr else 0.0,
                rmse=round(float(np.sqrt(((m.p - m.y) ** 2).mean())), 4), snaps=len(sp))


def walk(D, feats, y, alpha, seasons, gbm=False):
    """Out-of-sample predictions, each season predicted by a model trained on the seasons before it."""
    parts = []
    for ts in seasons[2:]:
        tr, te = D[D.season < ts], D[D.season == ts]
        if len(te) < 150 or len(tr) < 800:
            continue
        o = te[["date", "season", "pos", "age", "tier", y]].copy()
        o["ridge"] = ridge_pred(ridge_fit(tr[feats].values, tr[y].values, alpha), te[feats].values)
        if gbm:
            o["gbm"] = gbm_fit(tr[feats].values, tr[y].values).predict(te[feats].values)
        ac = tr.groupby([tr.pos, tr.age.round()])[y].mean()
        o["age_rule"] = [ac.get((p, a), 0) for p, a in zip(te.pos, te.age.round())]
        o["mom_rule"] = te["mom"].values
        parts.append(o)
    return pd.concat(parts) if parts else None


def train_horizon(D, y, sets, label):
    """Pick the feature set and alpha by out-of-sample RMSE, test the GBM ensemble, compare with simple rules, calibrate."""
    seasons = sorted(D.season.unique())
    D = D.dropna(subset=[y])
    trials = []
    for nm, feats in sets.items():
        for alpha in (50, 200, 800):
            O = walk(D, feats, y, alpha, seasons)
            if O is None:
                continue
            trials.append((score_preds(O, O.ridge.values, y)["rmse"], nm, alpha))
    if not trials:
        return None, None
    _, set_nm, alpha = min(trials)
    feats = sets[set_nm]
    O = walk(D, feats, y, alpha, seasons, gbm=True)
    O["ens"] = (O.ridge + O.gbm) / 2
    sc = {k: score_preds(O, O[c].values, y) for k, c in (("model", "ridge"), ("ensemble", "ens"), ("gbm", "gbm"), ("momentum", "mom_rule"), ("age", "age_rule"))}
    sc["zero"] = dict(rmse=round(float(np.sqrt((O[y] ** 2).mean())), 4), sp=0.0, spread=0.0)
    use_ens = sc["ensemble"]["rmse"] < sc["model"]["rmse"] and sc["ensemble"]["sp"] >= sc["model"]["sp"]
    O["p"] = O.ens if use_ens else O.ridge
    best = sc["ensemble"] if use_ens else sc["model"]
    per = []
    for ts, g in O.groupby("season"):
        r = {k: score_preds(g, g[c].values, y) for k, c in (("model", "p"), ("momentum", "mom_rule"), ("age", "age_rule"))}
        r["zero"] = dict(rmse=round(float(np.sqrt((g[y] ** 2).mean())), 4))
        r["season"] = int(ts)
        per.append(r)
    wins = sum(1 for r in per if r["model"]["rmse"] < min(r["zero"]["rmse"], r["age"]["rmse"]) and r["model"]["sp"] > max(r["momentum"]["sp"], r["age"]["sp"]))
    adopt = best["rmse"] < min(sc["zero"]["rmse"], sc["age"]["rmse"], sc["momentum"]["rmse"]) and best["sp"] > max(sc["momentum"]["sp"], sc["age"]["sp"])
    # calibration per value tier, then odds of +/-25% from out-of-sample misses (leave-one-season-out for the Brier check)
    cal = {t: round(float(min(1.2, max(0.05, (g.p * g[y]).sum() / max((g.p * g.p).sum(), 1e-9)))), 3) for t, g in O.groupby("tier")}
    O["pc"] = O.p * O.tier.map(cal).fillna(1)
    O["res"] = O[y] - O.pc
    qs = np.linspace(0, 1, 41)
    resq = {t: np.quantile(g.res, qs).round(4).tolist() for t, g in O.groupby("tier")}
    bri, bri0 = [], []
    for ts, g in O.groupby("season"):
        oth = O[O.season != ts]
        for t, gg in g.groupby("tier"):
            o_t = oth[oth.tier == t]
            if not len(o_t):
                continue
            rq = np.quantile(o_t.res, qs)
            pu = np.array([(p + rq > UP).mean() for p in gg.pc])
            base = (o_t[y] > UP).mean()
            hit = (gg[y] > UP).values
            bri += list((pu - hit) ** 2); bri0 += list((base - hit) ** 2)
    O["q"] = pd.qcut(O.pc.rank(method="first"), 5, labels=False)
    calib = [[round(float(np.expm1(g.pc.mean())) * 100, 1), round(float(np.expm1(g[y].mean())) * 100, 1)] for _, g in O.groupby("q")]
    by_pos = {p: score_preds(g, g.p.values, y)["sp"] for p, g in O.groupby("pos")}
    m = ridge_fit(D[feats].values, D[y].values, alpha)
    m.update(alpha=alpha, feats=feats, set=set_nm, ens=bool(use_ens), adopted=bool(adopt), cal=cal, resq=resq)
    if not adopt:
        m["age_curve"] = {f"{p}|{int(a)}": round(float(v), 4) for (p, a), v in D.groupby([D.pos, D.age.round()])[y].mean().items()}
    gbm = gbm_fit(D[feats].values, D[y].values) if use_ens else None
    avg = {k: dict(sp=v["sp"], spread=v["spread"], rmse=v["rmse"]) for k, v in sc.items()}
    avg["model"] = dict(sp=best["sp"], spread=best["spread"], rmse=best["rmse"])
    rep = dict(per_season=per, avg=avg, seasons_won=wins, seasons=len(per), adopted=bool(adopt), alpha=alpha, features=set_nm, ensemble=bool(use_ens),
               ridge_only=dict(sp=sc["model"]["sp"], rmse=sc["model"]["rmse"]), calib=calib, cal=cal, by_pos=by_pos,
               brier=round(float(np.mean(bri)), 4) if bri else None, brier_base=round(float(np.mean(bri0)), 4) if bri0 else None,
               feature_trials=sorted([[round(r, 4), n, a] for r, n, a in trials])[:6],
               top_features=[[f, round(b, 3)] for f, b in sorted(zip(feats, m["b"]), key=lambda t: -abs(t[1]))[:8]])
    log(label, y, "set", set_nm, "alpha", alpha, "ens", use_ens, "adopt", adopt, "won", wins, "/", len(per), "sp", best["sp"],
        "ridge sp", sc["model"]["sp"], "brier", rep["brier"], "base", rep["brier_base"])
    return (m, gbm), rep


def predict(model, X):
    m, gbm = model
    if m.get("adopted"):
        p = ridge_pred(m, X[m["feats"]].values)
        if m.get("ens") and gbm is not None:
            p = (p + gbm.predict(X[m["feats"]].values)) / 2
    else:
        p = np.array([m.get("age_curve", {}).get(f"{a}|{int(round(b))}", 0) for a, b in zip(X.pos, X.age)])
    p = p * np.array([m.get("cal", {}).get(t, 1.0) for t in X.tier])
    rq = m.get("resq", {})
    pu = np.array([float((pp + np.array(rq[t]) > UP).mean()) if t in rq else np.nan for pp, t in zip(p, X.tier)])
    pdn = np.array([float((pp + np.array(rq[t]) < DOWN).mean()) if t in rq else np.nan for pp, t in zip(p, X.tier)])
    return p, pu, pdn


def screen_stats(X, fn, h):
    msk = fn(X)
    g = X.loc[msk, "y_" + h].dropna(); a = X["y_" + h].dropna()
    if len(g) < 30:
        return None
    per = X[msk].groupby("season")["y_" + h].mean().dropna()
    base = X.groupby("season")["y_" + h].mean()
    return dict(avg=round(float(np.expm1(g.mean() - a.mean())) * 100, 1), hit=round(float((g > UP).mean()) * 100),
                base_hit=round(float((a > UP).mean()) * 100), n=int(len(g)),
                seasons_better=int(sum(1 for s_, v_ in per.items() if v_ > base.get(s_, 0))), seasons=int(len(per)))


# ---------------------------------------------------------------- calendar and offseason event effects
def agegrp(pos, age):
    cuts = {"QB": (26, 32), "RB": (24, 27), "WR": (24, 28), "TE": (25, 30)}[pos]
    if pd.isna(age):
        return None
    return f"{pos}|{'young' if age < cuts[0] else 'prime' if age < cuts[1] else 'older'}"


def seasonality(V, VI):
    """Typical 4-week value change vs similar-value players, by position, age group and calendar month."""
    Vi, alld = VI
    rows = []
    for d in alld:
        d2 = _near(alld, d, 28)
        if d2 is None or d2 == d:
            continue
        a = V[(V.date == d) & (V.v >= VMIN)][["sid", "pos", "v", "age_dp"]].copy()
        a["v2"] = a.sid.map(Vi[d2]); a = a.dropna(subset=["v2"])
        if len(a) < 50:
            continue
        a["r"] = np.log(a.v2 / a.v)
        a["band"] = ((a.v.rank(ascending=False, method="first") - 1) // 20).clip(upper=9)
        a["r"] = (a.r - a.groupby("band").r.transform("median")).clip(-1.5, 1.5)
        a["grp"] = [agegrp(p, x) for p, x in zip(a.pos, a.age_dp)]
        a["month"] = d.month; a["yr"] = d.year
        rows.append(a.dropna(subset=["grp"]))
    A = pd.concat(rows)
    out = {}
    for g, gg in A.groupby("grp"):
        m = gg.groupby("month").r.mean()
        cons = gg.groupby(["month", "yr"]).r.mean().groupby(level=0).apply(lambda s: [int((s < 0).sum()), int(len(s))])
        out[g] = dict(m=[round(float(np.expm1(m.get(i, 0))) * 100, 1) for i in range(1, 13)],   # % per 4 weeks starting in that month
                      down_years=[cons.get(i, [0, 0]) for i in range(1, 13)])
    return out


def sell_window(curve, month_now):
    """Cumulative typical drift over the next 12 months; best month to sell = peak of the path (0 = now)."""
    path, cum = [], 0.0
    for k in range(12):
        path.append(cum)
        cum += math.log1p(curve[(month_now - 1 + k) % 12] / 100)
    k = int(np.argmax(path))
    to_aug = sum(math.log1p(curve[(month_now - 1 + j) % 12] / 100) for j in range(((8 - month_now) % 12) or 12))
    return k, round(float(np.expm1(path[k])) * 100, 1), round(float(np.expm1(to_aug)) * 100, 1)


def event_effects(Xo):
    """Offseason events: value change from early February to the end of August vs similar-value players."""
    if Xo is None or not len(Xo) or "y_aug" not in Xo:
        return {}
    last = Xo.sort_values("date").groupby(["sid", "season"]).tail(1)   # events as known on Aug 1
    first = Xo[(Xo.date.dt.month == 2)].sort_values("date").groupby(["sid", "season"]).head(1)[["sid", "season", "v", "band"]]
    M = last.merge(first, on=["sid", "season"], suffixes=("", "_feb"))
    M["r"] = np.log(M.v_aug / M.v_feb)
    M = M.dropna(subset=["r"])
    M["r"] = (M.r - M.groupby(["season", "band_feb"]).r.transform("median")).clip(-1.5, 1.5)
    out = {}
    for k, nm, msk in (("moved", "Changed teams (free agency or trade)", M.moved == 1),
                       ("moved_rb", "RB who changed teams", (M.moved == 1) & (M.pos == "RB")),
                       ("comp12", "Team drafted a player at his position in round 1-2", M.comp12 == 1),
                       ("comp3", "Team drafted a player at his position in round 3", M.comp3 == 1),
                       ("qb_new", "New starting QB on his team (non-QBs)", M.qb_new == 1),
                       ("coach_new", "New head coach", M.coach_new == 1),
                       ("fa", "Contract ended (free agent that spring)", M.cl0 == 1)):
        g = M[msk]
        if len(g) < 15:
            continue
        per = g.groupby("season").r.mean()
        out[k] = dict(name=nm, n=int(len(g)), avg=round(float(np.expm1(g.r.mean())) * 100, 1),
                      worse=int((per < 0).sum()), seasons=int(len(per)), lost25=round(float((g.r < DOWN).mean()) * 100))
    return out


# ---------------------------------------------------------------- live helpers
def latest_archive(kind):
    fs = sorted(glob.glob(os.path.join(DOCS, "archive", kind, "*.json")))
    for f in reversed(fs):
        try:
            A = json.load(open(f))
            if A:
                k = max(A)
                return k, A[k]
        except Exception:  # noqa
            pass
    return None, {}


def starter_out(dep):
    """From Sleeper's depth charts: second-stringers whose starter is out."""
    by = {}
    for sid, row in dep.items():
        team, dpos, order, inj = row[0], row[1], row[2], row[3]
        if order is not None and dpos:
            by.setdefault((team, dpos), []).append((order, sid, inj))
    flag = {}
    for xs in by.values():
        xs.sort(key=lambda x: x[0])
        if len(xs) >= 2 and xs[0][0] == 1 and (xs[0][2] or "") in ("IR", "Out", "PUP", "Sus", "NFI", "DNR"):
            flag[xs[1][1]] = xs[0][1]
    return flag


# ---------------------------------------------------------------- main
def main():
    force = "--train" in sys.argv
    ids = pd.read_csv(fetch(f"{DPRAW}/db_playerids.csv", "ids.csv", 24 * 7), dtype=str)
    cur_path = dp_history()
    V = load_values(ids, cur_path)
    VI = _vindex(V)
    cur_season = season_of(TODAY)
    S = load_usage(ids, cur_season)
    bio, contracts = load_bio_contracts(ids)
    EV = load_events(ids)
    log("values", V.shape, V.date.min().date(), V.date.max().date(), "stats", S.shape, "contracts", len(contracts))

    mpath = os.path.join(DOCS, "futures_model.json")
    gpath = os.path.join(CACHE, "gbm.pkl")
    M = json.load(open(mpath)) if os.path.exists(mpath) else {}
    try:
        G = pickle.load(open(gpath, "rb")) if os.path.exists(gpath) else None
    except Exception:  # noqa
        G = None
    need_gbm = any(m.get("ens") for m in (M.get("models") or {}).values())
    stale = not M or M.get("version") != 3 or (TODAY - pd.Timestamp(M.get("trained", "2000-01-01"))).days >= 7 or (need_gbm and G is None)
    if force or stale:
        t0 = time.time()
        hist = sorted(d for d in V.date.unique() if d < TODAY - pd.Timedelta(days=1))
        Xh = featurize(build_rows(V, S, bio, contracts, hist, VI))
        Xo = featurize(build_off_rows(V, S, bio, contracts, [d for d in hist if offseason(d)], VI, EV))
        models, gbms, rep = {}, {}, {}
        sets = {"base": BASE_F, "base+role": BASE_F + ROLE_F, "base+role+team": BASE_F + ROLE_F + TEAM_F}
        for h in HORIZONS:
            mm, r = train_horizon(Xh, "y_" + h, sets, "in-season")
            if mm:
                models[h], gbms[h], rep[h] = mm[0], mm[1], r
        osets = {"last season": OFF_BASE, "last season+events": OFF_BASE + EVENT_F}
        mm, r = train_horizon(Xo, "y_aug", osets, "offseason")
        if mm:
            models["off"], gbms["off"], rep["off"] = mm[0], mm[1], r
        scr = {}
        for k, (nm, desc, fn) in SCREENS.items():
            scr[k] = dict(name=nm, rule=desc, n=int(fn(Xh).sum()), p8=screen_stats(Xh, fn, "p8"), aug=screen_stats(Xh, fn, "aug"))
        M = dict(version=3, trained=TODAY.strftime("%Y-%m-%d"), rows=int(len(Xh)), off_rows=int(len(Xo)),
                 seasons=[int(Xh.season.min()), int(Xh.season.max())], models=models, report=rep, screens=scr,
                 events=event_effects(Xo), calendar=seasonality(V, VI),
                 src="DynastyProcess weekly superflex values; nflverse stats, snaps, play-by-play, draft picks, schedules; OverTheCap contracts")
        json.dump(M, open(mpath, "w"), separators=(",", ":"))
        pickle.dump(gbms, open(gpath, "wb"))
        G = gbms
        log(f"trained in {time.time() - t0:.0f}s: {len(Xh)} in-season rows, {len(Xo)} offseason rows")

    # ---- today's calls
    now = V.date.max()
    off_ps = offseason(now)
    mode = "offseason" if off_ps else "in-season"
    players = {}
    dep_day, dep = latest_archive("sleeper_depth")
    so = starter_out(dep) if dep else {}
    Xn = (build_off_rows(V, S, bio, contracts, [now], VI, EV, live=True) if off_ps else build_rows(V, S, bio, contracts, [now], VI, live=True))
    if len(Xn):
        Xn = featurize(Xn)
        P = {}
        if off_ps and "off" in M["models"]:
            P["aug"] = predict((M["models"]["off"], (G or {}).get("off")), Xn)
        elif not off_ps:
            for h in HORIZONS:
                if h in M["models"]:
                    P[h] = predict((M["models"][h], (G or {}).get(h)), Xn)
        for k, (nm, desc, fn) in SCREENS.items():
            Xn["s_" + k] = fn(Xn).values if (not off_ps or k in ("contract_year", "aging_rb")) else False
        cal_ = M.get("calendar", {})
        for i, r in enumerate(Xn.itertuples()):
            why = []
            if not off_ps and r.dsnap >= .1: why.append(f"snap share up {r.dsnap * 100:.0f} pts lately")
            if r.perf_gap > 3: why.append(f"scoring {r.perf_gap:.1f} pts/game more than players at his price")
            if r.perf_gap < -3: why.append(f"scoring {-r.perf_gap:.1f} pts/game less than players at his price")
            if r.rz_sh >= .25 and r.pos != "QB": why.append(f"{r.rz_sh * 100:.0f}% of his team's red-zone chances")
            if r.mom > .2: why.append(f"value up {r.mom * 100:.0f}% in 4 weeks")
            if r.mom < -.2: why.append(f"value down {-r.mom * 100:.0f}% in 4 weeks")
            if r.cl == 0: why.append("final contract year" if not off_ps else "contract ended: free agent")
            if off_ps and r.moved: why.append("changed teams")
            if off_ps and r.comp12: why.append("team drafted a player at his position early")
            if off_ps and r.qb_new: why.append("new starting QB")
            if r.rk == 2 and r.s_old: why.append("starter ahead of him is aging")
            if r.rk == 2 and r.s_cl == 0: why.append("starter ahead of him is in a contract year")
            scr = [k for k in SCREENS if getattr(r, "s_" + k)]
            if r.sid in so:
                scr.append("starter_out"); why.insert(0, "starter ahead of him is out")
            o = dict(n=r.name, v=int(r.v), c_end=int(r.c_end) if not pd.isna(r.c_end) else None,
                     age=round(float(r.age), 1) if not pd.isna(r.age) else None, pos=r.pos, team=r.team,
                     snap=round(float(r.snap) * 100) if not pd.isna(r.snap) else None, tgt=round(float(r.tgt_sh) * 100), car=round(float(r.car_sh) * 100),
                     rz=round(float(r.rz_sh) * 100), air=round(float(r.air_sh) * 100), ppg=round(float(r.ppg), 1) if not pd.isna(r.ppg) else None,
                     gap=round(float(r.perf_gap), 1), depth=int(r.rk) if not pd.isna(r.rk) else None, tier=r.tier, screens=scr, why=why[:3])
            for h, (p, pu, pdn) in P.items():
                o[h] = round(float(np.expm1(p[i])) * 100, 1)
                if h == "aug":
                    o["pu"] = round(float(pu[i]) * 100) if not np.isnan(pu[i]) else None
                    o["pd"] = round(float(pdn[i]) * 100) if not np.isnan(pdn[i]) else None
            grp = agegrp(r.pos, r.age)
            if grp in cal_:
                k_, peak, to_aug = sell_window(cal_[grp]["m"], now.month)
                o["window"] = [MONTHS[(now.month - 1 + k_) % 12] if k_ else "now", peak, to_aug, grp]
            players[r.sid] = o
    con = {}
    for sid in contracts:
        c = contract_at(contracts, sid, cur_season)
        if c:
            con[sid] = [c["end"], round(c["cap"], 1), round(c["apy"], 1)]
    # ---- log once per ISO week; grade old weeks against today's values, vs players logged at a similar value
    lpath = os.path.join(DOCS, "futures_log.json")
    L = json.load(open(lpath)) if os.path.exists(lpath) else {"weeks": {}, "graded": {}}
    wk = "%d-W%02d" % TODAY.isocalendar()[:2]
    if players and wk not in L["weeks"]:
        L["weeks"][wk] = dict(date=now.strftime("%Y-%m-%d"), mode=mode,
                              p={s: [p["v"], p.get("p4"), p.get("p8"), p.get("aug"), p["screens"], p["pos"]] for s, p in players.items()})
    vnow = V[V.date == now].set_index("sid")["v"].to_dict()
    for k_, e in L["weeks"].items():
        age_d = (now - pd.Timestamp(e["date"])).days
        for h, lo, hi, idx in (("p4", 28, 34, 1), ("p8", 56, 62, 2)):
            key = f"{k_}|{h}"
            if not (lo <= age_d <= hi) or key in L["graded"]:
                continue
            rows_ = [(s, row) for s, row in e["p"].items() if s in vnow and row[0] > 0]
            if len(rows_) < 50:
                continue
            Gd = pd.DataFrame([(s, row[0], row[idx], math.log(vnow[s] / row[0]), row[4], row[5] if len(row) > 5 else None) for s, row in rows_],
                              columns=["s", "v", "p", "r", "scr", "pos"])
            Gd["band"] = ((Gd.v.rank(ascending=False, method="first") - 1) // 20).clip(upper=9)
            Gd["act"] = Gd.r - Gd.groupby("band").r.transform("median")
            scr_ = {}
            for sk in list(SCREENS) + list(LIVE_SCREENS):
                ss = Gd.act[[sk in x for x in Gd.scr]].values
                if len(ss):
                    scr_[sk] = [len(ss), round(float(np.expm1(np.mean(ss))) * 100, 1), round(float(np.mean(ss > UP)) * 100)]
            gr = dict(n=len(Gd), screens=scr_)
            Gp = Gd.dropna(subset=["p"])
            if len(Gp) >= 50:
                q = pd.qcut(Gp.p.rank(method="first"), 5, labels=False).values
                gr.update(sp=round(spearman(Gp.p, Gp.act), 3), top=round(float(np.expm1(Gp.act[q == 4].mean())) * 100, 1),
                          bottom=round(float(np.expm1(Gp.act[q == 0].mean())) * 100, 1),
                          by_pos={p: round(spearman(g.p, g.act), 3) for p, g in Gp.groupby("pos") if p and len(g) >= 15})
            L["graded"][key] = gr
    for k_ in sorted(L["weeks"])[:-60]:
        L["weeks"].pop(k_)
    json.dump(L, open(lpath, "w"), separators=(",", ":"))
    live = {}
    for h in ("p4", "p8"):
        g = [v for k, v in L["graded"].items() if k.endswith("|" + h) and "sp" in v]
        if g:
            live[h] = dict(snaps=len(g), sp=round(float(np.mean([x["sp"] for x in g])), 3), top=round(float(np.mean([x["top"] for x in g])), 1),
                           bottom=round(float(np.mean([x["bottom"] for x in g])), 1))
            bt = ((M.get("report") or {}).get(h) or {}).get("avg", {}).get("model", {}).get("sp")
            # drift alarm: four or more graded weeks running well below the backtest means the market has changed
            live[h]["drift"] = bool(bt is not None and len(g) >= 4 and live[h]["sp"] < bt - 0.15)
    live_scr = {}
    for v in L["graded"].values():
        for sk, (n_, avg_, hit_) in (v.get("screens") or {}).items():
            a = live_scr.setdefault(sk, [0, 0.0, 0.0]); a[0] += n_; a[1] += avg_ * n_; a[2] += hit_ * n_
    live_scr = {k: [a[0], round(a[1] / a[0], 1), round(a[2] / a[0])] for k, a in live_scr.items() if a[0]}
    out = dict(updated=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), values_date=now.strftime("%Y-%m-%d"), mode=mode,
               season=cur_season, week=int(Xn.week.max()) if len(Xn) and "week" in Xn else None, depth_date=dep_day,
               model=dict(trained=M.get("trained"), rows=M.get("rows"), off_rows=M.get("off_rows"), seasons=M.get("seasons"), report=M.get("report"),
                          screens=M.get("screens"), live_screens={k: dict(name=a, rule=b) for k, (a, b) in LIVE_SCREENS.items()},
                          events=M.get("events"), calendar=M.get("calendar"), src=M.get("src")),
               live=live, live_screens=live_scr, logged_weeks=len(L["weeks"]), players=players, contracts=con,
               attribution="Values: DynastyProcess (CC BY). Stats, play-by-play, draft, schedules, snaps and contracts: nflverse / OverTheCap.")
    json.dump(out, open(os.path.join(DOCS, "futures.json"), "w"), separators=(",", ":"))
    log("mode", mode, "players", len(players), "contracts", len(con), "starter-out flags", len(so), "live", live)


if __name__ == "__main__":
    main()
