"""Futures: where dynasty values are headed, sleeper screens and contract watch, with an honest track record.

Data (all open): DynastyProcess weekly dynasty values (superflex column) back to 2019, nflverse weekly stats,
snap counts, players and OverTheCap contracts.

Model: ridge regression on usage, production, age, contract and price features, predicting each player's value
change *relative to the whole market* 4 weeks, 8 weeks and next August ahead. Retrained weekly, tested
walk-forward by season (train on earlier seasons, test on the next) against three baselines: no change,
momentum and an age curve. A horizon's model is used only if it beats all three out of sample.

Screens are simple rules (rising role, open role, outproducing price, contract year, aging RB) with their
historical hit rates. Every Friday the current calls are logged and later graded against what values did.

Needs pandas, numpy and pyarrow (the futures workflow installs them). Writes docs/futures.json,
docs/futures_model.json and docs/futures_log.json.
"""
import os, re, sys, json, time, math, datetime, subprocess, urllib.request
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
TODAY = pd.Timestamp(datetime.date.today())
log = lambda *a: print("[futures]", *a, flush=True)


def fetch(url, name, max_age_h):
    p = os.path.join(CACHE, name)
    if os.path.exists(p) and (max_age_h is None or time.time() - os.path.getmtime(p) < max_age_h * 3600):
        return p
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dynasty-hq/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r, open(p + ".tmp", "wb") as f:
                f.write(r.read())
            os.replace(p + ".tmp", p)
            return p
        except Exception as e:  # noqa
            log("retry", url, e); time.sleep(3 * (i + 1))
    if os.path.exists(p):
        return p
    raise RuntimeError(f"could not download {url}")


def season_of(d):
    """NFL season a date belongs to (Feb-Aug count as the previous season's offseason)."""
    return d.year if d.month >= 3 else d.year - 1


def week1(y):
    lab = datetime.date(y, 9, 1)
    lab += datetime.timedelta(days=(0 - lab.weekday()) % 7)  # Labor Day (first Monday of September)
    return pd.Timestamp(lab + datetime.timedelta(days=3))


def week_done(d):
    """(season, last regular-season week finished) as of date d; values are taken on Fridays."""
    y = season_of(d)
    w1 = week1(y)
    if d < w1 + pd.Timedelta(days=5):
        return y, 0
    days = (d - w1).days
    return y, min(days // 7 + (1 if days % 7 >= 5 else 0), 18 if y >= 2021 else 17)


# ---------------------------------------------------------------- dynasty value history
def dp_history():
    g = os.path.join(CACHE, "dpgit")
    if not os.path.isdir(os.path.join(g, ".git")):
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", "https://github.com/dynastyprocess/data.git", g], check=True)
    else:
        subprocess.run(["git", "-C", g, "fetch", "-q", "origin"], check=False)
    out = subprocess.run(["git", "-C", g, "log", "origin/HEAD" if _has(g, "origin/HEAD") else "HEAD", f"--since={FIRST}-08-01",
                          "--format=%h %ad", "--date=short", "--", "files/values.csv"], capture_output=True, text=True).stdout.split("\n")
    seen = {}
    for line in out:
        if not line.strip():
            continue
        h, d = line.split()
        k = datetime.date.fromisoformat(d).isocalendar()[:2]
        seen.setdefault(k, (h, d))  # newest commit of each ISO week
    for h, d in seen.values():
        p = os.path.join(CACHE, "dp", f"{d}.csv")
        if not os.path.exists(p) or os.path.getsize(p) < 5000:
            with open(p, "w") as f:
                subprocess.run(["git", "-C", g, "show", f"{h}:files/values.csv"], stdout=f, stderr=subprocess.DEVNULL)
    # today's live file too
    cur = fetch(f"{DPRAW}/values.csv", "values_now.csv", 6)
    return cur


def _has(g, ref):
    return subprocess.run(["git", "-C", g, "rev-parse", "--verify", "-q", ref], capture_output=True).returncode == 0


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
        rows.append(t.dropna(subset=["sid", "v"])[["sid", "date", "pos", "name", "team", "v", "age_dp"]])
    V = pd.concat(rows).drop_duplicates(["sid", "date"], keep="last")
    return V


# ---------------------------------------------------------------- usage, bio, contracts
def load_usage(ids, last_season):
    g2sl = ids.dropna(subset=["gsis_id", "sleeper_id"]).set_index("gsis_id")["sleeper_id"].to_dict()
    pfr2sl = ids.dropna(subset=["pfr_id", "sleeper_id"]).set_index("pfr_id")["sleeper_id"].to_dict()
    st, sn = [], []
    for y in range(FIRST, last_season + 1):
        age = 12 if y >= last_season else None
        try:
            s = pd.read_csv(fetch(f"{NV}/stats_player/stats_player_week_{y}.csv", f"stats_{y}.csv", age), low_memory=False)
        except Exception as e:  # noqa
            log("no stats", y, e); continue
        s = s[(s["season_type"] == "REG") & s["position"].isin(["QB", "RB", "WR", "TE"])].copy()
        s["sid"] = s["player_id"].map(g2sl)
        tm = s.groupby(["team", "week"])[["targets", "carries"]].sum().rename(columns={"targets": "tm_tgt", "carries": "tm_car"})
        s = s.join(tm, on=["team", "week"])
        st.append(s[["sid", "season", "week", "team", "fantasy_points_ppr", "targets", "carries", "tm_tgt", "tm_car"]])
        try:
            n = pd.read_csv(fetch(f"{NV}/snap_counts/snap_counts_{y}.csv", f"snaps_{y}.csv", age), low_memory=False)
            n = n[(n["game_type"] == "REG") & n["position"].isin(["QB", "RB", "WR", "TE"])].copy()
            n["sid"] = n["pfr_player_id"].map(pfr2sl)
            sn.append(n[["sid", "season", "week", "offense_pct"]])
        except Exception as e:  # noqa
            log("no snaps", y, e)
    S = pd.concat(st).dropna(subset=["sid"])
    N = pd.concat(sn).dropna(subset=["sid"]).drop_duplicates(["sid", "season", "week"])
    return S.merge(N, on=["sid", "season", "week"], how="left")


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


# ---------------------------------------------------------------- rows and features
def build_rows(V, S, bio, contracts, dates, live=False):
    Vp = V.pivot_table(index="date", columns="sid", values="v")
    alld = list(Vp.index)
    Vi = {d: Vp.loc[d] for d in alld}

    def near(d, days):
        t = d + pd.Timedelta(days=days)
        c = [x for x in alld if abs((x - t).days) <= 4]
        return min(c, key=lambda x: abs((x - t).days)) if c else None

    def aug_next(y):
        c = [x for x in alld if x.year == y + 1 and x.month == 8]
        return c[-1] if c else None

    Sg = {k: g for k, g in S.groupby("season")}
    out = []
    for date in dates:
        y, w = week_done(date)
        s = Sg.get(y)
        if live and s is not None:
            w = int(s["week"].max())
        if s is None or w < 3:
            continue
        if not live and w > 17:
            continue
        s = s[s["week"] <= w]
        agg = {k: g.sort_values("week") for k, g in s.groupby("sid")}
        snap = V[V["date"] == date]
        dm4, dm12 = near(date, -28), near(date, -84)
        tgt = {} if live else {h: (near(date, dd) if dd else aug_next(y)) for h, dd in HORIZONS.items()}
        for r in snap.itertuples():
            g = agg.get(r.sid)
            if g is None or r.v < VMIN:
                continue
            l3 = g[g["week"] > w - 3]; early = g[g["week"] <= w - 3]
            b = bio.loc[r.sid] if r.sid in bio.index else None
            c = contract_at(contracts, r.sid, y)
            row = dict(sid=r.sid, name=r.name, date=date, season=y, week=w, pos=r.pos, v=r.v, age=r.age_dp, team=g["team"].values[-1],
                       exp=(y - float(b["rookie_season"])) if b is not None and str(b["rookie_season"]) not in ("nan", "") else np.nan,
                       draft_rd=float(b["draft_round"]) if b is not None and str(b["draft_round"]) not in ("nan", "") else 8.0,
                       gp_share=len(g) / w, ppg=g["fantasy_points_ppr"].mean(),
                       ppg_l3=l3["fantasy_points_ppr"].mean() if len(l3) else np.nan,
                       ppg_early=early["fantasy_points_ppr"].mean() if len(early) else np.nan, miss3=3 - len(l3),
                       snap=g["offense_pct"].mean(), snap_l3=l3["offense_pct"].mean() if len(l3) else np.nan,
                       snap_early=early["offense_pct"].mean() if len(early) else np.nan,
                       tgt_sh=g["targets"].sum() / max(g["tm_tgt"].sum(), 1), car_sh=g["carries"].sum() / max(g["tm_car"].sum(), 1),
                       cl=(c["end"] - y) if c else np.nan, c_end=c["end"] if c else np.nan,
                       v_m4=Vi[dm4].get(r.sid, np.nan) if dm4 is not None else np.nan,
                       v_m12=Vi[dm12].get(r.sid, np.nan) if dm12 is not None else np.nan)
            for h, d_ in tgt.items():
                row["v_" + h] = Vi[d_].get(r.sid, np.nan) if d_ is not None else np.nan
            out.append(row)
    return pd.DataFrame(out)


F = ["lv", "mom", "mom12", "res", "form", "dsnap", "snap", "tgt_sh", "car_sh", "perf_gap", "age_c", "age2", "young",
     "cl0", "cl_na", "rd1", "udfa", "gp_share", "miss3", "isQB", "isRB", "isTE", "rb_age", "qb_age", "exp"]


def featurize(X):
    X = X[X.v >= VMIN].copy()
    X["lv"] = np.log(X.v)
    X["mom"] = np.log(X.v / X.v_m4).clip(-.7, .7)
    X["mom12"] = np.log(X.v / X.v_m12).clip(-1, 1)
    X["res"] = X.ppg_l3.fillna(X.ppg) - X.ppg
    X["form"] = (X.ppg_l3 - X.ppg_early).fillna(0)
    X["dsnap"] = (X.snap_l3 - X.snap_early).fillna(0)

    def resid(g):
        if len(g) > 10:
            a, b = np.polyfit(g.lv, g.ppg, 1)
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
    X[F] = X[F].astype(float).fillna(0)
    # target: value change compared with players at a similar value that day (rank bands of 20). Against the whole
    # market, stars "gain" just because deep bench values collapse; against their own band, a call is a real buy or sell.
    X["rank"] = X.groupby("date").v.rank(ascending=False, method="first")
    X["band"] = ((X["rank"] - 1) // 20).clip(upper=9)
    X["tier"] = pd.cut(X["rank"], [0, 24, 60, 120, 10 ** 6], labels=TIERS).astype(str)
    for h in HORIZONS:
        if "v_" + h in X:
            r = np.log(X["v_" + h] / X.v)
            X["y_" + h] = (r - r.groupby([X.date, X.band]).transform("median")).clip(-1.5, 1.5)
    # depth chart proxy: snap rank within team and position; the starter's age and contract
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


# ---------------------------------------------------------------- model
def ridge_fit(X, y, alpha):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    A = Z.T @ Z + alpha * np.eye(Z.shape[1])
    b = np.linalg.solve(A, Z.T @ (y - y.mean()))
    return dict(mu=mu.tolist(), sd=sd.tolist(), b=b.tolist(), c=float(y.mean()))


def ridge_pred(m, X):
    return ((X - np.array(m["mu"])) / np.array(m["sd"])) @ np.array(m["b"]) + m["c"]


def spearman(a, b):
    ra, rb = pd.Series(a).rank().values, pd.Series(b).rank().values
    c = float(np.corrcoef(ra, rb)[0, 1])
    return 0.0 if math.isnan(c) else c


def score_preds(d, p, y):
    """Mean per-snapshot rank correlation, top-minus-bottom fifth spread, RMSE; n = independent weekly snapshots."""
    m = pd.DataFrame({"date": d.date.values, "p": p, "y": d[y].values}).dropna()
    sp, spr = [], []
    for _, g in m.groupby("date"):
        if len(g) < 25:
            continue
        sp.append(spearman(g.p, g.y))
        q = pd.qcut(g.p.rank(method="first"), 5, labels=False)
        spr.append(g.y[q == 4].mean() - g.y[q == 0].mean())
    return dict(sp=round(float(np.mean(sp)), 3) if sp else None, spread=round(float(np.mean(spr)) * 100, 1) if spr else None,
                rmse=round(float(np.sqrt(((m.p - m.y) ** 2).mean())), 4), snaps=len(sp))


def train(X):
    rep = {}
    models = {}
    seasons = sorted(X.season.unique())
    for h in HORIZONS:
        y = "y_" + h
        D = X.dropna(subset=[y])
        res = {k: [] for k in ("zero", "momentum", "age", "model")}
        best_alpha, best = 50, None
        per = []
        for alpha in (10, 50, 200):
            tot = []
            for ts in seasons[2:]:
                tr, te = D[D.season < ts], D[D.season == ts]
                if len(te) < 200 or len(tr) < 1000:
                    continue
                m = ridge_fit(tr[F].values, tr[y].values, alpha)
                tot.append(score_preds(te, ridge_pred(m, te[F].values), y)["rmse"])
            if tot and (best is None or np.mean(tot) < best):
                best, best_alpha = np.mean(tot), alpha
        oos = []
        for ts in seasons[2:]:
            tr, te = D[D.season < ts], D[D.season == ts]
            if len(te) < 200 or len(tr) < 1000:
                continue
            oos.append(pd.DataFrame({"tier": te.tier.values, "p": ridge_pred(ridge_fit(tr[F].values, tr[y].values, best_alpha), te[F].values), "y": te[y].values}))
            ac = tr.groupby([tr.pos, tr.age.round()])[y].mean()
            m = ridge_fit(tr[F].values, tr[y].values, best_alpha)
            row = dict(season=int(ts),
                       zero=dict(rmse=round(float(np.sqrt((te[y] ** 2).mean())), 4), sp=0.0, spread=0.0),
                       momentum=score_preds(te, te.mom.values, y),
                       age=score_preds(te, np.array([ac.get((p, a), 0) for p, a in zip(te.pos, te.age.round())]), y),
                       model=score_preds(te, ridge_pred(m, te[F].values), y))
            per.append(row)
        if not per:
            continue
        avg = {k: dict(sp=round(float(np.mean([r[k]["sp"] or 0 for r in per])), 3),
                       spread=round(float(np.mean([r[k]["spread"] or 0 for r in per])), 1),
                       rmse=round(float(np.mean([r[k]["rmse"] for r in per])), 4)) for k in ("zero", "momentum", "age", "model")}
        wins = sum(1 for r in per if r["model"]["rmse"] < min(r["zero"]["rmse"], r["age"]["rmse"]) and (r["model"]["sp"] or 0) > max(r["momentum"]["sp"] or 0, r["age"]["sp"] or 0))
        adopt = avg["model"]["rmse"] < min(avg["zero"]["rmse"], avg["age"]["rmse"], avg["momentum"]["rmse"]) and avg["model"]["sp"] > max(avg["momentum"]["sp"], avg["age"]["sp"])
        m = ridge_fit(D[F].values, D[y].values, best_alpha)
        m["alpha"] = best_alpha
        # calibration: out of sample, raw calls are too bold among the most valuable players (their values barely move),
        # so each value tier gets its own shrink factor = slope of what happened on what was predicted
        O = pd.concat(oos)
        m["cal"] = {t: round(float(min(1.2, max(0.05, (g.p * g.y).sum() / max((g.p * g.p).sum(), 1e-9)))), 3) for t, g in O.groupby("tier")}
        O["pc"] = O.p * O.tier.map(m["cal"]).fillna(1)
        O["q"] = pd.qcut(O.pc.rank(method="first"), 5, labels=False)
        calib = [[round(float(np.expm1(g.pc.mean())) * 100, 1), round(float(np.expm1(g.y.mean())) * 100, 1)] for _, g in O.groupby("q")]
        m["adopted"] = bool(adopt)
        if not adopt:  # fall back to the age curve
            m["age_curve"] = {f"{p}|{int(a)}": round(float(v), 4) for (p, a), v in D.groupby([D.pos, D.age.round()])[y].mean().items()}
        models[h] = m
        rep[h] = dict(per_season=per, avg=avg, calib=calib, cal=m["cal"], seasons_won=wins, seasons=len(per), adopted=bool(adopt), alpha=best_alpha,
                      top_features=[[f, round(b, 3)] for f, b in sorted(zip(F, m["b"]), key=lambda t: -abs(t[1]))[:8]])
        log(h, "alpha", best_alpha, "adopt", adopt, "won", wins, "/", len(per), avg)
    # screens: hit rates on all history (rules were set by hand, not fit, but say so)
    scr = {}
    for k, (nm, desc, fn) in SCREENS.items():
        msk = fn(X)
        o = dict(name=nm, rule=desc, n=int(msk.sum()))
        for h in ("p8", "aug"):
            g = X.loc[msk, "y_" + h].dropna(); a = X["y_" + h].dropna()
            if len(g) < 30:
                continue
            per = X[msk].groupby("season")["y_" + h].mean().dropna()
            base = X.groupby("season")["y_" + h].mean()
            o[h] = dict(avg=round(float(np.expm1(g.mean() - a.mean())) * 100, 1), hit=round(float((g > math.log(1.25)).mean()) * 100),
                        base_hit=round(float((a > math.log(1.25)).mean()) * 100), n=int(len(g)),
                        seasons_better=int(sum(1 for s_, v_ in per.items() if v_ > base.get(s_, 0))), seasons=int(len(per)))
        scr[k] = o
    return models, rep, scr


def predict(models, X):
    out = {}
    for h, m in models.items():
        if m.get("adopted"):
            out[h] = ridge_pred(m, X[F].values)
        else:
            out[h] = np.array([m.get("age_curve", {}).get(f"{p}|{int(round(a))}", 0) for p, a in zip(X.pos, X.age)])
        out[h] = out[h] * np.array([m.get("cal", {}).get(t, 1.0) for t in X.tier])
    return out


# ---------------------------------------------------------------- main
def main():
    force = "--train" in sys.argv
    ids = pd.read_csv(fetch(f"{DPRAW}/db_playerids.csv", "ids.csv", 24 * 7), dtype=str)
    cur_path = dp_history()
    V = load_values(ids, cur_path)
    cur_season = season_of(TODAY)
    S = load_usage(ids, cur_season)
    bio, contracts = load_bio_contracts(ids)
    log("values", V.shape, V.date.min().date(), V.date.max().date(), "stats", S.shape, "contracts", len(contracts))

    mpath = os.path.join(DOCS, "futures_model.json")
    M = json.load(open(mpath)) if os.path.exists(mpath) else {}
    stale = not M or (TODAY - pd.Timestamp(M.get("trained", "2000-01-01"))).days >= 7
    hist_dates = sorted(d for d in V.date.unique() if d < TODAY - pd.Timedelta(days=1))
    Xh = None
    if force or stale:
        t0 = time.time()
        Xh = featurize(build_rows(V, S, bio, contracts, hist_dates))
        models, rep, scr = train(Xh)
        M = dict(trained=TODAY.strftime("%Y-%m-%d"), rows=int(len(Xh)), seasons=[int(Xh.season.min()), int(Xh.season.max())],
                 features=F, models=models, report=rep, screens=scr, src="DynastyProcess weekly superflex values, nflverse stats/snaps/contracts")
        json.dump(M, open(mpath, "w"), separators=(",", ":"))
        log(f"trained in {time.time() - t0:.0f}s on {len(Xh)} rows")

    # ---- today's calls
    now = V.date.max()
    Xn = build_rows(V, S, bio, contracts, [now], live=True)
    players = {}
    if len(Xn):
        Xn = featurize(Xn)
        pr = predict(M["models"], Xn)
        for k, (nm, desc, fn) in SCREENS.items():
            Xn["s_" + k] = fn(Xn).values
        for i, r in enumerate(Xn.itertuples()):
            why = []
            if r.dsnap >= .1: why.append(f"snap share up {r.dsnap * 100:.0f} pts lately")
            if r.perf_gap > 3: why.append(f"scoring {r.perf_gap:.1f} pts/game more than players at his price")
            if r.perf_gap < -3: why.append(f"scoring {-r.perf_gap:.1f} pts/game less than players at his price")
            if r.mom > .2: why.append(f"value up {r.mom * 100:.0f}% in 4 weeks")
            if r.mom < -.2: why.append(f"value down {-r.mom * 100:.0f}% in 4 weeks")
            if r.cl == 0: why.append("final contract year")
            if r.rk == 2 and r.s_old: why.append("starter ahead of him is aging")
            if r.rk == 2 and r.s_cl == 0: why.append("starter ahead of him is in a contract year")
            players[r.sid] = dict(n=r.name, v=int(r.v), c_end=int(r.c_end) if not pd.isna(r.c_end) else None, age=round(float(r.age), 1) if not pd.isna(r.age) else None, pos=r.pos, team=r.team,
                                  p4=round(float(np.expm1(pr["p4"][i])) * 100, 1) if "p4" in pr else None,
                                  p8=round(float(np.expm1(pr["p8"][i])) * 100, 1) if "p8" in pr else None,
                                  aug=round(float(np.expm1(pr["aug"][i])) * 100, 1) if "aug" in pr else None,
                                  snap=round(float(r.snap) * 100), dsnap=round(float(r.dsnap) * 100), tgt=round(float(r.tgt_sh) * 100), car=round(float(r.car_sh) * 100),
                                  ppg=round(float(r.ppg), 1), gap=round(float(r.perf_gap), 1), depth=int(r.rk) if not pd.isna(r.rk) else None,
                                  screens=[k for k in SCREENS if getattr(r, "s_" + k)], why=why[:3])
    # contracts for everyone we can match (end year, % of cap), shown in the app next to players
    con = {}
    for sid, cs in contracts.items():
        c = contract_at(contracts, sid, cur_season)
        if c:
            con[sid] = [c["end"], round(c["cap"], 1), round(c["apy"], 1)]
    # ---- log once per ISO week, grade old weeks against today's values (independent weekly snapshots)
    lpath = os.path.join(DOCS, "futures_log.json")
    L = json.load(open(lpath)) if os.path.exists(lpath) else {"weeks": {}, "graded": {}}
    wk = "%d-W%02d" % TODAY.isocalendar()[:2]
    if players and wk not in L["weeks"]:
        L["weeks"][wk] = dict(date=now.strftime("%Y-%m-%d"), p={s: [p["v"], p["p4"], p["p8"], p["aug"], p["screens"]] for s, p in players.items()})
    vnow = V[V.date == now].set_index("sid")["v"].to_dict()
    for k_, e in L["weeks"].items():
        age_d = (now - pd.Timestamp(e["date"])).days
        for h, lo, hi, idx in (("p4", 28, 34, 1), ("p8", 56, 62, 2)):
            key = f"{k_}|{h}"
            if lo <= age_d <= hi and key not in L["graded"]:
                rows_ = [(s, row) for s, row in e["p"].items() if s in vnow and row[idx] is not None and row[0] > 0]
                if len(rows_) >= 50:
                    # graded like the model is trained: change vs players logged at a similar value (rank bands of 20)
                    G = pd.DataFrame([(s, row[0], row[idx], math.log(vnow[s] / row[0]), row[4]) for s, row in rows_], columns=["s", "v", "p", "r", "scr"])
                    G["band"] = ((G.v.rank(ascending=False, method="first") - 1) // 20).clip(upper=9)
                    G["act"] = G.r - G.groupby("band").r.transform("median")
                    a = G[["p", "act"]].values; act = a[:, 1]
                    q = pd.qcut(pd.Series(a[:, 0]).rank(method="first"), 5, labels=False).values
                    scr_ = {}
                    for sk in SCREENS:
                        ss = G.act[[sk in x for x in G.scr]].values
                        if len(ss):
                            scr_[sk] = [len(ss), round(float(np.expm1(np.mean(ss))) * 100, 1), round(float(np.mean(ss > math.log(1.25))) * 100)]
                    L["graded"][key] = dict(n=len(G), sp=round(spearman(a[:, 0], act), 3),
                                            top=round(float(np.expm1(act[q == 4].mean())) * 100, 1), bottom=round(float(np.expm1(act[q == 0].mean())) * 100, 1),
                                            screens=scr_)
    for k_ in sorted(L["weeks"])[:-60]:
        L["weeks"].pop(k_)
    json.dump(L, open(lpath, "w"), separators=(",", ":"))
    live = {}
    for h in ("p4", "p8"):
        g = [v for k, v in L["graded"].items() if k.endswith("|" + h)]
        if g:
            live[h] = dict(snaps=len(g), sp=round(float(np.mean([x["sp"] for x in g])), 3), top=round(float(np.mean([x["top"] for x in g])), 1),
                           bottom=round(float(np.mean([x["bottom"] for x in g])), 1))
    out = dict(updated=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), values_date=now.strftime("%Y-%m-%d"),
               season=cur_season, week=int(Xn.week.max()) if len(Xn) else None,
               model=dict(trained=M.get("trained"), rows=M.get("rows"), seasons=M.get("seasons"), report=M.get("report"), screens=M.get("screens"), src=M.get("src")),
               live=live, logged_weeks=len(L["weeks"]), players=players, contracts=con,
               attribution="Values: DynastyProcess (CC BY). Stats, snaps and contracts: nflverse / OverTheCap.")
    json.dump(out, open(os.path.join(DOCS, "futures.json"), "w"), separators=(",", ":"))
    log("players", len(players), "contracts", len(con), "live", live)


if __name__ == "__main__":
    main()
