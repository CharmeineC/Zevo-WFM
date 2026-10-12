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
import math
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import closing

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st


def _sheet_secrets():
    try:
        if "gcp_service_account" in st.secrets and ("sheet_url" in st.secrets or "sheet_key" in st.secrets):
            return st.secrets
    except Exception:  # no secrets file at all
        pass
    return None


SECRETS = _sheet_secrets()
USE_SHEETS = SECRETS is not None
if USE_SHEETS:
    _sid = str(SECRETS.get("sheet_url") or SECRETS.get("sheet_key"))
    DB_PATH = os.path.join(tempfile.gettempdir(),
                           f"wfm_cache_{hashlib.md5(_sid.encode()).hexdigest()[:10]}.db")
else:
    DB_PATH = os.environ.get("WFM_DB", "wfm.db")
TABLES = ["accounts", "agents", "schedule_overrides", "attendance", "leaves", "ot_requests", "settings",
          "interval_volume"]
_WRITE_RE = re.compile(r"^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.I)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ONE_DAY = dt.timedelta(days=1)
TIME_RE = r"^$|^([01]\d|2[0-3]):[0-5]\d$"
LOBS = ["Voice", "Non-Voice"]
LEAVE_TYPES = [
    "Vacation Leave (VL)", "Sick Leave (SL)", "Emergency Leave (EL)",
    "Bereavement Leave (BL)", "Maternity/Paternity (ML)", "Solo Parent Leave (SPL)",
    "Leave Without Pay (LWOP)",
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
    "site_tz": "Asia/Manila",  # timezone schedules are written in
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
  remarks TEXT, PRIMARY KEY(agent_id, date));
CREATE TABLE IF NOT EXISTS leaves(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, leave_type TEXT,
  start_date TEXT, end_date TEXT, days REAL, status TEXT DEFAULT 'Pending',
  reason TEXT, filed_on TEXT, decided_by TEXT);
CREATE TABLE IF NOT EXISTS ot_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, date TEXT, ot_type TEXT,
  start_time TEXT, end_time TEXT, hours REAL, reason TEXT, status TEXT DEFAULT 'Pending',
  covering_for INTEGER, filed_on TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA foreign_keys=ON")
    return c


def q(sql, params=()):
    with closing(_conn()) as c:
        return pd.read_sql_query(sql, c, params=[_py(p) for p in params])


def ex(sql, params=()):
    with closing(_conn()) as c, c:
        cur = c.execute(sql, [_py(p) for p in params])
        changed, rowid = cur.rowcount, cur.lastrowid
    m = _WRITE_RE.match(sql)
    if STORE is not None and m and changed:
        STORE.mark_dirty(m.group(1))
    return rowid


def exmany(sql, rows):
    """Bulk version of ex() for imports."""
    rows = [[_py(v) for v in r] for r in rows]
    if not rows:
        return
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
    """Google Sheet <-> local SQLite cache. One worksheet (tab) per table."""
    REFRESH_SEC = 120  # pick up edits made directly in the sheet

    def __init__(self, secrets):
        import gspread
        gc = gspread.service_account_from_dict(dict(secrets["gcp_service_account"]))
        self.sh = gc.open_by_url(secrets["sheet_url"]) if "sheet_url" in secrets else gc.open_by_key(secrets["sheet_key"])
        self.title = self.sh.title
        self.lock = threading.RLock()
        self.dirty, self.ws, self.nrows = set(), {}, {}
        self.loaded_at, self.last_error = 0.0, None
        self.load()

    @staticmethod
    def cols(table):
        with closing(_conn()) as c:
            return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]

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

    def load(self):
        """Replace the local cache with what's in the Google Sheet."""
        with self.lock:
            self._ensure_tabs()
            res = self.sh.values_batch_get(TABLES)
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
            self.loaded_at = time.time()

    def mark_dirty(self, table):
        if table in TABLES:
            with self.lock:
                self.dirty.add(table)

    def flush(self):
        """Write every changed table back to its tab."""
        with self.lock:
            for t in sorted(self.dirty):
                df = q(f"SELECT * FROM {t}")
                values = [list(df.columns)] + [[_cell(v) for v in r] for r in df.itertuples(index=False)]
                m = len(df.columns)
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
                self.dirty.discard(t)
            self.last_error = None

    def maybe_refresh(self):
        with self.lock:
            if not self.dirty and time.time() - self.loaded_at > self.REFRESH_SEC:
                self.load()


def init_db():
    with closing(_conn()) as c, c:
        c.executescript(SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        for col, typ in ACCOUNT_EXTRA_COLS.items():  # upgrade databases made by older versions
            if col not in have:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {col} {typ}")


def ensure_default_settings():
    for k, v in DEFAULT_SETTINGS.items():
        ex("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))


@st.cache_resource(show_spinner="Loading data from Google Sheets…")
def get_store():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    return SheetStore(SECRETS)


def flush_to_sheet():
    if STORE is None or not STORE.dirty:
        return True
    try:
        STORE.flush()
        return True
    except Exception as e:  # keep tables dirty; retried on next save
        STORE.last_error = str(e)
        st.error(f"Couldn't save to Google Sheets yet (will retry on the next action): {e}")
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


def build_schedule(start, end, account=None):
    """Effective schedule per agent per day: overrides > default shift/rest days,
    then approved leave replaces any working day."""
    cols = ["agent_id", "emp_id", "name", "account", "lob", "skills", "date",
            "kind", "shift_start", "shift_end", "note"]
    ag = agents_df(True, account)
    if ag.empty:
        return pd.DataFrame(columns=cols)
    s, e = str(start), str(end)
    ov = q("SELECT * FROM schedule_overrides WHERE date BETWEEN ? AND ?", (s, e))
    ovmap = {(int(r.agent_id), r.date): r for r in ov.itertuples()}
    lv = q("SELECT agent_id, leave_type, start_date, end_date FROM leaves "
           "WHERE status='Approved' AND start_date<=? AND end_date>=?", (e, s))
    rows = []
    for a in ag.itertuples():
        rds = rest_set(a.rest_days)
        mine = lv[lv.agent_id == a.id]
        for d in pd.date_range(start, end).date:
            ds = d.isoformat()
            if (a.id, ds) in ovmap:
                o = ovmap[(a.id, ds)]
                kind, ss, se, note = o.kind, o.shift_start, o.shift_end, o.note or ""
            elif DAYS[d.weekday()] in rds:
                kind, ss, se, note = "Rest Day", None, None, ""
            else:
                kind, ss, se, note = "Work", a.shift_start, a.shift_end, ""
            hit = mine[(mine.start_date <= ds) & (mine.end_date >= ds)]
            if len(hit) and kind in WORK_KINDS:
                kind, note = "Leave", hit.iloc[0].leave_type
            rows.append(dict(agent_id=int(a.id), emp_id=a.emp_id, name=a.name, account=a.account,
                             lob=a.lob, skills=a.skills or "", date=ds, kind=kind,
                             shift_start=ss, shift_end=se, note=note))
    return pd.DataFrame(rows, columns=cols)


def compute_attendance(day, shift_start, shift_end, time_in, time_out, status):
    """Returns (status, late_min, undertime_min, ot_min)."""
    if status in ("Absent", "NCNS", "On Leave", "Rest Day") or not shift_start:
        return status, 0, 0, 0
    if not time_in:
        return status, 0, 0, 0
    grace, block = setting("grace_min"), setting("ot_block_min")
    s, e = shift_window(day, shift_start, shift_end)
    tin = closest_dt(day, time_in, s)
    late = mins(tin - s) if tin > s + dt.timedelta(minutes=grace) else 0
    ut = ot = 0
    if time_out:
        tout = closest_dt(day, time_out, e)
        if tout < e:
            ut = mins(e - tout)
        elif tout > e:
            ot = int(mins(tout - e) // block * block)
    if status in ("", None, "Present", "Late", "Undertime"):
        status = "Late" if late else ("Undertime" if ut else "Present")
    return status, late, ut, ot


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
    day = st.date_input("Date", dt.date.today(), key="dash_day")
    sch = build_schedule(day, day, acct)
    if sch.empty:
        st.info("No agents yet. Add your roster under **Roster & Accounts**, or load sample data in **Settings**.")
        return
    att = q("SELECT agent_id, status, late_min FROM attendance WHERE date=?", (str(day),))
    m = sch.merge(att, on="agent_id", how="left")
    cov = q("SELECT a.account FROM ot_requests o JOIN agents a ON a.id=o.covering_for "
            "WHERE o.date=? AND o.status='Approved'", (str(day),))
    req = q("SELECT name AS account, required_hc FROM accounts")
    rows = []
    for acc, g in m.groupby("account"):
        sched = g[g.kind == "Work"]
        present = int(sched.status.isin(PRESENT_STATUS).sum())
        absent = int(sched.status.isin(ABSENT_STATUS).sum())
        covers = int((cov.account == acc).sum())
        rhc = req[req.account == acc].required_hc
        rhc = int(rhc.iloc[0]) if len(rhc) else 0
        projected = len(sched) - absent + covers
        rows.append({"Account": acc, "LOB": g.lob.iloc[0], "Required": rhc, "Scheduled": len(sched),
                     "On leave": int((g.kind == "Leave").sum()), "Rest day": int((g.kind == "Rest Day").sum()),
                     "Present": present, "Late": int((sched.late_min.fillna(0) > 0).sum()),
                     "Absent/NCNS": absent, "Cover OT in": covers,
                     "Projected HC": projected, "Variance": projected - rhc})
    summ = pd.DataFrame(rows)
    c = st.columns(6)
    c[0].metric("Scheduled", int(summ.Scheduled.sum()))
    c[1].metric("Present", int(summ.Present.sum()))
    c[2].metric("Late", int(summ.Late.sum()))
    c[3].metric("Absent/NCNS", int(summ["Absent/NCNS"].sum()))
    c[4].metric("On leave", int(summ["On leave"].sum()))
    c[5].metric("HC variance", int(summ.Variance.sum()))
    st.subheader("Staffing by account")
    st.dataframe(summ.style.map(lambda v: "color:#c0392b;font-weight:600" if isinstance(v, (int, float)) and v < 0 else "",
                                subset=["Variance"]), hide_index=True, width="stretch")

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
    pick = st.date_input("Week of", dt.date.today(), key="sched_week")
    w0 = week_start(pick)
    sch = build_schedule(w0, w0 + 6 * ONE_DAY, acct)
    if sch.empty:
        st.info("No agents in this view.")
        return
    sch["cell"] = [cell_label(r) for r in sch.itertuples()]
    sch["day"] = [f"{DAYS[dt.date.fromisoformat(d).weekday()]} {d[5:]}" for d in sch.date]
    order = list(dict.fromkeys(sch.day))
    grid = sch.pivot_table(index=["account", "name"], columns="day", values="cell", aggfunc="first")[order]
    st.dataframe(style_kinds(grid), width="stretch", height=min(600, 38 + 35 * len(grid)))
    st.caption("Grey = rest day · Red = leave · Blue = RD OT · Yellow = training · Purple = holiday")

    working = sch[sch.kind.isin(("Work", "Work (RD OT)"))].groupby("day").size().reindex(order, fill_value=0)
    st.bar_chart(working.rename("Agents on shift"))

    ag = agents_df(True, acct)
    labels = agent_label_map(ag)
    t1, t2, t3 = st.tabs(["Change a schedule", "Shift swap", "Current overrides"])
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
    st.header("⏱️ Daily Attendance")
    day = st.date_input("Shift date (the date the shift starts)", dt.date.today(), key="att_day")
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
              "status", "late_min", "undertime_min", "ot_min", "remarks"]]
    st.caption(f"Grace period: {setting('grace_min'):g} min · OT counted in {setting('ot_block_min'):g}-min blocks. "
               "Leave status blank to auto-tag Present / Late / Undertime from the time logs.")
    ed = st.data_editor(
        view, hide_index=True, width="stretch", key=f"att_ed_{day}_{acct}",
        disabled=["emp_id", "name", "account", "kind", "shift", "late_min", "undertime_min", "ot_min"],
        column_config={
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
            ex("INSERT OR REPLACE INTO attendance VALUES(?,?,?,?,?,?,?,?,?)",
               (r.agent_id, str(day), r.time_in or None, r.time_out or None, stt, late, ut, ot, r.remarks))
            saved += 1
        st.toast(f"Saved {saved} records")
        rerun()
    if c[1].button("Mark blanks as Present"):
        for r in ed.itertuples():
            src = m[m.agent_id == r.agent_id].iloc[0]
            if src.kind in WORK_KINDS and not r.status and not r.time_in:
                ex("INSERT OR REPLACE INTO attendance VALUES(?,?,?,?,?,?,?,?,?)",
                   (r.agent_id, str(day), src.shift_start, src.shift_end, "Present", 0, 0, 0, r.remarks))
        rerun()


def page_leaves(acct):
    st.header("🌴 Leave Management")
    ag = agents_df(True, acct)
    labels = agent_label_map(ag)
    t1, t2, t3, t4 = st.tabs(["File leave", "Pending approvals", "Leave calendar", "Balances"])
    with t1:
        if not labels:
            st.info("No agents yet.")
        else:
            aid = st.selectbox("Agent", list(labels), format_func=labels.get, key="lv_agent")
            arow = ag[ag.id == aid].iloc[0]
            c = st.columns(3)
            ltype = c[0].selectbox("Leave type", LEAVE_TYPES)
            d1 = c[1].date_input("From", dt.date.today() + ONE_DAY, key="lv_d1")
            d2 = c[2].date_input("To", dt.date.today() + ONE_DAY, key="lv_d2")
            reason = st.text_input("Reason")
            if d2 >= d1:
                days = working_days(arow.rest_days, d1, d2)
                st.write(f"**{days}** working day(s) — rest days ({arow.rest_days}) not counted.")
                load = leave_load(arow.account, d1, d2)
                load["after_approval"] = load.on_leave + 1
                over = load[load.after_approval > load.allowed]
                if len(over):
                    st.warning(f"⚠️ {len(over)} day(s) would exceed {arow.account}'s leave cap: "
                               + ", ".join(over.date))
                with st.expander("Leave load for these dates"):
                    st.dataframe(load, hide_index=True)
            if st.button("Submit leave", type="primary"):
                if d2 < d1:
                    st.error("End date is before start date.")
                else:
                    ex("INSERT INTO leaves(agent_id,leave_type,start_date,end_date,days,status,reason,filed_on) "
                       "VALUES(?,?,?,?,?,?,?,?)", (aid, ltype, str(d1), str(d2), days, "Pending", reason,
                                                   str(dt.date.today())))
                    st.success("Leave filed — waiting for approval.")
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
        f1 = c[0].date_input("From", dt.date.today() - dt.timedelta(days=7), key="lc1")
        f2 = c[1].date_input("To", dt.date.today() + dt.timedelta(days=30), key="lc2")
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
        yr = dt.date.today().year
        used = q("SELECT agent_id, leave_type, SUM(days) d FROM leaves WHERE status='Approved' "
                 "AND substr(start_date,1,4)=? GROUP BY agent_id, leave_type", (str(yr),))
        bal = ag[["id", "emp_id", "name", "account", "vl_credits", "sl_credits"]].copy()

        def used_of(aid, abbr):
            u = used[(used.agent_id == aid) & used.leave_type.str.contains(f"({abbr})", regex=False)]
            return float(u.d.sum())
        bal["vl_used"] = [used_of(i, "VL") for i in bal.id]
        bal["sl_used"] = [used_of(i, "SL") for i in bal.id]
        bal["vl_left"] = bal.vl_credits - bal.vl_used
        bal["sl_left"] = bal.sl_credits - bal.sl_used
        st.caption(f"Approved leave used in {yr}. Edit credits under Roster.")
        st.dataframe(bal.drop(columns="id"), hide_index=True, width="stretch")


def page_coverage(acct):
    st.header("🧩 Coverage Finder")
    day = st.date_input("Date needing coverage", dt.date.today(), key="cov_day")
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
                                          pre["for"], str(dt.date.today())))
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
                d = c[0].date_input("Date", dt.date.today())
                typ = c[1].selectbox("Type", OT_TYPES)
                s = c[2].text_input("From (HH:MM)", "07:00")
                e = c[3].text_input("To (HH:MM)", "09:00")
                reason = st.text_input("Reason / business need")
                if st.form_submit_button("File OT"):
                    if not (re.fullmatch(TIME_RE, s) and re.fullmatch(TIME_RE, e) and s and e):
                        st.error("Times must be HH:MM.")
                    else:
                        ws, we = shift_window(d, s, e)
                        ex("INSERT INTO ot_requests(agent_id,date,ot_type,start_time,end_time,hours,reason,status,filed_on) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (aid, str(d), typ, s, e,
                                                         round((we - ws).total_seconds() / 3600, 2), reason,
                                                         "Pending", str(dt.date.today())))
                        st.success("OT filed.")
    with t2:
        c = st.columns(2)
        f1 = c[0].date_input("From", week_start(dt.date.today()), key="ot1")
        f2 = c[1].date_input("To", week_start(dt.date.today()) + 6 * ONE_DAY, key="ot2")
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
        d = st.date_input("Week of", dt.date.today(), key="otw")
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
    d1 = c[0].date_input("From", dt.date.today().replace(day=1), key="r1")
    d2 = c[1].date_input("To", dt.date.today(), key="r2")
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
                              "time_out", "status", "late_min", "undertime_min", "ot_min", "remarks"]]
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
                                "vl_credits": st.column_config.NumberColumn("VL credits"),
                                "sl_credits": st.column_config.NumberColumn("SL credits"),
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
        st.caption("**Required HC** = agents needed on the floor per day (daily dashboard) · **Max leave %** = share of "
                   "headcount allowed on leave the same day. The interval columns drive the **Interval Planner**: "
                   "leave them blank to use sensible defaults for the channel.")
        ed = st.data_editor(acc, num_rows="dynamic", hide_index=True, key="acc_ed", column_config={
            "name": st.column_config.TextColumn("Account", required=True),
            "lob": st.column_config.SelectboxColumn("LOB", options=LOBS),
            "required_hc": st.column_config.NumberColumn("Required HC/day"),
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
        st.write("Columns: `emp_id, name, account, lob, team_lead, skills, shift_start, shift_end, rest_days, "
                 "vl_credits, sl_credits`")
        tmpl = pd.DataFrame([dict(emp_id="10001", name="Juan Dela Cruz", account="Telco Inbound", lob="Voice",
                                  team_lead="TL Santos", skills="Billing,Tech", shift_start="22:00",
                                  shift_end="07:00", rest_days="Sat,Sun", vl_credits=12, sl_credits=12)])
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
    today = dt.date.today()
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
        ex("INSERT OR REPLACE INTO attendance VALUES(?,?,?,?,?,?,?,?,?)",
           (r.agent_id, r.date, tin, tout, stt, lm, ut, ot, ""))
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
        st.download_button("⬇️ Download database backup", f.read(), f"wfm_backup_{dt.date.today()}.db")
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
        if st.checkbox("I understand this deletes everything") and st.button("Wipe all data"):
            for t in ("attendance", "leaves", "ot_requests", "schedule_overrides", "agents", "accounts", "interval_volume"):
                ex(f"DELETE FROM {t}")
            st.success("All data deleted.")


# ----------------------------------------------------------------- interval planner
COMMON_TZ = ["Asia/Manila", "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
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


def scheduled_by_interval(account, day):
    """Bodies on shift per interval: scheduled shifts − leaves − absences + approved OT/coverage."""
    sch = build_schedule(day - ONE_DAY, day)
    att = q("SELECT agent_id, date, status FROM attendance WHERE date BETWEEN ? AND ?",
            (str(day - ONE_DAY), str(day)))
    absent = {(int(a), d) for a, d, s in zip(att.agent_id, att.date, att.status) if s in ABSENT_STATUS}
    ot = q("SELECT o.agent_id, o.date, o.start_time, o.end_time, a.account AS own, c.account AS cov "
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
        target = r.cov if isinstance(r.cov, str) and r.cov else r.own
        if target != account or not r.start_time or not r.end_time:
            continue
        d = dt.date.fromisoformat(r.date)
        cands = [shift_window(d + dt.timedelta(days=k), r.start_time, r.end_time) for k in (0, 1)]
        base_shift = own_shift.get((int(r.agent_id), r.date))
        if base_shift and base_shift[0] is not None:  # post-shift OT after a graveyard shift lands the next day
            s0, e0 = base_shift
            cands.sort(key=lambda w: max((w[0] - e0).total_seconds(), (s0 - w[1]).total_seconds(), 0))
        windows.append(cands[0])
    t0 = dt.datetime.combine(day, dt.time())
    step = dt.timedelta(minutes=INTERVAL_MIN)
    out = []
    for i in range(len(INTERVALS)):
        a, b = t0 + i * step, t0 + (i + 1) * step
        out.append(sum(max(0.0, (min(e, b) - max(s, a)).total_seconds()) for s, e in windows if s) / INTERVAL_SEC)
    return np.array(out)


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
    today = dt.date.today()
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
    day = c[1].date_input("Date", dt.date.today(), key="iv_day")
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
    t1, t2, t3 = st.tabs(["Day view", "Week heatmap", "Volume data"])

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
                    f"{max(0.0, -short.variance.sum()) * INTERVAL_MIN / 60:.1f} HC-hours short", delta_color="off")
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
        st.caption("Short in some intervals? Use **Coverage Finder** with a partial window (e.g. 01:00–04:00) to "
                   "add pre/post-shift OT exactly where it's needed.")

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
                           "hc_hours_short": round(max(0.0, -s.variance.sum()) * INTERVAL_MIN / 60, 1),
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
        st.dataframe(cov, hide_index=True, width="stretch") if len(cov) else st.caption("No volume history yet.")
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
        up = st.file_uploader("Volume CSV", type=["csv"], key="iv_file")
        if up is not None:
            try:
                raw = pd.read_csv(up)
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
        with st.expander("Delete volume history"):
            c = st.columns(3)
            d1 = c[0].date_input("From", dt.date.today() - dt.timedelta(days=7), key="iv_del1")
            d2 = c[1].date_input("To", dt.date.today(), key="iv_del2")
            if c[2].button(f"Delete {account} volume in range"):
                ex("DELETE FROM interval_volume WHERE account=? AND date BETWEEN ? AND ?", (account, str(d1), str(d2)))
                rerun()


# ----------------------------------------------------------------- layout
PAGES = {
    "📊 Dashboard": page_dashboard,
    "⏱️ Attendance": page_attendance,
    "📶 Interval Planner": page_interval,
    "🗓️ Schedules": page_schedule,
    "🌴 Leaves": page_leaves,
    "🧩 Coverage Finder": page_coverage,
    "⏰ Overtime": page_ot,
    "📈 Reports": page_reports,
    "👥 Roster & Accounts": None,
    "⚙️ Settings": None,
}

with st.sidebar:
    st.title("🗓️ WFM Scheduler")
    page = st.radio("Go to", list(PAGES), label_visibility="collapsed")
    st.divider()
    acct_filter = st.selectbox("Account filter", ["All accounts"] + account_names())
    st.caption(f"Today: {dt.date.today():%a, %b %d %Y}")
    st.divider()
    if STORE is not None:
        synced = dt.datetime.fromtimestamp(STORE.loaded_at).strftime("%H:%M")
        st.caption(f"🟢 Google Sheet: **{STORE.title}**  \nLast loaded {synced}"
                   + (f"  \n⚠️ {len(STORE.dirty)} tab(s) waiting to save" if STORE.dirty else ""))
        if st.button("🔄 Reload from Google Sheet", width="stretch"):
            if flush_to_sheet():
                STORE.load()
            st.rerun()
    else:
        st.caption("💾 Saving to local file `wfm.db`")

if page == "👥 Roster & Accounts":
    page_roster()
elif page == "⚙️ Settings":
    page_settings()
else:
    PAGES[page](acct_filter)

# Push anything saved during this run (forms that don't rerun) to the sheet.
flush_to_sheet()
