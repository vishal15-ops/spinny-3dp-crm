"""
Spinny 3DP CRM — Cloud Edition v2 Final + Auto-Fix
Multi-city Bambu Lab print tracker + Orders + New Designs
4 separate city accounts | startup_fixes | today_total
"""

import os, sqlite3, threading, time, json
from datetime import datetime, date
from flask import Flask, render_template, jsonify, redirect, request
import requests

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'spinny_3dp.db')

# ─── BAMBU ACCOUNTS ───────────────────────────────────────────────────────────
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
STATUS_MAP = {1:"Queued", 2:"Printing", 3:"Failed", 4:"Completed", 5:"Cancelled", 6:"Failed"}
API_URL    = "https://api.bambulab.com/v1/user-service/my/tasks"
SHEETS_URL = os.environ.get("SHEETS_API_URL", "")

# ─── SHEETS CACHE ─────────────────────────────────────────────────────────────
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

# ─── DATABASE ─────────────────────────────────────────────────────────────────
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
            duration_min INTEGER DEFAULT 0, material_g REAL DEFAULT 0, status TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            synced_at TEXT, total_records INTEGER, new_records INTEGER, note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_date ON prints(date);
        CREATE INDEX IF NOT EXISTS idx_city ON prints(city);
    """)
    db.commit()
    db.close()

# ─── STARTUP AUTO-FIX ─────────────────────────────────────────────────────────
def startup_fixes():
    """Har restart/redeploy pe automatically sab theek karta hai"""
    if not os.path.exists(DB_PATH):
        return
    db = sqlite3.connect(DB_PATH)
    try:
        today = date.today().isoformat()

        # Fix 1: Purane "Printing" → "Completed" (aaj ke live prints safe rahenge)
        r1 = db.execute(
            "UPDATE prints SET status='Completed' WHERE status='Printing' AND date < ?",
            (today,)
        ).rowcount

        # Fix 2: Bengaluru printer → Bangalore city (trailing space bhi handle)
        r2 = db.execute(
            "UPDATE prints SET city='Bangalore' WHERE printer LIKE 'Bengaluru%' AND city != 'Bangalore'"
        ).rowcount

        # Fix 3: Unknown city jo Bengaluru printer se hai
        r3 = db.execute(
            "UPDATE prints SET city='Bangalore' WHERE city='Unknown' AND printer LIKE '%engaluru%'"
        ).rowcount

        # Fix 4: Hyderabad account mein jo Bengaluru printer hai → Bangalore
        r4 = db.execute(
            "UPDATE prints SET city='Bangalore' WHERE city='Hyderabad' AND printer LIKE '%engaluru%'"
        ).rowcount

        # Fix 5: Duration > 0 wale "Printing" records → Completed (Bambu API bug fix)
        r5 = db.execute(
            "UPDATE prints SET status='Completed' WHERE status='Printing' AND duration_min > 0"
        ).rowcount
        
        # Fix 6: endTime set hai but still "Printing" → Completed
        r6 = db.execute(
            "UPDATE prints SET status='Completed' WHERE status='Printing' AND end_time IS NOT NULL AND end_time != '' AND end_time NOT LIKE 'None%' AND LENGTH(end_time) > 5"
        ).rowcount

        db.commit()
        print(f"[AUTO-FIX] Status: +{r1} | Blr: +{r2} | Unknown: +{r3} | Hyd→Blr: +{r4} | Duration fix: +{r5} | EndTime fix: +{r6}")
    except Exception as e:
        print(f"[AUTO-FIX ERROR] {e}")
    finally:
        db.close()

# ─── HELPERS ──────────────────────────────────────────────────────────────────
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

# ─── SYNC ─────────────────────────────────────────────────────────────────────
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
            if tid in existing: continue
            st = parse_dt(t.get("startTime"))
            et = parse_dt(t.get("endTime"))
            if st and et:
                dur = int((et-st).total_seconds()/60)
                if dur > 720 or dur < 0: dur = 0
            else:
                dur = int((t.get("costTime") or 0))//60
                if dur > 720 or dur < 0: dur = 0
            # Fallback: use costTime if calculated dur is 0
            if dur == 0 and t.get("costTime"):
                dur = int((t.get("costTime") or 0))//60
                if dur > 720 or dur < 0: dur = 0
            printer = t.get("deviceName","Unknown")
            city = acc["city_override"] or PRINTER_CITY.get(printer.strip(), "Unknown")
            status = STATUS_MAP.get(int(t.get("status") or 0), str(t.get("status","")))
            # Bambu API bug fix: agar endTime set hai → print complete ho gaya
            if et and status == "Printing":
                status = "Completed"
            db.execute("""INSERT OR IGNORE INTO prints
                (task_id,date,part_name,printer,city,material,start_time,end_time,duration_min,material_g,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (tid,
                 st.strftime("%Y-%m-%d") if st else "",
                 t.get("title","Unknown"), printer, city, get_material(t),
                 st.strftime("%Y-%m-%d %H:%M") if st else "",
                 et.strftime("%Y-%m-%d %H:%M") if et else "",
                 dur, round(float(t.get("weight") or 0),2), status))
            existing.add(tid)
            new_count += 1
    total = db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    db.execute("INSERT INTO sync_log (synced_at,total_records,new_records,note) VALUES (?,?,?,?)",
               (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), total, new_count, "OK"))
    db.commit()
    db.close()
    print(f"[SYNC] Done +{new_count} | total {total}")
    # Auto-fix after every sync
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

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    db    = get_db()
    today = date.today().strftime("%Y-%m-%d")

    total     = db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    completed = db.execute("SELECT COUNT(*) FROM prints WHERE status='Completed'").fetchone()[0]
    failed    = db.execute("SELECT COUNT(*) FROM prints WHERE status IN ('Failed','Cancelled')").fetchone()[0]
    hrs_total = db.execute("SELECT COALESCE(SUM(duration_min),0)/60.0 FROM prints").fetchone()[0]
    mat_total = db.execute("SELECT COALESCE(SUM(material_g),0)/1000.0 FROM prints").fetchone()[0]

    cities_today = {}
    for c in CITIES:
        r = db.execute("""SELECT COUNT(*),
            COALESCE(SUM(duration_min),0)/60.0,
            COALESCE(SUM(material_g),0),
            COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0)
            FROM prints WHERE city=? AND date=?""", (c, today)).fetchone()
        cities_today[c] = {
            "prints": r[0], "hours": round(r[1],1),
            "mat_g": round(r[2],1), "ok": r[3],
            "color": CITY_COLOR[c]
        }

    today_total = {
        "prints": sum(v["prints"] for v in cities_today.values()),
        "ok":     sum(v["ok"]     for v in cities_today.values()),
        "hours":  round(sum(v["hours"]  for v in cities_today.values()), 1),
        "mat_g":  round(sum(v["mat_g"]  for v in cities_today.values()), 1),
        "failed": db.execute(
            "SELECT COUNT(*) FROM prints WHERE date=? AND status IN ('Failed','Cancelled')",
            (today,)).fetchone()[0]
    }

    recent = db.execute("""SELECT date,part_name,printer,city,material,duration_min,material_g,status,start_time,end_time
        FROM prints WHERE date != '' ORDER BY date DESC, start_time DESC LIMIT 25""").fetchall()
    last_sync = db.execute("SELECT synced_at,total_records FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()

    sheets = _sheets
    orders = sheets.get("orders", [])
    ord_city = {}
    for c in CITIES:
        co = [o for o in orders if o.get("order_city") == c]
        ord_city[c] = {
            "total":     len(co),
            "fulfilled": len([o for o in co if "fulfilled" in o.get("status","").lower()]),
            "pending":   len([o for o in co if o.get("status","").lower() not in ["fulfilled","cancelled",""]])
        }

    db.close()
    return render_template('dashboard.html',
        total=total, completed=completed, failed=failed,
        hrs_total=round(hrs_total,1), mat_total=round(mat_total,2),
        cities_today=cities_today, today_total=today_total,
        recent=recent, last_sync=last_sync, today=today,
        city_color=CITY_COLOR, cities=CITIES,
        ord_city=ord_city, total_orders=len(orders))


@app.route('/city/<city>')
def city_page(city):
    if city not in CITIES: return redirect('/')
    db    = get_db()
    today = date.today().strftime("%Y-%m-%d")
    ov = db.execute("""SELECT COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status IN ('Failed','Cancelled') THEN 1 ELSE 0 END),0),
        COALESCE(SUM(duration_min),0)/60.0,
        COALESCE(SUM(material_g),0)/1000.0
        FROM prints WHERE city=?""", (city,)).fetchone()
    td = db.execute("""SELECT COUNT(*), COALESCE(SUM(duration_min),0)/60.0, COALESCE(SUM(material_g),0)
        FROM prints WHERE city=? AND date=?""", (city, today)).fetchone()
    rows = db.execute("""SELECT date,part_name,printer,material,start_time,end_time,duration_min,material_g,status
        FROM prints WHERE city=? ORDER BY date DESC, start_time DESC""", (city,)).fetchall()
    db.close()
    return render_template('city.html', city=city, color=CITY_COLOR[city],
        ov=ov, td=td, rows=rows, today=today, city_color=CITY_COLOR, cities=CITIES)


@app.route('/monthly')
def monthly():
    db = get_db()
    rows = db.execute("""SELECT substr(date,1,7) as mo, city, COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(duration_min),0)/60.0,
        COALESCE(SUM(material_g),0)/1000.0
        FROM prints WHERE date!='' GROUP BY mo,city ORDER BY mo DESC,city""").fetchall()
    db.close()
    return render_template('monthly.html', rows=rows, city_color=CITY_COLOR, cities=CITIES)


@app.route('/materials')
def materials():
    db = get_db()
    top = db.execute("""SELECT material, COUNT(*), COALESCE(SUM(material_g),0)/1000.0
        FROM prints GROUP BY material ORDER BY 3 DESC""").fetchall()
    by_city = db.execute("""SELECT city,material,COUNT(*),COALESCE(SUM(material_g),0)/1000.0
        FROM prints GROUP BY city,material ORDER BY city,4 DESC""").fetchall()
    db.close()
    return render_template('materials.html', top=top, by_city=by_city,
                           city_color=CITY_COLOR, cities=CITIES)


@app.route('/fails')
def fails():
    db = get_db()
    rows = db.execute("""SELECT date,part_name,printer,city,material,duration_min,material_g,status
        FROM prints WHERE status IN ('Failed','Cancelled') ORDER BY date DESC""").fetchall()
    top = db.execute("""SELECT part_name,COUNT(*),city FROM prints
        WHERE status IN ('Failed','Cancelled') GROUP BY part_name ORDER BY 2 DESC LIMIT 20""").fetchall()
    db.close()
    return render_template('fails.html', rows=rows, top=top, city_color=CITY_COLOR, cities=CITIES)


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
    data = fetch_sheets(force=False)
    return render_template('designs.html', designs=data.get("designs",[]),
                           city_color=CITY_COLOR, cities=CITIES)


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
    total = db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    ls    = db.execute("SELECT synced_at FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    return jsonify({"status":"ok","total":total,"last_sync":ls[0] if ls else None})


# ─── STARTUP ──────────────────────────────────────────────────────────────────
init_db()
startup_fixes()   # ← Auto-fix on every restart/redeploy
threading.Thread(target=auto_sync_loop, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
