# Dynasty HQ

A personal dashboard for a Sleeper superflex best-ball dynasty league.

| Tab | What's there |
|---|---|
| League | Record and playoff/bye/title odds (with trend), this week's matchup, alerts, weekly briefing, standings, matchups, position strength by team, latest trades |
| My Team | Counted points per player, bye and depth check, league rank by position, sell-high candidates |
| Players | Sleeper vs our model vs blend (with backtest accuracy), buy-low targets |
| Trades | Trade Lab (paste an offer, season what-if odds, counter-offers), Partners, League trades (auto-graded), My history |
| Draft | Projected rookie draft order, your picks with early/mid/late pricing, draft capital, rookie board (fills after the NFL draft) |
| Managers | How each manager trades, how their deals aged, needs and roster age |
| Ask | Packages your data with a question and opens it in Claude |

## How it stays current
`.github/workflows/refresh.yml` runs `pipeline/build.py` every 4 hours and every 30 minutes during
Sunday games. It pulls Sleeper and FantasyCalc, rebuilds `docs/data.json`, `history.json`,
`alerts.json` and `digest.md`, and commits them. GitHub Pages serves `docs/`.

Notifications are GitHub issues created by the workflow, so they reach your email and the GitHub
app (you watch your own repo by default):
- **Alerts**: when something changes that affects your team (your player's injury status, a value swing, a trade involving you, a team that just became a buyer at a position you can spare).
- **Weekly digest**: every Tuesday morning. Run the workflow with "Post the weekly digest now" to test.

## How the numbers work
- **Simulation.** Every player's score each remaining week is simulated 600 times: right-skewed (boom weeks happen more than busts), partly shared with teammates, with a 5% chance of missing any game (25% if Questionable). The site re-runs the exact same draws in your browser, so odds match on every page.
- **What a player adds** = how much your best-ball score drops without him across those simulations, so bye/injury cover and boom weeks count. Shown for the full rest of season and for playoff weeks.
- **Trades you're considering** are judged on title chance (with and without the trade, same draws) and trade value. A change only counts if it's bigger than the simulation's margin.
- **Trade values**: FantasyCalc (daily) converted to the KeepTradeCut superflex TE-premium scale using `pipeline/ktc_snapshot.json`. Players and picks share one scale. Refresh the snapshot occasionally to re-tune the conversion; values keep updating either way.
- **Draft order**: reverse of final regular-season standings (points break ties), as your 2026 draft was set.
- **Data check**: every run rebuilds each weekly lineup from Sleeper's per-player points and compares scores; any mismatch shows on the site and in the header.
- **Three-season outlook**: every player's production is carried forward with typical aging by position, young players get credit for the upside their value implies, and each team's picks add rookies. 2027 and 2028 are each played out 200 times. The recommendation and trade checker weigh title chances across all three seasons (later seasons count a bit less), and call your window: contend, contend now/plan ahead, building, retool or rebuild.
- **New seasons**: the pipeline follows your league to its new Sleeper league id automatically, and works in the offseason.

## The recommendation ("What to do" tab)
Every update turns the numbers into one recommendation (make a trade, stay the course, look for an upgrade, or sell for the future), with the specific move and the reasons. It's built from fixed rules over the simulation results, so it works with no setup.
Optional: add a repository secret named `ANTHROPIC_API_KEY` (Settings → Secrets and variables → Actions) and Claude will write the recommendation in plain English from the same facts. It only calls Claude when the recommendation changes (at most about once a day), so the cost is pennies a month. If the call fails, the rules version is shown.

## Setup (one time)
1. Create a **public** repo and push this folder to `main`.
2. Settings → Pages → Source: *Deploy from a branch* → `main`, folder `/docs`.
3. Settings → Actions → General → Workflow permissions: *Read and write*.
4. Put your Pages URL in `config.json` → `site_url` (used for links in alerts and digests).
5. Actions → *Refresh data* → *Run workflow* once to build the first data.

## Configure
- `config.json`: `league_id`, `owner_id` (your Sleeper user id), simulation count, `ledger_through` (date the hand-graded history ends; newer trades are graded automatically).
- `pipeline/ktc_snapshot.json`: optional KTC values (superflex TE++), updated by hand.
- `docs/ledger.json`: hand-graded trade history up to `ledger_through` (itemized values).
- Generated each run: `docs/data.json`, `history.json` (odds over time), `values.json` (player values, 60 days), `trade_values.json` (values when each trade was first seen), `alerts.json`, `digest.md`.

## Run locally
`python pipeline/build.py` (Python 3.10+, no packages needed), then serve `docs/` with any local web server.
