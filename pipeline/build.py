"""Dynasty HQ data refresh.

Pulls live league data from Sleeper, scores everything in the league's own format,
builds our projection model next to Sleeper's, simulates the rest of the season
and writes docs/data.json for the static site.

Stdlib only. Runs in GitHub Actions on a schedule (see .github/workflows/refresh.yml).
Set FIXTURE_DIR to run offline against saved responses (used for testing).
"""
import json, os, re, sys, time, math, random, statistics as st, urllib.request, datetime, itertools, bisect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
# DYN=<key> builds one of the other dynasty leagues in config.json "leagues" into docs/<key>/ (no key: the main league in docs/)
DYN = os.environ.get("DYN") or ""
if DYN:
    CFG = {**CFG, **CFG["leagues"][DYN]}
DOCS = os.path.join(ROOT, "docs", DYN) if DYN else os.path.join(ROOT, "docs")
CACHE = os.path.join(ROOT, "cache")
OUT = os.path.join(DOCS, "data.json")
os.makedirs(DOCS, exist_ok=True)
FIX = os.environ.get("FIXTURE_DIR")
REC = os.environ.get("RECORD_DIR")   # save every response here (to build test fixtures)
os.makedirs(CACHE, exist_ok=True)

API = "https://api.sleeper.app/v1"
FC = "https://api.fantasycalc.com/values/current?isDynasty=true&numQbs=2&numTeams=12&ppr=0.5"
POS = ("QB", "RB", "WR", "TE")


# ---------------------------------------------------------------- fetching
def _fixture_name(url):
    return re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[1])[:180] + ".json"


def get(url, cache_hours=None, required=True, headers=None):
    """GET JSON. Optional on-disk cache (hours). In fixture mode reads saved responses."""
    if FIX:
        p = os.path.join(FIX, _fixture_name(url))
        if os.path.exists(p):
            return json.load(open(p))
        if required:
            raise FileNotFoundError(p)
        return None
    cp = os.path.join(CACHE, _fixture_name(url)) if cache_hours else None
    if cp and os.path.exists(cp) and time.time() - os.path.getmtime(cp) < cache_hours * 3600:
        d_ = json.load(open(cp))
        if REC:
            os.makedirs(REC, exist_ok=True); json.dump(d_, open(os.path.join(REC, _fixture_name(url)), "w"))
        return d_
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dynasty-hq/1.0", **(headers or {})})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
            if cp:
                json.dump(data, open(cp, "w"))
            if REC:
                os.makedirs(REC, exist_ok=True); json.dump(data, open(os.path.join(REC, _fixture_name(url)), "w"))
            return data
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (attempt + 1))
    if cp and os.path.exists(cp):
        print(f"WARN using stale cache for {url}: {last}")
        return json.load(open(cp))
    if required:
        raise RuntimeError(f"fetch failed {url}: {last}")
    print(f"WARN optional fetch failed {url}: {last}")
    return None


# ---------------------------------------------------------------- scoring
class Scorer:
    def __init__(self, ss):
        self.ss = {k: v for k, v in ss.items() if v}
        self.te_bonus = ss.get("bonus_rec_te", 0) or 0

    def __call__(self, stats, pos):
        if not stats:
            return 0.0
        t = 0.0
        for k, w in self.ss.items():
            if k == "bonus_rec_te":
                continue
            v = stats.get(k)
            if v:
                t += v * w
        if pos == "TE" and self.te_bonus:
            t += (stats.get("rec") or 0) * self.te_bonus
        return round(t, 2)


# Lineup shape, set from the league's roster positions in main(): starters per position, flex (RB/WR/TE) and superflex spots.
LU_N = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
LU_FLEX, LU_SF = 3, 1
MANAGED = False   # True when managers set lineups (not best ball): starters are chosen before the games, by projection


def set_lineup_shape(positions):
    global LU_FLEX, LU_SF
    for k in LU_N:
        LU_N[k] = sum(1 for x in positions if x == k)
    LU_FLEX = sum(1 for x in positions if x in ("FLEX", "WRRB_FLEX", "REC_FLEX"))
    LU_SF = sum(1 for x in positions if x == "SUPER_FLEX")


def best_lineup(vals):
    """vals: iterable of (pos, pts). Best possible lineup (greedy is exact: flex and superflex eligibility nest)."""
    by = {"QB": [], "RB": [], "WR": [], "TE": []}
    for p, v in vals:
        if p in by:
            by[p].append(v)
    for l in by.values():
        l.sort(reverse=True)
    t = sum(sum(by[p][:LU_N[p]]) for p in by)
    rest = sorted(by["RB"][LU_N["RB"]:] + by["WR"][LU_N["WR"]:] + by["TE"][LU_N["TE"]:], reverse=True)
    t += sum(rest[:LU_FLEX])
    if LU_SF:
        t += sum(sorted(by["QB"][LU_N["QB"]:] + rest[LU_FLEX:], reverse=True)[:LU_SF])
    return t


def lineup_split(items):
    """items: (pos, pts, id). Same optimal lineup as best_lineup, returned as (total, points by position, counted ids)."""
    by = {"QB": [], "RB": [], "WR": [], "TE": []}
    for p, v, i in items:
        if p in by:
            by[p].append((v, i, p))
    for l in by.values():
        l.sort(key=lambda x: -x[0])
    used = [x for p in by for x in by[p][:LU_N[p]]]
    rest = sorted(by["RB"][LU_N["RB"]:] + by["WR"][LU_N["WR"]:] + by["TE"][LU_N["TE"]:], key=lambda x: -x[0])
    used += rest[:LU_FLEX]
    if LU_SF:
        used += sorted(by["QB"][LU_N["QB"]:] + rest[LU_FLEX:], key=lambda x: -x[0])[:LU_SF]
    split = {p: 0.0 for p in by}
    for v, i, p in used:
        split[p] += v
    return sum(split.values()), split, [i for _, i, _ in used]


def bracket(seeds, win):
    """Fixed playoff bracket like Sleeper's default (no reseeding). seeds: best first. win(x, y, round) -> winner.
    When the field isn't a power of 2 (e.g. 6 teams) the top seeds get first-round byes.
    Returns (champion, {team: first opponent actually played})."""
    B = 1
    while B < len(seeds):
        B *= 2
    order = [1, 2]
    while len(order) < B:
        n2 = 2 * len(order) + 1
        order = [x for s_ in order for x in (s_, n2 - s_)]
    slots = [seeds[i - 1] if i <= len(seeds) else None for i in order]
    first, rnd = {}, 0
    while len(slots) > 1:
        nxt = []
        for a, b in zip(slots[::2], slots[1::2]):
            if a is None or b is None:
                nxt.append(a if b is None else b); continue
            first.setdefault(a, b); first.setdefault(b, a)
            nxt.append(win(a, b, rnd))
        slots = nxt; rnd += 1
    return slots[0], first


def bracket_shape(n):
    B = 1
    while B < n:
        B *= 2
    return B - n, max(1, B.bit_length() - 1)   # (byes, rounds)


def bracket_check(wb, order, n_playoff):
    """Compare our playoff format with Sleeper's own bracket (winners_bracket): number of teams, byes, rounds.
    Sleeper fills its bracket from the current standings, so also report whether our current seeds put the same
    teams in (byes and round 1); that confirms the tiebreakers too."""
    if not isinstance(wb, list) or not wb:
        return dict(available=False)
    main_ = [m for m in wb if isinstance(m, dict) and not m.get("p") or (isinstance(m, dict) and m.get("p") == 1)]
    r1 = [m for m in wb if isinstance(m, dict) and m.get("r") == 1]
    r1_teams = {t for m in r1 for t in (m.get("t1"), m.get("t2")) if t}
    byes = set()
    for m in wb:
        if isinstance(m, dict) and m.get("r") == 2 and not m.get("p"):
            for side in ("t1", "t2"):
                if m.get(side) and not m.get(side + "_from"):
                    byes.add(m[side])
    rounds = max((m.get("r") or 0) for m in main_) if main_ else 0
    b_app, r_app = bracket_shape(n_playoff)
    out = dict(available=True, sleeper=dict(teams=len(r1_teams) + len(byes), byes=len(byes), rounds=rounds),
               app=dict(teams=n_playoff, byes=b_app, rounds=r_app))
    out["format_ok"] = out["sleeper"] == out["app"]
    seeds = order[:n_playoff]
    if r1_teams:
        out["seeds_match"] = set(seeds[:b_app]) == byes and set(seeds[b_app:]) == r1_teams
    return out


# ---------------------------------------------------------------- shared noise model
# The site re-implements these exact functions in JavaScript, so a simulation run in the browser
# uses the same random draws as the pipeline: odds shown on every page agree.
def _i32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


def _imul(a, b):
    return _i32((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF))


def h3(a, b, c):
    """Same bits as the JavaScript hash (int32 math done on unsigned 32-bit values)."""
    M = 0xFFFFFFFF
    h = (2166136261 ^ (a & M)) & M
    h = ((h ^ (b & M)) * 16777619) & M
    h = ((h ^ (c & M)) * 16777619) & M
    h ^= h >> 13
    h = (h * 0x5BD1E995) & M
    h ^= h >> 15
    return h / 4294967296


def znorm(a, s, w):
    u1 = max(1e-9, h3(a, s * 31 + 1, w * 7 + 3)); u2 = h3(a, s * 17 + 5, w * 13 + 11)
    return math.sqrt(-2 * math.log(u1)) * math.cos(6.283185307 * u2)


_TZ = {}


def team_z(t, s, w):
    key = (t, s, w)
    if key not in _TZ:
        _TZ[key] = znorm(team_num(t), s, w)
    return _TZ[key]


def team_num(t):
    n = 0
    for ch in (t or "FA"):
        n = n * 37 + ord(ch)
    return 900000 + n


RHO_T = 0.15          # share of weekly swing that is shared by teammates (game script, shootouts)
P_MISS = 0.05         # chance a healthy player misses any given future game
P_MISS_STATUS = {"Questionable": 0.25}
W_IND, W_TEAM = math.sqrt(1 - RHO_T), math.sqrt(RHO_T)


# Absences after this week: measured on last season's fantasy starters. A new absence (injury, benching) starts in about
# 5.5% of healthy weeks, and it usually lasts: 81% of the time a player who missed a week also missed the next.
# Durations below are last season's, in weeks (the last bucket means "rest of the season").
ABS_START = 0.055
ABS_DUR = [(1, .31), (2, .20), (3, .10), (4, .06), (5, .04), (6, .07), (7, .06), (8, .04), (10, .04), (17, .08)]


def absences(a, s, weeks):
    """Which future weeks (index 1+) this player misses in simulation s. Week 0 uses his current injury status instead."""
    out = [False] * len(weeks)
    until = 0
    for k in range(1, len(weeks)):
        if k <= until:
            out[k] = True; continue
        w = weeks[k]
        if h3(a, s * 37 + 9, w * 41 + 17) < ABS_START:
            u, acc, d = h3(a, s * 53 + 3, w * 59 + 7), 0.0, ABS_DUR[-1][0]
            for dur, pr in ABS_DUR:
                acc += pr
                if u < acc:
                    d = dur; break
            until = k + d - 1; out[k] = True
    return out


# Shape of a weekly score around its projection. Default: lognormal. When SHAPE has the player's cell (position and
# projection level), draws follow the real outcomes measured for that cell instead: score / projection, as K quantiles.
# Real weeks have more duds and more blow-ups than a lognormal, and players projected for only a few points (deep bench)
# hit big weeks far more often than their projection suggests, which is what best ball lineups feed on.
# The same random numbers are used either way, so teammates still move together.
SHAPE = {}                    # "POS|level" -> [K quantiles of score/projection, their mean]
CV0 = {"QB": 0.42, "RB": 0.55, "WR": 0.62, "TE": 0.65}   # typical spread by position: a player's cv / CV0 scales his spread
LEVELS = (4, 8, 13)           # projection levels: under 4, 4-8, 8-13, 13+


def level(m):
    return 0 if m < 4 else 1 if m < 8 else 2 if m < 13 else 3


def phi(x):
    """Normal CDF (Abramowitz-Stegun 7.1.26; the page uses the identical formula)."""
    y = abs(x) / 1.4142135623730951
    t = 1.0 / (1.0 + 0.3275911 * y)
    e = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * math.exp(-y * y)
    return 0.5 * (1.0 + (e if x >= 0 else -e))


def shape_ratio(cell, f, z):
    q, mu = SHAPE[cell]
    K = len(q)
    x = min(max(phi(z) * K - 0.5, 0.0), K - 1.0)
    i = int(x)
    r = q[i] if i >= K - 1 else q[i] + (q[i + 1] - q[i]) * (x - i)
    return max(0.0, mu + (r - mu) * f)


def fit_shape(rows, K=40, min_n=80, norm=False):
    """rows: (pos, actual, projection) for games played. Returns {cell: [quantiles, mean]} for cells with enough games.
    norm=True rescales each cell to average exactly 1 (keeps the projection's mean, changes only the shape)."""
    out, by = {}, {}
    for ps, a, m in rows:
        if m > 0.5:
            by.setdefault(f"{ps}|{level(m)}", []).append(max(a, 0) / m)
    for cell, r in by.items():
        if len(r) < min_n:
            continue
        r = sorted(r)
        n = len(r)
        q = []
        for k in range(K):
            x = (k + 0.5) / K * n - 0.5
            i = min(max(int(x), 0), n - 2)
            q.append(r[i] + (r[i + 1] - r[i]) * min(max(x - i, 0), 1))
        mu = st.mean(q)
        if norm:
            q = [v / mu for v in q]; mu = 1.0
        out[cell] = [[round(v, 3) for v in q], round(mu, 3)]
    return out


def shape_tail(pos, m, cv, t):
    """Chance a score beats t under the measured shape (same table and scaling the draws use)."""
    cell = f"{pos}|{level(m)}"
    if m <= 0 or not t or cell not in SHAPE:
        return None
    q, mu = SHAPE[cell]
    f = cv / CV0.get(pos, 0.6)
    K = len(q)
    vals = [max(0.0, mu + (v - mu) * f) * m for v in q]
    return sum(1 for v in vals if v >= t) / K


def sim_points(m, cv, sid, team, s, w, p_miss, pos=None):
    """One simulated score (mean m), partly shared with teammates, with a chance of missing the game.
    Lognormal, or the measured real shape when SHAPE has the player's cell."""
    if m <= 0:
        return 0.0
    a = int(sid) if str(sid).isdigit() else 7
    if h3(a, s * 37 + 9, w * 41 + 17) < p_miss:
        return 0.0
    z = W_IND * znorm(a, s, w) + W_TEAM * team_z(team, s, w)
    if SHAPE and pos:
        cell = f"{pos}|{level(m)}"
        if cell in SHAPE:
            return m * shape_ratio(cell, cv / CV0.get(pos, 0.6), z)
    sig = math.sqrt(math.log(1 + cv * cv))
    return m * math.exp(sig * z - sig * sig / 2)


AGE_CLIFF = {"QB": 34, "RB": 27, "WR": 30, "TE": 31}
ORD = ["", "1st", "2nd", "3rd", "4th", "5th"]


# ---------------------------------------------------------------- small linear algebra
def lstsq(X, y):
    n = len(X[0])
    A = [[sum(r[i] * r[j] for r in X) for j in range(n)] for i in range(n)]
    b = [sum(r[i] * yy for r, yy in zip(X, y)) for i in range(n)]
    for i in range(n):  # gaussian elimination w/ partial pivot
        p = max(range(i, n), key=lambda r: abs(A[r][i]))
        A[i], A[p] = A[p], A[i]
        b[i], b[p] = b[p], b[i]
        if abs(A[i][i]) < 1e-9:
            continue
        for r in range(i + 1, n):
            f = A[r][i] / A[i][i]
            for c in range(i, n):
                A[r][c] -= f * A[i][c]
            b[r] -= f * b[i]
    x = [0.0] * n
    for i in reversed(range(n)):
        if abs(A[i][i]) < 1e-9:
            continue
        x[i] = (b[i] - sum(A[i][c] * x[c] for c in range(i + 1, n))) / A[i][i]
    return x


def _mean(x):
    x = list(x)
    return sum(x) / len(x)


def linfit(xs, ys):
    if len(xs) < 3:
        return 0.0, (st.mean(ys) if ys else 5.0)
    mx, my = st.mean(xs), st.mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx if vx else 0.0
    return a, my - a * mx


# ---------------------------------------------------------------- multi-season outlook
# Typical change in a player's production from one season to the next, by position and age
# (general NFL aging patterns: RBs peak early and fall fast; WRs and TEs peak later; QBs last longest).
AGE_STEP = {
    "QB": [(24, 0.08), (25, 0.05), (29, 0.02), (32, 0.0), (33, -0.05), (34, -0.10), (99, -0.18)],
    "RB": [(23, 0.08), (24, 0.03), (25, 0.0), (26, -0.05), (27, -0.10), (28, -0.15), (99, -0.22)],
    "WR": [(23, 0.10), (24, 0.06), (25, 0.03), (27, 0.0), (28, -0.04), (29, -0.08), (30, -0.12), (99, -0.18)],
    "TE": [(24, 0.10), (25, 0.06), (26, 0.03), (28, 0.0), (29, -0.05), (30, -0.10), (99, -0.15)],
}
RETIRE = {"QB": 40, "RB": 32, "WR": 35, "TE": 36}
ROOKIE_YR = (0.6, 0.85)        # share of a pick's value-implied production in a rookie's 1st and 2nd seasons
DISC = (1.0, 0.8, 0.6)         # weight on this season, next season, the one after (later = less certain)
FUT_WEEKS, FUT_PO = 10, 3      # each future season is simulated as 10 regular weeks + 3 playoff weeks
STR_S, STR_W = 120, 4          # sims x weeks used to measure a roster's strength in a future season


def age_step(pos, age):
    for upto, ch in AGE_STEP[pos]:
        if age <= upto:
            return 1 + ch
    return 1.0


def rookie_sid(y, rd, o):
    return 700000 + int(h3(int(y), int(rd), int(o)) * 99999)


def future_items(ids, P, pick_list, j, season, rk):
    """Players (mean points per game in season+j) plus the rookies this roster's picks bring by then."""
    out = []
    for p_ in ids:
        d = P[p_]
        fp = (d.get("fp") or [0, 0])[j - 1]
        if fp > 0:
            out.append((d["p"], fp, d.get("cv") or 0.6, p_, d.get("t") or "FA"))
    for pk in pick_list:
        y = pk["y"]
        if y <= season + j:
            yr = season + j - y          # 0 = rookie season
            if yr < len(ROOKIE_YR):
                m = ROOKIE_YR[yr] * max(0.0, rk[0] * pk["v"] + rk[1])
                if m > 0:
                    out.append(("WR", m, 0.7, rookie_sid(y, pk["rd"], pk["o"]), "FA"))
    return out


def future_strength(items, j):
    tot = 0.0
    for s_ in range(STR_S):
        for wk in range(STR_W):
            tot += best_lineup((pos, sim_points(m, cv, sid, tm, s_, 200 + j * 20 + wk, P_MISS, pos)) for pos, m, cv, sid, tm in items)
    return tot / (STR_S * STR_W)


# ---------------------------------------------------------------- the recommendation
def build_advice(c):
    """Turn the numbers into one plain recommendation. Every sentence is built from a number the site also shows."""
    P, me, mt, S0 = c["P"], c["me"], c["mt"], c["S0"]
    pct = lambda x: ">99%" if x >= 0.995 else ("<1%" if 0 < x < 0.005 else f"{round(x * 100)}%")
    ordn = lambda n: f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
    nm = lambda pid: P[pid]["n"]
    po, bye, title = mt["odds"]["playoffs"], mt["odds"]["bye"], mt["odds"]["title"]
    se = c["title_se"].get(me, 0.02)
    wk = c["this_week"]; dl = c["deadline"]
    open_ = bool(c["fut"]) and (not dl or (wk or 99) <= dl)
    weeks_left = (dl - wk + 1) if (open_ and dl and wk) else 0
    dp = c["depth"].get(me) or {}
    weak = sorted(dp, key=lambda p_: -dp[p_][1])[:1] if dp else []
    reasons, moves, watch = [], [], []
    for a in c["actions"]:
        moves.append(dict(kind="todo", text=a))
    if not c["fut"]:
        sells = [x for x in c["market"]["sell"]][:3]
        verdict, headline = "offseason", "Season's over: set up next year"
        reasons.append(f"You finished {mt['w']}-{mt['l']}. Until next season, trade value is what counts, not points.")
        for x in sells:
            moves.append(dict(kind="sell", pid=x["id"], text=f"Shop {nm(x['id'])} ({'; '.join(x['why'])})."))
        return dict(verdict=verdict, headline=headline, moves=moves, reasons=reasons, watch=watch, numbers=dict())
    # rank trade ideas by title chances across this season and the next two (later seasons count a bit less)
    ideas = sorted((dict(o, team=int(t)) for t, L in c["sugg"].items() for o in L if o.get("title1") is not None),
                   key=lambda o: -((o.get("E1", 0) - o.get("E0", 0)) if o.get("E1") is not None else (o["title1"] - o["title0"])))
    thr = max(0.02, se)
    win = c.get("window") or {}
    best = next((o for o in ideas if o["title1"] - o["title0"] >= thr and o["kr"] >= 0.85 * o["kg"]
                 and (o.get("E1") is None or o["E1"] >= o["E0"])), None)
    reasons.append(f"You're {mt['w']}-{mt['l']} and make the playoffs in {pct(po)} of simulated seasons. Your title chance is {pct(title)}.")
    tb, tn = mt.get("title_bye"), mt.get("title_nobye")
    if tb is not None and tn is not None and tb - tn >= 0.05 and 0.1 < bye < 0.9:
        reasons.append(f"A top-2 seed matters: you win the title {pct(tb)} of the time with the first-round bye and {pct(tn)} without it. You get the bye in {pct(bye)} of seasons right now.")
    pr = sorted([t for t in c["teams"] if t["odds"]["playoffs"] >= 0.05], key=lambda t: -(t.get("po") or 0))
    my_po_rank = next((i + 1 for i, t in enumerate(pr) if t["id"] == me), None)
    if my_po_rank:
        reasons.append(f"Your projected playoff-week lineup ranks {ordn(my_po_rank)} of {len(pr)} contenders ({mt.get('po')} points a week).")
    if weak:
        reasons.append(f"Your weakest position is {weak[0]} ({ordn(dp[weak[0]][1])} of {len(c['teams'])}), so that's where an upgrade helps most.")
    # the multi-season picture
    if win.get("ranks"):
        y_ = win["years"]; r_ = win["ranks"]; t_ = win["titles"]
        reasons.append(f"Looking ahead, your roster projects {ordn(r_[0])} in {y_[0]}, {ordn(r_[1])} in {y_[1]} and {ordn(r_[2])} in {y_[2]} "
                       f"(title chances about {pct(t_[0])}, {pct(t_[1])}, {pct(t_[2])}), after aging and the rookies your picks will bring.")
        if win["kind"] == "contend_now":
            fr = ", ".join(str(y) for y in win["firsts"]) or "none"
            reasons.append(f"Your window is now: your draft picks rank {ordn(win['picks_rank'])} of {len(c['teams'])} (1st-round picks: {fr}), so avoid trading more future picks unless a deal clearly raises your title chances.")
        elif win["kind"] == "contend":
            reasons.append("Your roster holds up for the next two seasons too, so you can afford to push for titles now.")
    if not open_:
        verdict, headline = "stay", "Ride it out: trading is closed"
        reasons.append("The trade deadline has passed, so the only moves left are free agents.")
    elif po < 0.3 or win.get("kind") == "rebuild":
        verdict, headline = "sell", "Sell for the future"
        reasons.append(f"With only a {pct(po)} playoff chance, points this season are worth less than trade value for next year.")
        older = sorted([p_ for p_ in c["ids_by"][me] if (P[p_].get("age") or 0) >= 27 and (P[p_].get("val") or 0) >= 2500], key=lambda p_: -(P[p_].get("val") or 0))[:3]
        for p_ in older:
            moves.append(dict(kind="sell", pid=p_, text=f"Shop {nm(p_)} (age {int(P[p_]['age'])}) for younger players or picks while he still has value."))
    elif best:
        verdict, headline = "trade", "Make a trade"
        give = " + ".join(nm(x) for x in best["give"]); get = " + ".join(nm(x) for x in best["get"])
        moves.append(dict(kind="trade", team=best["team"], give=best["give"], get=best["get"],
                          text=f"Offer {c['names'][best['team']]}: {give} for {get}."))
        gave_imp = sum(P[x].get("imp", 0) for x in best["give"])
        reasons.append(f"That trade raises your title chance from {pct(best['title0'])} to {pct(best['title1'])}, and your lineup comes out {best['dm']:.1f} points a week ahead overall. "
                       f"{give} add{'s' if len(best['give']) == 1 else ''} {gave_imp:.1f} points a week for you today, but {get} {'is' if len(best['get']) == 1 else 'are'} worth more to your lineup.")
        vd = best["kr"] - best["kg"]
        reasons.append(f"The trade values are close enough for them to say yes (you give {best['kg']:,}, get {best['kr']:,})" +
                       (f", and it helps their lineup too ({best['dt']:+.1f} a week), which makes it an easy sell." if best['dt'] >= 0.3 else
                        f", and it barely changes their lineup ({best['dt']:+.1f} a week)." if best['dt'] > -1 else f", though it costs their lineup {-best['dt']:.1f} a week, so expect some pushback."))
        if best.get("fut"):
            reasons.append(f"It doesn't cost your future: title chances in the next two seasons go from {pct(win['titles'][1])} and {pct(win['titles'][2])} to {pct(best['fut'][0])} and {pct(best['fut'][1])}.")
        if weeks_left:
            reasons.append(f"You have {weeks_left} week{'s' if weeks_left != 1 else ''} until the trade deadline.")
    else:
        verdict = "stay" if po >= 0.75 else "upgrade"
        headline = "Stay the course" if verdict == "stay" else "Look for a cheap upgrade"
        tested = len(ideas)
        reasons.append(f"We tested the best fair trade with {tested} teams and none clearly raises your title chance beyond the simulation's margin (about {round(thr * 100)} points)."
                       if tested else "No fair trade we found helps your lineup enough to be worth making.")
        if verdict == "upgrade":
            buys = [x for x in c["market"]["buy"] if not weak or P[x["id"]]["p"] == weak[0]][:2] or c["market"]["buy"][:2]
            for x in buys:
                moves.append(dict(kind="buy", pid=x["id"], team=P[x["id"]]["r"], text=f"Ask about {nm(x['id'])} ({c['names'].get(P[x['id']]['r'], '')}): he'd add {x['add']:.1f} points a week and is cheap for his production."))
        if weeks_left:
            reasons.append(f"Re-check before the deadline ({weeks_left} week{'s' if weeks_left != 1 else ''} left): injuries change this quickly.")
    if c["fa"] and c["fa"][0]["add"] >= 1.0:
        f = c["fa"][0]; cut = c["cut"][0] if c["cut"] else None
        moves.append(dict(kind="add", pid=f["id"], text=f"Pick up {nm(f['id'])} off waivers (+{f['add']:.1f} points a week)" + (f" and drop {nm(cut)}." if cut else ".")))
    # things to watch
    for p_ in c["ids_by"][me]:
        d = P[p_]
        if d.get("inj") and d.get("imp", 0) >= 3:
            watch.append(f"{d['n']} is {d['inj']}. He's one of your most important players ({d['imp']:.1f} points a week).")
    reg = [k for k, w in enumerate(c["fut"]) if w <= c["reg_end"]]
    wm = c["wk_mean"].get(me) or []
    if reg and wm:
        avg = sum(wm[k] for k in reg) / len(reg); low = min(reg, key=lambda k: wm[k])
        if wm[low] < 0.92 * avg:
            watch.append(f"Week {c['fut'][low]} is your weakest projected week ({wm[low]:.0f} points vs your usual {avg:.0f}), mostly byes.")
    for x in c["market"]["sell"][:2]:
        watch.append(f"{nm(x['id'])} may be worth more now than later: {x['why'][0]}.")
    for p_ in c["ids_by"][me]:
        vt = P[p_].get("vt")
        if vt and P[p_].get("val") and P[p_]["val"] >= 3000 and (P[p_]["val"] - vt[0]) / vt[0] <= -0.12:
            watch.append(f"{P[p_]['n']}'s trade value has dropped {round((vt[0] - P[p_]['val']) / vt[0] * 100)}% in {vt[1]} days.")
    return dict(verdict=verdict, headline=headline, moves=moves, reasons=reasons, watch=watch[:5], window=win.get("label"),
                numbers=dict(playoffs=po, bye=bye, title=title, title_after=best["title1"] if best else None))


def claude_advice(draft, model):
    """Optional: have Claude write the recommendation from the same facts. Returns None on any problem (the rules version is used)."""
    try:
        facts = json.dumps({k: draft[k] for k in ("verdict", "headline", "moves", "reasons", "watch", "numbers")}, ensure_ascii=False)
        prompt = ("You manage a superflex best-ball dynasty fantasy football team. Below is this week's analysis from the team's dashboard "
                  "(simulation-based; numbers are reliable). Write the manager's recommendation in plain English for a novice. "
                  "Keep the same verdict and the same specific moves, do not invent players, trades or numbers, and use only facts given. "
                  "Return JSON only: {\"headline\": short verdict like 'Make a trade' or 'Stay the course', \"summary\": 2-3 sentences, "
                  "\"reasons\": [3-5 short sentences], \"watch\": [0-4 short sentences]}.\n\nANALYSIS:\n" + facts)
        body = json.dumps({"model": model, "max_tokens": 900, "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                     headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read().decode())
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        j = json.loads(text[text.index("{"): text.rindex("}") + 1])
        res = {}
        if isinstance(j.get("headline"), str) and len(j["headline"]) < 80:
            res["headline"] = j["headline"]
        if isinstance(j.get("summary"), str):
            res["summary"] = j["summary"][:800]
        if isinstance(j.get("reasons"), list) and j["reasons"]:
            res["reasons"] = [str(x)[:400] for x in j["reasons"][:6]]
        if isinstance(j.get("watch"), list):
            res["watch"] = [str(x)[:300] for x in j["watch"][:5]]
        return res or None
    except Exception as e:  # noqa
        print(f"WARN Claude write-up skipped: {e}")
        return None


# ---------------------------------------------------------------- main build
def main():
    lid, me_owner = CFG["league_id"], CFG["owner_id"]
    state = get(f"{API}/state/nfl")
    league = get(f"{API}/league/{lid}")
    # Sleeper gives the league a new id every season: follow it forward so the app never gets stuck on an old year
    for _ in range(5):
        st_s = str(state.get("league_season") or state.get("season") or "")
        if not st_s.isdigit() or int(st_s) <= int(league["season"]):
            break
        mine_l = get(f"{API}/user/{me_owner}/leagues/nfl/{int(league['season']) + 1}", required=False) or []
        nxt = next((l_ for l_ in mine_l if str(l_.get("previous_league_id")) == str(lid)), None)
        if not nxt:
            break
        print(f"following league into {nxt.get('season')}: {nxt['league_id']}")
        lid = nxt["league_id"]; league = get(f"{API}/league/{lid}")
    season = int(league["season"])
    users = get(f"{API}/league/{lid}/users")
    rosters = get(f"{API}/league/{lid}/rosters")
    traded = get(f"{API}/league/{lid}/traded_picks", required=False) or []
    settings = league.get("settings", {})
    global MANAGED, SHAPE
    set_lineup_shape(league.get("roster_positions") or [])
    MANAGED = not settings.get("best_ball")
    sc_ = league.get("scoring_settings") or {}
    FC_URL = (f"https://api.fantasycalc.com/values/current?isDynasty=true&numQbs={2 if LU_SF else 1}"
              f"&numTeams={int(league.get('total_rosters') or 12)}&ppr={sc_.get('rec', 1) or 0:g}")
    playoff_start = int(settings.get("playoff_week_start", 15))
    reg_end = playoff_start - 1
    last_week = int(CFG.get("last_week", 17))
    n_playoff = int(settings.get("playoff_teams", 6))
    N_BYE, N_ROUNDS = bracket_shape(n_playoff)
    median_on = bool(settings.get("league_average_match"))
    score = Scorer(league["scoring_settings"])

    # current week: games of `week` may be in progress; completed = week-1 (or full season if offseason)
    st_season = str(state.get("season"))
    cur = int(state.get("week") or 1) if st_season == str(season) else (last_week + 1)
    if state.get("season_type") != "regular" and st_season == str(season):
        cur = max(cur, 1)
    done = max(0, min(cur - 1, last_week))
    fut = list(range(max(done + 1, 1), last_week + 1))

    players_db = get(f"{API}/players/nfl", cache_hours=0.3)  # injury status lives here: keep it fresh (the second app in the same run reuses it)
    # two-way players (e.g. Travis Hunter is listed as DB but plays WR for fantasy): use the fantasy position Sleeper scores him at
    for d_ in players_db.values():
        if isinstance(d_, dict) and d_.get("position") not in POS:
            fp_ = [x for x in (d_.get("fantasy_positions") or []) if x in POS]
            if fp_:
                d_["position"] = fp_[0]
    uname = {u["user_id"]: (u.get("metadata") or {}).get("team_name") or u.get("display_name") for u in users}
    teams = []
    for r in rosters:
        teams.append(dict(id=r["roster_id"], owner=r.get("owner_id"), name=uname.get(r.get("owner_id"), f"Team {r['roster_id']}"),
                          user=next((u.get("display_name") for u in users if u["user_id"] == r.get("owner_id")), ""),
                          players=[str(p) for p in (r.get("players") or [])],
                          taxi=[str(p) for p in (r.get("taxi") or [])], ir=[str(p) for p in (r.get("reserve") or [])],
                          faab_left=int(settings.get("waiver_budget", 0) or 0) - int((r.get("settings") or {}).get("waiver_budget_used", 0) or 0)))
    me = next(t["id"] for t in teams if t["owner"] == me_owner)
    rostered = {p: t["id"] for t in teams for p in t["players"]}

    def pinfo(pid):
        d = players_db.get(pid) or {}
        pos = d.get("position") or (d.get("fantasy_positions") or [None])[0]
        return dict(n=d.get("full_name") or f"{d.get('first_name','')} {d.get('last_name','')}".strip() or pid,
                    p=pos, t=d.get("team") or "", age=d.get("age"), inj=d.get("injury_status"), yrs=d.get("years_exp"))

    # ---------- stats (league-scored) this season + last season
    pts, use = {}, {}
    ext = {}       # pid -> "yr-wk" -> [snaps, team snaps, targets, carries, pass att, rz targets, rz carries, air yards]
    tm_tot = {}    # (team, "yr-wk") -> [team targets, team carries]

    def load_stats(yr, wk, cache):
        url = f"{API}/stats/nfl/regular/{yr}/{wk}"
        return get(url, cache_hours=(24 * 365 if cache else None), required=False) or {}

    for yr, weeks, cache in ((season - 1, range(1, last_week + 1), True), (season, range(1, done + 1), False)):
        for wk in weeks:
            data = load_stats(yr, wk, cache)
            for pid, s in data.items():
                if not isinstance(s, dict):
                    continue
                info = players_db.get(pid) or {}
                pos = info.get("position")
                if pos not in POS:
                    continue
                if not (s.get("gp") or s.get("gms_active") or s.get("off_snp")):
                    continue
                pts.setdefault(pid, {})[f"{yr}-{wk}"] = score(s, pos)
                use.setdefault(pid, {})[f"{yr}-{wk}"] = [s.get("pass_att", 0) or 0, s.get("rush_att", 0) or 0, s.get("rec_tgt", 0) or 0]
                g_ = lambda k_: float(s.get(k_) or 0)
                ext.setdefault(pid, {})[f"{yr}-{wk}"] = [g_("off_snp"), g_("tm_off_snp"), g_("rec_tgt"), g_("rush_att"), g_("pass_att"),
                                                         g_("rec_rz_tgt"), g_("rush_rz_att"), g_("rec_air_yd")]
                tk_ = (info.get("team") or "", f"{yr}-{wk}")
                tt_ = tm_tot.setdefault(tk_, [0.0, 0.0]); tt_[0] += g_("rec_tgt"); tt_[1] += g_("rush_att")

    # ---------- Sleeper projections (rescored), past weeks for backtest + future weeks
    proj = {}
    for wk in range(1, last_week + 1):
        data = get(f"{API}/projections/nfl/regular/{season}/{wk}", cache_hours=(24 * 365 if wk <= done else 0.3 if wk == done + 1 else 3), required=False) or {}
        for pid, s in data.items():
            if not isinstance(s, dict):
                continue
            pos = (players_db.get(pid) or {}).get("position")
            if pos not in POS or (pid not in rostered and not (players_db.get(pid) or {}).get("team")):
                continue
            v = score(s, pos)
            if v > 0:
                proj.setdefault(pid, {})[wk] = v

    # ---------- more inputs: ESPN's projections and betting lines, kept only if they make this season's misses smaller
    import sources as SRC
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
              for p_, d_ in proj_sl.items() for w, v in d_.items() if w <= done and v >= 3 and f"{season}-{w}" in pts.get(p_, {})]
    src_par, src_rep = SRC.tune(t_rows)   # applied later, only if the honest week-by-week test agrees
    src_rep.update(espn_players=n_es, lines=len(it_lines))

    # ---------- dynasty values (FantasyCalc API, auto) + optional KTC snapshot (manual file)
    fc_vals, fc_picks, fc_pick_raw, fc_meta = {}, {}, {}, {}
    fc = get(FC_URL, cache_hours=12, required=False) or []
    for e in fc:
        pl = e.get("player") or {}
        sid = pl.get("sleeperId")
        if pl.get("position") == "PICK":
            # names seen: "2027 1st", "2027 1st (Early)", "2027 Early 1st", "2027 Round 1", "2027 Pick 1.05"
            nm_ = pl.get("name", "")
            m = re.match(r"(\d{4})\s+(?:Pick\s+(\d)\.(\d+)|(?:(Early|Mid|Late)\s+)?(?:Round\s+)?(\d)(?:st|nd|rd|th)?(?:\s*\((Early|Mid|Late)\))?)\s*$", nm_)
            if m:
                y_ = int(m.group(1)); rd_ = int(m.group(2) or m.group(5))
                tier = m.group(4) or m.group(6)
                if m.group(2):  # exact pick number -> early / mid / late thirds of a 12-team round
                    n_ = int(m.group(3)); tier = "Early" if n_ <= 4 else "Late" if n_ >= 9 else "Mid"
                fc_pick_raw.setdefault((y_, rd_), {}).setdefault(tier or "", []).append(e.get("value", 0))
        elif sid:
            fc_vals[str(sid)] = e.get("value", 0)
            fc_meta[str(sid)] = (e.get("trend30Day") or 0, e.get("maybeRosterPercent"))
    fc_tier = {}
    for key, d_ in fc_pick_raw.items():
        for tier, vs in d_.items():
            fc_tier[key + (tier,)] = sum(vs) / len(vs)
        if d_.get(""):
            fc_picks[key] = fc_tier[key + ("",)]
        elif d_.get("Mid"):
            fc_picks[key] = fc_tier[key + ("Mid",)]
        else:
            fc_picks[key] = sum(sum(v) for v in d_.values()) / sum(len(v) for v in d_.values())
    ktc_path = os.path.join(ROOT, "pipeline", "ktc_snapshot.json")
    ktc = json.load(open(ktc_path)) if os.path.exists(ktc_path) else {"date": None, "players": {}, "picks": {}}
    # ---------- one value scale for players AND picks.
    # FantasyCalc (built from real trades, updated daily, set to 12-team superflex 0.5 PPR) decides the ORDER and
    # the gaps between everything, picks included. Its numbers are then laid onto the KeepTradeCut superflex
    # TE-premium scale by rank: the Nth most valuable asset on FantasyCalc gets the KTC value of the Nth most
    # valuable player in the KTC snapshot. One curve for everyone keeps players and picks on the same footing.
    # A per-position factor from the snapshot then adds back what FantasyCalc doesn't price, mainly TE premium.
    KP = ktc.get("players") or {}
    both = [(fc_vals[p_], KP[p_], (players_db.get(p_) or {}).get("position")) for p_ in KP if fc_vals.get(p_, 0) > 0 and KP[p_] > 0]
    def make_curve(prs):
        xf = sorted(x for x, _ in prs); xk = sorted(y for _, y in prs)
        def f_(v):
            if not xf or v <= 0:
                return None
            if v <= xf[0]:
                return xk[0] * v / xf[0]
            if v >= xf[-1]:
                return xk[-1]
            i = bisect.bisect_left(xf, v)
            lo, hi = xf[i - 1], xf[i]
            t_ = (v - lo) / (hi - lo) if hi > lo else 0.0
            return xk[i - 1] + t_ * (xk[i] - xk[i - 1])
        return f_
    nq = len(both)
    conv_all = make_curve([(x[0], x[1]) for x in both]) if nq >= 30 else (lambda v: None)
    def conv(f):  # for picks and anything without a position: the all-players curve
        return conv_all(f) if f else None
    # players: one curve per position, so each position keeps KTC's own shape (TE premium, QB premium in superflex)
    cal = {}
    for ps in POS:
        prs = [(x[0], x[1]) for x in both if x[2] == ps]
        if len(prs) >= 20:
            cal[ps] = (make_curve(prs), len(prs))
    def unified(pid, f=None, pos=None):
        f = fc_vals.get(pid) if f is None else f
        pos = pos or (players_db.get(pid) or {}).get("position")
        c_ = (cal[pos][0](f) if pos in cal else conv(f)) if f else None
        if c_ is not None:
            return int(round(min(9999, c_)))
        return KP.get(pid) or 0
    val_src = dict(live=nq >= 30, snapshot=ktc.get("date"), fitted={ps: cal[ps][1] for ps in cal}, matched=nq,
                   label=("FantasyCalc, updated daily, laid onto the KeepTradeCut superflex TE-premium scale" if nq >= 30
                          else f"KeepTradeCut snapshot from {ktc.get('date')} (live values unavailable this run)"))

    # ---------- matchups (past scores, future pairings, weekly rosters)
    weeks, sched, my_weekly, live, live_pp = {}, {}, {}, {}, {}
    cur_starters = {}
    all_weekly = {}
    checks = dict(lineups=0, lineup_bad=[], scored=0, score_bad=[])
    po_actual = {}
    for wk in range(1, last_week + 1):
        if wk > reg_end and wk > done + 1:
            break
        m = get(f"{API}/league/{lid}/matchups/{wk}", required=False) or []
        if wk > reg_end:
            if wk <= done:
                po_actual[wk] = {x["roster_id"]: round(x.get("points") or 0, 2) for x in m}
            elif any(x.get("players_points") for x in m):
                for x in m:
                    for k_, v_ in (x.get("players_points") or {}).items():
                        live_pp[str(k_)] = v_
                live = {x["roster_id"]: (round(x.get("points") or 0, 2) if MANAGED else round(best_lineup(((players_db.get(str(k_)) or {}).get("position"), v_)
                                                          for k_, v_ in (x.get("players_points") or {}).items()), 2)) for x in m}
                if not any(live.values()):
                    live = {}
            continue
        if wk <= done:
            weeks[wk] = [[x["roster_id"], x.get("matchup_id"), round(x.get("points") or 0, 2)] for x in m]
            # data check: rebuild every lineup from Sleeper's per-player points and our own scoring
            for x in m:
                pp = x.get("players_points") or {}
                if not pp:
                    continue
                calc = (sum(pp.get(str(k_), 0) for k_ in (x.get("starters") or []) if k_ and k_ != "0") if MANAGED
                        else best_lineup(((players_db.get(str(k_)) or {}).get("position"), v_) for k_, v_ in pp.items()))
                checks["lineups"] += 1
                if abs(calc - (x.get("points") or 0)) > 0.6:
                    checks["lineup_bad"].append([wk, x["roster_id"], round(calc, 1), x.get("points")])
                for k_, v_ in pp.items():
                    ours = pts.get(str(k_), {}).get(f"{season}-{wk}")
                    if ours is not None:
                        checks["scored"] += 1
                        if abs(ours - v_) > 0.11:
                            checks["score_bad"].append([wk, str(k_), ours, v_])
            for x in m:
                all_weekly.setdefault(wk, {})[x["roster_id"]] = [str(p) for p in ((x.get("starters") if MANAGED else x.get("players")) or []) if p and str(p) != "0"]
            mine = next((x for x in m if x["roster_id"] == me), None)
            if mine:
                my_weekly[wk] = [str(p) for p in (mine.get("players") or [])]
        else:
            sched[wk] = [[x["roster_id"], x.get("matchup_id")] for x in m]
            if wk == done + 1:
                cur_starters = {x["roster_id"]: [str(p) for p in (x.get("starters") or []) if p and str(p) != "0"] for x in m}
            if wk == done + 1 and any(x.get("players_points") for x in m):
                # Sleeper's "points" mid-week only counts the manually set starters; best ball counts the best
                # possible lineup, so rebuild it from every rostered player's points so far.
                for x in m:
                    for k_, v_ in (x.get("players_points") or {}).items():
                        live_pp[str(k_)] = v_
                live = {x["roster_id"]: (round(x.get("points") or 0, 2) if MANAGED else round(best_lineup(((players_db.get(str(k_)) or {}).get("position"), v_)
                                                          for k_, v_ in (x.get("players_points") or {}).items()), 2)) for x in m}
                if not any(live.values()):
                    live = {}

    # ---------- trades: this season + up to 2 prior seasons (previous_league_id chain)
    team_by_owner = {t["owner"]: t["id"] for t in teams}
    seasons = [(season, lid, rosters)]
    prev_id = league.get("previous_league_id")
    while prev_id and prev_id != "0" and len(seasons) < 3:
        pl_ = get(f"{API}/league/{prev_id}", cache_hours=24 * 7, required=False)
        if not pl_:
            break
        seasons.append((int(pl_.get("season") or season - len(seasons)), prev_id,
                        get(f"{API}/league/{prev_id}/rosters", cache_hours=24 * 7, required=False) or []))
        prev_id = pl_.get("previous_league_id")
    trades, seen_tx = [], set()
    for yr, l_id, l_rosters in seasons:
        own = {r["roster_id"]: r.get("owner_id") for r in l_rosters}
        for rd in range(0, last_week + 2):
            tx = get(f"{API}/league/{l_id}/transactions/{rd}", cache_hours=(None if yr == season else 24 * 30), required=False) or []
            for t in tx:
                if t.get("type") != "trade" or t.get("status") != "complete" or t.get("transaction_id") in seen_tx:
                    continue
                seen_tx.add(t.get("transaction_id"))
                adds = {str(k): v for k, v in (t.get("adds") or {}).items()}
                drops = {str(k): v for k, v in (t.get("drops") or {}).items()}
                sides = {}
                for rid in t.get("roster_ids") or []:
                    sides[rid] = dict(rid=rid, cur=team_by_owner.get(own.get(rid)), got=[], gave=[], pin=[], pout=[], fin=0, fout=0)
                for pid, rid in adds.items():
                    if rid in sides:
                        sides[rid]["got"].append(pid)
                for pid, rid in drops.items():
                    if rid in sides and pid in adds:
                        sides[rid]["gave"].append(pid)
                for dp in t.get("draft_picks") or []:
                    pk = (int(dp["season"]), int(dp["round"]), team_by_owner.get(own.get(dp.get("roster_id"))) or dp.get("roster_id"))
                    if dp.get("owner_id") in sides:
                        sides[dp["owner_id"]]["pin"].append(pk)
                    if dp.get("previous_owner_id") in sides:
                        sides[dp["previous_owner_id"]]["pout"].append(pk)
                for wb in t.get("waiver_budget") or []:
                    if wb.get("receiver") in sides:
                        sides[wb["receiver"]]["fin"] += wb.get("amount", 0)
                    if wb.get("sender") in sides:
                        sides[wb["sender"]]["fout"] += wb.get("amount", 0)
                ts = t.get("status_updated") or t.get("created") or 0
                trades.append(dict(id=t.get("transaction_id"), season=yr, leg=max(rd, int(t.get("leg") or rd)), ts=ts, sides=list(sides.values())))
    trades.sort(key=lambda x: -x["ts"])
    trade_pids = {p for tr in trades if tr["season"] == season for s in tr["sides"] for p in s["got"] + s["gave"]}

    # ---------- rookie drafts (this league's history): every pick, who made it, whose pick it originally was
    my_draft, draft_by = [], {}
    for yr, l_id, l_rosters in seasons:
        own = {r["roster_id"]: r.get("owner_id") for r in l_rosters}
        for dr in get(f"{API}/league/{l_id}/drafts", cache_hours=24, required=False) or []:
            if dr.get("status") != "complete" or int((dr.get("settings") or {}).get("rounds") or 99) > 6:
                continue  # skip startup drafts; rookie drafts only
            dyr = int(dr.get("season") or yr)
            slot_owner = {int(v): k for k, v in (dr.get("draft_order") or {}).items()}
            for pk in get(f"{API}/draft/{dr['draft_id']}/picks", cache_hours=24 * 30, required=False) or []:
                md = pk.get("metadata") or {}
                slot_ = int(pk.get("draft_slot") or 0)
                rec = dict(y=dyr, rd=int(pk.get("round") or 0), slot=slot_, no=pk.get("pick_no"), pid=str(pk.get("player_id") or ""),
                           by=pk.get("picked_by") or own.get(pk.get("roster_id")), orig=slot_owner.get(slot_),
                           n=f"{md.get('first_name', '')} {md.get('last_name', '')}".strip(), p=md.get("position"))
                draft_by[(dyr, rec["rd"], rec["orig"])] = rec
                if rec["by"] == me_owner:
                    my_draft.append(rec)

    # ---------- player universe: everyone rostered + my past weekly rosters + players moved in trades this season + my draft picks
    uni = set(rostered) | {p for l in my_weekly.values() for p in l} | trade_pids | {r["pid"] for r in my_draft if r["pid"]}
    # best free agents by Sleeper's rest-of-season projection (checked against your lineup later)
    fa_pool = sorted((p_ for p_ in proj if p_ not in rostered), key=lambda p_: -sum(v_ for w_, v_ in proj[p_].items() if w_ > done))[:int(CFG.get("fa_pool", 30))]
    uni |= set(fa_pool)

    # ---------- our model
    def g(pid, yr, upto=99):
        return sorted((int(k.split("-")[1]), v) for k, v in pts.get(pid, {}).items()
                      if k.startswith(f"{yr}-") and int(k.split("-")[1]) < upto)

    coef = {}
    for ps in POS:
        X, y = [], []
        for pid, d in pts.items():
            if (players_db.get(pid) or {}).get("position") != ps:
                continue
            for k, v in d.items():
                if k.startswith(f"{season-1}-"):
                    u = use[pid][k]
                    X.append([u[0], u[1], u[2], 1.0]); y.append(v)
        coef[ps] = lstsq(X, y) if len(X) > 30 else [0, 0, 0, 5]

    def val(pid):
        return unified(pid)

    prior_fit = {}
    for ps in POS:
        xs, ys = [], []
        for pid in sorted(uni):
            if pinfo(pid)["p"] != ps:
                continue
            gg = g(pid, season, done + 1)
            if len(gg) >= 3:
                xs.append(val(pid)); ys.append(st.mean(v for _, v in gg))
        prior_fit[ps] = linfit(xs, ys)

    def our_mean(pid, wk, par):
        k, wr, ws, wo = par
        pos = pinfo(pid)["p"]
        if pos not in POS:
            return 0.0
        cur_g = g(pid, season, wk)
        prev = g(pid, season - 1)
        if len(prev) >= 4:
            prior = _mean(v for _, v in prev)
        else:
            a, b = prior_fit[pos]; prior = max(1.0, a * val(pid) + b)
        n = len(cur_g)
        if n == 0:
            return prior
        recent = _mean(v for _, v in cur_g[-4:]); season_avg = _mean(v for _, v in cur_g)
        c = coef[pos]
        opp = _mean(c[0] * use[pid][f"{season}-{w}"][0] + c[1] * use[pid][f"{season}-{w}"][1] + c[2] * use[pid][f"{season}-{w}"][2] + c[3] for w, _ in cur_g[-4:])
        data = (wr * recent + ws * season_avg + wo * opp) / (wr + ws + wo)
        pw = k / (k + n)
        return pw * prior + (1 - pw) * data

    CV = {"QB": 0.42, "RB": 0.55, "WR": 0.62, "TE": 0.65}

    def our_sd(pid, m):
        pos = pinfo(pid)["p"] or "WR"
        vals = [v for _, v in g(pid, season, done + 1)] + [v for _, v in g(pid, season - 1)]
        cv = CV.get(pos, 0.6)
        if len(vals) >= 5:
            s = st.pstdev(vals); n = len(vals); return (n * s + 6 * cv * m) / (n + 6)
        return cv * m

    # backtest weeks 2..done to tune params + blend weight
    rows_by_par = {}
    grid = list(itertools.product([1, 2, 3, 4, 6], [0.2, 0.35, 0.5], [0.1, 0.25, 0.4], [0.2, 0.4, 0.6]))
    bt_pts = [(pid, w, pts[pid][f"{season}-{w}"], proj_sl[pid][w]) for pid in proj_sl for w in range(2, done + 1)
              if f"{season}-{w}" in pts.get(pid, {}) and w in proj_sl[pid]]
    best_par, best_mae = (2, 0.2, 0.1, 0.6), None
    wf = dict(pick="sleeper", n=0)
    if len(bt_pts) >= 100:
        OM = {par: [our_mean(pid, w, par) for pid, w, _, _ in bt_pts] for par in grid}
        # honest test first: every setting chosen from earlier weeks only, scored on the next week
        R_ = [dict(wk=w, a=a, sl=s, es=es_proj.get(pid, {}).get(w), it=it_lines.get((team_of(pid), w)), ev=s >= 3) for pid, w, a, s in bt_pts]
        wf, _, _ = SRC.choose(R_, OM)
        if wf["pick"] in ("sources", "sources+model") and src_rep.get("used"):
            for p_, d_ in proj.items():
                for w in d_:
                    d_[w] = round(SRC.combine(proj_sl[p_][w], es_proj.get(p_, {}).get(w), it_lines.get((team_of(p_), w)), src_par), 2)
        else:
            src_rep["used"] = False
        bt_pts = [(pid, w, a, proj[pid][w]) for pid, w, a, _ in bt_pts]
        best_par = min(OM, key=lambda k: st.mean(abs(r[2] - o) for r, o in zip(bt_pts, OM[k])))
        rows = [(r[2], r[3], o) for r, o in zip(bt_pts, OM[best_par])]
        mae = lambda f: st.mean(abs(a - f(s, o)) for a, s, o in rows)
        bw_err = min((mae(lambda s, o, q=q: q * s + (1 - q) * o), q) for q in [i / 20 for i in range(21)])
        # when a player's recent games disagree a lot with Sleeper (role change, new starter, injury return),
        # test separately how much to trust Sleeper vs. what he has actually been doing
        div_rows = [r_ for r_ in rows if abs(r_[2] - r_[1]) / max(r_[1], 3) > 0.5]
        w_div = bw_err[1]
        if len(div_rows) >= 60:
            w_div = min(((st.mean(abs(a - (q * s + (1 - q) * o)) for a, s, o in div_rows)), q) for q in [i / 20 for i in range(21)])[1]
        backtest = dict(n=len(rows), sleeper=round(mae(lambda s, o: s), 2), ours=round(mae(lambda s, o: o), 2), blend=round(bw_err[0], 2), w=bw_err[1],
                        w_div=w_div, n_div=len(div_rows),
                        blend2=round(mae(lambda s, o: (w_div if abs(o - s) / max(s, 3) > 0.5 else bw_err[1]) * s + (1 - (w_div if abs(o - s) / max(s, 3) > 0.5 else bw_err[1])) * o), 2))
        if wf["pick"] not in ("sleeper+model", "sources+model"):   # the usage model didn't help on unseen weeks
            backtest.update(w=1.0, w_div=1.0)
        backtest["model_used"] = wf["pick"] in ("sleeper+model", "sources+model")
    else:
        backtest = dict(n=len(bt_pts), sleeper=None, ours=None, blend=None, w=0.85, w_div=0.85, n_div=0)
    src_rep["oos"] = wf
    print(f"sources: {src_rep}")
    BW = backtest["w"]; BWD = backtest["w_div"]
    def blend(s, o):
        q = BWD if abs(o - s) / max(s, 3) > 0.5 else BW
        return q * s + (1 - q) * o

    # ---------- calibration: re-play every game so far (best ball, projections as they were that week) with each player's
    # spread scaled up or down, and keep the "noise dial" whose win chances best match what happened (pulled toward 1 until
    # there are enough games to trust it).
    CVP = {"QB": 0.42, "RB": 0.55, "WR": 0.62, "TE": 0.65}
    # Games played this season: (week, pos, actual, projection that week). Used to measure the real shape of weekly scores.
    shape_rows_all = [(w, pinfo(pid)["p"], pts[pid][f"{season}-{w}"], proj[pid][w]) for pid in proj for w in proj[pid]
                      if w <= done and f"{season}-{w}" in pts.get(pid, {}) and pinfo(pid)["p"] in POS]
    # last season's games too (Sleeper's projections then vs. what happened), always "earlier", so still an honest test
    prev_rows = []
    for wk in range(1, 18):
        data = get(f"{API}/projections/nfl/regular/{season - 1}/{wk}", cache_hours=24 * 365, required=False) or {}
        for pid, s_ in data.items():
            pos = (players_db.get(pid) or {}).get("position")
            a_ = pts.get(pid, {}).get(f"{season - 1}-{wk}")
            if pos in POS and isinstance(s_, dict) and a_ is not None:
                m_ = score(s_, pos)
                if m_ > 0.5:
                    prev_rows.append((0, pos, a_, m_))
    print(f"score shape data: {len(shape_rows_all)} games this season, {len(prev_rows)} last season")
    shape_rows_all = prev_rows + shape_rows_all
    def shape_upto(wk, norm=False):
        return fit_shape([(ps, a, m) for w, ps, a, m in shape_rows_all if w < wk], norm=norm)
    # How often a player who still had a projection when the week locked didn't play at all (measured, not assumed):
    # in managed leagues managers bench players ruled out, so for starters this is about zero.
    _bf_n = _bf_miss = 0
    for wk in range(1, done + 1):
        for rid, ids in (all_weekly.get(wk) or {}).items():
            for p in ids:
                if pinfo(p)["p"] in POS and proj.get(p, {}).get(wk, 0) > 0:
                    _bf_n += 1; _bf_miss += 0 if f"{season}-{wk}" in pts.get(p, {}) else 1
    BF_MISS = round(_bf_miss / _bf_n, 3) if _bf_n >= 200 else 0.03
    print(f"replay: {_bf_miss}/{_bf_n} projected {'starters' if MANAGED else 'rostered players'} didn't play -> miss chance {BF_MISS}")
    def backfill(mult, sims=200, detail=None, shape=False, wk_from=1):
        """Re-play past weeks with projections as they were. shape=True: each week uses the real score shape measured
        from EARLIER weeks only (lognormal until there are enough games), so the test is honest."""
        global SHAPE
        keep = SHAPE
        out_ = []
        try:
            for wk in range(wk_from, done + 1):
                SHAPE = shape_upto(wk, norm=(shape == "norm")) if shape else {}
                rows = weeks.get(wk) or []
                draws = {}
                for rid, mid, _ in rows:
                    items = []
                    for p in all_weekly.get(wk, {}).get(rid, []):
                        ps = pinfo(p)["p"]; m_ = proj.get(p, {}).get(wk, 0)
                        if ps in POS and m_ > 0:
                            items.append((ps, m_, CVP.get(ps, 0.6) * mult, p, pinfo(p)["t"]))
                    draws[rid] = [(sum(sim_points(m_, cv_, sid, tm, s_, 900 + wk, BF_MISS, ps) for ps, m_, cv_, sid, tm in items) if MANAGED
                                   else best_lineup((ps, sim_points(m_, cv_, sid, tm, s_, 900 + wk, BF_MISS, ps)) for ps, m_, cv_, sid, tm in items)) for s_ in range(sims)]
                if detail is not None:   # how every team did vs. what was expected before the week (for "your season so far")
                    med_ = st.median([r[2] for r in rows]) if rows else 0
                    for rid, mid, sc_ in rows:
                        if rid not in draws:
                            continue
                        o = next((r for r in rows if r[1] == mid and r[0] != rid), None)
                        xs = sorted(draws[rid])
                        rec_ = dict(wk=wk, rid=rid, sc=round(sc_, 1), exp=round(sum(xs) / len(xs), 1), lo=round(xs[int(0.1 * len(xs))], 1), hi=round(xs[int(0.9 * len(xs))], 1))
                        if o and o[0] in draws:
                            rec_.update(opp=o[0], osc=round(o[2], 1), oexp=round(sum(draws[o[0]]) / len(draws[o[0]]), 1),
                                        wp=round((sum(1 for a, b in zip(draws[rid], draws[o[0]]) if a > b) + 0.5) / (len(xs) + 1), 3))
                        if median_on:
                            rec_["med"] = 1 if sc_ > med_ else 0
                        detail.append(rec_)
                for rid, mid, sc_ in rows:
                    o = next((r for r in rows if r[1] == mid and r[0] != rid), None)
                    if not o or rid > o[0] or rid not in draws or o[0] not in draws:
                        continue
                    out_.append((wk, (sum(1 for a, b in zip(draws[rid], draws[o[0]]) if a > b) + 0.5) / (sims + 1), 1 if sc_ > o[2] else 0))
        finally:
            SHAPE = keep
        return out_

    # Which score shape fits this league better? Replay weeks 3+ each way (shapes learned only from earlier games) and keep
    # a real shape only if team scores land where expected more often without making win chances worse.
    shape_rep = dict(used=False, mode=None)
    try:
        if done >= 3:
            res = {}
            for mode in (None, "norm", "raw"):
                det_ = []
                g_ = backfill(1.0, 200, detail=det_, shape=mode, wk_from=3)
                ll_ = st.mean(-(o * math.log(max(p_, 1e-4)) + (1 - o) * math.log(max(1 - p_, 1e-4))) for _, p_, o in g_) if g_ else None
                res[mode or "lognormal"] = dict(n=len(det_), bias=round(st.mean(r["sc"] - r["exp"] for r in det_), 2),
                                                cover=round(st.mean(1 if r["lo"] <= r["sc"] <= r["hi"] else 0 for r in det_), 3),
                                                mae=round(st.mean(abs(r["sc"] - r["exp"]) for r in det_), 2), logloss=round(ll_, 4) if ll_ else None)
            fit_err = lambda r: abs(r["bias"]) / 4 + abs(r["cover"] - 0.8) * 50   # 4 pts of bias ~ 5 points of coverage miss
            base = res["lognormal"]
            ok = [k for k in ("norm", "raw") if fit_err(res[k]) < fit_err(base) - 0.25 and (res[k]["logloss"] or 0) <= (base["logloss"] or 0) + 0.003]
            pick = min(ok, key=lambda k: fit_err(res[k])) if ok else None
            full = fit_shape([(ps, a, m) for w, ps, a, m in shape_rows_all], norm=(pick == "norm")) if pick else {}
            shape_rep = dict(used=bool(pick and full), mode=pick, test=res, n_games=len(shape_rows_all), weeks=[3, done])
            if shape_rep["used"]:
                SHAPE = full
                shape_rep["cells"] = {k: [len(v[0]), v[1]] for k, v in full.items()}
            print(f"score shape test: {res} -> {pick or 'lognormal'}")
    except Exception as e:  # noqa
        print(f"WARN shape test skipped: {e}")
    try:
        cal = SRC.calibrate(lambda m_, *a, **k: backfill(m_, *a, shape=shape_rep["mode"], **k)) if done else dict(m=1.0, n=0)
    except Exception as e:  # noqa
        print(f"WARN calibration skipped: {e}"); cal = dict(m=1.0, n=0)
    CAL_M = cal["m"]
    print(f"calibration: {cal}")
    past_all = []
    try:
        if done:
            backfill(CAL_M, 300, detail=past_all, shape=shape_rep["mode"])
    except Exception as e:  # noqa
        print(f"WARN past weeks skipped: {e}")
    past_me = [r for r in past_all if r["rid"] == me]
    past_bias = round(st.mean(r["sc"] - r["exp"] for r in past_all), 1) if past_all else None

    # ---------- per-player record
    P = {}
    for pid in sorted(uni):
        info = pinfo(pid)
        if info["p"] not in POS:
            continue
        team = info["t"]
        o = our_mean(pid, done + 1, best_par)
        out_now = info["inj"] in ("Out", "IR", "PUP", "Sus", "Doubtful")
        sl, ou, pr = [], [], []
        for wk in fut:
            spv = proj.get(pid, {}).get(wk)
            avail = spv is not None and spv > 0  # Sleeper omits players on bye / not expected to play
            if wk == fut[0] and out_now:
                avail = False
            if info["inj"] in ("IR", "PUP", "Sus") and wk < fut[0] + 4:  # multi-week designations
                avail = False
            if not avail:
                sl.append(0); ou.append(0); pr.append(0); continue
            sl.append(round(spv, 2)); ou.append(round(o, 2)); pr.append(round(blend(spv, o), 2))
        bye = None
        prev25 = g(pid, season - 1)
        P[pid] = dict(n=info["n"], p=info["p"], t=team, age=info["age"], inj=info["inj"], r=rostered.get(pid, 0),
                      ktc=ktc["players"].get(pid, 0), fc=fc_vals.get(pid, 0), val=unified(pid),
                      a=[pts.get(pid, {}).get(f"{season}-{w}") for w in range(1, done + 1)],
                      a25=round(st.mean(v for _, v in prev25), 1) if prev25 else None,
                      sl=sl, ou=ou, pr=pr, sd=round(our_sd(pid, max(o, 1)) * CAL_M, 2),
                      u=[use.get(pid, {}).get(f"{season}-{w}") for w in range(1, done + 1)])
        dbp = players_db.get(pid) or {}
        if info["inj"]:
            P[pid]["injd"] = {k_: dbp.get(k_) for k_ in ("injury_body_part", "injury_notes", "practice_participation", "injury_start_date") if dbp.get(k_)}
        if pid not in rostered:
            P[pid]["gone"] = 1
            if pid in fa_pool:
                P[pid]["fa"] = 1
    # bye week per player from projections gaps on team level
    team_byes = {}
    # use the player database's bye info if present, else infer from team-wide zero weeks
    tw = {}
    for pid, d in P.items():
        if not d["t"] or d.get("gone"):
            continue
        for i, wk in enumerate(fut):
            tw.setdefault(d["t"], {}).setdefault(wk, []).append(d["sl"][i] > 0)
    for t, wmap in tw.items():
        for wk, flags in wmap.items():
            if len(flags) >= 2 and not any(flags):
                team_byes[t] = wk
    sch = get(f"https://api.sleeper.com/schedule/nfl/regular/{season}", required=False)  # fresh: game status drives live locks
    bye_src = "inferred from projections"
    if isinstance(sch, list) and sch and isinstance(sch[0], dict) and "week" in sch[0]:
        played = {}
        for gm in sch:
            for side in ("home", "away"):
                if gm.get(side):
                    played.setdefault(gm[side], set()).add(int(gm["week"]))
        found = 0
        for tm, wks in played.items():
            missing = [w for w in range(1, 19) if w not in wks]
            if len(missing) == 1:
                team_byes[tm] = missing[0]; found += 1
        unknown = sorted({d["t"] for d in P.values() if d["t"] and not d.get("gone")} - set(played))
        if unknown:
            print(f"WARN team codes not in NFL schedule (kept inferred byes): {unknown}")
        bye_src = "NFL schedule" if found else bye_src
    for d in P.values():
        d["bye"] = team_byes.get(d["t"])

    # ---------- matchups: how generous each NFL defense is to each position, adjusted for the offenses it faced
    opp_tw = {}
    if isinstance(sch, list):
        for gm in sch:
            if isinstance(gm, dict) and gm.get("home") and gm.get("away") and str(gm.get("week", "")).isdigit():
                w_ = int(gm["week"]); opp_tw[(gm["home"], w_)] = gm["away"]; opp_tw[(gm["away"], w_)] = gm["home"]
    tp = {}  # (team, pos, wk) -> points that offense's players at that position scored (QB: the top QB)
    for pid_, d_ in pts.items():
        inf_ = players_db.get(pid_) or {}
        ps_, tm_ = inf_.get("position"), inf_.get("team")
        if ps_ not in POS or not tm_:
            continue
        for wk in range(1, done + 1):
            v_ = d_.get(f"{season}-{wk}")
            if v_ is None:
                continue
            k_ = (tm_, ps_, wk)
            tp[k_] = max(tp.get(k_, 0.0), v_) if ps_ == "QB" else tp.get(k_, 0.0) + v_
    off_avg = {}
    for (tm_, ps_, wk), v_ in tp.items():
        off_avg.setdefault((tm_, ps_), []).append(v_)
    off_avg = {k_: sum(v) / len(v) for k_, v in off_avg.items() if v}
    ratios = {}
    for (tm_, ps_, wk), v_ in tp.items():
        d_ = opp_tw.get((tm_, wk)); o_ = off_avg.get((tm_, ps_))
        if d_ and o_ and o_ > 1:
            ratios.setdefault((d_, ps_), []).append(v_ / o_)
    DEF = {}
    for (d_, ps_), r_ in ratios.items():
        n_ = len(r_); f_ = (sum(r_) + 4 * 1.0) / (n_ + 4)  # shrink toward average: early-season samples are small
        DEF.setdefault(d_, {})[ps_] = round(min(1.3, max(0.75, f_)), 3)
    def mu_of(team, pos):
        """playoff-week opponents and how generous they are to this position (1.0 = average)"""
        out = []
        for wk in fut:
            if wk <= reg_end:
                continue
            o_ = opp_tw.get((team, wk))
            out.append([wk, o_ or "", (DEF.get(o_, {}) or {}).get(pos, 1.0) if o_ else None])
        return out if any(x[1] for x in out) else []
    n_mu = 0
    for pid_, d_ in P.items():
        if not d_.get("t"):
            continue
        mu = mu_of(d_["t"], d_["p"])
        if mu:
            d_["mu"] = mu
            # a modest nudge to playoff-week projections (Sleeper's far-out projections only lightly reflect opponents)
            for wk, o_, f_ in mu:
                if f_ and wk in fut:
                    k_ = fut.index(wk)
                    d_["pr"][k_] = round(d_["pr"][k_] * min(1.10, max(0.90, f_ ** 0.5)), 2)
            n_mu += 1
    print(f"matchups: defense factors for {len(DEF)} teams, playoff matchups for {n_mu} players")

    # ---------- simulation helpers
    for t in teams:
        for pid in t["taxi"]:
            if pid in P:
                P[pid]["slot"] = "taxi"
        for pid in t["ir"]:
            if pid in P:
                P[pid]["slot"] = "ir"

    def roster_ids(rid):
        """Players who can score for rid: active roster only (taxi and IR players don't count in best ball)."""
        return [p for p in P if P[p]["r"] == rid and not P[p].get("gone") and not P[p].get("slot")]

    def det_weeks(ids):
        return [best_lineup((P[p]["p"], P[p]["pr"][k]) for p in ids) for k in range(len(fut))]

    # ---------- simulation: every rostered player's score in every simulated week, drawn once and reused everywhere
    SIMS = int(CFG.get("season_sims", 600))
    NW = len(fut) or 1
    def cv_of(d):
        base = max(next((x for x in d["ou"] if x > 0), 1), 1)
        return min(1.2, max(0.25, d["sd"] / base))
    # ---------- games already played this week: lock in real points instead of simulating them
    game_state = {}
    if fut and live_pp and isinstance(sch, list):
        for gm in sch:
            if isinstance(gm, dict) and str(gm.get("week")) == str(fut[0]):
                for side in ("home", "away"):
                    if gm.get(side):
                        game_state[gm[side]] = gm.get("status") or ""
    clock = {}
    if fut and live_pp:
        try:
            clock = SRC.game_clock(season, fut[0], lambda u, cache_hours=None: get(u, cache_hours=cache_hours, required=False))
        except Exception as e:  # noqa
            print(f"WARN game clock unavailable: {e}")
    n_lock = 0
    for pid, d in P.items():
        st_ = game_state.get(d.get("t")); left = clock.get(d.get("t"))
        if pid in live_pp and (st_ in ("complete", "in_progress") or (left is not None and left < 1)):
            # finished: the real score. In progress: real score so far plus the share of the game still to play
            # (from the quarter and clock; half the game if the clock isn't available)
            frac = 0 if (st_ == "complete" or left == 0) else (left if left is not None and left < 1 else 0.5)
            d["lk"] = [round(live_pp[pid], 2), round(frac, 3)]
            n_lock += 1
    if live_pp:
        print(f"live week {fut[0] if fut else '?'}: {n_lock} players locked to real scores ({len(game_state)} teams with status)")
    def wk0(d, s_, cv, pid):
        lk = d.get("lk")
        if not lk:
            return None
        return lk[0] + (sim_points(d["pr"][0] * lk[1], cv, pid, d["t"], s_, fut[0], 0, d["p"]) if lk[1] > 0 else 0.0)

    VAL = {}
    sim_ids = [p for p in P if (P[p]["r"] and not P[p].get("gone")) or P[p].get("fa")]
    for pid in sim_ids:
        d = P[pid]; cv = cv_of(d); d["cv"] = round(cv, 3)
        rows = []
        a_ = int(pid) if str(pid).isdigit() else 7
        for s_ in range(SIMS):
            row = []
            ab = absences(a_, s_, fut)
            for k, wk in enumerate(fut):
                if k == 0 and d.get("lk"):
                    row.append(wk0(d, s_, cv, pid)); continue
                if ab[k]:
                    row.append(0.0); continue
                pm = P_MISS_STATUS.get(d["inj"], P_MISS) if k == 0 else 0
                row.append(sim_points(d["pr"][k], cv, pid, d["t"], s_, wk, pm, d["p"]))
            rows.append(row)
        VAL[pid] = rows

    # ---------- boom weeks: best ball keeps a player's big games and drops his duds, so how OFTEN he booms matters.
    # A "boom" is a top-k week at his position (k = 3 per starting spot, e.g. a top-3 TE or a top-9 WR week in a 3-WR league):
    # the threshold is the k-th best score at the position each week this season, averaged.
    boom_thr = {}
    for ps in POS:
        kk = max(3, 3 * LU_N.get(ps, 1))
        per = []
        for wk in range(1, done + 1):
            sc_ = sorted((v.get(f"{season}-{wk}") for pid_, v in pts.items() if (players_db.get(pid_) or {}).get("position") == ps
                          and v.get(f"{season}-{wk}") is not None), reverse=True)
            if len(sc_) >= kk:
                per.append(sc_[kk - 1])
        boom_thr[ps] = [round(st.mean(per), 1) if per else None, kk]
    def lognorm_tail(m, cv, t):
        if m <= 0 or not t:
            return 0.0
        sig = math.sqrt(math.log(1 + cv * cv)); mu = math.log(m) - sig * sig / 2
        return 1 - 0.5 * (1 + math.erf((math.log(t) - mu) / (sig * math.sqrt(2))))
    for pid, d in P.items():
        thr = (boom_thr.get(d["p"]) or [None])[0]
        if not thr or pid not in VAL:
            continue
        ks_ = [k for k in range(len(fut)) if d["pr"][k] > 0 and not (k == 0 and live_pp)]   # rest of season, games not yet under way
        sim_rate = (sum(1 for s_ in range(SIMS) for k in ks_ if VAL[pid][s_][k] >= thr) / (SIMS * len(ks_))) if ks_ else None
        played = [v for v in d["a"] if v is not None]
        d["boom"] = [round(sim_rate, 3) if sim_rate is not None else None, round(sum(1 for v in played if v >= thr) / len(played), 3) if played else None, len(played)]
    boom_pos = {}
    for ps in POS:
        n_st = 12 * (LU_N.get(ps, 1) + (1 if ps in ("RB", "WR") else 0))
        top = sorted((d for d in P.values() if d["p"] == ps and d.get("boom") and d["boom"][0] is not None), key=lambda d: -sum(d["pr"]))[:n_st]
        boom_pos[ps] = round(st.mean(d["boom"][0] for d in top), 3) if top else None

    # honest test of the boom chances: replay this season's weeks using only what was known before each game
    # (projection that week, spread from earlier games), and compare with how often big weeks actually happened.
    # Also compare against giving every player his position's typical spread, to see if player-specific spreads help.
    bt_boom = []
    for pid_, d_ in pts.items():
        ps_ = (players_db.get(pid_) or {}).get("position")
        thr = (boom_thr.get(ps_) or [None])[0]
        if ps_ not in POS or not thr:
            continue
        for wk in range(2, done + 1):
            a_ = d_.get(f"{season}-{wk}"); m_ = (proj_sl.get(pid_) or {}).get(wk)
            if a_ is None or not m_ or m_ < 3:
                continue
            hist_ = [v for w_, v in g(pid_, season, wk)] + [v for _, v in g(pid_, season - 1)]
            cvp = CV.get(ps_, 0.6)
            if len(hist_) >= 5:
                sd_ = (len(hist_) * st.pstdev(hist_) + 6 * cvp * m_) / (len(hist_) + 6); cv_ = min(1.2, max(0.25, sd_ / max(m_, 1)))
            else:
                cv_ = cvp
            tail_ = lambda m__, c__, t__: (lambda v__: lognorm_tail(m__, c__, t__) if v__ is None else v__)(shape_tail(ps_, m__, c__, t__))
            bt_boom.append((tail_(m_, cv_ * CAL_M, thr) * 0.97, tail_(m_, cvp * CAL_M, thr) * 0.97, 1 if a_ >= thr else 0, ps_))
    boom_test = dict(n=len(bt_boom), thr={k: v for k, v in boom_thr.items()}, pos_avg=boom_pos)
    if len(bt_boom) >= 200:
        ll = lambda i: st.mean(-(o * math.log(max(r[i], 1e-4)) + (1 - o) * math.log(max(1 - r[i], 1e-4))) for r in bt_boom for o in [r[2]])
        bins = []
        for lo, hi in ((0, 0.05), (0.05, 0.12), (0.12, 0.25), (0.25, 0.45), (0.45, 1.01)):
            b_ = [r for r in bt_boom if lo <= r[0] < hi]
            if b_:
                bins.append([lo, hi, len(b_), round(st.mean(r[0] for r in b_), 3), round(st.mean(r[2] for r in b_), 3)])
        boom_test.update(bins=bins, predicted=round(st.mean(r[0] for r in bt_boom), 3), actual=round(st.mean(r[2] for r in bt_boom), 3),
                         ll_player=round(ll(0), 4), ll_pos=round(ll(1), 4))
    print(f"boom: thresholds {boom_thr}; test {({k: v for k, v in boom_test.items() if k in ('n', 'predicted', 'actual', 'll_player', 'll_pos')})}")
    _ST = {}
    def starters(ids, k):
        """Managed lineups: who a manager starts in week k, chosen before the games by projection."""
        key = (tuple(ids), k)
        if key not in _ST:
            _ST[key] = lineup_split([(P[p]["p"], P[p]["pr"][k], p) for p in ids if p in VAL])[2]
        return _ST[key]
    def lineup(ids, s_, k):
        if MANAGED:
            return sum(VAL[p][s_][k] for p in starters(ids, k))
        return best_lineup((P[p]["p"], VAL[p][s_][k]) for p in ids if p in VAL)

    # ---------- standings so far
    rec = {t["id"]: [0, 0, 0.0] for t in teams}
    rec_h = {t["id"]: [0, 0] for t in teams}; rec_m = {t["id"]: [0, 0] for t in teams}
    for wk, rows in weeks.items():
        scores = [r[2] for r in rows]
        med = st.median(scores) if scores else 0
        for rid, mid, sc_ in rows:
            opp = next((r[2] for r in rows if r[1] == mid and r[0] != rid), None)
            rec[rid][2] += sc_
            if opp is not None:
                rec[rid][0 if sc_ > opp else 1] += 1; rec_h[rid][0 if sc_ > opp else 1] += 1
            if median_on:
                rec[rid][0 if sc_ > med else 1] += 1; rec_m[rid][0 if sc_ > med else 1] += 1

    ids_by = {t["id"]: roster_ids(t["id"]) for t in teams}
    rem = [wk for wk in fut if wk <= reg_end]
    po_k = [fut.index(wk) for wk in fut if wk > reg_end]
    this_week = fut[0] if fut else None

    def run_season(ids_map, adj_w=None, adj_pf=None):
        """Play out the rest of the season SIMS times. Draft order = reverse of final regular-season standings (wins, then points)."""
        T = list(ids_map)
        res = {r: dict(playoffs=0, bye=0, title=0, xw=0.0, slot=0.0, wp=0, seed=[0] * len(T), opp1={}, title_bye=0, title_nobye=0) for r in T}
        for s_ in range(SIMS):
            W = {r: rec[r][0] + (adj_w or {}).get(r, 0) for r in T}; PF = {r: rec[r][2] + (adj_pf or {}).get(r, 0) for r in T}
            cache = {}
            def sc(r, k):
                key = (r, k)
                if key not in cache:
                    cache[key] = lineup(ids_map[r], s_, k)
                return cache[key]
            for wk in rem:
                k = fut.index(wk)
                vals = {r: sc(r, k) for r in T}
                med = st.median(vals.values())
                for r, mid in sched.get(wk, []):
                    if r not in vals:
                        continue
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
                res[r]["slot"] += len(T) - i          # worst record picks first
            seeds = order[:n_playoff]
            for i, r in enumerate(seeds):
                res[r]["playoffs"] += 1
                if i < N_BYE:
                    res[r]["bye"] += 1
            def psc(r, i):
                """Playoff round i score: the real result if that week is played, otherwise simulated."""
                wk_ = reg_end + 1 + i
                if wk_ in po_actual:
                    return po_actual[wk_].get(r, 0.0)
                if wk_ in fut:
                    return sc(r, fut.index(wk_))
                return 0.0
            if len(seeds) >= 2 and len(po_k) + len(po_actual) >= N_ROUNDS:
                champ, first = bracket(seeds, lambda x, y, i: x if psc(x, i) >= psc(y, i) else y)
                for x, y in first.items():
                    res[x]["opp1"][y] = res[x]["opp1"].get(y, 0) + 1
                res[champ]["title"] += 1
                res[champ]["title_bye" if champ in seeds[:N_BYE] else "title_nobye"] += 1
        for r in T:
            for k2 in ("playoffs", "bye", "title", "wp", "title_bye", "title_nobye"):
                res[r][k2] = res[r][k2] / SIMS
            res[r]["xw"] /= SIMS; res[r]["slot"] /= SIMS
            res[r]["seed"] = [round(x / SIMS, 3) for x in res[r]["seed"]]
            res[r]["opp1"] = {o: round(c / SIMS, 3) for o, c in res[r]["opp1"].items()}
        return res

    S0 = run_season(ids_by)
    odds = {r: dict(playoffs=round(S0[r]["playoffs"], 3), bye=round(S0[r]["bye"], 3), title=round(S0[r]["title"], 3)) for r in S0}
    exp_wins = {r: S0[r]["xw"] for r in S0}; slot = {r: S0[r]["slot"] for r in S0}
    wp = {r: S0[r]["wp"] * SIMS for r in S0}
    title_se = {r: round(math.sqrt(max(odds[r]["title"] * (1 - odds[r]["title"]), 1e-4) / SIMS), 3) for r in S0}

    # weekly score means from the same draws
    wk_mean = {t["id"]: [sum(lineup(ids_by[t["id"]], s_, k) for s_ in range(SIMS)) / SIMS for k in range(len(fut))] for t in teams}
    ros_ppg = {r: round(st.mean([x for x in v if x > 0]) if any(x > 0 for x in v) else 0, 1) for r, v in wk_mean.items()}
    po_ppg = {r: round(st.mean([v[k] for k in po_k]) if po_k else 0, 1) for r, v in wk_mean.items()}
    proj_this = {r: round(v[0], 1) for r, v in wk_mean.items()} if fut else {}
    proj_range = {}
    if fut:
        for t in teams:
            xs = sorted(lineup(ids_by[t["id"]], s_, 0) for s_ in range(SIMS))
            proj_range[t["id"]] = [round(xs[int(0.1 * SIMS)], 1), round(xs[int(0.9 * SIMS)], 1)]

    # ---------- managed leagues: who to start this week. Best lineup by projection, your lineup as set in Sleeper, and the
    # closest calls on your bench, each with the win chance against your opponent's lineup (same draws as everything else).
    lineup_adv = None
    if MANAGED and fut:
        try:
            k0 = 0
            mine_ids = ids_by[me]
            mid_ = next((m_ for r_, m_ in sched.get(fut[0], []) if r_ == me), None)
            opp_ = next((r_ for r_, m_ in sched.get(fut[0], []) if m_ == mid_ and r_ != me), None) if mid_ is not None else None
            opp_ids = [p for p in cur_starters.get(opp_, []) if p in VAL] if opp_ is not None else []
            opp_set = bool(opp_ids) and len(opp_ids) >= sum(LU_N.values()) + LU_FLEX + LU_SF - 1
            if opp_ is not None and not opp_set:
                opp_ids = starters(ids_by[opp_], k0)
            def wp_of(ids):
                if opp_ is None:
                    return None
                return round(sum(1 for s_ in range(SIMS) if sum(VAL[p][s_][k0] for p in ids) > sum(VAL[p][s_][k0] for p in opp_ids)) / SIMS, 3)
            best_ids = starters(mine_ids, k0)
            mine_cur = [p for p in cur_starters.get(me, []) if p in VAL]
            locked = lambda p: bool(P[p].get("lk"))
            close = []
            keep_ = set(mine_cur or best_ids)   # starters whose games already started stay in the lineup
            for b in mine_ids:
                if b in best_ids or locked(b) or P[b]["pr"][k0] <= 0:
                    continue
                forced = lineup_split([(P[p]["p"], (9999 if (locked(p) and p in keep_) else 999 if p == b else P[p]["pr"][k0]), p) for p in mine_ids if p in VAL])[2]
                out_ = [p for p in best_ids if p not in forced]
                if not out_ or any(locked(p) for p in out_):
                    continue
                w_ = wp_of(forced)
                close.append(dict(start=b, sit=out_[0], dproj=round(P[b]["pr"][k0] - P[out_[0]]["pr"][k0], 1), wp=w_))
            close.sort(key=lambda x: -x["dproj"])
            lineup_adv = dict(wk=fut[0], opp=opp_, opp_set=opp_set, best=best_ids, best_wp=wp_of(best_ids), best_proj=round(sum(P[p]["pr"][k0] for p in best_ids), 1),
                              cur=mine_cur, cur_wp=wp_of(mine_cur) if len(mine_cur) == len(best_ids) else None,
                              cur_proj=round(sum(P[p]["pr"][k0] for p in mine_cur), 1) if mine_cur else None,
                              changes=[dict(start=a, sit=b) for a, b in zip([p for p in best_ids if p not in mine_cur], [p for p in mine_cur if p not in best_ids])] if mine_cur else [],
                              close=close[:4])
            print(f"lineup: best {lineup_adv['best_proj']} ({lineup_adv['best_wp']}), set in Sleeper {lineup_adv['cur_proj']} ({lineup_adv['cur_wp']}), {len(lineup_adv['changes'])} changes")
        except Exception as e:  # noqa
            print(f"WARN lineup advice skipped: {e}")

    # ---------- what each player adds, from simulation (counts bye/injury cover and boom weeks, which best ball rewards)
    IMP_S = min(SIMS, int(CFG.get("impact_sims", 200)))
    for t in teams:
        ids = ids_by[t["id"]]; acc = {p: 0.0 for p in ids}; acc_po = {p: 0.0 for p in ids}
        for s_ in range(IMP_S):
            for k in range(len(fut)):
                if MANAGED:
                    full, used = lineup(ids, s_, k), starters(ids, k)
                else:
                    full, _, used = lineup_split([(P[p]["p"], VAL[p][s_][k], p) for p in ids if p in VAL])
                for c in used:
                    d_ = full - (lineup([p for p in ids if p != c], s_, k) if MANAGED else best_lineup((P[p]["p"], VAL[p][s_][k]) for p in ids if p in VAL and p != c))
                    acc[c] += d_
                    if k in po_k:
                        acc_po[c] += d_
        for p in ids:
            P[p]["imp"] = round(acc[p] / (IMP_S * NW), 2) if fut else 0
            P[p]["imp_po"] = round(acc_po[p] / (IMP_S * len(po_k)), 2) if po_k else 0
    def mc_total(ids, sims=IMP_S):
        return sum(lineup(ids, s_, k) for s_ in range(sims) for k in range(len(fut))) / sims if fut else 0
    NPO = len(po_k)
    _mcp = {}
    def mc_pair(ids, sims=IMP_S):
        """(total points over every week left, total over the playoff weeks only), averaged over the simulations"""
        key = tuple(ids)
        if key in _mcp:
            return _mcp[key]
        if not fut:
            return (0.0, 0.0)
        tot = po = 0.0; pos_ = set(po_k)
        for s_ in range(sims):
            for k in range(len(fut)):
                v = lineup(ids, s_, k); tot += v
                if k in pos_:
                    po += v
        r = (tot / sims, po / sims); _mcp[key] = r
        return r
    def gain2(before, after):
        a, b = mc_pair(before), mc_pair(after)
        return (b[0] - a[0]) / NW, ((b[1] - a[1]) / NPO if NPO else None)
    _mcb = {}
    def mc_add(ids, extra):
        if not fut:
            return 0
        key = tuple(ids)
        if key not in _mcb:
            _mcb[key] = mc_total(ids)
        return (mc_total(ids + [extra]) - _mcb[key]) / NW

    # ---------- my roster board, needs, trade ideas
    base = {t["id"]: det_weeks(ids_by[t["id"]]) for t in teams}
    board = []
    for pid in ids_by[me]:
        lost, cnt = 0.0, 0
        for wk, ids in my_weekly.items():
            vals = lambda ex=None: best_lineup((P[i]["p"], (P[i]["a"][wk - 1] or 0)) for i in ids if i in P and i != ex)
            if pid in ids:
                d = vals() - vals(pid); lost += d; cnt += d > 1e-3
        board.append(dict(id=pid, past=round(lost, 1), cnt=cnt, ros=P[pid].get("imp", 0), po=P[pid].get("imp_po", 0)))
    needs = {}
    for t in teams:
        needs[t["id"]] = {}
        for ps in POS:
            P["__x"] = dict(p=ps, pr=[12.0] * NW)
            needs[t["id"]][ps] = round((sum(det_weeks(ids_by[t["id"]] + ["__x"])) - sum(base[t["id"]])) / NW, 2)
        del P["__x"]
    def v_of(p):
        return P[p].get("val") or 0
    sugg = {}
    mk = ids_by[me]; bm = sum(base[me])
    for t in teams:
        if t["id"] == me:
            continue
        tids = ids_by[t["id"]]; bt = sum(base[t["id"]]); out = []
        for give in [(x,) for x in mk] + list(itertools.combinations(mk, 2)):
            gv = sum(v_of(x) for x in give)
            for got in tids:
                rv = v_of(got)
                if not rv or gv < 0.92 * rv or gv > 1.30 * rv:
                    continue
                nm = [i for i in mk if i not in give] + [got]; nt = [i for i in tids if i != got] + list(give)
                dwm = det_weeks(nm); dm = (sum(dwm) - bm) / NW; dt = (sum(det_weeks(nt)) - bt) / NW
                dmp = (sum(dwm[k] for k in po_k) - sum(base[me][k] for k in po_k)) / NPO if NPO else -99
                if (dm >= 0.8 or dmp >= 0.8) and dt >= -1.0:
                    out.append(dict(give=list(give), get=[got], dm=round(dm, 2), dm_po=round(dmp, 2), dt=round(dt, 2), kg=gv, kr=rv))
        cand = []
        for key_, n_ in (("dm", 7), ("dm_po", 5)):  # best for the whole season, and best for the playoff weeks
            seen = {o["get"][0] for o in cand}; k_ = 0
            for o in sorted(out, key=lambda x: -x[key_]):
                if o["get"][0] in seen:
                    continue
                seen.add(o["get"][0]); cand.append(o); k_ += 1
                if k_ == n_:
                    break
        top = []
        for o in cand:
            nm = [i for i in mk if i not in o["give"]] + o["get"]; nt = [i for i in tids if i not in o["get"]] + o["give"]
            g_, gp_ = gain2(mk, nm); o["dm"] = round(g_, 2); o["dm_po"] = round(gp_, 2) if gp_ is not None else None
            o["dt"] = round(gain2(tids, nt)[0], 2)
            if (o["dm"] >= 1.0 or (o["dm_po"] or 0) >= 1.0) and o["dt"] >= -1.5:
                top.append(o)
        top.sort(key=lambda x: -x["dm"])
        keep = top[:4] + [o for o in sorted(top[4:], key=lambda x: -(x["dm_po"] or 0))[:2]]
        sugg[t["id"]] = keep
    # free agents: what each would add to your lineup (best ball counts byes, injuries and boom weeks)
    # who to drop: least useful to the lineup AND not worth much in trade (never suggest cutting a valuable dynasty asset)
    cheap = [p_ for p_ in ids_by[me] if (P[p_].get("val") or 0) < 1800]
    cut = sorted(cheap or ids_by[me], key=lambda p_: (P[p_].get("imp", 0) + (P[p_].get("val") or 0) / 2000, P[p_].get("val") or 0))[:3]
    roster_cap = int(settings.get("roster_size") or 0) or len([s_ for s_ in (league.get("roster_positions") or []) if s_ not in ("IR", "TAXI")]) or 28
    def net_add2(pid):
        """(points a week gained over the rest of the season, over the playoff weeks) by adding him,
        after cutting your least useful player if the roster is full."""
        ids = ids_by[me]
        after = ids + [pid]
        if len(ids) >= roster_cap and cut:
            c_ = next((x for x in cut if x != pid), None)
            if c_:
                after = [i for i in ids if i != c_] + [pid]
        return gain2(ids, after)
    def net_add(pid):
        return net_add2(pid)[0]
    fa = []
    for pid in [p for p in P if P[p].get("fa")]:
        add, addp = net_add2(pid)
        if add >= 0.3 or (addp or 0) >= 0.3:
            fa.append(dict(id=pid, add=round(add, 2), add_po=round(addp, 2) if addp is not None else None, val=P[pid].get("val") or 0))
    fa = sorted(fa, key=lambda x: -x["add"])[:8] + [f_ for f_ in sorted(fa, key=lambda x: -(x["add_po"] or 0))[:8] if f_ not in sorted(fa, key=lambda x: -x["add"])[:8]]
    # title-odds check on the single best idea per team (same draws, so the change is the trade, not noise)
    best_ideas = sorted(((t_id, o) for t_id, L in sugg.items() for o in L[:1]), key=lambda x: -x[1]["dm"])[:int(CFG.get("idea_title_checks", 6))]
    for t_id, o in best_ideas:
        alt = dict(ids_by); alt[me] = [i for i in mk if i not in o["give"]] + o["get"]; alt[int(t_id)] = [i for i in ids_by[int(t_id)] if i not in o["get"]] + o["give"]
        Sx = run_season(alt)
        o["title0"] = round(S0[me]["title"], 3); o["title1"] = round(Sx[me]["title"], 3)

    names = {t["id"]: t["name"] for t in teams}
    nT = len(teams)

    # ---------- picks (next 3 rookie drafts) with projected slots for next year's draft
    PV = ktc.get("picks") or {}
    def pick_base(y, rd):
        f = conv(fc_picks.get((y, rd), 0)); k_ = PV.get(f"{y}-{rd}")
        if f and k_:
            # the all-players curve runs a little hot at the low end (late-round picks), so average it with KTC's own pick price
            return 0.5 * f + 0.5 * k_
        return f or k_ or {1: 5000, 2: 3200, 3: 2300, 4: 1600}.get(rd, 1000)
    def pick_val(y, rd, orig=None):
        v = pick_base(y, rd)
        if y == season + 1 and orig in slot:  # early / mid / late: FantasyCalc's own price for that tier when it has one
            tier = "Early" if slot[orig] <= nT / 3 else "Late" if slot[orig] > 2 * nT / 3 else "Mid"
            ft = conv(fc_tier.get((y, rd, tier), 0)); fg = conv(fc_picks.get((y, rd), 0))
            v = v * (ft / fg) if ft and fg else v * (1.15 if tier == "Early" else 0.87 if tier == "Late" else 1.0)
        return round(v)
    years = [season + 1, season + 2, season + 3]
    owner = {(y, rd, t["id"]): t["id"] for y in years for rd in range(1, int(settings.get("draft_rounds", 4)) + 1) for t in teams}
    for tp in traded:
        k = (int(tp["season"]), int(tp["round"]), tp["roster_id"])
        if k in owner:
            owner[k] = tp["owner_id"]
    picks = []
    for (y, rd, o), n in owner.items():
        s_ = round(slot[o]) if y == season + 1 else None
        lab = f"{y} {ORD[rd]} ({names[o]})" + (f" · proj {rd}.{s_:02d}" if s_ else "")
        picks.append(dict(y=y, rd=rd, o=o, own=n, v=pick_val(y, rd, o), slot=round(slot[o], 1) if y == season + 1 else None, label=lab))

    def pval(pid):
        return unified(pid)
    def pk_val(pk):
        y, rd, o = pk
        return pick_val(y if y > season else season + 1, rd, o if y == season + 1 else None)
    def pk_label(pk):
        y, rd, o = pk
        return f"{y} {ORD[rd] if rd < len(ORD) else rd} ({names.get(o, 'former team')})"
    def pname(pid):
        return (P.get(pid) or {}).get("n") or pinfo(pid)["n"]

    # ---------- what each traded asset is worth TODAY, wherever it ended up.
    # A player nobody in the league rosters any more counts as 0 (if no team wants him, he isn't worth anything here).
    # A pick that has been used counts as the player it became; an unused pick counts at today's pick price.
    rost_team = {p_: rid for rid, l_ in ids_by.items() for p_ in l_}
    owner_of = {t["id"]: t["owner"] for t in teams}
    def now_player(pid):
        rid = rost_team.get(pid)
        if rid is None:
            return 0, "not on any roster now (counts as 0)"
        return pval(pid), ("still on your team" if rid == me else f"now on {names.get(rid, 'another team')}")
    def now_pick(pk):
        y, rd, o = pk
        if y <= season:
            r = draft_by.get((y, rd, owner_of.get(o, o)))
            if r and r.get("pid"):
                v_, st_ = now_player(r["pid"])
                return v_, f"became {pname(r['pid']) or r['n']}, {st_}"
            return pk_val(pk), "drafted pick (player not found)"
        return pk_val(pk), "pick not used yet"
    def items_now(pids, picks_, faab):
        out = []
        for p_ in pids:
            v_, st_ = now_player(p_); out.append([pname(p_), v_, st_])
        for k_ in picks_:
            v_, st_ = now_pick(k_); out.append([pk_label(k_), v_, st_])
        if faab:
            out.append([f"${faab} FAAB", 0, ""])
        return out

    # the hand-built trade history (docs/ledger.json, older trades) is stored by name: re-price it at today's values too
    name_pid = {}
    for pid_, d_ in (players_db or {}).items():
        if d_.get("position") in POS and d_.get("full_name"):
            k_ = re.sub(r"[^a-z]", "", d_["full_name"].lower())
            if k_ not in name_pid or pid_ in P:
                name_pid[k_] = pid_
    for pid_, d_ in P.items():
        name_pid[re.sub(r"[^a-z]", "", (d_.get("n") or "").lower())] = pid_
    tname = {re.sub(r"[^a-z]", "", n_.lower()): t_ for t_, n_ in names.items()}
    def ledger_item(label):
        lab = label.strip()
        if "FAAB" in lab:
            return [lab, 0, ""]
        m_ = re.match(r"(\d{4})\s+(\d)(?:st|nd|rd|th)\s*\((.*)\)$", lab)
        if m_:
            y_, rd_, note = int(m_.group(1)), int(m_.group(2)), m_.group(3)
            b_ = re.match(r"became\s+(.+)$", note)
            if b_:
                pid_ = name_pid.get(re.sub(r"[^a-z]", "", b_.group(1).lower()))
                if pid_:
                    v_, st_ = now_player(pid_); return [lab, v_, st_]
                return [lab, 0, "not on any roster now (counts as 0)"]
            o_ = me if note.strip().lower() in ("yours", "your", "mine") else tname.get(re.sub(r"[^a-z]", "", re.sub(r"'s$", "", note.strip()).lower()))
            v_, st_ = now_pick((y_, rd_, o_ if o_ is not None else me))
            return [lab, v_, st_]
        nm_ = re.sub(r"\s*\(.*\)$", "", lab)
        pid_ = name_pid.get(re.sub(r"[^a-z]", "", nm_.lower()))
        if not pid_:
            return [lab, 0, "not on any roster now (counts as 0)"]
        v_, st_ = now_player(pid_)
        return [lab, v_, st_]
    ledger_now = []
    try:
        for r_ in json.load(open(os.path.join(DOCS, "ledger.json"))):
            if isinstance(r_, dict):
                ledger_now.append(dict(got=[ledger_item(x[0]) for x in r_.get("got", [])], gave=[ledger_item(x[0]) for x in r_.get("gave", [])]))
            else:
                ledger_now.append(None)
    except Exception as e:  # noqa
        print(f"WARN could not re-price ledger.json: {e}")

    # ---------- market flags: dynasty value vs best-ball production
    def ppg(pid):
        v = [x for x in P[pid]["pr"] if x > 0]
        return st.mean(v) if len(v) >= 2 else 0.0
    # what the market usually pays for this much production AT THIS AGE (age matters a lot in dynasty)
    vfit = {}
    for ps in POS:
        X, y = [], []
        for pid, d in P.items():
            if d["p"] == ps and not d.get("gone") and v_of(pid) > 0 and ppg(pid) >= 3 and d["age"]:
                X.append([ppg(pid), d["age"], 1.0]); y.append(v_of(pid))
        vfit[ps] = lstsq(X, y) if len(X) >= 12 else None
    def fair(pid):
        c = vfit[P[pid]["p"]]; d = P[pid]
        if not c:
            return max(300.0, float(v_of(pid)))
        return max(300.0, c[0] * ppg(pid) + c[1] * (d["age"] or 26) + c[2])
    Bm = {b["id"]: b for b in board}
    sell, buy = [], []
    for pid in ids_by[me]:
        d = P[pid]; v = v_of(pid)
        if v < 1500:
            continue
        r = (v - fair(pid)) / max(fair(pid), 800)
        why = []
        if r >= 0.3 and ppg(pid) >= 3 and (d["age"] or 0) >= AGE_CLIFF[d["p"]] - 3:  # young stashes are not sells
            why.append(f"value {v:,} vs ~{round(fair(pid)):,} for {ppg(pid):.1f} pts/wk of production")
        if d["age"] and d["age"] >= AGE_CLIFF[d["p"]]:
            why.append(f"age {d['age']:.0f}: {d['p']} values usually drop after this season")
        if why and Bm.get(pid, {}).get("ros", 0) < 1.5:
            sell.append(dict(id=pid, v=v, fair=round(fair(pid)), ppg=round(ppg(pid), 1), add=Bm.get(pid, {}).get("ros", 0), why=why))
    sell.sort(key=lambda x: -x["v"])
    THR = {"QB": 15, "RB": 9, "WR": 9, "TE": 7}
    for pid, d in P.items():
        if d.get("gone") or d["r"] in (0, me) or ppg(pid) < THR[d["p"]]:
            continue
        v = v_of(pid); r = (v - fair(pid)) / max(fair(pid), 800)
        if r > -0.25:
            continue
        add, addp = net_add2(pid)
        if add >= 1.0 or (addp or 0) >= 1.0:
            why = [f"produces {ppg(pid):.1f} pts/wk; that output usually costs ~{round(fair(pid)):,}, he's {v:,}"]
            same = sorted([ppg(x) for x in ids_by[me] if P[x]["p"] == d["p"]], reverse=True)
            need_n = {"QB": 2, "RB": 4, "WR": 5, "TE": 2}[d["p"]]  # starters + realistic flex use at that spot
            better = sum(1 for x in same if x >= ppg(pid))
            if len(same) >= need_n and better >= need_n:
                why.append(f"you already have {better} {d['p']}s who score more, so he'd mostly fill in on byes and bad weeks")
            if d["age"] and d["age"] >= AGE_CLIFF[d["p"]]:
                why.append(f"age {d['age']:.0f}: cheap because of age, so it's points now with little resale")
            buy.append(dict(id=pid, v=v, fair=round(fair(pid)), ppg=round(ppg(pid), 1), add=round(add, 2), add_po=round(addp, 2) if addp is not None else None, old=bool(d["age"] and d["age"] >= AGE_CLIFF[d["p"]]), why=why))
    buy.sort(key=lambda x: -x["add"] / (x["v"] / 1000 + 0.5))
    buy_po = sorted([b_ for b_ in buy if b_.get("add_po") is not None], key=lambda x: -x["add_po"] / (x["v"] / 1000 + 0.5))
    market = dict(sell=sell[:8], buy=buy[:12] + [b_ for b_ in buy_po[:12] if b_ not in buy[:12]])

    # ---------- positional strength: counted points per week by position, ranked across the league
    depth = {}
    for t in teams:
        tot = {p: 0.0 for p in POS}
        for k in range(len(fut)):
            _, sp, _ = lineup_split([(P[p]["p"], P[p]["pr"][k], p) for p in ids_by[t["id"]]])
            for p in POS:
                tot[p] += sp[p] / NW
        depth[t["id"]] = {p: [round(tot[p], 1), 0] for p in POS}
    for p in POS:
        for i, rid in enumerate(sorted(depth, key=lambda r: -depth[r][p][0])):
            depth[rid][p][1] = i + 1

    # ---------- trade grading (every completed trade this season, both sides) + manager profiles
    rem_w = len(fut)
    def replay(got, gave, start_wk):
        """My counted points and matchup results from start_wk on, real lineups vs. without the trade."""
        dpts, dw = 0.0, 0
        for wk in sorted(my_weekly):
            if wk < start_wk or wk not in weeks:
                continue
            ids = my_weekly[wk]
            act = lambda pid: (P[pid]["a"][wk - 1] or 0) if pid in P and wk - 1 < len(P[pid]["a"]) else 0
            real = best_lineup((P[i]["p"], act(i)) for i in ids if i in P)
            alt_ids = [i for i in ids if i not in got] + [i for i in gave if i not in ids]
            alt = best_lineup((P[i]["p"], act(i)) for i in alt_ids if i in P)
            rows = weeks[wk]; mine = next((x for x in rows if x[0] == me), None)
            if not mine:
                continue
            opp = next((x[2] for x in rows if x[1] == mine[1] and x[0] != me), None)
            others = [x[2] for x in rows if x[0] != me]
            def res(s):
                return (1 if opp is not None and s > opp else 0) + (1 if median_on and s > st.median(others + [s]) else 0)
            dpts += real - alt; dw += res(mine[2]) - res(mine[2] - (real - alt))
        return round(dpts, 1), dw

    def verdict(total_pts, vin, vout):
        vp = (vin - vout) / max(vout, 1)
        if total_pts >= 10 and vp >= -0.15: return "Win"
        if total_pts >= 10: return "Win-now"
        if vp >= 0.15 and total_pts >= -10: return "Value win"
        if total_pts <= -10 and vp <= 0.05: return "Loss"
        if vp <= -0.3 and total_pts < 10: return "Loss"
        return "Push"

    feed = []
    for tr in trades:
        if tr["season"] != season:
            continue
        out_sides = []
        for s_ in tr["sides"]:
            rid = s_["cur"]
            gi = items_now(s_["got"], s_["pin"], s_["fin"]); go = items_now(s_["gave"], s_["pout"], s_["fout"])
            vin = sum(x[1] for x in gi); vout = sum(x[1] for x in go)
            ros = None
            if rid in ids_by:
                cur_ids = ids_by[rid]
                alt = [i for i in cur_ids if i not in s_["got"]] + [i for i in s_["gave"] if i in P and i not in cur_ids]
                ros = round((sum(det_weeks(cur_ids)) - sum(det_weeks(alt))) / NW, 2)
            side = dict(rid=rid, name=names.get(rid, "Former manager"),
                        got=[pname(p) for p in s_["got"]] + [pk_label(k) for k in s_["pin"]] + ([f"${s_['fin']} FAAB"] if s_["fin"] else []),
                        gave=[pname(p) for p in s_["gave"]] + [pk_label(k) for k in s_["pout"]] + ([f"${s_['fout']} FAAB"] if s_["fout"] else []),
                        got_ids=s_["got"], gave_ids=s_["gave"], vin=round(vin), vout=round(vout), ros=ros,
                        got_items=gi, gave_items=go,
                        rem_w=rem_w)
            if rid == me:
                side["past"], side["dw"] = replay(s_["got"], s_["gave"], max(tr["leg"], 1))
                side["verdict"] = verdict(side["past"] + (ros or 0) * rem_w, vin, vout)
            else:
                side["verdict"] = verdict((ros or 0) * rem_w, vin, vout)
            out_sides.append(side)
        when = datetime.datetime.fromtimestamp(tr["ts"] / 1000, datetime.timezone.utc).strftime("%Y-%m-%d") if tr["ts"] else f"{season} wk {tr['leg']}"
        feed.append(dict(id=tr["id"], date=when, wk=tr["leg"], mine=any(x["rid"] == me for x in out_sides), sides=out_sides))

    # ---------- each team's approach: where they stand now + how they've traded over the last year (by value, not count)
    pick_cap = {t["id"]: sum(p_["v"] for p_ in picks if p_["own"] == t["id"]) for t in teams}
    cap_rank = {r: i + 1 for i, r in enumerate(sorted(pick_cap, key=lambda r: -pick_cap[r]))}
    now_ms = time.time() * 1000
    approach = {}
    for t in teams:
        rid = t["id"]; sc_ = 0; why = []
        po = S0[rid]["playoffs"] if rid in S0 else 0
        wl_ = f"{rec[rid][0]}-{rec[rid][1]}" if rid in rec else "" 
        if done >= 3 or not fut:
            if po >= 0.6: sc_ += 2; why.append(f"{wl_}, {po:.0%} to make the playoffs")
            elif po >= 0.3: sc_ += 1; why.append(f"{wl_}, in the playoff hunt ({po:.0%})")
            elif po < 0.05: sc_ -= 2; why.append(f"{wl_}, out of the playoff race")
            elif po < 0.15: sc_ -= 1; why.append(f"{wl_}, long shot for the playoffs ({po:.0%})")
        vals_ = [(v_of(p_), P[p_]["age"]) for p_ in ids_by[rid] if v_of(p_) and P[p_]["age"]]
        ra = sum(v * a_ for v, a_ in vals_) / sum(v for v, _ in vals_) if vals_ else None
        if ra is not None:
            if ra <= 24.8: sc_ -= 1; why.append(f"young roster (value-weighted age {ra:.1f})")
            elif ra >= 26.8: sc_ += 1; why.append(f"veteran roster (value-weighted age {ra:.1f})")
        cr = cap_rank[rid]
        if cr <= 3: sc_ -= 1; why.append(f"{ORD[cr] if cr < len(ORD) else cr} most draft-pick value in the league")
        elif cr > nT - 3: sc_ += 1; why.append(f"little draft-pick value ({cr} of {nT})")
        # trades in the last 12 months: value of veterans (27+) bought minus young players (24-) and picks bought
        lean_v = 0.0
        for tr in trades:
            if not tr.get("ts") or now_ms - tr["ts"] > 365 * 86400 * 1000:
                continue
            s_ = next((x for x in tr["sides"] if x["cur"] == rid), None)
            if not s_:
                continue
            def bucket(pids, pks, sign):
                nonlocal lean_v
                for p_ in pids:
                    a_ = pinfo(p_)["age"] or 26; v = pval(p_)
                    lean_v += sign * (v if a_ >= 27 else -v if a_ <= 24 else 0)
                for k_ in pks:
                    lean_v -= sign * pk_val(k_)
            bucket(s_["got"], s_["pin"], 1); bucket(s_["gave"], s_["pout"], -1)
        if lean_v >= 4000: sc_ += 1; why.append("recent trades bought proven veterans")
        elif lean_v <= -4000: sc_ -= 1; why.append("recent trades bought young players and picks")
        style = "Win-now" if sc_ >= 2 else "Rebuild" if sc_ <= -2 else "Balanced"
        approach[rid] = (style, why)

    profiles = {}
    for t in teams:
        rid = t["id"]; my_tr = [tr for tr in trades if any(s_["cur"] == rid for s_ in tr["sides"])]
        partners, age_in, age_out, val_bal, picks_net, faab_net = {}, [], [], 0, 0, 0
        for tr in my_tr:
            s_ = next(x for x in tr["sides"] if x["cur"] == rid)
            for o in tr["sides"]:
                if o["cur"] != rid:
                    partners[o["cur"]] = partners.get(o["cur"], 0) + 1
            age_in += [pinfo(p)["age"] for p in s_["got"] if pinfo(p)["age"]]
            age_out += [pinfo(p)["age"] for p in s_["gave"] if pinfo(p)["age"]]
            val_bal += sum(pval(p) for p in s_["got"]) + sum(pk_val(k) for k in s_["pin"]) - sum(pval(p) for p in s_["gave"]) - sum(pk_val(k) for k in s_["pout"])
            picks_net += len(s_["pin"]) - len(s_["pout"]); faab_net += s_["fin"] - s_["fout"]
        ai = st.mean(age_in) if age_in else None; ao = st.mean(age_out) if age_out else None
        style, why = approach[rid]
        # trade habits (what this manager has actually said yes to), for the acceptance estimate
        now_ms_ = time.time() * 1000; bals, n_pin, n_cons, n_spread, n_mid = [], 0, 0, 0, 0; last_ts = 0
        for tr in my_tr:
            s_ = next(x for x in tr["sides"] if x["cur"] == rid)
            vin = sum(pval(p_) for p_ in s_["got"]) + sum(pk_val(k_) for k_ in s_["pin"])
            vout = sum(pval(p_) for p_ in s_["gave"]) + sum(pk_val(k_) for k_ in s_["pout"])
            if vin + vout > 0:
                bals.append((vin - vout) / max(vout, vin, 1))
            n_pin += 1 if s_["pin"] else 0
            ni, no = len(s_["got"]) + len(s_["pin"]), len(s_["gave"]) + len(s_["pout"])
            n_cons += 1 if no > ni else 0      # gave more pieces than they got (consolidated)
            n_spread += 1 if ni > no else 0    # took more pieces back
            if tr["season"] == season and 1 <= (tr.get("leg") or 0) <= reg_end:
                n_mid += 1
            last_ts = max(last_ts, tr.get("ts") or 0)
        bals.sort(); nt_ = len(my_tr)
        habits = dict(n365=sum(1 for tr in my_tr if tr.get("ts") and now_ms_ - tr["ts"] <= 365 * 86400 * 1000),
                      days=round((now_ms_ - last_ts) / 86400000) if last_ts else None,
                      pin=round(n_pin / nt_, 2) if nt_ else None, cons=round(n_cons / nt_, 2) if nt_ else None,
                      spread=round(n_spread / nt_, 2) if nt_ else None, mid=n_mid,
                      bal_lo=round(bals[max(0, int(len(bals) * 0.2) - 1)], 2) if len(bals) >= 3 else None,
                      bal_med=round(bals[len(bals) // 2], 2) if bals else None)
        ids = ids_by[rid]; vals = [(v_of(p), P[p]["age"]) for p in ids if v_of(p) and P[p]["age"]]
        top_part = max(partners, key=partners.get) if partners else None
        profiles[rid] = dict(n=len(my_tr), this=sum(1 for tr in my_tr if tr["season"] == season), style=style, why=why,
                             partners=len(partners), top=[names.get(top_part, "Former manager"), partners[top_part]] if top_part is not None else None,
                             with_me=partners.get(me, 0), bal=round(val_bal), picks=picks_net, faab=faab_net,
                             age_in=round(ai, 1) if ai else None, age_out=round(ao, 1) if ao else None,
                             roster_age=round(sum(v * a for v, a in vals) / sum(v for v, _ in vals), 1) if vals else None,
                             value=round(sum(v_of(p) for p in ids)), pick_value=sum(p["v"] for p in picks if p["own"] == rid), habits=habits)

    # ---------- rookie board: unrostered first-year players once they show up in FantasyCalc (after the NFL draft)
    rookies = []
    for e in fc:
        pl = e.get("player") or {}; sid = str(pl.get("sleeperId") or "")
        info = players_db.get(sid) or {}
        if not sid or sid in rostered or info.get("position") not in POS or info.get("years_exp") != 0:
            continue
        rookies.append(dict(id=sid, n=info.get("full_name") or pl.get("name"), p=info.get("position"), t=info.get("team") or "", v=unified(sid, e.get("value", 0), info.get("position"))))
    rookies.sort(key=lambda x: -x["v"]); tier = 1
    for i, r in enumerate(rookies[:48]):
        if i and rookies[i - 1]["v"] > 0 and r["v"] < 0.88 * rookies[i - 1]["v"]:
            tier += 1
        r["tier"] = tier
    rookies = rookies[:48]

    # ---------- how I used past picks: who I drafted and how it's going, and picks I traded away (who they became)
    counted, counted_wk = {}, {}
    for wk, ids in my_weekly.items():
        _, _, used = lineup_split([(P[i]["p"], P[i]["a"][wk - 1] or 0, i) for i in ids if i in P and len(P[i]["a"]) >= wk])
        for i in used:
            c = counted.setdefault(i, [0.0, 0]); c[0] += P[i]["a"][wk - 1] or 0; c[1] += 1
            counted_wk.setdefault(i, {})[wk] = P[i]["a"][wk - 1] or 0
    owner_of = {t["id"]: t["owner"] for t in teams}
    team_of_owner = {t["owner"]: t["id"] for t in teams}
    def slot_price(rd, slot_):
        v = pick_base(season + 1, rd)
        return round(v * (1.15 if slot_ <= nT / 3 else 0.87 if slot_ > 2 * nT / 3 else 1.0))
    def grade(v, price):
        return "Hit" if v >= 1.25 * price else "Fair" if v >= 0.8 * price else "Miss"
    my_trades = [tr for tr in trades if any(s_["cur"] == me for s_ in tr["sides"])]
    used_picks = []
    for r in sorted(my_draft, key=lambda r: (r["y"], r["no"] or 0)):
        pid = r["pid"]; v = pval(pid); price = slot_price(r["rd"], r["slot"])
        nm = (P.get(pid) or {}).get("n") or r["n"] or pid
        pos = (P.get(pid) or {}).get("p") or r["p"]
        status, detail, sold = "Released", "", None
        if pid in ids_by[me]:
            status = "On your roster"
        else:
            # most recent trade where I sent him away
            tr = next((tr for tr in my_trades if pid in next(s_ for s_ in tr["sides"] if s_["cur"] == me)["gave"]), None)
            if tr:
                mine = next(s_ for s_ in tr["sides"] if s_["cur"] == me); oth = next((s_ for s_ in tr["sides"] if s_["cur"] != me), None)
                status = "Traded to " + names.get(oth["cur"] if oth else None, "another team")
                got_l = [pname(x) for x in mine["got"]] + [pk_label(k) for k in mine["pin"]] + ([f"${mine['fin']} FAAB"] if mine["fin"] else [])
                gave_l = [pname(x) for x in mine["gave"] if x != pid] + [pk_label(k) for k in mine["pout"]] + ([f"${mine['fout']} FAAB"] if mine["fout"] else [])
                got_v = sum(pval(x) for x in mine["got"]) + sum(pk_val(k) for k in mine["pin"])
                gave_v = sum(pval(x) for x in mine["gave"]) + sum(pk_val(k) for k in mine["pout"])
                ratio = got_v / gave_v if gave_v else None
                share = got_v * (v / gave_v) if gave_v else got_v  # his slice of the return, split by today's values
                pkg_pts = sum(pts_ for x in mine["got"] for wk, pts_ in counted_wk.get(x, {}).items() if tr["season"] < season or wk >= tr["leg"])
                sale = None if ratio is None else "Sold high" if ratio >= 1.15 else "Sold low" if ratio <= 0.87 else "Fair sale"
                when = datetime.datetime.fromtimestamp(tr["ts"] / 1000, datetime.timezone.utc).strftime("%Y-%m-%d") if tr["ts"] else str(tr["season"])
                sold = dict(date=when, to=names.get(oth["cur"] if oth else None, "another team"), got=got_l, with_=gave_l,
                            got_v=round(got_v), gave_v=round(gave_v), ratio=round(ratio, 2) if ratio is not None else None, share=round(share),
                            sale=sale, pkg_pts=round(pkg_pts, 1))
            elif rostered.get(pid):
                status = "Now on " + names[rostered[pid]]
        a_ = [x for x in (P.get(pid) or {}).get("a", []) if x is not None]
        c = counted.get(pid, [0.0, 0])
        used_picks.append(dict(y=r["y"], pick=f"{r['rd']}.{r['slot']:02d}", rd=r["rd"], pid=pid, n=nm, p=pos,
                               via=None if r["orig"] == me_owner else names.get(team_of_owner.get(r["orig"]), "another team"),
                               status=status, detail=detail, v=v, price=price, sold=sold,
                               grade=grade(sold["share"] if sold else v, price),
                               cnt=round(c[0], 1), wks=c[1], ppg=round(st.mean(a_), 1) if a_ else None,
                               impact=Bm.get(pid, {}).get("ros") if pid in ids_by[me] else None))
    traded_picks = []
    for tr in my_trades:
        mine = next(s_ for s_ in tr["sides"] if s_["cur"] == me); oth = next((s_ for s_ in tr["sides"] if s_["cur"] != me), None)
        for (y, rd, o) in mine["pout"]:
            if y > season:
                continue  # not drafted yet; shows under picks owned elsewhere
            r = draft_by.get((y, rd, owner_of.get(o, o)))
            became = None
            if r:
                became = dict(n=(P.get(r["pid"]) or {}).get("n") or pinfo(r["pid"])["n"] or r["n"], p=r["p"], pick=f"{rd}.{r['slot']:02d}",
                              v=pval(r["pid"]), by=names.get(team_of_owner.get(r["by"]), "another team"))
            when = datetime.datetime.fromtimestamp(tr["ts"] / 1000, datetime.timezone.utc).strftime("%Y-%m-%d") if tr["ts"] else str(tr["season"])
            traded_picks.append(dict(date=when, pick=pk_label((y, rd, o)), to=names.get(oth["cur"] if oth else None, "another team"),
                                     got=[pname(x) for x in mine["got"]] + [pk_label(k) for k in mine["pin"]],
                                     got_v=round(sum(pval(x) for x in mine["got"])), became=became))
    traded_picks.sort(key=lambda x: x["date"])

    tm_out = []
    for t in teams:
        r = rec[t["id"]]
        tm_out.append(dict(id=t["id"], name=t["name"], user=t["user"], w=r[0], l=r[1], pf=round(r[2], 1), odds=odds[t["id"]],
                           xw=round(exp_wins[t["id"]], 1), ros=ros_ppg[t["id"]], proj=proj_this.get(t["id"]), wp=round(wp.get(t["id"], 0) / SIMS, 3),
                           slot=round(slot[t["id"]], 1), h2h=rec_h[t["id"]], med=rec_m[t["id"]], faab=t["faab_left"],
                           live=live.get(t["id"]), taxi=len(t["taxi"]), ir=len(t["ir"]),
                           po=po_ppg.get(t["id"]), rng=proj_range.get(t["id"]), seed=S0[t["id"]]["seed"], title_se=title_se[t["id"]],
                           opp1={str(k): v for k, v in sorted(S0[t["id"]]["opp1"].items(), key=lambda x: -x[1])[:4]},
                           title_bye=round(S0[t["id"]]["title_bye"] / max(S0[t["id"]]["bye"], 1e-9), 3) if S0[t["id"]]["bye"] else None,
                           title_nobye=round(S0[t["id"]]["title_nobye"] / max(S0[t["id"]]["playoffs"] - S0[t["id"]]["bye"], 1e-9), 3) if S0[t["id"]]["playoffs"] - S0[t["id"]]["bye"] > 0.001 else None))
    # ---------- my to-do list: things only a manager action fixes
    actions = []
    if lineup_adv and lineup_adv["changes"] and lineup_adv["cur_wp"] is not None and (lineup_adv["best_wp"] or 0) - lineup_adv["cur_wp"] >= 0.005:
        ch_ = "; ".join(f"start {P[c['start']]['n']} over {P[c['sit']]['n']}" for c in lineup_adv["changes"])
        actions.append(f"Set your lineup: {ch_} (win chance {lineup_adv['cur_wp']:.0%} → {lineup_adv['best_wp']:.0%}).")
    me_t = next(t for t in teams if t["id"] == me)
    for pid in me_t["ir"]:
        if pid in P and not P[pid]["inj"]:
            actions.append(f"{P[pid]['n']} is healthy but still on IR. He can't score until you move him back to your roster.")
    for pid in me_t["taxi"]:
        if pid in P:
            add = (sum(det_weeks(ids_by[me] + [pid])) - bm) / NW
            if add >= 1.0:
                actions.append(f"{P[pid]['n']} is on your taxi squad but would add {add:.1f} pts/wk to your lineup. Promote him (you'll need an open roster spot).")
    ir_open = (int(settings.get("reserve_slots", 0) or 0) if any(t["ir"] for t in teams) else 0) - len(me_t["ir"])
    if ir_open > 0 and any("taxi squad" in a for a in actions):  # only matters when you need a roster spot
        for pid in ids_by[me]:
            if P[pid]["inj"] in ("IR", "Out", "PUP") and (P[pid]["inj"] != "Out" or settings.get("reserve_allow_out")):
                actions.append(f"{P[pid]['n']} ({P[pid]['inj']}) can go on IR to free a roster spot; you have {ir_open} IR slot{'s' if ir_open > 1 else ''} open.")
    now = datetime.datetime.now(datetime.timezone.utc)
    gen = now.strftime("%Y-%m-%dT%H:%MZ")

    # ---------- history (one point per day) + alerts (diff against the previous refresh)
    prev = None
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:  # noqa
            prev = None
    hist_path = os.path.join(DOCS, "history.json")
    hist = json.load(open(hist_path)) if os.path.exists(hist_path) else []
    snap = dict(d=now.strftime("%Y-%m-%d"), wk=this_week, done=done, odds={str(t["id"]): [odds[t["id"]]["playoffs"], odds[t["id"]]["title"]] for t in teams},
                val=round(sum(v_of(p) for p in ids_by[me])), w=rec[me][0], l=rec[me][1])
    hist = [h for h in hist if h["d"] != snap["d"]] + [snap]
    hist = hist[-180:]
    # per-player value history (one point a day, ~60 days) -> 30-day value trend
    vh_path = os.path.join(DOCS, "values.json")
    vh = json.load(open(vh_path)) if os.path.exists(vh_path) else {}
    vh[snap["d"]] = {pid: d["val"] for pid, d in P.items() if not d.get("gone") and d.get("val")}
    for dk in sorted(vh)[:-60]:
        del vh[dk]
    target = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    past_days = [dk for dk in sorted(vh) if dk < snap["d"]]
    ref = next((dk for dk in past_days if dk >= target), past_days[0] if past_days else None)
    if ref:
        days = (now.date() - datetime.date.fromisoformat(ref)).days
        for pid, d in P.items():
            old_v = vh[ref].get(pid)
            if old_v and d.get("val"):
                d["vt"] = [old_v, days]
    # trade values at the moment each trade first shows up (so trades can be judged on what was known then, not just hindsight)
    tv_path = os.path.join(DOCS, "trade_values.json")
    tvals = json.load(open(tv_path)) if os.path.exists(tv_path) else {}
    for f in feed:
        if f["id"] not in tvals:
            tvals[f["id"]] = dict(seen=snap["d"], sides={str(sd["rid"]): [sd["vin"], sd["vout"]] for sd in f["sides"]})
        tv = tvals[f["id"]]
        for sd in f["sides"]:
            v0 = tv["sides"].get(str(sd["rid"]))
            if v0:
                sd["vin0"], sd["vout0"], sd["seen"] = v0[0], v0[1], tv["seen"]

    al_path = os.path.join(DOCS, "alerts.json")
    alerts = json.load(open(al_path)) if os.path.exists(al_path) else []
    new = []
    def add_alert(kind, mine_, txt):
        new.append(dict(t=gen, k=kind, me=mine_, txt=txt))
    BAD = ("Out", "IR", "PUP", "Sus", "Doubtful")
    if prev:
        pP, pA = prev.get("players", {}), prev.get("analysis", {})
        pneeds = pA.get("needs", {})
        for pid, d in P.items():
            if d.get("gone") or pid not in pP:
                continue
            was, now_s = pP[pid].get("inj"), d["inj"]
            if was == now_s:
                continue
            if d["r"] == me:
                if now_s in BAD and was not in BAD:
                    imp = Bm.get(pid, {}).get("ros", 0)
                    add_alert("injury", True, f"{d['n']} is now {now_s}. Rest-of-season, he now adds {imp:.1f} counted pts/wk to your lineup.")
                elif was in BAD and now_s not in BAD:
                    add_alert("return", True, f"{d['n']} is back ({now_s or 'active'}).")
            elif now_s in BAD and was not in BAD and v_of(pid) >= 2500:
                rid = d["r"]; pos = d["p"]
                before = (pneeds.get(str(rid)) or {}).get(pos); after = needs[rid][pos]
                spare = [P[x]["n"] for x in ids_by[me] if P[x]["p"] == pos and Bm.get(x, {}).get("ros", 9) < 1.0 and v_of(x) >= 800]
                if before is not None and after - before >= 1.0:
                    add_alert("buyer", True, f"{d['n']} ({names[rid]}) is {now_s}: their {pos} need jumped {before:.1f} → {after:.1f} pts/wk, so they may be a {pos} buyer."
                              + (f" Your spare {pos}s: {', '.join(spare[:3])}." if spare else ""))
                else:
                    add_alert("injury", False, f"{d['n']} ({names[rid]}) is now {now_s}.")
        for pid in ids_by[me]:
            old = (pP.get(pid) or {}).get("val") or 0; cur_v = P[pid].get("val") or 0
            if old >= 1000 and abs(cur_v - old) >= max(300, 0.12 * old):
                add_alert("value", True, f"{P[pid]['n']}'s value moved {old:,} → {cur_v:,} ({(cur_v - old) / old:+.0%}).")
        seen_ids = {f.get("id") for f in (prev.get("feed") or [])}
        for f in feed:
            if f["id"] not in seen_ids:
                s0, s1 = (f["sides"] + [{}, {}])[:2]
                add_alert("trade", f["mine"], f"Trade: {s0.get('name')} get {', '.join(s0.get('got', [])) or '—'}; {s1.get('name')} get {', '.join(s1.get('got', [])) or '—'}.")
    prev_actions = set((prev or {}).get("meta", {}).get("actions") or [])
    for a in actions:
        if a not in prev_actions:
            add_alert("action", True, a)
    alerts = (alerts + new)[-100:]
    os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
    push = [a for a in new if a["me"]]
    open(os.path.join(ROOT, "out", f"new_alerts{('_' + DYN) if DYN else ''}.md"), "w").write(
        ("\n".join(f"- {a['txt']}" for a in push) + (f"\n\n[Open Dynasty HQ]({CFG['site_url']})" if CFG.get("site_url") else "") + "\n") if push else "")

    # ---------- weekly digest (markdown; the workflow posts it as a GitHub issue on Tuesdays)
    md = []
    mt = next(x for x in tm_out if x["id"] == me)
    order_now = sorted(tm_out, key=lambda x: (-x["w"], -x["pf"]))
    try:
        checks["bracket"] = bracket_check(get(f"{API}/league/{lid}/winners_bracket", cache_hours=1, required=False), [x["id"] for x in order_now], n_playoff)
        print(f"bracket check: {checks['bracket']}")
    except Exception as e:  # noqa
        print(f"WARN bracket check skipped: {e}")
    md.append(f"# {mt['name']} · Week {this_week or '—'} briefing\n")
    if done and done in weeks:
        rows = weeks[done]; mine = next((x for x in rows if x[0] == me), None)
        if mine:
            opp = next((x for x in rows if x[1] == mine[1] and x[0] != me), None)
            med_ = st.median([x[2] for x in rows])
            res_ = ("W" if opp and mine[2] > opp[2] else "L") + (("/W" if mine[2] > med_ else "/L") if median_on else "")
            md.append(f"**Week {done}:** {mine[2]:.1f}" + (f" vs {names[opp[0]]} {opp[2]:.1f}" if opp else "") + f" ({res_}{', median ' + format(med_, '.1f') if median_on else ''})")
            ids = my_weekly.get(done, [])
            _, sp, used = lineup_split([(P[i]["p"], P[i]["a"][done - 1] or 0, i) for i in ids if i in P and len(P[i]["a"]) >= done])
            top = sorted(used, key=lambda i: -(P[i]["a"][done - 1] or 0))[:4]
            md.append("Top counted: " + ", ".join(f"{P[i]['n']} {P[i]['a'][done - 1]:.1f}" for i in top) + "\n")
    wk_ago = next((h for h in reversed(hist) if (now - datetime.datetime.fromisoformat(h["d"]).replace(tzinfo=datetime.timezone.utc)).days >= 6), None)
    chg = lambda a, b: f" ({(a - b) * 100:+.0f} pts this week)" if b is not None else ""
    po_then = wk_ago["odds"].get(str(me), [None, None]) if wk_ago else [None, None]
    md.append(f"**Record:** {mt['w']}-{mt['l']}, #{order_now.index(mt) + 1} of {nT} · {mt['pf']} PF")
    md.append(f"**Odds:** playoffs {mt['odds']['playoffs']:.0%}{chg(mt['odds']['playoffs'], po_then[0])} · bye {mt['odds']['bye']:.0%} · title {mt['odds']['title']:.0%}{chg(mt['odds']['title'], po_then[1])}\n")
    if fut:
        bt_ = S0[me]
        md.append(f"**Playoff path:** most likely seed {max(range(len(bt_['seed'])), key=lambda i: bt_['seed'][i]) + 1}; "
                  f"title chance with a bye {bt_['title_bye'] / max(bt_['bye'], 1e-9):.0%} vs without {bt_['title_nobye'] / max(bt_['playoffs'] - bt_['bye'], 1e-9):.0%}" if bt_['bye'] and bt_['playoffs'] - bt_['bye'] > 0.001 else "")
        ti = sorted((o for L_ in sugg.values() for o in L_ if o.get("title1") is not None), key=lambda o: o["title0"] - o["title1"])
        if ti and ti[0]["title1"] - ti[0]["title0"] >= 0.01:
            o = ti[0]; md.append(f"**Best trade idea:** give {', '.join(P[x]['n'] for x in o['give'])} for {', '.join(P[x]['n'] for x in o['get'])}: title chance {o['title0']:.0%} → {o['title1']:.0%}")
        else:
            md.append("**Best trade idea:** none that clearly raises your title chance; standing pat is fine.")
        if fa:
            md.append(f"**Waivers:** {P[fa[0]['id']]['n']} would add {fa[0]['add']:.1f} pts/wk to your lineup.")
        md.append("__WINDOW__")
        md.append("")
    if this_week and this_week in sched:
        pr_ = next((x for x in sched[this_week] if x[0] == me), None)
        opp = next((x[0] for x in sched[this_week] if pr_ and x[1] == pr_[1] and x[0] != me), None)
        if opp:
            md.append(f"**Week {this_week}:** vs {names[opp]} · projected {proj_this.get(me)} – {proj_this.get(opp)} · win chance {wp.get(me, 0) / SIMS:.0%}")
        byes = [P[p]["n"] for p in ids_by[me] if P[p]["bye"] == this_week]
        inj = [f"{P[p]['n']} ({P[p]['inj']})" for p in ids_by[me] if P[p]["inj"] in BAD]
        if byes:
            md.append(f"On bye: {', '.join(byes)}")
        if inj:
            md.append(f"Injured: {', '.join(inj)}")
        md.append("")
    if actions:
        md.append("## To do")
        md += [f"- {a}" for a in actions]
        md.append("")
    recent = [a for a in alerts if a["me"] and (now - datetime.datetime.strptime(a["t"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=datetime.timezone.utc)).days < 7]
    if recent:
        md.append("## Alerts this week")
        md += [f"- {a['txt']}" for a in recent[-8:]]
        md.append("")
    wk_feed = [f for f in feed if f["date"] >= (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")]
    if wk_feed:
        md.append("## League trades")
        for f in wk_feed:
            md.append("- " + " ⇄ ".join(f"**{s_['name']}** get {', '.join(s_['got']) or '—'} ({s_['verdict']})" for s_ in f["sides"]))
        md.append("")
    if market["sell"] or market["buy"]:
        md.append("## Market")
        for x in market["sell"][:2]:
            md.append(f"- Sell-high: {P[x['id']]['n']}: {'; '.join(x['why'])}")
        for x in market["buy"][:3]:
            md.append(f"- Buy-low: {P[x['id']]['n']} ({names[P[x['id']]['r']]}): +{x['add']:.1f} counted pts/wk for you, value {x['v']:,}")
        md.append("")
    if CFG.get("site_url"):
        md.append(f"[Open Dynasty HQ]({CFG['site_url']})")
    digest = "\n".join(md)
    open(os.path.join(DOCS, "digest.md"), "w").write(digest)

    # ---------- three-season outlook: aging, young players' upside, and the rookies each team's picks bring
    yrs = [season, season + 1, season + 2]
    vfit2 = {}
    for ps in POS:
        xs, ys = [], []
        for pid_, d in P.items():
            if d["p"] == ps and not d.get("gone") and d.get("val") and ppg(pid_) >= 3:
                xs.append(d["val"]); ys.append(ppg(pid_))
        vfit2[ps] = linfit(xs, ys)
    def v_ppg(ps, v):
        a_, b_ = vfit2[ps]
        return max(0.0, a_ * v + b_)
    for pid_, d in P.items():
        if d["p"] not in POS or (d.get("gone") and not d.get("fa")):
            continue
        age = d["age"] or 26
        x = ppg(pid_)
        if x <= 0:
            a_ = [v for v in d["a"] if v is not None]
            x = (sum(a_) / len(a_)) if a_ else (d.get("a25") or v_ppg(d["p"], d.get("val") or 0))
        wv = 0.5 if age < 24 else 0.35 if age < 25 else 0.2   # the market prices young players' growth; lean on it more for them
        vp = v_ppg(d["p"], d.get("val") or 0); mult = 1.0; fp = []
        for j in (1, 2):
            if age + j >= RETIRE[d["p"]]:
                fp.append(0.0); continue
            mult *= age_step(d["p"], int(age) + j - 1)
            w = wv * (0.8 ** (j - 1))
            fp.append(round((1 - w) * x * mult + w * vp, 2))
        d["fp"] = fp
    rk = vfit2["WR"]
    own_picks = {t["id"]: [pk for pk in picks if pk["own"] == t["id"]] for t in teams}
    fut_str = {t["id"]: [ros_ppg.get(t["id"], 0)] for t in teams}
    for j in (1, 2):
        for t in teams:
            fut_str[t["id"]].append(round(future_strength(future_items(ids_by[t["id"]], P, own_picks[t["id"]], j, season, rk), j), 1))
    # future title chances: play each future season out (random schedule, same playoff format) from the projected rosters
    FS = int(CFG.get("future_sims", 200))
    fut_title = {t["id"]: [S0[t["id"]]["title"]] for t in teams}
    T_ = [t["id"] for t in teams]
    for j in (1, 2):
        items = {r: future_items(ids_by[r], P, own_picks[r], j, season, rk) for r in T_}
        wins = {r: 0 for r in T_}
        for s_ in range(FS):
            sc = {r: [best_lineup((pos, sim_points(m, cv, sid, tm, s_, 600 + j * 40 + wk, P_MISS, pos)) for pos, m, cv, sid, tm in items[r])
                      for wk in range(FUT_WEEKS + FUT_PO)] for r in T_}
            W = {r: 0 for r in T_}; PF = {r: 0.0 for r in T_}
            for wk in range(FUT_WEEKS):
                order_ = sorted(T_, key=lambda r: h3(r, s_ * 53 + 7, wk * 59 + j))
                vals = {r: sc[r][wk] for r in T_}; med = st.median(vals.values())
                for i in range(0, len(order_) - 1, 2):
                    a_, b_ = order_[i], order_[i + 1]
                    W[a_ if vals[a_] > vals[b_] else b_] += 1
                for r in T_:
                    PF[r] += vals[r]
                    if median_on and vals[r] > med:
                        W[r] += 1
            seeds = sorted(T_, key=lambda r: (W[r], PF[r]), reverse=True)[:n_playoff]
            if len(seeds) >= 2:
                k0 = FUT_WEEKS
                wins[bracket(seeds, lambda x_, y_, i: x_ if sc[x_][k0 + i] >= sc[y_][k0 + i] else y_)[0]] += 1
        for r in T_:
            fut_title[r].append(round(wins[r] / FS, 3))
    fut_rank = {r: [] for r in T_}
    for j in range(3):
        for i, r in enumerate(sorted(T_, key=lambda r: -fut_str[r][j])):
            fut_rank[r].append(i + 1)
    curve = {j: sorted([fut_str[r][j], fut_title[r][j]] for r in T_) for j in (1, 2)}
    def title_from(strength, j):
        pts_ = curve[j]
        if strength <= pts_[0][0]:
            return pts_[0][1]
        for (x0, y0), (x1, y1) in zip(pts_, pts_[1:]):
            if strength <= x1:
                return y0 + (y1 - y0) * (strength - x0) / max(x1 - x0, 1e-9)
        return pts_[-1][1]
    def exp_titles(t3):
        return sum(w * t for w, t in zip(DISC, t3))
    # judge the trade ideas over three seasons, not just this one
    for t_id, L in sugg.items():
        for o in L:
            if o.get("title1") is None:
                continue
            new_ids = [i for i in ids_by[me] if i not in o["give"]] + o["get"]
            fut_after = []
            for j in (1, 2):
                s_old = future_strength(future_items(ids_by[me], P, own_picks[me], j, season, rk), j)
                s_new = future_strength(future_items(new_ids, P, own_picks[me], j, season, rk), j)
                fut_after.append(round(max(0.0, fut_title[me][j] + title_from(s_new, j) - title_from(s_old, j)), 3))
            o["fut"] = fut_after
            o["E0"] = round(exp_titles(fut_title[me]), 3)
            o["E1"] = round(exp_titles([o["title1"]] + fut_after), 3)
    mine_r, mine_t = fut_rank[me], fut_title[me]
    po_me = S0[me]["playoffs"]
    # a contender: title chance well above an average team's (1 in N), or a top-2 roster that's all but in the playoffs
    contend_now = mine_t[0] >= 1.6 / max(nT, 1) or (mine_r[0] <= 2 and po_me >= 0.9)
    if contend_now and mine_r[2] <= 4:
        window = dict(kind="contend", label="Window open: contend")
    elif contend_now:
        window = dict(kind="contend_now", label="Contend now, plan ahead")
    elif po_me >= 0.3 and mine_r[1] <= 3:
        window = dict(kind="build", label="Building: next year looks better")
    elif po_me < 0.3 and mine_r[1] >= 6 and mine_r[2] >= 6:
        window = dict(kind="rebuild", label="Rebuild")
    else:
        window = dict(kind="retool", label="Retool: compete while getting younger")
    vw = [(P[p_].get("val") or 0, P[p_]["age"]) for p_ in ids_by[me] if P[p_].get("val") and P[p_]["age"]]
    window.update(ranks=mine_r, titles=mine_t, years=yrs, E=round(exp_titles(mine_t), 3),
                  age=round(sum(v * a for v, a in vw) / sum(v for v, _ in vw), 1) if vw else None,
                  firsts=[pk["y"] for pk in own_picks[me] if pk["rd"] == 1],
                  picks_rank=sorted(T_, key=lambda r: -sum(pk["v"] for pk in own_picks[r])).index(me) + 1)
    outlook = dict(years=yrs, disc=list(DISC), rookie=list(ROOKIE_YR), rk=list(rk), curve={str(j): curve[j] for j in curve},
                   teams={str(r): dict(str=fut_str[r], rank=fut_rank[r], title=fut_title[r], E=round(exp_titles(fut_title[r]), 3)) for r in T_},
                   window=window, sims=FS, str_s=STR_S, str_w=STR_W)

    w_ = window
    digest = digest.replace("__WINDOW__", f"**Window:** {w_['label']}. Projected rank " + ", ".join(f"{y}: {r}" for y, r in zip(w_["years"], w_["ranks"])) + ".")
    open(os.path.join(DOCS, "digest.md"), "w").write(digest)
    # ---------- the recommendation: what to do with the team this week, and why
    advice = build_advice(dict(P=P, me=me, mt=next(x for x in tm_out if x["id"] == me), teams=tm_out, names=names, S0=S0, title_se=title_se,
                               sugg=sugg, fa=fa, cut=cut, market=market, actions=actions, depth=depth, ids_by=ids_by, fut=fut, reg_end=reg_end,
                               wk_mean=wk_mean, po_ppg=po_ppg, this_week=this_week, deadline=int(settings.get("trade_deadline", 0) or 0),
                               n_playoff=n_playoff, picks=picks, season=season, window=window, outlook=outlook))
    prev_adv = (prev or {}).get("advice") or {}
    advice["source"] = "rules"
    key_now = json.dumps([advice["verdict"], advice["headline"], [m["text"] for m in advice["moves"]]])
    if os.environ.get("ANTHROPIC_API_KEY") and fut:
        if prev_adv.get("source") == "claude" and prev_adv.get("key") == key_now and prev_adv.get("date") == now.strftime("%Y-%m-%d"):
            advice = prev_adv  # nothing changed today: reuse Claude's write-up instead of paying for a new one
        else:
            written = claude_advice(advice, CFG.get("claude_model", "claude-sonnet-5-5"))
            if written:
                advice.update(written); advice["source"] = "claude"
    advice["key"] = key_now; advice["date"] = now.strftime("%Y-%m-%d"); advice.setdefault("updated", gen)
    if prev_adv and prev_adv.get("verdict") != advice["verdict"]:
        advice["changed_from"] = prev_adv.get("headline")
    elif prev_adv.get("changed_from") and prev_adv.get("date") == advice["date"]:
        advice["changed_from"] = prev_adv["changed_from"]

    # ---------- research: weekly role (snaps, targets, carries, red zone) and expected points from that role
    # xFP = what a player's usage is usually worth in this league's scoring, fit per position on this season + last season
    XF = {}
    def xrow(e_):
        return [e_[4], e_[3], e_[2], e_[5], e_[6], e_[7] / 10.0, 1.0]
    for ps in POS:
        X_, y_ = [], []
        for pid_, d_ in ext.items():
            if (players_db.get(pid_) or {}).get("position") != ps:
                continue
            for k_, e_ in d_.items():
                if k_ in pts.get(pid_, {}) and (e_[0] > 0 or e_[2] + e_[3] + e_[4] > 0):
                    X_.append(xrow(e_)); y_.append(pts[pid_][k_])
        XF[ps] = lstsq(X_, y_) if len(X_) > 60 else None
    research = []
    for pid_, d_ in ext.items():
        info_ = pinfo(pid_)
        if info_["p"] not in POS:
            continue
        wk_rows = []; mx_opp = 0
        for wk in range(1, done + 1):
            k_ = f"{season}-{wk}"; e_ = d_.get(k_)
            if not e_:
                wk_rows.append(None); continue
            tt_ = tm_tot.get((info_["t"] or "", k_), [0, 0])
            xf_ = sum(c * x for c, x in zip(XF[info_["p"]], xrow(e_))) if XF.get(info_["p"]) else None
            opp_ = e_[4] + e_[3] if info_["p"] == "QB" else e_[2] + e_[3]
            mx_opp = max(mx_opp, opp_)
            wk_rows.append([round(pts.get(pid_, {}).get(k_, 0), 1), round(e_[0] / e_[1], 3) if e_[1] else None, int(e_[2]), int(e_[3]), int(e_[4]),
                            int(e_[5] + e_[6]), round(e_[2] / tt_[0], 3) if tt_[0] else None, round(max(xf_, 0), 1) if xf_ is not None else None])
        own_ = rostered.get(pid_, 0)
        if not own_ and (not info_["t"] or mx_opp < 3):
            continue
        if all(w_ is None for w_ in wk_rows):
            continue
        fcv_ = fc_vals.get(pid_, 0); tr_, rp_ = fc_meta.get(pid_, (0, None))
        v_ = unified(pid_) if fcv_ else 0
        # FantasyCalc's 30-day trend is a change in ITS value; convert the value 30 days ago onto our scale and diff
        old_ = unified(pid_, max(fcv_ - tr_, 1), info_["p"]) if fcv_ and tr_ else v_
        research.append(dict(id=pid_, n=info_["n"], p=info_["p"], t=info_["t"], r=own_, age=info_["age"], inj=info_["inj"],
                             v=v_, vt=round(v_ - old_) if fcv_ and tr_ else 0, ro=rp_, w=wk_rows,
                             pr=[round(x, 1) for x in (P.get(pid_, {}).get("pr") or [])[:4]],
                             mu=mu_of(info_["t"], info_["p"]) if info_["t"] else []))
    print(f"research: {len(research)} players with usage, xFP fit for {sorted(k for k in XF if XF[k])}")

    # ---------- accuracy 1: backtest the Research flags on last season + this season (same rules as the page)
    FLOOR = {"QB": 15, "RB": 9, "WR": 9, "TE": 7}; MINABS = {"QB": 4, "RB": 3, "WR": 2.5, "TE": 2}
    def games_of(pid_, yr):
        out = []
        for wk in range(1, (last_week if yr < season else done) + 1):
            e_ = ext.get(pid_, {}).get(f"{yr}-{wk}")
            if not e_:
                continue
            ps_ = pinfo(pid_)["p"]
            xf_ = sum(c * x for c, x in zip(XF[ps_], xrow(e_))) if XF.get(ps_) else None
            out.append(dict(wk=wk, fp=pts.get(pid_, {}).get(f"{yr}-{wk}", 0.0), opp=(e_[4] + e_[3]) if ps_ == "QB" else (e_[2] + e_[3]),
                            sn=(e_[0] / e_[1]) if e_[1] else None, xf=max(xf_, 0) if xf_ is not None else None))
        return out
    mean_ = lambda a: sum(a) / len(a) if a else None
    bt_rows = []  # one row per player-week where a flag could be evaluated: features + what happened next
    for pid_ in ext:
        ps_ = pinfo(pid_)["p"]
        if ps_ not in POS:
            continue
        for yr in (season - 1, season):
            G_ = games_of(pid_, yr)
            for i in range(5, len(G_) - 2):  # need >=2 games before the window and 3 after
                hst, nxt = G_[:i], G_[i:i + 3]
                L_, B_ = hst[-3:], hst[:-3]
                if len(B_) < 2:
                    continue
                oL, oB = mean_([g["opp"] for g in L_]), mean_([g["opp"] for g in B_])
                sL = mean_([g["sn"] for g in L_ if g["sn"] is not None]); sB = mean_([g["sn"] for g in B_ if g["sn"] is not None])
                fL = mean_([g["fp"] for g in L_]); xL = mean_([g["xf"] for g in L_ if g["xf"] is not None])
                bt_rows.append(dict(p=ps_, oL=oL, oB=oB, sus=all(g["opp"] > oB for g in L_[-2:]), dS=(sL - sB) if sL is not None and sB is not None else None, sL=sL,
                                    fL=fL, xL=xL, fB=mean_([g["fp"] for g in B_]), oN=mean_([g["opp"] for g in nxt]), fN=mean_([g["fp"] for g in nxt])))
    def flag_eval():
        out = dict(n=len(bt_rows), up={}, down={}, hot={}, cold={})
        if len(bt_rows) < 200:
            return out
        for thr in (0.15, 0.25, 0.4):
            up = [r for r in bt_rows if (r["oL"] >= r["oB"] * (1 + thr) and r["oL"] - r["oB"] >= MINABS[r["p"]] and r["sus"]) or (r["dS"] is not None and r["dS"] >= 0.12 and (r["sL"] or 0) >= 0.4)]
            dn = [r for r in bt_rows if (r["oL"] <= r["oB"] * (1 - thr) and r["oB"] - r["oL"] >= MINABS[r["p"]]) or (r["dS"] is not None and r["dS"] <= -0.12)]
            if up:
                kept = [r for r in up if r["oN"] >= r["oB"] + 0.5 * (r["oL"] - r["oB"])]
                out["up"][str(thr)] = dict(n=len(up), kept=round(len(kept) / len(up), 3), lift=round(mean_([r["fN"] - r["fB"] for r in up]), 2))
            if dn:
                stuck = [r for r in dn if r["oN"] <= r["oB"] - 0.5 * (r["oB"] - r["oL"])]
                out["down"][str(thr)] = dict(n=len(dn), kept=round(len(stuck) / len(dn), 3), lift=round(mean_([r["fN"] - r["fB"] for r in dn]), 2))
        for lk in (2, 3, 4):
            hot = [r for r in bt_rows if r["xL"] is not None and r["fL"] - r["xL"] >= lk and r["fL"] >= FLOOR[r["p"]] * 0.8]
            cold = [r for r in bt_rows if r["xL"] is not None and r["fL"] - r["xL"] <= -lk and r["xL"] >= FLOOR[r["p"]]]
            if hot:
                out["hot"][str(lk)] = dict(n=len(hot), kept=round(sum(1 for r in hot if r["fN"] < r["fL"]) / len(hot), 3), lift=round(mean_([r["fN"] - r["fL"] for r in hot]), 2))
            if cold:
                out["cold"][str(lk)] = dict(n=len(cold), kept=round(sum(1 for r in cold if r["fN"] > r["fL"]) / len(cold), 3), lift=round(mean_([r["fN"] - r["fL"] for r in cold]), 2))
        # baseline: how often ANY player's next 3 games beat his last 3 (so a flag's hit rate can be compared to chance)
        out["base_up"] = round(sum(1 for r in bt_rows if r["fN"] > r["fL"]) / len(bt_rows), 3)
        def pick(d, need=25):
            ok = [(v["kept"], k) for k, v in d.items() if v["n"] >= need]
            return max(ok)[1] if ok else None
        out["rec"] = dict(up=pick(out["up"]), hot=pick(out["hot"]), cold=pick(out["cold"]))
        return out
    flag_bt = flag_eval()
    print(f"flag backtest: {flag_bt.get('n')} player-windows, recommended {flag_bt.get('rec')}")

    # ---------- accuracy 2: log this week's predictions before kickoff, grade past weeks as they finish
    pred_path = os.path.join(DOCS, "predictions.json")
    try:
        PRED = json.load(open(pred_path))
    except Exception:  # noqa
        PRED = {}
    PRED.setdefault("weeks", {}); PRED.setdefault("odds", [])
    started = False
    if isinstance(sch, list) and fut:
        started = any(isinstance(gm, dict) and str(gm.get("week")) == str(fut[0]) and gm.get("status") in ("in_progress", "complete") for gm in sch)
    if fut and not started:
        PRED["weeks"][f"{season}-{fut[0]}"] = dict(made=gen, wp={str(r): round(S0[r]["wp"], 3) for r in S0},
                                                   proj={str(t["id"]): proj_this.get(t["id"]) for t in teams},
                                                   pl={pid_: d_["pr"][0] for pid_, d_ in P.items() if d_.get("r") and d_["pr"] and d_["pr"][0] > 0})
    day_ = gen[:10]
    if not PRED["odds"] or PRED["odds"][-1].get("d") != day_:
        PRED["odds"].append(dict(d=day_, wk=this_week, t={str(r): [round(S0[r]["playoffs"], 3), round(S0[r]["title"], 3)] for r in S0}))
    else:
        PRED["odds"][-1] = dict(d=day_, wk=this_week, t={str(r): [round(S0[r]["playoffs"], 3), round(S0[r]["title"], 3)] for r in S0})
    PRED["odds"] = PRED["odds"][-200:]
    json.dump(PRED, open(pred_path, "w"), separators=(",", ":"))
    graded = dict(games=[], pl_err=[], team_err=[])
    for key_, rec_ in PRED["weeks"].items():
        yr_, wk_ = map(int, key_.split("-"))
        if yr_ != season or wk_ > done or wk_ not in weeks:
            continue
        rows_ = weeks[wk_]; act = {r_[0]: r_[2] for r_ in rows_}
        for r_, mid_, pts_ in rows_:
            o_ = next((x[0] for x in rows_ if x[1] == mid_ and x[0] != r_), None)
            p_ = rec_["wp"].get(str(r_))
            if o_ is not None and p_ is not None and r_ < o_:  # each game once
                graded["games"].append([wk_, p_, 1 if pts_ > act[o_] else 0])
            pj_ = rec_["proj"].get(str(r_))
            if pj_ is not None:
                graded["team_err"].append(abs(pj_ - pts_))
        for pid_, pj_ in rec_["pl"].items():
            a_ = pts.get(pid_, {}).get(f"{season}-{wk_}")
            if a_ is not None:
                graded["pl_err"].append(abs(pj_ - a_))
    G_ = graded["games"]
    bins = []
    for lo_ in (0.0, 0.2, 0.4, 0.6, 0.8):
        b_ = [g for g in G_ if lo_ <= g[1] < lo_ + 0.2 or (lo_ == 0.8 and g[1] == 1.0)]
        if b_:
            bins.append([lo_, round(lo_ + 0.2, 1), len(b_), round(mean_([g[1] for g in b_]), 3), round(mean_([g[2] for g in b_]), 3)])
    # last completed week as it looked before kickoff (from the prediction log), for questions like "was this loss expected?"
    last_pre = None
    if done:
        r_ = PRED["weeks"].get(f"{season}-{done}")
        if r_:
            last_pre = dict(wk=done, wp=r_["wp"].get(str(me)), proj={k_: v_ for k_, v_ in r_["proj"].items()})
    accuracy = dict(flags=flag_bt, last_pre=last_pre, games=len(G_), brier=round(mean_([(g[1] - g[2]) ** 2 for g in G_]), 3) if G_ else None,
                    favorites=round(mean_([1 if (g[1] >= 0.5) == (g[2] == 1) else 0 for g in G_]), 3) if G_ else None, bins=bins,
                    pl_mae=round(mean_(graded["pl_err"]), 2) if graded["pl_err"] else None, pl_n=len(graded["pl_err"]),
                    team_mae=round(mean_(graded["team_err"]), 1) if graded["team_err"] else None,
                    weeks_logged=sorted(PRED["weeks"].keys()), backtest=backtest, sources=src_rep, calib=cal, shape=shape_rep, past_me=past_me, past_bias=past_bias, boom=boom_test,
                    odds=[dict(d=o_["d"], wk=o_["wk"], po=o_["t"].get(str(me), [None, None])[0], t=o_["t"].get(str(me), [None, None])[1]) for o_ in PRED["odds"]])

    data = dict(meta=dict(league=league.get("name"), lid=str(lid), season=season, done=done, fut=fut, reg_end=reg_end, me=me, median=median_on,
                          n_playoff=n_playoff, generated=gen, this_week=this_week, actions=actions,
                          pick_prices={rd: pick_base(season + 1, rd) for rd in range(1, int(settings.get("draft_rounds", 4) or 4) + 1)},
                          rules=dict(best_ball=bool(settings.get("best_ball")), teams=nT, playoff_teams=n_playoff, playoff_start=playoff_start,
                                     playoff_weeks_per_round=2 if settings.get("playoff_round_type") == 2 else 1, reseed=False,
                                     trade_deadline=int(settings.get("trade_deadline", 0) or 0), faab=int(settings.get("waiver_budget", 0) or 0),
                                     # Sleeper can carry taxi/IR settings over from a copied league even when they aren't used (best ball):
                                     # only report them if a roster actually has players there
                                     taxi_slots=int(settings.get("taxi_slots", 0) or 0) if any(t["taxi"] for t in teams) else 0,
                                     ir_slots=int(settings.get("reserve_slots", 0) or 0) if any(t["ir"] for t in teams) else 0,
                                     roster_size=sum(1 for x in league.get("roster_positions", [])), draft_rounds=int(settings.get("draft_rounds", 4) or 4),
                                     median=median_on, pick_trading=bool(settings.get("pick_trading", 1))),
                          ktc_date=ktc.get("date"), values=val_src, backtest=backtest, checks=checks, byes=bye_src, fa_checked=len(fa_pool), faab=int(settings.get("waiver_budget", 0) or 0), sims=SIMS, ledger_through=CFG.get("ledger_through"),
                          scoring=CFG.get("scoring_label", ""), lu=dict(n=LU_N, flex=LU_FLEX, sf=LU_SF, managed=MANAGED), shape=SHAPE, key=DYN, slots=" · ".join(p for p in league.get("roster_positions", []) if p != "BN")),
                teams=tm_out, players=P, weeks=weeks, po_actual={str(k): {str(r): v for r, v in d.items()} for k, d in po_actual.items()}, sched=sched, m26={str(k): v for k, v in my_weekly.items()}, picks=picks,
                advice=advice, outlook=outlook, feed=feed, ledger_now=ledger_now, research=research, defense=DEF, accuracy=accuracy, past_picks=dict(used=used_picks, traded=traded_picks), profiles={str(k): v for k, v in profiles.items()}, rookies=rookies, digest=digest, lineup=lineup_adv,
                analysis=dict(board=board, sugg={str(k): v for k, v in sugg.items()}, needs={str(k): v for k, v in needs.items()},
                              market=market, depth={str(k): v for k, v in depth.items()}, fa=fa, cut=cut,
                              counted={k: [round(v[0], 1), v[1]] for k, v in counted.items()},
                              counted_wk={k: sorted(v) for k, v in counted_wk.items()}))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(data, open(OUT, "w"), separators=(",", ":"))
    json.dump(hist, open(hist_path, "w"), separators=(",", ":"))
    json.dump(alerts, open(al_path, "w"), separators=(",", ":"))
    json.dump(vh, open(vh_path, "w"), separators=(",", ":"))
    json.dump(tvals, open(tv_path, "w"), separators=(",", ":"))
    print(f"wrote {OUT}: week {cur}, done {done}, {len(P)} players, {len(feed)} trades this season, {len(new)} new alerts, backtest {backtest}")


if __name__ == "__main__":
    main()
