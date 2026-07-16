"""Spinny 3DP CRM - Cloud Edition. Real data from Bambu API, auto-cleaned."""
import os, re, sqlite3, threading, time, html as _html, json as _json
from datetime import datetime, date, timezone, timedelta
from flask import Flask, render_template, jsonify, redirect, request
import requests
from collections import defaultdict

IST = timezone(timedelta(hours=5, minutes=30))
app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'spinny_3dp.db')

CANCEL_FLOW_G_PER_MIN = 0.4
MAX_PLAUSIBLE_G_PER_MIN = 5.0

# ============================================================
# MATERIAL COST (INR per KG) — invoice se (ex-GST base rate).
# 1 spool = 1 kg maana hai. Naya rate aaye to yahan update kar do.
#
# CONFIRMED RATES (dono invoices se cross-check hue):
#   ABS+     = Rs  820 /kg   (invoice SO267964, 13/07/2026)
#   PLA+     = Rs  990 /kg   (invoice SO267964, 13/07/2026)
#   PETG+    = Rs  960 /kg   (invoice SO267964, 13/07/2026)
#   TPU-95A  = Rs 1585 /kg   (confirmed 2 invoices — matches exactly)
#   PA12     = Rs 4652 /kg   (confirmed 2 invoices — matches exactly)
#
# NOTE: Agar GST-inclusive cost chahiye to har number ko *1.18 karke daal do
# (e.g. ABS 820 -> 967.6).
# ============================================================
MATERIAL_COST_PER_KG = {
    "ABS":  820.0,
    "PLA":  990.0,
    "PETG": 960.0,
    "TPU":  1585.0,
    "PA12": 4652.0,
}

def part_cost_inr(matg, material):
    """Ek part ka material cost (Rs) = grams/1000 * per-kg rate."""
    m = (material or "").upper()
    if "TPU" in m:    perkg = MATERIAL_COST_PER_KG["TPU"]
    elif "PA" in m:   perkg = MATERIAL_COST_PER_KG["PA12"]
    elif "PETG" in m: perkg = MATERIAL_COST_PER_KG["PETG"]
    elif "PLA" in m:  perkg = MATERIAL_COST_PER_KG["PLA"]
    else:             perkg = MATERIAL_COST_PER_KG["ABS"]
    return float(matg or 0) / 1000.0 * perkg


ACCOUNTS = [
    {"label":"Pune_Blr_History","token":"AQAI_IPb10d_E9OJD-cxbBW7_CY_qw8T8Qv8yZ8AEuKBIt2YzYoYj2pgMz-APjAVScBFeNOAVV5425tx6GIte-g98L8_Fcm8hZgd7TlxxfdJzt5L1WnkA9urKvE3PfXKFH4ugqYFO34aJTaB","city_override":None},
    {"label":"Bangalore_New","token":"AQB3PWzBA4I5xpRQEx9x3X35oMnx2KNdD_Gh700Pw7tEdc0ek14YOpH8ByslcCwi-PcYCxcX1CDZc3G8W2rzBNwzXEVvywSTBOmJ-ZodyO8xy5F2OAX25SlDeZAlaojTxI7EiUD0yQsQvssw","city_override":"Bangalore"},
    {"label":"Hyderabad","token":"AQDByHOAzLNr0YeDJ7bl-NmwVUKlKI_PEoAkXfdd9D2OTKFY2wIABf4BBNTg4VGkRJwxDV7w3WEYnu83rfJMaEcul9rROkKCfflsZg1wbK09Kj45n-xqZ1VVScfpTSpbETvNSVI1Cf7N1MNr","city_override":"Hyderabad"},
    {"label":"Delhi","token":"AQAD7PzLCRKTwYBhNAFaxH6zVzOR96F2P1lVrsEUTslb7Nf1qJ8jII05YKyZ551Bkju_pThffdA-mJPhsw6HFB184Bzj8zG3KvNMnoHuTl9YrhxZRILd8ALBON33VBnIDtgoN4G0W-aJ8A","city_override":"Delhi"},
]
PRINTER_CITY = {
    "Spinny-02":"Pune","Bengaluru Printer":"Bangalore",
    "Bengaluru 3D Printer":"Bangalore","Bengaluru 3D Printer ":"Bangalore",
}
CITIES = ["Pune","Bangalore","Hyderabad","Delhi"]
CITY_COLOR = {"Pune":"#2196F3","Bangalore":"#9C27B0","Hyderabad":"#FF9800","Delhi":"#43A047","Unknown":"#90A4AE"}
STATUS_MAP = {1:"Queued",2:"In Process",3:"Cancelled",4:"Completed",5:"Cancelled",6:"Cancelled"}
API_URL  = "https://api.bambulab.com/v1/user-service/my/tasks"
SHEETS_URL = os.environ.get("SHEETS_API_URL","")
STOCK_SHEET_URL = os.environ.get("STOCK_SHEET_URL","")

_sheets = {"orders":[],"designs":[],"pendency":[],"fetched_at":0}
SHEETS_TTL = 1800

# ============================================================
# MACHINE NAME NORMALIZATION
# ============================================================
# "3DP Remarks" column mein machine names loosely likhe jaate hain
# (e.g. "P1S DELHI", "P1S", "X1-CARBON", "X1 Carbon"). Bambu printer
# ki "printer" field mein bhi apna naam hota hai (e.g. "3DP-01P-673",
# "Spinny-01"). In dono ko match karne ke liye ek normalize function
# + mapping table use karte hain. Naye printer/city milte hi is
# mapping mein add karte jao.
# ============================================================
def normalize_machine(raw):
    s = str(raw or "").strip().upper()
    s = re.sub(r'\s+', ' ', s)
    if not s: return ""
    if "P1S" in s: return "P1S"
    if "X1" in s: return "X1 CARBON"
    return s

# Bambu "printer" field -> normalized machine key (extend as needed)
PRINTER_TO_MACHINE = {
    "3DP-01P-673": "P1S",
    "Spinny-01": "X1 CARBON",
    "Spinny-02": "P1S",
}

def machine_of_printer(printer_name):
    key = str(printer_name or "").strip()
    if key in PRINTER_TO_MACHINE:
        return PRINTER_TO_MACHINE[key]
    return normalize_machine(key)

def fetch_sheets(force=False):
    global _sheets
    if not SHEETS_URL: return _sheets
    if not force and time.time()-_sheets["fetched_at"]<SHEETS_TTL: return _sheets
    try:
        r = requests.get(SHEETS_URL,timeout=20); d=r.json()
        if d.get("ok"):
            _sheets={"orders":d["data"].get("orders",[]),"designs":d["data"].get("designs",[]),"pendency":d["data"].get("pendency",[]),"fetched_at":time.time()}
    except Exception as e: print(f"[SHEETS] {e}")
    return _sheets

def get_db():
    db=sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA busy_timeout=30000")
    return db

def init_db():
    db=get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS prints (
            id INTEGER PRIMARY KEY, task_id TEXT UNIQUE,
            date TEXT, part_name TEXT, printer TEXT, city TEXT,
            material TEXT, start_time TEXT, end_time TEXT,
            duration_min INTEGER DEFAULT 0, material_g REAL DEFAULT 0,
            status TEXT, device_model TEXT DEFAULT '',
            filament_color TEXT DEFAULT '', ist_done INTEGER DEFAULT 0,
            cost_time INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            synced_at TEXT, total_records INTEGER, new_records INTEGER, note TEXT);
        CREATE TABLE IF NOT EXISTS sheets_cache (
            key TEXT PRIMARY KEY, data TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS stock_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE, unit TEXT DEFAULT 'Kgs', active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS stock_txn (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, city TEXT, item_id INTEGER,
            txn_type TEXT, qty REAL, note TEXT DEFAULT '', created_at TEXT);
        CREATE TABLE IF NOT EXISTS stock_sheet_log (
            source_id TEXT PRIMARY KEY, synced_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_date ON prints(date);
        CREATE INDEX IF NOT EXISTS idx_city ON prints(city);""")
    for col in ["ALTER TABLE prints ADD COLUMN ist_done INTEGER DEFAULT 0",
                "ALTER TABLE prints ADD COLUMN device_model TEXT DEFAULT ''",
                "ALTER TABLE prints ADD COLUMN filament_color TEXT DEFAULT ''",
                "ALTER TABLE prints ADD COLUMN cost_time INTEGER DEFAULT 0",
                "ALTER TABLE stock_items ADD COLUMN reorder_level REAL DEFAULT 0",
                "ALTER TABLE stock_txn ADD COLUMN txn_time TEXT DEFAULT ''",
                "ALTER TABLE stock_txn ADD COLUMN machine TEXT DEFAULT ''",
                "ALTER TABLE prints ADD COLUMN manual_status TEXT DEFAULT ''"]:  # <-- NAYA COLUMN (cancel reclassify)
        try: db.execute(col)
        except: pass
    STOCK_SEED=[("eSUN ABS+ Filament 1.75mm Black","Kgs"),
        ("eSUN ABS+ Filament 1.75mm White","Kgs"),
        ("eSUN PLA+ Filament 1.75mm Black","Kgs"),
        ("eSUN PLA+ Filament 1.75mm White","Kgs"),
        ("eSUN TPU-95A Filament 1.75mm Black","Kgs"),
        ("eSUN ePA12 Filament 1.75mm Black","Kgs"),
        ("eSUN ePA12 Filament 1.75mm White","Kgs"),
        ("Dye Penetrant Spray","Piece"),
        ("Glue Stick 3D","Piece"),
        ("MAX Microfiber Cloth 30x40 cm","Piece"),
        ("Dettol Alcohol Sanitizer","Piece"),
        ("3D Printer Gear Grease Lubricant","Piece")]
    for nm,un in STOCK_SEED:
        try: db.execute("INSERT OR IGNORE INTO stock_items (name,unit) VALUES (?,?)",(nm,un))
        except: pass
    db.commit(); db.close()

def load_sheets_cache():
    global _sheets
    try:
        db=sqlite3.connect(DB_PATH)
        row=db.execute("SELECT data FROM sheets_cache WHERE key='sheets_data'").fetchone()
        if row:
            cached=_json.loads(row[0])
            _sheets={"orders":cached.get("orders",[]),"designs":cached.get("designs",[]),"pendency":cached.get("pendency",[]),"fetched_at":time.time()}
        db.close()
    except Exception as e: print(f"[CACHE] Load error: {e}")

def save_sheets_cache():
    try:
        db=get_db()
        db.execute("INSERT OR REPLACE INTO sheets_cache (key,data,updated_at) VALUES (?,?,?)",
            ("sheets_data",_json.dumps({"orders":_sheets["orders"],"designs":_sheets["designs"],"pendency":_sheets["pendency"]}),
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")))
        db.commit(); db.close()
    except Exception as e: print(f"[CACHE] Save error: {e}")

import hashlib, base64 as _b64
GH_TOKEN=os.environ.get("GH_TOKEN","")
GH_REPO=os.environ.get("GH_BACKUP_REPO","")
GH_PATH="crm_backup.json"
_backup_state={"last":"never","hash":"","error":""}

def _gh_headers(raw=False):
    h={"Authorization":f"Bearer {GH_TOKEN}","X-GitHub-Api-Version":"2022-11-28"}
    h["Accept"]="application/vnd.github.raw+json" if raw else "application/vnd.github+json"
    return h

def collect_backup():
    db=get_db()
    data={"prints":[dict(r) for r in db.execute("SELECT * FROM prints").fetchall()],
          "stock_items":[dict(r) for r in db.execute("SELECT * FROM stock_items").fetchall()],
          "stock_txn":[dict(r) for r in db.execute("SELECT * FROM stock_txn").fetchall()],
          "sheets":{"orders":_sheets["orders"],"designs":_sheets["designs"],"pendency":_sheets["pendency"]}}
    db.close(); return data

def do_backup(force=False):
    global _backup_state
    if not GH_TOKEN or not GH_REPO:
        _backup_state["error"]="GH_TOKEN / GH_BACKUP_REPO env vars not set"; return False
    try:
        data=collect_backup()
        core=_json.dumps(data,sort_keys=True,default=str)
        h=hashlib.md5(core.encode()).hexdigest()
        if h==_backup_state["hash"] and not force:
            return True
        now=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        data["backed_up_at"]=now
        b64=_b64.b64encode(_json.dumps(data,default=str).encode()).decode()
        url=f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
        sha=None
        r=requests.get(url,headers=_gh_headers(),timeout=30)
        if r.status_code==200: sha=r.json().get("sha")
        body={"message":f"CRM auto-backup {now} [skip render]","content":b64}
        if sha: body["sha"]=sha
        r=requests.put(url,headers=_gh_headers(),json=body,timeout=90)
        if r.status_code in (200,201):
            _backup_state={"last":now,"hash":h,"error":""}
            return True
        _backup_state["error"]=f"GitHub API {r.status_code}"; return False
    except Exception as e:
        _backup_state["error"]=str(e); return False

def backup_async():
    threading.Thread(target=do_backup,daemon=True).start()

def restore_from_github():
    if not GH_TOKEN or not GH_REPO: return
    try:
        db=get_db()
        pc=db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
        sc=db.execute("SELECT COUNT(*) FROM stock_txn").fetchone()[0]
        if pc>0 and sc>0: db.close(); return
        url=f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
        r=requests.get(url,headers=_gh_headers(raw=True),timeout=90)
        if r.status_code!=200:
            db.close(); return
        d=_json.loads(r.text)
        if pc==0:
            for p in d.get("prints",[]):
                try:
                    db.execute("""INSERT OR IGNORE INTO prints
                        (task_id,date,part_name,printer,city,material,start_time,end_time,duration_min,material_g,status,device_model,filament_color,ist_done,cost_time,manual_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (p.get("task_id"),p.get("date",""),p.get("part_name",""),p.get("printer",""),p.get("city",""),
                         p.get("material",""),p.get("start_time",""),p.get("end_time",""),p.get("duration_min",0),
                         p.get("material_g",0),p.get("status",""),p.get("device_model",""),p.get("filament_color",""),
                         p.get("ist_done",0),p.get("cost_time",0),p.get("manual_status","")))
                except: pass
        if sc==0:
            for it in d.get("stock_items",[]):
                try: db.execute("INSERT OR REPLACE INTO stock_items (id,name,unit,active,reorder_level) VALUES (?,?,?,?,?)",
                        (it["id"],it["name"],it.get("unit","Kgs"),it.get("active",1),it.get("reorder_level",0)))
                except: pass
            for t in d.get("stock_txn",[]):
                try:
                    db.execute("INSERT INTO stock_txn (id,date,city,item_id,txn_type,qty,note,created_at,txn_time,machine) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (t["id"],t["date"],t["city"],t["item_id"],t["txn_type"],t["qty"],t.get("note",""),t.get("created_at",""),t.get("txn_time",""),t.get("machine","")))
                except: pass
        db.commit(); db.close()
        global _sheets
        sh=d.get("sheets",{})
        if sh.get("orders") or sh.get("designs"):
            _sheets={"orders":sh.get("orders",[]),"designs":sh.get("designs",[]),"pendency":sh.get("pendency",[]),"fetched_at":time.time()}
            save_sheets_cache()
    except Exception as e: print(f"[RESTORE] Error: {e}")

def auto_backup_loop():
    time.sleep(120)
    while True:
        do_backup()
        time.sleep(1800)

def parse_dt(v):
    if not v: return None
    try:
        s=str(v)
        if 'T' in s: return datetime.strptime(s.replace('Z','').split('.')[0],"%Y-%m-%dT%H:%M:%S")
        iv=int(float(s))
        if iv>1e12: iv//=1000
        return datetime.fromtimestamp(iv) if iv>0 else None
    except: return None

def parse_txn_time(v):
    s=str(v or "").strip()
    if not s: return ""
    m=re.match(r'^(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM|am|pm)?$', s)
    if not m: return ""
    hh=int(m.group(1)); mm=m.group(2); ap=(m.group(3) or "").upper()
    if ap=="PM" and hh<12: hh+=12
    if ap=="AM" and hh==12: hh=0
    if hh>23: return ""
    return f"{hh:02d}:{mm}"

def get_material(t):
    try:
        ams=t.get("amsDetailMapping") or []
        if isinstance(ams,list) and ams:
            types={m.get("filamentType") or m.get("sourceColor","") for m in ams if m}
            types=[x for x in types if x]
            if types: return "+".join(sorted(types))
        ft=t.get("filamentType","")
        if ft: return ft
    except: pass
    return "ABS"

def compute_record(t, acc):
    st=parse_dt(t.get("startTime")); et=parse_dt(t.get("endTime"))
    cost=int(t.get("costTime") or 0)
    d_wall = int((et-st).total_seconds()/60) if (st and et and et>st) else 0
    d_cost = cost//60 if (60<=cost<=86400) else 0
    status=STATUS_MAP.get(int(t.get("status") or 0),str(t.get("status","")))
    if status=="Cancelled":
        dur = d_wall if 0 < d_wall <= 1440 else 0
    elif 0 < d_wall <= 1440:
        if d_cost and d_wall > d_cost*1.5 and (d_wall - d_cost) > 120:
            dur = d_cost
        else:
            dur = d_wall
    elif d_cost:
        dur = d_cost
    else:
        dur = 0
    if status in ("Queued","In Process") and et and (datetime.utcnow()-et).total_seconds()>300:
        status="Completed"
    if status=="Queued" and et:
        status="Completed"
    weight_g = float(t.get("weight") or 0)
    if status=="Completed" and d_cost>0 and 0<=d_wall<max(3, int(d_cost*0.1)):
        status="Cancelled"
    if status=="Completed" and d_wall>0 and (weight_g/d_wall)>MAX_PLAUSIBLE_G_PER_MIN:
        status="Cancelled"
    if status=="Cancelled" and d_wall>2 and (weight_g/d_wall)<=MAX_PLAUSIBLE_G_PER_MIN:
        status="Completed"
    st_ist=st.replace(tzinfo=timezone.utc).astimezone(IST) if st else None
    if st_ist and dur>0 and status!="In Process":
        et_ist=st_ist+timedelta(minutes=dur)
    elif et:
        et_ist=et.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        et_ist=None
    printer=t.get("deviceName","Unknown")
    city=acc["city_override"] or PRINTER_CITY.get(printer.strip(),"Unknown")
    fil_color=""
    try:
        ams=t.get("amsDetailMapping") or []
        if isinstance(ams,list) and ams and isinstance(ams[0],dict):
            fil_color=ams[0].get("sourceColor","")[:6]
    except: pass
    return {
        "date": st_ist.strftime("%Y-%m-%d") if st_ist else "",
        "part": t.get("title","Unknown"),
        "printer": printer, "city": city, "material": get_material(t),
        "start": st_ist.strftime("%Y-%m-%d %H:%M") if st_ist else "",
        "end": et_ist.strftime("%Y-%m-%d %H:%M") if et_ist else "",
        "dur": dur, "matg": round(float(t.get("weight") or 0),2),
        "status": status, "model": t.get("deviceModel",""),
        "color": fil_color, "cost": cost,
    }

def dedup_prints(db):
    rows=db.execute("""SELECT id,printer,start_time,end_time,status,duration_min,part_name
                       FROM prints
                       WHERE start_time!='' AND end_time!='' AND LENGTH(end_time)>10
                         AND printer NOT IN ('','Unknown')
                         AND duration_min>0 AND duration_min<=1440
                         AND (manual_status IS NULL OR manual_status='')
                       ORDER BY printer,start_time""").fetchall()
    by_printer=defaultdict(list)
    for r in rows:
        try:
            st=datetime.strptime(r[2],"%Y-%m-%d %H:%M")
            et=datetime.strptime(r[3],"%Y-%m-%d %H:%M")
            if et<=st: continue
            by_printer[r[1]].append({"id":r[0],"st":st,"et":et,"status":r[4],"dur":r[5] or 0,"part":(r[6] or "").strip()})
        except: pass
    rank={"Completed":5,"Failed":4,"Cancelled":3,"In Process":2,"Queued":1}
    to_del=set()
    for printer,jobs in by_printer.items():
        jobs.sort(key=lambda x:x["st"])
        kept=[]
        for j in jobs:
            clash=None
            for k in kept:
                overlap = j["st"]<k["et"] and k["st"]<j["et"]
                if overlap:
                    clash=k; break
            if clash:
                if j["dur"]>clash["dur"] or (j["dur"]==clash["dur"] and rank.get(j["status"],0)>rank.get(clash["status"],0)):
                    to_del.add(clash["id"]); kept.remove(clash); kept.append(j)
                else:
                    to_del.add(j["id"])
            else:
                kept.append(j)
    for d in to_del:
        db.execute("DELETE FROM prints WHERE id=?",(d,))
    db.commit()
    return len(to_del)

def remove_restart_cancels(db):
    """RESTART-CANCEL CLEANUP (all cities):
    Agar kisi Cancelled print ke BAAD (24 hr ke andar) same city + same printer +
    same part ka Completed print mil jata hai, toh wo cancel sirf ek restart tha —
    real cancel nahi. Usse hata do.
    Real cancel = cancel hua aur dobara print NAHI hua — wo record rakha jayega.
    (e.g. Polo AC Vent 16:55 Cancelled 2m + 16:59 Done 9h59m -> cancel wali row delete)"""
    rows=db.execute("""SELECT id,city,printer,TRIM(part_name),start_time,material_g
        FROM prints WHERE status='Cancelled' AND start_time!='' AND LENGTH(start_time)>10
          AND (manual_status IS NULL OR manual_status='')""").fetchall()
    dones=db.execute("""SELECT city,printer,TRIM(part_name),start_time,material_g
        FROM prints WHERE status='Completed' AND start_time!='' AND LENGTH(start_time)>10""").fetchall()
    done_map=defaultdict(list)
    for d in dones:
        done_map[(d[0],d[1],(d[2] or '').lower())].append((d[3],float(d[4] or 0)))
    to_del=[]
    for r in rows:
        key=(r[1],r[2],(r[3] or '').lower())
        try: cst=datetime.strptime(r[4],"%Y-%m-%d %H:%M")
        except: continue
        cg=float(r[5] or 0)
        for dst_s,dg in done_map.get(key,[]):
            try: dst=datetime.strptime(dst_s,"%Y-%m-%d %H:%M")
            except: continue
            gap=(dst-cst).total_seconds()
            if 0<=gap<=86400:  # Done, cancel ke baad 24 hr ke andar
                tol=max(1.0, cg*0.1)  # planned weight ~same (re-slice tolerance)
                if cg==0 or abs(dg-cg)<=tol:
                    to_del.append(r[0]); break
    for i in to_del:
        db.execute("DELETE FROM prints WHERE id=?",(i,))
    db.commit()
    return len(to_del)

def apply_manual_overrides(db):
    """MANUAL RECLASSIFY OVERRIDE:
    Jab operator kisi Cancelled part pe Success / Failed / Time button dabata hai,
    to uski choice `manual_status` column me save hoti hai. Ye function har
    startup_fix ke SABSE END me chalta hai — yani sync aur baaki auto-fix rules
    ke BAAD. Isliye operator ki choice hamesha jeetti hai aur 2-ghante wale
    sync/auto-fix se overwrite nahi hoti.
       Success -> Completed  (success stats me count hoga)
       Failed  -> Failed
       Time    -> Cancelled  (genuine cancel, locked)"""
    db.execute("UPDATE prints SET status=manual_status WHERE manual_status IS NOT NULL AND manual_status!=''")
    db.commit()

def startup_fixes():
    if not os.path.exists(DB_PATH): return
    db=sqlite3.connect(DB_PATH)
    try:
        db.execute("UPDATE prints SET status='Completed' WHERE status IN ('In Process','Printing','Queued') AND end_time IS NOT NULL AND end_time!='' AND LENGTH(end_time)>10")
        db.execute("DELETE FROM prints WHERE status NOT IN ('Completed','Failed','Cancelled')")
        db.execute("UPDATE prints SET city='Bangalore' WHERE printer LIKE 'Bengaluru%' AND city!='Bangalore'")
        db.execute("UPDATE prints SET city='Bangalore' WHERE city IN ('Unknown','Hyderabad') AND printer LIKE '%engaluru%'")
        db.execute("UPDATE prints SET duration_min=0 WHERE duration_min>1440")
        db.execute("UPDATE prints SET status='Cancelled' WHERE status='Failed'")
        db.execute(f"UPDATE prints SET status='Completed' WHERE status='Cancelled' AND duration_min>2 AND (material_g*1.0/duration_min)<={MAX_PLAUSIBLE_G_PER_MIN}")
        db.execute(f"UPDATE prints SET status='Cancelled' WHERE status='Completed' AND duration_min>0 AND (material_g*1.0/duration_min)>{MAX_PLAUSIBLE_G_PER_MIN}")
        db.commit()
        dedup_prints(db)
        remove_restart_cancels(db)
        apply_manual_overrides(db)   # <-- NAYA: operator ki manual choice hamesha last me re-apply
    except Exception as e: print(f"[AUTO-FIX ERROR] {e}")
    finally: db.close()

def fetch_tasks(token):
    s=requests.Session(); s.headers.update({"Authorization":f"Bearer {token}"})
    tasks,offset=[],0
    while True:
        for attempt in range(3):
            try: r=s.get(API_URL,params={"limit":100,"offset":offset},timeout=30); data=r.json(); break
            except:
                if attempt==2: return tasks
                time.sleep(3)
        batch=data.get("hits") or data.get("data") or []
        if not batch: break
        tasks.extend(batch)
        if len(tasks)>=data.get("total",0) or len(batch)<100: break
        offset+=100
    return tasks

def do_sync():
    all_records = []
    for acc in ACCOUNTS:
        tasks=fetch_tasks(acc["token"])
        for t in tasks:
            tid=str(t.get("id",""))
            if not tid: continue
            rec=compute_record(t, acc)
            all_records.append((tid, rec))
    db=get_db()
    existing=set(r[0] for r in db.execute("SELECT task_id FROM prints").fetchall())
    new_count=0; upd_count=0
    for tid, rec in all_records:
        if rec["status"] not in ("Completed","Failed","Cancelled"):
            if tid in existing:
                db.execute("DELETE FROM prints WHERE task_id=?",(tid,))
            continue
        if tid in existing:
            db.execute("""UPDATE prints SET date=?,part_name=?,printer=?,city=?,material=?,
                start_time=?,end_time=?,duration_min=?,material_g=?,status=?,device_model=?,
                filament_color=?,cost_time=?,ist_done=1 WHERE task_id=?""",
                (rec["date"],rec["part"],rec["printer"],rec["city"],rec["material"],
                 rec["start"],rec["end"],rec["dur"],rec["matg"],rec["status"],rec["model"],
                 rec["color"],rec["cost"],tid))
            upd_count+=1
        else:
            db.execute("""INSERT OR IGNORE INTO prints
                (task_id,date,part_name,printer,city,material,start_time,end_time,
                 duration_min,material_g,status,device_model,filament_color,ist_done,cost_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (tid,rec["date"],rec["part"],rec["printer"],rec["city"],rec["material"],
                 rec["start"],rec["end"],rec["dur"],rec["matg"],rec["status"],rec["model"],
                 rec["color"],rec["cost"]))
            existing.add(tid); new_count+=1
    total=db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    db.execute("INSERT INTO sync_log (synced_at,total_records,new_records,note) VALUES (?,?,?,?)",
               (datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),total,new_count,f"upd:{upd_count}"))
    db.commit(); db.close()
    startup_fixes()
    try: sync_stock_sheet()
    except Exception as e: print(f"[STOCK_SHEET] sync error: {e}")
    return new_count

def auto_sync_loop():
    try: do_sync()
    except Exception as e: print(f"[SYNC] Startup error: {e}")
    while True:
        time.sleep(7200)
        try: do_sync()
        except Exception as e: print(f"[SYNC] Error: {e}")

HOURS_SQL = "COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN duration_min ELSE 0 END),0)"
MAT_SQL   = "COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN material_g ELSE 0 END),0)"
CANCEL_HOURS_SQL = "COALESCE(SUM(CASE WHEN status='Cancelled' THEN duration_min ELSE 0 END),0)"
CANCEL_MAT_SQL   = "COALESCE(SUM(CASE WHEN status='Cancelled' THEN material_g ELSE 0 END),0)"

def build_recent_html(rows, today):
    html=""
    for row in rows:
        r=tuple(row)
        r_date=_html.escape(str(r[0] or "")); r_part=_html.escape(str(r[1] or "")); r_city=_html.escape(str(r[3] or ""))
        r_dur=int(r[5] or 0); r_matg=r[6] or 0
        r_status=_html.escape(str(r[7] or "")); r_st=str(r[8] or ""); r_et=str(r[9] or "")
        st_hm=r_st[11:16] if len(r_st)>10 else "-"
        et_hm=r_et[11:16] if len(r_et)>10 else "-"
        cc=CITY_COLOR.get(r_city,"#999")
        dur_str=(f"{r_dur//60}h {r_dur%60}m" if r_dur>=60 else f"{r_dur}m") if r_dur>0 else "-"
        et_cell=et_hm if et_hm!="-" else "-"
        if r_status=="Completed":    badge="<span class='badge b-completed'>&#10003; Done</span>"
        elif r_status=="Failed":     badge="<span class='badge b-failed'>&#10007; Failed</span>"
        elif r_status=="Cancelled":  badge="<span class='badge b-cancelled'>&#8856; Cancelled</span>"
        else:                        badge=f"<span class='badge b-cancelled'>{r_status}</span>"
        html+=(f"<tr><td class='mono'>{r_date}</td>"
               f"<td class='td-part' title='{r_part}'>{r_part}</td>"
               f"<td><span class='b-city' style='background:{cc}'>{r_city}</span></td>"
               f"<td class='mono' style='color:#1d4ed8;font-weight:600'>{st_hm}</td>"
               f"<td class='mono' style='color:#7c3aed;font-weight:600'>{et_cell}</td>"
               f"<td class='mono' style='font-weight:600'>{dur_str}</td>"
               f"<td class='mono'>{r_matg}g</td><td>{badge}</td></tr>")
    return html or "<tr><td colspan='8' style='text-align:center;color:#9ca3af;padding:24px'>No prints yet</td></tr>"

def build_daily_html(db, today):
    dates=db.execute("SELECT DISTINCT date FROM prints WHERE date!='' ORDER BY date DESC LIMIT 30").fetchall()
    html=""; cc={"Pune":"#2196F3","Bangalore":"#9C27B0","Hyderabad":"#FF9800","Delhi":"#43A047"}
    for drow in dates:
        d=str(drow[0]); total_p=0; total_h=0.0; total_m=0; cells=""
        for cn in CITIES:
            r=db.execute(f"SELECT COUNT(*),COALESCE(ROUND({HOURS_SQL}/60.0,1),0),COALESCE(ROUND({MAT_SQL},0),0) FROM prints WHERE date=? AND city=?",(d,cn)).fetchone()
            p,h,m=int(r[0] or 0),float(r[1] or 0),int(r[2] or 0)
            total_p+=p; total_h+=h; total_m+=m
            if p>0: cells+=f"<td class='mono'><span style='color:{cc[cn]};font-weight:600'>{p}</span> <span style='font-size:11px;color:#9ca3af'>({h}h)</span></td>"
            else:   cells+="<td class='mono' style='color:#d1d5db'>-</td>"
        rs=f"style='background:rgba(233,30,99,0.07);font-weight:700'" if d==today else ""
        ds=f"style='color:#e91e63'" if d==today else ""
        dot=" &#9679;" if d==today else ""
        html+=f"<tr {rs}><td class='mono' {ds}>{d}{dot}</td>{cells}<td class='mono' style='font-weight:700'>{total_p}</td><td class='mono' style='font-weight:700'>{round(total_h,1)}h</td><td class='mono'>{total_m}g</td></tr>"
    return html or "<tr><td colspan='8' style='text-align:center;color:#9ca3af;padding:24px'>No data - click Sync Now</td></tr>"

@app.route('/')
def dashboard():
    """CLEAN DASHBOARD (redesigned):
    Sirf 2 tables — dono City + Machine ke breakdown me.
      1) Machine Hours — avg per ACTIVE DAY (Today / Yesterday / Last 7d / This Month / Last Month / All Time)
      2) Production & Success Rate — Total parts, Success, Cancel, Success Rate + avg per active day
    'Active day' = jis din us machine ne actually print kiya (active-status + duration>0).
    Baaki sab faltu cheezein (orders, recent prints, daily summary, snapshot cards) hata di gayi."""
    db=get_db()
    today=date.today()
    today_s=today.strftime("%Y-%m-%d")
    yday_s=(today-timedelta(days=1)).strftime("%Y-%m-%d")
    week_s=(today-timedelta(days=6)).strftime("%Y-%m-%d")
    month_s=today.replace(day=1).strftime("%Y-%m-%d")
    lm_end=today.replace(day=1)-timedelta(days=1)
    lm_start=lm_end.replace(day=1)
    lm_start_s=lm_start.strftime("%Y-%m-%d")
    lm_end_s=lm_end.strftime("%Y-%m-%d")

    ph=",".join("?"*len(CITIES))

    # PRINTER_DISPLAY_ALIAS — kuch machines Bambu app me time ke saath rename
    # hue hain (nickname badla), isliye purane data me alag "printer" naam
    # se store hai. Ye same physical machine hai, sirf naam alag-alag waqt
    # pe tha — isliye dashboard par ek hi row me merge karte hain.
    # Confirmed by Vishal: Bangalore me abhi sirf EK machine hai.
    PRINTER_DISPLAY_ALIAS = {
        ("Bangalore", "3DP-01P-639"):     "Bengaluru 3D Printer",
        ("Bangalore", "P1S_2"):           "Bengaluru 3D Printer",
        ("Bangalore", "Shaik Gaous Spinny"): "Bengaluru 3D Printer",
    }
    def disp_printer(city, printer):
        raw = printer or "Unknown"
        return PRINTER_DISPLAY_ALIAS.get((city, raw), raw)

    grp=db.execute(f"""SELECT city, printer, MAX(device_model) AS model, date,
        COUNT(*) AS total,
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0) AS completed,
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0) AS cancelled,
        COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN duration_min ELSE 0 END),0) AS amin,
        MAX(CASE WHEN status IN ('Completed','In Process','Failed') AND duration_min>0 THEN 1 ELSE 0 END) AS act
        FROM prints WHERE date!='' AND city IN ({ph})
        GROUP BY city, printer, date""", CITIES).fetchall()

    stats=defaultdict(lambda:{"model":"","total":0,"completed":0,"cancelled":0,
        "today_h":0.0,"today_d":0,"yday_h":0.0,"yday_d":0,"week_h":0.0,"week_d":0,
        "month_h":0.0,"month_d":0,"lm_h":0.0,"lm_d":0,"all_h":0.0,"all_d":0})
    for r in grp:
        e=stats[(r["city"], disp_printer(r["city"], r["printer"]))]
        if r["model"] and not e["model"]: e["model"]=r["model"]
        h=float(r["amin"] or 0)/60.0; act=int(r["act"] or 0); d=r["date"]
        e["total"]+=r["total"]; e["completed"]+=r["completed"]; e["cancelled"]+=r["cancelled"]
        e["all_h"]+=h; e["all_d"]+=act
        if d==today_s: e["today_h"]+=h; e["today_d"]+=act
        if d==yday_s:  e["yday_h"]+=h;  e["yday_d"]+=act
        if week_s<=d<=today_s:      e["week_h"]+=h;  e["week_d"]+=act
        if month_s<=d<=today_s:     e["month_h"]+=h; e["month_d"]+=act
        if lm_start_s<=d<=lm_end_s: e["lm_h"]+=h;    e["lm_d"]+=act

    def fmt_tat(mins):
        mins=int(round(mins))
        return f"{mins//60}h {mins%60}m" if mins>=60 else f"{mins}m"

    def av(h,d): return round(h/d,1) if d else 0.0

    hours_rows=[]; prod_rows=[]
    for c in CITIES:
        machs=sorted([k for k in stats if k[0]==c], key=lambda x:x[1])
        for key in machs:
            e=stats[key]; city,printer=key
            hours_rows.append({"city":city,"machine":printer,"model":e["model"],
                "today":av(e["today_h"],e["today_d"]),"yday":av(e["yday_h"],e["yday_d"]),
                "week":av(e["week_h"],e["week_d"]),"month":av(e["month_h"],e["month_d"]),
                "lm":av(e["lm_h"],e["lm_d"]),"alltime":av(e["all_h"],e["all_d"])})
            sr=round(e["completed"]/e["total"]*100,1) if e["total"] else 0
            prod_rows.append({"city":city,"machine":printer,"model":e["model"],
                "total":e["total"],"completed":e["completed"],"cancelled":e["cancelled"],
                "success_rate":sr,"active_days":e["all_d"],
                "avg_parts":round(e["total"]/e["all_d"],1) if e["all_d"] else 0})

    # Avg Part Cost (Rs) + Avg TAT — BLENDED across all materials (per Vishal's
    # call: material-wise rows looked too cluttered). Shown in the same
    # Today/Yesterday/Last 7d/This Month/Last Month/All Time layout as the
    # Hours table above, for a consistent look. Avg TAT is all-time only.
    ct=defaultdict(lambda:{"today_c":0.0,"today_n":0,"yday_c":0.0,"yday_n":0,
        "week_c":0.0,"week_n":0,"month_c":0.0,"month_n":0,
        "lm_c":0.0,"lm_n":0,"all_c":0.0,"all_n":0,"all_dur":0})
    for r in db.execute(f"""SELECT city, printer, material, date,
            COUNT(*) AS cnt, COALESCE(SUM(material_g),0) AS matg,
            COALESCE(SUM(duration_min),0) AS dur
            FROM prints WHERE status='Completed' AND city IN ({ph})
            GROUP BY city, printer, material, date""", CITIES).fetchall():
        key=(r["city"], disp_printer(r["city"], r["printer"]))
        e=ct[key]; cost=part_cost_inr(r["matg"], r["material"]); cnt=r["cnt"]; d=r["date"]
        e["all_c"]+=cost; e["all_n"]+=cnt; e["all_dur"]+=r["dur"]
        if d==today_s: e["today_c"]+=cost; e["today_n"]+=cnt
        if d==yday_s:  e["yday_c"]+=cost;  e["yday_n"]+=cnt
        if week_s<=d<=today_s:      e["week_c"]+=cost;  e["week_n"]+=cnt
        if month_s<=d<=today_s:     e["month_c"]+=cost; e["month_n"]+=cnt
        if lm_start_s<=d<=lm_end_s: e["lm_c"]+=cost;    e["lm_n"]+=cnt

    def avc(c,n): return round(c/n) if n else 0

    cost_rows=[]
    for c in CITIES:
        keys=sorted([k for k in ct if k[0]==c], key=lambda x:x[1])
        for key in keys:
            e=ct[key]; city,printer=key
            cost_rows.append({"city":city,"machine":printer,
                "today":avc(e["today_c"],e["today_n"]),"yday":avc(e["yday_c"],e["yday_n"]),
                "week":avc(e["week_c"],e["week_n"]),"month":avc(e["month_c"],e["month_n"]),
                "lm":avc(e["lm_c"],e["lm_n"]),"alltime":avc(e["all_c"],e["all_n"]),
                "avg_tat":fmt_tat(e["all_dur"]/e["all_n"]) if e["all_n"] else "-"})

    total=db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    last_sync=db.execute("SELECT synced_at,total_records FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    return render_template('dashboard.html', today=today_s, total=total, last_sync=last_sync,
        hours_rows=hours_rows, prod_rows=prod_rows, cost_rows=cost_rows,
        city_color=CITY_COLOR, cities=CITIES)

@app.route('/city/<city>')
def city_page(city):
    if city not in CITIES: return redirect('/')
    db=get_db(); today=date.today().strftime("%Y-%m-%d")
    ov=db.execute(f"""SELECT COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Failed' THEN 1 ELSE 0 END),0),
        {HOURS_SQL}/60.0,{MAT_SQL}/1000.0,
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
        {CANCEL_HOURS_SQL}/60.0,{CANCEL_MAT_SQL}/1000.0
        FROM prints WHERE city=?""",(city,)).fetchone()
    td=db.execute(f"""SELECT COUNT(*),{HOURS_SQL}/60.0,{MAT_SQL},
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
        {CANCEL_HOURS_SQL}/60.0,{CANCEL_MAT_SQL}
        FROM prints WHERE city=? AND date=?""",(city,today)).fetchone()
    active_days=db.execute("SELECT COUNT(DISTINCT date) FROM prints WHERE city=? AND date!='' AND status IN ('Completed','In Process','Failed') AND duration_min>0",(city,)).fetchone()[0] or 0
    active_months=db.execute("SELECT COUNT(DISTINCT substr(date,1,7)) FROM prints WHERE city=? AND date!='' AND status IN ('Completed','In Process','Failed') AND duration_min>0",(city,)).fetchone()[0] or 0
    total_hours=float(ov[3] or 0)
    avg_daily=round(total_hours/active_days,1) if active_days else 0
    avg_monthly=round(total_hours/active_months,1) if active_months else 0
    d30=(date.today()-timedelta(days=30)).strftime("%Y-%m-%d")
    r30=db.execute(f"SELECT COUNT(DISTINCT date),{HOURS_SQL}/60.0 FROM prints WHERE city=? AND date>=? AND status IN ('Completed','In Process','Failed') AND duration_min>0",(city,d30)).fetchone()
    days30=r30[0] or 0; hrs30=float(r30[1] or 0)
    avg_daily_30=round(hrs30/days30,1) if days30 else 0
    avg={"daily":avg_daily,"monthly":avg_monthly,"active_days":active_days,
         "active_months":active_months,"daily_30":avg_daily_30,"days_30":days30}
    rows=db.execute("SELECT date,part_name,printer,material,start_time,end_time,duration_min,material_g,status,device_model,filament_color,task_id FROM prints WHERE city=? ORDER BY date DESC,start_time DESC",(city,)).fetchall()
    db.close()
    return render_template('city.html',city=city,color=CITY_COLOR[city],
        ov=ov,td=td,avg=avg,rows=rows,today=today,city_color=CITY_COLOR,cities=CITIES)

@app.route('/monthly')
def monthly():
    db=get_db()
    rows=db.execute(f"""SELECT substr(date,1,7) as mo,city,COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
        ROUND({HOURS_SQL}/60.0,1),ROUND({MAT_SQL}/1000.0,3),
        ROUND({CANCEL_HOURS_SQL}/60.0,1),ROUND({CANCEL_MAT_SQL}/1000.0,3)
        FROM prints WHERE date!='' GROUP BY mo,city ORDER BY mo DESC,city""").fetchall()
    md=defaultdict(lambda:{c:{"total":0,"done":0,"cancelled":0,"hours":0,"mat_kg":0,"cancel_hours":0,"cancel_mat_kg":0} for c in CITIES})
    for r in rows:
        if r[1] in CITIES: md[r[0]][r[1]]={"total":r[2],"done":r[3],"cancelled":r[4],"hours":r[5],"mat_kg":r[6],"cancel_hours":r[7],"cancel_mat_kg":r[8]}
    months_list=[]
    for mo in sorted(md.keys(),reverse=True):
        cd=md[mo]
        months_list.append((
            mo,cd,
            sum(v["total"] for v in cd.values()),
            sum(v["done"] for v in cd.values()),
            sum(v["cancelled"] for v in cd.values()),
            round(sum(v["hours"] for v in cd.values()),1),
            round(sum(v["mat_kg"] for v in cd.values()),2),
            round(sum(v["cancel_hours"] for v in cd.values()),1),
            round(sum(v["cancel_mat_kg"] for v in cd.values()),2),
        ))
    db.close()
    return render_template('monthly.html',months_list=months_list,city_color=CITY_COLOR,cities=CITIES)

def fil_name(mat,col):
    m=(mat or "").upper(); c=(col or "").lower().strip()
    try:
        rv=int(c[0:2],16); gv=int(c[2:4],16); bv=int(c[4:6],16)
        white=(rv>200 and gv>200 and bv>200)
    except: white=False
    if "ABS" in m: return "eSun ABS+ "+("White" if white else "Black")
    elif "PLA" in m: return "eSUN PLA+ "+("White" if white else "Black")
    elif "TPU" in m: return "eSUN TPU-95A"
    elif "PA" in m: return "eSUN ePA12"
    return mat or "Unknown"

CATEGORY_TO_ITEM = {
    "eSun ABS+ Black":"eSUN ABS+ Filament 1.75mm Black",
    "eSun ABS+ White":"eSUN ABS+ Filament 1.75mm White",
    "eSUN PLA+ Black":"eSUN PLA+ Filament 1.75mm Black",
    "eSUN PLA+ White":"eSUN PLA+ Filament 1.75mm White",
    "eSUN TPU-95A":"eSUN TPU-95A Filament 1.75mm Black",
    "eSUN ePA12":"eSUN ePA12 Filament 1.75mm Black",
}
ITEM_TO_CATEGORY = {v:k for k,v in CATEGORY_TO_ITEM.items()}
ITEM_TO_CATEGORY["eSUN ePA12 Filament 1.75mm White"] = "eSUN ePA12"

STOCK_SHEET_CITY_MAP = {"Bengaluru":"Bangalore","Bangalore":"Bangalore","Pune":"Pune","Delhi":"Delhi","Hyderabad":"Hyderabad"}
STOCK_SHEET_MATERIAL_MAP = {
    "ABS Black (KG)":"eSUN ABS+ Filament 1.75mm Black",
    "ABS White (KG)":"eSUN ABS+ Filament 1.75mm White",
    "PLA Black (KG)":"eSUN PLA+ Filament 1.75mm Black",
    "PLA White (KG)":"eSUN PLA+ Filament 1.75mm White",
    "TPU Black (KG)":"eSUN TPU-95A Filament 1.75mm Black",
    "PA12 Black (KG)":"eSUN ePA12 Filament 1.75mm Black",
    "PA12 White (KG)":"eSUN ePA12 Filament 1.75mm White",
    "Microfiber Towels (pcs)":"MAX Microfiber Cloth 30x40 cm",
    "Dettol Sanitizer (units)":"Dettol Alcohol Sanitizer",
    "Lubricant Tubes (units)":"3D Printer Gear Grease Lubricant",
    "Glue Sticks 3D (units)":"Glue Stick 3D",
}

def _process_stock_rows(rows):
    db = get_db()
    items = {row["name"]: row["id"] for row in db.execute("SELECT id,name FROM stock_items").fetchall()}
    seen = set(r[0] for r in db.execute("SELECT source_id FROM stock_sheet_log").fetchall())
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    added = 0
    for row in rows:
        sid = str(row.get("source_id","")).strip()
        if not sid or sid in seen:
            continue
        city_raw = str(row.get("city","")).strip()
        city = STOCK_SHEET_CITY_MAP.get(city_raw, city_raw)
        if city not in CITIES:
            continue
        mat_raw = str(row.get("material","")).strip()
        item_name = STOCK_SHEET_MATERIAL_MAP.get(mat_raw, mat_raw)
        item_id = items.get(item_name)
        if not item_id:
            unit = "Kgs" if "(KG)" in mat_raw.upper() else "Piece"
            db.execute("INSERT OR IGNORE INTO stock_items (name,unit) VALUES (?,?)",(item_name,unit))
            got = db.execute("SELECT id FROM stock_items WHERE name=?",(item_name,)).fetchone()
            if not got:
                continue
            item_id = got[0]
            items[item_name] = item_id
        try: qty = float(row.get("qty") or 0)
        except: qty = 0
        if qty<=0:
            continue
        direction = str(row.get("direction","")).strip().upper()
        entered_by = str(row.get("entered_by","")).strip()
        # NAYA: machine field ("3DP Remarks" column se aata hai) — normalize
        # karke store karte hain (P1S, X1 CARBON, etc.). Sirf ISSUE (OUT)
        # entries ke liye meaningful hai — kis machine ke against material
        # OUT kiya gaya.
        machine_raw = str(row.get("machine","")).strip()
        machine = normalize_machine(machine_raw)
        if direction=="IN":
            txn_type = "OPENING" if "opening" in entered_by.lower() else "PURCHASE"
        elif direction=="OUT":
            txn_type = "ISSUE"
        else:
            continue
        d = str(row.get("date","")).strip() or date.today().strftime("%Y-%m-%d")
        tt = parse_txn_time(row.get("time",""))
        db.execute("INSERT INTO stock_txn (date,city,item_id,txn_type,qty,note,created_at,txn_time,machine) VALUES (?,?,?,?,?,?,?,?,?)",
            (d, city, item_id, txn_type, qty, f"From Sheet ({entered_by or 'manual'})", now_str, tt, machine))
        db.execute("INSERT OR IGNORE INTO stock_sheet_log (source_id,synced_at) VALUES (?,?)",(sid,now_str))
        seen.add(sid)
        added += 1
    db.commit(); db.close()
    if added: backup_async()
    return added

def sync_stock_sheet():
    if not STOCK_SHEET_URL: return 0
    try:
        r = requests.get(STOCK_SHEET_URL, timeout=20)
        payload = r.json()
        rows = payload.get("rows", []) if isinstance(payload, dict) else payload
    except Exception as e:
        print(f"[STOCK_SHEET] fetch error: {e}")
        return 0
    return _process_stock_rows(rows)

@app.route('/materials')
def materials():
    db=get_db()
    FILS=["eSun ABS+ Black","eSun ABS+ White","eSUN PLA+ Black","eSUN PLA+ White","eSUN TPU-95A","eSUN ePA12"]

    raw=db.execute("""SELECT material,filament_color,COUNT(*),
        COALESCE(SUM(material_g),0)/1000.0,
        COALESCE(SUM(CASE WHEN status='Failed' THEN 1 ELSE 0 END),0)
        FROM prints WHERE status IN ('Completed','Failed') GROUP BY material,filament_color ORDER BY 4 DESC""").fetchall()
    fil_total={}
    for fn in FILS: fil_total[fn]={"parts":0,"kg":0.0,"failed":0}
    fil_total["Other"]={"parts":0,"kg":0.0,"failed":0}
    for r in raw:
        fn=fil_name(r[0],r[1])
        if fn not in fil_total: fn="Other"
        fil_total[fn]["parts"]+=r[2]; fil_total[fn]["kg"]+=round(float(r[3]),3); fil_total[fn]["failed"]+=r[4]
    top=[(k,v) for k,v in fil_total.items() if v["kg"]>0]
    top.sort(key=lambda x:x[1]["kg"],reverse=True)

    mraw=db.execute("""SELECT substr(date,1,7) as mo,city,material,filament_color,
        COUNT(*),COALESCE(SUM(material_g),0)/1000.0,
        COALESCE(SUM(CASE WHEN status='Failed' THEN 1 ELSE 0 END),0)
        FROM prints WHERE date!='' AND status IN ('Completed','Failed')
        GROUP BY mo,city,material,filament_color ORDER BY 1 DESC,2,3""").fetchall()

    months_set=set(); _mdata={}
    for r in mraw:
        mo,city,mat,col=r[0],r[1],r[2],r[3]
        fn=fil_name(mat,col)
        if fn not in FILS: fn="Other"
        months_set.add(mo)
        if mo not in _mdata: _mdata[mo]={}
        if city not in _mdata[mo]: _mdata[mo][city]={}
        if fn not in _mdata[mo][city]: _mdata[mo][city][fn]={"parts":0,"kg":0.0,"failed":0}
        _mdata[mo][city][fn]["parts"]+=r[4]
        _mdata[mo][city][fn]["kg"]+=round(float(r[5]),3)
        _mdata[mo][city][fn]["failed"]+=r[6]

    months_list=sorted(months_set,reverse=True)[:6]
    sel_mo=request.args.get("mo",months_list[0] if months_list else "")

    draw=db.execute("""SELECT date,city,COALESCE(SUM(material_g),0)/1000.0
        FROM prints WHERE date!='' AND substr(date,1,7)=? AND status IN ('Completed','Failed')
        GROUP BY date,city""",(sel_mo,)).fetchall()
    _ddata={}
    for r in draw:
        d,city,kg=r[0],r[1],round(float(r[2]),3)
        if city not in CITIES: continue
        if d not in _ddata: _ddata[d]={c:0.0 for c in CITIES}
        _ddata[d][city]=kg
    daily_list=[]
    for d in sorted(_ddata.keys(),reverse=True):
        row=_ddata[d]
        tot=round(sum(row.values()),3)
        daily_list.append((d,row,tot))

    db.close()
    return render_template('materials.html',top=top,mdata=_mdata,months_list=months_list,
        sel_mo=sel_mo,filaments=FILS,city_color=CITY_COLOR,cities=CITIES,
        daily_list=daily_list)

@app.route('/fails')
def fails():
    db=get_db()
    rows=db.execute("SELECT date,part_name,printer,city,material,duration_min,material_g,status FROM prints WHERE status IN ('Failed','Cancelled') ORDER BY date DESC").fetchall()
    top=db.execute("SELECT part_name,COUNT(*),city FROM prints WHERE status IN ('Failed','Cancelled') GROUP BY part_name ORDER BY 2 DESC LIMIT 20").fetchall()
    db.close()
    return render_template('fails.html',rows=rows,top=top,city_color=CITY_COLOR,cities=CITIES)

@app.route('/orders')
def orders():
    data=fetch_sheets(force=False); all_orders=data.get("orders",[])
    cf=request.args.get("city","All"); sf=request.args.get("status","All")
    filtered=all_orders
    if cf!="All": filtered=[o for o in filtered if o.get("order_city")==cf or o.get("source_city")==cf]
    if sf!="All": filtered=[o for o in filtered if sf.lower() in o.get("status","").lower()]
    filtered=sorted(filtered,key=lambda x:x.get("date",""),reverse=True)
    city_stats={}
    for c in CITIES:
        co=[o for o in all_orders if o.get("order_city")==c]
        city_stats[c]={"total":len(co),"fulfilled":len([o for o in co if "fulfilled" in o.get("status","").lower()]),"pending":len([o for o in co if o.get("status","").lower() not in ["fulfilled","cancelled",""]]),"color":CITY_COLOR[c]}
    _daily=defaultdict(lambda:{c:0 for c in CITIES})
    for o in all_orders:
        d=o.get("date",""); oc=o.get("order_city","")
        if d and oc in CITIES: _daily[d][oc]+=1
    daily_summary=sorted(_daily.items(),key=lambda x:x[0],reverse=True)[:30]
    today_date=date.today().strftime("%Y-%m-%d")
    statuses=sorted(set(o.get("status","") for o in all_orders if o.get("status","")))
    return render_template('orders.html',orders=filtered,city_stats=city_stats,city_filter=cf,
        status_filter=sf,statuses=statuses,city_color=CITY_COLOR,cities=CITIES,
        total=len(all_orders),daily_summary=daily_summary,today_date=today_date)

@app.route('/designs')
def designs():
    data=fetch_sheets(force=False); all_designs=data.get("designs",[])
    today_str=date.today().strftime("%Y-%m-%d")
    all_designs=sorted(all_designs,key=lambda x:x.get("printed_date","") or x.get("design_date",""),reverse=True)
    today_list=[d for d in all_designs if d.get("printed_date","")==today_str]
    filter_today=request.args.get("filter","")
    filter_date=request.args.get("date","")
    if filter_today=="today":
        show_designs=today_list
    elif filter_date:
        show_designs=[d for d in all_designs if d.get("printed_date","")==filter_date]
    else:
        show_designs=all_designs
    _daily=defaultdict(lambda:{c:0 for c in CITIES})
    for d in all_designs:
        dd=d.get("printed_date",""); dc=d.get("city","")
        if dd and dd<=today_str and dc in CITIES: _daily[dd][dc]+=1
    daily_summary=sorted(_daily.items(),key=lambda x:x[0],reverse=True)[:30]
    return render_template('designs.html',designs=show_designs,all_count=len(all_designs),
        city_color=CITY_COLOR,cities=CITIES,today_count=len(today_list),
        filter_today=filter_today,filter_date=filter_date,today_str=today_str,
        daily_summary=daily_summary)

def _fmt_qty(v):
    v=float(v or 0)
    return str(int(v)) if v==int(v) else f"{v:g}"

@app.route('/stock')
def stock_page():
    db=get_db(); cf=request.args.get("city","All")
    items=db.execute("SELECT * FROM stock_items WHERE active=1 ORDER BY unit DESC,id").fetchall()
    txns=db.execute("SELECT t.*,i.name AS iname,i.unit AS iunit FROM stock_txn t JOIN stock_items i ON i.id=t.item_id ORDER BY t.id DESC").fetchall()
    agg=defaultdict(lambda:{"OPENING":0.0,"PURCHASE":0.0,"ISSUE":0.0})
    for t in txns: agg[(t["city"],t["item_id"])][t["txn_type"]]+=float(t["qty"] or 0)
    def cur(city,iid):
        a=agg[(city,iid)]; return a["OPENING"]+a["PURCHASE"]-a["ISSUE"]
    def has_any(city,iid):
        a=agg[(city,iid)]; return (a["OPENING"] or a["PURCHASE"] or a["ISSUE"]) > 0
    def has_any_all(iid):
        return any(has_any(c,iid) for c in CITIES)
    if cf=="All":
        head="".join(f"<th style='text-align:center;padding:11px 8px;color:{CITY_COLOR[c]};font-weight:800;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>{c}</th>" for c in CITIES)
        rows=""
        for it in items:
            if not has_any_all(it["id"]):
                continue
            rl=float(it["reorder_level"] or 0)
            tot=sum(cur(c,it["id"]) for c in CITIES)
            cells=""
            for c in CITIES:
                v=cur(c,it["id"])
                present=has_any(c,it["id"])
                if not present:
                    cells+="<td style='text-align:center;padding:9px 8px;color:#d1d5db'>—</td>"
                    continue
                if v<=0: bg="#fee2e2"; col="#dc2626"; warn="⚠ "
                elif rl>0 and v<=rl: bg="#fef3c7"; col="#b45309"; warn="⚠ "
                else: bg="#f3f4f6"; col="#111827"; warn=""
                cells+=f"<td style='text-align:center;padding:8px 6px'><span style='display:inline-block;min-width:44px;padding:3px 8px;border-radius:20px;background:{bg};font-weight:700;color:{col};font-size:12px'>{warn}{_fmt_qty(v)}</span></td>"
            rows+=(f"<tr style='border-bottom:1px solid #f3f4f6'><td style='padding:10px 14px;font-weight:600;color:#111827'>{_html.escape(it['name'])}</td>"
                f"<td style='padding:10px 8px;color:#9ca3af;font-size:12px'>{it['unit']}</td>{cells}"
                f"<td style='text-align:center;padding:10px 8px;font-weight:800;color:var(--pink)'>{_fmt_qty(tot)}</td></tr>")
        summary_html=(f"<table style='width:100%;border-collapse:collapse;font-size:13px'><thead><tr style='background:linear-gradient(180deg,#fafbfc,#f3f4f6)'>"
            f"<th style='text-align:left;padding:11px 14px;color:#6b7280;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>Material</th>"
            f"<th style='text-align:left;padding:11px 8px;color:#6b7280;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>Unit</th>{head}"
            f"<th style='text-align:center;padding:11px 8px;color:#6b7280;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>Total</th></tr></thead><tbody>{rows}</tbody></table>")
    else:
        rows=""
        for it in items:
            if not has_any(cf,it["id"]):
                continue
            rl=float(it["reorder_level"] or 0)
            a=agg[(cf,it["id"])]; v=a["OPENING"]+a["PURCHASE"]-a["ISSUE"]
            if v<=0: bg="#fee2e2"; col="#dc2626"; warn="⚠ "
            elif rl>0 and v<=rl: bg="#fef3c7"; col="#b45309"; warn="⚠ "
            else: bg="#f3f4f6"; col="#111827"; warn=""
            rows+=(f"<tr style='border-bottom:1px solid #f3f4f6'><td style='padding:10px 14px;font-weight:600;color:#111827'>{_html.escape(it['name'])}</td>"
                f"<td style='padding:10px 8px;color:#9ca3af;font-size:12px'>{it['unit']}</td>"
                f"<td style='text-align:center;padding:10px 8px'>{_fmt_qty(a['OPENING'])}</td>"
                f"<td style='text-align:center;padding:10px 8px;color:#16a34a;font-weight:600'>+{_fmt_qty(a['PURCHASE'])}</td>"
                f"<td style='text-align:center;padding:10px 8px;color:#dc2626;font-weight:600'>−{_fmt_qty(a['ISSUE'])}</td>"
                f"<td style='text-align:center;padding:8px 8px'><span style='display:inline-block;min-width:50px;padding:3px 10px;border-radius:20px;background:{bg};font-weight:800;color:{col};font-size:13px'>{warn}{_fmt_qty(v)}</span></td></tr>")
        summary_html=(f"<table style='width:100%;border-collapse:collapse;font-size:13px'><thead><tr style='background:linear-gradient(180deg,#fafbfc,#f3f4f6)'>"
            f"<th style='text-align:left;padding:11px 14px;color:#6b7280;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>Material</th>"
            f"<th style='text-align:left;padding:11px 8px;color:#6b7280;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>Unit</th>"
            f"<th style='text-align:center;padding:11px 8px;color:#6b7280;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>Opening</th>"
            f"<th style='text-align:center;padding:11px 8px;color:#6b7280;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>Purchased</th>"
            f"<th style='text-align:center;padding:11px 8px;color:#6b7280;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>Issued/Used</th>"
            f"<th style='text-align:center;padding:11px 8px;color:#6b7280;border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;letter-spacing:.03em'>Current Stock</th></tr></thead><tbody>{rows}</tbody></table>")
    tabs=""
    for c in ["All"]+CITIES:
        on=(c==cf)
        tabs+=(f"<a href='/stock?city={c}' style='text-decoration:none'><div style='padding:8px 18px;border-radius:8px;font-size:13px;font-weight:600;"
            f"background:{'#e91e63' if on else '#fff'};color:{'#fff' if on else '#374151'};border:2px solid {'#e91e63' if on else '#e5e7eb'}'>{c}</div></a>")
    show=[t for t in txns if cf=="All" or t["city"]==cf][:50]
    TYPE_BADGE={"OPENING":("🏁 Opening","#6366f1"),"PURCHASE":("🛒 Purchase","#16a34a"),"ISSUE":("📤 Issue","#dc2626")}
    log_rows=""
    for t in show:
        lb,tc=TYPE_BADGE.get(t["txn_type"],(t["txn_type"],"#6b7280"))
        sign="−" if t["txn_type"]=="ISSUE" else "+"
        try: t_time=t["txn_time"] or ""
        except: t_time=""
        # NAYA: machine badge bhi dikhado agar hai
        try: t_machine=t["machine"] or ""
        except: t_machine=""
        machine_badge=f" <span style='background:#eef2ff;color:#4338ca;padding:1px 6px;border-radius:8px;font-size:10px;font-weight:700'>🖨 {_html.escape(t_machine)}</span>" if t_machine else ""
        date_cell=f"{t['date']}" + (f" <span style='color:#9ca3af;font-size:11px'>{t_time}</span>" if t_time else "")
        log_rows+=(f"<tr style='border-bottom:1px solid #f3f4f6'>"
            f"<td style='padding:8px 12px;white-space:nowrap;color:#6b7280'>{date_cell}</td>"
            f"<td style='padding:8px 10px'><span style='color:{CITY_COLOR.get(t['city'],'#6b7280')};font-weight:600'>{t['city']}</span></td>"
            f"<td style='padding:8px 10px'>{_html.escape(t['iname'])}</td>"
            f"<td style='padding:8px 10px'>{lb}{machine_badge}</td>"
            f"<td style='padding:8px 10px;text-align:center;font-weight:700;color:{tc}'>{sign}{_fmt_qty(t['qty'])} {t['iunit']}</td>"
            f"<td style='padding:8px 10px;color:#6b7280'>{_html.escape(t['note'] or '')}</td>"
            f"<td style='padding:8px 10px;text-align:center'><button onclick='delTxn({t['id']})' style='background:none;border:none;cursor:pointer;color:#dc2626;font-size:14px' title='Delete entry'>🗑</button></td></tr>")
    if not log_rows: log_rows="<tr><td colspan='7' style='padding:20px;text-align:center;color:#9ca3af'>No entries yet</td></tr>"
    item_options="".join(f"<option value='{it['id']}'>{_html.escape(it['name'])} ({it['unit']})</option>" for it in items)
    city_options="".join(f"<option value='{c}'>{c}</option>" for c in CITIES)
    reorder_rows=""
    db.close()
    return render_template('stock.html',summary_html=summary_html,tabs_html=tabs,
        log_html=log_rows,item_options=item_options,city_options=city_options,
        reorder_html=reorder_rows,
        city_filter=cf,today=date.today().strftime("%Y-%m-%d"),txn_count=len(txns),
        backup_last=_backup_state["last"],backup_on=bool(GH_TOKEN and GH_REPO))

def compute_wastage():
    """
    Har Issue (OUT) entry ke against actual material use nikalta hai.
    FIXED: Ab agar Issue entry mein machine tag hai (P1S / X1 CARBON),
    to us Issue ke period mein sirf USI machine ke prints count hote
    hain — dusri machine ka material is bucket mein mix nahi hota.
    Agar Issue mein machine tag nahi hai (purani entries / manually
    add ki gayi), to purana city-wide behavior chalta hai (fallback),
    taaki purana data crash na ho.
    """
    db=get_db()
    now_dt=datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    items=db.execute("SELECT * FROM stock_items WHERE unit='Kgs' AND active=1").fetchall()
    out=[]
    for it in items:
        cat=ITEM_TO_CATEGORY.get(it["name"])
        if not cat: continue
        for c in CITIES:
            issues=db.execute("""SELECT date,qty,COALESCE(txn_time,'') AS tt, COALESCE(machine,'') AS mc
                FROM stock_txn WHERE city=? AND item_id=? AND txn_type='ISSUE'
                ORDER BY date ASC, tt ASC, id ASC""",(c,it["id"])).fetchall()

            # Issue entries ko machine ke hisab se alag-alag timeline mein bant do.
            # Jinke paas machine tag nahi hai unko "" (city-wide/unknown) bucket mein.
            by_bucket = defaultdict(list)
            for row in issues:
                t=(row["tt"] or "").strip() or "00:00"
                dt=f"{row['date']} {t}"
                q=float(row["qty"] or 0)
                mc = row["mc"] or ""
                bucket = by_bucket[mc]
                if bucket and bucket[-1][0]==dt:
                    bucket[-1][1]+=q
                else:
                    bucket.append([dt,q])

            for mc, points in by_bucket.items():
                for i,(start_dt,qv) in enumerate(points):
                    is_last=(i+1==len(points))
                    end_dt = points[i+1][0] if not is_last else "9999-12-31 23:59"
                    period_end_display = points[i+1][0] if not is_last else now_dt

                    if mc:
                        # Machine-specific Issue — sirf usi machine ke prints match karo
                        prints=db.execute("""SELECT material,filament_color,material_g,status,duration_min,printer
                            FROM prints WHERE city=? AND start_time>=? AND start_time<?
                            AND status IN ('Completed','Failed','Cancelled')""",(c,start_dt,end_dt)).fetchall()
                        prints = [p for p in prints if machine_of_printer(p["printer"])==mc]
                    else:
                        # Machine tag nahi hai (purani entry) — sabhi machines count (purana behavior)
                        prints=db.execute("""SELECT material,filament_color,material_g,status,duration_min,printer
                            FROM prints WHERE city=? AND start_time>=? AND start_time<?
                            AND status IN ('Completed','Failed','Cancelled')""",(c,start_dt,end_dt)).fetchall()

                    actual_g=0.0
                    by_machine=defaultdict(float)
                    for p in prints:
                        if fil_name(p["material"],p["filament_color"])!=cat: continue
                        pm = machine_of_printer(p["printer"])
                        if p["status"]=="Cancelled":
                            g = round(float(p["duration_min"] or 0)*CANCEL_FLOW_G_PER_MIN,1)
                        else:
                            g = float(p["material_g"] or 0)
                        actual_g += g
                        by_machine[pm] += g

                    issued_g=qv*1000
                    diff_g=issued_g-actual_g
                    pct=(diff_g/issued_g*100) if issued_g>0 else 0
                    out.append({"city":c,"material":it["name"],"machine":mc or "All machines (no tag)",
                        "date":start_dt,"period_end":period_end_display,
                        "issued_g":round(issued_g,1),"actual_g":round(actual_g,1),
                        "diff_g":round(diff_g,1),"pct":round(pct,1),
                        "status":"ongoing" if is_last else "closed",
                        "by_machine":{k:round(v,1) for k,v in by_machine.items()}})
    db.close()
    out.sort(key=lambda x:x["date"],reverse=True)
    return out

def build_wastage_html(rows):
    if not rows:
        return "<tr><td colspan='9' style='padding:24px;text-align:center;color:#9ca3af'>No Issue entries found yet — add an Issue entry on the Daily Materials page first, then wastage will be calculated</td></tr>"
    html=""
    for r in rows:
        row_bg=""
        if r["status"]=="ongoing":
            status_badge="<span style='background:#dbeafe;color:#1e40af;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600'>🔄 Ongoing</span>"
            label="Remaining"
            col="#6b7280"
        else:
            status_badge="<span style='background:#f3f4f6;color:#374151;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600'>✅ Closed</span>"
            label="Wastage"
            if r["pct"]<0:
                col="#7c3aed"; row_bg="background:#f5f3ff;"
            elif r["pct"]<=10: col="#16a34a"
            elif r["pct"]<=25: col="#d97706"
            else: col="#dc2626"; row_bg="background:#fef2f2;"
        flag = "<div style='font-size:10px;color:#7c3aed;font-weight:700;margin-top:2px'>⚠ Issue entry likely missing</div>" if r["pct"]<0 else ""

        by_machine = r.get("by_machine", {})
        machine_html = ""
        if len(by_machine) > 1:
            parts = ", ".join(f"{_html.escape(m)}: {g}g" for m, g in sorted(by_machine.items(), key=lambda x:-x[1]))
            machine_html = f"<div style='font-size:10px;color:#6b7280;margin-top:3px'>🖨 {parts}</div>"

        machine_col = f"<span style='background:#eef2ff;color:#4338ca;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:700'>{_html.escape(r['machine'])}</span>"

        html+=(f"<tr style='border-bottom:1px solid #f3f4f6;{row_bg}'>"
            f"<td style='padding:8px 12px;white-space:nowrap;color:#6b7280;font-size:12px'>{r['date']} → {r['period_end']}</td>"
            f"<td style='padding:8px 10px'>{status_badge}</td>"
            f"<td style='padding:8px 10px'><span style='color:{CITY_COLOR.get(r['city'],'#6b7280')};font-weight:600'>{r['city']}</span></td>"
            f"<td style='padding:8px 10px'>{machine_col}</td>"
            f"<td style='padding:8px 10px'>{_html.escape(r['material'])}</td>"
            f"<td style='padding:8px 10px;text-align:center'>{r['issued_g']}g</td>"
            f"<td style='padding:8px 10px;text-align:center'>{r['actual_g']}g{machine_html}</td>"
            f"<td style='padding:8px 10px;text-align:center;font-weight:600;color:{col}'>{label}: {r['diff_g']}g{flag}</td>"
            f"<td style='padding:8px 10px;text-align:center;font-weight:700;color:{col}'>{r['pct']}%</td></tr>")
    return html

def build_alert_html(rows):
    problems = [r for r in rows if r["pct"] < 0]
    if not problems:
        return ""
    items = ""
    for r in problems:
        shortfall = abs(r["diff_g"])
        items += (f"<li style='margin-bottom:6px'>"
            f"<b style='color:{CITY_COLOR.get(r['city'],'#6b7280')}'>{r['city']}</b> ({r['machine']}) — {_html.escape(r['material'])} "
            f"({r['date']} → {r['period_end']}): used <b>{r['actual_g']}g</b> but only <b>{r['issued_g']}g</b> was issued "
            f"— <b style='color:#7c3aed'>~{shortfall:.0f}g not logged</b>. Add a missing Issue entry on Daily Materials to fix.</li>")
    return (f"<div style='background:linear-gradient(135deg,#fef2f2,#fee2e2);border:1.5px solid #fca5a5;"
        f"border-radius:12px;padding:16px 20px;margin-bottom:18px'>"
        f"<div style='font-size:13.5px;font-weight:800;color:#991b1b;margin-bottom:8px'>"
        f"🚨 {len(problems)} spool(s) show more material used than issued — an Issue entry is likely missing</div>"
        f"<ul style='margin:0 0 0 18px;font-size:12.5px;color:#7f1d1d;line-height:1.5'>{items}</ul></div>")

@app.route('/wastage')
def wastage_page():
    rows=compute_wastage()
    wastage_html=build_wastage_html(rows)
    alert_html=build_alert_html(rows)
    return render_template('wastage.html',wastage_html=wastage_html,alert_html=alert_html,city_color=CITY_COLOR,cities=CITIES)

def _date_presets():
    today = date.today()
    yesterday = today - timedelta(days=1)
    this_month_start = today.replace(day=1)
    last_month_end = this_month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    return {
        "today":      (today, today),
        "yesterday":  (yesterday, yesterday),
        "7d":         (today - timedelta(days=6), today),
        "30d":        (today - timedelta(days=29), today),
        "this_month": (this_month_start, today),
        "last_month": (last_month_start, last_month_end),
    }

ACTIVE_STATUS_SQL = "status IN ('Completed','In Process','Failed') AND duration_min>0"

@app.route('/analytics')
def analytics():
    presets = {k: (v[0].strftime("%Y-%m-%d"), v[1].strftime("%Y-%m-%d")) for k, v in _date_presets().items()}
    from_date = request.args.get('from', '').strip()
    to_date   = request.args.get('to', '').strip()
    if not from_date or not to_date:
        from_date, to_date = presets["7d"]

    db = get_db()

    overall = db.execute(f"""SELECT COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Failed' THEN 1 ELSE 0 END),0),
        {HOURS_SQL}/60.0, {MAT_SQL}/1000.0
        FROM prints WHERE date>=? AND date<=?""", (from_date, to_date)).fetchone()

    total_jobs = overall[0] or 0
    completed  = overall[1] or 0
    cancelled  = overall[2] or 0
    failed     = overall[3] or 0
    total_hours  = round(float(overall[4] or 0), 1)
    total_mat_kg = round(float(overall[5] or 0), 2)
    success_rate = round((completed / total_jobs * 100), 1) if total_jobs else 0

    try:
        d_from_obj = datetime.strptime(from_date, "%Y-%m-%d").date()
        d_to_obj   = datetime.strptime(to_date, "%Y-%m-%d").date()
        days_in_range = max((d_to_obj - d_from_obj).days + 1, 1)
    except Exception:
        days_in_range = 1

    active_days_overall = db.execute(f"""SELECT COUNT(DISTINCT date) FROM prints
        WHERE date>=? AND date<=? AND {ACTIVE_STATUS_SQL}""", (from_date, to_date)).fetchone()[0] or 0

    avg_jobs_day  = round(total_jobs / active_days_overall, 1) if active_days_overall else 0
    avg_hours_day = round(total_hours / active_days_overall, 2) if active_days_overall else 0
    avg_mat_day   = round(total_mat_kg / active_days_overall, 2) if active_days_overall else 0

    avg_jobs_per_hour = round(total_jobs / total_hours, 2) if total_hours else 0
    avg_mat_g_per_hour = round((total_mat_kg * 1000) / total_hours, 1) if total_hours else 0

    machine_active_rows = db.execute(f"""SELECT printer, city, COUNT(DISTINCT date) FROM prints
        WHERE date>=? AND date<=? AND {ACTIVE_STATUS_SQL}
        GROUP BY printer, city""", (from_date, to_date)).fetchall()
    machine_active_map = {(r[0] or "Unknown", r[1] or "Unknown"): r[2] for r in machine_active_rows}

    city_active_rows = db.execute(f"""SELECT city, COUNT(DISTINCT date) FROM prints
        WHERE date>=? AND date<=? AND {ACTIVE_STATUS_SQL}
        GROUP BY city""", (from_date, to_date)).fetchall()
    city_active_map = {r[0]: r[1] for r in city_active_rows}

    machine_rows = db.execute(f"""SELECT printer, city, COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Failed' THEN 1 ELSE 0 END),0),
        {HOURS_SQL}/60.0, {MAT_SQL}
        FROM prints WHERE date>=? AND date<=?
        GROUP BY printer, city ORDER BY city, printer""", (from_date, to_date)).fetchall()

    machines = []
    for r in machine_rows:
        tot, comp, canc, fail = r[2] or 0, r[3] or 0, r[4] or 0, r[5] or 0
        hrs  = round(float(r[6] or 0), 1)
        matg = round(float(r[7] or 0), 1)
        sr = round((comp / tot * 100), 1) if tot else 0
        active_days = machine_active_map.get((r[0] or "Unknown", r[1] or "Unknown"), 0)
        machines.append({
            "printer": r[0] or "Unknown", "city": r[1] or "Unknown",
            "total": tot, "completed": comp, "cancelled": canc, "failed": fail,
            "hours": hrs, "material_g": matg, "success_rate": sr,
            "active_days": active_days,
            "avg_hours_day": round(hrs / active_days, 2) if active_days else 0,
            "mat_g_per_hour": round(matg / hrs, 1) if hrs else 0,
        })

    city_rows = db.execute(f"""SELECT city, COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Failed' THEN 1 ELSE 0 END),0),
        {HOURS_SQL}/60.0, {MAT_SQL}/1000.0
        FROM prints WHERE date>=? AND date<=?
        GROUP BY city ORDER BY city""", (from_date, to_date)).fetchall()

    city_stats = []
    for r in city_rows:
        tot, comp, canc, fail = r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0
        hrs   = round(float(r[5] or 0), 1)
        matkg = round(float(r[6] or 0), 2)
        sr = round((comp / tot * 100), 1) if tot else 0
        active_days = city_active_map.get(r[0], 0)
        city_stats.append({
            "city": r[0] or "Unknown", "total": tot, "completed": comp,
            "cancelled": canc, "failed": fail, "hours": hrs,
            "material_kg": matkg, "success_rate": sr,
            "color": CITY_COLOR.get(r[0], "#999"),
            "active_days": active_days,
            "avg_hours_day": round(hrs / active_days, 2) if active_days else 0,
            "mat_g_per_hour": round((matkg * 1000) / hrs, 1) if hrs else 0,
        })

    mat_rows = db.execute("""SELECT city, material, filament_color, COUNT(*),
        COALESCE(SUM(material_g),0)
        FROM prints WHERE date>=? AND date<=? AND status IN ('Completed','Failed')
        GROUP BY city, material, filament_color""", (from_date, to_date)).fetchall()

    mat_agg = defaultdict(lambda: {"parts": 0, "kg": 0.0, "by_city": defaultdict(float)})
    for r in mat_rows:
        city, mat, col, cnt, matg = r[0], r[1], r[2], r[3], r[4]
        fn = fil_name(mat, col)
        mat_agg[fn]["parts"] += cnt
        mat_agg[fn]["kg"] += round(float(matg) / 1000.0, 3)
        mat_agg[fn]["by_city"][city] += round(float(matg) / 1000.0, 3)

    max_mat_kg = max([v["kg"] for v in mat_agg.values()] or [1])
    materials_list = []
    for fn, v in sorted(mat_agg.items(), key=lambda x: x[1]["kg"], reverse=True):
        materials_list.append({
            "name": fn, "parts": v["parts"], "kg": round(v["kg"], 2),
            "pct_of_max": round((v["kg"] / max_mat_kg) * 100, 1) if max_mat_kg else 0,
            "by_city": {c: round(v["by_city"].get(c, 0), 2) for c in CITIES},
        })

    db.close()

    return render_template('analytics.html',
        from_date=from_date, to_date=to_date, days_in_range=days_in_range,
        active_days_overall=active_days_overall, presets=presets,
        total_jobs=total_jobs, completed=completed, cancelled=cancelled, failed=failed,
        total_hours=total_hours, total_mat_kg=total_mat_kg, success_rate=success_rate,
        avg_jobs_day=avg_jobs_day, avg_hours_day=avg_hours_day, avg_mat_day=avg_mat_day,
        avg_jobs_per_hour=avg_jobs_per_hour, avg_mat_g_per_hour=avg_mat_g_per_hour,
        machines=machines, city_stats=city_stats, materials_list=materials_list,
        city_color=CITY_COLOR, cities=CITIES)

# ============================================================
# DEEP ANALYTICS — /deep
# Click-through drill-down page: city card pe click karo to us
# city ki POORI detail khul jati hai (day-wise, machine-wise,
# top parts, materials, cancelled log, part search). Template
# file ki zaroorat nahi — pura HTML yahi se render hota hai.
# ============================================================
DEEP_CSS = """
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Segoe UI',system-ui,sans-serif}
body{background:#f4f5f7;color:#111827;padding:24px}
a{text-decoration:none;color:inherit}
.wrap{max-width:1250px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px} .sub{color:#6b7280;font-size:13px;margin-bottom:18px}
.bar{background:linear-gradient(135deg,#1e1b4b,#831843);border-radius:14px;padding:14px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:18px}
.bar input{padding:7px 10px;border-radius:8px;border:none;font-size:13px}
.bar button,.chip{padding:7px 14px;border-radius:8px;border:none;background:#e91e63;color:#fff;font-weight:700;font-size:12.5px;cursor:pointer}
.chip{background:rgba(255,255,255,.14)} .chip:hover{background:rgba(255,255,255,.28)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.kpi{border-radius:14px;padding:16px;color:#fff}
.kpi .v{font-size:26px;font-weight:800} .kpi .l{font-size:11px;opacity:.9;text-transform:uppercase;letter-spacing:.04em;margin-top:2px} .kpi .s{font-size:11px;opacity:.85;margin-top:4px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-bottom:22px}
.ccard{border-radius:14px;padding:16px;color:#fff;transition:transform .12s;display:block}
.ccard:hover{transform:translateY(-3px);box-shadow:0 8px 20px rgba(0,0,0,.25)}
.ccard h3{font-size:16px;margin-bottom:8px} .ccard .row{display:flex;justify-content:space-between;font-size:12.5px;padding:2.5px 0}
.ccard .hint{margin-top:8px;font-size:11px;opacity:.85;font-weight:700}
.sec{background:#fff;border-radius:14px;padding:18px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.sec h2{font-size:15px;margin-bottom:12px;color:#374151}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;padding:8px 10px;color:#6b7280;font-size:11px;text-transform:uppercase;border-bottom:2px solid #e5e7eb;letter-spacing:.03em}
td{padding:7px 10px;border-bottom:1px solid #f3f4f6}
.g{color:#16a34a;font-weight:700}.r{color:#dc2626;font-weight:700}.mono{font-family:Consolas,monospace}
.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}
.bd{background:#dcfce7;color:#166534}.bc{background:#fee2e2;color:#991b1b}
.pill{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:700;color:#fff}
.barbg{background:#f3f4f6;border-radius:6px;height:8px;overflow:hidden}.barfill{height:8px;border-radius:6px;background:linear-gradient(90deg,#e91e63,#9c27b0)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.search input{padding:8px 12px;border:1.5px solid #e5e7eb;border-radius:8px;width:280px;font-size:13px}
.topnav{display:flex;gap:10px;margin-bottom:14px;font-size:12.5px;font-weight:700}
.topnav a{background:#fff;padding:7px 14px;border-radius:8px;color:#374151;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.topnav a.on{background:#e91e63;color:#fff}
</style>"""

@app.route('/deep')
def deep_analytics():
    presets = {k: (v[0].strftime("%Y-%m-%d"), v[1].strftime("%Y-%m-%d")) for k, v in _date_presets().items()}
    f = request.args.get('from','').strip(); t = request.args.get('to','').strip()
    if not f or not t: f, t = presets["7d"]
    city = request.args.get('city','').strip()
    if city not in CITIES: city = ""
    q = request.args.get('q','').strip()
    db = get_db()

    def qs(extra=""):
        base = f"from={f}&to={t}"
        if extra: base += "&" + extra
        return base

    where = "date>=? AND date<=?"; params = [f, t]
    if city: where += " AND city=?"; params.append(city)

    ov = db.execute(f"""SELECT COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
        {HOURS_SQL}/60.0,{MAT_SQL}/1000.0
        FROM prints WHERE {where}""", params).fetchone()
    tot, ok, canc = ov[0] or 0, ov[1] or 0, ov[2] or 0
    hrs = round(float(ov[3] or 0),1); mat = round(float(ov[4] or 0),2)
    sr = round(ok/tot*100,1) if tot else 0

    kpis = f"""<div class='kpis'>
      <div class='kpi' style='background:linear-gradient(135deg,#2563eb,#1e40af)'><div class='v'>{tot}</div><div class='l'>Total Jobs</div></div>
      <div class='kpi' style='background:linear-gradient(135deg,#16a34a,#15803d)'><div class='v'>{ok}</div><div class='l'>Success</div><div class='s'>{sr}% rate</div></div>
      <div class='kpi' style='background:linear-gradient(135deg,#dc2626,#991b1b)'><div class='v'>{canc}</div><div class='l'>Cancelled</div></div>
      <div class='kpi' style='background:linear-gradient(135deg,#db2777,#9d174d)'><div class='v'>{hrs}h</div><div class='l'>Machine Hours</div></div>
      <div class='kpi' style='background:linear-gradient(135deg,#7c3aed,#5b21b6)'><div class='v'>{mat}kg</div><div class='l'>Material Used</div></div>
    </div>"""

    GRAD = {"Pune":"linear-gradient(135deg,#2196F3,#1565c0)","Bangalore":"linear-gradient(135deg,#9C27B0,#6a1b9a)",
            "Hyderabad":"linear-gradient(135deg,#FF9800,#e65100)","Delhi":"linear-gradient(135deg,#43A047,#1b5e20)"}
    cards = "<div class='cards'>"
    for c in CITIES:
        r = db.execute(f"""SELECT COUNT(*),
            COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
            {HOURS_SQL}/60.0,{MAT_SQL}/1000.0
            FROM prints WHERE date>=? AND date<=? AND city=?""",(f,t,c)).fetchone()
        ct, cok, cca = r[0] or 0, r[1] or 0, r[2] or 0
        ch = round(float(r[3] or 0),1); cm = round(float(r[4] or 0),2)
        csr = round(cok/ct*100,1) if ct else 0
        sel = "outline:3px solid #111827;" if c == city else ""
        cards += (f"<a class='ccard' style='background:{GRAD[c]};{sel}' href='/deep?{qs('city='+c)}'>"
            f"<h3>{c}</h3>"
            f"<div class='row'><span>Jobs</span><b>{ct}</b></div>"
            f"<div class='row'><span>✅ Success</span><b>{cok} ({csr}%)</b></div>"
            f"<div class='row'><span>✖ Cancelled</span><b>{cca}</b></div>"
            f"<div class='row'><span>Hours</span><b>{ch}h</b></div>"
            f"<div class='row'><span>Material</span><b>{cm}kg</b></div>"
            f"<div class='hint'>👆 Click for full details</div></a>")
    cards += "</div>"

    scope = city if city else "All Cities"

    mrows = db.execute(f"""SELECT printer,device_model,city,COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
        {HOURS_SQL}/60.0,{MAT_SQL}
        FROM prints WHERE {where} GROUP BY printer,city ORDER BY 7 DESC""", params).fetchall()
    mtab = ""
    for r in mrows:
        mt, mok, mca = r[3] or 0, r[4] or 0, r[5] or 0
        mh = round(float(r[6] or 0),1); mg = round(float(r[7] or 0),1)
        msr = round(mok/mt*100,1) if mt else 0
        cc = CITY_COLOR.get(r[2],"#999")
        mtab += (f"<tr><td><b>{_html.escape(r[0] or 'Unknown')}</b> <span style='color:#9ca3af;font-size:11px'>{_html.escape(r[1] or '')}</span></td>"
            f"<td><span class='pill' style='background:{cc}'>{r[2]}</span></td>"
            f"<td class='mono'>{mt}</td><td class='mono g'>{mok}</td><td class='mono r'>{mca}</td>"
            f"<td class='mono'><b>{msr}%</b></td><td class='mono'>{mh}h</td><td class='mono'>{mg}g</td></tr>")

    drows = db.execute(f"""SELECT date,COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END),0),
        {HOURS_SQL}/60.0,{MAT_SQL}
        FROM prints WHERE {where} AND date!='' GROUP BY date ORDER BY date DESC""", params).fetchall()
    dtab = ""
    for r in drows:
        dtab += (f"<tr><td class='mono'>{r[0]}</td><td class='mono'>{r[1]}</td>"
            f"<td class='mono g'>{r[2]}</td><td class='mono r'>{r[3]}</td>"
            f"<td class='mono'>{round(float(r[4] or 0),1)}h</td><td class='mono'>{round(float(r[5] or 0),1)}g</td></tr>")

    prows = db.execute(f"""SELECT part_name,COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(material_g),0),COALESCE(AVG(CASE WHEN duration_min>0 THEN duration_min END),0)
        FROM prints WHERE {where} GROUP BY TRIM(part_name) ORDER BY 2 DESC LIMIT 15""", params).fetchall()
    maxp = max([r[1] for r in prows] or [1])
    ptab = ""
    for r in prows:
        pn = _html.escape((r[0] or "")[:55])
        pct = round(r[1]/maxp*100)
        ptab += (f"<tr><td title='{_html.escape(r[0] or '')}'>{pn}</td>"
            f"<td class='mono'><b>{r[1]}</b></td><td class='mono g'>{r[2]}</td>"
            f"<td class='mono'>{round(float(r[3] or 0),1)}g</td><td class='mono'>{int(r[4] or 0)}m</td>"
            f"<td style='width:120px'><div class='barbg'><div class='barfill' style='width:{pct}%'></div></div></td></tr>")

    frows = db.execute(f"""SELECT material,filament_color,COUNT(*),COALESCE(SUM(material_g),0)
        FROM prints WHERE {where} AND status IN ('Completed','Failed')
        GROUP BY material,filament_color""", params).fetchall()
    fagg = defaultdict(lambda: [0,0.0])
    for r in frows:
        fn = fil_name(r[0],r[1]); fagg[fn][0]+=r[2]; fagg[fn][1]+=float(r[3] or 0)
    ftab = ""
    for fn,v in sorted(fagg.items(), key=lambda x:-x[1][1]):
        ftab += f"<tr><td>{_html.escape(fn)}</td><td class='mono'>{v[0]}</td><td class='mono'><b>{round(v[1]/1000,3)}kg</b></td></tr>"

    crows = db.execute(f"""SELECT date,part_name,printer,duration_min,material_g,task_id,status,
        COALESCE(manual_status,'')
        FROM prints WHERE {where}
          AND (status='Cancelled' OR (manual_status IS NOT NULL AND manual_status!=''))
        ORDER BY date DESC,start_time DESC LIMIT 40""", params).fetchall()
    ctab = ""
    for r in crows:
        tid = _html.escape(str(r[5] or ""))
        cur_status = r[6] or ""; man = r[7] or ""
        # current-state chip
        if man=="Completed":   chip="<span class='badge bd'>✓ Success (manual)</span>"
        elif man=="Failed":    chip="<span class='badge' style='background:#fef3c7;color:#92400e'>✗ Failed (manual)</span>"
        elif man=="Cancelled": chip="<span class='badge bc'>⊘ Cancel (manual)</span>"
        else:                  chip="<span class='badge bc'>⊘ Cancelled</span>"
        def _b(choice,txt,bg):
            active = "opacity:1;" if ((choice=='success' and man=='Completed') or (choice=='failed' and man=='Failed') or (choice=='cancel' and man=='Cancelled')) else "opacity:.55;"
            return (f"<button onclick=\"reclass('{tid}','{choice}')\" "
                    f"style='border:none;border-radius:7px;padding:4px 9px;margin:1px;cursor:pointer;"
                    f"font-size:11px;font-weight:700;color:#fff;background:{bg};{active}'>{txt}</button>")
        btns = _b('success','✓ Success','#16a34a') + _b('failed','✗ Failed','#d97706') + _b('cancel','⊘ Cancel','#6b7280')
        if man:
            btns += (f"<button onclick=\"reclass('{tid}','reset')\" "
                     f"style='border:none;border-radius:7px;padding:4px 9px;margin:1px;cursor:pointer;"
                     f"font-size:11px;font-weight:700;color:#374151;background:#e5e7eb'>↺ Reset</button>")
        ctab += (f"<tr><td class='mono'>{r[0]}</td><td>{_html.escape((r[1] or '')[:50])}</td>"
            f"<td>{_html.escape(r[2] or '')}</td><td class='mono'>{r[3] or 0}m</td>"
            f"<td class='mono'>{round(float(r[4] or 0),1)}g</td><td>{chip}</td>"
            f"<td style='white-space:nowrap'>{btns}</td></tr>")
    if not ctab: ctab = "<tr><td colspan='7' style='text-align:center;color:#9ca3af;padding:16px'>Koi real cancel nahi is range me 🎉</td></tr>"

    stab = ""
    if q:
        srows = db.execute(f"""SELECT date,part_name,printer,city,duration_min,material_g,status,substr(end_time,12,5)
            FROM prints WHERE {where} AND part_name LIKE ?
            ORDER BY date DESC,start_time DESC LIMIT 60""", params+['%'+q+'%']).fetchall()
        for r in srows:
            b = "<span class='badge bd'>✓ Done</span>" if r[6]=="Completed" else "<span class='badge bc'>⊘ Cancelled</span>"
            cc = CITY_COLOR.get(r[3],"#999")
            stab += (f"<tr><td class='mono'>{r[0]}</td><td>{_html.escape((r[1] or '')[:50])}</td>"
                f"<td><span class='pill' style='background:{cc}'>{r[3]}</span></td>"
                f"<td>{_html.escape(r[2] or '')}</td><td class='mono'>{r[4] or 0}m</td>"
                f"<td class='mono'>{round(float(r[5] or 0),1)}g</td><td class='mono'>{r[7] or '-'}</td><td>{b}</td></tr>")
        if not stab: stab = "<tr><td colspan='8' style='text-align:center;color:#9ca3af;padding:16px'>Koi part nahi mila</td></tr>"

    db.close()

    chips = "".join(f"<a class='chip' href='/deep?from={v[0]}&to={v[1]}{'&city='+city if city else ''}'>{lbl}</a>"
        for k,lbl,v in [("today","Today",presets["today"]),("yesterday","Yesterday",presets["yesterday"]),
                        ("7d","Last 7 Days",presets["7d"]),("30d","Last 30 Days",presets["30d"]),
                        ("this_month","This Month",presets["this_month"]),("last_month","Last Month",presets["last_month"])])
    clear_city = f"<a class='chip' href='/deep?{qs()}'>✕ Clear City Filter</a>" if city else ""

    page = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Deep Analytics — Spinny 3DP</title>{DEEP_CSS}</head><body><div class='wrap'>
<div class='topnav'><a href='/'>← Dashboard</a><a href='/analytics'>Analytics</a><a class='on' href='/deep'>🔎 Deep Analytics</a><a href='/wastage'>Wastage</a><a href='/stock'>Stock</a></div>
<h1>🔎 Deep Analytics — {scope}</h1>
<div class='sub'>{f} → {t} · Kisi bhi city card pe click karo puri detail ke liye</div>
<div class='bar'>
<form method='get' action='/deep' style='display:flex;gap:8px;align-items:center'>
<input type='date' name='from' value='{f}'><input type='date' name='to' value='{t}'>
{f"<input type='hidden' name='city' value='{city}'>" if city else ""}
<button type='submit'>Apply</button></form>
{chips}{clear_city}</div>
{kpis}
{cards}
<div class='sec'><h2>🖨️ Machine-wise Performance — {scope}</h2><table><tr><th>Printer</th><th>City</th><th>Jobs</th><th>Success</th><th>Cancel</th><th>Rate</th><th>Hours</th><th>Material</th></tr>{mtab}</table></div>
<div class='grid2'>
<div class='sec'><h2>📅 Day-wise Trend — {scope}</h2><table><tr><th>Date</th><th>Jobs</th><th>Success</th><th>Cancel</th><th>Hours</th><th>Material</th></tr>{dtab}</table></div>
<div class='sec'><h2>🧵 Material Breakdown — {scope}</h2><table><tr><th>Filament</th><th>Parts</th><th>Used</th></tr>{ftab}</table>
<h2 style='margin-top:18px'>⊘ Real Cancels — click to reclassify</h2>
<div style='font-size:11px;color:#9ca3af;margin-bottom:8px'>Success = part ko success me count karo · Failed = fail · Cancel = genuine cancel (locked). Har choice sync ke baad bhi tiki rehti hai.</div>
<table><tr><th>Date</th><th>Part</th><th>Printer</th><th>Run</th><th>Mat</th><th>Now</th><th>Reclassify</th></tr>{ctab}</table></div>
</div>
<div class='sec'><h2>🏆 Top 15 Parts — {scope}</h2><table><tr><th>Part Name</th><th>Prints</th><th>Success</th><th>Total Mat</th><th>Avg Time</th><th></th></tr>{ptab}</table></div>
<div class='sec search'><h2>🔍 Part Search — {scope}</h2>
<form method='get' action='/deep' style='display:flex;gap:8px;margin-bottom:12px'>
<input type='hidden' name='from' value='{f}'><input type='hidden' name='to' value='{t}'>
{f"<input type='hidden' name='city' value='{city}'>" if city else ""}
<input type='text' name='q' value='{_html.escape(q)}' placeholder='Part ka naam likho... (e.g. cowl top)'>
<button type='submit' style='padding:8px 16px;border-radius:8px;border:none;background:#e91e63;color:#fff;font-weight:700;cursor:pointer'>Search</button></form>
{f"<table><tr><th>Date</th><th>Part</th><th>City</th><th>Printer</th><th>Run</th><th>Mat</th><th>Finish</th><th>Status</th></tr>{stab}</table>" if q else "<div style='color:#9ca3af;font-size:13px'>Kisi bhi part ka pura history dekhne ke liye upar naam type karo</div>"}
</div>
</div>
<script>
function reclass(tid, choice){{
  fetch('/api/set_status', {{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{task_id: tid, choice: choice}})
  }})
  .then(function(r){{ return r.json(); }})
  .then(function(d){{
    if(d && d.ok){{ location.reload(); }}
    else {{ alert('Error: ' + ((d && d.error) || 'failed')); }}
  }})
  .catch(function(e){{ alert('Network error: ' + e); }});
}}
</script>
</body></html>"""
    return page

@app.route('/api/stock_add',methods=['POST'])
def stock_add():
    try:
        p=request.get_json(force=True)
        qty=float(p.get("qty",0))
        if qty<=0 or p.get("city") not in CITIES or p.get("txn_type") not in ["OPENING","PURCHASE","ISSUE"]:
            return jsonify({"ok":False,"error":"invalid"}),400
        db=get_db()
        tt=parse_txn_time(p.get("time",""))
        machine = normalize_machine(p.get("machine",""))
        db.execute("INSERT INTO stock_txn (date,city,item_id,txn_type,qty,note,created_at,txn_time,machine) VALUES (?,?,?,?,?,?,?,?,?)",
            (p.get("date") or date.today().strftime("%Y-%m-%d"),p["city"],int(p["item_id"]),p["txn_type"],qty,
             p.get("note","").strip(),datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),tt,machine))
        db.commit(); db.close()
        backup_async()
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400

@app.route('/api/stock_delete',methods=['POST'])
def stock_delete():
    try:
        p=request.get_json(force=True)
        db=get_db(); db.execute("DELETE FROM stock_txn WHERE id=?",(int(p["id"]),)); db.commit(); db.close()
        backup_async()
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400

@app.route('/api/stock_item_add',methods=['POST'])
def stock_item_add():
    try:
        p=request.get_json(force=True)
        nm=p.get("name","").strip(); un=p.get("unit","Kgs").strip() or "Kgs"
        if not nm: return jsonify({"ok":False,"error":"name required"}),400
        db=get_db(); db.execute("INSERT OR IGNORE INTO stock_items (name,unit) VALUES (?,?)",(nm,un)); db.commit(); db.close()
        backup_async()
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400

@app.route('/api/stock_item_set_reorder',methods=['POST'])
def stock_item_set_reorder():
    try:
        p=request.get_json(force=True)
        db=get_db()
        db.execute("UPDATE stock_items SET reorder_level=? WHERE id=?",(float(p.get("reorder_level",0)),int(p["item_id"])))
        db.commit(); db.close()
        backup_async()
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400

@app.route('/api/stock_export')
def stock_export():
    db=get_db()
    items=[dict(r) for r in db.execute("SELECT * FROM stock_items").fetchall()]
    txns=[dict(r) for r in db.execute("SELECT * FROM stock_txn").fetchall()]
    db.close()
    resp=jsonify({"items":items,"txns":txns,"exported_at":datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")})
    resp.headers["Content-Disposition"]="attachment; filename=stock_backup.json"
    return resp

@app.route('/api/stock_import',methods=['POST'])
def stock_import():
    try:
        p=request.get_json(force=True)
        db=get_db()
        db.execute("DELETE FROM stock_txn"); db.execute("DELETE FROM stock_items")
        for it in p.get("items",[]):
            db.execute("INSERT OR REPLACE INTO stock_items (id,name,unit,active,reorder_level) VALUES (?,?,?,?,?)",
                (it["id"],it["name"],it.get("unit","Kgs"),it.get("active",1),it.get("reorder_level",0)))
        for t in p.get("txns",[]):
            db.execute("INSERT INTO stock_txn (id,date,city,item_id,txn_type,qty,note,created_at,txn_time,machine) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (t["id"],t["date"],t["city"],t["item_id"],t["txn_type"],t["qty"],t.get("note",""),t.get("created_at",""),t.get("txn_time",""),t.get("machine","")))
        db.commit(); db.close()
        backup_async()
        return jsonify({"ok":True,"txns":len(p.get("txns",[]))})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400

@app.route('/api/sheets_update',methods=['POST'])
def sheets_update():
    global _sheets
    try:
        p=request.get_json(force=True)
        if p:
            _sheets={"orders":p.get("orders",[]),"designs":p.get("designs",[]),"pendency":p.get("pendency",[]),"fetched_at":time.time()}
            save_sheets_cache()
            return jsonify({"ok":True})
    except Exception as e: print(f"[SHEETS] Push error: {e}")
    return jsonify({"ok":False}),400

@app.route('/api/stock_update',methods=['POST'])
def stock_update():
    try:
        p=request.get_json(force=True)
        rows = p.get("rows", []) if isinstance(p, dict) else (p or [])
        added = _process_stock_rows(rows)
        return jsonify({"ok":True,"stock_new":added})
    except Exception as e:
        print(f"[STOCK_SHEET] push error: {e}")
        return jsonify({"ok":False,"error":str(e)}),400

@app.route('/api/stock_sheet_reset', methods=['POST'])
def stock_sheet_reset():
    try:
        db = get_db()
        deleted = db.execute("DELETE FROM stock_txn WHERE note LIKE 'From Sheet%'").rowcount
        db.execute("DELETE FROM stock_sheet_log")
        db.commit(); db.close()
        backup_async()
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route('/api/debug_stock_sheet')
def debug_stock_sheet():
    if not STOCK_SHEET_URL:
        return jsonify({"ok":False,"error":"STOCK_SHEET_URL env var is not set on this server"})
    try:
        r = requests.get(STOCK_SHEET_URL, timeout=20, allow_redirects=True)
    except Exception as e:
        return jsonify({"ok":False,"stage":"http_request","error":str(e)})
    result = {
        "ok": True,
        "url_used": STOCK_SHEET_URL,
        "final_url": r.url,
        "status_code": r.status_code,
        "content_type": r.headers.get("Content-Type",""),
        "raw_preview": r.text[:800],
    }
    try:
        payload = r.json()
        rows = payload.get("rows", []) if isinstance(payload, dict) else payload
        result["json_parsed"] = True
        result["row_count"] = len(rows) if isinstance(rows, list) else "not a list"
        result["sample_rows"] = rows[:3] if isinstance(rows, list) else None
    except Exception as e:
        result["json_parsed"] = False
        result["json_error"] = str(e)
    return jsonify(result)

@app.route('/api/sync',methods=['GET','POST'])
def api_sync():
    try:
        n=do_sync()
        s=0
        try: s=sync_stock_sheet()
        except Exception as e: print(f"[STOCK_SHEET] {e}")
        return jsonify({"ok":True,"new":n,"stock_sheet_new":s})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/set_status',methods=['POST'])
def set_status():
    """Cancelled part ko manually reclassify karo: Success / Failed / Time.
    Choice `manual_status` me save hoti hai (taaki agla sync/auto-fix overwrite
    na kare) aur `status` bhi turant update ho jata hai.
       success -> Completed | failed -> Failed | time -> Cancelled (locked)
       reset   -> override hata do, wapas normal Cancelled"""
    try:
        p=request.get_json(force=True)
        tid=str(p.get("task_id","")).strip()
        choice=str(p.get("choice","")).strip().lower()
        MAP={"success":"Completed","failed":"Failed","time":"Cancelled","cancel":"Cancelled","reset":""}
        if not tid or choice not in MAP:
            return jsonify({"ok":False,"error":"invalid task_id/choice"}),400
        db=get_db()
        if choice=="reset":
            db.execute("UPDATE prints SET manual_status='', status='Cancelled' WHERE task_id=?",(tid,))
        else:
            new_status=MAP[choice]
            db.execute("UPDATE prints SET manual_status=?, status=? WHERE task_id=?",(new_status,new_status,tid))
        changed=db.total_changes
        db.commit(); db.close()
        if not changed:
            return jsonify({"ok":False,"error":"task_id not found"}),404
        backup_async()
        return jsonify({"ok":True,"choice":choice})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400

@app.route('/api/backup_now',methods=['GET','POST'])
def api_backup_now():
    ok=do_backup(force=True)
    return jsonify({"ok":ok,"last":_backup_state["last"],"error":_backup_state["error"]})

@app.route('/api/debug_bambu')
def debug_bambu():
    city=request.args.get("city","Delhi")
    date_filter=request.args.get("date","")
    acc=next((a for a in ACCOUNTS if (a["city_override"]==city)),None)
    if not acc:
        acc=next((a for a in ACCOUNTS if a["city_override"] is None),None)
    if not acc: return jsonify({"error":"no account for city"}),400
    tasks=fetch_tasks(acc["token"])
    rows=[]
    seen_ids=defaultdict(int); seen_titles=defaultdict(list)
    for t in tasks:
        rec=compute_record(t, acc)
        if acc["city_override"] is None and rec["city"]!=city: continue
        if date_filter and rec["date"]!=date_filter: continue
        tid=str(t.get("id",""))
        seen_ids[tid]+=1
        st=parse_dt(t.get("startTime")); et=parse_dt(t.get("endTime"))
        d_wall=int((et-st).total_seconds()/60) if (st and et and et>st) else 0
        cost=int(t.get("costTime") or 0)
        row={"id":tid,"title":rec["part"],"date":rec["date"],
             "start":rec["start"],"end":rec["end"],
             "raw_status":t.get("status"),"mapped_status":rec["status"],
             "wall_min":d_wall,"cost_min":cost//60,"final_dur":rec["dur"],
             "material_g":rec["matg"],"device":rec["printer"]}
        rows.append(row)
        seen_titles[rec["part"]].append(row)
    rows.sort(key=lambda x:(x["date"],x["start"]),reverse=True)
    dup_ids={k:v for k,v in seen_ids.items() if v>1}
    dup_titles={k:len(v) for k,v in seen_titles.items() if len(v)>1}
    return jsonify({
        "city":city,"date_filter":date_filter or "all",
        "total_tasks_from_bambu":len(rows),
        "duplicate_task_ids":dup_ids,
        "repeated_titles":dup_titles,
        "rows":rows[:120]
    })

@app.route('/api/daily_success')
def api_daily_success():
    day = request.args.get('date') or datetime.now(IST).strftime('%Y-%m-%d')
    db = get_db()
    rows = db.execute("""SELECT city, part_name, printer,
            date, substr(end_time,12,5) AS end_t
        FROM prints
        WHERE status='Completed' AND date=?
          AND city IN ('Pune','Bangalore','Hyderabad','Delhi')
        ORDER BY end_time ASC""", (day,)).fetchall()
    db.close()
    out = {c: [] for c in CITIES}
    for r in rows:
        out[r["city"]].append({
            "date": r["date"], "part": r["part_name"],
            "printer": r["printer"], "end_time": r["end_t"]
        })
    return jsonify(out)

@app.route('/api/debug_raw')
def debug_raw():
    city = request.args.get("city", "Delhi")
    q = request.args.get("q", "").lower()
    acc = next((a for a in ACCOUNTS if a["city_override"] == city), None)
    if not acc:
        acc = next((a for a in ACCOUNTS if a["city_override"] is None), None)
    if not acc: return jsonify({"error":"no account for city"}),400
    tasks = fetch_tasks(acc["token"])
    out = []
    for t in tasks:
        if q and q not in str(t.get("title", "")).lower():
            continue
        out.append(t)
        if len(out) >= 5:
            break
    return jsonify({"count": len(out), "raw_tasks": out})

@app.route('/api/health')
def health():
    db=get_db(); total=db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    ip=db.execute("SELECT COUNT(*) FROM prints WHERE status='In Process'").fetchone()[0]
    ls=db.execute("SELECT synced_at FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    return jsonify({"status":"ok","total":total,"in_process":ip,"last_sync":ls[0] if ls else None})

init_db()
load_sheets_cache()
restore_from_github()
startup_fixes()
threading.Thread(target=auto_sync_loop,daemon=True).start()
threading.Thread(target=auto_backup_loop,daemon=True).start()
if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)
