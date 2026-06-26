"""
Spinny 3DP CRM — Cloud Edition
Real status from Bambu API: In Process / Completed / Failed
"""
import os, sqlite3, threading, time
from datetime import datetime, date, timezone, timedelta
from collections import defaultdict
from flask import Flask, render_template, jsonify, redirect, request
import requests
 
IST = timezone(timedelta(hours=5, minutes=30))
app = Flask(__name__)
 
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'spinny_3dp.db')
 
# ─── BAMBU ACCOUNTS ────────────────────────────────────────────────────────────
ACCOUNTS = [
    {
        "label":         "Pune_Blr_History",
        "token":         "AQAI_IPb10d_E9OJD-cxbBW7_CY_qw8T8Qv8yZ8AEuKBIt2YzYoYj2pgMz-APjAVScBFeNOAVV5425tx6GIte-g98L8_Fcm8hZgd7TlxxfdJzt5L1WnkA9urKvE3PfXKFH4ugqYFO34aJTaB",
        "city_override": None
    },
    {
        "label":         "Bangalore_New",
        "token":         "AQB3PWzBA4I5xpRQEx9x3X35oMnx2KNdD_Gh700Pw7tEdc0ek14YOpH8ByslcCwi-PcYCxcX1CDZc3G8W2rzBNwzXEVvywSTBOmJ-ZodyO8xy5F2OAX25SlDeZAlaojTxI7EiUD0yQsQvssw",
        "city_override": "Bangalore"
    },
    {
        "label":         "Hyderabad",
        "token":         "AQDByHOAzLNr0YeDJ7bl-NmwVUKlKI_PEoAkXfdd9D2OTKFY2wIABf4BBNTg4VGkRJwxDV7w3WEYnu83rfJMaEcul9rROkKCfflsZg1wbK09Kj45n-xqZ1VVScfpTSpbETvNSVI1Cf7N1MNr",
        "city_override": "Hyderabad"
    },
    {
        "label":         "Delhi",
        "token":         "AQAD7PzLCRKTwYBhNAFaxH6zVzOR96F2P1lVrsEUTslb7Nf1qJ8jII05YKyZ551Bkju_pThffdA-mJPhsw6HFB184Bzj8zG3KvNMnoHuTl9YrhxZRILd8ALBON33VBnIDtgoN4G0W-aJ8A",
        "city_override": "Delhi"
    },
]
 
PRINTER_CITY = {
    "Spinny-02":             "Pune",
    "Bengaluru Printer":     "Bangalore",
    "Bengaluru 3D Printer":  "Bangalore",
    "Bengaluru 3D Printer ": "Bangalore",
}
 
CITIES     = ["Pune", "Bangalore", "Hyderabad", "Delhi"]
CITY_COLOR = {
    "Pune":      "#2196F3",
    "Bangalore": "#9C27B0",
    "Hyderabad": "#FF9800",
    "Delhi":     "#43A047",
    "Unknown":   "#90A4AE",
}
 
# Bambu API real status codes
# 1=Queued  2=Currently printing (In Process)  3=Failed  4=Completed  5=Cancelled  6=Failed
STATUS_MAP = {1:"Queued", 2:"In Process", 3:"Failed", 4:"Completed", 5:"Cancelled", 6:"Failed"}
 
API_URL    = "https://api.bambulab.com/v1/user-service/my/tasks"
SHEETS_URL = os.environ.get("SHEETS_API_URL", "")
 
# ─── SHEETS CACHE ──────────────────────────────────────────────────────────────
_sheets    = {"orders": [], "designs": [], "fetched_at": 0}
SHEETS_TTL = 1800
 
def fetch_sheets(force=False):
    global _sheets
    if not SHEETS_URL:
        return _sheets
    if not force and time.time() - _sheets["fetched_at"] < SHEETS_TTL:
        return _sheets
    try:
        r = requests.get(SHEETS_URL, timeout=20)
        d = r.json()
        if d.get("ok"):
            _sheets = {
                "orders":     d["data"].get("orders", []),
                "designs":    d["data"].get("designs", []),
                "fetched_at": time.time()
            }
    except Exception as e:
        print(f"[SHEETS] Error: {e}")
    return _sheets
 
# ─── DATABASE ──────────────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db
 
def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS prints (
            id INTEGER PRIMARY KEY, task_id TEXT UNIQUE,
            date TEXT, part_name TEXT, printer TEXT, city TEXT,
            material TEXT, start_time TEXT, end_time TEXT,
            duration_min INTEGER DEFAULT 0, material_g REAL DEFAULT 0,
            status TEXT, device_model TEXT DEFAULT '',
            filament_color TEXT DEFAULT '', ist_done INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            synced_at TEXT, total_records INTEGER, new_records INTEGER, note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_date ON prints(date);
        CREATE INDEX IF NOT EXISTS idx_city ON prints(city);
    """)
    for col_sql in [
        "ALTER TABLE prints ADD COLUMN ist_done INTEGER DEFAULT 0",
        "ALTER TABLE prints ADD COLUMN device_model TEXT DEFAULT ''",
        "ALTER TABLE prints ADD COLUMN filament_color TEXT DEFAULT ''"
    ]:
        try: db.execute(col_sql)
        except: pass
    db.commit()
    db.close()
 
# ─── STARTUP AUTO-FIX ──────────────────────────────────────────────────────────
def startup_fixes():
    if not os.path.exists(DB_PATH):
        return
    db = sqlite3.connect(DB_PATH)
    try:
        today = date.today().isoformat()
 
        # Fix 1: Old "In Process" (date < today) → Completed
        # Today's "In Process" left alone — might actually be printing right now
        r1 = db.execute(
            "UPDATE prints SET status='Completed' WHERE status='In Process' AND date < ?", (today,)
        ).rowcount
        # Also fix legacy "Printing" label records (old DB)
        db.execute("UPDATE prints SET status='Completed' WHERE status='Printing' AND date < ?", (today,))
 
        # Fix 2: Today's "In Process" with endTime that's in the past → Completed
        # (Only if endTime clearly passed - not for currently printing)
        r2 = db.execute(
            "UPDATE prints SET status='Completed' WHERE status='In Process' "
            "AND end_time IS NOT NULL AND end_time != '' AND LENGTH(end_time) > 5 "
            "AND end_time < datetime('now', '+5:30 hours', '-10 minutes')"
        ).rowcount
        db.execute(
            "UPDATE prints SET status='Completed' WHERE status='Printing' "
            "AND end_time IS NOT NULL AND end_time != '' AND LENGTH(end_time) > 5 "
            "AND end_time < datetime('now', '+5:30 hours', '-10 minutes')"
        )
 
        # Fix 3: Bengaluru printer → Bangalore city
        r3 = db.execute(
            "UPDATE prints SET city='Bangalore' WHERE printer LIKE 'Bengaluru%' AND city != 'Bangalore'"
        ).rowcount
        db.execute("UPDATE prints SET city='Bangalore' WHERE city='Unknown' AND printer LIKE '%engaluru%'")
        db.execute("UPDATE prints SET city='Bangalore' WHERE city='Hyderabad' AND printer LIKE '%engaluru%'")
 
        # Fix 3b: Queued with endTime → Completed (Bambu API bug)
        db.execute(
            "UPDATE prints SET status='Completed' WHERE status='Queued' "
            "AND end_time IS NOT NULL AND end_time != '' AND LENGTH(end_time) > 5"
        )
        # Fix 3c: Queued old records (date < today, no endTime) → Completed
        db.execute(
            "UPDATE prints SET status='Completed' WHERE status='Queued' AND date < ?", (today,)
        )
 
        # Fix 4: Unrealistic duration > 1440 min (24h) → 0
        r4 = db.execute("UPDATE prints SET duration_min=0 WHERE duration_min > 1440").rowcount
 
        db.commit()
 
        # Fix 5: start==end but duration>0 → recalculate end_time
        bad = db.execute(
            "SELECT id,start_time,duration_min FROM prints "
            "WHERE start_time=end_time AND duration_min>0 AND start_time!=''"
        ).fetchall()
        r5 = 0
        for rec in bad:
            try:
                st_dt = datetime.strptime(rec[1], "%Y-%m-%d %H:%M")
                et_dt = st_dt + timedelta(minutes=rec[2])
                db.execute("UPDATE prints SET end_time=? WHERE id=?",
                           (et_dt.strftime("%Y-%m-%d %H:%M"), rec[0]))
                r5 += 1
            except: pass
 
        # Fix 6: UTC→IST one-time migration
        utc_recs = db.execute(
            "SELECT id,start_time,end_time FROM prints "
            "WHERE (ist_done IS NULL OR ist_done=0) AND start_time!=''"
        ).fetchall()
        r6 = 0
        for rec in utc_recs:
            try:
                st_utc = datetime.strptime(rec[1], "%Y-%m-%d %H:%M")
                st_ist = st_utc + timedelta(hours=5, minutes=30)
                et_str = ""
                if rec[2] and len(str(rec[2])) > 5:
                    et_utc = datetime.strptime(rec[2], "%Y-%m-%d %H:%M")
                    et_ist = et_utc + timedelta(hours=5, minutes=30)
                    et_str = et_ist.strftime("%Y-%m-%d %H:%M")
                db.execute(
                    "UPDATE prints SET start_time=?, end_time=?, date=?, ist_done=1 WHERE id=?",
                    (st_ist.strftime("%Y-%m-%d %H:%M"), et_str, st_ist.strftime("%Y-%m-%d"), rec[0])
                )
                r6 += 1
            except: pass
 
        # Re-run Fix 5 after IST conversion
        bad2 = db.execute(
            "SELECT id,start_time,duration_min FROM prints "
            "WHERE start_time=end_time AND duration_min>0 AND start_time!=''"
        ).fetchall()
        for rec in bad2:
            try:
                st_dt = datetime.strptime(rec[1], "%Y-%m-%d %H:%M")
                et_dt = st_dt + timedelta(minutes=rec[2])
                db.execute("UPDATE prints SET end_time=? WHERE id=?",
                           (et_dt.strftime("%Y-%m-%d %H:%M"), rec[0]))
            except: pass
 
        db.commit()
        print(f"[AUTO-FIX] OldDone:{r1} EndTimeFix:{r2} Blr:{r3} DurCap:{r4} EndCalc:{r5} IST:{r6}")
 
    except Exception as e:
        print(f"[AUTO-FIX ERROR] {e}")
    finally:
        db.close()
 
# ─── HELPERS ───────────────────────────────────────────────────────────────────
def parse_dt(v):
    if not v: return None
    try:
        s = str(v)
        if 'T' in s:
            return datetime.strptime(s.replace('Z','').split('.')[0], "%Y-%m-%dT%H:%M:%S")
        iv = int(float(s))
        if iv > 1e12: iv //= 1000
        return datetime.fromtimestamp(iv) if iv > 0 else None
    except: return None
 
def get_material(t):
    try:
        ams = t.get("amsDetailMapping") or []
        if isinstance(ams, list) and ams:
            types = {m.get("filamentType") or m.get("sourceColor", "") for m in ams if m}
            types = [x for x in types if x]
            if types: return "+".join(sorted(types))
        ft = t.get("filamentType", "")
        if ft: return ft
    except: pass
    return "ABS"
 
# ─── SYNC ──────────────────────────────────────────────────────────────────────
def fetch_tasks(token):
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    tasks, offset = [], 0
    while True:
        for attempt in range(3):
            try:
                r = session.get(API_URL, params={"limit":100,"offset":offset}, timeout=30)
                data = r.json(); break
            except:
                if attempt == 2: return tasks
                time.sleep(3)
        batch = data.get("hits") or data.get("data") or []
        if not batch: break
        tasks.extend(batch)
        if len(tasks) >= data.get("total",0) or len(batch) < 100: break
        offset += 100
    return tasks
 
def do_sync():
    print(f"[SYNC] {datetime.now().strftime('%H:%M:%S')}")
    db = get_db()
    existing = set(r[0] for r in db.execute("SELECT task_id FROM prints").fetchall())
    new_count = 0
 
    for acc in ACCOUNTS:
        tasks = fetch_tasks(acc["token"])
        for t in tasks:
            tid = str(t.get("id",""))
            if tid in existing:
                # Re-check: if Bambu says In Process (status=2) with no endTime → update DB
                bambu_st = int(t.get("status") or 0)
                if bambu_st == 2:
                    et_chk = parse_dt(t.get("endTime"))
                    if not et_chk:
                        # No endTime = genuinely currently printing
                        db.execute("UPDATE prints SET status='In Process' WHERE task_id=? AND status != 'In Process'", (tid,))
                continue
 
            st = parse_dt(t.get("startTime"))
            et = parse_dt(t.get("endTime"))
 
            # Duration: prefer actual (end-start), fallback costTime
            if st and et:
                dur = int((et-st).total_seconds()/60)
                if dur > 1440 or dur < 0: dur = 0
            else:
                dur = int((t.get("costTime") or 0)) // 60
                if dur > 1440 or dur < 0: dur = 0
 
            # Smart fallback: actual < 5 min but costTime > 5 min → use costTime
            if dur < 5 and t.get("costTime"):
                cost_dur = int((t.get("costTime") or 0)) // 60
                if 5 <= cost_dur <= 1440:
                    dur = cost_dur
 
            printer = t.get("deviceName", "Unknown")
            city = acc["city_override"] or PRINTER_CITY.get(printer.strip(), "Unknown")
 
            # Real Bambu status
            status = STATUS_MAP.get(int(t.get("status") or 0), str(t.get("status","")))
 
            # Bambu API bug: status=2 but endTime is set
            # Only mark Completed if endTime is MORE THAN 5 min in past
            # If endTime is recent/future → print is still running → keep In Process
            if status == "In Process" and et:
                now_utc = datetime.utcnow()
                diff_sec = (now_utc - et).total_seconds()
                if diff_sec > 300:   # endTime was 5+ min ago → print is done
                    status = "Completed"
                # else: endTime is recent or future → still In Process
 
            # UTC → IST
            st_ist = st.replace(tzinfo=timezone.utc).astimezone(IST) if st else None
            et_ist = et.replace(tzinfo=timezone.utc).astimezone(IST) if et else None
 
            # start==end but duration>0 → calculate real end from duration
            if st_ist and et_ist and st_ist == et_ist and dur > 0:
                et_ist = st_ist + timedelta(minutes=dur)
 
            device_model = t.get("deviceModel", "")
            fil_color = ""
            try:
                ams = t.get("amsDetailMapping") or []
                if isinstance(ams, list) and ams and isinstance(ams[0], dict):
                    fil_color = ams[0].get("sourceColor", "")[:6]
            except: pass
 
            db.execute("""
                INSERT OR IGNORE INTO prints
                (task_id,date,part_name,printer,city,material,start_time,end_time,
                 duration_min,material_g,status,device_model,filament_color,ist_done)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            """, (
                tid,
                st_ist.strftime("%Y-%m-%d") if st_ist else "",
                t.get("title", "Unknown"), printer, city, get_material(t),
                st_ist.strftime("%Y-%m-%d %H:%M") if st_ist else "",
                et_ist.strftime("%Y-%m-%d %H:%M") if et_ist else "",
                dur,
                round(float(t.get("weight") or 0), 2),
                status, device_model, fil_color
            ))
            existing.add(tid)
            new_count += 1
 
    total = db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    db.execute(
        "INSERT INTO sync_log (synced_at,total_records,new_records,note) VALUES (?,?,?,?)",
        (datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"), total, new_count, "OK")
    )
    db.commit()
    db.close()
    print(f"[SYNC] Done +{new_count} | total {total}")
    startup_fixes()
    return new_count
 
def auto_sync_loop():
    try: do_sync()
    except Exception as e: print(f"[SYNC] Startup error: {e}")
    try: fetch_sheets(force=True)
    except: pass
    while True:
        time.sleep(7200)
        try: do_sync()
        except Exception as e: print(f"[SYNC] Error: {e}")
 
# ─── ROUTES ────────────────────────────────────────────────────────────────────
@app.route('/')
def dashboard():
    db    = get_db()
    today = date.today().strftime("%Y-%m-%d")
 
    total     = db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    completed = db.execute("SELECT COUNT(*) FROM prints WHERE status='Completed'").fetchone()[0]
    failed    = db.execute("SELECT COUNT(*) FROM prints WHERE status IN ('Failed','Cancelled')").fetchone()[0]
    in_proc   = db.execute("SELECT COUNT(*) FROM prints WHERE status='In Process'").fetchone()[0]
    hrs_total = db.execute("SELECT COALESCE(SUM(duration_min),0)/60.0 FROM prints WHERE status IN ('Completed','In Process','Failed')").fetchone()[0]
    mat_total = db.execute("SELECT COALESCE(SUM(material_g),0)/1000.0 FROM prints").fetchone()[0]
 
    cities_today = {}
    for c in CITIES:
        r = db.execute("""
            SELECT COUNT(*),
                COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN duration_min ELSE 0 END),0)/60.0,
                COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN material_g ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN status='In Process' THEN 1 ELSE 0 END),0)
            FROM prints WHERE city=? AND date=? AND status NOT IN ('Queued','Cancelled')""", (c, today)).fetchone()
        cities_today[c] = {
            "prints": r[0], "hours": round(r[1],1),
            "mat_g": round(r[2],1), "ok": r[3], "live": r[4],
            "color": CITY_COLOR[c]
        }
 
    today_total = {
        "prints": sum(v["prints"] for v in cities_today.values()),
        "ok":     sum(v["ok"]     for v in cities_today.values()),
        "live":   sum(v["live"]   for v in cities_today.values()),
        "hours":  round(sum(v["hours"] for v in cities_today.values()), 1),
        "mat_g":  round(sum(v["mat_g"] for v in cities_today.values()), 1),
        "failed": db.execute(
            "SELECT COUNT(*) FROM prints WHERE date=? AND status IN ('Failed','Cancelled')",
            (today,)).fetchone()[0]
    }
 
    recent_rows = db.execute("""
        SELECT date,part_name,printer,city,material,duration_min,material_g,status,start_time,end_time
        FROM prints WHERE date != '' ORDER BY date DESC, start_time DESC LIMIT 25
    """).fetchall()
    # Pre-build HTML to avoid Jinja2 loop variable bug on Python 3.14
    recent_html = ""
    for row in recent_rows:
        r_date, r_part, r_printer, r_city, r_mat, r_dur, r_matg, r_status, r_st, r_et = (
            row[0] or "", row[1] or "", row[2] or "", row[3] or "",
            row[4] or "", row[5] or 0, row[6] or 0, row[7] or "", row[8] or "", row[9] or ""
        )
        st_hm = r_st[11:16] if r_st and len(r_st) > 10 else "—"
        et_hm = r_et[11:16] if r_et and len(r_et) > 10 else "—"
        city_color = CITY_COLOR.get(r_city, "#999")
        # Duration
        if r_dur and int(r_dur) > 0:
            d = int(r_dur)
            dur_str = f"{d//60}h {d%60}m" if d >= 60 else f"{d}m"
        else:
            dur_str = "—"
        # End time / Live
        if r_status == "In Process":
            et_cell = "<span style='color:#f59e0b'>Live</span>"
        elif et_hm != "—":
            et_cell = et_hm
        else:
            et_cell = "—"
        # Status badge
        if r_status == "Completed":
            badge = "<span class='badge b-completed'>✓ Done</span>"
        elif r_status == "Failed":
            badge = "<span class='badge b-failed'>✗ Failed</span>"
        elif r_status == "In Process":
            badge = "<span class='badge b-printing' style='background:#f59e0b;color:#fff'>In Process</span>"
        elif r_status == "Cancelled":
            badge = "<span class='badge b-cancelled'>Cancelled</span>"
        else:
            badge = f"<span class='badge b-cancelled'>{r_status}</span>"
        # Part name with live dot
        part_prefix = "<span style='color:#f59e0b'>● </span>" if r_status == "In Process" else ""
        row_style = "style='background:rgba(245,158,11,0.08)'" if r_status == "In Process" else ""
        recent_html += (
            f"<tr {row_style}>"
            f"<td class='mono'>{r_date}</td>"
            f"<td class='td-part' title='{r_part}'>{part_prefix}{r_part}</td>"
            f"<td><span class='b-city' style='background:{city_color}'>{r_city}</span></td>"
            f"<td class='mono' style='color:#1d4ed8;font-weight:600'>{st_hm}</td>"
            f"<td class='mono' style='color:#7c3aed;font-weight:600'>{et_cell}</td>"
            f"<td class='mono' style='font-weight:600'>{dur_str}</td>"
            f"<td class='mono'>{r_matg}g</td>"
            f"<td>{badge}</td>"
            f"</tr>"
        )
    if not recent_html:
        recent_html = "<tr><td colspan='8' style='text-align:center;color:#9ca3af;padding:24px'>No prints yet</td></tr>"
 
    # Daily summary — last 30 days, per city
    dates = db.execute(
        "SELECT DISTINCT date FROM prints WHERE date != '' ORDER BY date DESC LIMIT 30"
    ).fetchall()
    daily_data = []
    for drow in dates:
        d = drow[0]
        total_p = 0; total_h = 0.0; total_m = 0
        city_row = {}
        for cn in CITIES:
            r = db.execute("""
                SELECT COUNT(*),
                    COALESCE(ROUND(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN duration_min ELSE 0 END)/60.0,1),0),
                    COALESCE(ROUND(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN material_g ELSE 0 END),0),0)
                FROM prints WHERE date=? AND city=? AND status NOT IN ('Queued','Cancelled')""", (d, cn)).fetchone()
            p, h, m = r[0] or 0, float(r[1] or 0), int(r[2] or 0)
            city_row[cn] = {"parts": p, "hours": h, "mat_g": m}
            total_p += p; total_h += h; total_m += m
        # Use dict (not tuple) for Jinja2 compatibility
        daily_data.append({
            "date":        d,
            "Pune":        city_row.get("Pune",      {"parts":0,"hours":0.0,"mat_g":0}),
            "Bangalore":   city_row.get("Bangalore",  {"parts":0,"hours":0.0,"mat_g":0}),
            "Hyderabad":   city_row.get("Hyderabad",  {"parts":0,"hours":0.0,"mat_g":0}),
            "Delhi":       city_row.get("Delhi",      {"parts":0,"hours":0.0,"mat_g":0}),
            "total_parts": total_p,
            "total_hours": round(total_h,1),
            "total_mat":   total_m
        })
 
    last_sync = db.execute(
        "SELECT synced_at,total_records FROM sync_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
 
    sheets  = _sheets
    orders  = sheets.get("orders", [])
    designs = sheets.get("designs", [])
 
    ord_city = {}
    for c in CITIES:
        co = [o for o in orders if o.get("order_city") == c]
        ord_city[c] = {
            "total":     len(co),
            "fulfilled": len([o for o in co if "fulfilled" in o.get("status","").lower()]),
            "pending":   len([o for o in co if o.get("status","").lower() not in ["fulfilled","cancelled",""]])
        }
 
    today_designs = [d for d in designs if d.get("design_date","") == today]
    month         = today[:7]
    month_designs = [d for d in designs if str(d.get("design_date","")).startswith(month)]
 
    db.close()
    return render_template('dashboard.html',
        total=total, completed=completed, failed=failed, in_proc=in_proc,
        hrs_total=round(hrs_total,1), mat_total=round(mat_total,2),
        cities_today=cities_today, today_total=today_total,
        recent_html=recent_html, daily_rows_html=daily_rows_html,
        last_sync=last_sync, today=today,
        city_color=CITY_COLOR, cities=CITIES,
        ord_city=ord_city, total_orders=len(orders),
        today_designs=today_designs, month_designs=month_designs,
        total_designs=len(designs))
 
 
@app.route('/city/<city>')
def city_page(city):
    if city not in CITIES: return redirect('/')
    db    = get_db()
    today = date.today().strftime("%Y-%m-%d")
    ov = db.execute("""
        SELECT COUNT(*),
            COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status='Failed' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN duration_min ELSE 0 END),0)/60.0,
            COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN material_g ELSE 0 END),0)/1000.0
        FROM prints WHERE city=?""", (city,)).fetchone()
    td = db.execute("""
        SELECT COUNT(*),
            COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN duration_min ELSE 0 END),0)/60.0,
            COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN material_g ELSE 0 END),0)
        FROM prints WHERE city=? AND date=? AND status NOT IN ('Queued','Cancelled')""", (city, today)).fetchone()
    rows = db.execute("""
        SELECT date,part_name,printer,material,start_time,end_time,
               duration_min,material_g,status,device_model,filament_color
        FROM prints WHERE city=? ORDER BY date DESC, start_time DESC""", (city,)).fetchall()
    db.close()
    return render_template('city.html', city=city, color=CITY_COLOR[city],
        ov=ov, td=td, rows=rows, today=today, city_color=CITY_COLOR, cities=CITIES)
 
 
@app.route('/monthly')
def monthly():
    db = get_db()
    rows = db.execute("""
        SELECT substr(date,1,7) as mo, city,
            COUNT(*) as total,
            COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0) as done,
            COALESCE(SUM(CASE WHEN status IN ('Failed','Cancelled') THEN 1 ELSE 0 END),0) as failed,
            ROUND(COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN duration_min ELSE 0 END),0)/60.0, 1) as hours,
            ROUND(COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN material_g ELSE 0 END),0)/1000.0, 3) as mat_kg
        FROM prints WHERE date!='' GROUP BY mo,city ORDER BY mo DESC,city
    """).fetchall()
 
    months_data = defaultdict(
        lambda: {c: {"total":0,"done":0,"failed":0,"hours":0,"mat_kg":0} for c in CITIES}
    )
    for r in rows:
        if r[1] in CITIES:
            months_data[r[0]][r[1]] = {
                "total":r[2],"done":r[3],"failed":r[4],"hours":r[5],"mat_kg":r[6]
            }
 
    months_list = []
    for mo in sorted(months_data.keys(), reverse=True):
        cd = months_data[mo]
        months_list.append((
            mo, cd,
            sum(v["total"]  for v in cd.values()),
            sum(v["done"]   for v in cd.values()),
            sum(v["failed"] for v in cd.values()),
            round(sum(v["hours"]  for v in cd.values()), 1),
            round(sum(v["mat_kg"] for v in cd.values()), 2)
        ))
 
    db.close()
    return render_template('monthly.html', months_list=months_list,
                           city_color=CITY_COLOR, cities=CITIES)
 
 
@app.route('/materials')
def materials():
    db = get_db()
    top = db.execute("""
        SELECT material, COUNT(*), COALESCE(SUM(material_g),0)/1000.0
        FROM prints GROUP BY material ORDER BY 3 DESC""").fetchall()
    by_city = db.execute("""
        SELECT city,material,COUNT(*),COALESCE(SUM(material_g),0)/1000.0
        FROM prints GROUP BY city,material ORDER BY city,4 DESC""").fetchall()
    db.close()
    return render_template('materials.html', top=top, by_city=by_city,
                           city_color=CITY_COLOR, cities=CITIES)
 
 
@app.route('/fails')
def fails():
    db = get_db()
    rows = db.execute("""
        SELECT date,part_name,printer,city,material,duration_min,material_g,status
        FROM prints WHERE status IN ('Failed','Cancelled') ORDER BY date DESC""").fetchall()
    top = db.execute("""
        SELECT part_name,COUNT(*),city FROM prints
        WHERE status IN ('Failed','Cancelled') GROUP BY part_name ORDER BY 2 DESC LIMIT 20""").fetchall()
    db.close()
    return render_template('fails.html', rows=rows, top=top,
                           city_color=CITY_COLOR, cities=CITIES)
 
 
@app.route('/orders')
def orders():
    data          = fetch_sheets(force=False)
    all_orders    = data.get("orders", [])
    city_filter   = request.args.get("city", "All")
    status_filter = request.args.get("status", "All")
    filtered = all_orders
    if city_filter != "All":
        filtered = [o for o in filtered if o.get("order_city")==city_filter or o.get("source_city")==city_filter]
    if status_filter != "All":
        filtered = [o for o in filtered if status_filter.lower() in o.get("status","").lower()]
    city_stats = {}
    for c in CITIES:
        co = [o for o in all_orders if o.get("order_city")==c]
        city_stats[c] = {
            "total":     len(co),
            "fulfilled": len([o for o in co if "fulfilled" in o.get("status","").lower()]),
            "pending":   len([o for o in co if o.get("status","").lower() not in ["fulfilled","cancelled",""]]),
            "color":     CITY_COLOR[c]
        }
    statuses = sorted(set(o.get("status","") for o in all_orders if o.get("status","")))
    return render_template('orders.html',
        orders=filtered, city_stats=city_stats,
        city_filter=city_filter, status_filter=status_filter,
        statuses=statuses, city_color=CITY_COLOR, cities=CITIES,
        total=len(all_orders))
 
 
@app.route('/designs')
def designs():
    data        = fetch_sheets(force=False)
    all_designs = data.get("designs", [])
    today_str   = date.today().strftime("%Y-%m-%d")
    today_count = len([d for d in all_designs if d.get("design_date","") == today_str])
    return render_template('designs.html', designs=all_designs,
                           city_color=CITY_COLOR, cities=CITIES,
                           today_count=today_count)
 
 
@app.route('/api/sheets_update', methods=['POST'])
def sheets_update():
    global _sheets
    try:
        payload = request.get_json(force=True)
        if payload:
            _sheets = {
                "orders":     payload.get("orders", []),
                "designs":    payload.get("designs", []),
                "fetched_at": time.time()
            }
            return jsonify({"ok": True})
    except Exception as e:
        print(f"[SHEETS] Push error: {e}")
    return jsonify({"ok": False}), 400
 
 
@app.route('/api/sync', methods=['GET','POST'])
def api_sync():
    try:
        n = do_sync()
        fetch_sheets(force=True)
        return jsonify({"ok": True, "new": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
 
 
@app.route('/api/health')
def health():
    db = get_db()
    total   = db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    in_proc = db.execute("SELECT COUNT(*) FROM prints WHERE status='In Process'").fetchone()[0]
    ls      = db.execute("SELECT synced_at FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    return jsonify({
        "status":     "ok",
        "total":      total,
        "in_process": in_proc,
        "last_sync":  ls[0] if ls else None
    })
 
 
# ─── STARTUP ───────────────────────────────────────────────────────────────────
init_db()
startup_fixes()
threading.Thread(target=auto_sync_loop, daemon=True).start()
 
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
