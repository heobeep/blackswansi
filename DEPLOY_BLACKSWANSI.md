# Deploy BlackSwanSI on Render (free plan), step by step

**What you get:** a Telegram bot + Mini App dashboard running in the cloud for $0, in **paper mode**
(simulated money). Upgrade to a paid plan later when you need it (Part 9).

## What to know before you start (honest limits of the free plan)
* Free instances **sleep after ~15 minutes without traffic** and **lose their files on every restart**.
  BlackSwanSI works around both: it pings itself, and it keeps an **encrypted backup of its data pinned in your
  private Telegram chat** that it restores automatically. A free outside pinger (Part 6) is the backstop.
* This is good enough for **paper trading and a hobby**. It is **not** recommended for real money. Upgrade first (Part 9).
* Render gives free accounts a monthly allowance of instance hours (about enough for one service running all
  month). If you run other free services, you can run out.
* You need **a computer for about 10 minutes** for Part 3 (uploading folders to GitHub is awkward on a phone).
  Everything else can be done from a phone.

## You will need
1. A **Telegram** account.  2. A **GitHub** account (github.com, free).  3. A **Render** account (render.com, free).
4. The `blackswansi.zip` file.

---
## Part 1: Create your Telegram bot (phone is fine)
1. In Telegram, open **@BotFather** and send `/newbot`.
2. Give it a name (e.g. *BlackSwanSI*) and a username ending in `bot` (e.g. `MyBlackSwanSI_bot`).
3. BotFather replies with a **token** like `123456789:AAH...`. Copy it somewhere private. This is your `BOT_TOKEN`.
4. Open **@userinfobot** and send `/start`. It replies with your numeric **Id** (e.g. `987654321`). This is your `ADMIN_IDS`.

## Part 2: Unzip the project (computer)
Unzip `blackswansi.zip`. Inside the folder you should see `Dockerfile`, `render.yaml`, `requirements.txt`
and a folder named `app`.

## Part 3: Put the code on GitHub (computer)
1. On github.com click **+ → New repository**. Name it `blackswansi`. Choose **Private**. Click **Create repository**.
2. On the new repo page click **uploading an existing file**.
3. Open the unzipped folder, **select everything inside it** (not the folder itself) and **drag it into the browser**.
   Make sure the `app` folder comes along with its sub-folders.
4. Scroll down, click **Commit changes**.
5. **Check:** on the repo's main page you must see `Dockerfile` and `render.yaml` **at the top level** (not inside
   another folder). If they are inside a folder, redo the upload with the folder's contents.
6. Never upload a file named `.env`.

## Part 4: Deploy on Render (phone is fine)
1. Go to **dashboard.render.com** and log in (sign up with GitHub for the easiest setup).
2. Click **New + → Blueprint**.
3. Connect your GitHub account, allow access to the `blackswansi` repo, select it, click **Connect**.
4. Render reads `render.yaml` and shows a service called **blackswansi** on the **Free** plan.
5. It asks for two values:
   * `BOT_TOKEN` → paste the token from Part 1.
   * `ADMIN_IDS` → paste your numeric Id from Part 1.
6. Click **Apply / Deploy Blueprint**. The first build takes about 5–10 minutes. Wait for the status **Live**.

## Part 5: First start and safety copy (phone is fine)
1. On the service page, copy the address at the top (like `https://blackswansi-xxxx.onrender.com`).
2. Open `https://blackswansi-xxxx.onrender.com/health` in your browser. You should see `{"status":"ok", ...}`.
   (The first request after sleep can take 30–60 seconds.)
3. **Save your encryption key:** in Render open the service → **Environment** → find `ENCRYPTION_KEY` → reveal and
   copy it into a password manager. **If you lose it, your backups and stored API keys cannot be read.**
4. In Telegram open your bot and send **/start**. You are the admin, so you are allowed automatically.
   (This also lets the bot send you its encrypted backups. Backups cannot start until you do this.)

## Part 6: Keep it awake (free outside pinger, phone is fine)
The bot pings itself, but Render may still restart it, so add a free backstop:
1. Go to **uptimerobot.com**, create a free account, click **+ New monitor**.
2. Type: **HTTP(s)**. URL: `https://blackswansi-xxxx.onrender.com/health`. Interval: **5 minutes**. Save.
This also emails you if the bot goes down.

## Part 7: Try it (paper mode)
In your Telegram chat with the bot:
1. `/setcapital 1000` (simulated money; $300+ is needed for positions to be large enough).
2. `/starttrade`
3. Within an hour it will make its first check. `/holdings` shows what it holds. Holding **100% cash is normal**
   when the market is in a downtrend.
4. Tap the dashboard button to see the Mini App.
5. `/setrisk` shows the risk options (conservative, balanced, growth).

## Part 8: Prove it survives a restart (do this once)
1. In Render open the service → **Manual Deploy → Deploy latest commit**.
2. When it is Live again, send `/holdings` and `/status`. Your capital and holdings must be **unchanged**.
3. You should also receive a message: *"♻️ BlackSwanSI restarted and restored its data from the latest backup."*
If not, check **Logs** and the troubleshooting table below.

## Part 9: Upgrade later (when you need it)
Upgrade when you want real money, no sleeping, or more users.
1. Render → your service → **Settings → Instance Type** → choose a paid type (e.g. **Starter**).
2. **Disks → Add Disk**: name `botdata`, mount path `/data`, size 1 GB.
3. **Environment**: set `DATABASE_PATH` = `/data/trading_bot.db`, and `KEEPALIVE` = `false` (optional).
4. Click **Manual Deploy**. On first start the new empty disk is filled automatically from your Telegram backup,
   so nothing is lost.
Keep the same `ENCRYPTION_KEY`. (`render.paid.yaml` in the project contains this configuration for reference.)

## Part 10: Going live with real money (later, optional)
Do this only after weeks of good paper results **and on a paid plan**.
1. In Binance create an API key: **Spot trading ON, withdrawals OFF**, and restrict it to the IP addresses Render shows
   for your service (service page → Connect → Outbound). Update it if Render changes them.
2. In Render Environment set `ALLOW_LIVE_TRADING` = `true` and redeploy.
3. In Telegram send `/setkeys <API_KEY> <API_SECRET>` (the bot deletes your message and rejects keys that allow withdrawals).
4. Start with about **$20** and watch the first trades. Keep `ENABLE_EARN` = `false` until that works cleanly.

## Everyday use
* **Stop trading:** `/stoptrade`. **Stop the server:** Render → service → Suspend.
* **Update code:** change files on GitHub, then **Manual Deploy** in Render (auto-deploy is off on purpose).
* **Never share** your `BOT_TOKEN`, `ENCRYPTION_KEY` or Binance keys.

## Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Deploy fails, log says `BOT_TOKEN is required` / `ADMIN_IDS is required` | You skipped Part 4 step 5. Add them under Environment, redeploy |
| Build fails: "Dockerfile not found" | Files are inside a folder in the repo. Redo Part 3 step 3 |
| `/health` slow or times out the first time | The free instance was asleep; it needs up to a minute to wake |
| Bot does not answer `/start` | Service not Live yet, or `BOT_TOKEN` wrong. Check Logs |
| No backup message ever arrives | You have not sent `/start` to the bot yet (Part 5 step 4) |
| After a restart the data is gone | You changed `ENCRYPTION_KEY` after backups were made. Restore the old key |
| Logs show `HTTP 451` | Binance blocks the region. Recreate the service in Singapore or Frankfurt |
| App refuses to start: "Could not check for a database backup" | Telegram was unreachable; Render retries automatically. It protects your data on purpose |
| `/setkeys` rejected | The key allows withdrawals, or the IP restriction does not match |

## Free plan limits in one line
Sleeps if untouched, can restart anytime, shares monthly hours, and data lives in your encrypted Telegram backup.
Fine for learning. Upgrade before real money.
