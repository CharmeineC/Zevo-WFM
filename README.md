# WFM Scheduler — BPO Attendance, Leave, OT & Coverage Tracker

A free Streamlit app for workforce/scheduling teams running voice and non-voice accounts.

**Where data is stored:** in a **Google Sheet** you own (recommended — survives restarts, has version history, and your team can open it directly).
If Google isn't set up, the app falls back to a local file, `wfm.db`, which is handy for trying it out on your PC.

## What it does

**Two kinds of login.** Agents see only their own **🕒 My Shift** (log in / break / lunch / log out, their schedule and recent attendance) and **📝 My Requests** (file time off and OT for approval). Admins see everything below plus **🟢 Live Floor** and **🔐 Users**.

| Page | Use it for |
|---|---|
| **Live Floor** | Who's working, on break, over break, late or not logged in right now — refreshes every 30 seconds |
| **Dashboard** | Today's staffing per account: **required HC calculated from volume**, scheduled vs present, lates, absences/NCNS, time off, coverage and OT hours, headcount variance, short intervals, open gaps, 14-day trend |
| **Attendance** | Filled automatically from agents' log ins/outs: late, undertime, breaks, over-breaks and rendered OT. Admins can still type or correct times, and fix clock events (e.g. a forgotten log out) under **Clock logs**. Handles graveyard/overnight shifts (e.g. 22:00–07:00). |
| **Interval Planner** | 30-minute volume forecast, required HC (Erlang C for voice/chat, workload for email/back office), scheduled HC and **HC variance per interval**, projected service level, forecast vs actual, a weekly variance heatmap, and the **🧮 OT plan**: who should do OT and when to fill the short intervals |
| **Schedules** | Weekly roster grid of everyone's permanent schedule (color-coded rest days, time off, RD OT, training). One-off exceptions like training, holidays or swaps can be set here; day-to-day staffing is adjusted with OT. |
| **Time Off** | File time off (counts working days only), cap warning when too many people on one account are off the same day, approve/deny, calendar, days-off summary per contractor. No leave credits — built for independent contractors. |
| **Coverage Finder** | Pick a leave or absence → ranked list of who can cover, with the reason anyone is excluded. One click assigns the cover and logs approved OT. |
| **Overtime** | File and approve OT, weekly OT vs cap, and **unapproved OT** (rendered past shift end without an approved request) to check before payroll |
| **Reports** | Per-agent summary for any date range: attendance %, tardiness %, late minutes, absences, NCNS, undertime, rendered vs approved OT, leave days. CSV export. |
| **Roster & Accounts** | Edit agents and accounts in a spreadsheet-style grid, or bulk-import a CSV |
| **Users** | Create logins for all agents in one click (temporary passwords), add admins, reset passwords, unlock, disable |
| **Settings** | Grace period, rest rules, OT caps, forecast history, site timezone, break/lunch rules; backup/restore; sample data |

### How the coverage finder decides

For the window you need covered, every active agent is checked against:

- not on approved time off that day
- not already scheduled in an overlapping shift (checks the day before and after too, so graveyard shifts are handled)
- at least **min rest hours** (default 8h) between their own shift and the cover window — unless it's back-to-back, which counts as a **shift extension** (pre/post-shift OT), capped at **max hours per stretch** (default 13h)
- weekly approved OT stays under the **weekly OT cap** (default 12h)

Eligible agents are ranked: same account (+3) › required skill match (+2) › same LOB voice/non-voice (+1) › shift extension (+1), minus OT already done this week (spreads OT fairly).
Cover types: **Rest Day OT**, **Pre/Post-shift OT**, or **Same-day OT (separate block)**. You can cover a partial window (e.g. split a 9h shift between two people).

### Interval Planner: how the numbers are made

**1. Forecast volume per 30 min.** For the date you pick, the app takes the same weekday from the last *N* weeks (default 6, changeable) and averages each interval, weighting recent weeks more (newest week counts 6×, oldest 1×). AHT is averaged the same way, weighted by volume. Use **Volume adj. %** to lift or cut the forecast for promos, billing cycles, outages or new launches.

**2. Required HC per interval.**
- **Voice:** Erlang C — the fewest agents that hit your service level (e.g. 80% answered in 20s) without going over max occupancy.
- **Chat:** same, with AHT divided by **chats at once** (concurrency).
- **Email / Back Office:** workload ÷ occupancy (volume × AHT ÷ 1,800s ÷ max occupancy).
- Then **Required HC = on-queue need ÷ (1 − shrinkage)**. Shrinkage covers breaks, lunch, coaching, meetings, system issues, etc. (default 30%).

**3. Scheduled HC per interval.** Everyone on shift for that account in that half hour: default shifts and schedule changes, minus approved leaves and Absent/NCNS, plus approved OT and coverage. Coverage OT counts toward the account being covered. Partial intervals count partially (a 22:15 start counts as 0.5 in the 22:00 interval). Training and holidays are not counted.

**4. Variance = Scheduled HC − Required HC.** Negative (red) = understaffed, positive (green) = overstaffed. **Projected SL** is the service level you'd get if the scheduled agents (after shrinkage) work the forecast.

**5. Daily required HC (Dashboard).** Total interval need ÷ shift length (default 9h incl. lunch, in Settings). Example: if the 48 intervals add up to 76.5 HC-hours of need, required = 76.5 ÷ 9 = 8.5 → **9 people**. Accounts with no volume history yet fall back to the manual number in Accounts.

### OT plan: filling short intervals without changing schedules

Everyone keeps their permanent schedule. **Interval Planner → 🧮 OT plan → Build OT plan** looks at the short intervals and suggests the OT blocks that fill the most shortage for the fewest OT hours:

- **Pre-shift OT** (come in early), **post-shift OT** (stay late, including after a graveyard shift), or **rest-day OT**.
- It only suggests what the rules allow: no overlap with shifts, **min rest** between work stretches (default 8h), **max hours straight** including OT (default 13h), **weekly OT cap** (default 12h), block length limits (default 1–4h pre/post, up to 9h rest-day OT per person per day), and at least **60%** of each block must fall in short intervals so OT isn't wasted on already-covered time.
- People without time off or absences that day only. It prefers the account's own agents and spreads OT to those with less OT this week. Tick **Also use agents from other accounts (same LOB)** or filter by skill if needed.
- Review the list, untick anything you don't want, then **Approve selected OT**, or **Save as Pending** if you need to confirm with agents first (approve later under **Overtime → Approvals & log**). Approved OT immediately shows in the interval view, the dashboard and the attendance sheet (rest-day OT agents appear on the log for that day).
- All limits are adjustable under **Settings → OT plan rules**.

**Account settings** (Roster & Accounts → Accounts): Channel, SL target %, SL seconds, Chats at once, Shrinkage %, Max occupancy %. Blank values use these defaults:

| Channel | SL | Chats at once | Shrinkage | Max occupancy |
|---|---|---|---|---|
| Voice | 80% in 20s | 1 | 30% | 85% |
| Chat | 80% in 60s | 2 | 30% | 85% |
| Email / Back Office | (workload) | 1 | 30% | 90% |

**Loading volume history** (Interval Planner → Volume data): upload a CSV export from your ACD/dialer, chat or ticketing tool with
`date, interval, volume` and optionally `aht` (seconds) and `account`. Common header names like *Calls Offered*, *Contacts*, *Handle Time* are recognized, times like `13:30` or `1:30 PM` both work, and 15-minute data is combined into 30-minute intervals.
Pick the **timezone of the file** (US clients usually report in their own time). The app converts to your site timezone (Settings, default US Central time — America/Chicago), including daylight saving changes. Upload at least 2–3 weeks; 6–8 weeks gives a steadier forecast. Keep adding each week's actuals so the forecast stays current and the day view can show forecast accuracy.

### Logins and the time clock

**First-time setup.** The first person to open a fresh app creates the **admin login**. To make sure nobody else can do that, add a line `setup_code = "pick-a-secret"` to your secrets, at the top next to `sheet_url` (above the `[gcp_service_account]` line — see `secrets.toml.example`); the app then asks for that code before creating the first admin. Add more admins under **Users → Add a user**.

**Agent logins.** Import your roster first, then **Users → Create agent logins**. Each agent gets username = employee ID and a random temporary password, shown once (download the list and send each person theirs privately). They set their own password the first time they sign in. Passwords are stored encrypted (salted PBKDF2), and 5 wrong tries lock the login for 10 minutes (admins can unlock it). If someone forgets their password, an admin uses **Reset password**.

**How the clock matches the schedule.**
- **Log in** attaches to the shift that's starting or running — up to 4 hours early (setting), or earlier if the agent has approved pre-shift OT. Graveyard shifts work across midnight: a 22:00 Mon log in and 07:00 Tue log out both count for Monday's shift.
- **Late** = log in after shift start + grace period. **Undertime** = log out before shift end. **Rendered OT** = time before the shift start or after the shift end, in 30-minute blocks.
- **Breaks:** default 1 × 30-minute break per shift and no lunch (the **Start lunch** button only appears if lunches per shift is set above 0). Minutes beyond the allowance are **over-break**; an extra break beyond the allowed number counts in full. Logging out during a break ends the break. Change the rules under **Settings → Time clock**.
- **Filing OT:** the time must be outside the agent's scheduled shift — overlapping a shift, choosing Rest Day OT on a working day (or pre/post-shift OT on a rest day), filing during approved time off, or overlapping OT they already filed is rejected with a message.
- **Filing time off:** it can't start or end on a rest day or holiday, and rest days inside a range aren't counted. 1 or 2 rest days (or more) per agent are both fine — list them in the roster's rest days column, e.g. `Sun` or `Sat,Sun`.
- **Log out → log back in** (disconnection) keeps the first log in time. Logging in with no shift scheduled (e.g. a rest day without approved RD OT) is recorded as **Unscheduled**.
- **No-shows:** agents who have a login and never log in are marked **Absent ("Auto: no log in")** one hour after their shift ends. Agents without a login are never auto-marked. Turn this off in Settings if you need to.
- **Forgot to log out:** the next log in flags the old shift **"No log out"**. Fix it in **Attendance → Clock logs → Add a missing event**; attendance recalculates.
- All clock times use the **site timezone** in Settings (default US Central — America/Chicago, daylight saving handled), even though the cloud server runs on UTC.

**Staying signed in.** Refreshing the page or reopening the link keeps you signed in for 12 hours (Settings → Time clock → *Stay signed in…*; 0 turns it off). It uses a browser cookie holding a random key — only a scrambled copy is stored in the sheet's `sessions` tab, so the sheet can't be used to sign in as someone. **Sign out** ends it immediately, and resetting a password, disabling or deleting a login ends that person's sessions everywhere. **On shared computers, agents must click Sign out at the end of their shift**, or the next person could use their session.

**Notes.** Agents' clicks are saved to the Google Sheet in small batches every few seconds, so 20+ agents logging in at once stay well within Google's limits; admin actions save immediately.

### Attendance rules

- **Late**: time in later than shift start + grace period (default 5 min). Late minutes counted from shift start.
- **Undertime**: time out before shift end.
- **Rendered OT**: time out after shift end, rounded down to OT blocks (default 30 min).
- Leave **Status** blank and it auto-tags Present / Late / Undertime. Choose Absent, NCNS, Half-day, etc. to override.
- The **shift date** is the date the shift starts (a 22:00 Monday shift ending 07:00 Tuesday is logged under Monday).

## Run it on your computer

```bash
pip install -r requirements.txt
streamlit run app.py
```

It opens at http://localhost:8501. Create the admin login when asked, then go to **Settings → Load sample data** to try it with 3 accounts and 22 agents, then **Wipe all data** when you're ready to load your real roster.

To share it on your office network, run `streamlit run app.py --server.address 0.0.0.0` and teammates open `http://<your-PC-IP>:8501`.

## Connect Google Sheets (one-time, ~10 minutes, free)

The app logs in to Google as a **service account**: a robot Google user that only sees the sheets you share with it.

**A. Create the service account and key**

1. Go to https://console.cloud.google.com and sign in with the Google account that should own the data.
2. Top bar → project picker → **New project** (name it e.g. `wfm-scheduler`) → Create, and make sure it's selected.
3. Menu → **APIs & Services → Library**. Search and **Enable** both **Google Sheets API** and **Google Drive API**.
4. Menu → **IAM & Admin → Service Accounts → Create service account**. Name it `wfm-app` → **Done** (no roles needed).
5. Click the new service account → **Keys** tab → **Add key → Create new key → JSON**. A `.json` file downloads.
   Keep it private — it's a password. Copy the `client_email` inside it (looks like `wfm-app@wfm-scheduler.iam.gserviceaccount.com`).

> No billing or credit card is needed for this. If your company Google Workspace blocks "service account key creation", use a personal Gmail account for the project instead, or ask IT.

**B. Create the sheet**

1. Create a blank Google Sheet, e.g. **WFM Data**.
2. Click **Share**, paste the service account's `client_email`, give it **Editor**, untick "Notify people", Share.
3. Copy the sheet's URL.

The app creates all the tabs it needs on first run: `accounts`, `agents`, `schedule_overrides`, `attendance`, `leaves`, `ot_requests`, `settings`, `interval_volume`, `users`, `time_logs`, `sessions`. The `users` tab holds encrypted passwords only — still, share the sheet with admins only.

**C. Give the app the key**

Create a file `.streamlit/secrets.toml` next to `app.py` (see `secrets.toml.example`):

```toml
sheet_url = "https://docs.google.com/spreadsheets/d/XXXXXXXX/edit"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "wfm-app@wfm-scheduler.iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
universe_domain = "googleapis.com"
```

Copy each value from the downloaded JSON key. Keep the `\n` in `private_key` exactly as they appear in the JSON.
Run the app — the sidebar shows **🟢 Google Sheet: WFM Data** when it's connected.

### How the sheet sync works

- On start, the app reads the whole sheet into a fast local cache. Every save writes the changed tab back to the sheet right away.
- You and your TLs can open the sheet to view, filter or fix data. The app re-reads it every 2 minutes, or immediately with **🔄 Reload from Google Sheet** in the sidebar.
  Keep the header row and tab names as they are, and avoid editing a tab in the sheet while someone is saving the same thing in the app (the app's save rewrites that tab).
- Made a mistake? **File → Version history** in Google Sheets restores any earlier state.
- Google Sheets holds up to 10 million cells. A 300-agent site logs about 1M attendance cells a year, which is fine — but saves get slower as the attendance tab grows, so copy the old year to an archive sheet and clear it at the start of each year.

## Put it online for free (Streamlit Community Cloud)

1. Create a free GitHub account, make a **private** repository, and upload `app.py`, `requirements.txt` and `.gitignore`.
   **Never upload `secrets.toml` or the JSON key** to GitHub.
2. Go to https://share.streamlit.io, sign in with GitHub, click **Create app**, pick the repo and `app.py`.
3. Before deploying, open **Advanced settings → Secrets** and paste the contents of your `secrets.toml`. Deploy.
4. Decide who can open the link. Every page is behind the app's own login, so you can share the link with your agents. For an extra layer, keep the app **private** in its Streamlit sharing settings and invite each person's email — note that Streamlit's free tier allows one private app per account.
5. Optional: also upload the `.streamlit/config.toml` file (keep the folder name) — it hides Streamlit's developer menu from agents.

Because the data lives in Google Sheets, nothing is lost when the free app sleeps or restarts.
(Without Google Sheets, the cloud app's local `wfm.db` is wiped on every restart — you'd have to download a backup from **Settings** each shift.)

## Getting your real data in

1. **Roster & Accounts → Accounts**: add each account with LOB, channel (Voice / Chat / Email), service level target and shrinkage, and max % allowed off the same day.
2. **Roster & Accounts → Import CSV**: download the template, fill it from your HR roster, upload.
   Rest days use `Mon,Tue,Wed,Thu,Fri,Sat,Sun` (comma-separated). Shift times are 24-hour `HH:MM`.
3. **Interval Planner → Volume data**: upload 6–8 weeks of interval volume per account so required HC can be calculated.
4. **Users → Create agent logins**, then send each agent their username and temporary password.

## Daily routine for the scheduler

1. Keep **Live Floor** open: chase anyone late or over break.
2. Start of shift — **Dashboard**: check open gaps and headcount variance.
3. **Interval Planner → 🧮 OT plan**: build the OT plan for today (and tomorrow), approve it or send it to agents to confirm. The week heatmap shows which days will need the most OT.
4. **Coverage Finder**: fill a specific person's full shift when they're off or absent.
5. After the shift — **Attendance**: review lates, over-breaks, "No log out" and auto-absences; fix clock mistakes under **Clock logs** (change Absent to NCNS where it applies).
6. **Time Off / Overtime**: approve pending requests.
7. Weekly/cut-off — **Reports**: export for payroll and coaching.
