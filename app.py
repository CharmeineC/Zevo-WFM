"""
BPO Workforce Scheduler (WFM) — Streamlit + Google Sheets (or local SQLite)
Tracks schedules, attendance (lates / absences / undertime / OT), leaves,
OT requests, and recommends who can cover leaves and absences.

Storage:
  * If .streamlit/secrets.toml (or Streamlit Cloud "Secrets") has
    [gcp_service_account] + sheet_url, all data lives in that Google Sheet
    (one tab per table). The app keeps a fast local cache and writes changes
    back to the sheet after every save.
  * Otherwise it uses a local SQLite file, wfm.db.

Run:  streamlit run app.py
"""
import datetime as dt
import hashlib
import inspect
import json
import hmac
import io
import math
import os
import random
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from collections import Counter
from contextlib import closing
from zoneinfo import ZoneInfo

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


def secret(key):
    """Read a top-level secret; also accept it if it was pasted inside the [gcp_service_account] section."""
    try:
        if key in st.secrets:
            return st.secrets[key]
        sa = st.secrets.get("gcp_service_account")
        if sa is not None and key in sa:
            return sa[key]
    except Exception:  # no secrets file at all
        pass
    return None


def _service_account():
    """The Google key: either the whole JSON key file pasted as gcp_json, or the [gcp_service_account] section."""
    raw = secret("gcp_json")
    if raw:
        try:
            info = json.loads(str(raw).strip())
        except ValueError as e:
            st.error("The `gcp_json` in your Secrets isn't valid. Paste the **entire** contents of the Google key "
                     f"file between the three quote marks `\'\'\'` — from the first `{{` to the last `}}`. ({e})")
            st.stop()
        if info.get("type") != "service_account" or "private_key" not in info:
            st.error("The `gcp_json` in your Secrets doesn't look like a Google service-account key file.")
            st.stop()
        return info
    try:
        if "gcp_service_account" in st.secrets:
            return {k: v for k, v in dict(st.secrets["gcp_service_account"]).items()
                    if k not in ("sheet_url", "sheet_key", "setup_code")}
    except Exception:
        pass
    return None


def _sheet_secrets():
    sa = _service_account()
    if sa is None:
        return None
    url, key = secret("sheet_url"), secret("sheet_key")
    if not (url or key):
        st.error("Your Secrets have the Google key but no `sheet_url`. Add `sheet_url = \"https://docs.google.com/"
                 "spreadsheets/d/...\"` as the first line of Secrets. Nothing is being saved until then.")
        st.stop()
    return {"gcp_service_account": sa, **({"sheet_url": url} if url else {"sheet_key": key})}


SECRETS = _sheet_secrets()
USE_SHEETS = SECRETS is not None
ON_CLOUD = os.getcwd().startswith("/mount/src") or os.path.exists("/mount/src")  # Streamlit Community Cloud
if USE_SHEETS:
    _sid = str(SECRETS.get("sheet_url") or SECRETS.get("sheet_key"))
    DB_PATH = os.path.join(tempfile.gettempdir(),
                           f"wfm_cache_{hashlib.md5(_sid.encode()).hexdigest()[:10]}.db")
else:
    DB_PATH = os.environ.get("WFM_DB", "wfm.db")
TABLES = ["accounts", "agents", "schedule_overrides", "attendance", "leaves", "ot_requests", "settings",
          "interval_volume", "users", "time_logs", "sessions", "schedule_changes"]
APPEND_TABLES = {"time_logs"}  # insert-only logs are appended to the sheet instead of rewriting the tab
_WRITE_RE = re.compile(r"^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.I)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ONE_DAY = dt.timedelta(days=1)
TIME_RE = r"^$|^([01]\d|2[0-3]):[0-5]\d$"
LOBS = ["Voice", "Non-Voice"]
LEAVE_TYPES = [  # contractors: time off is unpaid, so no credits/balances are tracked
    "Planned Time Off (PTO)", "Sick (SL)", "Emergency (EL)", "Bereavement (BL)",
    "Personal / Other (OFF)",
]
ATT_STATUS = ["", "Present", "Late", "Undertime", "Half-day", "Absent", "NCNS", "On Leave", "Rest Day"]
PRESENT_STATUS = ["Present", "Late", "Undertime", "Half-day"]
ABSENT_STATUS = ["Absent", "NCNS"]
OVERRIDE_KINDS = ["Work", "Rest Day", "Training", "Holiday", "Work (RD OT)"]
WORK_KINDS = ("Work", "Work (RD OT)", "Training")
OT_TYPES = ["Pre-shift OT", "Post-shift OT", "Rest Day OT", "Holiday OT", "Coverage OT"]
DEFAULT_SETTINGS = {
    "grace_min": "5",          # minutes of grace before tagged late
    "min_rest_hrs": "8",       # minimum rest between shifts for coverage
    "max_daily_hrs": "13",     # max shift + OT hours in one stretch (incl. break)
    "max_weekly_ot_hrs": "12", # weekly OT cap per agent
    "ot_block_min": "30",      # rendered OT counted in blocks of N minutes
    "forecast_weeks": "6",     # same-weekday history used for the interval forecast
    "shift_hours": "9",        # hours a person is on site per shift (incl. lunch) -> daily HC from volume
    "ot_min_block_hrs": "1",   # shortest OT block the OT plan will offer
    "ot_max_ext_hrs": "4",     # longest pre/post-shift extension
    "rd_ot_max_hrs": "9",      # longest rest-day OT block
    "ot_min_eff_pct": "60",    # an OT block must spend at least this % of its time in short intervals
    "site_tz": "America/Chicago",  # timezone schedules and the time clock follow
    "break_min": "30",         # minutes allowed per break
    "breaks_allowed": "1",     # breaks per shift
    "lunch_min": "60",         # minutes allowed for lunch
    "lunches_allowed": "0",    # 0 = no lunch (the Start lunch button is hidden)
    "login_early_hrs": "4",    # how early before a shift an agent can log in to it
    "auto_absent": "1",        # mark agents with a login Absent when they never log in for a shift
    "remember_hrs": "12",      # stay signed in through page refreshes for this many hours (0 = off)
}
CHANNELS = ["Voice", "Chat", "Email / Back Office"]
INTERVAL_MIN = 30
INTERVALS = [f"{h:02d}:{m:02d}" for h in range(24) for m in range(0, 60, INTERVAL_MIN)]
# per-channel defaults: SL %, SL seconds, concurrency, shrinkage %, max occupancy %
CHANNEL_DEFAULTS = {"Voice": (80, 20, 1, 30, 85), "Chat": (80, 60, 2, 30, 85),
                    "Email / Back Office": (90, 86400, 1, 30, 90)}
ACCOUNT_EXTRA_COLS = {"channel": "TEXT", "sl_target_pct": "REAL", "sl_seconds": "REAL", "concurrency": "REAL",
                      "shrinkage_pct": "REAL", "max_occupancy_pct": "REAL"}

st.set_page_config(page_title="WFM Scheduler", page_icon="🗓️", layout="wide")

# ----------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  name TEXT PRIMARY KEY, lob TEXT NOT NULL DEFAULT 'Voice',
  required_hc INTEGER DEFAULT 0, max_leave_pct REAL DEFAULT 10,
  channel TEXT, sl_target_pct REAL, sl_seconds REAL, concurrency REAL,
  shrinkage_pct REAL, max_occupancy_pct REAL);
CREATE TABLE IF NOT EXISTS interval_volume(
  account TEXT, date TEXT, interval TEXT, volume REAL, aht REAL,
  PRIMARY KEY(account, date, interval));
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, emp_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  account TEXT, lob TEXT, team_lead TEXT, skills TEXT DEFAULT '',
  shift_start TEXT DEFAULT '22:00', shift_end TEXT DEFAULT '07:00', rest_days TEXT DEFAULT 'Sat,Sun',
  vl_credits REAL DEFAULT 0, sl_credits REAL DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS schedule_overrides(
  agent_id INTEGER, date TEXT, kind TEXT, shift_start TEXT, shift_end TEXT, note TEXT,
  PRIMARY KEY(agent_id, date));
CREATE TABLE IF NOT EXISTS attendance(
  agent_id INTEGER, date TEXT, time_in TEXT, time_out TEXT, status TEXT,
  late_min INTEGER DEFAULT 0, undertime_min INTEGER DEFAULT 0, ot_min INTEGER DEFAULT 0,
  remarks TEXT, break_min INTEGER DEFAULT 0, overbreak_min INTEGER DEFAULT 0, PRIMARY KEY(agent_id, date));
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'agent', agent_id INTEGER,
  active INTEGER DEFAULT 1, must_change INTEGER DEFAULT 1, failed INTEGER DEFAULT 0, locked_until TEXT,
  last_login TEXT, created_on TEXT);
CREATE TABLE IF NOT EXISTS schedule_changes(
  agent_id INTEGER, effective_from TEXT, shift_start TEXT, shift_end TEXT, rest_days TEXT, note TEXT,
  PRIMARY KEY(agent_id, effective_from));
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, username TEXT, created TEXT, expires TEXT);
CREATE TABLE IF NOT EXISTS time_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, shift_date TEXT, event TEXT, ts TEXT,
  source TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS leaves(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, leave_type TEXT,
  start_date TEXT, end_date TEXT, days REAL, status TEXT DEFAULT 'Pending',
  reason TEXT, filed_on TEXT, decided_by TEXT);
CREATE TABLE IF NOT EXISTS ot_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, date TEXT, ot_type TEXT,
  start_time TEXT, end_time TEXT, hours REAL, reason TEXT, status TEXT DEFAULT 'Pending',
  covering_for INTEGER, filed_on TEXT, for_account TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA foreign_keys=ON")
    return c


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def q(sql, params=()):
    with closing(_conn()) as c:
        return pd.read_sql_query(sql, c, params=[_py(p) for p in params])


def ex(sql, params=()):
    m = _WRITE_RE.match(sql)
    with (STORE.lock if STORE is not None else _NoLock()):  # no sheet reload can wipe a write in progress
        with closing(_conn()) as c, c:
            cur = c.execute(sql, [_py(p) for p in params])
            changed, rowid = cur.rowcount, cur.lastrowid
        if STORE is not None and m and changed:
            table = m.group(1)
            if table in APPEND_TABLES and re.match(r"^\s*INSERT\s+INTO", sql, re.I):
                STORE.queue_append(table, rowid)
            else:
                STORE.mark_dirty(table)
    return rowid


def exmany(sql, rows):
    """Bulk version of ex() for imports."""
    rows = [[_py(v) for v in r] for r in rows]
    if not rows:
        return
    with (STORE.lock if STORE is not None else _NoLock()):
        with closing(_conn()) as c, c:
            c.executemany(sql, rows)
        m = _WRITE_RE.match(sql)
        if STORE is not None and m:
            STORE.mark_dirty(m.group(1))


def _py(v):
    """Convert numpy / pandas scalars to plain Python for sqlite."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if hasattr(v, "item"):
        return v.item()
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _cell(v):
    """Value as written to Google Sheets."""
    v = _py(v)
    return "" if v is None else v


class SheetStore:
    """Google Sheet <-> local SQLite cache. One worksheet (tab) per table.

    `lock` guards the local cache and the change lists (held only briefly).
    `io_lock` serialises network work, so a reload can never overwrite changes that are still being saved.
    A background thread saves changes every few seconds, batching bursts (e.g. 20 agents logging in at once)."""
    REFRESH_SEC = 120  # pick up edits made directly in the sheet
    FLUSH_SEC = 4

    def __init__(self, secrets):
        import gspread
        gc = gspread.service_account_from_dict(dict(secrets["gcp_service_account"]))
        self.sh = gc.open_by_url(secrets["sheet_url"]) if "sheet_url" in secrets else gc.open_by_key(secrets["sheet_key"])
        self.title = self.sh.title
        self.lock, self.io_lock = threading.RLock(), threading.RLock()
        self.dirty, self.appends, self.ws, self.nrows = set(), {}, {}, {}
        self.loaded_at, self.last_error, self.last_saved = 0.0, None, None
        self.load(force=True)
        threading.Thread(target=self._loop, daemon=True, name="sheet-flusher").start()

    @staticmethod
    def cols(table):
        with closing(_conn()) as c:
            return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]

    @property
    def pending(self):
        return bool(self.dirty or any(self.appends.values()))

    def _loop(self):
        while True:
            time.sleep(self.FLUSH_SEC)
            if self.pending:
                try:
                    self.flush()
                except Exception as e:  # kept pending; retried next round
                    self.last_error = f"{type(e).__name__}: {e}"
                    time.sleep(self.FLUSH_SEC * 3)

    def _ensure_tabs(self):
        existing = {w.title: w for w in self.sh.worksheets()}
        for t in TABLES:
            if t not in existing:
                cols = self.cols(t)
                w = self.sh.add_worksheet(title=t, rows=100, cols=max(len(cols), 1))
                w.update(range_name="A1", values=[cols], value_input_option="RAW")
                try:
                    w.freeze(rows=1)
                except Exception:
                    pass
                existing[t] = w
            self.ws[t] = existing[t]
            self.nrows[t] = existing[t].row_count

    def load(self, force=False):
        """Replace the local cache with what's in the Google Sheet (skipped while changes are unsaved)."""
        with self.io_lock:
            if self.pending and not force:
                return False
            self._ensure_tabs()
            res = self.sh.values_batch_get(TABLES)
            with self.lock:
                if self.pending and not force:
                    return False
                with closing(_conn()) as c, c:
                    for t, vr in zip(TABLES, res.get("valueRanges", [])):
                        vals = vr.get("values", [])
                        c.execute(f"DELETE FROM {t}")
                        if len(vals) < 2:
                            continue
                        header = [str(h).strip() for h in vals[0]]
                        pairs = [(col, header.index(col)) for col in self.cols(t) if col in header]
                        if not pairs:
                            continue
                        rows = []
                        for row in vals[1:]:
                            if not any(str(x).strip() for x in row):
                                continue
                            rows.append([row[i] if i < len(row) and str(row[i]).strip() != "" else None
                                         for _, i in pairs])
                        names = ",".join(col for col, _ in pairs)
                        marks = ",".join("?" * len(pairs))
                        c.executemany(f"INSERT OR REPLACE INTO {t}({names}) VALUES({marks})", rows)
                self.dirty.clear()
                self.appends.clear()
                self.loaded_at = time.time()
            return True

    def mark_dirty(self, table):
        if table in TABLES:
            with self.lock:
                self.dirty.add(table)

    def queue_append(self, table, rowid):
        with self.lock:
            self.appends.setdefault(table, []).append(rowid)

    def flush(self):
        """Write changed tabs back to the sheet (full rewrite, or append for insert-only logs)."""
        with self.io_lock:
            with self.lock:  # snapshot under the data lock, then talk to Google without holding it
                rewrites = {}
                for t in sorted(self.dirty):
                    df = q(f"SELECT * FROM {t}")
                    rewrites[t] = (list(df.columns), [[_cell(v) for v in r] for r in df.itertuples(index=False)])
                appends = {}
                for t, ids in self.appends.items():
                    if ids and t not in rewrites:
                        marks = ",".join("?" * len(ids))
                        df = q(f"SELECT * FROM {t} WHERE id IN ({marks}) ORDER BY id", ids)
                        appends[t] = (list(df.columns), [[_cell(v) for v in r] for r in df.itertuples(index=False)])
                done_ids = {t: list(v) for t, v in self.appends.items()}
                self.dirty.clear()
                self.appends.clear()
            try:
                for t, (cols, rows) in rewrites.items():
                    values = [cols] + rows
                    m = len(cols)
                    values += [[""] * m] * max(0, 2 - len(values))  # a tab keeps >=2 rows; blank the spare one
                    n = len(values)
                    ws = self.ws[t]
                    if self.nrows[t] < n or ws.col_count < m:
                        ws.resize(rows=max(self.nrows[t], n), cols=max(ws.col_count, m))
                        self.nrows[t] = max(self.nrows[t], n)
                    ws.update(range_name="A1", values=values, value_input_option="RAW")
                    if self.nrows[t] > max(n, 2):  # trim rows left over from deleted records
                        ws.resize(rows=max(n, 2))
                        self.nrows[t] = max(n, 2)
                    rewrites[t] = None
                for t, (cols, rows) in appends.items():
                    if rows:
                        ws = self.ws[t]
                        if ws.col_count < len(cols):
                            ws.resize(cols=len(cols))
                        ws.append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS",
                                       table_range="A1")
                        self.nrows[t] += len(rows)
                    done_ids.pop(t, None)
            except Exception:
                with self.lock:  # put back whatever didn't make it
                    self.dirty.update(t for t, v in rewrites.items() if v is not None)
                    for t, ids in done_ids.items():
                        if t in appends or t in rewrites:
                            self.appends.setdefault(t, [])[:0] = ids
                raise
            self.last_error, self.last_saved = None, time.time()

    def maybe_refresh(self):
        if not self.pending and time.time() - self.loaded_at > self.REFRESH_SEC:
            self.load()


def init_db():
    with closing(_conn()) as c, c:
        c.execute("PRAGMA journal_mode=WAL")  # many agents reading/writing at once
        c.executescript(SCHEMA)
        for table, extra in (("accounts", ACCOUNT_EXTRA_COLS), ("ot_requests", {"for_account": "TEXT"}),
                             ("attendance", {"break_min": "INTEGER", "overbreak_min": "INTEGER"})):
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, typ in extra.items():  # upgrade databases made by older versions
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def ensure_default_settings():
    for k, v in DEFAULT_SETTINGS.items():
        ex("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))


@st.cache_resource(show_spinner="Loading data from Google Sheets…")
def get_store():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    return SheetStore(SECRETS)


def flush_to_sheet(sync=None):
    """Save pending changes. Admin actions save right away; agent clicks are batched by the background saver."""
    if STORE is None or not STORE.pending:
        return True
    if sync is None:
        sync = is_admin()
    if not sync:
        return True
    try:
        STORE.flush()
        return True
    except Exception as e:  # kept pending; the background saver retries
        STORE.last_error = str(e)
        st.error(f"Couldn't save to Google Sheets yet (it will keep retrying automatically): {e}")
        return False


def rerun():
    """Save pending changes to the sheet, then refresh the page."""
    flush_to_sheet()
    st.rerun()


def upsert_account(r):
    """Insert/update an account, filling blank interval settings with channel defaults."""
    def val(k, default):
        v = r.get(k)
        return default if v is None or (isinstance(v, float) and pd.isna(v)) or v == "" else v
    lob = val("lob", "Voice")
    channel = val("channel", "Voice" if lob == "Voice" else "Chat")
    sl, sls, conc, shr, occ = CHANNEL_DEFAULTS.get(channel, CHANNEL_DEFAULTS["Voice"])
    ex("INSERT OR REPLACE INTO accounts(name,lob,required_hc,max_leave_pct,channel,sl_target_pct,sl_seconds,"
       "concurrency,shrinkage_pct,max_occupancy_pct) VALUES(?,?,?,?,?,?,?,?,?,?)",
       (r["name"], lob, val("required_hc", 0), val("max_leave_pct", 10), channel, val("sl_target_pct", sl),
        val("sl_seconds", sls), val("concurrency", conc), val("shrinkage_pct", shr), val("max_occupancy_pct", occ)))


def setting_str(key):
    df = q("SELECT value FROM settings WHERE key=?", (key,))
    v = str(df.value[0]).strip() if len(df) and df.value[0] is not None else ""
    return v or DEFAULT_SETTINGS[key]


def setting(key):
    return float(setting_str(key))


def site_tz():
    try:
        return ZoneInfo(setting_str("site_tz"))
    except Exception:
        return ZoneInfo(DEFAULT_SETTINGS["site_tz"])


def now_site():
    """Current wall-clock time at the site (naive). Streamlit Cloud servers run on UTC."""
    fake = os.environ.get("WFM_TEST_NOW")  # automated tests only
    if fake:
        return dt.datetime.fromisoformat(fake)
    return dt.datetime.now(site_tz()).replace(tzinfo=None, microsecond=0)


def today_site():
    return now_site().date()


def current_user():
    return st.session_state.get("auth")


def is_admin():
    u = current_user()
    return bool(u and u.get("role") == "admin")


STORE = None
init_db()
if USE_SHEETS:
    try:
        STORE = get_store()
        STORE.maybe_refresh()
    except Exception as e:
        st.error("Couldn't connect to Google Sheets. Check that the sheet is shared with the service-account "
                 f"email (as Editor) and that the Sheets and Drive APIs are enabled.\n\n{type(e).__name__}: {e}")
        st.stop()
ensure_default_settings()
flush_to_sheet()

# ----------------------------------------------------------------- helpers


def parse_t(s):
    return dt.datetime.strptime(s, "%H:%M").time()


def shift_window(day, start, end):
    """Start/end datetimes for a shift that begins on `day` (handles overnight)."""
    if not start or not end:
        return None, None
    s = dt.datetime.combine(day, parse_t(start))
    e = dt.datetime.combine(day, parse_t(end))
    if e <= s:
        e += ONE_DAY
    return s, e


def closest_dt(day, t_str, target):
    t = parse_t(t_str)
    cands = [dt.datetime.combine(day + dt.timedelta(days=k), t) for k in (-1, 0, 1)]
    return min(cands, key=lambda x: abs(x - target))


def mins(td):
    return int(round(td.total_seconds() / 60))


def rest_set(rest_days):
    return {x.strip() for x in str(rest_days or "").split(",") if x.strip()}


def leave_abbrev(leave_type):
    m = re.search(r"\((\w+)\)", leave_type or "")
    return m.group(1) if m else "LV"


def agents_df(active_only=True, account=None):
    sql, p = "SELECT * FROM agents WHERE 1=1", []
    if active_only:
        sql += " AND active=1"
    if account and account != "All accounts":
        sql += " AND account=?"
        p.append(account)
    return q(sql + " ORDER BY account, name", p)


def account_names():
    return q("SELECT name FROM accounts ORDER BY name").name.tolist()


def agent_label_map(df):
    return {int(r.id): f"{r.name} ({r.emp_id}) · {r.account}" for r in df.itertuples()}


def week_start(day):
    return day - dt.timedelta(days=day.weekday())


def build_schedule(start, end, account=None, agent_id=None):
    """Effective schedule per agent per day: overrides > default shift/rest days,
    then approved leave replaces any working day."""
    cols = ["agent_id", "emp_id", "name", "account", "lob", "skills", "date",
            "kind", "shift_start", "shift_end", "note"]
    ag = agents_df(True, account)
    if agent_id is not None:
        ag = ag[ag.id == int(agent_id)]
    if ag.empty:
        return pd.DataFrame(columns=cols)
    s, e = str(start), str(end)
    vers = q("SELECT agent_id, effective_from, shift_start, shift_end, rest_days FROM schedule_changes "
             "WHERE effective_from<=? ORDER BY effective_from", (e,))
    vmap = {}
    for r in vers.itertuples():
        vmap.setdefault(int(r.agent_id), []).append((r.effective_from, r.shift_start, r.shift_end, r.rest_days))
    ov = q("SELECT * FROM schedule_overrides WHERE date BETWEEN ? AND ?", (s, e))
    ovmap = {(int(r.agent_id), r.date): r for r in ov.itertuples()}
    lv = q("SELECT agent_id, leave_type, start_date, end_date FROM leaves "
           "WHERE status='Approved' AND start_date<=? AND end_date>=?", (e, s))
    rows = []
    for a in ag.itertuples():
        base = (a.shift_start, a.shift_end, a.rest_days)
        versions = vmap.get(int(a.id), [])
        mine = lv[lv.agent_id == a.id]
        for d in pd.date_range(start, end).date:
            ds = d.isoformat()
            ss0, se0, rd0 = base
            for eff, vs, ve, vr in versions:  # permanent schedule in effect on this date
                if eff <= ds:
                    ss0, se0, rd0 = vs, ve, vr
            rds = rest_set(rd0)
            if (a.id, ds) in ovmap:
                o = ovmap[(a.id, ds)]
                kind, ss, se, note = o.kind, o.shift_start, o.shift_end, o.note or ""
            elif DAYS[d.weekday()] in rds:
                kind, ss, se, note = "Rest Day", None, None, ""
            else:
                kind, ss, se, note = "Work", ss0, se0, ""
            hit = mine[(mine.start_date <= ds) & (mine.end_date >= ds)]
            if len(hit) and kind in WORK_KINDS:
                kind, note = "Leave", hit.iloc[0].leave_type
            rows.append(dict(agent_id=int(a.id), emp_id=a.emp_id, name=a.name, account=a.account,
                             lob=a.lob, skills=a.skills or "", date=ds, kind=kind,
                             shift_start=ss, shift_end=se, note=note))
    return pd.DataFrame(rows, columns=cols)


def set_permanent_schedule(agent_id, effective_from, shift_start, shift_end, rest_days, note=""):
    """Change an agent's permanent schedule from a date on. Earlier dates keep the old schedule."""
    eff = str(effective_from)
    have = q("SELECT 1 FROM schedule_changes WHERE agent_id=?", (agent_id,))
    if have.empty:  # remember the schedule that applied until now
        cur = q("SELECT shift_start, shift_end, rest_days FROM agents WHERE id=?", (agent_id,))
        if len(cur):
            ex("INSERT OR REPLACE INTO schedule_changes VALUES(?,?,?,?,?,?)",
               (agent_id, "2000-01-01", cur.shift_start[0], cur.shift_end[0], cur.rest_days[0], "original"))
    ex("INSERT OR REPLACE INTO schedule_changes VALUES(?,?,?,?,?,?)",
       (agent_id, eff, shift_start, shift_end, rest_days, note))
    sync_current_schedules()


def sync_current_schedules():
    """Keep the roster's shift columns showing the permanent schedule in effect today."""
    today = str(today_site())
    cur = q("SELECT c.agent_id, c.shift_start, c.shift_end, c.rest_days FROM schedule_changes c "
            "JOIN (SELECT agent_id, MAX(effective_from) eff FROM schedule_changes WHERE effective_from<=? "
            "GROUP BY agent_id) m ON m.agent_id=c.agent_id AND m.eff=c.effective_from", (today,))
    ag = q("SELECT id, shift_start, shift_end, rest_days FROM agents")
    now = {int(r.id): (r.shift_start, r.shift_end, r.rest_days) for r in ag.itertuples()}
    for r in cur.itertuples():
        want = (r.shift_start, r.shift_end, r.rest_days)
        if now.get(int(r.agent_id)) != want:
            ex("UPDATE agents SET shift_start=?, shift_end=?, rest_days=? WHERE id=?", (*want, int(r.agent_id)))


def compute_attendance(day, shift_start, shift_end, time_in, time_out, status):
    """Returns (status, late_min, undertime_min, ot_min) from HH:MM time logs."""
    if status in ("Absent", "NCNS", "On Leave", "Rest Day") or not shift_start:
        return status, 0, 0, 0
    if not time_in:
        return status, 0, 0, 0
    s, e = shift_window(day, shift_start, shift_end)
    tin = closest_dt(day, time_in, s)
    tout = closest_dt(day, time_out, e) if time_out else None
    return attendance_from_times(s, e, tin, tout, status)


def attendance_from_times(s, e, tin, tout, status=""):
    """Late / undertime / rendered OT from full datetimes (shift s-e, logged tin-tout)."""
    grace, block = setting("grace_min"), setting("ot_block_min")
    late = mins(tin - s) if tin > s + dt.timedelta(minutes=grace) else 0
    ut = ot = 0
    if tin < s:  # came in early (e.g. approved pre-shift OT)
        ot += int(mins(s - tin) // block * block)
    if tout is not None:
        if tout < e:
            ut = mins(e - tout)
        elif tout > e:
            ot += int(mins(tout - e) // block * block)
    if status in ("", None, "Present", "Late", "Undertime"):
        status = "Late" if late else ("Undertime" if ut else "Present")
    return status, late, ut, ot


def save_attendance(agent_id, date, time_in, time_out, status, late, ut, ot, remarks=None,
                    break_min=None, overbreak_min=None):
    """Insert or update one attendance row. Break columns are only touched when given (clock data is kept)."""
    cols = dict(time_in=time_in or None, time_out=time_out or None, status=status, late_min=late,
                undertime_min=ut, ot_min=ot, remarks=remarks)
    if break_min is not None:
        cols.update(break_min=break_min, overbreak_min=overbreak_min or 0)
    names = ",".join(cols)
    ex(f"INSERT INTO attendance(agent_id,date,{names}) VALUES(?,?,{','.join('?' * len(cols))}) "
       f"ON CONFLICT(agent_id,date) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in cols),
       (agent_id, str(date), *cols.values()))


def _day(d):
    return f"{d:%a %b %d}"


def check_time_off(agent_id, d1, d2):
    """Validate a time-off request against the agent's real schedule. Returns (working_days, error)."""
    if d2 < d1:
        return 0, "End date is before start date."
    if (d2 - d1).days > 60:
        return 0, "That's more than 60 days — please split it into smaller requests."
    sch = build_schedule(d1, d2, agent_id=agent_id)
    kinds = dict(zip(sch.date, sch.kind))
    off = {"Rest Day": "your rest day", "Holiday": "a holiday"}
    for d, label in ((d1, "start"), (d2, "end")):
        k = kinds.get(str(d))
        if k in off:
            return 0, (f"{_day(d)} is {off[k]} — you can't file time off on it. "
                       f"Time off must {label} on a working day.")
    clash = q("SELECT leave_type, start_date, end_date FROM leaves WHERE agent_id=? AND status IN ('Pending','Approved') "
              "AND start_date<=? AND end_date>=?", (agent_id, str(d2), str(d1)))
    if len(clash):
        c = clash.iloc[0]
        return 0, f"There's already time off filed for {c.start_date} → {c.end_date} ({c.leave_type})."
    days = sum(1 for k in kinds.values() if k in WORK_KINDS)
    if days == 0:
        return 0, "There are no working days in those dates."
    return days, None


def check_ot(agent_id, d, start, end, ot_type):
    """Validate an OT request: it must fall outside the agent's scheduled hours. Returns (hours, error)."""
    if not (re.fullmatch(TIME_RE, start or "") and re.fullmatch(TIME_RE, end or "") and start and end):
        return 0, "Times must be HH:MM (24-hour), e.g. 07:00."
    sch = build_schedule(d - ONE_DAY, d + ONE_DAY, agent_id=agent_id)
    rows = {r.date: r for r in sch.itertuples()}
    today = rows.get(str(d))
    if today is not None and today.kind == "Leave":
        return 0, f"You're on approved time off on {_day(d)}."
    shifts = [(r.date, *shift_window(dt.date.fromisoformat(r.date), r.shift_start, r.shift_end), r.shift_start,
               r.shift_end) for r in sch.itertuples() if r.kind in WORK_KINDS]
    base = next(((s, e) for dd, s, e, *_ in shifts if dd == str(d)), None)
    ws, we = place_ot(d, start, end, base) if base else shift_window(d, start, end)
    hrs = round((we - ws).total_seconds() / 3600, 2)
    for dd, s, e, ss, se in shifts:
        if ws < e and s < we:
            return 0, (f"{start}–{end} overlaps your scheduled shift {ss}–{se} ({_day(dt.date.fromisoformat(dd))}). "
                       "OT can only be filed outside your schedule.")
    works_today = base is not None
    if ot_type == "Rest Day OT" and works_today:
        return 0, f"{_day(d)} is a working day for you — choose Pre-shift or Post-shift OT."
    if ot_type in ("Pre-shift OT", "Post-shift OT") and not works_today:
        return 0, f"You have no shift on {_day(d)} — choose Rest Day OT."
    if ot_type == "Pre-shift OT" and we > base[0]:
        return 0, f"Pre-shift OT must end by your shift start ({base[0]:%H:%M})."
    if ot_type == "Post-shift OT" and ws < base[1]:
        return 0, f"Post-shift OT must start at or after your shift end ({base[1]:%H:%M})."
    if hrs > setting("max_daily_hrs"):
        return 0, f"That's {hrs:g}h — longer than the {setting('max_daily_hrs'):g}h limit."
    mine = q("SELECT date, start_time, end_time, status FROM ot_requests WHERE agent_id=? "
             "AND status IN ('Pending','Approved') AND date BETWEEN ? AND ?",
             (agent_id, str(d - ONE_DAY), str(d + ONE_DAY)))
    for r in mine.itertuples():
        od = dt.date.fromisoformat(r.date)
        ob = next(((s, e) for dd, s, e, *_ in shifts if dd == r.date), None)
        os_, oe = place_ot(od, r.start_time, r.end_time, ob) if ob else shift_window(od, r.start_time, r.end_time)
        if ws < oe and os_ < we:
            return 0, f"This overlaps OT you already filed ({r.date} {r.start_time}–{r.end_time}, {r.status})."
    return hrs, None


def working_days(rest_days, start, end):
    rds = rest_set(rest_days)
    return sum(1 for d in pd.date_range(start, end) if DAYS[d.weekday()] not in rds)


def leave_load(account, start, end):
    """Per-day leave count vs allowed for an account."""
    acc = q("SELECT max_leave_pct FROM accounts WHERE name=?", (account,))
    pct = float(acc.max_leave_pct[0]) if len(acc) else 10.0
    sch = build_schedule(start, end, account)
    out = []
    for d, g in sch.groupby("date"):
        base = int(g.kind.isin(WORK_KINDS + ("Leave",)).sum())
        on_leave = int((g.kind == "Leave").sum())
        allowed = max(1, int(base * pct / 100))
        out.append(dict(date=d, headcount=base, on_leave=on_leave, allowed=allowed))
    return pd.DataFrame(out, columns=["date", "headcount", "on_leave", "allowed"])


def weekly_ot(day):
    w0 = week_start(day)
    df = q("SELECT agent_id, SUM(hours) h FROM ot_requests WHERE status='Approved' "
           "AND date BETWEEN ? AND ? GROUP BY agent_id", (str(w0), str(w0 + 6 * ONE_DAY)))
    return {int(a): float(h) for a, h in zip(df.agent_id, df.h)}


def find_coverage(day, need_start, need_end, account, skill="", exclude=()):
    """Rank every active agent for covering a window on `day`."""
    ns, ne = shift_window(day, need_start, need_end)
    need_h = (ne - ns).total_seconds() / 3600
    min_rest, max_daily, max_wk = setting("min_rest_hrs"), setting("max_daily_hrs"), setting("max_weekly_ot_hrs")
    lob_df = q("SELECT lob FROM accounts WHERE name=?", (account,))
    lob = lob_df.lob[0] if len(lob_df) else None
    sch = build_schedule(day - ONE_DAY, day + ONE_DAY)
    otw = weekly_ot(day)
    skill = (skill or "").strip().lower()
    out = []
    for aid, g in sch.groupby("agent_id"):
        if aid in exclude:
            continue
        info = g.iloc[0]
        today = g[g.date == day.isoformat()].iloc[0]
        reasons, adjacent = [], False
        if today.kind == "Leave":
            reasons.append("On approved leave")
        if today.kind in ("Holiday",):
            reasons.append("Holiday off")
        for r in g.itertuples():
            if r.kind not in WORK_KINDS:
                continue
            ws, we = shift_window(dt.date.fromisoformat(r.date), r.shift_start, r.shift_end)
            if ws < ne and ns < we:
                reasons.append(f"Already scheduled {r.shift_start}-{r.shift_end} ({r.date})")
                continue
            gap_h = ((ns - we) if we <= ns else (ws - ne)).total_seconds() / 3600
            if gap_h < 1:  # back-to-back = shift extension
                adjacent = True
                if (we - ws).total_seconds() / 3600 + need_h > max_daily:
                    reasons.append(f"Shift + OT exceeds {max_daily:g}h daily max")
            elif gap_h < min_rest:
                reasons.append(f"Only {gap_h:.1f}h rest (min {min_rest:g}h)")
        wk = otw.get(int(aid), 0.0)
        if wk + need_h > max_wk:
            reasons.append(f"Weekly OT would hit {wk + need_h:.1f}h (cap {max_wk:g}h)")
        if today.kind in WORK_KINDS:
            ctype = "Pre/Post-shift OT" if adjacent else "Same-day OT (separate block)"
        else:
            ctype = "Rest Day OT"
        skills = str(info.skills or "").lower()
        score = (3 if info.account == account else 0) + (1 if info.lob == lob else 0)
        score += 2 if skill and skill in skills else 0
        score += 1 if adjacent else 0
        score -= wk / 4
        out.append(dict(agent_id=int(aid), name=info["name"], emp_id=info.emp_id, account=info.account,
                        lob=info.lob, skills=info.skills, today=today.kind if today.kind != "Work"
                        else f"Work {today.shift_start}-{today.shift_end}",
                        cover_type=ctype, ot_this_week=round(wk, 1), score=round(score, 2),
                        eligible=not reasons, why_not="; ".join(reasons)))
    df = pd.DataFrame(out)
    if df.empty:
        return df
    return df.sort_values(["eligible", "score"], ascending=[False, False]).reset_index(drop=True)


def open_gaps(day, account=None):
    """Leaves + absences on `day` that have no coverage assigned yet."""
    sch = build_schedule(day, day, account)
    att = q("SELECT agent_id, status FROM attendance WHERE date=?", (str(day),))
    m = sch.merge(att, on="agent_id", how="left")
    ag = agents_df(True)[["id", "shift_start", "shift_end"]].rename(
        columns={"id": "agent_id", "shift_start": "def_start", "shift_end": "def_end"})
    m = m.merge(ag, on="agent_id", how="left")
    gaps = m[(m.kind == "Leave") | ((m.kind == "Work") & m.status.isin(ABSENT_STATUS))].copy()
    covered = q("SELECT covering_for FROM ot_requests WHERE date=? AND status!='Denied' "
                "AND covering_for IS NOT NULL", (str(day),)).covering_for.astype(int).tolist()
    gaps["covered"] = gaps.agent_id.isin(covered)
    gaps["reason"] = gaps.apply(lambda r: r.note if r.kind == "Leave" else r.status, axis=1)
    gaps["shift_start"] = gaps.shift_start.fillna(gaps.def_start)
    gaps["shift_end"] = gaps.shift_end.fillna(gaps.def_end)
    return gaps[["agent_id", "name", "account", "lob", "shift_start", "shift_end", "reason", "covered"]]


def style_kinds(df):
    colors = {"RD": "#e8e8e8", "LV": "#ffd6d6", "OT": "#d6f0ff", "TRN": "#fff1c2", "HOL": "#e6dcff"}

    def color(v):
        v = str(v)
        if v.startswith("RD OT"):
            return f"background-color:{colors['OT']}"
        if v == "RD":
            return f"background-color:{colors['RD']}"
        if v.startswith("TRN"):
            return f"background-color:{colors['TRN']}"
        if v == "HOL":
            return f"background-color:{colors['HOL']}"
        if re.fullmatch(r"[A-Z]{2,4}", v):
            return f"background-color:{colors['LV']}"
        return ""
    styler = df.style
    return (styler.map if hasattr(styler, "map") else styler.applymap)(color)


def cell_label(r):
    if r.kind == "Rest Day":
        return "RD"
    if r.kind == "Leave":
        return leave_abbrev(r.note)
    if r.kind == "Holiday":
        return "HOL"
    if r.kind == "Training":
        return f"TRN {r.shift_start}-{r.shift_end}"
    if r.kind == "Work (RD OT)":
        return f"RD OT {r.shift_start}-{r.shift_end}"
    return f"{r.shift_start}-{r.shift_end}"


# ----------------------------------------------------------------- pages


def page_dashboard(acct):
    st.header("📊 Daily Dashboard")
    if mark_no_shows():
        flush_to_sheet()
    day = st.date_input("Date", today_site(), key="dash_day")
    sch = build_schedule(day, day, acct)
    if sch.empty:
        st.info("No agents yet. Add your roster under **Roster & Accounts**, or load sample data in **Settings**.")
        return
    att = q("SELECT agent_id, status, late_min FROM attendance WHERE date=?", (str(day),))
    m = sch.merge(att, on="agent_id", how="left")
    cov = q("SELECT a.account FROM ot_requests o JOIN agents a ON a.id=o.covering_for "
            "WHERE o.date=? AND o.status='Approved'", (str(day),))
    ot_hrs = q("SELECT COALESCE(o.for_account, c.account, a.account) AS account, SUM(o.hours) AS h "
               "FROM ot_requests o JOIN agents a ON a.id=o.agent_id LEFT JOIN agents c ON c.id=o.covering_for "
               "WHERE o.date=? AND o.status='Approved' GROUP BY 1", (str(day),))
    req = q("SELECT name AS account, required_hc FROM accounts")
    rows = []
    for acc, g in m.groupby("account"):
        sched = g[g.kind == "Work"]
        present = int(sched.status.isin(PRESENT_STATUS).sum())
        absent = int(sched.status.isin(ABSENT_STATUS).sum())
        covers = int((cov.account == acc).sum())
        vol_req, peak, short_iv = required_day_hc(acc, day)
        if vol_req is None:  # no volume history yet -> manual number from Accounts
            rhc = req[req.account == acc].required_hc
            rhc, src = (int(rhc.iloc[0]) if len(rhc) and pd.notna(rhc.iloc[0]) else 0), "manual"
        else:
            rhc, src = vol_req, "volume"
        projected = len(sched) - absent + covers
        oh = ot_hrs[ot_hrs.account == acc].h
        rows.append({"Account": acc, "LOB": g.lob.iloc[0], "Required": rhc, "Req. from": src,
                     "Peak interval HC": round(peak, 1) if peak is not None else None,
                     "Scheduled": len(sched), "Time off": int((g.kind == "Leave").sum()),
                     "Rest day": int((g.kind == "Rest Day").sum()),
                     "Present": present, "Late": int((sched.late_min.fillna(0) > 0).sum()),
                     "Absent/NCNS": absent, "Cover OT in": covers,
                     "OT hrs": round(float(oh.iloc[0]), 1) if len(oh) else 0.0,
                     "Projected HC": projected, "Variance": projected - rhc,
                     "Short intervals": short_iv})
    summ = pd.DataFrame(rows)
    c = st.columns(6)
    c[0].metric("Scheduled", int(summ.Scheduled.sum()))
    c[1].metric("Present", int(summ.Present.sum()))
    c[2].metric("Late", int(summ.Late.sum()))
    c[3].metric("Absent/NCNS", int(summ["Absent/NCNS"].sum()))
    c[4].metric("Time off", int(summ["Time off"].sum()))
    c[5].metric("HC variance", int(summ.Variance.sum()))
    st.subheader("Staffing by account")
    st.dataframe(summ.style.map(lambda v: "color:#c0392b;font-weight:600" if isinstance(v, (int, float)) and v < 0 else "",
                                subset=["Variance"]).format(precision=1, na_rep="—"), hide_index=True, width="stretch")
    st.caption(f"**Required** comes from the volume forecast (total interval need ÷ {setting('shift_hours'):g}h shifts) "
               "when the account has volume history, otherwise from the manual number in Accounts. "
               "**Short intervals** = 30-min intervals below need even after approved OT — fix them in "
               "**Interval Planner → OT plan**.")

    st.subheader("Open gaps (no coverage yet)")
    gaps = open_gaps(day, acct)
    og = gaps[~gaps.covered]
    if og.empty:
        st.success("All leaves and absences for this date have coverage.")
    else:
        st.dataframe(og.drop(columns=["agent_id", "covered"]), hide_index=True, width="stretch")
        st.caption("Go to **Coverage Finder** to assign someone.")

    st.subheader("Last 14 days")
    start = day - dt.timedelta(days=13)
    hist = q("SELECT t.date, t.status, t.late_min, a.account FROM attendance t JOIN agents a ON a.id=t.agent_id "
             "WHERE t.date BETWEEN ? AND ?", (str(start), str(day)))
    if acct != "All accounts":
        hist = hist[hist.account == acct]
    if hist.empty:
        st.caption("No attendance logged in this period yet.")
    else:
        trend = hist.groupby("date").agg(
            Lates=("late_min", lambda s: int((s > 0).sum())),
            Absences=("status", lambda s: int(s.isin(ABSENT_STATUS).sum())))
        st.bar_chart(trend)


def page_schedule(acct):
    st.header("🗓️ Schedules")
    pick = st.date_input("Week of", today_site(), key="sched_week")
    w0 = week_start(pick)
    sch = build_schedule(w0, w0 + 6 * ONE_DAY, acct)
    if sch.empty:
        st.info("No agents in this view yet — import your team schedule below.")
        page_schedule_import(acct)
        return
    sch["cell"] = [cell_label(r) for r in sch.itertuples()]
    sch["day"] = [f"{DAYS[dt.date.fromisoformat(d).weekday()]} {d[5:]}" for d in sch.date]
    order = list(dict.fromkeys(sch.day))
    grid = sch.pivot_table(index=["account", "name"], columns="day", values="cell", aggfunc="first")[order]
    st.dataframe(style_kinds(grid), width="stretch", height=min(600, 38 + 35 * len(grid)))
    st.caption("Grey = rest day · Red = leave · Blue = RD OT · Yellow = training · Purple = holiday")

    working = sch[sch.kind.isin(("Work", "Work (RD OT)"))].groupby("day").size().reindex(order, fill_value=0)
    wdf = working.rename("agents").reset_index()
    st.altair_chart(alt.Chart(wdf).mark_bar(color="#4c78a8").encode(
        x=alt.X("day:N", sort=order, title=None), y=alt.Y("agents:Q", title="Agents on shift"),
        tooltip=["day", "agents"]), width="stretch", height=220)

    ag = agents_df(True, acct)
    labels = agent_label_map(ag)
    t1, t2, t3, t4, t5 = st.tabs(["Change a schedule (one-off)", "Shift swap", "Current overrides",
                                  "🔁 Permanent change", "📥 Import schedule"])
    with t4:
        page_permanent_change(acct)
    with t5:
        page_schedule_import(acct)
    with t1:
        with st.form("override"):
            ids = st.multiselect("Agent(s)", list(labels), format_func=labels.get)
            c = st.columns(2)
            d1 = c[0].date_input("From", w0)
            d2 = c[1].date_input("To", w0)
            kind = st.selectbox("Set day as", OVERRIDE_KINDS)
            c = st.columns(2)
            ss = c[0].text_input("Shift start (HH:MM)", "22:00")
            se = c[1].text_input("Shift end (HH:MM)", "07:00")
            note = st.text_input("Note", "")
            if st.form_submit_button("Save change"):
                if kind in WORK_KINDS and not (re.match(TIME_RE, ss) and re.match(TIME_RE, se) and ss and se):
                    st.error("Enter shift times as HH:MM.")
                elif not ids or d2 < d1:
                    st.error("Pick agent(s) and a valid date range.")
                else:
                    for aid in ids:
                        for d in pd.date_range(d1, d2).date:
                            ex("INSERT OR REPLACE INTO schedule_overrides VALUES(?,?,?,?,?,?)",
                               (aid, d.isoformat(), kind, ss if kind in WORK_KINDS else None,
                                se if kind in WORK_KINDS else None, note))
                    st.success("Schedule updated.")
                    rerun()
    with t2:
        st.caption("Swaps the two agents' effective schedules for one date (e.g. a rest-day swap).")
        with st.form("swap"):
            c = st.columns(3)
            a1 = c[0].selectbox("Agent A", list(labels), format_func=labels.get, key="sw_a")
            a2 = c[1].selectbox("Agent B", list(labels), format_func=labels.get, key="sw_b")
            sd = c[2].date_input("Date", w0, key="sw_d")
            if st.form_submit_button("Swap"):
                one = build_schedule(sd, sd)
                ra, rb = one[one.agent_id == a1], one[one.agent_id == a2]
                if a1 == a2 or ra.empty or rb.empty:
                    st.error("Pick two different active agents.")
                elif "Leave" in (ra.kind.iloc[0], rb.kind.iloc[0]):
                    st.error("Can't swap a day that is on approved leave.")
                else:
                    for me, other in ((a1, rb.iloc[0]), (a2, ra.iloc[0])):
                        ex("INSERT OR REPLACE INTO schedule_overrides VALUES(?,?,?,?,?,?)",
                           (me, sd.isoformat(), other.kind, other.shift_start, other.shift_end, "Shift swap"))
                    st.success("Swapped.")
                    rerun()
    with t3:
        ov = q("SELECT o.agent_id, g.name, o.date, o.kind, o.shift_start, o.shift_end, o.note "
               "FROM schedule_overrides o JOIN agents g ON g.id=o.agent_id WHERE o.date BETWEEN ? AND ? "
               "ORDER BY o.date", (str(w0), str(w0 + 6 * ONE_DAY)))
        if acct != "All accounts":
            ov = ov[ov.agent_id.isin(ag.id)]
        if ov.empty:
            st.caption("No overrides this week; everyone follows their default shift.")
        else:
            ov["remove"] = False
            ed = st.data_editor(ov, hide_index=True, disabled=list(ov.columns[:-1]), key="ov_ed",
                                column_config={"agent_id": None})
            if st.button("Remove selected overrides"):
                for r in ed[ed.remove].itertuples():
                    ex("DELETE FROM schedule_overrides WHERE agent_id=? AND date=?", (r.agent_id, r.date))
                rerun()


def page_attendance(acct):
    st.header("⏱️ Attendance")
    if mark_no_shows():
        flush_to_sheet()
    t_sheet, t_logs = st.tabs(["Daily sheet", "Clock logs"])
    with t_logs:
        page_clock_logs(acct)
    with t_sheet:
        _attendance_sheet(acct)


def _attendance_sheet(acct):
    day = st.date_input("Shift date (the date the shift starts)", today_site(), key="att_day")
    sch = build_schedule(day, day, acct)
    show_rd = st.toggle("Include rest days", False)
    if not show_rd:
        sch = sch[sch.kind != "Rest Day"]
    if sch.empty:
        st.info("Nobody scheduled in this view.")
        return
    att = q("SELECT * FROM attendance WHERE date=?", (str(day),))
    m = sch.merge(att, on="agent_id", how="left")
    m["shift"] = [f"{a}-{b}" if a else "" for a, b in zip(m.shift_start, m.shift_end)]
    m["status"] = [s if isinstance(s, str) and s else ("On Leave" if k == "Leave" else
                   "Rest Day" if k == "Rest Day" else "") for s, k in zip(m.status, m.kind)]
    for col in ("time_in", "time_out", "remarks"):
        m[col] = m[col].fillna("")
    view = m[["agent_id", "emp_id", "name", "account", "kind", "shift", "time_in", "time_out",
              "status", "late_min", "undertime_min", "break_min", "overbreak_min", "ot_min", "remarks"]]
    st.caption(f"Filled automatically when agents log in and out. Grace period: {setting('grace_min'):g} min · "
               f"OT counted in {setting('ot_block_min'):g}-min blocks. You can still type or correct times here "
               "(leave status blank to auto-tag Present / Late / Undertime); to fix clock events, use **Clock logs**.")
    ed = st.data_editor(
        view, hide_index=True, width="stretch", key=f"att_ed_{day}_{acct}",
        disabled=["emp_id", "name", "account", "kind", "shift", "late_min", "undertime_min", "break_min",
                  "overbreak_min", "ot_min"],
        column_config={
            "break_min": "Breaks (min)", "overbreak_min": "Over-break (min)",
            "agent_id": None,
            "time_in": st.column_config.TextColumn("Time in", validate=TIME_RE, help="HH:MM, 24h"),
            "time_out": st.column_config.TextColumn("Time out", validate=TIME_RE, help="HH:MM, 24h"),
            "status": st.column_config.SelectboxColumn("Status", options=ATT_STATUS),
            "late_min": "Late (min)", "undertime_min": "UT (min)", "ot_min": "Rendered OT (min)",
        })
    c = st.columns([1, 1, 4])
    if c[0].button("💾 Save attendance", type="primary"):
        saved = 0
        for r in ed.itertuples():
            src = m[m.agent_id == r.agent_id].iloc[0]
            if not (r.time_in or r.time_out or r.status or r.remarks):
                continue
            stt, late, ut, ot = compute_attendance(day, src.shift_start, src.shift_end,
                                                   r.time_in, r.time_out, r.status or "")
            save_attendance(r.agent_id, day, r.time_in, r.time_out, stt, late, ut, ot, r.remarks)
            saved += 1
        st.toast(f"Saved {saved} records")
        rerun()
    if c[1].button("Mark blanks as Present"):
        for r in ed.itertuples():
            src = m[m.agent_id == r.agent_id].iloc[0]
            if src.kind in WORK_KINDS and not r.status and not r.time_in:
                save_attendance(r.agent_id, day, src.shift_start, src.shift_end, "Present", 0, 0, 0, r.remarks)
        rerun()


def page_leaves(acct):
    st.header("🌴 Time Off")
    ag = agents_df(True, acct)
    labels = agent_label_map(ag)
    t1, t2, t3, t4 = st.tabs(["File time off", "Pending approvals", "Time-off calendar", "Summary"])
    with t1:
        if not labels:
            st.info("No agents yet.")
        else:
            aid = st.selectbox("Agent", list(labels), format_func=labels.get, key="lv_agent")
            arow = ag[ag.id == aid].iloc[0]
            c = st.columns(3)
            ltype = c[0].selectbox("Type", LEAVE_TYPES)
            d1 = c[1].date_input("From", today_site() + ONE_DAY, key="lv_d1")
            d2 = c[2].date_input("To", today_site() + ONE_DAY, key="lv_d2")
            reason = st.text_input("Reason")
            if d2 >= d1:
                days, prob = check_time_off(aid, d1, d2)
                if prob:
                    st.warning(prob.replace("you can't", "time off can't").replace("You're", "They're"))
                else:
                    st.write(f"**{days}** working day(s) — rest days ({arow.rest_days}) not counted.")
                load = leave_load(arow.account, d1, d2)
                load["after_approval"] = load.on_leave + 1
                over = load[load.after_approval > load.allowed]
                if len(over):
                    st.warning(f"⚠️ {len(over)} day(s) would exceed {arow.account}'s leave cap: "
                               + ", ".join(over.date))
                with st.expander("Leave load for these dates"):
                    st.dataframe(load, hide_index=True)
            if st.button("Submit time off", type="primary"):
                days, err = check_time_off(aid, d1, d2)
                if err:
                    st.error(err.replace("you can't", "time off can't").replace("You're", "They're"))
                else:
                    ex("INSERT INTO leaves(agent_id,leave_type,start_date,end_date,days,status,reason,filed_on) "
                       "VALUES(?,?,?,?,?,?,?,?)", (aid, ltype, str(d1), str(d2), days, "Pending", reason,
                                                   str(today_site())))
                    st.success("Time off filed — waiting for approval.")
    with t2:
        pend = q("SELECT l.*, a.name, a.account FROM leaves l JOIN agents a ON a.id=l.agent_id "
                 "WHERE l.status='Pending' ORDER BY l.start_date")
        if acct != "All accounts":
            pend = pend[pend.account == acct]
        if pend.empty:
            st.success("No pending leaves.")
        approver = st.text_input("Approver name", "WFM", key="approver")
        for r in pend.itertuples():
            with st.expander(f"{r.name} · {r.leave_type} · {r.start_date} → {r.end_date} ({r.days:g}d)"):
                st.write(f"Account: **{r.account}** · Filed {r.filed_on} · Reason: {r.reason or '—'}")
                load = leave_load(r.account, r.start_date, r.end_date)
                load["after_approval"] = load.on_leave + 1
                st.dataframe(load, hide_index=True)
                if (load.after_approval > load.allowed).any():
                    st.warning("Approving puts at least one day over the account's leave cap.")
                c = st.columns(4)
                if c[0].button("✅ Approve", key=f"ap{r.id}"):
                    ex("UPDATE leaves SET status='Approved', decided_by=? WHERE id=?", (approver, r.id))
                    rerun()
                if c[1].button("❌ Deny", key=f"dn{r.id}"):
                    ex("UPDATE leaves SET status='Denied', decided_by=? WHERE id=?", (approver, r.id))
                    rerun()
    with t3:
        c = st.columns(2)
        f1 = c[0].date_input("From", today_site() - dt.timedelta(days=7), key="lc1")
        f2 = c[1].date_input("To", today_site() + dt.timedelta(days=30), key="lc2")
        allv = q("SELECT l.id, a.name, a.account, l.leave_type, l.start_date, l.end_date, l.days, l.status, "
                 "l.reason, l.decided_by FROM leaves l JOIN agents a ON a.id=l.agent_id "
                 "WHERE l.start_date<=? AND l.end_date>=? ORDER BY l.start_date", (str(f2), str(f1)))
        if acct != "All accounts":
            allv = allv[allv.account == acct]
        allv["cancel"] = False
        ed = st.data_editor(allv, hide_index=True, width="stretch",
                            disabled=list(allv.columns[:-1]), column_config={"id": None})
        if st.button("Cancel selected leaves"):
            for r in ed[ed.cancel].itertuples():
                ex("UPDATE leaves SET status='Cancelled' WHERE id=?", (r.id,))
            rerun()
    with t4:
        c = st.columns(2)
        s1 = c[0].date_input("From", today_site().replace(month=1, day=1), key="lvs1")
        s2 = c[1].date_input("To", today_site().replace(month=12, day=31), key="lvs2")
        used = q("SELECT agent_id, leave_type, SUM(days) d FROM leaves WHERE status='Approved' "
                 "AND start_date<=? AND end_date>=? GROUP BY agent_id, leave_type", (str(s2), str(s1)))
        summ = ag[["id", "emp_id", "name", "account"]].merge(
            used.pivot_table(index="agent_id", columns="leave_type", values="d", aggfunc="sum").reset_index()
            if len(used) else pd.DataFrame(columns=["agent_id"]), left_on="id", right_on="agent_id", how="left")
        summ = summ.drop(columns=["id", "agent_id"]).fillna(0)
        type_cols = [c for c in summ.columns if c not in ("emp_id", "name", "account")]
        summ["total_days_off"] = summ[type_cols].sum(axis=1) if type_cols else 0
        st.caption("Approved days off per contractor (working days only). Use it to spot frequent absences.")
        st.dataframe(summ.sort_values("total_days_off", ascending=False), hide_index=True, width="stretch")


def page_coverage(acct):
    st.header("🧩 Coverage Finder")
    day = st.date_input("Date needing coverage", today_site(), key="cov_day")
    gaps = open_gaps(day, acct)
    accts = account_names()
    if not accts:
        st.info("Set up accounts first.")
        return
    pre = {"account": accts[0], "start": "22:00", "end": "07:00", "for": None, "label": "Manual"}
    if len(gaps):
        st.subheader("Gaps on this date")
        st.dataframe(gaps.drop(columns="agent_id"), hide_index=True, width="stretch")
        opts = {int(r.agent_id): f"{r.name} · {r.reason} · {r.shift_start}-{r.shift_end}"
                + (" ✅ covered" if r.covered else "") for r in gaps.itertuples()}
        pick = st.selectbox("Find cover for", [None] + list(opts),
                            format_func=lambda k: "— enter a window manually —" if k is None else opts[k])
        if pick:
            g = gaps[gaps.agent_id == pick].iloc[0]
            pre = {"account": g.account, "start": g.shift_start, "end": g.shift_end, "for": pick, "label": g["name"]}
    else:
        st.caption("No leaves or absences recorded for this date — you can still search a window manually.")

    c = st.columns(4)
    account = c[0].selectbox("Account to cover", accts, index=accts.index(pre["account"])
                             if pre["account"] in accts else 0, key=f"cov_acc_{pre['for']}")
    ns = c[1].text_input("Cover from (HH:MM)", pre["start"], key=f"cov_s_{pre['for']}")
    ne = c[2].text_input("Cover to (HH:MM)", pre["end"], key=f"cov_e_{pre['for']}")
    skill = c[3].text_input("Required skill (optional)", "", help="e.g. Billing, Tier 2, Spanish")
    if not (re.fullmatch(TIME_RE, ns) and re.fullmatch(TIME_RE, ne) and ns and ne):
        st.error("Enter times as HH:MM.")
        return
    excl = (pre["for"],) if pre["for"] else ()
    res = find_coverage(day, ns, ne, account, skill, excl)
    if res.empty:
        st.info("No agents to evaluate.")
        return
    only = st.toggle("Show eligible only", True)
    view = res[res.eligible] if only else res
    st.caption(f"Ranked by: same account > skill match > same LOB > shift extension, minus OT already done this week. "
               f"Rules: min rest {setting('min_rest_hrs'):g}h · max {setting('max_daily_hrs'):g}h/day · "
               f"OT cap {setting('max_weekly_ot_hrs'):g}h/week.")
    st.dataframe(view.drop(columns=["agent_id"]), hide_index=True, width="stretch")
    elig = res[res.eligible]
    if elig.empty:
        st.warning("Nobody passes the rules. Try a shorter window (split cover), relax settings, or check other accounts.")
        return
    lbl = {int(r.agent_id): f"{r.name} · {r.cover_type} · {r.account}" for r in elig.itertuples()}
    cid = st.selectbox("Assign", list(lbl), format_func=lbl.get)
    if st.button("Assign coverage (creates approved OT)", type="primary"):
        row = elig[elig.agent_id == cid].iloc[0]
        s, e = shift_window(day, ns, ne)
        hrs = round((e - s).total_seconds() / 3600, 2)
        ex("INSERT INTO ot_requests(agent_id,date,ot_type,start_time,end_time,hours,reason,status,covering_for,filed_on) "
           "VALUES(?,?,?,?,?,?,?,?,?,?)", (cid, str(day), "Coverage OT", ns, ne, hrs,
                                          f"{row.cover_type} covering {pre['label']} ({account})", "Approved",
                                          pre["for"], str(today_site())))
        if row.cover_type == "Rest Day OT":
            ex("INSERT OR REPLACE INTO schedule_overrides VALUES(?,?,?,?,?,?)",
               (cid, str(day), "Work (RD OT)", ns, ne, f"Covering {pre['label']}"))
        st.success(f"{row['name']} assigned — {hrs:g}h OT logged.")
        rerun()


def page_ot(acct):
    st.header("⏰ Overtime")
    ag = agents_df(True, acct)
    labels = agent_label_map(ag)
    t1, t2, t3 = st.tabs(["File OT", "Approvals & log", "Weekly OT vs cap"])
    with t1:
        if labels:
            with st.form("ot"):
                aid = st.selectbox("Agent", list(labels), format_func=labels.get)
                c = st.columns(4)
                d = c[0].date_input("Date", today_site())
                typ = c[1].selectbox("Type", OT_TYPES)
                s = c[2].text_input("From (HH:MM)", "07:00")
                e = c[3].text_input("To (HH:MM)", "09:00")
                reason = st.text_input("Reason / business need")
                if st.form_submit_button("File OT"):
                    hrs, err = check_ot(aid, d, s, e, typ)
                    if err:
                        st.error(err.replace("your ", "their ").replace("You ", "They ").replace("You're", "They're"))
                    else:
                        ex("INSERT INTO ot_requests(agent_id,date,ot_type,start_time,end_time,hours,reason,status,filed_on) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (aid, str(d), typ, s, e, hrs, reason,
                                                         "Pending", str(today_site())))
                        st.success("OT filed.")
    with t2:
        c = st.columns(2)
        f1 = c[0].date_input("From", week_start(today_site()), key="ot1")
        f2 = c[1].date_input("To", week_start(today_site()) + 6 * ONE_DAY, key="ot2")
        log = q("SELECT o.id, a.name, a.account, o.date, o.ot_type, o.start_time, o.end_time, o.hours, "
                "o.reason, o.status FROM ot_requests o JOIN agents a ON a.id=o.agent_id "
                "WHERE o.date BETWEEN ? AND ? ORDER BY o.date", (str(f1), str(f2)))
        if acct != "All accounts":
            log = log[log.account == acct]
        ed = st.data_editor(log, hide_index=True, width="stretch", key="ot_ed",
                            disabled=[c for c in log.columns if c != "status"],
                            column_config={"id": None, "status": st.column_config.SelectboxColumn(
                                options=["Pending", "Approved", "Denied"])})
        if st.button("Save OT decisions"):
            for r in ed.itertuples():
                ex("UPDATE ot_requests SET status=? WHERE id=?", (r.status, r.id))
            rerun()
    with t3:
        d = st.date_input("Week of", today_site(), key="otw")
        otw = weekly_ot(d)
        w0 = week_start(d)
        rend = q("SELECT agent_id, SUM(ot_min)/60.0 h FROM attendance WHERE date BETWEEN ? AND ? GROUP BY agent_id",
                 (str(w0), str(w0 + 6 * ONE_DAY)))
        rmap = dict(zip(rend.agent_id.astype(int), rend.h))
        cap = setting("max_weekly_ot_hrs")
        tbl = ag[["id", "name", "account"]].copy()
        tbl["approved_ot_hrs"] = [otw.get(int(i), 0.0) for i in tbl.id]
        tbl["rendered_ot_hrs"] = [round(rmap.get(int(i), 0.0), 2) for i in tbl.id]
        tbl["cap_left"] = cap - tbl.approved_ot_hrs
        tbl["unapproved_ot"] = (tbl.rendered_ot_hrs - tbl.approved_ot_hrs).clip(lower=0)
        st.caption("**Unapproved OT** = time rendered past shift end with no approved OT — review before payroll.")
        st.dataframe(tbl.drop(columns="id").sort_values("approved_ot_hrs", ascending=False),
                     hide_index=True, width="stretch")


def page_reports(acct):
    st.header("📈 Reports")
    c = st.columns(2)
    d1 = c[0].date_input("From", today_site().replace(day=1), key="r1")
    d2 = c[1].date_input("To", today_site(), key="r2")
    sch = build_schedule(d1, d2, acct)
    if sch.empty:
        st.info("No data.")
        return
    att = q("SELECT * FROM attendance WHERE date BETWEEN ? AND ?", (str(d1), str(d2)))
    ot = q("SELECT agent_id, SUM(hours) h FROM ot_requests WHERE status='Approved' AND date BETWEEN ? AND ? "
           "GROUP BY agent_id", (str(d1), str(d2)))
    m = sch.merge(att, on=["agent_id", "date"], how="left")
    g = m.groupby(["emp_id", "name", "account"])
    rep = pd.DataFrame({
        "scheduled_days": g.apply(lambda x: int((x.kind == "Work").sum())),
        "present": g.apply(lambda x: int(x.status.isin(PRESENT_STATUS).sum())),
        "lates": g.apply(lambda x: int((x.late_min.fillna(0) > 0).sum())),
        "late_min_total": g.apply(lambda x: int(x.late_min.fillna(0).sum())),
        "absent": g.apply(lambda x: int((x.status == "Absent").sum())),
        "ncns": g.apply(lambda x: int((x.status == "NCNS").sum())),
        "undertime_min": g.apply(lambda x: int(x.undertime_min.fillna(0).sum())),
        "break_min": g.apply(lambda x: int(pd.to_numeric(x.break_min, errors="coerce").fillna(0).sum())),
        "overbreak_min": g.apply(lambda x: int(pd.to_numeric(x.overbreak_min, errors="coerce").fillna(0).sum())),
        "rendered_ot_hrs": g.apply(lambda x: round(x.ot_min.fillna(0).sum() / 60, 2)),
        "leave_days": g.apply(lambda x: int((x.kind == "Leave").sum())),
        "agent_id": g.agent_id.first(),
    }).reset_index()
    rep["approved_ot_hrs"] = rep.agent_id.map(dict(zip(ot.agent_id, ot.h))).fillna(0)
    logged = rep.present + rep.absent + rep.ncns
    rep["attendance_%"] = (100 * rep.present / logged.where(logged > 0)).round(1)
    rep["tardiness_%"] = (100 * rep.lates / rep.present.where(rep.present > 0)).round(1)
    rep = rep.drop(columns="agent_id")
    c = st.columns(4)
    tot_logged = logged.sum()
    c[0].metric("Attendance", f"{100 * rep.present.sum() / tot_logged:.1f}%" if tot_logged else "—")
    c[1].metric("Lates", int(rep.lates.sum()))
    c[2].metric("Absent + NCNS", int(rep.absent.sum() + rep.ncns.sum()))
    c[3].metric("Approved OT hrs", f"{rep.approved_ot_hrs.sum():.1f}")
    st.dataframe(rep, hide_index=True, width="stretch")
    st.download_button("⬇️ Agent summary (CSV)", rep.to_csv(index=False), f"agent_summary_{d1}_{d2}.csv")
    raw = m[m.status.notna()][["date", "emp_id", "name", "account", "shift_start", "shift_end", "time_in",
                              "time_out", "status", "late_min", "undertime_min", "break_min", "overbreak_min",
                              "ot_min", "remarks"]]
    st.download_button("⬇️ Raw attendance log (CSV)", raw.to_csv(index=False), f"attendance_{d1}_{d2}.csv")
    st.subheader("Top tardiness")
    st.bar_chart(rep.set_index("name").late_min_total.sort_values(ascending=False).head(15))


def page_roster():
    st.header("👥 Roster & Accounts")
    t1, t2, t3 = st.tabs(["Agents", "Accounts", "Import CSV"])
    with t1:
        st.caption("Shift times are 24h HH:MM (overnight shifts are fine, e.g. 22:00-07:00). "
                   "Rest days comma-separated, e.g. `Sat,Sun`. Untick **active** for resigned agents.")
        ag = agents_df(False).drop(columns="id")
        ag["active"] = ag.active.astype(bool)
        accts = account_names()
        ed = st.data_editor(ag, num_rows="dynamic", hide_index=True, width="stretch", key="ag_ed",
                            column_config={
                                "emp_id": st.column_config.TextColumn("Emp ID", required=True),
                                "name": st.column_config.TextColumn("Name", required=True),
                                "account": st.column_config.SelectboxColumn("Account", options=accts),
                                "lob": st.column_config.SelectboxColumn("LOB", options=LOBS),
                                "skills": st.column_config.TextColumn("Skills", help="comma-separated"),
                                "shift_start": st.column_config.TextColumn("Shift start", validate=TIME_RE),
                                "shift_end": st.column_config.TextColumn("Shift end", validate=TIME_RE),
                                "rest_days": st.column_config.TextColumn("Rest days"),
                                "vl_credits": None, "sl_credits": None,  # contractors: no leave credits
                            })
        if st.button("💾 Save roster", type="primary"):
            errs = []
            for i, r in ed.iterrows():
                if not r.emp_id or not r["name"]:
                    continue
                bad = [d for d in rest_set(r.rest_days) if d not in DAYS]
                if bad:
                    errs.append(f"{r['name']}: unknown rest day {bad}")
                    continue
                ex("""INSERT INTO agents(emp_id,name,account,lob,team_lead,skills,shift_start,shift_end,rest_days,
                      vl_credits,sl_credits,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(emp_id) DO UPDATE SET name=excluded.name, account=excluded.account,
                      lob=excluded.lob, team_lead=excluded.team_lead, skills=excluded.skills,
                      shift_start=excluded.shift_start, shift_end=excluded.shift_end, rest_days=excluded.rest_days,
                      vl_credits=excluded.vl_credits, sl_credits=excluded.sl_credits, active=excluded.active""",
                   (str(r.emp_id).strip(), r["name"], r.account, r.lob, r.team_lead, r.skills or "",
                    r.shift_start or "22:00", r.shift_end or "07:00", r.rest_days or "Sat,Sun",
                    r.vl_credits or 0, r.sl_credits or 0, int(bool(r.active) if pd.notna(r.active) else 1)))
            removed = set(ag.emp_id) - set(ed.emp_id.dropna())
            for emp in removed:
                ex("UPDATE agents SET active=0 WHERE emp_id=?", (emp,))
            for e in errs:
                st.error(e)
            if not errs:
                st.success("Roster saved." + (f" {len(removed)} removed row(s) set inactive." if removed else ""))
    with t2:
        acc = q("SELECT * FROM accounts ORDER BY name")
        st.caption("**Required HC** is calculated from volume (Interval Planner); the manual number is only a fallback for accounts without volume data · **Max leave %** = share of "
                   "headcount allowed on leave the same day. The interval columns drive the **Interval Planner**: "
                   "leave them blank to use sensible defaults for the channel.")
        ed = st.data_editor(acc, num_rows="dynamic", hide_index=True, key="acc_ed", column_config={
            "name": st.column_config.TextColumn("Account", required=True),
            "lob": st.column_config.SelectboxColumn("LOB", options=LOBS),
            "required_hc": st.column_config.NumberColumn("Manual required HC/day", help="Only used when the account has no volume history; otherwise required HC is calculated from volume"),
            "max_leave_pct": st.column_config.NumberColumn("Max leave %"),
            "channel": st.column_config.SelectboxColumn("Channel", options=CHANNELS,
                                                        help="Voice & Chat use Erlang C; Email/Back Office uses workload"),
            "sl_target_pct": st.column_config.NumberColumn("SL target %", help="e.g. 80 for 80/20"),
            "sl_seconds": st.column_config.NumberColumn("SL seconds", help="e.g. 20 for 80/20 voice, 60 for chat"),
            "concurrency": st.column_config.NumberColumn("Chats at once", help="Chat only; 1 for voice/email"),
            "shrinkage_pct": st.column_config.NumberColumn("Shrinkage %", help="Breaks, lunch, coaching, meetings, "
                                                           "system downtime — time on shift but not taking contacts"),
            "max_occupancy_pct": st.column_config.NumberColumn("Max occupancy %"),
        })
        if st.button("💾 Save accounts"):
            for r in ed.to_dict("records"):
                if r.get("name"):
                    upsert_account(r)
            for n in set(acc.name) - set(ed.name.dropna()):
                ex("DELETE FROM accounts WHERE name=?", (n,))
            st.success("Accounts saved.")
    with t3:
        st.write("Columns: `emp_id, name, account, lob, team_lead, skills, shift_start, shift_end, rest_days`")
        tmpl = pd.DataFrame([dict(emp_id="10001", name="Juan Dela Cruz", account="Telco Inbound", lob="Voice",
                                  team_lead="TL Santos", skills="Billing,Tech", shift_start="22:00",
                                  shift_end="07:00", rest_days="Sat,Sun")])
        st.download_button("Download template", tmpl.to_csv(index=False), "roster_template.csv")
        up = st.file_uploader("Upload roster CSV", type="csv")
        if up is not None and st.button("Import"):
            df = pd.read_csv(up, dtype=str).fillna("")
            for r in df.to_dict("records"):
                if r.get("account") and r["account"] not in account_names():
                    ex("INSERT OR IGNORE INTO accounts(name,lob) VALUES(?,?)", (r["account"], r.get("lob") or "Voice"))
                ex("""INSERT INTO agents(emp_id,name,account,lob,team_lead,skills,shift_start,shift_end,rest_days,
                      vl_credits,sl_credits) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(emp_id) DO UPDATE SET name=excluded.name, account=excluded.account,
                      lob=excluded.lob, team_lead=excluded.team_lead, skills=excluded.skills,
                      shift_start=excluded.shift_start, shift_end=excluded.shift_end, rest_days=excluded.rest_days,
                      vl_credits=excluded.vl_credits, sl_credits=excluded.sl_credits, active=1""",
                   (r["emp_id"], r["name"], r.get("account"), r.get("lob"), r.get("team_lead"), r.get("skills"),
                    r.get("shift_start") or "22:00", r.get("shift_end") or "07:00", r.get("rest_days") or "Sat,Sun",
                    float(r.get("vl_credits") or 0), float(r.get("sl_credits") or 0)))
            st.success(f"Imported {len(df)} agents.")


def load_sample_data():
    rnd = random.Random(7)
    accounts = [("Telco Inbound", "Voice", 8, 15, "Voice"), ("E-commerce Chat", "Non-Voice", 5, 20, "Chat"),
                ("Email & Back Office", "Non-Voice", 3, 25, "Email / Back Office")]
    for name, lob, hc, pct, ch in accounts:
        upsert_account(dict(name=name, lob=lob, required_hc=hc, max_leave_pct=pct, channel=ch))
    sample_interval_volume(rnd)
    first = ["Juan", "Maria", "Jose", "Ana", "Mark", "Kristine", "John Paul", "Angelica", "Carlo", "Jasmine",
             "Paolo", "Rica", "Miguel", "Joy", "Rafael", "Bea", "Kevin", "Trisha", "Ramon", "Liza", "Nico", "Carla"]
    last = ["Dela Cruz", "Santos", "Reyes", "Garcia", "Mendoza", "Bautista", "Villanueva", "Ramos", "Aquino",
            "Castillo", "Flores", "Navarro"]
    plan = [("Telco Inbound", "Voice", ["21:00-06:00", "22:00-07:00", "23:00-08:00"], "Billing,Tech,Retention", 11),
            ("E-commerce Chat", "Non-Voice", ["22:00-07:00", "08:00-17:00"], "Chat,Refunds,Spanish", 7),
            ("Email & Back Office", "Non-Voice", ["08:00-17:00", "13:00-22:00"], "Email,Data Entry,Billing", 4)]
    rest_patterns = ["Sat,Sun", "Sun,Mon", "Fri,Sat", "Tue,Wed", "Wed,Thu"]
    emp = 10001
    for acc, lob, shifts, skills, n in plan:
        for i in range(n):
            ss, se = rnd.choice(shifts).split("-")
            sk = ",".join(rnd.sample(skills.split(","), 2))
            ex("INSERT OR IGNORE INTO agents(emp_id,name,account,lob,team_lead,skills,shift_start,shift_end,"
               "rest_days,vl_credits,sl_credits) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (str(emp), f"{rnd.choice(first)} {rnd.choice(last)}", acc, lob, f"TL {rnd.choice(last)}", sk,
                ss, se, rest_patterns[i % len(rest_patterns)], 12, 12))
            emp += 1
    today = today_site()
    ag = agents_df(True)
    picks = ag.sample(4, random_state=3)
    for k, r in enumerate(picks.itertuples()):
        d1 = today + dt.timedelta(days=k - 1)
        d2 = d1 + dt.timedelta(days=rnd.choice([0, 1]))
        ex("INSERT INTO leaves(agent_id,leave_type,start_date,end_date,days,status,reason,filed_on) "
           "VALUES(?,?,?,?,?,?,?,?)", (r.id, LEAVE_TYPES[k % 3], str(d1), str(d2),
                                       working_days(r.rest_days, d1, d2), "Approved" if k < 3 else "Pending",
                                       "Sample", str(today - dt.timedelta(days=5))))
    sch = build_schedule(today - dt.timedelta(days=14), today - ONE_DAY)
    for r in sch[sch.kind == "Work"].itertuples():
        roll = rnd.random()
        d = dt.date.fromisoformat(r.date)
        s, e = shift_window(d, r.shift_start, r.shift_end)
        if roll < 0.03:
            stt, tin, tout = "Absent", None, None
        elif roll < 0.04:
            stt, tin, tout = "NCNS", None, None
        else:
            late = rnd.choice([0] * 8 + [3, 12, 25, 47])
            extra = rnd.choice([0] * 9 + [60, 90])
            tin = (s + dt.timedelta(minutes=late - rnd.randint(0, 10) * (late == 0))).strftime("%H:%M")
            tout = (e + dt.timedelta(minutes=extra + rnd.randint(0, 9))).strftime("%H:%M")
            stt = ""
        stt, lm, ut, ot = compute_attendance(d, r.shift_start, r.shift_end, tin, tout, stt)
        save_attendance(r.agent_id, r.date, tin, tout, stt, lm, ut, ot, "")
        if ot:
            ex("INSERT INTO ot_requests(agent_id,date,ot_type,start_time,end_time,hours,reason,status,filed_on) "
               "VALUES(?,?,?,?,?,?,?,?,?)", (r.agent_id, r.date, "Post-shift OT", r.shift_end, tout,
                                             ot / 60, "Queue backlog", "Approved", r.date))


def page_settings():
    st.header("⚙️ Settings")
    with st.form("settings"):
        st.markdown("**Attendance & coverage rules**")
        c = st.columns(5)
        vals = {}
        labels = {"grace_min": "Grace period (min)", "min_rest_hrs": "Min rest between shifts (h)",
                  "max_daily_hrs": "Max hours per stretch (h)", "max_weekly_ot_hrs": "Weekly OT cap (h)",
                  "ot_block_min": "OT block (min)"}
        for i, (k, lbl) in enumerate(labels.items()):
            vals[k] = c[i].number_input(lbl, value=setting(k), step=1.0)
        st.markdown("**Interval planner**")
        c = st.columns([1, 2, 2])
        vals["forecast_weeks"] = c[0].number_input("Forecast history (weeks)", 1.0, 26.0, setting("forecast_weeks"),
                                                   step=1.0, help="Same-weekday history used for the forecast")
        tz_now = setting_str("site_tz")
        vals["site_tz"] = c[1].selectbox("Site timezone (your schedules)", COMMON_TZ,
                                         index=COMMON_TZ.index(tz_now) if tz_now in COMMON_TZ else 0)
        vals["shift_hours"] = c[2].number_input("Shift length incl. lunch (h)", 1.0, 16.0, setting("shift_hours"),
                                                step=0.5, help="Used to turn interval need into daily headcount")
        st.markdown("**OT plan rules**")
        c = st.columns(4)
        vals["ot_min_block_hrs"] = c[0].number_input("Shortest OT block (h)", 0.5, 12.0, setting("ot_min_block_hrs"),
                                                     step=0.5)
        vals["ot_max_ext_hrs"] = c[1].number_input("Longest pre/post-shift OT (h)", 0.5, 12.0,
                                                   setting("ot_max_ext_hrs"), step=0.5)
        vals["rd_ot_max_hrs"] = c[2].number_input("Longest rest-day OT (h)", 0.5, 16.0, setting("rd_ot_max_hrs"),
                                                  step=0.5)
        vals["ot_min_eff_pct"] = c[3].number_input("Min % of block in short intervals", 10.0, 100.0,
                                                   setting("ot_min_eff_pct"), step=5.0,
                                                   help="Higher = less OT spent in intervals that are already covered")
        st.markdown("**Time clock**")
        c = st.columns(6)
        vals["break_min"] = c[0].number_input("Break length (min)", 1.0, 120.0, setting("break_min"), step=5.0)
        vals["breaks_allowed"] = c[1].number_input("Breaks per shift", 0.0, 10.0, setting("breaks_allowed"), step=1.0)
        vals["lunch_min"] = c[2].number_input("Lunch length (min)", 1.0, 180.0, setting("lunch_min"), step=5.0,
                                              help="Ignored when lunches per shift is 0")
        vals["lunches_allowed"] = c[3].number_input("Lunches per shift", 0.0, 3.0, setting("lunches_allowed"),
                                                    step=1.0, help="0 hides the Start lunch button")
        vals["login_early_hrs"] = c[4].number_input("Can log in up to (h) early", 0.5, 8.0,
                                                    setting("login_early_hrs"), step=0.5)
        vals["remember_hrs"] = st.number_input(
            "Stay signed in through page refreshes for (hours)", 0.0, 720.0, setting("remember_hrs"), step=1.0,
            help="0 = refreshing the page signs people out. Keep it short if agents share computers.")
        vals["auto_absent"] = 1.0 if c[5].toggle("Auto-mark no-shows Absent", bool(int(setting("auto_absent"))),
                                                 help="Only for agents who have a login") else 0.0
        if st.form_submit_button("Save settings"):
            for k, v in vals.items():
                ex("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)))
            st.success("Saved.")
    st.subheader("Backup & restore")
    if USE_SHEETS:
        st.caption("Your data lives in Google Sheets, which keeps its own version history "
                   "(File → Version history). A download here is an extra snapshot; restoring one overwrites the sheet.")
    else:
        st.caption("Download a backup regularly — especially on Streamlit Community Cloud, where the app's "
                   "disk is wiped whenever it restarts or redeploys. Connect Google Sheets (see README) to avoid this.")
    with open(DB_PATH, "rb") as f:
        st.download_button("⬇️ Download database backup", f.read(), f"wfm_backup_{today_site()}.db")
    up = st.file_uploader("Restore from backup (.db)", type=["db", "sqlite"])
    if up is not None and st.button("Restore (replaces all current data)"):
        data = up.getvalue()
        if not data.startswith(b"SQLite format 3"):
            st.error("That isn't a SQLite backup file.")
        else:
            with open(DB_PATH, "wb") as f:
                f.write(data)
            init_db()
            if STORE is not None:
                for t in TABLES:
                    STORE.mark_dirty(t)
            st.success("Restored.")
    st.subheader("Demo data")
    if st.button("Load sample data (3 accounts, 22 agents, 2 weeks of attendance)"):
        load_sample_data()
        st.success("Sample data loaded — check the Dashboard.")
    with st.expander("Danger zone"):
        if st.checkbox("I understand this deletes everything except admin logins") and st.button("Wipe all data"):
            for t in ("attendance", "leaves", "ot_requests", "schedule_overrides", "agents", "accounts", "interval_volume",
                      "time_logs"):
                ex(f"DELETE FROM {t}")
            ex("DELETE FROM users WHERE role='agent'")  # agent logins point at roster rows that are now gone
            ex("DELETE FROM sessions WHERE username NOT IN (SELECT username FROM users)")
            st.success("All data deleted. Admin logins were kept; agent logins were removed — recreate them under "
                       "**Users** after importing your roster.")


# ----------------------------------------------------------------- interval planner
COMMON_TZ = ["America/Chicago", "Asia/Manila", "America/New_York", "America/Denver", "America/Phoenix",
             "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Toronto", "Europe/London",
             "Europe/Berlin", "Australia/Sydney", "Asia/Singapore", "Asia/Kolkata", "UTC"]
INTERVAL_SEC = INTERVAL_MIN * 60


def account_params(account):
    df = q("SELECT * FROM accounts WHERE name=?", (account,))
    r = df.iloc[0].to_dict() if len(df) else {"lob": "Voice"}
    ch = r.get("channel") if isinstance(r.get("channel"), str) and r.get("channel") in CHANNELS else \
        ("Voice" if r.get("lob") == "Voice" else "Chat")
    d = CHANNEL_DEFAULTS[ch]

    def g(k, i):
        v = r.get(k)
        return float(d[i]) if v is None or (isinstance(v, float) and pd.isna(v)) or v == "" else float(v)
    return dict(channel=ch, sl_pct=min(max(g("sl_target_pct", 0), 1), 99.9), sl_sec=max(g("sl_seconds", 1), 1),
                conc=max(g("concurrency", 2), 1), shrink=min(max(g("shrinkage_pct", 3), 0), 90),
                max_occ=min(max(g("max_occupancy_pct", 4), 10), 100))


def _traffic(volume, aht, p):
    eff_aht = aht / (p["conc"] if p["channel"] == "Chat" else 1)
    return volume * eff_aht / INTERVAL_SEC, eff_aht


def erlang_requirement(volume, aht, p):
    """Agents needed taking contacts in the interval (before shrinkage)."""
    if volume <= 0 or aht <= 0:
        return 0.0
    if p["channel"] == "Email / Back Office":  # deferred work: workload ÷ occupancy
        return volume * aht / INTERVAL_SEC / (p["max_occ"] / 100)
    a, eff_aht = _traffic(volume, aht, p)
    target, occ = p["sl_pct"] / 100, p["max_occ"] / 100
    b, n = 1.0, 0
    for n in range(1, int(a * 3) + 100):
        b = a * b / (n + a * b)  # Erlang B recursion (numerically stable)
        if n <= a:
            continue
        c = n * b / (n - a * (1 - b))  # Erlang C: probability a contact waits
        sl = 1 - c * math.exp(-(n - a) * p["sl_sec"] / eff_aht)
        if sl >= target and a / n <= occ:
            return float(n)
    return float(n)


def erlang_sl(volume, aht, agents, p):
    """Projected service level if `agents` are taking contacts."""
    if volume <= 0 or aht <= 0 or p["channel"] == "Email / Back Office":
        return None
    a, eff_aht = _traffic(volume, aht, p)
    n = int(math.floor(agents + 1e-9))
    if n <= a:
        return 0.0
    b = 1.0
    for k in range(1, n + 1):
        b = a * b / (k + a * b)
    c = n * b / (n - a * (1 - b))
    return max(0.0, 1 - c * math.exp(-(n - a) * p["sl_sec"] / eff_aht))


def forecast_day(account, day, weeks=None, adj_pct=0.0):
    """Weighted same-weekday average of the last `weeks` weeks (newest counts most)."""
    weeks = int(weeks or setting("forecast_weeks"))
    out = pd.DataFrame({"interval": INTERVALS, "volume": 0.0, "aht": 0.0})
    hist = q("SELECT date, interval, volume, aht FROM interval_volume WHERE account=? AND date>=? AND date<?",
             (account, str(day - dt.timedelta(days=7 * weeks)), str(day)))
    if hist.empty:
        return out, []
    hist = hist[pd.to_datetime(hist.date).dt.weekday == day.weekday()].copy()
    dates = sorted(hist.date.unique(), reverse=True)[:weeks]
    if not dates:
        return out, []
    hist["volume"] = pd.to_numeric(hist.volume, errors="coerce").fillna(0)
    hist["aht"] = pd.to_numeric(hist.aht, errors="coerce").fillna(0)
    hist["work"] = hist.volume * hist.aht
    w = pd.Series({d: float(len(dates) - i) for i, d in enumerate(dates)})
    grid = lambda col: hist.pivot_table(index="interval", columns="date", values=col, aggfunc="sum") \
        .reindex(index=INTERVALS, columns=dates).fillna(0)
    vol, work = grid("volume"), grid("work")
    wv, ww = vol.mul(w, axis=1).sum(axis=1), work.mul(w, axis=1).sum(axis=1)
    overall_aht = ww.sum() / wv.sum() if wv.sum() else 0.0
    out["volume"] = (wv / w.sum() * (1 + adj_pct / 100)).values
    out["aht"] = (ww / wv.replace(0, np.nan)).fillna(overall_aht).values
    return out, dates


def place_ot(d, start, end, base_shift):
    """Datetime window of an OT record. Post-shift OT after a graveyard shift lands on the next calendar day."""
    cands = [shift_window(d + dt.timedelta(days=k), start, end) for k in (0, 1)]
    if base_shift and base_shift[0] is not None:
        s0, e0 = base_shift
        cands.sort(key=lambda w: max((w[0] - e0).total_seconds(), (s0 - w[1]).total_seconds(), 0))
    return cands[0]


def cover_vec(day, windows):
    """Headcount each 30-min interval of `day` gets from a list of (start, end) windows (partial = fraction)."""
    t0 = dt.datetime.combine(day, dt.time())
    vec = np.zeros(len(INTERVALS))
    for s, e in windows:
        if s is None:
            continue
        a, b = (s - t0).total_seconds() / INTERVAL_SEC, (e - t0).total_seconds() / INTERVAL_SEC
        for i in range(max(0, math.floor(a)), min(len(INTERVALS), math.ceil(b))):
            vec[i] += max(0.0, min(b, i + 1) - max(a, i))
    return vec


def ot_target(r):
    """Account an approved OT row counts toward: explicit, else the covered agent's, else the agent's own."""
    for v in (getattr(r, "for_account", None), getattr(r, "cov", None), getattr(r, "own", None)):
        if isinstance(v, str) and v:
            return v
    return None


def scheduled_by_interval(account, day):
    """Bodies on shift per interval: scheduled shifts − time off − absences + approved OT/coverage."""
    sch = build_schedule(day - ONE_DAY, day)
    att = q("SELECT agent_id, date, status FROM attendance WHERE date BETWEEN ? AND ?",
            (str(day - ONE_DAY), str(day)))
    absent = {(int(a), d) for a, d, s in zip(att.agent_id, att.date, att.status) if s in ABSENT_STATUS}
    ot = q("SELECT o.agent_id, o.date, o.start_time, o.end_time, o.for_account, a.account AS own, c.account AS cov "
           "FROM ot_requests o JOIN agents a ON a.id=o.agent_id LEFT JOIN agents c ON c.id=o.covering_for "
           "WHERE o.status='Approved' AND o.date BETWEEN ? AND ?", (str(day - ONE_DAY), str(day)))
    ot_keys = {(int(a), d) for a, d in zip(ot.agent_id, ot.date)}
    own_shift, windows = {}, []
    for r in sch.itertuples():
        if r.kind in ("Work", "Work (RD OT)"):
            own_shift[(r.agent_id, r.date)] = shift_window(dt.date.fromisoformat(r.date), r.shift_start, r.shift_end)
        if r.account != account or (r.agent_id, r.date) in absent:
            continue
        if r.kind == "Work" or (r.kind == "Work (RD OT)" and (r.agent_id, r.date) not in ot_keys):
            windows.append(own_shift[(r.agent_id, r.date)])
    for r in ot.itertuples():
        if ot_target(r) != account or not r.start_time or not r.end_time:
            continue
        windows.append(place_ot(dt.date.fromisoformat(r.date), r.start_time, r.end_time,
                                own_shift.get((int(r.agent_id), r.date))))
    return cover_vec(day, windows)


def interval_plan(account, day, adj_pct=0.0, weeks=None):
    p = account_params(account)
    fc, used = forecast_day(account, day, weeks, adj_pct)
    sched = scheduled_by_interval(account, day)
    keep = 1 - p["shrink"] / 100
    net = [erlang_requirement(v, a, p) for v, a in zip(fc.volume, fc.aht)]
    df = fc.rename(columns={"volume": "forecast_volume", "aht": "forecast_aht"})
    df["required_on_queue"] = net
    df["required_hc"] = [n / keep if n else 0.0 for n in net]
    df["scheduled_hc"] = sched
    df["variance"] = df.scheduled_hc - df.required_hc
    df["projected_sl"] = [erlang_sl(v, a, s * keep, p) for v, a, s in zip(fc.volume, fc.aht, sched)]
    act = q("SELECT interval, volume FROM interval_volume WHERE account=? AND date=?", (account, str(day)))
    df["actual_volume"] = df.interval.map(dict(zip(act.interval, pd.to_numeric(act.volume, errors="coerce"))))
    return df, used, p


def required_day_hc(account, day):
    """Daily headcount implied by the volume forecast: total interval need ÷ shift length.
    Returns (required HC, peak interval HC, short intervals) or (None, None, None) without history."""
    df, used, _ = interval_plan(account, day)
    if not used:
        return None, None, None
    need = df.required_hc.sum() * INTERVAL_MIN / 60 / max(setting("shift_hours"), 1)
    return int(math.ceil(need - 1e-9)), float(df.required_hc.max()), int((df.variance < -0.5).sum())


def _hours(td):
    return td.total_seconds() / 3600


def _fits(s, e, blocks, min_rest_h, max_daily_h):
    """Can an OT window [s, e) be added to an agent's existing work blocks without breaking the rules?"""
    blocks = [b for b in blocks if b and b[0] is not None]
    if any(bs < e and s < be for bs, be in blocks):
        return False
    merged = []
    for a, b in sorted(blocks + [(s, e)]):
        if merged and a <= merged[-1][1]:  # touching = one continuous stretch of work
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    k = next(i for i, (a, b) in enumerate(merged) if a <= s and e <= b)
    a, b = merged[k]
    if _hours(b - a) > max_daily_h + 1e-9:
        return False
    if k > 0 and _hours(a - merged[k - 1][1]) < min_rest_h:
        return False
    if k + 1 < len(merged) and _hours(merged[k + 1][0] - b) < min_rest_h:
        return False
    return True


def _steps(lo, hi):
    return [x / 2 for x in range(max(1, int(round(lo * 2))), int(round(hi * 2)) + 1)]


def plan_ot(account, day, adj_pct=0.0, weeks=None, include_other=False, skill=""):
    """Recommend OT blocks (pre-shift, post-shift, rest-day) that fill short intervals on `day`,
    keeping everyone's permanent schedule as is and respecting rest, max-hours and weekly OT cap rules."""
    df, used, p = interval_plan(account, day, adj_pct, weeks)
    req, sched0 = df.required_hc.values, df.scheduled_hc.values
    min_rest, max_daily, cap = setting("min_rest_hrs"), setting("max_daily_hrs"), setting("max_weekly_ot_hrs")
    min_blk, max_ext, rd_max = setting("ot_min_block_hrs"), setting("ot_max_ext_hrs"), setting("rd_ot_max_hrs")
    min_eff = setting("ot_min_eff_pct") / 100
    lob_df = q("SELECT lob FROM accounts WHERE name=?", (account,))
    lob = lob_df.lob[0] if len(lob_df) else None
    sch = build_schedule(day - 2 * ONE_DAY, day + ONE_DAY)
    att = q("SELECT agent_id, date, status FROM attendance WHERE date BETWEEN ? AND ?",
            (str(day - ONE_DAY), str(day)))
    absent = {(int(a), d) for a, d, s in zip(att.agent_id, att.date, att.status) if s in ABSENT_STATUS}
    ot = q("SELECT agent_id, date, start_time, end_time FROM ot_requests WHERE status IN ('Approved','Pending') "
           "AND date BETWEEN ? AND ?", (str(day - 2 * ONE_DAY), str(day + ONE_DAY)))
    otw, skill_l, t0 = weekly_ot(day), (skill or "").strip().lower(), dt.datetime.combine(day, dt.time())
    blocks, info, cands = {}, {}, []
    for aid, g in sch.groupby("agent_id"):
        aid = int(aid)
        first = g.iloc[0]
        same = first.account == account
        if not same and not (include_other and first.lob == lob):
            continue
        if skill_l and skill_l not in str(first.skills or "").lower():
            continue
        rows = {r.date: r for r in g.itertuples()}
        today = rows.get(str(day))
        if today is None or today.kind in ("Leave", "Holiday") or (aid, str(day)) in absent:
            continue
        shifts = {d: shift_window(dt.date.fromisoformat(d), r.shift_start, r.shift_end)
                  for d, r in rows.items() if r.kind in WORK_KINDS}
        blocks[aid] = list(shifts.values()) + [
            place_ot(dt.date.fromisoformat(r.date), r.start_time, r.end_time, shifts.get(r.date))
            for r in ot[ot.agent_id == aid].itertuples() if r.start_time and r.end_time]
        info[aid] = dict(name=first["name"], emp_id=first.emp_id, home_account=first.account, same=same)
        opts = []
        for dstr in (str(day - ONE_DAY), str(day)):  # extend yesterday's graveyard shift or today's shift
            r = rows.get(dstr)
            if r is None or r.kind not in ("Work", "Work (RD OT)") or (aid, dstr) in absent:
                continue
            s, e = shifts[dstr]
            for h in _steps(min_blk, max_ext):
                opts.append(("Pre-shift OT", dstr, s - dt.timedelta(hours=h), s))
                opts.append(("Post-shift OT", dstr, e, e + dt.timedelta(hours=h)))
        if today.kind == "Rest Day":
            for i in range(len(INTERVALS)):
                ws = t0 + dt.timedelta(minutes=INTERVAL_MIN * i)
                for h in _steps(min_blk, rd_max):
                    we = ws + dt.timedelta(hours=h)
                    if we > t0 + ONE_DAY:
                        break
                    opts.append(("Rest Day OT", str(day), ws, we))
        for typ, dstr, s, e in opts:
            vec = cover_vec(day, [(s, e)])
            if vec.sum() > 0:
                cands.append(dict(agent_id=aid, ot_type=typ, date=dstr, start=s, end=e, hours=_hours(e - s), vec=vec))

    sched = sched0.astype(float).copy()
    short = np.maximum(0, req - sched)
    planned, rd_used, picks, valid = {}, {}, [], {}
    for _ in range(300):
        best, best_score = None, 0.0
        for i, c in enumerate(cands):
            aid = c["agent_id"]
            if otw.get(aid, 0) + planned.get(aid, 0) + c["hours"] > cap + 1e-9:
                continue
            if c["ot_type"] == "Rest Day OT" and rd_used.get(aid, 0) + c["hours"] > rd_max + 1e-9:
                continue  # rest-day OT limit is per person per day, not per block
            gain = float(np.minimum(c["vec"], short).sum()) * INTERVAL_MIN / 60  # short HC-hours it fills
            if gain <= 0 or gain / c["hours"] < min_eff:
                continue
            if i not in valid:
                valid[i] = _fits(c["start"], c["end"], blocks[aid], min_rest, max_daily)
            if not valid[i]:
                continue
            score = gain - 0.5 * (c["hours"] - gain) + (0.3 if info[aid]["same"] else 0) \
                - 0.02 * (otw.get(aid, 0) + planned.get(aid, 0))
            if score > best_score:
                best, best_score, best_gain = i, score, gain
        if best is None:
            break
        c = cands[best]
        aid = c["agent_id"]
        sched += c["vec"]
        short = np.maximum(0, req - sched)
        blocks[aid] = blocks[aid] + [(c["start"], c["end"])]
        planned[aid] = planned.get(aid, 0) + c["hours"]
        if c["ot_type"] == "Rest Day OT":
            rd_used[aid] = rd_used.get(aid, 0) + c["hours"]
        valid = {k: v for k, v in valid.items() if cands[k]["agent_id"] != aid}
        picks.append(dict(agent_id=aid, ot_type=c["ot_type"], shift_date=c["date"], _s=c["start"], _e=c["end"],
                          fills_short_hrs=best_gain))
    # join back-to-back blocks for the same person into one block
    merged = []
    for pk in sorted(picks, key=lambda x: (x["agent_id"], x["_s"])):
        last = merged[-1] if merged else None
        if last and last["agent_id"] == pk["agent_id"] and last["ot_type"] == pk["ot_type"] \
                and last["shift_date"] == pk["shift_date"] and last["_e"] == pk["_s"]:
            last["_e"], last["fills_short_hrs"] = pk["_e"], last["fills_short_hrs"] + pk["fills_short_hrs"]
        else:
            merged.append(dict(pk))
    rows = []
    for pk in merged:
        aid = pk["agent_id"]
        rows.append(dict(agent_id=aid, name=info[aid]["name"], emp_id=info[aid]["emp_id"],
                         home_account=info[aid]["home_account"], ot_type=pk["ot_type"], shift_date=pk["shift_date"],
                         start=pk["_s"].strftime("%H:%M"), end=pk["_e"].strftime("%H:%M"),
                         hours=_hours(pk["_e"] - pk["_s"]), fills_short_hrs=round(pk["fills_short_hrs"], 1),
                         week_ot_after=round(otw.get(aid, 0) + planned[aid], 1), _s=pk["_s"]))
    plan = pd.DataFrame(rows, columns=["agent_id", "name", "emp_id", "home_account", "ot_type", "shift_date",
                                       "start", "end", "hours", "fills_short_hrs", "week_ot_after", "_s"])
    plan = plan.sort_values(["_s", "name"]).drop(columns="_s").reset_index(drop=True)
    curve = pd.DataFrame({"interval": INTERVALS, "required_hc": req, "scheduled_now": sched0, "with_ot_plan": sched})
    return plan, curve, used


def intercom_export(df):
    return {"Conversation ID", "Created at", "Inbound/Outbound"} <= set(df.columns)


def intercom_to_rows(df, channels=None):
    """One row per handled inbound conversation, ready for normalize_volume."""
    keep = df["Inbound/Outbound"].astype(str).str.lower().eq("inbound")
    if "Team assigned to (name)" in df:
        keep &= ~df["Team assigned to (name)"].astype(str).str.lower().eq("duplicate")
    if "Teammate replies" in df:
        keep &= pd.to_numeric(df["Teammate replies"], errors="coerce").fillna(0) > 0
    if channels is not None and "Channel" in df:
        keep &= df["Channel"].astype(str).isin(channels)
    return pd.DataFrame({"datetime": df.loc[keep, "Created at"].astype(str), "volume": 1})


def normalize_volume(df, source_tz, site_tz, default_aht):
    """Map an ACD/chat export to (date, interval, volume, aht) in site time, 30-min buckets."""
    cols = {c.lower().strip().replace(" ", "_"): c for c in df.columns}
    pick = lambda *names: next((cols[n] for n in names if n in cols), None)
    c_date = pick("date", "day", "report_date")
    c_int = pick("interval", "interval_start", "time", "start_time", "half_hour")
    c_dt = pick("datetime", "timestamp", "interval_datetime", "date_time")
    c_vol = pick("volume", "offered", "calls_offered", "contacts", "calls", "chats", "emails", "received")
    c_aht = pick("aht", "aht_seconds", "aht_sec", "handle_time", "avg_handle_time")
    if c_vol is None or not (c_dt or (c_date and c_int)):
        raise ValueError("Need a `volume` column and either `date` + `interval` columns or a `datetime` column.")
    raw = df[c_dt].astype(str) if c_dt else df[c_date].astype(str) + " " + df[c_int].astype(str)
    ts = pd.to_datetime(raw, errors="coerce", format="mixed")
    if source_tz != site_tz:
        ts = ts.dt.tz_localize(source_tz, ambiguous="NaT", nonexistent="shift_forward") \
            .dt.tz_convert(site_tz).dt.tz_localize(None)
    vol = pd.to_numeric(df[c_vol], errors="coerce").fillna(0)
    aht = pd.to_numeric(df[c_aht], errors="coerce").fillna(default_aht) if c_aht else pd.Series(default_aht,
                                                                                               index=df.index)
    work = pd.DataFrame({"ts": ts.dt.floor(f"{INTERVAL_MIN}min"), "volume": vol, "work": vol * aht})
    skipped = int(work.ts.isna().sum())
    g = work.dropna(subset=["ts"]).groupby("ts", as_index=False).sum()
    g["aht"] = np.where(g.volume > 0, g.work / g.volume.replace(0, np.nan), default_aht)
    g["date"] = g.ts.dt.strftime("%Y-%m-%d")
    g["interval"] = g.ts.dt.strftime("%H:%M")
    return g[["date", "interval", "volume", "aht"]].round({"aht": 1}), skipped


def sample_interval_volume(rnd):
    """8 weeks of realistic-looking history in site time for the demo accounts."""
    today = today_site()
    profiles = {  # (peak hour in site time, spread h, contacts at peak per 30 min)
        "Telco Inbound": dict(peaks=[(26.5, 2.6, 15)], base=0.0, aht=330, weekend=0.6),
        "E-commerce Chat": dict(peaks=[(26.0, 2.4, 8), (11.5, 2.0, 6)], base=0.0, aht=540, weekend=1.0),
        "Email & Back Office": dict(peaks=[(13.0, 2.5, 5)], base=0.0, aht=420, weekend=0.4),
    }
    rows = []
    for acc, pr in profiles.items():
        for k in range(56, 0, -1):
            d = today - dt.timedelta(days=k)
            mult = (pr["weekend"] if d.weekday() >= 5 else 1.0) * (1.08 if d.weekday() == 0 else 1.0)
            mult *= (1 + 0.04 * rnd.gauss(0, 1)) * (1 + 0.004 * (56 - k))
            for i, iv in enumerate(INTERVALS):
                h = i * INTERVAL_MIN / 60 + INTERVAL_MIN / 120
                lam = pr["base"] + sum(amp * math.exp(-0.5 * (min(abs(h - c), abs(h - c + 24), abs(h - c - 24)) / sd) ** 2)
                                       for c, sd, amp in pr["peaks"])
                lam *= mult
                vol = max(0, round(rnd.gauss(lam, math.sqrt(lam))))
                rows.append((acc, str(d), iv, vol, round(pr["aht"] * (1 + 0.08 * rnd.gauss(0, 1)))))
    exmany("INSERT OR REPLACE INTO interval_volume VALUES(?,?,?,?,?)", rows)


def _variance_color(v):
    if not isinstance(v, (int, float)) or pd.isna(v):
        return ""
    if v <= -2:
        return "background-color:#f1948a;color:#000"
    if v < -0.5:
        return "background-color:#fadbd8;color:#000"
    if v >= 2:
        return "background-color:#abebc6;color:#000"
    if v > 0.5:
        return "background-color:#e9f7ef;color:#000"
    return ""


def page_interval(acct):
    st.header("📶 Interval Planner")
    accts = account_names()
    if not accts:
        st.info("Add accounts under **Roster & Accounts** first, or load sample data in **Settings**.")
        return
    c = st.columns([2, 1.2, 1, 1])
    account = c[0].selectbox("Account", accts, index=accts.index(acct) if acct in accts else 0, key="iv_acc")
    day = c[1].date_input("Date", today_site(), key="iv_day")
    adj = c[2].number_input("Volume adj. %", -90.0, 500.0, 0.0, step=5.0,
                            help="Lift or cut the forecast, e.g. +15 for a promo, billing cycle or outage")
    weeks = c[3].number_input("History (weeks)", 1, 26, int(setting("forecast_weeks")), key="iv_weeks")
    p = account_params(account)
    rule = (f"{p['sl_pct']:g}% answered in {p['sl_sec']:g}s" if p["channel"] != "Email / Back Office"
            else "workload ÷ occupancy")
    st.caption(f"**{p['channel']}** · {rule}"
               + (f" · {p['conc']:g} chats at once" if p["channel"] == "Chat" else "")
               + f" · max occupancy {p['max_occ']:g}% · shrinkage {p['shrink']:g}% · times in "
               f"{setting_str('site_tz')} · change these under Roster & Accounts → Accounts")
    t1, t_ot, t2, t3 = st.tabs(["Day view", "🧮 OT plan", "Week heatmap", "Volume data"])

    with t1:
        df, used, p = interval_plan(account, day, adj, weeks)
        if not used:
            st.warning(f"No volume history for {day:%A}s in the last {weeks} weeks — upload it in the "
                       "**Volume data** tab. Showing scheduled headcount only.")
        else:
            st.caption(f"Forecast from {len(used)} {day:%A}(s): {', '.join(used)} (newest weighted most).")
        active = df[(df.required_hc > 0) | (df.scheduled_hc > 0) | (df.actual_volume.fillna(0) > 0)]
        short = df[df.variance < -0.5]
        vol_tot = df.forecast_volume.sum()
        sl_rows = df.dropna(subset=["projected_sl"])
        day_sl = (sl_rows.projected_sl * sl_rows.forecast_volume).sum() / sl_rows.forecast_volume.sum() \
            if len(sl_rows) and sl_rows.forecast_volume.sum() else None
        m = st.columns(5)
        m[0].metric("Forecast volume", f"{vol_tot:,.0f}")
        if len(active):
            pk = df.loc[df.required_hc.idxmax()]
            m[1].metric("Peak required HC", f"{pk.required_hc:.1f}", f"at {pk.interval}", delta_color="off")
            worst = df.loc[df.variance.idxmin()]
            m[2].metric("Worst interval", f"{worst.variance:+.1f}", f"at {worst.interval}", delta_color="off")
        m[3].metric("Understaffed intervals", len(short),
                    f"{np.maximum(0, -df.variance).sum() * INTERVAL_MIN / 60:.1f} HC-hours short", delta_color="off")
        m[4].metric("Projected SL (day)", f"{day_sl:.0%}" if day_sl is not None else "—",
                    f"target {p['sl_pct']:g}%" if day_sl is not None else None, delta_color="off")

        if len(active):
            lo, hi = INTERVALS.index(active.interval.iloc[0]), INTERVALS.index(active.interval.iloc[-1])
            view = df.iloc[max(lo - 1, 0):hi + 2]
            x = alt.X("interval:N", sort=INTERVALS, title=None, axis=alt.Axis(labelAngle=-60))
            tip = ["interval", alt.Tooltip("scheduled_hc:Q", format=".1f"), alt.Tooltip("required_hc:Q", format=".1f"),
                   alt.Tooltip("variance:Q", format="+.1f"), alt.Tooltip("forecast_volume:Q", format=".0f")]
            bars = alt.Chart(view).mark_bar(opacity=0.55, color="#4c78a8").encode(
                x=x, y=alt.Y("scheduled_hc:Q", title="Headcount"), tooltip=tip)
            line = alt.Chart(view).mark_line(color="#e45756", point=True, strokeWidth=2).encode(
                x=x, y="required_hc:Q", tooltip=tip)
            st.markdown("**Scheduled (bars) vs required (red line)**")
            st.altair_chart(bars + line, width="stretch")
            var = alt.Chart(view).mark_bar().encode(
                x=x, y=alt.Y("variance:Q", title="Variance (HC)"), tooltip=tip,
                color=alt.condition("datum.variance < 0", alt.value("#e45756"), alt.value("#54a24b")))
            st.markdown("**HC variance** (below zero = understaffed)")
            st.altair_chart(var, width="stretch", height=220)
            if view.actual_volume.notna().any():
                vl = view.melt(id_vars="interval", value_vars=["forecast_volume", "actual_volume"],
                               var_name="series", value_name="contacts").dropna()
                st.markdown("**Forecast vs actual volume**")
                st.altair_chart(alt.Chart(vl).mark_line(point=True).encode(
                    x=x, y="contacts:Q", color="series:N", tooltip=["interval", "series", "contacts"]),
                    width="stretch", height=220)
                acc_rows = view.dropna(subset=["actual_volume"])
                if acc_rows.actual_volume.sum():
                    err = (acc_rows.forecast_volume - acc_rows.actual_volume).abs().sum() / acc_rows.actual_volume.sum()
                    st.caption(f"Forecast accuracy this day: **{max(0, 1 - err):.0%}** (1 − weighted absolute error).")

        show = (active if st.toggle("Hide empty intervals", True) else df).copy()
        show["projected_sl"] = (show.projected_sl * 100).round(0)
        table = show[["interval", "actual_volume", "forecast_volume", "forecast_aht", "required_on_queue",
                      "required_hc", "scheduled_hc", "variance", "projected_sl"]].round(
            {"forecast_volume": 1, "forecast_aht": 0, "required_on_queue": 1, "required_hc": 1,
             "scheduled_hc": 1, "variance": 1})
        styler = table.style.format(precision=1, na_rep="")
        st.dataframe((styler.map if hasattr(styler, "map") else styler.applymap)(_variance_color, subset=["variance"]),
                     hide_index=True, width="stretch", column_config={
                         "actual_volume": "Actual vol", "forecast_volume": "Forecast vol", "forecast_aht": "AHT (s)",
                         "required_on_queue": st.column_config.NumberColumn(
                             "Req. on queue", help="Agents needed taking contacts (Erlang C / workload)"),
                         "required_hc": st.column_config.NumberColumn("Req. HC", help="On-queue need ÷ (1 − shrinkage)"),
                         "scheduled_hc": st.column_config.NumberColumn(
                             "Sched. HC", help="On shift: schedule − leave − absences + approved OT/coverage"),
                         "variance": "Variance", "projected_sl": st.column_config.NumberColumn(
                             "Proj. SL %", help="Service level if scheduled HC (after shrinkage) works the forecast")})
        st.download_button("⬇️ Download interval plan (CSV)", df.to_csv(index=False),
                           f"interval_plan_{account}_{day}.csv")
        st.caption("Short in some intervals? Open the **🧮 OT plan** tab to get OT recommendations that fill them.")

    with t_ot:
        st.caption("Schedules stay as they are. This finds the short intervals and suggests **who should do OT and "
                   "exactly when** — extending a shift (pre/post) or a rest-day block — within your rules: "
                   f"min rest {setting('min_rest_hrs'):g}h · max {setting('max_daily_hrs'):g}h straight · "
                   f"weekly OT cap {setting('max_weekly_ot_hrs'):g}h · blocks {setting('ot_min_block_hrs'):g}–"
                   f"{setting('ot_max_ext_hrs'):g}h (rest day up to {setting('rd_ot_max_hrs'):g}h) · "
                   f"at least {setting('ot_min_eff_pct'):g}% of each block in short intervals. Change these in Settings.")
        c = st.columns([1.4, 1.4, 1])
        other = c[0].toggle("Also use agents from other accounts (same LOB)", False, key="otp_other")
        skill = c[1].text_input("Only agents with skill (optional)", "", key="otp_skill")
        key = (account, str(day), adj, weeks, other, skill)
        if c[2].button("🧮 Build OT plan", type="primary", width="stretch"):
            with st.spinner("Matching OT to short intervals…"):
                st.session_state["ot_plan"] = (key, plan_ot(account, day, adj, weeks, other, skill))
        saved = st.session_state.get("ot_plan")
        if not saved or saved[0] != key:
            st.info("Click **Build OT plan** to get recommendations for this account and date.")
        else:
            plan, curve, used = saved[1]
            if not used:
                st.warning("No volume history for this weekday yet, so there's no requirement to plan against.")
            keep_h = INTERVAL_MIN / 60
            before = float(np.maximum(0, curve.required_hc - curve.scheduled_now).sum() * keep_h)
            after = float(np.maximum(0, curve.required_hc - curve.with_ot_plan).sum() * keep_h)
            m = st.columns(4)
            m[0].metric("Short HC-hours now", f"{before:.1f}")
            m[1].metric("After OT plan", f"{after:.1f}", f"{after - before:+.1f}", delta_color="inverse")
            m[2].metric("OT hours suggested", f"{plan.hours.sum():.1f}" if len(plan) else "0")
            m[3].metric("Agents", plan.agent_id.nunique() if len(plan) else 0)
            lo = curve[(curve.required_hc > 0) | (curve.with_ot_plan > 0)]
            if len(lo):
                view = curve.iloc[max(lo.index[0] - 1, 0):lo.index[-1] + 2]
                long = view.melt("interval", var_name="series", value_name="hc")
                long["series"] = long.series.map({"required_hc": "Required", "scheduled_now": "Scheduled now",
                                                  "with_ot_plan": "With OT plan"})
                st.altair_chart(alt.Chart(long).mark_line(point=True, interpolate="step-after").encode(
                    x=alt.X("interval:N", sort=INTERVALS, title=None, axis=alt.Axis(labelAngle=-60)),
                    y=alt.Y("hc:Q", title="Headcount"),
                    color=alt.Color("series:N", scale=alt.Scale(
                        domain=["Required", "Scheduled now", "With OT plan"], range=["#e45756", "#9d9d9d", "#4c78a8"])),
                    tooltip=["interval", "series", alt.Tooltip("hc:Q", format=".1f")]), width="stretch")
            if plan.empty:
                if before > 0:
                    st.info("Nobody can take OT in the short intervals within the current rules. Try including other "
                            "accounts, raising the weekly OT cap, or lowering the minimum efficiency in Settings.")
                else:
                    st.success("No short intervals — no OT needed for this day.")
            else:
                plan_ed = plan.copy()
                plan_ed.insert(0, "use", True)
                ed = st.data_editor(plan_ed, hide_index=True, width="stretch", key=f"otp_ed_{hash(key)}",
                                    disabled=[c for c in plan_ed.columns if c != "use"], column_config={
                                        "agent_id": None, "use": st.column_config.CheckboxColumn("Use", width="small"),
                                        "shift_date": st.column_config.TextColumn(
                                            "Shift date", help="OT is logged on the date the extended shift started"),
                                        "fills_short_hrs": st.column_config.NumberColumn(
                                            "Fills (HC-hrs)", help="Short HC-hours this block covers"),
                                        "week_ot_after": st.column_config.NumberColumn("Week OT after (h)")})
                st.download_button("⬇️ OT plan (CSV)", plan.drop(columns="agent_id").to_csv(index=False),
                                   f"ot_plan_{account}_{day}.csv")
                b = st.columns(2)
                act = None
                if b[0].button("✅ Approve selected OT", type="primary"):
                    act = "Approved"
                if b[1].button("📨 Save as Pending (confirm with agents first)"):
                    act = "Pending"
                if act:
                    n = 0
                    for r in ed[ed.use].itertuples():
                        ex("INSERT INTO ot_requests(agent_id,date,ot_type,start_time,end_time,hours,reason,status,"
                           "filed_on,for_account) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (r.agent_id, r.shift_date, r.ot_type, r.start, r.end, r.hours,
                            f"OT plan: short intervals on {day} ({account})", act, str(today_site()), account))
                        if act == "Approved" and r.ot_type == "Rest Day OT":
                            ex("INSERT OR IGNORE INTO schedule_overrides VALUES(?,?,?,?,?,?)",
                               (r.agent_id, r.shift_date, "Work (RD OT)", r.start, r.end, "OT plan"))
                        n += 1
                    st.session_state.pop("ot_plan", None)
                    st.success(f"{n} OT block(s) saved as {act}." + (" Approve them later under Overtime → "
                                                                     "Approvals." if act == "Pending" else ""))
                    rerun()

    with t2:
        w0 = week_start(day)
        grid, totals = {}, []
        for i in range(7):
            d = w0 + dt.timedelta(days=i)
            pdf, used_d, _ = interval_plan(account, d, adj, weeks)
            col = f"{DAYS[i]} {d:%m-%d}"
            grid[col] = pdf.set_index("interval").variance
            grid[col][(pdf.required_hc.values == 0) & (pdf.scheduled_hc.values == 0)] = np.nan
            s = pdf[pdf.variance < -0.5]
            totals.append({"day": col, "forecast_volume": round(pdf.forecast_volume.sum()),
                           "peak_required_hc": round(pdf.required_hc.max(), 1),
                           "understaffed_intervals": len(s),
                           "hc_hours_short": round(float(np.maximum(0, -pdf.variance).sum()) * INTERVAL_MIN / 60, 1),
                           "history_weeks_used": len(used_d)})
        heat = pd.DataFrame(grid).dropna(how="all")
        st.caption("HC variance per interval (scheduled − required). Red = short, green = over. "
                   "Blank = no demand and nobody scheduled.")
        if heat.empty:
            st.info("No demand or schedules this week yet.")
        else:
            hs = heat.style.format("{:+.1f}", na_rep="")
            st.dataframe((hs.map if hasattr(hs, "map") else hs.applymap)(_variance_color), width="stretch",
                         height=min(900, 38 + 35 * len(heat)))
        st.dataframe(pd.DataFrame(totals), hide_index=True, width="stretch")

    with t3:
        cov = q("SELECT account, MIN(date) AS first_date, MAX(date) AS last_date, COUNT(DISTINCT date) AS days, "
                "ROUND(SUM(volume)) AS total_volume FROM interval_volume GROUP BY account ORDER BY account")
        st.markdown("**History loaded**")
        if not len(cov):
            st.caption("No volume history yet.")
        else:
            cov["in_account_list"] = np.where(cov.account.isin(accts), "Yes", "No — leftover/sample")
            st.dataframe(cov, hide_index=True, width="stretch")
            orphans = cov[~cov.account.isin(accts)].account.tolist()
            if orphans:
                st.caption("⚠️ Some history belongs to accounts that aren't in your account list any more "
                           "(for example the sample accounts). Delete it below.")
            c = st.columns([3, 1.4])
            chosen = c[0].multiselect("Delete ALL volume history for", cov.account.tolist(), default=orphans,
                                      key="iv_hist_del")
            if c[1].button("🗑️ Delete history", disabled=not chosen, key="iv_hist_btn", width="stretch"):
                for a in chosen:
                    ex("DELETE FROM interval_volume WHERE account=?", (a,))
                st.session_state.pop("ot_plan", None)
                st.session_state.pop("iv_hist_del", None)
                rerun()
        st.markdown("**Upload interval volume** (CSV from your ACD, chat or ticketing platform)")
        st.caption("Columns: `date`, `interval` (start time, e.g. 13:30 or 1:30 PM), `volume` (offered/received), "
                   "and optionally `aht` in seconds and `account`. A single `datetime` column also works. "
                   "15-minute data is combined into 30-minute intervals.")
        tmpl = pd.DataFrame({"date": ["2026-10-05"] * 3, "interval": ["08:00", "08:30", "09:00"],
                             "volume": [42, 55, 61], "aht": [355, 362, 348]})
        st.download_button("Download template", tmpl.to_csv(index=False), "interval_volume_template.csv")
        c = st.columns(3)
        up_acc = c[0].selectbox("Account (if the file has no account column)", accts,
                                index=accts.index(account), key="iv_up_acc")
        src_tz = c[1].selectbox("Timezone of the times in the file", COMMON_TZ,
                                index=COMMON_TZ.index("America/Chicago"), key="iv_src_tz",
                                help="US client reports are usually in the client's time; daylight saving is handled")
        def_aht = c[2].number_input("AHT (s) if not in file", 1.0, 36000.0, 360.0, key="iv_def_aht")
        st.caption("**Intercom:** you can upload the raw Intercom conversation export as-is — it's counted per 30 "
                   "minutes automatically (inbound only, duplicates and conversations no teammate replied to are left "
                   "out). Do this once a week with the previous week's export.")
        up = st.file_uploader("Volume CSV", type=["csv"], key="iv_file")
        if up is not None:
            try:
                raw = pd.read_csv(up, low_memory=False)
                if intercom_export(raw):
                    chans = sorted(raw["Channel"].dropna().astype(str).unique())
                    pick = st.multiselect("Intercom channels for this account", chans, key="iv_ic_ch",
                                          default=[c for c in chans if c.lower() != "phone call"] or chans,
                                          help="E.g. leave out Phone call for a messaging account, and upload the "
                                               "same file again with only Phone call for the phone account")
                    raw = intercom_to_rows(raw, pick)
                    st.caption(f"Intercom export detected: {len(raw):,} conversations counted.")
                site_tz = setting_str("site_tz")
                acc_col = next((c for c in raw.columns if c.lower().strip() == "account"), None)
                parts = raw.groupby(raw[acc_col].astype(str)) if acc_col else [(up_acc, raw)]
                frames, skipped = [], 0
                for acc_name, part in parts:
                    norm, sk = normalize_volume(part, src_tz, site_tz, def_aht)
                    norm.insert(0, "account", acc_name)
                    frames.append(norm)
                    skipped += sk
                ready = pd.concat(frames, ignore_index=True)
                st.write(f"**{len(ready):,}** intervals ready ({ready.date.min()} → {ready.date.max()}, "
                         f"{src_tz} → {site_tz})" + (f" · {skipped} rows skipped (unreadable time)" if skipped else ""))
                unknown = sorted(set(ready.account) - set(accts))
                if unknown:
                    st.warning(f"Accounts not in your list (will be added as Voice): {', '.join(unknown)}")
                st.dataframe(ready.head(20), hide_index=True)
                if st.button("Import volume", type="primary"):
                    for u in unknown:
                        upsert_account({"name": u, "lob": "Voice"})
                    exmany("INSERT OR REPLACE INTO interval_volume VALUES(?,?,?,?,?)",
                           ready[["account", "date", "interval", "volume", "aht"]].values.tolist())
                    st.success(f"Imported {len(ready):,} intervals.")
                    rerun()
            except Exception as e:
                st.error(f"Couldn't read that file: {e}")
        with st.expander("Delete part of an account's history (date range)"):
            have = cov.account.tolist() if len(cov) else []
            if not have:
                st.caption("Nothing to delete.")
            else:
                c = st.columns(4)
                del_acc = c[0].selectbox("Account", have, index=have.index(account) if account in have else 0,
                                         key="iv_del_acc")
                row = cov[cov.account == del_acc].iloc[0]
                lo, hi = dt.date.fromisoformat(row.first_date), dt.date.fromisoformat(row.last_date)
                d1 = c[1].date_input("From", lo, key=f"iv_del1_{del_acc}")
                d2 = c[2].date_input("To", hi, key=f"iv_del2_{del_acc}")
                if c[3].button("Delete range", key="iv_del_btn"):
                    ex("DELETE FROM interval_volume WHERE account=? AND date BETWEEN ? AND ?",
                       (del_acc, str(d1), str(d2)))
                    st.session_state.pop("ot_plan", None)
                    rerun()


# ----------------------------------------------------------------- schedule grid import
_LOOKALIKE = str.maketrans({"А": "A", "а": "a", "Р": "P", "р": "p", "М": "M", "м": "m", " ": " "})
_TIME_RX = re.compile(r"(\d{1,4})(?::(\d{2}))?(?::\d{2})?\s*([AP])\.?\s*M\b", re.I)
_LEAVE_WORDS = [("EMERGENCY", "Emergency (EL)"), ("SICK", "Sick (SL)"), ("BEREAVE", "Bereavement (BL)"),
                ("LEAVE", "Planned Time Off (PTO)"), ("VL", "Planned Time Off (PTO)"), ("PTO", "Planned Time Off (PTO)")]


def _hhmm(num, minutes, ampm):
    num = str(num)
    if not minutes and len(num) > 2:  # 230PM -> 2:30 PM, 1130AM -> 11:30 AM
        num, minutes = num[:-2], num[-2:]
    h, m = int(num), int(minutes or 0)
    if not (1 <= h <= 12 and 0 <= m < 60):
        return None
    h = h % 12 + (12 if ampm.upper() == "P" else 0)
    return f"{h:02d}:{m:02d}"


def _block_times(text):
    return [t for t in (_hhmm(n, m, ap) for n, m, ap in _TIME_RX.findall(text)) if t]


def parse_schedule_cell(raw, default_hours=8):
    """Read one cell of the team schedule. Returns (entry, warning).
    entry: None (not scheduled) | ("rest",) | ("leave", type) | ("work"|"training", start, end)."""
    text = str(raw or "").translate(_LOOKALIKE).strip()
    up = text.upper()
    if not text:
        return None, None
    if up in ("RD", "OFF", "REST DAY", "DAY OFF"):
        return ("rest",), None
    kind = "training" if "TRAINING" in up else "work"
    blocks = [b for b in re.split(r"[|;&]| AND ", text, flags=re.I) if b.strip()]
    parsed = []
    for b in blocks:
        ts = _block_times(b)
        if not ts:
            continue
        start = ts[0]
        end = ts[1] if len(ts) > 1 else None
        if end is None:
            s_dt = dt.datetime.strptime(start, "%H:%M")
            end = (s_dt + dt.timedelta(hours=default_hours)).strftime("%H:%M")
        parsed.append((start, end, len(ts)))
    leave = next((name for word, name in _LEAVE_WORDS if re.search(rf"\b{word}\b", up)), None)
    if not parsed:
        if leave:
            return ("leave", leave), None
        if kind == "training":
            return ("training", None, None), None
        return None, f"couldn't read '{text}'"
    warn = None

    def length(se):
        s_, e_ = shift_window(dt.date(2000, 1, 1), se[0], se[1])
        return (e_ - s_).total_seconds() / 3600
    if len(parsed) > 1:
        warn = f"'{text}' has {len(parsed)} blocks; only one shift per day is supported, kept the longest"
    best = max(parsed, key=length)
    if best[2] > 2:
        warn = f"'{text}' has more than two times; used {best[0]}–{best[1]}"
    if length(best) > 16:
        s_dt = dt.datetime.strptime(best[0], "%H:%M")
        best = (best[0], (s_dt + dt.timedelta(hours=default_hours)).strftime("%H:%M"), best[2])
        warn = f"'{text}' looks longer than 16h; used {best[0]}–{best[1]}"
    if leave:
        warn = (warn + "; " if warn else "") + f"'{text}' mixes a shift and leave; kept the shift"
    return (kind, best[0], best[1]), warn


def _slug(name):
    return re.sub(r"[^a-z0-9]+", ".", str(name).strip().lower()).strip(".")


def read_schedule_grid(df, year):
    """Turn the team schedule sheet (dates down, people across) into {name: {date: cell}}."""
    raw = df.astype(str).replace({"nan": "", "None": ""}).values.tolist()
    hdr_i = next((i for i, r in enumerate(raw[:15]) if str(r[0]).strip().upper() == "DATE"), None)
    if hdr_i is None:
        raise ValueError("Couldn't find the header row starting with DATE.")
    hdr = [str(h).strip() for h in raw[hdr_i]]
    people = [(j, h) for j, h in enumerate(hdr) if j >= 2 and h and h.upper() not in ("DATE", "DAY")]
    grid, last_month = {}, None
    for r in raw[hdr_i + 1:]:
        cell0 = str(r[0]).strip() if r else ""
        if not cell0:
            continue
        try:
            d = dt.datetime.strptime(f"{cell0} {year}", "%b %d %Y").date()
        except ValueError:
            try:
                d = pd.to_datetime(cell0).date()
            except Exception:
                continue
        for j, name in people:
            grid.setdefault(name.title(), {})[d] = r[j] if j < len(r) else ""
    if not grid:
        raise ValueError("No dated rows found under the header.")
    return grid


def plan_schedule_import(grid, start, end, default_hours=8):
    """From the grid, work out for each person:
    - their permanent schedule (usual shift + days off), with a new version whenever the pattern changes for
      2+ weeks in a row (e.g. a shift change from Nov 2),
    - one-off changes on single dates, approved time off, and training.
    Blank cells after someone's last filled-in date mean "not scheduled yet" and are ignored."""
    plan = {"people": [], "exceptions": [], "leaves": [], "warnings": []}
    for name, days in grid.items():
        parsed = {}
        for d, raw in days.items():
            entry, warn = parse_schedule_cell(raw, default_hours)
            parsed[d] = entry
            if warn and start <= d <= end:
                plan["warnings"].append(f"{name} {d:%b %d}: {warn}")
        filled = [d for d, e in parsed.items() if e is not None]
        if not filled:
            continue
        last = min(max(filled), end)
        rng = [d for d in sorted(parsed) if start <= d <= last]
        if not any(parsed[d] and parsed[d][0] in ("work", "training", "leave") for d in rng):
            continue
        # weekly signature: (usual shift, days off)
        weeks = {}
        for d in rng:
            weeks.setdefault(week_start(d), []).append(d)
        sigs = []
        for w0, ds in sorted(weeks.items()):
            work = Counter((parsed[d][1], parsed[d][2]) for d in ds if parsed[d] and parsed[d][0] == "work")
            off = frozenset(DAYS[d.weekday()] for d in ds if parsed[d] is None or parsed[d][0] == "rest")
            full = len(ds) == 7
            sigs.append((w0, work.most_common(1)[0][0] if work else None, off if full else None))
        # fill partial weeks with the neighbouring full week's days off
        known = [x for x in sigs if x[1] and x[2] is not None]
        if not known:
            any_work = Counter((e[1], e[2]) for e in parsed.values() if e and e[0] == "work")
            if not any_work:
                continue
            known = [(sigs[0][0], any_work.most_common(1)[0][0], frozenset())]
        first_sig = Counter((x[1], x[2]) for x in known[:4]).most_common(1)[0][0]  # one odd week doesn't count
        versions = [(start, first_sig[0], first_sig[1])]
        i = next(k for k, x in enumerate(known) if (x[1], x[2]) == first_sig)
        while i < len(known):
            w0, shift, off = known[i]
            cur = versions[-1]
            if (shift, off) != (cur[1], cur[2]):
                nxt = known[i + 1] if i + 1 < len(known) else None
                if nxt and (nxt[1], nxt[2]) == (shift, off) and (nxt[0] - w0).days == 7:
                    versions.append((max(w0, start), shift, off))
            i += 1

        def pattern_on(d):
            v = [x for x in versions if x[0] <= d]
            return v[-1] if v else versions[0]
        plan["people"].append(dict(
            name=name, username=_slug(name), shift_start=versions[0][1][0], shift_end=versions[0][1][1],
            rest_days=",".join(x for x in DAYS if x in versions[0][2]), last_scheduled=last,
            versions=[(v[0], v[1][0], v[1][1], ",".join(x for x in DAYS if x in v[2])) for v in versions]))
        for d in rng:
            e = parsed[d]
            _, (ss, se), off = pattern_on(d)
            is_rest_day = DAYS[d.weekday()] in off
            if e is None or e[0] == "rest":
                if not is_rest_day:
                    plan["exceptions"].append(dict(name=name, date=d, kind="Rest Day", start=None, end=None))
            elif e[0] == "leave":
                plan["leaves"].append(dict(name=name, date=d, type=e[1]))
            elif e[0] == "training":
                plan["exceptions"].append(dict(name=name, date=d, kind="Training", start=e[1] or ss, end=e[2] or se))
            elif is_rest_day or (e[1], e[2]) != (ss, se):
                plan["exceptions"].append(dict(name=name, date=d, kind="Work", start=e[1], end=e[2]))
    merged = []
    for lv in sorted(plan["leaves"], key=lambda x: (x["name"], x["date"])):
        if merged and merged[-1]["name"] == lv["name"] and merged[-1]["type"] == lv["type"] \
                and (lv["date"] - merged[-1]["end"]).days == 1:
            merged[-1]["end"] = lv["date"]
        else:
            merged.append(dict(name=lv["name"], start=lv["date"], end=lv["date"], type=lv["type"]))
    plan["leaves"] = merged
    return plan


def apply_schedule_import(plan, start, end, account, lob, deactivate=()):
    """Write the plan: agents (new or updated), permanent schedules from `start`, exceptions and time off."""
    ag = q("SELECT id, emp_id, name FROM agents")
    by_name = {str(n).strip().lower(): int(i) for i, n in zip(ag.id, ag.name)}
    by_emp = {str(e).strip().lower(): int(i) for i, e in zip(ag.id, ag.emp_id)}
    ids, created = {}, 0
    for p in plan["people"]:
        aid = by_name.get(p["name"].lower()) or by_emp.get(p["username"])
        if aid is None:
            aid = ex("INSERT INTO agents(emp_id,name,account,lob,shift_start,shift_end,rest_days,active) "
                     "VALUES(?,?,?,?,?,?,?,?)", (p["username"], p["name"], account, lob, p["shift_start"],
                                                 p["shift_end"], p["rest_days"], 1))
            created += 1
        else:
            ex("UPDATE agents SET active=1 WHERE id=?", (aid,))
        ex("DELETE FROM schedule_changes WHERE agent_id=? AND effective_from>=? AND note='schedule import'",
           (aid, str(start)))
        for eff, ss, se, rd in p["versions"]:
            set_permanent_schedule(aid, eff, ss, se, rd, "schedule import")
        ids[p["name"]] = aid
    for aid in deactivate:
        ex("UPDATE agents SET active=0 WHERE id=?", (int(aid),))
    touched = list(ids.values())
    if touched:
        marks = ",".join("?" * len(touched))
        ex(f"DELETE FROM schedule_overrides WHERE agent_id IN ({marks}) AND date BETWEEN ? AND ? "
           "AND (note IS NULL OR note IN ('', 'schedule import'))", (*touched, str(start), str(end)))
        ex(f"DELETE FROM leaves WHERE agent_id IN ({marks}) AND reason='schedule import' "
           "AND start_date<=? AND end_date>=?", (*touched, str(end), str(start)))
    exmany("INSERT OR REPLACE INTO schedule_overrides VALUES(?,?,?,?,?,?)",
           [(ids[x["name"]], str(x["date"]), x["kind"], x["start"], x["end"], "schedule import")
            for x in plan["exceptions"] if x["name"] in ids])
    today = str(today_site())
    exmany("INSERT INTO leaves(agent_id,leave_type,start_date,end_date,days,status,reason,filed_on,decided_by) "
           "VALUES(?,?,?,?,?,?,?,?,?)",
           [(ids[x["name"]], x["type"], str(x["start"]), str(x["end"]), (x["end"] - x["start"]).days + 1,
             "Approved", "schedule import", today, "schedule import") for x in plan["leaves"] if x["name"] in ids])
    sync_current_schedules()
    return dict(created=created, updated=len(ids) - created, exceptions=len(plan["exceptions"]),
                leaves=len(plan["leaves"]))


def _read_any_table(up):
    name = up.name.lower()
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(up, header=None, dtype=str)
    data = up.getvalue().decode("utf-8-sig", errors="replace")
    sep = "\t" if data.count("\t") > data.count(",") else ","
    return pd.read_csv(io.StringIO(data), header=None, dtype=str, sep=sep, keep_default_na=False,
                       engine="python", on_bad_lines="skip")


def page_schedule_import(acct):
    st.markdown("**Import the team schedule** (dates down the side, one column per person)")
    st.caption("Export your schedule sheet as CSV/TSV (or upload the Excel file). Cells can be a start time "
               "(e.g. `2:00 PM`), a range (`12PM-8PM`, `10PM / 8AM`), `RD`, `LEAVE`, `SICK`, `VL`, `EMERGENCY` or "
               "`TRAINING`. Each person's **permanent schedule** is worked out from the recent weeks (their usual "
               "shift and days off); anything different on a given date is saved as a one-off change, and leave "
               "becomes approved time off. Re-uploading replaces what an earlier import set for those dates.")
    up = st.file_uploader("Schedule file", type=["csv", "tsv", "txt", "xlsx"], key="grid_file")
    c = st.columns(4)
    year = c[0].number_input("Year", 2020, 2100, today_site().year, key="grid_year")
    start = c[1].date_input("Load from", week_start(today_site()), key="grid_start")
    hours = c[2].number_input("Hours when only a start time is given", 1.0, 16.0, 8.0, step=0.5, key="grid_hours")
    accts = account_names() or ["ZEVO Messaging"]
    account = c[3].selectbox("Account for new people", accts,
                             index=accts.index("ZEVO Messaging") if "ZEVO Messaging" in accts else 0, key="grid_acct")
    if up is None:
        return
    try:
        grid = read_schedule_grid(_read_any_table(up), int(year))
    except Exception as e:
        st.error(f"Couldn't read that file: {e}")
        return
    last = max(max(v) for v in grid.values())
    end = st.date_input("Load until", last, key="grid_end")
    plan = plan_schedule_import(grid, start, end, hours)
    ppl = pd.DataFrame(plan["people"])
    if ppl.empty:
        st.warning("Nobody with shifts was found in that date range.")
        return
    ag = q("SELECT id, emp_id, name FROM agents WHERE active=1")
    known = {str(n).lower() for n in ag.name} | {str(e).lower() for e in ag.emp_id}
    ppl["status"] = ["update" if (n.lower() in known or u in known) else "new" for n, u in zip(ppl.name, ppl.username)]
    ppl["exceptions"] = [sum(1 for x in plan["exceptions"] if x["name"] == n) for n in ppl.name]
    ppl["time_off"] = [sum(1 for x in plan["leaves"] if x["name"] == n) for n in ppl.name]
    st.write(f"**{len(ppl)} people** · permanent schedules from {start:%b %d} · "
             f"{len(plan['exceptions'])} one-off changes · {len(plan['leaves'])} time-off entries "
             f"· {start:%b %d} → {end:%b %d, %Y}")
    ppl["rest_days"] = ppl.rest_days.replace("", "none")
    ppl["later_changes"] = ["; ".join(f"from {v[0]:%b %d}: {v[1]}–{v[2]}, off {v[3] or 'none'}" for v in vs[1:])
                            for vs in ppl.versions]
    st.dataframe(ppl[["name", "username", "status", "shift_start", "shift_end", "rest_days", "later_changes",
                      "last_scheduled", "exceptions", "time_off"]], hide_index=True, width="stretch", column_config={
        "username": st.column_config.TextColumn("Username / Emp ID", help="Used as their login username"),
        "rest_days": "Days off", "later_changes": "Permanent changes later",
        "last_scheduled": st.column_config.DateColumn(
            "Last date in sheet", help="After this date their permanent schedule continues — deactivate them in "
                                       "Roster if they've left"),
        "exceptions": "One-off changes", "time_off": "Time off"})
    if plan["warnings"]:
        with st.expander(f"⚠️ {len(plan['warnings'])} cell(s) needed a judgment call"):
            st.write("\n".join(f"- {w}" for w in plan["warnings"]))
    names = {n.lower() for n in ppl.name} | set(ppl.username)
    others = ag[[str(n).lower() not in names and str(e).lower() not in names for n, e in zip(ag.name, ag.emp_id)]]
    deactivate = []
    if len(others):
        lbl = {int(r.id): f"{r.name} ({r.emp_id})" for r in others.itertuples()}
        deactivate = st.multiselect("These active agents aren't in the schedule — deactivate them?", list(lbl),
                                    default=list(lbl), format_func=lbl.get, key="grid_deact",
                                    help="Deactivated agents disappear from schedules and can't sign in; "
                                         "their history is kept")
    if st.button("📥 Import schedule", type="primary", key="grid_go"):
        res = apply_schedule_import(plan, start, end, account,
                                    q("SELECT lob FROM accounts WHERE name=?", (account,)).lob.iloc[0]
                                    if account in account_names() else "Non-Voice", deactivate)
        for aid in deactivate:
            for u in q("SELECT username FROM users WHERE agent_id=?", (aid,)).username:
                ex("UPDATE users SET active=0 WHERE username=?", (u,))
                end_user_sessions(u)
        st.session_state["grid_done"] = res
        rerun()
    if st.session_state.get("grid_done"):
        r = st.session_state.pop("grid_done")
        st.success(f"Imported: {r['created']} new people, {r['updated']} updated, {r['exceptions']} one-off changes, "
                   f"{r['leaves']} time-off entries. Next: **Users → Create agent logins** for anyone new.")


def page_permanent_change(acct):
    st.markdown("**Permanent schedule change**")
    st.caption("Use this when someone's regular shift or days off change for good. Pick the date it starts — "
               "earlier dates keep the old schedule, so past attendance stays correct. For a single day, use "
               "**Change a schedule** instead.")
    ag = agents_df(True, acct)
    labels = agent_label_map(ag)
    if not labels:
        st.info("No agents.")
        return
    with st.form("perm_change"):
        ids = st.multiselect("Agent(s)", list(labels), format_func=labels.get, key="pc_ids")
        c = st.columns(3)
        eff = c[0].date_input("Starting", today_site() + ONE_DAY, key="pc_eff")
        ss = c[1].text_input("New shift start (HH:MM)", "08:00", key="pc_ss")
        se = c[2].text_input("New shift end (HH:MM)", "16:00", key="pc_se")
        rd = st.multiselect("Days off", DAYS, default=["Sat", "Sun"], key="pc_rd")
        if st.form_submit_button("Save permanent change", type="primary"):
            if not ids:
                st.error("Pick at least one agent.")
            elif not (re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", ss) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", se)):
                st.error("Times must be HH:MM (24-hour), e.g. 22:00.")
            else:
                for aid in ids:
                    set_permanent_schedule(aid, eff, ss, se, ",".join(rd), f"changed by {current_user()['username']}")
                st.success(f"Saved. From {eff:%a %b %d}: {ss}–{se}, days off {', '.join(rd) or 'none'}.")
    hist = q("SELECT a.name, c.effective_from, c.shift_start, c.shift_end, c.rest_days, c.note FROM schedule_changes c "
             "JOIN agents a ON a.id=c.agent_id WHERE c.effective_from>'2000-01-01' ORDER BY c.effective_from DESC, a.name")
    if acct != "All accounts":
        hist = hist[hist.name.isin(ag.name)]
    if len(hist):
        st.markdown("**Change history**")
        st.dataframe(hist, hide_index=True, width="stretch")


# ----------------------------------------------------------------- logins
PW_ITER = 200_000
LOCK_AFTER, LOCK_MIN = 5, 10
CLOCK_EVENTS = {"login": "Log in", "break_start": "Break start", "lunch_start": "Lunch start",
                "break_end": "Break end", "logout": "Log out"}


def hash_pw(pw):
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PW_ITER)
    return f"pbkdf2_sha256${PW_ITER}${salt.hex()}${h.hex()}"


def check_pw(pw, stored):
    try:
        _, it, salt, h = str(stored).split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def temp_password():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def pw_problem(pw):
    if len(pw) < 8:
        return "Use at least 8 characters."
    if pw.isalpha() or pw.isdigit():
        return "Mix letters and numbers."
    return None


def get_user(username):
    df = q("SELECT * FROM users WHERE lower(username)=lower(?)", (str(username).strip(),))
    return df.iloc[0].to_dict() if len(df) else None


def authenticate(username, pw):
    u = get_user(username)
    now = now_site()
    if u is None:
        return None, "Wrong username or password."
    if not int(u.get("active") or 0):
        return None, "This login is disabled. Please contact your admin."
    if u.get("locked_until") and str(u["locked_until"]) > now.isoformat():
        return None, f"Too many wrong attempts. Try again after {str(u['locked_until'])[11:16]}."
    if not check_pw(pw, u["pw_hash"]):
        failed = int(u.get("failed") or 0) + 1
        locked = (now + dt.timedelta(minutes=LOCK_MIN)).isoformat() if failed >= LOCK_AFTER else None
        ex("UPDATE users SET failed=?, locked_until=? WHERE username=?",
           (0 if locked else failed, locked, u["username"]))
        return None, ("Too many wrong attempts — locked for %d minutes." % LOCK_MIN) if locked \
            else "Wrong username or password."
    ex("UPDATE users SET failed=0, locked_until=NULL, last_login=? WHERE username=?", (now.isoformat(), u["username"]))
    return u, None


SESSION_COOKIE = "wfm_session"


def _token_hash(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def start_session(u, remember=True, sid=None):
    st.session_state["auth"] = {"username": u["username"], "role": u["role"],
                                "agent_id": int(u["agent_id"]) if pd.notna(u.get("agent_id")) and
                                u.get("agent_id") not in (None, "") else None, "sid": sid}
    hrs = setting("remember_hrs")
    if remember and hrs > 0 and sid is None:
        token = secrets.token_urlsafe(32)  # only its hash is stored; the browser keeps the token
        now = now_site()
        ex("DELETE FROM sessions WHERE expires<?", (now.isoformat(),))
        ex("INSERT INTO sessions(token_hash,username,created,expires) VALUES(?,?,?,?)",
           (_token_hash(token), u["username"], now.isoformat(), (now + dt.timedelta(hours=hrs)).isoformat()))
        st.session_state["auth"]["sid"] = _token_hash(token)
        st.session_state["_cookie_set"] = (token, int(hrs * 3600))


def end_user_sessions(username, keep_sid=None):
    """Sign a user out everywhere (password reset, disabled, deleted...)."""
    ex("DELETE FROM sessions WHERE username=? AND token_hash IS NOT ?", (username, keep_sid))


def logout_session():
    sid = (current_user() or {}).get("sid")
    if sid:
        ex("DELETE FROM sessions WHERE token_hash=?", (sid,))
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.session_state["_cookie_clear"] = True
    st.session_state["_signed_out"] = True  # don't re-read the old cookie in this browser tab


def _page_js(code):
    """Run a small script in the app page (used only for the sign-in key in the browser)."""
    if "unsafe_allow_javascript" in inspect.signature(st.html).parameters:
        st.html(f"<script>{code}</script>", unsafe_allow_javascript=True)
    else:  # older Streamlit: run it from a component frame by injecting into the page
        components.html("<script>(function(){const d = window.parent.document; const s = d.createElement('script');"
                        f"s.text = {json.dumps(code)}; d.head.appendChild(s); s.remove();}})();</script>", height=0)


def _cookie_js():
    """Save or clear the sign-in key in the browser: a cookie, plus local storage as a backup for hosts
    that don't pass cookies through to the app."""
    if st.session_state.get("_cookie_set"):
        token, age = st.session_state.pop("_cookie_set")
        _page_js(f"document.cookie = '{SESSION_COOKIE}={token}; path=/; max-age={age}; SameSite=Lax'"
                 " + (location.protocol === 'https:' ? '; Secure' : '');"
                 f"try {{ localStorage.setItem('{SESSION_COOKIE}', JSON.stringify("
                 f"{{t: '{token}', exp: Date.now() + {age * 1000}}})); }} catch (e) {{}}")
    elif st.session_state.pop("_cookie_clear", False):
        _page_js(f"document.cookie = '{SESSION_COOKIE}=; path=/; max-age=0; SameSite=Lax';"
                 f"try {{ localStorage.removeItem('{SESSION_COOKIE}'); }} catch (e) {{}}")
    elif not current_user() and not st.session_state.get("_signed_out") and not st.session_state.get("_ls_tried") \
            and setting("remember_hrs") > 0:
        # Backup path: the cookie didn't reach us, so ask the page for the saved key (reloads once with ?s=...).
        st.session_state["_ls_tried"] = True
        _page_js(f"try {{ const v = JSON.parse(localStorage.getItem('{SESSION_COOKIE}') || 'null');"
                 "const u = new URL(location.href);"
                 "if (v && v.t && v.exp > Date.now() && !u.searchParams.has('s')) {"
                 "u.searchParams.set('s', v.t); location.replace(u.toString()); } } catch (e) {}")


def _session_from_token(token):
    h = _token_hash(token)
    row = q("SELECT username, expires FROM sessions WHERE token_hash=?", (h,))
    if row.empty or str(row.expires.iloc[0]) < now_site().isoformat():
        return None, h
    u = get_user(row.username.iloc[0])
    if u is None or not int(u.get("active") or 0):
        return None, h
    return u, h


def restore_from_cookie():
    """Sign back in after a page refresh using the browser's sign-in cookie."""
    if st.session_state.get("_signed_out") or setting("remember_hrs") <= 0:
        return False
    token = None
    try:
        if not os.environ.get("WFM_TEST_NO_COOKIES"):  # automated tests of the backup path only
            token = st.context.cookies.get(SESSION_COOKIE)
    except Exception:
        token = None
    from_url = st.query_params.get("s")
    if from_url:
        st.query_params.clear()  # take the key out of the address bar straight away
        token = token or from_url
    if not token:
        return False
    u, h = _session_from_token(token)
    if u is None:
        st.session_state["_cookie_clear"] = True
        return False
    start_session(u, sid=h)
    return True


def auth_gate():
    """Show the login (or first-admin setup) screen until someone is signed in."""
    u = current_user()
    if not u and restore_from_cookie():
        u = current_user()
    _cookie_js()
    if u:
        fresh = get_user(u["username"])
        ended = u.get("sid") and q("SELECT 1 FROM sessions WHERE token_hash=?", (u["sid"],)).empty
        if fresh is None or not int(fresh.get("active") or 0) or ended:
            logout_session()
            st.rerun()
        u["role"] = fresh["role"]
        if int(fresh.get("must_change") or 0):
            page_change_password(forced=True)
            st.stop()
        return
    n_admins = int(q("SELECT COUNT(*) n FROM users WHERE role='admin'").n[0])
    _, mid, _ = st.columns([1, 1.3, 1])
    with mid:
        st.title("🗓️ WFM Scheduler")
        if n_admins == 0:
            if not USE_SHEETS:
                st.error("⚠️ **Not connected to Google Sheets.** This app is saving to a temporary file that is erased "
                         "whenever the app restarts (every update, and after it sleeps) — so you'd keep landing on "
                         "this screen. Add your Google key and `sheet_url` in the app's **Secrets** (see README), "
                         "then come back." if ON_CLOUD else
                         "Not connected to Google Sheets — data is saved in the local file `wfm.db` on this computer.")
            st.subheader("First-time setup: create the admin login")
            st.caption("No admin exists yet. Create the first admin now, then add other admins and agent logins "
                       "under **Users**.")
            setup_code = str(secret("setup_code") or "")
            code_needed = bool(setup_code)
            with st.form("setup"):
                un = st.text_input("Admin username")
                pw = st.text_input("Password", type="password")
                pw2 = st.text_input("Repeat password", type="password")
                code = st.text_input("Setup code (from your app secrets)", type="password") if code_needed else ""
                if st.form_submit_button("Create admin", type="primary"):
                    prob = pw_problem(pw)
                    if code_needed and not hmac.compare_digest(code, setup_code):
                        st.error("Wrong setup code.")
                    elif not un.strip():
                        st.error("Enter a username.")
                    elif prob:
                        st.error(prob)
                    elif pw != pw2:
                        st.error("Passwords don't match.")
                    elif int(q("SELECT COUNT(*) n FROM users WHERE role='admin'").n[0]):
                        st.error("An admin was just created. Please sign in.")
                    else:
                        ex("INSERT INTO users(username,pw_hash,role,active,must_change,created_on) VALUES(?,?,?,?,?,?)",
                           (un.strip(), hash_pw(pw), "admin", 1, 0, now_site().isoformat()))
                        flush_to_sheet(sync=True)
                        start_session(get_user(un.strip()))
                        st.rerun()
        else:
            role = st.radio("Sign in as", ["🧑‍💼 Agent", "🛡️ Admin"], horizontal=True, key="login_role")
            want = "admin" if "Admin" in role else "agent"
            with st.form("login"):
                un = st.text_input("Username", placeholder="Admin username" if want == "admin" else "Your username")
                pw = st.text_input("Password", type="password")
                if st.form_submit_button(f"Sign in as {want.title()}", type="primary", width="stretch"):
                    user, err = authenticate(un, pw)
                    if err:
                        st.error(err)
                    elif user["role"] != want:
                        other = "Admin" if user["role"] == "admin" else "Agent"
                        st.error(f"That's an {other.lower()} login — choose **{other}** above and sign in again.")
                    else:
                        start_session(user)
                        st.rerun()
            st.caption("Agents: log in, take breaks and file time off or OT. Admins: schedules, attendance and "
                       "reports. Forgot your password? Ask an admin to reset it.")
    st.stop()


def page_change_password(forced=False):
    u = current_user()
    _, mid, _ = st.columns([1, 1.3, 1]) if forced else (None, st.container(), None)
    with mid:
        st.subheader("🔑 Set a new password" if forced else "🔑 Change password")
        if forced:
            st.caption("You're using a temporary password. Please choose your own to continue.")
        with st.form("chpw"):
            old = st.text_input("Current password", type="password")
            pw = st.text_input("New password", type="password", help="At least 8 characters, letters and numbers")
            pw2 = st.text_input("Repeat new password", type="password")
            if st.form_submit_button("Save password", type="primary"):
                me = get_user(u["username"])
                prob = pw_problem(pw)
                if not check_pw(old, me["pw_hash"]):
                    st.error("Current password is wrong.")
                elif prob:
                    st.error(prob)
                elif pw != pw2:
                    st.error("New passwords don't match.")
                elif pw == old:
                    st.error("Choose a password different from the current one.")
                else:
                    ex("UPDATE users SET pw_hash=?, must_change=0 WHERE username=?", (hash_pw(pw), me["username"]))
                    end_user_sessions(me["username"], keep_sid=u.get("sid"))
                    flush_to_sheet(sync=True)
                    st.success("Password saved.")
                    if forced:
                        st.rerun()
        if forced and st.button("Sign out"):
            logout_session()
            st.rerun()


# ----------------------------------------------------------------- time clock
def agent_row(agent_id):
    df = q("SELECT * FROM agents WHERE id=?", (agent_id,))
    return df.iloc[0] if len(df) else None


def _dt(s):
    """Parse a stored timestamp; tolerant of hand edits in the sheet (e.g. '2026-10-12 8:00')."""
    try:
        return dt.datetime.fromisoformat(str(s).strip())
    except ValueError:
        return pd.to_datetime(str(s).strip(), format="mixed").to_pydatetime().replace(tzinfo=None)


def agent_shift_windows(agent_id, d1, d2):
    """Work windows (with login-open time) for one agent's shifts starting d1..d2."""
    sch = build_schedule(d1, d2, agent_id=agent_id)
    sch = sch[sch.kind.isin(WORK_KINDS)]
    ot = q("SELECT date, start_time, end_time FROM ot_requests WHERE agent_id=? AND status='Approved' "
           "AND date BETWEEN ? AND ?", (agent_id, str(d1), str(d2)))
    early = dt.timedelta(hours=setting("login_early_hrs"))
    out = []
    for r in sch.itertuples():
        d = dt.date.fromisoformat(r.date)
        s, e = shift_window(d, r.shift_start, r.shift_end)
        opens = s - early
        for o in ot[ot.date == r.date].itertuples():
            if o.start_time and o.end_time:
                os_, oe = place_ot(d, o.start_time, o.end_time, (s, e))
                if oe <= s:  # approved pre-shift OT: allow logging in for it
                    opens = min(opens, os_ - dt.timedelta(minutes=30))
        out.append(dict(shift_date=r.date, kind=r.kind, start=s, end=e, opens=opens,
                        label=f"{r.shift_start}–{r.shift_end}"))
    return out


def resolve_shift(agent_id, now):
    """Which shift a log-in at `now` belongs to (handles graveyard shifts and early OT log-ins)."""
    wins = [w for w in agent_shift_windows(agent_id, now.date() - ONE_DAY, now.date() + ONE_DAY)
            if w["opens"] <= now <= w["end"]]
    if not wins:
        return None
    return min(wins, key=lambda w: abs((w["start"] - now).total_seconds()))


def clock_state(agent_id, now=None):
    """Current state from the most recent clock event: out / in / break / lunch."""
    now = now or now_site()
    df = q("SELECT * FROM time_logs WHERE agent_id=? AND ts>=? ORDER BY ts, id",
           (agent_id, (now - dt.timedelta(hours=30)).isoformat()))
    if df.empty or df.iloc[-1].event == "logout":
        return dict(state="out", shift_date=None, since=None)
    last = df.iloc[-1]
    shift_date = last.shift_date
    sess = df[df.shift_date == shift_date]
    login_ts = _dt(sess[sess.event == "login"].ts.iloc[-1]) if (sess.event == "login").any() else _dt(last.ts)
    # an open session goes stale if the shift ended long ago (forgot to log out)
    wins = [w for w in agent_shift_windows(agent_id, dt.date.fromisoformat(shift_date),
                                           dt.date.fromisoformat(shift_date)) if w["shift_date"] == shift_date]
    end = wins[0]["end"] if wins else login_ts + dt.timedelta(hours=setting("max_daily_hrs"))
    if now > end + dt.timedelta(hours=4):
        return dict(state="out", shift_date=None, since=None, stale=shift_date)
    state = {"break_start": "break", "lunch_start": "lunch"}.get(last.event, "in")
    return dict(state=state, shift_date=shift_date, since=_dt(last.ts), login=login_ts)


def break_summary(events, now=None):
    """Totals per break type from a shift's events; ongoing break counted to `now` when given."""
    allowed = {"break": (setting("break_min"), int(setting("breaks_allowed"))),
               "lunch": (setting("lunch_min"), int(setting("lunches_allowed")))}
    used = {"break": [], "lunch": []}
    open_kind, open_ts = None, None
    for ev, ts in events:
        if ev in ("break_start", "lunch_start"):
            if open_kind:
                used[open_kind].append(mins(ts - open_ts))
            open_kind, open_ts = ev.split("_")[0], ts
        elif ev in ("break_end", "logout", "login") and open_kind:
            used[open_kind].append(mins(ts - open_ts))
            open_kind = None
    ongoing = None
    if open_kind and now is not None:
        ongoing = (open_kind, mins(now - open_ts))
        used[open_kind].append(ongoing[1])
    total = sum(sum(v) for v in used.values())
    over = 0
    for kind, durs in used.items():
        lim, count = allowed[kind]
        for i, d in enumerate(durs):
            over += d if i >= count else max(0, d - lim)  # extra breaks count in full
    return dict(total=total, over=int(over), used=used, ongoing=ongoing, allowed=allowed)


def rebuild_attendance(agent_id, shift_date):
    """Recalculate one attendance row from the clock logs of that shift."""
    logs = q("SELECT event, ts FROM time_logs WHERE agent_id=? AND shift_date=? ORDER BY ts, id",
             (agent_id, str(shift_date)))
    if logs.empty:
        return
    events = [(e, _dt(t)) for e, t in zip(logs.event, logs.ts)]
    logins = [t for e, t in events if e == "login"]
    if not logins:
        return
    tin = logins[0]
    tout = events[-1][1] if events[-1][0] == "logout" else None
    wins = [w for w in agent_shift_windows(agent_id, dt.date.fromisoformat(str(shift_date)),
                                           dt.date.fromisoformat(str(shift_date)))]
    prev = q("SELECT status, remarks FROM attendance WHERE agent_id=? AND date=?", (agent_id, str(shift_date)))
    remarks = prev.remarks.iloc[0] if len(prev) and isinstance(prev.remarks.iloc[0], str) else ""
    remarks = remarks.replace("Auto: no log in", "").replace("No log out", "").strip(" ;")
    if wins:
        stt, late, ut, ot = attendance_from_times(wins[0]["start"], wins[0]["end"], tin, tout)
    else:
        stt, late, ut, ot = "Unscheduled", 0, 0, 0
    b = break_summary(events)
    save_attendance(agent_id, shift_date, tin.strftime("%H:%M"), tout.strftime("%H:%M") if tout else None,
                    stt, late, ut, ot, remarks, break_min=b["total"], overbreak_min=b["over"])


def clock_action(agent_id, action, now=None):
    """Record a clock event for an agent. Returns (ok, message)."""
    now = now or now_site()
    stc = clock_state(agent_id, now)
    if stc.get("stale"):  # forgot to log out last shift: flag it, then carry on
        prev = q("SELECT remarks FROM attendance WHERE agent_id=? AND date=?", (agent_id, stc["stale"]))
        rem = prev.remarks.iloc[0] if len(prev) and isinstance(prev.remarks.iloc[0], str) else ""
        if "No log out" not in rem:
            ex("UPDATE attendance SET remarks=? WHERE agent_id=? AND date=?",
               ((rem + "; No log out").strip("; "), agent_id, stc["stale"]))
    state = stc["state"]

    def log(event, shift_date, ts=now):
        ex("INSERT INTO time_logs(agent_id,shift_date,event,ts,source) VALUES(?,?,?,?,?)",
           (agent_id, str(shift_date), event, ts.isoformat(), "clock"))

    if action == "login":
        if state != "out":
            return False, "You're already logged in."
        w = resolve_shift(agent_id, now)
        shift_date = w["shift_date"] if w else str(now.date())
        before = q("SELECT MIN(ts) t FROM time_logs WHERE agent_id=? AND shift_date=? AND event='login'",
                   (agent_id, shift_date)).t.iloc[0]
        log("login", shift_date)
        rebuild_attendance(agent_id, shift_date)
        if before:
            return True, f"Logged back in. Your first log in at {_dt(before):%H:%M} still counts for this shift."
        if not w:
            return True, "Logged in. You have no shift scheduled right now, so this is recorded as unscheduled."
        late = mins(now - w["start"])
        if late > setting("grace_min"):
            return True, f"Logged in for your {w['label']} shift — {late} min late."
        return True, f"Logged in for your {w['label']} shift. Have a good one!"
    if state == "out":
        return False, "You're not logged in."
    sd = stc["shift_date"]
    if action in ("break_start", "lunch_start"):
        if state != "in":
            return False, "End your current break first."
        log(action, sd)
        rebuild_attendance(agent_id, sd)
        return True, "Enjoy your " + ("lunch." if action == "lunch_start" else "break.")
    if action == "break_end":
        if state not in ("break", "lunch"):
            return False, "You're not on a break."
        log("break_end", sd)
        rebuild_attendance(agent_id, sd)
        return True, "Welcome back."
    if action == "logout":
        if state in ("break", "lunch"):
            log("break_end", sd)
        log("logout", sd)
        rebuild_attendance(agent_id, sd)
        return True, "Logged out. Thanks for today!"
    return False, "Unknown action."


def mark_no_shows(now=None):
    """Mark agents who have a login as Absent when their shift ended and they never logged in."""
    if not int(setting("auto_absent")):
        return 0
    now = now or now_site()
    users = q("SELECT agent_id, created_on FROM users WHERE role='agent' AND active=1 AND agent_id IS NOT NULL")
    if users.empty:
        return 0
    since = {int(float(a)): str(c or "")[:10] for a, c in zip(users.agent_id, users.created_on)}
    d1, d2 = now.date() - 2 * ONE_DAY, now.date()
    sch = build_schedule(d1, d2)
    sch = sch[sch.agent_id.isin(since) & sch.kind.isin(WORK_KINDS)]
    logged = {(int(a), d) for a, d in q("SELECT DISTINCT agent_id, shift_date FROM time_logs WHERE shift_date>=?",
                                        (str(d1),)).itertuples(index=False)}
    have = {(int(a), d) for a, d in q("SELECT agent_id, date FROM attendance WHERE date>=?",
                                      (str(d1),)).itertuples(index=False)}
    n = 0
    for r in sch.itertuples():
        if r.date < since[r.agent_id] or (r.agent_id, r.date) in logged or (r.agent_id, r.date) in have:
            continue
        _, e = shift_window(dt.date.fromisoformat(r.date), r.shift_start, r.shift_end)
        if now > e + dt.timedelta(hours=1):
            save_attendance(r.agent_id, r.date, None, None, "Absent", 0, 0, 0, "Auto: no log in")
            n += 1
    return n


# ----------------------------------------------------------------- agent pages
def _fmt_dur(m):
    m = int(max(0, m))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def page_my_shift():
    u = current_user()
    aid = u.get("agent_id")
    ag = agent_row(aid) if aid is not None else None
    if ag is None:
        st.error("Your login isn't linked to an agent on the roster. Please contact your admin.")
        return
    st.header(f"🕒 Hi, {str(ag['name']).split()[0]}!")
    _clock_panel(int(aid))

    st.subheader("My schedule — next 7 days")
    today = today_site()
    mine = build_schedule(today, today + 6 * ONE_DAY, agent_id=int(aid)).copy()
    if len(mine):
        mine["day"] = [f"{dt.date.fromisoformat(d):%a %b %d}" for d in mine.date]
        mine["schedule"] = [cell_label(r) if r.kind != "Leave" else f"Time off ({r.note})" for r in mine.itertuples()]
        st.dataframe(mine[["day", "schedule"]], hide_index=True, width="stretch")
    st.subheader("My recent attendance")
    att = q("SELECT date, time_in, time_out, status, late_min, undertime_min, break_min, overbreak_min, ot_min, "
            "remarks FROM attendance WHERE agent_id=? AND date>=? ORDER BY date DESC",
            (int(aid), str(today - dt.timedelta(days=14))))
    if att.empty:
        st.caption("Nothing yet.")
    else:
        st.dataframe(att, hide_index=True, width="stretch", column_config={
            "late_min": "Late (min)", "undertime_min": "UT (min)", "break_min": "Breaks (min)",
            "overbreak_min": "Over-break (min)", "ot_min": "OT (min)"})


@st.fragment(run_every="30s")
def _clock_panel(aid):
    now = now_site()
    stc = clock_state(aid, now)
    state = stc["state"]
    w = resolve_shift(aid, now) if state == "out" else None
    with st.container(border=True):
        c = st.columns([2, 1])
        with c[0]:
            st.caption(f"{now:%A, %B %d · %H:%M} ({setting_str('site_tz')})")
            if state == "out":
                if w:
                    late = mins(now - w["start"])
                    st.markdown(f"### Your shift: **{w['label']}**")
                    if late > 0:
                        st.warning(f"Your shift started {_fmt_dur(late)} ago — please log in.")
                    else:
                        st.info(f"Starts in {_fmt_dur(-late)}.")
                else:
                    nxt = [x for x in agent_shift_windows(aid, now.date(), now.date() + 7 * ONE_DAY)
                           if x["start"] > now]
                    st.markdown("### You're logged out")
                    if nxt:
                        st.caption(f"Next shift: {nxt[0]['start']:%a %b %d} · {nxt[0]['label']}")
            else:
                logs = q("SELECT event, ts FROM time_logs WHERE agent_id=? AND shift_date=? ORDER BY ts, id",
                         (aid, stc["shift_date"]))
                b = break_summary([(e, _dt(t)) for e, t in zip(logs.event, logs.ts)], now)
                label = {"in": "🟢 Working", "break": "☕ On break", "lunch": "🍱 On lunch"}[state]
                st.markdown(f"### {label}")
                st.caption(f"Logged in at {stc['login']:%H:%M} · shift date {stc['shift_date']} · "
                           f"worked {_fmt_dur(mins(now - stc['login']))}")
                if b["ongoing"]:
                    kind, m = b["ongoing"]
                    lim = b["allowed"][kind][0]
                    msg = f"{kind.title()} running for **{m} min** (allowed {lim:g})"
                    if m > lim:
                        st.error(msg + " — please end it now.")
                    else:
                        st.info(msg)
                used_b, used_l = b["used"]["break"], b["used"]["lunch"]
                lunch_txt = (f"Lunch used: {len(used_l)}/{b['allowed']['lunch'][1]} · "
                             if b["allowed"]["lunch"][1] else "")
                st.caption(f"Breaks used: {len(used_b)}/{b['allowed']['break'][1]} "
                           f"({b['allowed']['break'][0]:g} min each) · {lunch_txt}over-break so far: {b['over']} min")
        with c[1]:
            acts = {"out": [("login", "▶️ Log in", "primary")],
                    "in": [("break_start", "☕ Start break", "secondary")]
                    + ([("lunch_start", "🍱 Start lunch", "secondary")] if int(setting("lunches_allowed")) else [])
                    + [("logout", "⏹️ Log out", "primary")],
                    "break": [("break_end", "✅ End break", "primary"), ("logout", "⏹️ Log out", "secondary")],
                    "lunch": [("break_end", "✅ End lunch", "primary"), ("logout", "⏹️ Log out", "secondary")]}[state]
            for act, lbl, kind in acts:
                if st.button(lbl, key=f"clk_{act}", type=kind, width="stretch"):
                    ok, msg = clock_action(aid, act)
                    st.session_state["clock_msg"] = (ok, msg)
                    st.rerun()
            if st.session_state.get("clock_msg"):
                ok, msg = st.session_state.pop("clock_msg")
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)


def page_my_requests():
    u = current_user()
    aid = u.get("agent_id")
    ag = agent_row(aid) if aid is not None else None
    if ag is None:
        st.error("Your login isn't linked to an agent on the roster. Please contact your admin.")
        return
    aid = int(aid)
    st.header("📝 My Requests")
    t1, t2, t3 = st.tabs(["File time off", "File OT", "My requests"])
    with t1:
        with st.form("my_leave"):
            c = st.columns(3)
            ltype = c[0].selectbox("Type", LEAVE_TYPES)
            d1 = c[1].date_input("From", today_site() + ONE_DAY)
            d2 = c[2].date_input("To", today_site() + ONE_DAY)
            reason = st.text_input("Reason")
            if st.form_submit_button("Submit time off", type="primary"):
                days, err = check_time_off(aid, d1, d2)
                if not err and d1 < today_site() and ltype.startswith("Planned"):
                    err = "Planned time off must be in the future."
                if err:
                    st.error(err)
                else:
                    ex("INSERT INTO leaves(agent_id,leave_type,start_date,end_date,days,status,reason,filed_on) "
                       "VALUES(?,?,?,?,?,?,?,?)", (aid, ltype, str(d1), str(d2), days, "Pending", reason,
                                                   str(today_site())))
                    st.success(f"Filed {days:g} working day(s) — waiting for approval.")
    with t2:
        st.caption("OT must be outside your scheduled shift and is paid only when approved. Times are "
                   f"{setting_str('site_tz').split('/')[-1].replace('_', ' ')} time, 24-hour. For OT right after a "
                   "graveyard shift, use the date your shift started.")
        with st.form("my_ot"):
            c = st.columns(4)
            d = c[0].date_input("Shift date", today_site())
            typ = c[1].selectbox("Type", ["Pre-shift OT", "Post-shift OT", "Rest Day OT"])
            s_ = c[2].text_input("From (HH:MM)", "07:00")
            e_ = c[3].text_input("To (HH:MM)", "09:00")
            reason = st.text_input("Reason")
            if st.form_submit_button("Submit OT", type="primary"):
                hrs, err = check_ot(aid, d, s_, e_, typ)
                if err:
                    st.error(err)
                else:
                    ex("INSERT INTO ot_requests(agent_id,date,ot_type,start_time,end_time,hours,reason,status,"
                       "filed_on,for_account) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (aid, str(d), typ, s_, e_, hrs, reason, "Pending", str(today_site()), ag.account))
                    st.success(f"Filed {hrs:g}h OT — waiting for approval.")
    with t3:
        lv = q("SELECT id, leave_type AS type, start_date, end_date, days, status, reason FROM leaves "
               "WHERE agent_id=? ORDER BY start_date DESC LIMIT 50", (aid,))
        ot = q("SELECT id, date, ot_type AS type, start_time, end_time, hours, status, reason FROM ot_requests "
               "WHERE agent_id=? ORDER BY date DESC LIMIT 50", (aid,))
        st.markdown("**Time off**")
        if len(lv):
            st.dataframe(lv.drop(columns="id"), hide_index=True, width="stretch")
        else:
            st.caption("None yet.")
        st.markdown("**Overtime**")
        if len(ot):
            st.dataframe(ot.drop(columns="id"), hide_index=True, width="stretch")
        else:
            st.caption("None yet.")
        pend = {f"lv{r.id}": f"Time off · {r.type} · {r.start_date}→{r.end_date}"
                for r in lv[lv.status == "Pending"].itertuples()}
        pend.update({f"ot{r.id}": f"OT · {r.date} {r.start_time}-{r.end_time}"
                     for r in ot[ot.status == "Pending"].itertuples()})
        if pend:
            pick = st.selectbox("Cancel a pending request", list(pend), format_func=pend.get, key="my_cancel")
            if st.button("Cancel request"):
                kind, rid = pick[:2], int(pick[2:])
                if kind == "lv":
                    ex("UPDATE leaves SET status='Cancelled' WHERE id=? AND agent_id=? AND status='Pending'", (rid, aid))
                else:
                    ex("UPDATE ot_requests SET status='Cancelled' WHERE id=? AND agent_id=? AND status='Pending'",
                       (rid, aid))
                st.rerun()


# ----------------------------------------------------------------- admin: live floor, users, clock logs
def floor_status(now=None):
    """Everyone who should be (or is) on the floor right now."""
    now = now or now_site()
    sch = build_schedule(now.date() - ONE_DAY, now.date() + ONE_DAY)
    users = q("SELECT agent_id FROM users WHERE role='agent' AND active=1 AND agent_id IS NOT NULL")
    has_login = {int(float(a)) for a in users.agent_id}
    logs = q("SELECT agent_id, shift_date, event, ts FROM time_logs WHERE ts>=? ORDER BY ts, id",
             ((now - dt.timedelta(hours=30)).isoformat(),))
    rows = []
    seen = set()
    for r in sch.itertuples():
        if r.kind not in WORK_KINDS and r.kind != "Leave":
            continue
        d = dt.date.fromisoformat(r.date)
        if r.kind == "Leave":
            ag = agents_df(True)
            base = ag[ag.id == r.agent_id]
            if base.empty or d != now.date():
                continue
            s, e = shift_window(d, base.shift_start.iloc[0], base.shift_end.iloc[0])
        else:
            s, e = shift_window(d, r.shift_start, r.shift_end)
        mine = logs[(logs.agent_id == r.agent_id) & (logs.shift_date == r.date)]
        if not (s - dt.timedelta(hours=1) <= now <= e + dt.timedelta(hours=1)) and mine.empty:
            continue
        if not mine.empty and mine.iloc[-1].event == "logout" and now > e + dt.timedelta(hours=1):
            continue
        seen.add((r.agent_id, r.date))
        events = [(ev, _dt(t)) for ev, t in zip(mine.event, mine.ts)]
        b = break_summary(events, now) if events else None
        login = next((t for ev, t in events if ev == "login"), None)
        if r.kind == "Leave":
            status, flag = "Time off", ""
        elif not events:
            late = mins(now - s)
            status = f"Not logged in — {late} min late" if late > setting("grace_min") else "Not logged in yet"
            flag = "late" if late > setting("grace_min") else ""
        else:
            last = events[-1][0]
            if last == "logout":
                status, flag = f"Logged out {events[-1][1]:%H:%M}", ""
            elif last in ("break_start", "lunch_start"):
                kind, m = b["ongoing"]
                lim = b["allowed"][kind][0]
                status = f"On {kind} {m} min" + (f" — OVER by {m - lim:g}" if m > lim else "")
                flag = "over" if m > lim else "break"
            elif now > e + dt.timedelta(hours=1):
                status, flag = "Still logged in after shift — missing log out?", "late"
            else:
                status, flag = "Working", "ok"
        rows.append(dict(agent=r.name, account=r.account, shift=f"{s:%H:%M}–{e:%H:%M}",
                         shift_date=r.date, status=status, logged_in=f"{login:%H:%M}" if login else "",
                         late_min=max(0, mins(login - s)) if login and login > s + dt.timedelta(
                             minutes=setting("grace_min")) else 0,
                         break_min=b["total"] if b else 0, overbreak_min=b["over"] if b else 0,
                         has_login="Yes" if r.agent_id in has_login else "No", _flag=flag))
    # unscheduled sessions still open
    for (aid, sd), g in logs.groupby(["agent_id", "shift_date"]):
        if (aid, sd) in seen or g.iloc[-1].event == "logout":
            continue
        ag = agent_row(int(aid))
        rows.append(dict(agent=ag["name"] if ag is not None else aid, account=ag["account"] if ag is not None else "",
                         shift="unscheduled", shift_date=sd, status="Working (unscheduled)",
                         logged_in=_dt(g[g.event == "login"].ts.iloc[0]).strftime("%H:%M") if (g.event == "login").any()
                         else "", late_min=0, break_min=0, overbreak_min=0, has_login="Yes", _flag="ok"))
    return pd.DataFrame(rows, columns=["agent", "account", "shift", "shift_date", "status", "logged_in", "late_min",
                                       "break_min", "overbreak_min", "has_login", "_flag"])


def page_live_floor(acct):
    st.header("🟢 Live Floor")
    n = mark_no_shows()
    if n:
        st.toast(f"Marked {n} no-show(s) as Absent")
        flush_to_sheet()
    _live_board(acct)


@st.fragment(run_every="30s")
def _live_board(acct):
    now = now_site()
    df = floor_status(now)
    if acct != "All accounts":
        df = df[df.account == acct]
    st.caption(f"As of {now:%H:%M:%S} ({setting_str('site_tz')}) · refreshes every 30 seconds")
    m = st.columns(6)
    m[0].metric("On shift now", int((~df.status.str.startswith("Time off")).sum()))
    m[1].metric("Working", int((df._flag == "ok").sum()))
    m[2].metric("On break/lunch", int(df._flag.isin(["break", "over"]).sum()))
    m[3].metric("Over-break now", int((df._flag == "over").sum()))
    m[4].metric("Late / not logged in", int((df._flag == "late").sum()))
    m[5].metric("Time off", int(df.status.eq("Time off").sum()))
    if df.empty:
        st.info("Nobody is scheduled around this time.")
        return
    colors = {"late": "background-color:#fadbd8;color:#000", "over": "background-color:#f5b7b1;color:#000",
              "break": "background-color:#fef9e7;color:#000", "ok": "background-color:#e9f7ef;color:#000"}
    order = {"over": 0, "late": 1, "break": 2, "ok": 3, "": 4}
    df = df.assign(_o=df._flag.map(order)).sort_values(["_o", "account", "agent"]).drop(columns="_o")
    flags = df._flag.tolist()
    view = df.drop(columns="_flag").reset_index(drop=True)
    sty = view.style.apply(lambda row: [colors.get(flags[row.name], "")] * len(row), axis=1)
    st.dataframe(sty, hide_index=True, width="stretch", height=min(700, 40 + 35 * len(view)), column_config={
        "agent": "Agent", "account": "Account", "shift": "Shift", "shift_date": "Shift date", "status": "Status",
        "logged_in": "Logged in", "late_min": "Late (min)", "break_min": "Breaks (min)",
        "overbreak_min": "Over-break (min)", "has_login": "Has login"})
    if (view.has_login == "No").any():
        st.caption("Agents without a login can't clock in — create their logins under **Users**.")


def page_users(acct):
    st.header("🔐 Users & Logins")
    me = current_user()["username"]
    users = q("SELECT u.username, u.role, a.name AS agent, a.emp_id, a.account, u.active, u.must_change, "
              "u.last_login, u.locked_until, u.agent_id FROM users u LEFT JOIN agents a ON a.id=u.agent_id "
              "ORDER BY u.role, a.name, u.username")
    m = st.columns(3)
    m[0].metric("Admins", int((users.role == "admin").sum()))
    m[1].metric("Agent logins", int((users.role == "agent").sum()))
    ag = agents_df(True)
    no_login = ag[~ag.id.isin(pd.to_numeric(users.agent_id, errors="coerce").dropna().astype(int))]
    m[2].metric("Active agents without a login", len(no_login))
    show = users.drop(columns=["agent_id"]).copy()
    show["active"] = show.active.fillna(0).astype(int).map({1: "Yes", 0: "No"})
    show["must_change"] = show.must_change.fillna(0).astype(int).map({1: "Temp password", 0: ""})
    st.dataframe(show, hide_index=True, width="stretch", column_config={"must_change": "Password"})

    if st.session_state.get("new_creds") is not None:
        creds = st.session_state["new_creds"]
        st.success(f"{len(creds)} login(s) ready. Share each temporary password privately — it's shown only now. "
                   "Agents must set their own password the first time they sign in.")
        st.dataframe(creds, hide_index=True, width="stretch")
        st.download_button("⬇️ Download logins (CSV)", creds.to_csv(index=False), "new_logins.csv")
        if st.button("Done — hide passwords"):
            st.session_state.pop("new_creds")
            st.rerun()

    t1, t2, t3 = st.tabs(["Create agent logins", "Add a user", "Manage a user"])
    with t1:
        st.caption("Creates a login for every active agent who doesn't have one. Username = employee ID; "
                   "each gets a random temporary password.")
        if no_login.empty:
            st.success("Every active agent has a login.")
        else:
            st.dataframe(no_login[["emp_id", "name", "account"]], hide_index=True, width="stretch")
            if st.button(f"Create {len(no_login)} agent login(s)", type="primary", key="mk_logins"):
                rows = []
                for r in no_login.itertuples():
                    un = str(r.emp_id).strip()
                    if get_user(un) is not None:
                        un = f"{un}-{r.id}"
                    pw = temp_password()
                    ex("INSERT INTO users(username,pw_hash,role,agent_id,active,must_change,created_on) "
                       "VALUES(?,?,?,?,?,?,?)", (un, hash_pw(pw), "agent", int(r.id), 1, 1, now_site().isoformat()))
                    rows.append(dict(name=r.name, account=r.account, username=un, temporary_password=pw))
                st.session_state["new_creds"] = pd.DataFrame(rows)
                rerun()
    with t2:
        with st.form("add_user"):
            c = st.columns(3)
            role = c[0].selectbox("Role", ["agent", "admin"])
            un = c[1].text_input("Username", help="For agents, the employee ID works well")
            pw = c[2].text_input("Password (blank = generate a temporary one)", type="password")
            labels = agent_label_map(ag)
            link = st.selectbox("Linked agent (agents only)", [None] + list(labels),
                                format_func=lambda k: "—" if k is None else labels[k])
            if st.form_submit_button("Add user", type="primary"):
                temp = not pw
                pw = pw or temp_password()
                prob = None if temp else pw_problem(pw)
                if not un.strip():
                    st.error("Enter a username.")
                elif get_user(un) is not None:
                    st.error("That username is taken.")
                elif role == "agent" and link is None:
                    st.error("Link the agent login to someone on the roster.")
                elif role == "agent" and len(q("SELECT 1 FROM users WHERE agent_id=?", (link,))):
                    st.error("That agent already has a login.")
                elif prob:
                    st.error(prob)
                else:
                    ex("INSERT INTO users(username,pw_hash,role,agent_id,active,must_change,created_on) "
                       "VALUES(?,?,?,?,?,?,?)", (un.strip(), hash_pw(pw), role, link if role == "agent" else None,
                                                 1, 1, now_site().isoformat()))
                    st.session_state["new_creds"] = pd.DataFrame([dict(
                        name=labels.get(link, "") if role == "agent" else "(admin)", account="", username=un.strip(),
                        temporary_password=pw if temp else "(the password you typed)")])
                    rerun()
    with t3:
        if users.empty:
            st.caption("No users.")
        else:
            pick = st.selectbox("User", users.username.tolist(), key="mg_user",
                                format_func=lambda x: f"{x} · " + str(users.set_index('username').loc[x, 'role']) +
                                (f" · {users.set_index('username').loc[x, 'agent']}"
                                 if isinstance(users.set_index('username').loc[x, 'agent'], str) else ""))
            row = users.set_index("username").loc[pick]
            active = int(row.active or 0)
            admins_active = int(((users.role == "admin") & (users.active.fillna(0).astype(int) == 1)).sum())
            last_admin = row.role == "admin" and active and admins_active <= 1
            c = st.columns(4)
            if c[0].button("🔁 Reset password", key="mg_reset"):
                pw = temp_password()
                ex("UPDATE users SET pw_hash=?, must_change=1, failed=0, locked_until=NULL WHERE username=?",
                   (hash_pw(pw), pick))
                end_user_sessions(pick)
                st.session_state["new_creds"] = pd.DataFrame([dict(name=row.agent if isinstance(row.agent, str) else "",
                                                                   account="", username=pick, temporary_password=pw)])
                rerun()
            if c[1].button("🔓 Unlock", key="mg_unlock"):
                ex("UPDATE users SET failed=0, locked_until=NULL WHERE username=?", (pick,))
                rerun()
            if c[2].button("Disable" if active else "Enable", key="mg_toggle",
                           disabled=bool(last_admin or pick == me)):
                ex("UPDATE users SET active=? WHERE username=?", (0 if active else 1, pick))
                end_user_sessions(pick)
                rerun()
            if c[3].button("🗑️ Delete", key="mg_delete", disabled=bool(last_admin or pick == me)):
                ex("DELETE FROM users WHERE username=?", (pick,))
                end_user_sessions(pick)
                rerun()
            if pick == me or last_admin:
                st.caption("You can't disable or delete your own login or the last active admin.")


def page_clock_logs(acct):
    st.subheader("Clock logs")
    st.caption("Every log in / break / log out, as recorded. Fix mistakes here (e.g. a forgotten log out) — "
               "attendance recalculates automatically.")
    day = st.date_input("Shift date", today_site(), key="cl_day")
    ag = agents_df(True, acct)
    logs = q("SELECT l.id, a.name, l.agent_id, l.shift_date, l.event, l.ts, l.source, l.note FROM time_logs l "
             "JOIN agents a ON a.id=l.agent_id WHERE l.shift_date=? ORDER BY a.name, l.ts, l.id", (str(day),))
    logs = logs[logs.agent_id.isin(ag.id)]
    if logs.empty:
        st.caption("No clock events for this shift date.")
    else:
        view = logs.assign(event=logs.event.map(CLOCK_EVENTS).fillna(logs.event),
                           time=[str(t)[:16].replace("T", " ") for t in logs.ts])
        st.dataframe(view[["id", "name", "event", "time", "source", "note"]], hide_index=True, width="stretch")
        c = st.columns([3, 1])
        dels = c[0].multiselect("Delete events (by id)", view.id.tolist(), key="cl_del",
                                format_func=lambda i: f"#{i} · " + " · ".join(
                                    view[view.id == i][["name", "event", "time"]].iloc[0].astype(str)))
        if c[1].button("Delete selected", disabled=not dels, key="cl_del_btn"):
            hit = logs[logs.id.isin(dels)][["agent_id", "shift_date"]].drop_duplicates()
            for i in dels:
                ex("DELETE FROM time_logs WHERE id=?", (int(i),))
            for r in hit.itertuples():
                if len(q("SELECT 1 FROM time_logs WHERE agent_id=? AND shift_date=?", (r.agent_id, r.shift_date))):
                    rebuild_attendance(int(r.agent_id), r.shift_date)
                else:
                    ex("DELETE FROM attendance WHERE agent_id=? AND date=?", (int(r.agent_id), r.shift_date))
            st.session_state.pop("cl_del", None)
            rerun()
    with st.form("cl_add"):
        st.markdown("**Add a missing event**")
        labels = agent_label_map(ag)
        c = st.columns(4)
        aid = c[0].selectbox("Agent", list(labels), format_func=labels.get) if labels else None
        ev = c[1].selectbox("Event", list(CLOCK_EVENTS), format_func=CLOCK_EVENTS.get, index=4)
        d = c[2].date_input("Date of the event", day, help="Calendar date it happened (after midnight = next day)")
        t = c[3].text_input("Time (HH:MM)", "07:00")
        note = st.text_input("Reason for the correction", "Forgot to log out")
        if st.form_submit_button("Add event") and aid is not None:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                st.error("Time must be HH:MM.")
            else:
                ts = dt.datetime.combine(d, parse_t(t))
                ex("INSERT INTO time_logs(agent_id,shift_date,event,ts,source,note) VALUES(?,?,?,?,?,?)",
                   (aid, str(day), ev, ts.isoformat(), f"admin:{current_user()['username']}", note))
                rebuild_attendance(aid, day)
                rerun()


# ----------------------------------------------------------------- layout
auth_gate()  # nothing below runs until someone is signed in
USER = current_user()

ADMIN_PAGES = {
    "🟢 Live Floor": page_live_floor,
    "📊 Dashboard": page_dashboard,
    "⏱️ Attendance": page_attendance,
    "📶 Interval Planner": page_interval,
    "🗓️ Schedules": page_schedule,
    "🌴 Time Off": page_leaves,
    "🧩 Coverage Finder": page_coverage,
    "⏰ Overtime": page_ot,
    "📈 Reports": page_reports,
    "👥 Roster & Accounts": lambda acct: page_roster(),
    "🔐 Users": page_users,
    "⚙️ Settings": lambda acct: page_settings(),
    "🔑 My password": lambda acct: page_change_password(),
}
AGENT_PAGES = {
    "🕒 My Shift": lambda acct: page_my_shift(),
    "📝 My Requests": lambda acct: page_my_requests(),
    "🔑 My password": lambda acct: page_change_password(),
}
PAGES = ADMIN_PAGES if is_admin() else AGENT_PAGES  # agents never get the admin pages
if is_admin():
    sync_current_schedules()  # roster shows the permanent schedule in effect today (cheap; writes only on change)

with st.sidebar:
    st.title("🗓️ WFM Scheduler")
    who = USER["username"]
    if USER.get("agent_id") is not None:
        ar = agent_row(USER["agent_id"])
        who = ar["name"] if ar is not None else who
    st.caption(f"Signed in as **{who}** ({USER['role']})")
    page = st.radio("Go to", list(PAGES), label_visibility="collapsed")
    st.divider()
    acct_filter = st.selectbox("Account filter", ["All accounts"] + account_names()) if is_admin() else "All accounts"
    st.caption(f"Today: {now_site():%a, %b %d %Y · %H:%M}")
    if st.button("Sign out", width="stretch"):
        logout_session()
        st.rerun()
    if is_admin():
        st.divider()
        if STORE is not None:
            synced = dt.datetime.fromtimestamp(STORE.loaded_at).strftime("%H:%M")
            waiting = len(STORE.dirty) + sum(1 for v in STORE.appends.values() if v)
            st.caption(f"🟢 Google Sheet: **{STORE.title}**  \nLast loaded {synced}"
                       + (f"  \n⏳ {waiting} change(s) saving…" if waiting else "")
                       + (f"  \n⚠️ Last save error: {STORE.last_error}" if STORE.last_error else ""))
            if st.button("🔄 Reload from Google Sheet", width="stretch"):
                if flush_to_sheet(sync=True):
                    STORE.load()
                st.rerun()
        elif ON_CLOUD:
            st.error("⚠️ Not connected to Google Sheets — all data (including logins) is erased whenever the app "
                     "restarts. Add your Google key and `sheet_url` in the app's Secrets.")
        else:
            st.caption("💾 Saving to local file `wfm.db`")

PAGES[page](acct_filter)

# Push anything saved during this run (forms that don't rerun) to the sheet.
flush_to_sheet()
