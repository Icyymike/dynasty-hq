# Going live (about 15 minutes, one time)

You need a free GitHub account. Everything runs on GitHub for free: it pulls fresh data from Sleeper on a schedule and hosts the app at your own web address.

## 1. Make the repository
1. Go to github.com → **+** (top right) → **New repository**.
2. Name it `dynasty-hq`. Choose **Public** (free hosting needs public). Don't add a README. Click **Create repository**.

## 2. Upload the files
1. Unzip `dynasty-hq.zip` on your computer.
2. On the new repo page click **uploading an existing file**.
3. Drag in everything inside the unzipped folder (`docs`, `pipeline`, `setup`, `config.json`, `README.md`, `GO-LIVE.md`). Click **Commit changes**.
4. The scheduler file lives in a hidden folder that Macs and Windows often skip when dragging, so add it by hand:
   **Add file → Create new file**, type the name exactly `.github/workflows/refresh.yml`,
   open `setup/refresh.yml` from the zip, copy all of it, paste, then **Commit changes**.

## 3. Turn on the website
**Settings → Pages** → Source: **Deploy from a branch** → Branch: **main**, folder **/docs** → **Save**.
After a minute it shows your address, like `https://YOURNAME.github.io/dynasty-hq/`.

## 4. Let the updater save data
**Settings → Actions → General** → scroll to **Workflow permissions** → pick **Read and write permissions** → **Save**.

## 5. (Optional) Claude-written advice
The app works without this. With it, the "What to do" tab is written up by Claude each day.
Get a key at console.anthropic.com (API usage is billed separately from your Claude plan), then
**Settings → Secrets and variables → Actions → New repository secret**: name `ANTHROPIC_API_KEY`, paste the key.
Never paste the key into any file in the repo.

## 6. Put your address in the config
Open `config.json` in the repo → pencil icon → set `"site_url": "https://YOURNAME.github.io/dynasty-hq/"` → **Commit changes**.

## 7. Run it the first time
**Actions** tab → if asked, click **I understand my workflows, enable them** → **Refresh data** → **Run workflow**.
It takes about 2–5 minutes. A green check means it worked.

## 8. Check it against Sleeper
Open your address. At the very bottom of the page you should see the update time and **"Data check passed"** (if it says the check failed, a red chip also appears at the top).
Spot-check: your record and last week's score should match Sleeper exactly.
If the run shows a red X, open it, copy the error text and send it to Claude.

## After that
It updates itself: every 4 hours, every 30 minutes while games are on, and a weekly summary every Tuesday
(posted as an issue in the repo, so GitHub emails it to you if you "Watch" the repo).
At the end of the season it follows your league into next year automatically.
Phone tip: open the address in Safari → Share → **Add to Home Screen**, and it works like an app.
