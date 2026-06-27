"""Spinny 3DP CRM - Cloud Edition. Real data from Bambu API, auto-cleaned."""
import os, sqlite3, threading, time, html as _html
from datetime import datetime, date, timezone, timedelta
from flask import Flask, render_template, jsonify, redirect, request
import requests
from collections import defaultdict

IST = timezone(timedelta(hours=5, minutes=30))
app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'spinny_3dp.db')

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
STATUS_MAP = {1:"Queued",2:"In Process",3:"Failed",4:"Completed",5:"Cancelled",6:"Failed"}
API_URL  = "https://api.bambulab.com/v1/user-service/my/tasks"
SHEETS_URL = os.environ.get("SHEETS_API_URL","")

_sheets = {"orders":[],"designs":[],"pendency":[],"fetched_at":0}
SHEETS_TTL = 1800

def fetch_sheets(force=False):
    global _sheets
    if not SHEETS_URL: return _sheets
    if not force and time.time()-_sheets["fetched_at"]<SHEETS_TTL: return _sheets
    try:
        r = requests.get(SHEETS_URL,timeout=20); d=r.json()
        if d.get("ok"):
            _sheets={"orders":d["data"].get("orders",[]),"designs":d["data"].get("designs",[]),"fetched_at":time.time()}
    except Exception as e: print(f"[SHEETS] {e}")
    return _sheets

def get_db():
    db=sqlite3.connect(DB_PATH); db.row_factory=sqlite3.Row; return db

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
        CREATE INDEX IF NOT EXISTS idx_date ON prints(date);
        CREATE INDEX IF NOT EXISTS idx_city ON prints(city);""")
    for col in ["ALTER TABLE prints ADD COLUMN ist_done INTEGER DEFAULT 0",
                "ALTER TABLE prints ADD COLUMN device_model TEXT DEFAULT ''",
                "ALTER TABLE prints ADD COLUMN filament_color TEXT DEFAULT ''",
                "ALTER TABLE prints ADD COLUMN cost_time INTEGER DEFAULT 0"]:
        try: db.execute(col)
        except: pass
    db.commit(); db.close()

def parse_dt(v):
    if not v: return None
    try:
        s=str(v)
        if 'T' in s: return datetime.strptime(s.replace('Z','').split('.')[0],"%Y-%m-%dT%H:%M:%S")
        iv=int(float(s))
        if iv>1e12: iv//=1000
        return datetime.fromtimestamp(iv) if iv>0 else None
    except: return None

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
    if 0 < d_wall <= 1440:
        if d_cost and d_wall > d_cost*1.5 and (d_wall - d_cost) > 120:
            dur = d_cost
        else:
            dur = d_wall
    elif d_cost:
        dur = d_cost
    else:
        dur = 0
    status=STATUS_MAP.get(int(t.get("status") or 0),str(t.get("status","")))
    if status in ("Queued","In Process") and et and (datetime.utcnow()-et).total_seconds()>300:
        status="Completed"
    if status=="Queued" and et:
        status="Completed"
    if status=="Cancelled":
        status="Failed"
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
    rows=db.execute("""SELECT id,printer,start_time,end_time,status,duration_min
                       FROM prints
                       WHERE start_time!='' AND end_time!='' AND LENGTH(end_time)>10
                         AND printer NOT IN ('','Unknown')
                         AND duration_min>0 AND duration_min<=1440
                       ORDER BY printer,start_time""").fetchall()
    by_printer=defaultdict(list)
    for r in rows:
        try:
            st=datetime.strptime(r[2],"%Y-%m-%d %H:%M")
            et=datetime.strptime(r[3],"%Y-%m-%d %H:%M")
            if et<=st: continue
            by_printer[r[1]].append({"id":r[0],"st":st,"et":et,"status":r[4]})
        except: pass
    rank={"Completed":4,"Failed":3,"In Process":2,"Queued":1}
    to_del=set()
    for printer,jobs in by_printer.items():
        jobs.sort(key=lambda x:x["st"])
        kept=[]
        for j in jobs:
            clash=None
            for k in kept:
                if j["st"]<k["et"] and k["st"]<j["et"]:
                    clash=k; break
            if clash:
                if rank.get(j["status"],0)>rank.get(clash["status"],0):
                    to_del.add(clash["id"]); kept.remove(clash); kept.append(j)
                else:
                    to_del.add(j["id"])
            else:
                kept.append(j)
    for d in to_del:
        db.execute("DELETE FROM prints WHERE id=?",(d,))
    db.commit()
    return len(to_del)

def startup_fixes():
    if not os.path.exists(DB_PATH): return
    db=sqlite3.connect(DB_PATH)
    try:
        db.execute("UPDATE prints SET status='Failed' WHERE status='Cancelled'")
        db.execute("UPDATE prints SET status='Completed' WHERE status IN ('In Process','Printing','Queued') AND end_time IS NOT NULL AND end_time!='' AND LENGTH(end_time)>10")
        db.execute("DELETE FROM prints WHERE status NOT IN ('Completed','Failed')")
        db.execute("UPDATE prints SET city='Bangalore' WHERE printer LIKE 'Bengaluru%' AND city!='Bangalore'")
        db.execute("UPDATE prints SET city='Bangalore' WHERE city IN ('Unknown','Hyderabad') AND printer LIKE '%engaluru%'")
        db.execute("UPDATE prints SET duration_min=0 WHERE duration_min>1440")
        db.commit()
        dn=dedup_prints(db)
        print(f"[AUTO-FIX] Dedup:{dn}")
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
    print(f"[SYNC] {datetime.now().strftime('%H:%M:%S')}")
    db=get_db()
    existing=set(r[0] for r in db.execute("SELECT task_id FROM prints").fetchall())
    new_count=0; upd_count=0
    for acc in ACCOUNTS:
        tasks=fetch_tasks(acc["token"])
        for t in tasks:
            tid=str(t.get("id",""))
            if not tid: continue
            rec=compute_record(t, acc)
            if rec["status"] not in ("Completed","Failed"):
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
    print(f"[SYNC] Done +{new_count} updated:{upd_count} | total {total}")
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

HOURS_SQL = "COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN duration_min ELSE 0 END),0)"
MAT_SQL   = "COALESCE(SUM(CASE WHEN status IN ('Completed','In Process','Failed') THEN material_g ELSE 0 END),0)"

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
    db=get_db(); today=date.today().strftime("%Y-%m-%d")
    total    =db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    completed=db.execute("SELECT COUNT(*) FROM prints WHERE status='Completed'").fetchone()[0]
    failed   =db.execute("SELECT COUNT(*) FROM prints WHERE status IN ('Failed','Cancelled')").fetchone()[0]
    in_proc  =db.execute("SELECT COUNT(*) FROM prints WHERE status='In Process'").fetchone()[0]
    hrs_total=db.execute(f"SELECT {HOURS_SQL}/60.0 FROM prints WHERE status IN ('Completed','In Process','Failed')").fetchone()[0]
    mat_total=db.execute(f"SELECT {MAT_SQL}/1000.0 FROM prints WHERE status IN ('Completed','In Process','Failed')").fetchone()[0]
    cities_today={}
    for c in CITIES:
        r=db.execute(f"""SELECT COUNT(*),{HOURS_SQL}/60.0,{MAT_SQL},
            COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status='In Process' THEN 1 ELSE 0 END),0)
            FROM prints WHERE city=? AND date=?""",(c,today)).fetchone()
        cities_today[c]={"prints":r[0],"hours":round(float(r[1] or 0),1),"mat_g":round(float(r[2] or 0),1),"ok":r[3],"live":r[4],"color":CITY_COLOR[c]}
    today_total={"prints":sum(v["prints"] for v in cities_today.values()),"ok":sum(v["ok"] for v in cities_today.values()),
                 "live":sum(v["live"] for v in cities_today.values()),"hours":round(sum(v["hours"] for v in cities_today.values()),1),
                 "mat_g":round(sum(v["mat_g"] for v in cities_today.values()),1),
                 "failed":db.execute("SELECT COUNT(*) FROM prints WHERE date=? AND status IN ('Failed','Cancelled')",(today,)).fetchone()[0]}
    recent_rows=db.execute("SELECT date,part_name,printer,city,material,duration_min,material_g,status,start_time,end_time FROM prints WHERE date!='' ORDER BY date DESC,start_time DESC LIMIT 25").fetchall()
    recent_html=build_recent_html(recent_rows,today)
    daily_rows_html=build_daily_html(db,today)
    last_sync=db.execute("SELECT synced_at,total_records FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    sheets=_sheets; orders=sheets.get("orders",[]); designs=sheets.get("designs",[])
    ord_city={}
    for c in CITIES:
        co=[o for o in orders if o.get("order_city")==c]
        ord_city[c]={"total":len(co),"fulfilled":len([o for o in co if "fulfilled" in o.get("status","").lower()]),"pending":len([o for o in co if o.get("status","").lower() not in ["fulfilled","cancelled",""]])}
    today_designs=[d for d in designs if d.get("printed_date","")==today]
    month_designs=[d for d in designs if str(d.get("printed_date","")).startswith(today[:7])]
    db.close()
    return render_template('dashboard.html',
        total=total,completed=completed,failed=failed,in_proc=in_proc,
        hrs_total=round(float(hrs_total or 0),1),mat_total=round(float(mat_total or 0),2),
        cities_today=cities_today,today_total=today_total,
        recent_html=recent_html,daily_rows_html=daily_rows_html,
        last_sync=last_sync,today=today,city_color=CITY_COLOR,cities=CITIES,
        ord_city=ord_city,total_orders=len(orders),
        today_designs=today_designs,month_designs=month_designs,total_designs=len(designs))

@app.route('/city/<city>')
def city_page(city):
    if city not in CITIES: return redirect('/')
    db=get_db(); today=date.today().strftime("%Y-%m-%d")
    ov=db.execute(f"""SELECT COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status IN ('Failed','Cancelled') THEN 1 ELSE 0 END),0),
        {HOURS_SQL}/60.0,{MAT_SQL}/1000.0 FROM prints WHERE city=?""",(city,)).fetchone()
    td=db.execute(f"SELECT COUNT(*),{HOURS_SQL}/60.0,{MAT_SQL} FROM prints WHERE city=? AND date=?",(city,today)).fetchone()
    rows=db.execute("SELECT date,part_name,printer,material,start_time,end_time,duration_min,material_g,status,device_model,filament_color FROM prints WHERE city=? ORDER BY date DESC,start_time DESC",(city,)).fetchall()
    db.close()
    return render_template('city.html',city=city,color=CITY_COLOR[city],
        ov=ov,td=td,rows=rows,today=today,city_color=CITY_COLOR,cities=CITIES)

@app.route('/monthly')
def monthly():
    db=get_db()
    rows=db.execute(f"""SELECT substr(date,1,7) as mo,city,COUNT(*),
        COALESCE(SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status IN ('Failed','Cancelled') THEN 1 ELSE 0 END),0),
        ROUND({HOURS_SQL}/60.0,1),ROUND({MAT_SQL}/1000.0,3)
        FROM prints WHERE date!='' GROUP BY mo,city ORDER BY mo DESC,city""").fetchall()
    md=defaultdict(lambda:{c:{"total":0,"done":0,"failed":0,"hours":0,"mat_kg":0} for c in CITIES})
    for r in rows:
        if r[1] in CITIES: md[r[0]][r[1]]={"total":r[2],"done":r[3],"failed":r[4],"hours":r[5],"mat_kg":r[6]}
    months_list=[]
    for mo in sorted(md.keys(),reverse=True):
        cd=md[mo]; months_list.append((mo,cd,sum(v["total"] for v in cd.values()),sum(v["done"] for v in cd.values()),sum(v["failed"] for v in cd.values()),round(sum(v["hours"] for v in cd.values()),1),round(sum(v["mat_kg"] for v in cd.values()),2)))
    db.close()
    return render_template('monthly.html',months_list=months_list,city_color=CITY_COLOR,cities=CITIES)

@app.route('/materials')
def materials():
    db=get_db()
    top=db.execute("SELECT material,COUNT(*),COALESCE(SUM(material_g),0)/1000.0 FROM prints GROUP BY material ORDER BY 3 DESC").fetchall()
    by_city=db.execute("SELECT city,material,COUNT(*),COALESCE(SUM(material_g),0)/1000.0 FROM prints GROUP BY city,material ORDER BY city,4 DESC").fetchall()
    db.close()
    return render_template('materials.html',top=top,by_city=by_city,city_color=CITY_COLOR,cities=CITIES)

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
    today_limit=date.today().strftime("%Y-%m-%d")
    # printed_date = primary reference
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
    # Daily summary by printed_date only, no future dates
    _daily=defaultdict(lambda:{c:0 for c in CITIES})
    for d in all_designs:
        dd=d.get("printed_date",""); dc=d.get("city","")
        if dd and dd<=today_limit and dc in CITIES: _daily[dd][dc]+=1
    daily_summary=sorted(_daily.items(),key=lambda x:x[0],reverse=True)[:30]
    return render_template('designs.html',designs=show_designs,all_count=len(all_designs),
        city_color=CITY_COLOR,cities=CITIES,today_count=len(today_list),
        filter_today=filter_today,filter_date=filter_date,today_str=today_str,
        daily_summary=daily_summary)

@app.route('/api/sheets_update',methods=['POST'])
def sheets_update():
    global _sheets
    try:
        p=request.get_json(force=True)
if p: _sheets={"orders":p.get("orders",[]),"designs":p.get("designs",[]),"pendency":p.get("pendency",[]),"fetched_at":time.time()}; return jsonify({"ok":True})    except Exception as e: print(f"[SHEETS] Push error: {e}")
    return jsonify({"ok":False}),400

@app.route('/api/sync',methods=['GET','POST'])
def api_sync():
    try: n=do_sync(); fetch_sheets(force=True); return jsonify({"ok":True,"new":n})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/health')
def health():
    db=get_db(); total=db.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    ip=db.execute("SELECT COUNT(*) FROM prints WHERE status='In Process'").fetchone()[0]
    ls=db.execute("SELECT synced_at FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    return jsonify({"status":"ok","total":total,"in_process":ip,"last_sync":ls[0] if ls else None})

init_db()
startup_fixes()
threading.Thread(target=auto_sync_loop,daemon=True).start()
if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)
