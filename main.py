import os
import telebot
from telebot import types
import sqlite3
import csv
import io
import threading
import schedule
import time
from datetime import datetime, date, timedelta

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "8968461153:AAETpKpkeupU1XSOa0wEue2QF4MlbmmKMK0"
_admin_env = os.environ.get("ADMIN_IDS", "").strip()
if _admin_env:
    ADMIN_IDS = [int(x.strip()) for x in _admin_env.replace(";",",").split(",") if x.strip().isdigit()]
else:
    ADMIN_IDS = [1261052681]

bot = telebot.TeleBot(TOKEN)
DB_PATH = os.environ.get("DB_PATH", "/data/topmart.db")
try:
    _dbdir = os.path.dirname(DB_PATH)
    if _dbdir: os.makedirs(_dbdir, exist_ok=True)
    # test writability
    with open(DB_PATH + ".write_test", "w") as _f: _f.write("ok")
    os.remove(DB_PATH + ".write_test")
except Exception as _e:
    print(f"⚠️ {DB_PATH} not writable ({_e}). Falling back to topmart.db in current dir.")
    DB_PATH = "topmart.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
        name TEXT,
        role TEXT DEFAULT 'agent',
        viloyat TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS dokonlar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nomi TEXT, egasi TEXT, telefon TEXT,
        viloyat TEXT, hudud TEXT,
        latitude REAL, longitude REAL,
        foto TEXT, agent_id INTEGER,
        holat TEXT DEFAULT 'faol', created_at TEXT,
        owner_telegram_id INTEGER
    );
    
    CREATE TABLE IF NOT EXISTS mahsulotlar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nomi TEXT, narx INTEGER,
        birlik TEXT DEFAULT 'dona', faol INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS savdolar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dokon_id INTEGER, agent_id INTEGER,
        jami_summa INTEGER, tolov_turi TEXT,
        foto TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS savdo_tafsilot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        savdo_id INTEGER, mahsulot_id INTEGER,
        miqdor INTEGER, narx INTEGER, summa INTEGER
    );
    CREATE TABLE IF NOT EXISTS olmagan_dokonlar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dokon_id INTEGER, agent_id INTEGER,
        sabab TEXT, sabab_text TEXT,
        latitude REAL, longitude REAL,
        qaytish_sanasi TEXT,
        bajarildi INTEGER DEFAULT 0, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS pul_olish (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dokon_id INTEGER, agent_id INTEGER,
        summa INTEGER, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS nasiya (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dokon_id INTEGER, agent_id INTEGER,
        savdo_id INTEGER,
        jami_summa INTEGER,
        tolangan INTEGER DEFAULT 0,
        qoldiq INTEGER,
        created_at TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS mijoz_balans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dokon_id INTEGER UNIQUE,
        balans INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS revisitlar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dokon_id INTEGER, agent_id INTEGER,
        last_order_date TEXT,
        revisit_date TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_revisit_pending ON revisitlar(revisit_date,status);
    CREATE TABLE IF NOT EXISTS agent_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER, oy TEXT,
        savdo_plan INTEGER DEFAULT 0,
        dokon_plan INTEGER DEFAULT 0,
        created_at TEXT,
        UNIQUE(agent_id, oy)
    );
    """)
    conn.commit()
    # Migrations for existing DBs
    try: c.execute("ALTER TABLE dokonlar ADD COLUMN owner_telegram_id INTEGER")
    except: pass
    try: c.execute("ALTER TABLE olmagan_dokonlar ADD COLUMN foto TEXT")
    except: pass
    # Repeat System fields
    for col,typ in [("first_order_date","TEXT"),("last_order_date","TEXT"),
                    ("total_orders","INTEGER DEFAULT 0"),("repeat_orders","INTEGER DEFAULT 0"),
                    ("total_sales","INTEGER DEFAULT 0"),("avg_repeat_days","REAL DEFAULT 0")]:
        try: c.execute(f"ALTER TABLE dokonlar ADD COLUMN {col} {typ}")
        except: pass
    conn.commit()
    # One-time backfill of repeat stats from existing savdolar
    try:
        c.execute("SELECT COUNT(*) FROM dokonlar WHERE total_orders>0")
        if c.fetchone()[0]==0:
            c.execute("SELECT DISTINCT dokon_id FROM savdolar")
            for (did,) in c.fetchall():
                c.execute("SELECT created_at,jami_summa FROM savdolar WHERE dokon_id=? ORDER BY created_at",(did,))
                rows=c.fetchall()
                if not rows: continue
                first=rows[0][0]; last=rows[-1][0]
                total=len(rows); tsum=sum(r[1] or 0 for r in rows)
                repeat_n=max(0,total-1); avg=0.0
                if repeat_n>0:
                    diffs=[]
                    from datetime import datetime as _dt
                    for i in range(1,total):
                        try:
                            d1=_dt.fromisoformat(rows[i-1][0]); d2=_dt.fromisoformat(rows[i][0])
                            diffs.append((d2-d1).days)
                        except: pass
                    if diffs: avg=sum(diffs)/len(diffs)
                c.execute("""UPDATE dokonlar SET first_order_date=?,last_order_date=?,
                             total_orders=?,repeat_orders=?,total_sales=?,avg_repeat_days=?
                             WHERE id=?""",(first,last,total,repeat_n,tsum,avg,did))
            conn.commit()
    except Exception as _e: pass
    # One-time fix: To'lqinjon's viloyat was wrongly set as Farg'ona, should be Namangan
    try:
        c.execute("UPDATE dokonlar SET viloyat='Namangan' WHERE agent_id=8577758808 AND viloyat=\"Farg'ona\"")
        c.execute("UPDATE users SET viloyat='Namangan' WHERE telegram_id=8577758808 AND viloyat=\"Farg'ona\"")
    except: pass
    try: c.execute("CREATE TABLE IF NOT EXISTS mijoz_balans (id INTEGER PRIMARY KEY AUTOINCREMENT, dokon_id INTEGER UNIQUE, balans INTEGER DEFAULT 0)")
    except: pass
    conn.commit(); conn.close()

def get_db(): return sqlite3.connect(DB_PATH)
user_state = {}
def set_state(uid,s,d=None): user_state[uid]={"state":s,"data":d or {}}
def get_state(uid): return user_state.get(uid,{"state":None,"data":{}})
def clear_state(uid): user_state.pop(uid,None)
def get_balans(dokon_id):
    conn=get_db();c=conn.cursor()
    c.execute("SELECT balans FROM mijoz_balans WHERE dokon_id=?",(dokon_id,))
    row=c.fetchone(); conn.close(); return row[0] if row else 0
def update_balans_delta(c,dokon_id,delta):
    c.execute("INSERT INTO mijoz_balans (dokon_id,balans) VALUES (?,?) ON CONFLICT(dokon_id) DO UPDATE SET balans=balans+?",(dokon_id,delta,delta))
def get_user(tid):
    conn=get_db();c=conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id=?",(tid,))
    r=c.fetchone();conn.close();return r
def is_admin(tid):
    if tid in ADMIN_IDS: return True
    u=get_user(tid); return u and u[3]=="admin"
def all_admin_ids():
    """Env ADMIN_IDS + DB role='admin' users (de-duplicated)."""
    ids=set(ADMIN_IDS)
    try:
        conn=get_db();c=conn.cursor()
        c.execute("SELECT telegram_id FROM users WHERE role='admin'")
        for (tid,) in c.fetchall():
            if tid: ids.add(tid)
        conn.close()
    except: pass
    return ids
def notify_admins(text=None, photo=None, caption=None):
    """Send a notification to every admin (env + DB)."""
    for aid in all_admin_ids():
        try:
            if photo: bot.send_photo(aid, photo, caption=caption or text)
            else: bot.send_message(aid, text)
        except: pass
def check_pending(uid):
    u=get_user(uid)
    if u and u[3]=="pending":
        bot.send_message(uid,"⏳ Hisobingiz hali tasdiqlanmagan. Admin tasdiqlashini kuting.")
        return True
    return False
def fmt(a):
    try: return f"{round(float(a)):,}".replace(","," ")+" so'm"
    except: return "0 so'm"
def _send_repeat_report(uid):
    conn=get_db();c=conn.cursor()
    c.execute("""SELECT id,nomi,viloyat,last_order_date,avg_repeat_days,total_orders,repeat_orders,total_sales
                 FROM dokonlar WHERE holat='faol'""")
    rows=c.fetchall(); conn.close()
    if not rows:
        bot.send_message(uid,"❗ Faol dokon yo'q.",reply_markup=main_kb("admin")); return
    hot=warm=cold=new=0; repeat_stores=0; total_sales=0
    cold_list=[]
    for r in rows:
        did,nomi,vil,last_d,avg_d,t_o,r_o,t_s=r
        t_o=t_o or 0; r_o=r_o or 0; t_s=t_s or 0; avg_d=avg_d or 0
        total_sales+=t_s
        if r_o>0: repeat_stores+=1
        lbl,days=get_store_status(last_d,avg_d)
        if "HOT" in lbl: hot+=1
        elif "WARM" in lbl: warm+=1
        elif "COLD" in lbl:
            cold+=1
            cold_list.append((days or 0,nomi,vil or '—',days))
        else: new+=1
    rate=round((repeat_stores/len(rows))*100,1) if rows else 0
    cold_list.sort(reverse=True)
    text=(f"🔁 REPEAT HISOBOTI\n{'━'*26}\n"
          f"🏪 Jami faol: {len(rows)}\n"
          f"🟢 HOT: {hot}\n"
          f"🟡 WARM: {warm}\n"
          f"🔴 COLD: {cold}\n"
          f"⚪ NEW (savdosiz): {new}\n\n"
          f"📈 Repeat Rate: {rate}%\n"
          f"   ({repeat_stores}/{len(rows)} dokon qayta savdo qilgan)\n"
          f"💰 Jami savdo: {fmt(total_sales)}\n")
    if cold_list:
        text+=f"\n{'━'*26}\n🔴 QAYTA KIRISH KERAK (top 15):\n"
        for d,nomi,vil,days in cold_list[:15]:
            text+=f"  • {nomi} ({vil}) — {days} kun\n"
    bot.send_message(uid,text,reply_markup=main_kb("admin"))

@bot.message_handler(func=lambda m:m.text=="🔁 Repeat hisoboti")
def repeat_hisoboti(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    _send_repeat_report(uid)

def _build_lost_dokons_report(scope_agent_id=None):
    """Return text + counts for 'lost' (silent) dokons report.
    scope_agent_id=None → admin (barcha agentlar). Aks holda — bitta agent."""
    from datetime import datetime as _dt
    conn=get_db();c=conn.cursor()
    if scope_agent_id:
        c.execute("""SELECT d.id,d.nomi,d.viloyat,d.hudud,d.last_order_date,d.created_at,d.agent_id,
                            COALESCE(u.name,'—'),d.total_orders
                     FROM dokonlar d LEFT JOIN users u ON u.telegram_id=d.agent_id
                     WHERE d.holat='faol' AND d.agent_id=?""",(scope_agent_id,))
    else:
        c.execute("""SELECT d.id,d.nomi,d.viloyat,d.hudud,d.last_order_date,d.created_at,d.agent_id,
                            COALESCE(u.name,'—'),d.total_orders
                     FROM dokonlar d LEFT JOIN users u ON u.telegram_id=d.agent_id
                     WHERE d.holat='faol'""")
    rows=c.fetchall(); conn.close()
    now=_dt.now()
    # Buckets
    new_no_sale=[]  # registered 14+ days ago, never bought
    yellow=[]  # 30-60 days silent
    orange=[]  # 60-90 days
    red=[]     # 90+ days
    for did,nomi,vil,hudud,last_d,created_at,agent_id,agent_name,total_o in rows:
        if not last_d or (total_o or 0)==0:
            try:
                cr=_dt.fromisoformat(created_at) if created_at else None
                if cr and (now-cr).days>=14:
                    new_no_sale.append((did,nomi,vil,agent_name,(now-cr).days,"yangi"))
            except: pass
            continue
        try:
            ld=_dt.fromisoformat(last_d); days=(now-ld).days
        except: continue
        rec=(did,nomi,vil,agent_name,days,last_d[:10])
        if days>=90: red.append(rec)
        elif days>=60: orange.append(rec)
        elif days>=30: yellow.append(rec)
    # Sort each desc by days
    for lst in (red,orange,yellow,new_no_sale): lst.sort(key=lambda x:-x[4])
    title="⚠️ YO'QOLAYOTGAN DOKONLAR" + (f"\n👤 {scope_agent_id}" if scope_agent_id else " (BARCHA)")
    text=(f"{title}\n{'━'*26}\n"
          f"🔴 90+ kun jim: {len(red)} ta\n"
          f"🟠 60-90 kun: {len(orange)} ta\n"
          f"🟡 30-60 kun: {len(yellow)} ta\n"
          f"⚪ Yangi (savdo yo'q): {len(new_no_sale)} ta\n")
    def _fmt_block(emoji, label, items, limit=15):
        if not items: return ""
        s=f"\n{'━'*26}\n{emoji} {label} ({len(items)} ta):\n"
        for did,nomi,vil,aname,days,extra in items[:limit]:
            if scope_agent_id:
                s+=f"  • {nomi} ({vil or '—'}) — {days} kun\n"
            else:
                s+=f"  • {nomi} ({vil or '—'}, {aname}) — {days} kun\n"
        if len(items)>limit:
            s+=f"  … +{len(items)-limit} ta\n"
        return s
    text+=_fmt_block("🔴","KRITIK (90+ kun)",red)
    text+=_fmt_block("🟠","XAVFLI (60-90 kun)",orange)
    text+=_fmt_block("🟡","DIQQAT (30-60 kun)",yellow)
    text+=_fmt_block("⚪","YANGI — savdo yo'q",new_no_sale)
    total=len(red)+len(orange)+len(yellow)+len(new_no_sale)
    if total==0:
        text+=f"\n{'━'*26}\n✅ Hammasi joyida! Yo'qolayotgan dokon yo'q."
    return text,total

@bot.message_handler(func=lambda m:m.text=="⚠️ Yo'qolayotgan dokonlar")
def yoqolayotgan_dokonlar(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    text,_=_build_lost_dokons_report()
    _send_long(uid, text)

def send_weekly_lost_alert():
    """Cron: har dushanba 09:00 — adminlarga yo'qolayotgan dokonlar hisoboti."""
    text,total=_build_lost_dokons_report()
    if total==0: return
    header="📊 HAFTALIK OGOHLANTIRISH\n\n"
    for aid in all_admin_ids():
        _send_long(aid, header+text)

def _build_old_nasiya_report(scope_agent_id=None):
    """Old-credit aging report. Groups outstanding nasiya by age:
    🟡 30-60 kun, 🟠 60-90 kun, 🔴 90+ kun."""
    from datetime import datetime as _dt
    conn=get_db();c=conn.cursor()
    base="""SELECT n.id,n.dokon_id,d.nomi,d.viloyat,n.jami_summa,n.tolangan,n.qoldiq,
                   n.created_at,n.agent_id,COALESCE(u.name,'—')
            FROM nasiya n
            JOIN dokonlar d ON d.id=n.dokon_id
            LEFT JOIN users u ON u.telegram_id=n.agent_id
            WHERE n.qoldiq>0"""
    if scope_agent_id:
        c.execute(base+" AND n.agent_id=?",(scope_agent_id,))
    else:
        c.execute(base)
    rows=c.fetchall(); conn.close()
    now=_dt.now()
    yellow=[]; orange=[]; red=[]
    sum_y=sum_o=sum_r=0
    for nid,did,nomi,vil,jami,tol,qoldiq,created_at,agent_id,aname in rows:
        try:
            cr=_dt.fromisoformat(created_at); days=(now-cr).days
        except: continue
        if days<30: continue
        rec=(nomi,vil,aname,qoldiq,days,created_at[:10])
        if days>=90: red.append(rec); sum_r+=qoldiq
        elif days>=60: orange.append(rec); sum_o+=qoldiq
        else: yellow.append(rec); sum_y+=qoldiq
    for lst in (red,orange,yellow): lst.sort(key=lambda x:-x[3])
    title="💸 ESKI NASIYALAR" + ("" if scope_agent_id else " (BARCHA AGENTLAR)")
    total_sum=sum_y+sum_o+sum_r
    text=(f"{title}\n{'━'*26}\n"
          f"🔴 90+ kun: {len(red)} ta — {fmt(sum_r)}\n"
          f"🟠 60-90 kun: {len(orange)} ta — {fmt(sum_o)}\n"
          f"🟡 30-60 kun: {len(yellow)} ta — {fmt(sum_y)}\n"
          f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
          f"💰 Jami muddatli qarz: {fmt(total_sum)}\n")
    def _fmt_block(emoji, label, items, limit=15):
        if not items: return ""
        s=f"\n{'━'*26}\n{emoji} {label} ({len(items)} ta):\n"
        for nomi,vil,aname,qoldiq,days,sana in items[:limit]:
            if scope_agent_id:
                s+=f"  • {nomi} ({vil or '—'}) — {fmt(qoldiq)} | {days} kun ({sana})\n"
            else:
                s+=f"  • {nomi} ({aname}) — {fmt(qoldiq)} | {days} kun\n"
        if len(items)>limit:
            s+=f"  … +{len(items)-limit} ta\n"
        return s
    text+=_fmt_block("🔴","KRITIK (90+ kun)",red)
    text+=_fmt_block("🟠","XAVFLI (60-90 kun)",orange)
    text+=_fmt_block("🟡","DIQQAT (30-60 kun)",yellow)
    if len(red)+len(orange)+len(yellow)==0:
        text+=f"\n{'━'*26}\n✅ Eski nasiya yo'q! Hammasi yangi yoki to'langan."
    return text, len(red)+len(orange)+len(yellow), total_sum

@bot.message_handler(func=lambda m:m.text=="💸 Eski nasiyalar")
def eski_nasiyalar(msg):
    uid=msg.from_user.id
    if not is_admin(uid):
        # Agent — only own
        user=get_user(uid)
        if not user or user[3] not in ("agent","supervisor"): return
        text,_,_=_build_old_nasiya_report(scope_agent_id=uid)
        _send_long(uid, text); return
    text,_,_=_build_old_nasiya_report()
    _send_long(uid, text)

def send_weekly_old_nasiya_alert():
    """Cron: dushanba 09:30 — adminlar va har bir agentga muddatli nasiyalar."""
    # Adminlarga umumiy
    text,total,_=_build_old_nasiya_report()
    if total>0:
        header="💸 HAFTALIK NASIYA OGOHLANTIRISH\n\n"
        for aid in all_admin_ids():
            _send_long(aid, header+text)
    # Har agentga o'zinikini
    conn=get_db();c=conn.cursor()
    c.execute("SELECT DISTINCT agent_id FROM nasiya WHERE qoldiq>0")
    agent_ids=[r[0] for r in c.fetchall()]
    conn.close()
    for aid in agent_ids:
        atext,atotal,_=_build_old_nasiya_report(scope_agent_id=aid)
        if atotal>0:
            try: _send_long(aid, "💸 SIZNING MUDDATLI NASIYALARINGIZ\n\n"+atext)
            except: pass

# ───────────── OYLIK REYTING ─────────────
def _build_monthly_rating(oy=None):
    """Returns text for monthly top-agents rating."""
    if oy is None: oy=datetime.now().strftime("%Y-%m")
    conn=get_db();c=conn.cursor()
    c.execute("""SELECT u.telegram_id,u.name,u.viloyat,
                        COALESCE(SUM(s.jami_summa),0) as savdo,
                        COUNT(DISTINCT s.id) as savdo_n
                 FROM users u
                 LEFT JOIN savdolar s ON s.agent_id=u.telegram_id AND substr(s.created_at,1,7)=?
                 WHERE u.role IN ('agent','supervisor')
                 GROUP BY u.telegram_id ORDER BY savdo DESC""",(oy,))
    by_savdo=c.fetchall()
    c.execute("""SELECT u.telegram_id,u.name,COUNT(d.id) as dn
                 FROM users u
                 LEFT JOIN dokonlar d ON d.agent_id=u.telegram_id AND substr(d.created_at,1,7)=?
                 WHERE u.role IN ('agent','supervisor')
                 GROUP BY u.telegram_id ORDER BY dn DESC""",(oy,))
    by_dokon=c.fetchall()
    c.execute("""SELECT u.telegram_id,u.name,COALESCE(SUM(p.summa),0) as inkasso
                 FROM users u
                 LEFT JOIN pul_olish p ON p.agent_id=u.telegram_id AND substr(p.created_at,1,7)=?
                 WHERE u.role IN ('agent','supervisor')
                 GROUP BY u.telegram_id ORDER BY inkasso DESC""",(oy,))
    by_inkasso=c.fetchall()
    conn.close()
    medals=["🥇","🥈","🥉"]
    def _list(rows, value_idx, fmt_fn):
        lines=[]
        for i,r in enumerate(rows[:5]):
            val=r[value_idx]
            if val<=0 and i>=3: break
            med=medals[i] if i<3 else f" {i+1}."
            lines.append(f"  {med} {r[1]} — {fmt_fn(val)}")
        return "\n".join(lines) if lines else "  —"
    text=(f"🏆 OYLIK REYTING\n📅 {oy}\n{'━'*26}\n\n"
          f"💰 TOP SAVDO:\n{_list(by_savdo,3,fmt)}\n\n"
          f"🏪 TOP YANGI DOKON OCHUVCHI:\n{_list(by_dokon,2,lambda v: f'{v} ta')}\n\n"
          f"💵 TOP INKASSO (yig'gan pul):\n{_list(by_inkasso,2,fmt)}\n")
    # Overall winner = #1 in savdo
    if by_savdo and by_savdo[0][3]>0:
        w=by_savdo[0]
        text+=f"\n{'━'*26}\n🎉 OY G'OLIBI: {w[1]} ({w[2] or '—'})\n💰 {fmt(w[3])} | {w[4]} ta savdo"
    return text

@bot.message_handler(func=lambda m:m.text=="🏆 Oylik reyting")
def oylik_reyting(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    _send_long(uid,_build_monthly_rating())

def send_monthly_rating_if_last_day():
    """Cron daily 20:00: if today is last day of month, broadcast rating to all admins+agents."""
    from calendar import monthrange
    now=datetime.now()
    if now.day != monthrange(now.year,now.month)[1]: return
    text=_build_monthly_rating()
    # Send to all admins
    targets=set(all_admin_ids())
    # And to all agents
    conn=get_db();c=conn.cursor()
    c.execute("SELECT telegram_id FROM users WHERE role IN ('agent','supervisor')")
    for (tid,) in c.fetchall():
        if tid: targets.add(tid)
    conn.close()
    for tid in targets:
        try: _send_long(tid,text)
        except: pass

# ───────────── PLAN VS FAKT ─────────────
def get_agent_plan(agent_id, oy=None):
    """Returns (savdo_plan, dokon_plan) for given month (YYYY-MM)."""
    if oy is None: oy=datetime.now().strftime("%Y-%m")
    conn=get_db();c=conn.cursor()
    c.execute("SELECT savdo_plan,dokon_plan FROM agent_plans WHERE agent_id=? AND oy=?",(agent_id,oy))
    r=c.fetchone(); conn.close()
    return (r[0] or 0, r[1] or 0) if r else (0,0)

def get_agent_fakt(agent_id, oy=None):
    """Returns (savdo_fakt, dokon_fakt) - actual monthly sales & new dokons."""
    if oy is None: oy=datetime.now().strftime("%Y-%m")
    conn=get_db();c=conn.cursor()
    c.execute("SELECT COALESCE(SUM(jami_summa),0) FROM savdolar WHERE agent_id=? AND substr(created_at,1,7)=?",(agent_id,oy))
    savdo=c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM dokonlar WHERE agent_id=? AND substr(created_at,1,7)=?",(agent_id,oy))
    dokon=c.fetchone()[0]
    conn.close()
    return savdo, dokon

def _progress_bar(pct, width=10):
    full=int(min(100,max(0,pct))/100*width)
    return "█"*full+"░"*(width-full)

def _plan_status_emoji(pct):
    if pct>=100: return "🏆"
    if pct>=80: return "🟢"
    if pct>=50: return "🟡"
    if pct>=25: return "🟠"
    return "🔴"

def _plan_block(name, savdo_p, dokon_p, savdo_f, dokon_f, with_name=True):
    s=f"👤 {name}\n" if with_name else ""
    if savdo_p>0:
        pct=savdo_f/savdo_p*100
        s+=(f"  💰 Savdo: {fmt(savdo_f)} / {fmt(savdo_p)}\n"
            f"  {_plan_status_emoji(pct)} [{_progress_bar(pct)}] {pct:.0f}%\n")
    else:
        s+=f"  💰 Savdo: {fmt(savdo_f)} (reja qo'yilmagan)\n"
    if dokon_p>0:
        pct=dokon_f/dokon_p*100
        s+=(f"  🏪 Yangi dokon: {dokon_f} / {dokon_p}\n"
            f"  {_plan_status_emoji(pct)} [{_progress_bar(pct)}] {pct:.0f}%\n")
    else:
        s+=f"  🏪 Yangi dokon: {dokon_f} (reja qo'yilmagan)\n"
    return s

@bot.message_handler(func=lambda m:m.text=="🎯 Mening rejam")
def mening_rejam(msg):
    uid=msg.from_user.id; user=get_user(uid)
    if not user: return
    oy=datetime.now().strftime("%Y-%m")
    sp,dp=get_agent_plan(uid,oy)
    sf,df=get_agent_fakt(uid,oy)
    text=f"🎯 MENING REJAM\n📅 {oy}\n{'━'*26}\n\n"+_plan_block(user[2],sp,dp,sf,df,with_name=False)
    if sp==0 and dp==0:
        text+=f"\n💡 Admin sizga hali oylik reja qo'ymagan.\nReja qo'yilgach, bu yerda ko'rasiz."
    else:
        # Days left in month
        from calendar import monthrange
        now=datetime.now()
        days_total=monthrange(now.year,now.month)[1]
        days_left=days_total-now.day+1
        text+=f"\n📅 Oy oxirigacha: {days_left} kun qoldi"
        if sp>0 and sf<sp:
            need=sp-sf; per_day=need/max(1,days_left)
            text+=f"\n💪 Kuniga kerak: {fmt(int(per_day))}"
    bot.send_message(uid,text)

@bot.message_handler(func=lambda m:m.text=="🎯 Reja boshqaruv")
def reja_boshqaruv(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    oy=datetime.now().strftime("%Y-%m")
    conn=get_db();c=conn.cursor()
    c.execute("SELECT telegram_id,name,viloyat FROM users WHERE role IN ('agent','supervisor') ORDER BY name")
    agents=c.fetchall(); conn.close()
    if not agents:
        bot.send_message(uid,"❗ Agentlar yo'q."); return
    text=f"🎯 REJA vs FAKT — BARCHA AGENTLAR\n📅 {oy}\n{'━'*26}\n\n"
    for tid,name,vil in agents:
        sp,dp=get_agent_plan(tid,oy); sf,df=get_agent_fakt(tid,oy)
        text+=_plan_block(f"{name} ({vil or '—'})",sp,dp,sf,df)+"\n"
    text+=f"{'━'*26}\n💡 Agentga reja qo'yish uchun pastdan tanlang:"
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for tid,name,vil in agents:
        kb.add(f"🎯 {tid}||{name}")
    kb.add("❌ Bekor qilish")
    set_state(uid,"plan_agent_select",{"oy":oy})
    _send_long(uid,text)
    bot.send_message(uid,"👤 Reja qo'ymoqchi bo'lgan agentni tanlang:",reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="plan_agent_select")
def s_plan_agent_select(msg):
    uid=msg.from_user.id
    if not msg.text.startswith("🎯 "):
        if msg.text=="❌ Bekor qilish":
            clear_state(uid); user=get_user(uid)
            bot.send_message(uid,"Bekor qilindi",reply_markup=main_kb(user[3]))
        return
    try:
        rest=msg.text[2:].strip()
        tid_str,name=rest.split("||",1); tid=int(tid_str)
    except:
        bot.send_message(uid,"❗ Xato format"); return
    data=get_state(uid)["data"]; oy=data["oy"]
    sp,dp=get_agent_plan(tid,oy)
    set_state(uid,"plan_savdo_input",{"oy":oy,"tid":tid,"name":name})
    txt=(f"👤 {name}\n📅 {oy}\n\n"
         f"Hozirgi reja: 💰 {fmt(sp)} savdo | 🏪 {dp} dokon\n\n"
         f"💰 Yangi SAVDO rejasini kiriting (so'm):\nMasalan: 500000000\nO'zgartirmaslik uchun: 0")
    bot.send_message(uid,txt,reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="plan_savdo_input")
def s_plan_savdo_input(msg):
    uid=msg.from_user.id
    if msg.text=="❌ Bekor qilish":
        clear_state(uid); user=get_user(uid)
        bot.send_message(uid,"Bekor qilindi",reply_markup=main_kb(user[3])); return
    try:
        savdo=int(msg.text.replace(" ","").replace(",",""))
        if savdo<0: raise ValueError
    except:
        bot.send_message(uid,"❗ Faqat raqam kiriting"); return
    data=get_state(uid)["data"]; data["savdo"]=savdo
    set_state(uid,"plan_dokon_input",data)
    bot.send_message(uid,f"✅ Savdo rejasi: {fmt(savdo)}\n\n🏪 Endi YANGI DOKON rejasini kiriting:\nMasalan: 10\nO'zgartirmaslik uchun: 0",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="plan_dokon_input")
def s_plan_dokon_input(msg):
    uid=msg.from_user.id
    if msg.text=="❌ Bekor qilish":
        clear_state(uid); user=get_user(uid)
        bot.send_message(uid,"Bekor qilindi",reply_markup=main_kb(user[3])); return
    try:
        dokon=int(msg.text.replace(" ",""))
        if dokon<0: raise ValueError
    except:
        bot.send_message(uid,"❗ Faqat raqam kiriting"); return
    data=get_state(uid)["data"]
    tid=data["tid"]; name=data["name"]; oy=data["oy"]; savdo=data["savdo"]
    # Determine final values (0 = keep existing)
    cur_sp,cur_dp=get_agent_plan(tid,oy)
    final_sp = savdo if savdo>0 else cur_sp
    final_dp = dokon if dokon>0 else cur_dp
    conn=get_db();c=conn.cursor()
    c.execute("""INSERT INTO agent_plans (agent_id,oy,savdo_plan,dokon_plan,created_at)
                 VALUES (?,?,?,?,?)
                 ON CONFLICT(agent_id,oy) DO UPDATE SET savdo_plan=?, dokon_plan=?""",
              (tid,oy,final_sp,final_dp,datetime.now().isoformat(),final_sp,final_dp))
    conn.commit(); conn.close()
    clear_state(uid); user=get_user(uid)
    bot.send_message(uid,f"✅ {name} uchun {oy} rejasi saqlandi:\n💰 {fmt(final_sp)} savdo\n🏪 {final_dp} dokon",reply_markup=main_kb(user[3]))
    # Notify agent
    try: bot.send_message(tid,f"🎯 Admin sizga {oy} oyiga reja qo'ydi:\n💰 Savdo: {fmt(final_sp)}\n🏪 Yangi dokon: {final_dp}\n\nKo'rish: 🎯 Mening rejam")
    except: pass

def update_dokon_repeat(c, dokon_id, jami_summa):
    """Repeat System: update store stats after each new order."""
    from datetime import datetime as _dt
    today=_dt.now()
    c.execute("SELECT total_orders,repeat_orders,avg_repeat_days,last_order_date,first_order_date FROM dokonlar WHERE id=?",(dokon_id,))
    row=c.fetchone()
    if not row: return
    total,repeat_n,avg,last_d,first_d=row
    total=total or 0; repeat_n=repeat_n or 0; avg=avg or 0.0
    if total==0:
        first_d=today.isoformat()
    else:
        try:
            ld=_dt.fromisoformat(last_d); days=(today-ld).days
            total_repeat_time=avg*repeat_n
            repeat_n+=1
            avg=(total_repeat_time+days)/repeat_n
        except: pass
    total+=1
    c.execute("""UPDATE dokonlar SET first_order_date=COALESCE(first_order_date,?),
                 last_order_date=?, total_orders=?, repeat_orders=?, avg_repeat_days=?,
                 total_sales=COALESCE(total_sales,0)+? WHERE id=?""",
              (first_d,today.isoformat(),total,repeat_n,avg,jami_summa or 0,dokon_id))

def get_store_status(last_order_date, avg_repeat_days):
    """Returns (emoji_label, days_since_last)"""
    from datetime import datetime as _dt
    if not last_order_date: return ("⚪ NEW", None)
    try:
        ld=_dt.fromisoformat(last_order_date)
        days=(_dt.now()-ld).days
    except: return ("⚪ NEW", None)
    avg=avg_repeat_days or 0
    if avg<=0: return ("🟢 HOT" if days<=7 else ("🟡 WARM" if days<=21 else "🔴 COLD"), days)
    if days<=avg: return ("🟢 HOT", days)
    if days<=avg*2: return ("🟡 WARM", days)
    return ("🔴 COLD", days)

def fmt_miq(q):
    try:
        f=float(q)
        return str(int(f)) if f==int(f) else f"{f:g}"
    except: return str(q)

def main_kb(role):
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    if role in("agent","supervisor","admin"):
        kb.add("🏪 Yangi dokon","📦 Tovar berish")
        kb.add("💰 Pul olish","❌ Tovar olmadi")
        kb.add("📋 Qaytib kirish kerak","💳 Nasiya boshqaruv")
        kb.add("🔍 Qidiruv")
    if role in("agent","supervisor"):
        kb.add("🎯 Mening rejam")
    if role in("supervisor","admin"): kb.add("👥 Agentlar statistikasi")
    if role=="admin":
        kb.add("📈 Umumiy stat","🛍 Mahsulotlar")
        kb.add("👥 Mijozlar bazasi","👤 Agent boshqaruv")
        kb.add("📄 Dokonlar PDF","📢 Xabar yuborish")
        kb.add("🔁 Repeat hisoboti","⚠️ Yo'qolayotgan dokonlar")
        kb.add("💸 Eski nasiyalar","🎯 Reja boshqaruv")
        kb.add("🏆 Oylik reyting")
    return kb
def cancel_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("❌ Bekor qilish"); return kb
def skip_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("⏭ O'tkazib yuborish","❌ Bekor qilish"); return kb
def location_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📍 Location yuborish",request_location=True))
    kb.add("❌ Bekor qilish"); return kb
def tolov_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    kb.add("💵 Naqd","💳 Karta")
    kb.add("📝 Nasiya","🔀 Aralash")
    kb.add("❌ Bekor qilish"); return kb
def sabab_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    kb.add("💸 Narx qimmat","📦 Hozir tovari bor")
    kb.add("🏢 Boshqa firma","😕 Sifat yoqmadi")
    kb.add("🚪 Egasi yo'q edi","🕐 Keyin keling dedi")
    kb.add("🚫 Sotilmaydi dedi","📝 Boshqa sabab")
    kb.add("❌ Bekor qilish"); return kb
def viloyat_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=3)
    kb.add("Namangan","Farg'ona","Andijon"); kb.add("❌ Bekor qilish"); return kb

# ── GLOBAL CANCEL — must be the FIRST handler registered ─────
@bot.message_handler(func=lambda m:m.text=="❌ Bekor qilish")
def cancel_h(msg):
    uid=msg.from_user.id; clear_state(uid); user=get_user(uid)
    if user:
        bot.send_message(uid,"❌ Bekor qilindi.",reply_markup=main_kb(user[3]))
    else:
        bot.send_message(uid,"❌ Bekor qilindi.",reply_markup=types.ReplyKeyboardRemove())

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uid=msg.from_user.id; user=get_user(uid)
    if not user:
        set_state(uid,"reg_name")
        bot.send_message(uid,"👋 TOP MART botiga xush kelibsiz!\n\nIsmingizni kiriting:",reply_markup=types.ReplyKeyboardRemove())
    else:
        bot.send_message(uid,f"✅ Xush kelibsiz, {user[2]}!\n🔰 Rol: {user[3].upper()}",reply_markup=main_kb(user[3]))

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="reg_name")
def reg_name(msg):
    uid=msg.from_user.id
    existing=get_user(uid)
    if existing:
        clear_state(uid)
        bot.send_message(uid,f"✅ Xush kelibsiz, {existing[2]}!",reply_markup=main_kb(existing[3])); return
    set_state(uid,"reg_viloyat",{"name":msg.text.strip()})
    bot.send_message(uid,"📍 Viloyatingizni tanlang:",reply_markup=viloyat_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="reg_viloyat")
def reg_viloyat(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    existing=get_user(uid)
    if existing:
        clear_state(uid)
        bot.send_message(uid,f"✅ Xush kelibsiz, {existing[2]}!",reply_markup=main_kb(existing[3])); return
    viloyatlar=["Namangan","Farg'ona","Andijon"]
    if msg.text not in viloyatlar:
        bot.send_message(uid,"❗ Iltimos ro'yxatdan viloyat tanlang:",reply_markup=viloyat_kb()); return
    conn=get_db();c=conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (telegram_id,name,role,viloyat,created_at) VALUES (?,?,?,?,?)",
              (uid,data["name"],"pending",msg.text,datetime.now().isoformat()))
    conn.commit();conn.close();clear_state(uid)
    bot.send_message(uid,f"✅ {data['name']}, ro'yxatdan o'tdingiz!\n\n⏳ Hisobingiz admin tomonidan tasdiqlanishini kuting. Tasdiqlanganingizda xabar olasiz.",reply_markup=types.ReplyKeyboardRemove())
    for aid in all_admin_ids():
        try: bot.send_message(aid,f"🆕 Yangi agent:\n👤 {data['name']}\n📍 {msg.text}\n🆔 {uid}\n\n/approve {uid}\n/supervisor {uid}")
        except: pass

@bot.message_handler(commands=["approve"])
def approve(msg):
    if not is_admin(msg.from_user.id): return
    try:
        tid=int(msg.text.split()[1])
        conn=get_db();c=conn.cursor()
        c.execute("SELECT name,role FROM users WHERE telegram_id=?",(tid,))
        row=c.fetchone()
        if not row: bot.send_message(msg.from_user.id,"❗ Foydalanuvchi topilmadi."); conn.close(); return
        if row[1]!="pending": bot.send_message(msg.from_user.id,f"⚠️ Bu foydalanuvchi allaqachon '{row[1]}' rolida."); conn.close(); return
        c.execute("UPDATE users SET role='agent' WHERE telegram_id=?",(tid,))
        conn.commit();conn.close()
        bot.send_message(tid,"✅ Hisobingiz tasdiqlandi! Endi botdan foydalanishingiz mumkin.\n/start bosing.")
        bot.send_message(msg.from_user.id,f"✅ {row[0]} tasdiqlandi va 'agent' roliga o'tkazildi.")
    except Exception as e: bot.send_message(msg.from_user.id,f"❗ /approve 123456789\n{e}")

@bot.message_handler(commands=["pending"])
def pending_cmd(msg):
    if not is_admin(msg.from_user.id): return
    conn=get_db();c=conn.cursor()
    c.execute("SELECT telegram_id,name,viloyat,created_at FROM users WHERE role='pending' ORDER BY created_at")
    rows=c.fetchall();conn.close()
    if not rows:
        bot.send_message(msg.from_user.id,"✅ Tasdiq kutayotgan agent yo'q."); return
    text=f"⏳ TASDIQ KUTAYOTGANLAR — {len(rows)} ta\n{'━'*28}\n\n"
    for i,(tid,name,viloyat,created_at) in enumerate(rows,1):
        try: dt_str=created_at[:16].replace("T"," ")
        except: dt_str=str(created_at)
        text+=(f"{i}. 👤 {name}\n"
               f"   📍 {viloyat}\n"
               f"   🆔 {tid}\n"
               f"   🕐 {dt_str}\n"
               f"   ✅ /approve {tid}  |  ❌ /reject {tid}\n\n")
    bot.send_message(msg.from_user.id,text)

@bot.message_handler(commands=["reject"])
def reject_cmd(msg):
    if not is_admin(msg.from_user.id): return
    try:
        tid=int(msg.text.split()[1])
        conn=get_db();c=conn.cursor()
        c.execute("SELECT name,role FROM users WHERE telegram_id=?",(tid,))
        row=c.fetchone()
        if not row: bot.send_message(msg.from_user.id,"❗ Foydalanuvchi topilmadi."); conn.close(); return
        if row[1]!="pending": bot.send_message(msg.from_user.id,f"⚠️ Bu foydalanuvchi '{row[1]}' rolida, rad etib bo'lmaydi."); conn.close(); return
        c.execute("DELETE FROM users WHERE telegram_id=?",(tid,))
        conn.commit();conn.close()
        try: bot.send_message(tid,"❌ Afsus, hisobingiz admin tomonidan rad etildi. Muammo bo'lsa, adminга murojaat qiling.")
        except: pass
        bot.send_message(msg.from_user.id,f"🗑 {row[0]} rad etildi va tizimdan o'chirildi.")
    except Exception as e: bot.send_message(msg.from_user.id,f"❗ /reject 123456789\n{e}")

@bot.message_handler(commands=["broadcast"])
def broadcast_start(msg):
    if not is_admin(msg.from_user.id): return
    set_state(msg.from_user.id,"broadcast_text",{})
    bot.send_message(msg.from_user.id,
        "📢 Barcha faol agentlarga yuboriladigan xabarni yozing.\n\n"
        "Matn, emoji, har qanday format qabul qilinadi.\n"
        "Bekor qilish uchun: ❌ Bekor qilish",
        reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="broadcast_text")
def broadcast_text_h(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    text=msg.text.strip()
    conn=get_db();c=conn.cursor()
    c.execute("SELECT telegram_id,name FROM users WHERE role IN ('agent','supervisor')")
    agents=c.fetchall();conn.close()
    clear_state(uid)
    if not agents:
        bot.send_message(uid,"❗ Faol agent yo'q.",reply_markup=main_kb("admin")); return
    sent=0; failed=0
    broadcast_body=(
        f"📢 TOP MART XABARNOMASI\n{'━'*28}\n\n"
        f"{text}\n\n"
        f"{'━'*28}\n"
        f"🏢 TOP MART boshqaruvi")
    for (tid,name) in agents:
        try:
            bot.send_message(tid,broadcast_body)
            sent+=1
        except:
            failed+=1
    bot.send_message(uid,
        f"✅ Xabar yuborildi!\n\n"
        f"📨 Muvaffaqiyatli: {sent} ta\n"
        f"❌ Yetkazilmadi: {failed} ta\n"
        f"👥 Jami: {len(agents)} ta agent",
        reply_markup=main_kb("admin"))

@bot.message_handler(commands=["supervisor"])
def make_sup(msg):
    if not is_admin(msg.from_user.id): return
    try:
        tid=int(msg.text.split()[1])
        conn=get_db();c=conn.cursor()
        c.execute("UPDATE users SET role='supervisor' WHERE telegram_id=?",(tid,))
        conn.commit();conn.close()
        bot.send_message(tid,"✅ Supervisor qildingiz!")
        bot.send_message(msg.from_user.id,"✅ Supervisor qilindi.")
    except Exception as e: bot.send_message(msg.from_user.id,f"❗{e}")

@bot.message_handler(commands=["makeadmin"])
def make_adm(msg):
    if not is_admin(msg.from_user.id): return
    try:
        tid=int(msg.text.split()[1])
        conn=get_db();c=conn.cursor()
        c.execute("UPDATE users SET role='admin' WHERE telegram_id=?",(tid,))
        conn.commit();conn.close()
        bot.send_message(msg.from_user.id,"✅ Admin qilindi.")
    except Exception as e: bot.send_message(msg.from_user.id,f"❗{e}")

@bot.message_handler(commands=["myid"])
def myid(msg): bot.send_message(msg.from_user.id,f"Sizning ID: {msg.from_user.id}")

@bot.message_handler(commands=["eksport","export"])
def eksport(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    import json as _json, datetime as _dt
    conn=get_db();c=conn.cursor()
    tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'").fetchall()]
    dump={"_meta":{"exported_at":_dt.datetime.now().isoformat(),"db_path":DB_PATH,"host":os.uname().nodename}}
    summary=[]
    for t in tables:
        cols=[d[1] for d in c.execute(f"PRAGMA table_info({t})").fetchall()]
        rows=c.execute(f"SELECT * FROM {t}").fetchall()
        dump[t]=[dict(zip(cols,r)) for r in rows]
        summary.append(f"  {t}: {len(rows)} ta")
    conn.close()
    data=_json.dumps(dump,ensure_ascii=False,indent=2,default=str).encode("utf-8")
    fname=f"topmart_backup_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    bio=io.BytesIO(data); bio.name=fname
    bot.send_document(uid,bio,caption=f"📦 To'liq DB backup\n📁 Host: {dump['_meta']['host']}\n🗄 Path: {DB_PATH}\n\n"+"\n".join(summary))

@bot.message_handler(commands=["agents"])
def agents_list(msg):
    if not is_admin(msg.from_user.id): return
    conn=get_db();c=conn.cursor()
    c.execute("""SELECT u.telegram_id,u.name,u.role,u.viloyat,u.created_at,
                        COUNT(DISTINCT d.id) as dokonlar
                 FROM users u
                 LEFT JOIN dokonlar d ON d.agent_id=u.telegram_id AND d.holat='faol'
                 GROUP BY u.telegram_id
                 ORDER BY u.role, u.viloyat, u.name""")
    rows=c.fetchall(); conn.close()
    if not rows: bot.send_message(msg.from_user.id,"❗ Hech qanday foydalanuvchi yo'q."); return
    role_icon={"admin":"🔴","supervisor":"🟡","agent":"🟢"}
    text="👥 Barcha foydalanuvchilar:\n\n"
    for r in rows:
        tid,name,role,viloyat,created_at,dokonlar=r
        icon=role_icon.get(role,"⚪")
        sana=created_at[:10] if created_at else "—"
        text+=f"{icon} {name}\n"
        text+=f"   📍 {viloyat or '—'} | 🔰 {role.upper()}\n"
        text+=f"   🏪 {dokonlar} ta dokon | 🗓 {sana}\n"
        text+=f"   🆔 {tid}\n\n"
    bot.send_message(msg.from_user.id, text)

@bot.message_handler(commands=["deleteagent"])
def delete_agent(msg):
    if not is_admin(msg.from_user.id): return
    parts=msg.text.split()
    if len(parts)<2:
        bot.send_message(msg.from_user.id,
            "❗ Foydalanish:\n/deleteagent <telegram_id>\n\n"
            "Agent ID ni /agents buyrug'i orqali toping.")
        return
    try:
        tid=int(parts[1])
        if tid==msg.from_user.id:
            bot.send_message(msg.from_user.id,"❗ O'zingizni o'chira olmaysiz."); return
        conn=get_db();c=conn.cursor()
        c.execute("SELECT name,role,viloyat FROM users WHERE telegram_id=?",(tid,))
        agent=c.fetchone()
        if not agent:
            conn.close()
            bot.send_message(msg.from_user.id,"❗ Bunday ID li foydalanuvchi topilmadi."); return
        name,role,viloyat=agent
        c.execute("UPDATE dokonlar SET holat='nofaol' WHERE agent_id=?",(tid,))
        deactivated=c.execute("SELECT changes()").fetchone()[0]
        c.execute("DELETE FROM users WHERE telegram_id=?",(tid,))
        conn.commit();conn.close()
        bot.send_message(msg.from_user.id,
            f"✅ Agent o'chirildi!\n\n"
            f"👤 {name}\n"
            f"📍 {viloyat or '—'} | 🔰 {role.upper()}\n"
            f"🏪 {deactivated} ta do'kon nofaol qilindi.\n"
            f"🆔 {tid}")
        try: bot.send_message(tid,"⛔ Sizning akkauntingiz admin tomonidan o'chirildi.")
        except: pass
    except ValueError:
        bot.send_message(msg.from_user.id,"❗ ID raqam bo'lishi kerak.\n/deleteagent 123456789")

@bot.message_handler(commands=["dokonlar"])
def dokonlar_list(msg):
    if not is_admin(msg.from_user.id): return
    conn=get_db();c=conn.cursor()
    c.execute("""SELECT d.nomi,d.egasi,d.telefon,d.viloyat,d.hudud,d.holat,u.name,d.created_at
                 FROM dokonlar d
                 LEFT JOIN users u ON u.telegram_id=d.agent_id
                 ORDER BY d.viloyat, d.nomi""")
    rows=c.fetchall(); conn.close()
    if not rows: bot.send_message(msg.from_user.id,"❗ Hech qanday dokon yo'q."); return
    holat_icon={"faol":"🟢","nofaol":"🔴"}
    viloyat_cur=None; text=""
    for r in rows:
        nomi,egasi,telefon,viloyat,hudud,holat,agent,created_at=r
        if viloyat!=viloyat_cur:
            if text: bot.send_message(msg.from_user.id,text)
            viloyat_cur=viloyat; text=f"📍 {viloyat or '—'} viloyati:\n\n"
        icon=holat_icon.get(holat,"⚪")
        sana=created_at[:10] if created_at else "—"
        text+=f"{icon} {nomi}\n"
        text+=f"   👤 {egasi} | 📞 {telefon or '—'}\n"
        if hudud: text+=f"   🗺 {hudud}\n"
        text+=f"   🧑 Agent: {agent or '—'} | 🗓 {sana}\n\n"
    if text: bot.send_message(msg.from_user.id,text)

@bot.message_handler(commands=["savdolar"])
def savdolar_cmd(msg):
    if not is_admin(msg.from_user.id): return
    conn=get_db();c=conn.cursor()
    bugun=date.today().isoformat(); oy=datetime.now().strftime("%Y-%m")
    c.execute("""SELECT u.name,u.viloyat,
                        COALESCE(SUM(CASE WHEN s.created_at LIKE ? THEN s.jami_summa ELSE 0 END),0) as bugun_savdo,
                        COALESCE(SUM(CASE WHEN s.created_at LIKE ? THEN s.jami_summa ELSE 0 END),0) as oy_savdo,
                        COALESCE(SUM(CASE WHEN p.created_at LIKE ? THEN p.summa ELSE 0 END),0) as bugun_pul,
                        COALESCE(SUM(CASE WHEN p.created_at LIKE ? THEN p.summa ELSE 0 END),0) as oy_pul,
                        COUNT(DISTINCT CASE WHEN s.created_at LIKE ? THEN s.id END) as bugun_n,
                        COUNT(DISTINCT CASE WHEN s.created_at LIKE ? THEN s.id END) as oy_n
                 FROM users u
                 LEFT JOIN savdolar s ON s.agent_id=u.telegram_id
                 LEFT JOIN pul_olish p ON p.agent_id=u.telegram_id
                 WHERE u.role IN ('agent','supervisor')
                 GROUP BY u.telegram_id
                 ORDER BY oy_savdo DESC""",
              (f"{bugun}%",f"{oy}%",f"{bugun}%",f"{oy}%",f"{bugun}%",f"{oy}%"))
    rows=c.fetchall(); conn.close()
    if not rows: bot.send_message(msg.from_user.id,"❗ Agentlar yo'q."); return
    jami_bs=jami_os=jami_bp=jami_op=0
    text=f"📊 Savdolar hisoboti\n🗓 {bugun}\n\n"
    for i,r in enumerate(rows,1):
        name,viloyat,bs,os_,bp,op,bn,on_=r
        jami_bs+=bs; jami_os+=os_; jami_bp+=bp; jami_op+=op
        text+=f"{i}. {name} ({viloyat or '—'})\n"
        text+=f"   📦 Bugun: {fmt(bs)} ({bn} ta)\n"
        text+=f"   💰 Bugun pul: {fmt(bp)}\n"
        text+=f"   📦 Oy: {fmt(os_)} ({on_} ta)\n"
        text+=f"   💰 Oy pul: {fmt(op)}\n\n"
    text+=(f"━━━━━━━━━━━━━━\n"
           f"📦 Jami bugungi savdo: {fmt(jami_bs)}\n"
           f"💰 Jami bugungi pul: {fmt(jami_bp)}\n"
           f"📦 Jami oylik savdo: {fmt(jami_os)}\n"
           f"💰 Jami oylik pul: {fmt(jami_op)}")
    bot.send_message(msg.from_user.id, text)

@bot.message_handler(commands=["export"])
def export_cmd(msg):
    if not is_admin(msg.from_user.id): return
    conn=get_db(); c=conn.cursor()

    # Savdolar sheet
    c.execute("""SELECT s.id, s.created_at, u.name, u.viloyat, d.nomi, d.telefon,
                        s.jami_summa, s.tolov_turi,
                        GROUP_CONCAT(m.nomi||' x'||st.miqdor||' ('||st.summa||')', ' | ') as mahsulotlar
                 FROM savdolar s
                 LEFT JOIN users u ON u.telegram_id=s.agent_id
                 LEFT JOIN dokonlar d ON d.id=s.dokon_id
                 LEFT JOIN savdo_tafsilot st ON st.savdo_id=s.id
                 LEFT JOIN mahsulotlar m ON m.id=st.mahsulot_id
                 GROUP BY s.id
                 ORDER BY s.created_at DESC""")
    savdolar=c.fetchall()

    # Pul olish sheet
    c.execute("""SELECT p.created_at, u.name, u.viloyat, d.nomi, d.telefon, p.summa
                 FROM pul_olish p
                 LEFT JOIN users u ON u.telegram_id=p.agent_id
                 LEFT JOIN dokonlar d ON d.id=p.dokon_id
                 ORDER BY p.created_at DESC""")
    pullar=c.fetchall()

    # Olmagan dokonlar sheet
    c.execute("""SELECT o.created_at, u.name, u.viloyat, d.nomi, d.telefon,
                        o.sabab_text, o.qaytish_sanasi,
                        CASE WHEN o.bajarildi=1 THEN 'Ha' ELSE 'Yoq' END
                 FROM olmagan_dokonlar o
                 LEFT JOIN users u ON u.telegram_id=o.agent_id
                 LEFT JOIN dokonlar d ON d.id=o.dokon_id
                 ORDER BY o.created_at DESC""")
    olmagan=c.fetchall()
    conn.close()

    out=io.StringIO()
    w=csv.writer(out)

    w.writerow(["=== SAVDOLAR ==="])
    w.writerow(["#","Sana","Agent","Viloyat","Dokon","Telefon","Jami summa","Tolov turi","Mahsulotlar"])
    for r in savdolar: w.writerow(r)

    w.writerow([])
    w.writerow(["=== PUL OLISH ==="])
    w.writerow(["Sana","Agent","Viloyat","Dokon","Telefon","Summa"])
    for r in pullar: w.writerow(r)

    w.writerow([])
    w.writerow(["=== TOVAR OLMAGAN DOKONLAR ==="])
    w.writerow(["Sana","Agent","Viloyat","Dokon","Telefon","Sabab","Qaytish sanasi","Bajarildi"])
    for r in olmagan: w.writerow(r)

    out.seek(0)
    filename=f"topmart_export_{date.today().isoformat()}.csv"
    bot.send_document(msg.from_user.id,
        (filename, out.getvalue().encode("utf-8-sig")),
        caption=f"📊 TOP MART ma'lumotlar bazasi\n🗓 {date.today().isoformat()}\n\n"
                f"• Savdolar: {len(savdolar)} ta\n"
                f"• Pul olish: {len(pullar)} ta\n"
                f"• Olmagan dokonlar: {len(olmagan)} ta")

@bot.message_handler(commands=["addproduct"])
def add_prod(msg):
    if not is_admin(msg.from_user.id): return
    try:
        t=msg.text.replace("/addproduct","").strip().split("|")
        nomi,narx,birlik=t[0].strip(),int(t[1].strip()),t[2].strip()
        conn=get_db();c=conn.cursor()
        c.execute("INSERT INTO mahsulotlar (nomi,narx,birlik) VALUES (?,?,?)",(nomi,narx,birlik))
        conn.commit();conn.close()
        bot.send_message(msg.from_user.id,f"✅ {nomi} — {fmt(narx)}/{birlik}")
    except: bot.send_message(msg.from_user.id,"❗ /addproduct Arqon 5mm|35000|dona")

@bot.message_handler(commands=["updateprice"])
def upd_price(msg):
    if not is_admin(msg.from_user.id): return
    try:
        p=msg.text.split()[1].split("|"); mid,narx=int(p[0]),int(p[1])
        conn=get_db();c=conn.cursor()
        c.execute("UPDATE mahsulotlar SET narx=? WHERE id=?",(narx,mid))
        conn.commit();conn.close()
        bot.send_message(msg.from_user.id,f"✅ #{mid}: {fmt(narx)}")
    except: bot.send_message(msg.from_user.id,"❗ /updateprice 1|40000")

@bot.message_handler(commands=["delproduct"])
def del_prod(msg):
    if not is_admin(msg.from_user.id): return
    try:
        mid=int(msg.text.split()[1])
        conn=get_db();c=conn.cursor()
        c.execute("UPDATE mahsulotlar SET faol=0 WHERE id=?",(mid,))
        conn.commit();conn.close()
        bot.send_message(msg.from_user.id,f"✅ #{mid} o'chirildi.")
    except: bot.send_message(msg.from_user.id,"❗ /delproduct 1")

@bot.message_handler(func=lambda m:m.text=="🏪 Yangi dokon")
def yangi_dokon(msg):
    uid=msg.from_user.id; user=get_user(uid)
    if not user: return
    if check_pending(uid): return
    set_state(uid,"dokon_nomi",{})
    bot.send_message(uid,"🏪 Dokon nomini kiriting:",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="dokon_nomi")
def s_dokon_nomi(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["nomi"]=msg.text.strip(); set_state(uid,"dokon_egasi",data)
    bot.send_message(uid,"👤 Dokon egasining ismi:")

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="dokon_egasi")
def s_dokon_egasi(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["egasi"]=msg.text.strip(); set_state(uid,"dokon_telefon",data)
    bot.send_message(uid,"📞 Telefon raqami:")

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="dokon_telefon")
def s_dokon_tel(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["telefon"]=msg.text.strip(); set_state(uid,"dokon_owner_tg",data)
    bot.send_message(uid,"📱 Dokon egasi Telegram da botga ulangan? /start bosgan bo'lsa ID si:\n(O'tkazib yuborish mumkin)",reply_markup=skip_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="dokon_owner_tg")
def s_dokon_owner_tg(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text=="⏭ O'tkazib yuborish":
        data["owner_telegram_id"]=None
    else:
        try: data["owner_telegram_id"]=int(msg.text.strip())
        except: data["owner_telegram_id"]=None
    set_state(uid,"dokon_hudud",data)
    bot.send_message(uid,"🗺 Hudud/ko'cha (ixtiyoriy):",reply_markup=skip_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="dokon_hudud")
def s_dokon_hudud(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["hudud"]="" if msg.text=="⏭ O'tkazib yuborish" else msg.text.strip()
    set_state(uid,"dokon_location",data)
    bot.send_message(uid,"📍 Location yuboring:",reply_markup=location_kb())

@bot.message_handler(content_types=["location"],func=lambda m:get_state(m.from_user.id)["state"]=="dokon_location")
def s_dokon_loc(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["lat"]=msg.location.latitude; data["lon"]=msg.location.longitude
    set_state(uid,"dokon_foto",data)
    bot.send_message(uid,"📸 Dokon rasmini yuboring:",reply_markup=skip_kb())

@bot.message_handler(content_types=["photo"],func=lambda m:get_state(m.from_user.id)["state"]=="dokon_foto")
def s_dokon_foto_p(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["foto"]=msg.photo[-1].file_id; _save_dokon(uid,data)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="dokon_foto")
def s_dokon_foto_s(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["foto"]=None; _save_dokon(uid,data)

def _save_dokon(uid,data):
    user=get_user(uid); conn=get_db(); c=conn.cursor()
    c.execute("INSERT INTO dokonlar (nomi,egasi,telefon,viloyat,hudud,latitude,longitude,foto,agent_id,created_at,owner_telegram_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (data["nomi"],data["egasi"],data["telefon"],user[4],data.get("hudud",""),data.get("lat"),data.get("lon"),data.get("foto"),uid,datetime.now().isoformat(),data.get("owner_telegram_id")))
    conn.commit();conn.close();clear_state(uid)
    owner_note=f"\n📱 Egasi TG: {data['owner_telegram_id']}" if data.get("owner_telegram_id") else ""
    bot.send_message(uid,f"✅ Dokon saqlandi!\n🏪 {data['nomi']}\n👤 {data['egasi']}\n📞 {data['telefon']}{owner_note}",reply_markup=main_kb(user[3]))
    lat=data.get("lat"); lon=data.get("lon")
    maps_link=f"\n🗺 https://maps.google.com/?q={lat},{lon}" if lat and lon else ""
    notif_text=(f"🏪 Yangi dokon qo'shildi!\n\n"
                f"👤 Agent: {user[2]}\n"
                f"📍 Viloyat: {user[4]}\n"
                f"📌 Hudud: {data.get('hudud','—') or '—'}\n\n"
                f"🏪 Dokon: {data['nomi']}\n"
                f"👤 Egasi: {data['egasi']}\n"
                f"📞 Telefon: {data['telefon']}{owner_note}{maps_link}")
    foto_id=data.get("foto")
    for aid in all_admin_ids():
        try:
            if foto_id: bot.send_photo(aid, foto_id, caption=notif_text)
            else: bot.send_message(aid, notif_text)
        except: pass
    # Channel notification (optional via env var)
    channel=os.environ.get("NEW_DOKON_CHANNEL_ID","").strip()
    if channel:
        try: ch_target=int(channel)
        except: ch_target=channel  # @username
        try:
            if foto_id: bot.send_photo(ch_target, foto_id, caption=notif_text)
            else: bot.send_message(ch_target, notif_text)
            if lat and lon:
                try: bot.send_location(ch_target, lat, lon)
                except: pass
        except Exception as e:
            for aid in all_admin_ids():
                try: bot.send_message(aid, f"⚠️ Kanalga yuborib bo'lmadi ({channel}): {e}")
                except: pass
    if data.get("owner_telegram_id"):
        try:
            bot.send_message(data["owner_telegram_id"],
                f"👋 Salom! Siz TOP MART tizimiga ulandingiz.\n"
                f"🏪 Dokoningiz: {data['nomi']}\n"
                f"Endi har bir savdodan chek olasiz.")
        except: pass

def _mah_list_kb(mahsulotlar, tanlangan):
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for i,(mid,nomi,narx,birlik) in enumerate(mahsulotlar,1):
        miqdor=tanlangan.get(mid,0)
        mark=f" ✅ ×{fmt_miq(miqdor)}" if miqdor>0 else ""
        kb.add(f"{i}. {nomi} — {fmt(narx)}/{birlik}{mark}")
    kb.add("❌ Bekor qilish")
    return kb

def _next_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    kb.add("➕ Yana mahsulot qo'shish")
    kb.add("✅ Savdoni yakunlash")
    kb.add("❌ Bekor qilish")
    return kb

@bot.message_handler(func=lambda m:m.text=="📦 Tovar berish")
def tovar_berish(msg):
    uid=msg.from_user.id; user=get_user(uid)
    if not user: return
    if check_pending(uid): return
    conn=get_db();c=conn.cursor()
    if is_admin(uid):
        c.execute("SELECT id,nomi FROM dokonlar WHERE holat='faol' ORDER BY nomi")
    else:
        c.execute("SELECT id,nomi FROM dokonlar WHERE agent_id=? AND holat='faol' ORDER BY nomi",(uid,))
    dokonlar=c.fetchall()
    c.execute("SELECT id,nomi,narx,birlik FROM mahsulotlar WHERE faol=1 ORDER BY nomi")
    mahsulotlar=c.fetchall(); conn.close()
    if not dokonlar: bot.send_message(uid,"❗ Faol dokon yo'q."); return
    if not mahsulotlar: bot.send_message(uid,"❗ Mahsulotlar yo'q."); return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for d in dokonlar: kb.add(f"🏪 {d[0]}||{d[1]}")
    kb.add("❌ Bekor qilish")
    set_state(uid,"savdo_dokon",{"mahsulotlar":mahsulotlar,"tanlangan":{}})
    bot.send_message(uid,"🏪 Dokonni tanlang:",reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_dokon")
def s_savdo_dokon(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if not msg.text.startswith("🏪 "): return
    try:
        did,dnomi=msg.text.replace("🏪 ","").split("||",1)
        data["dokon_id"]=int(did); data["dokon_nomi"]=dnomi
    except: return
    set_state(uid,"savdo_pick_mah",data)
    bot.send_message(uid,
        f"🏪 {data['dokon_nomi']}\n\n📦 Mahsulot tanlang:",
        reply_markup=_mah_list_kb(data["mahsulotlar"],data["tanlangan"]))

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_pick_mah")
def s_savdo_pick_mah(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    mahsulotlar=data["mahsulotlar"]
    for i,(mid,nomi,narx,birlik) in enumerate(mahsulotlar,1):
        if msg.text.startswith(f"{i}. "):
            data["cur_mid"]=mid; data["cur_nomi"]=nomi
            data["cur_narx"]=narx; data["cur_birlik"]=birlik
            set_state(uid,"savdo_miqdor",data)
            bot.send_message(uid,
                f"📦 {nomi}\n💰 Narx: {fmt(narx)}/{birlik}\n\nNechta?",
                reply_markup=cancel_kb())
            return

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_miqdor")
def s_savdo_miqdor(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    try:
        miqdor=float(msg.text.strip().replace(",","."))
        if miqdor<=0: raise ValueError
    except:
        bot.send_message(uid,"❗ Iltimos, musbat son kiriting (masalan: 1.5):"); return
    mid=data["cur_mid"]; nomi=data["cur_nomi"]
    narx=data["cur_narx"]; birlik=data["cur_birlik"]
    prev=data["tanlangan"].get(mid,0)
    yangi=prev+miqdor
    data["tanlangan"][mid]=yangi
    total_line=fmt(narx*yangi)
    set_state(uid,"savdo_next",data)
    bot.send_message(uid,
        f"✅ Qo'shildi: {nomi} ×{fmt_miq(yangi)} {birlik} × {fmt(narx)} = {total_line}\n\n"
        f"Nima qilasiz?",
        reply_markup=_next_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_next")
def s_savdo_next(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text=="➕ Yana mahsulot qo'shish":
        set_state(uid,"savdo_pick_mah",data)
        bot.send_message(uid,"📦 Mahsulot tanlang:",
            reply_markup=_mah_list_kb(data["mahsulotlar"],data["tanlangan"]))
    elif msg.text=="✅ Savdoni yakunlash":
        tanlangan=data["tanlangan"]; mahsulotlar=data["mahsulotlar"]
        lines=[]; jami=0
        for mid,nomi,narx,birlik in mahsulotlar:
            miqdor=tanlangan.get(mid,0)
            if miqdor>0:
                summa=narx*miqdor; jami+=summa
                lines.append(f"  • {nomi}\n     {fmt_miq(miqdor)} {birlik} × {fmt(narx)} = {fmt(summa)}")
        if not lines:
            bot.send_message(uid,"❗ Hech narsa tanlanmadi!"); return
        summary=(f"🧾 BUYURTMA XULOSASI\n{'━'*24}\n"
                 f"🏪 {data['dokon_nomi']}\n\n"
                 +"\n".join(lines)+
                 f"\n{'━'*24}\n💰 Jami: {fmt(jami)}\n\n"
                 f"💳 To'lov turini tanlang:")
        set_state(uid,"savdo_tolov",data)
        bot.send_message(uid,summary,reply_markup=tolov_kb())

def _go_foto(uid,data):
    set_state(uid,"savdo_foto",data)
    bot.send_message(uid,"📸 Chek rasmini yuboring:",reply_markup=skip_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_tolov")
def s_savdo_tolov(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]; t=msg.text
    if "Naqd" in t: data["tolov"]="naqd"; _go_foto(uid,data)
    elif "Karta" in t: data["tolov"]="karta"; _go_foto(uid,data)
    elif "Nasiya" in t: data["tolov"]="nasiya"; _go_foto(uid,data)
    elif "Aralash" in t:
        data["tolov"]="aralash"; data["naqd"]=0; data["karta"]=0; data["nasiya_qism"]=0
        set_state(uid,"savdo_aralash_naqd",data)
        bot.send_message(uid,"💵 Naqd qancha? (0 bo'lsa 0 kiriting):",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_aralash_naqd")
def s_aralash_naqd(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    try: data["naqd"]=int(msg.text.replace(" ","").replace(",",""))
    except: bot.send_message(uid,"❗ Raqam kiriting:"); return
    set_state(uid,"savdo_aralash_karta",data)
    bot.send_message(uid,"💳 Karta qancha? (0 bo'lsa 0 kiriting):",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_aralash_karta")
def s_aralash_karta(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    try: data["karta"]=int(msg.text.replace(" ","").replace(",",""))
    except: bot.send_message(uid,"❗ Raqam kiriting:"); return
    set_state(uid,"savdo_aralash_nasiya",data)
    bot.send_message(uid,"📝 Nasiya qancha? (0 bo'lsa 0 kiriting):",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_aralash_nasiya")
def s_aralash_nasiya_h(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    try: data["nasiya_qism"]=int(msg.text.replace(" ","").replace(",",""))
    except: bot.send_message(uid,"❗ Raqam kiriting:"); return
    jami=sum(m[2]*data["tanlangan"].get(m[0],0) for m in data["mahsulotlar"])
    n=data["naqd"]; k=data["karta"]; nas=data["nasiya_qism"]
    total=n+k+nas; diff=jami-total
    warn=""
    if diff>0: warn=f"\n⚠️ {fmt(diff)} kam kiritildi!"
    elif diff<0: warn=f"\n⚠️ {fmt(-diff)} ko'p kiritildi!"
    summary=(f"🔀 ARALASH TO'LOV\n{'━'*24}\n"
             f"💵 Naqd:   {fmt(n)}\n"
             f"💳 Karta:  {fmt(k)}\n"
             f"📝 Nasiya: {fmt(nas)}\n"
             f"{'━'*24}\n"
             f"💰 Savdo jami: {fmt(jami)}{warn}\n\n"
             f"Tasdiqlaysizmi?")
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    kb.add("✅ Tasdiqlash","🔄 Qayta kiritish"); kb.add("❌ Bekor qilish")
    set_state(uid,"savdo_aralash_tasdiq",data)
    bot.send_message(uid,summary,reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_aralash_tasdiq")
def s_aralash_tasdiq(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text=="✅ Tasdiqlash": _go_foto(uid,data)
    elif msg.text=="🔄 Qayta kiritish":
        data["naqd"]=0; data["karta"]=0; data["nasiya_qism"]=0
        set_state(uid,"savdo_aralash_naqd",data)
        bot.send_message(uid,"💵 Naqd qancha? (0 bo'lsa 0 kiriting):",reply_markup=cancel_kb())

@bot.message_handler(content_types=["photo"],func=lambda m:get_state(m.from_user.id)["state"]=="savdo_foto")
def s_savdo_foto_p(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["foto"]=msg.photo[-1].file_id; _check_balans_before_save(uid,data)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_foto")
def s_savdo_foto_s(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["foto"]=None; _check_balans_before_save(uid,data)

def _check_balans_before_save(uid,data):
    did=data["dokon_id"]; tolov=data["tolov"]
    balans=get_balans(did)
    if balans>0 and tolov=="nasiya":
        jami=sum(m[2]*data["tanlangan"].get(m[0],0) for m in data["mahsulotlar"])
        deducted=min(balans,jami); yangi_balans=balans-deducted
        conn=get_db();c=conn.cursor()
        update_balans_delta(c,did,-deducted)
        conn.commit();conn.close()
        data["balans_ishlatildi"]=deducted; data["yangi_balans"]=yangi_balans
        bot.send_message(uid,f"✅ {fmt(deducted)} so'm balans nasiyadan ayirildi.\nQolgan balans: {fmt(yangi_balans)}")
        _save_savdo(uid,data)
    elif balans>0 and tolov=="aralash" and data.get("nasiya_qism",0)>0:
        nas=data["nasiya_qism"]; deducted=min(balans,nas); yangi_balans=balans-deducted
        conn=get_db();c=conn.cursor()
        update_balans_delta(c,did,-deducted)
        conn.commit();conn.close()
        data["nasiya_qism"]=nas-deducted
        data["balans_ishlatildi"]=deducted; data["yangi_balans"]=yangi_balans
        bot.send_message(uid,f"✅ {fmt(deducted)} so'm balans nasiyadan ayirildi.\nQolgan balans: {fmt(yangi_balans)}")
        _save_savdo(uid,data)
    elif balans>0 and tolov in("naqd","karta"):
        data["mavjud_balans"]=balans
        set_state(uid,"savdo_balans_confirm",data)
        kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
        kb.add("✅ Ha, ayirish","❌ Yo'q, to'liq to'lov")
        bot.send_message(uid,
            f"💰 Bu mijozda {fmt(balans)} so'm ortiqcha pul bor.\n"
            f"Tovar summasidan ayirilsinmi?",reply_markup=kb)
    else:
        _save_savdo(uid,data)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="savdo_balans_confirm")
def s_savdo_balans_confirm(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    balans=data["mavjud_balans"]
    if msg.text=="✅ Ha, ayirish":
        jami=sum(m[2]*data["tanlangan"].get(m[0],0) for m in data["mahsulotlar"])
        deducted=min(balans,jami); yangi_balans=balans-deducted
        conn=get_db();c=conn.cursor()
        update_balans_delta(c,data["dokon_id"],-deducted)
        conn.commit();conn.close()
        data["balans_ishlatildi"]=deducted; data["yangi_balans"]=yangi_balans
        _save_savdo(uid,data)
    elif msg.text=="❌ Yo'q, to'liq to'lov":
        _save_savdo(uid,data)

TOLOV_LABEL={"naqd":"💵 Naqd","karta":"💳 Karta","nasiya":"📝 Nasiya","aralash":"🔀 Aralash"}

def _tolov_info_str(data):
    tolov=data["tolov"]
    if tolov=="aralash":
        return (f"\n💵 Naqd: {fmt(data.get('naqd',0))}"
                f"\n💳 Karta: {fmt(data.get('karta',0))}"
                f"\n📝 Nasiya: {fmt(data.get('nasiya_qism',0))}")
    return f"\n{TOLOV_LABEL.get(tolov,tolov)}"

def _save_savdo(uid,data):
    user=get_user(uid); conn=get_db(); c=conn.cursor()
    jami=sum(m[2]*data["tanlangan"].get(m[0],0) for m in data["mahsulotlar"])
    tolov=data["tolov"]; now=datetime.now().isoformat()
    c.execute("INSERT INTO savdolar (dokon_id,agent_id,jami_summa,tolov_turi,foto,created_at) VALUES (?,?,?,?,?,?)",
              (data["dokon_id"],uid,jami,tolov,data.get("foto"),now))
    sid=c.lastrowid; lines=[]
    update_dokon_repeat(c, data["dokon_id"], jami)
    # Qayta kirish workflow: schedule a revisit N days later (default 7)
    try:
        rdays=int(os.environ.get("REVISIT_DAYS","7"))
        revisit_date=(date.today()+timedelta(days=rdays)).isoformat()
        # Cancel any earlier pending revisit for this dokon — replace with the latest
        c.execute("UPDATE revisitlar SET status='superseded' WHERE dokon_id=? AND status='pending'",(data["dokon_id"],))
        c.execute("INSERT INTO revisitlar (dokon_id,agent_id,last_order_date,revisit_date,status,created_at) VALUES (?,?,?,?,?,?)",
                  (data["dokon_id"],uid,date.today().isoformat(),revisit_date,"pending",now))
    except Exception as _e: pass
    for m in data["mahsulotlar"]:
        mid,nomi,narx,birlik=m; miqdor=data["tanlangan"].get(mid,0)
        if miqdor>0:
            c.execute("INSERT INTO savdo_tafsilot (savdo_id,mahsulot_id,miqdor,narx,summa) VALUES (?,?,?,?,?)",(sid,mid,miqdor,narx,narx*miqdor))
            lines.append(f"  • {nomi}\n     {fmt_miq(miqdor)} {birlik} × {fmt(narx)} = {fmt(narx*miqdor)}")
    nasiya_summa=0
    balans_ishlatildi=data.get("balans_ishlatildi",0)
    if tolov=="nasiya": nasiya_summa=max(0,jami-balans_ishlatildi)
    elif tolov=="aralash": nasiya_summa=data.get("nasiya_qism",0)
    if nasiya_summa>0:
        c.execute("INSERT INTO nasiya (dokon_id,agent_id,savdo_id,jami_summa,tolangan,qoldiq,created_at,updated_at) VALUES (?,?,?,?,0,?,?,?)",
                  (data["dokon_id"],uid,sid,nasiya_summa,nasiya_summa,now,now))
    # Fetch owner telegram id and store's total remaining nasiya for receipt
    c.execute("SELECT owner_telegram_id FROM dokonlar WHERE id=?",(data["dokon_id"],))
    row=c.fetchone(); owner_tg=row[0] if row else None
    c.execute("SELECT COALESCE(SUM(qoldiq),0) FROM nasiya WHERE dokon_id=? AND qoldiq>0",(data["dokon_id"],))
    jami_nasiya_qoldiq=c.fetchone()[0]
    conn.commit();conn.close();clear_state(uid)
    tolov_str=_tolov_info_str(data)
    foto_id=data.get("foto")
    yangi_balans=data.get("yangi_balans",None)
    balans_line=""
    if balans_ishlatildi>0:
        balans_line=f"\n💰 Balans ishlatildi: -{fmt(balans_ishlatildi)}"
        if yangi_balans is not None:
            balans_line+=f"\n💳 Qolgan balans: {fmt(yangi_balans)}"
    bot.send_message(uid,"✅ Savdo saqlandi!\n\n🏪 "+data["dokon_nomi"]+"\n"+"\n".join(lines)+f"\n\n💰 Jami: {fmt(jami)}"+tolov_str+balans_line,reply_markup=main_kb(user[3]))
    # Admin notification — forward photo if present
    admin_text=(f"📦 Yangi savdo!\n\n"
                f"👤 Agent: {user[2]}\n"
                f"📍 Viloyat: {user[4]}\n"
                f"🏪 Dokon: {data['dokon_nomi']}\n\n"
                f"🛍 Mahsulotlar:\n"+"\n".join(lines)+
                f"\n\n💰 Jami: {fmt(jami)}"+tolov_str+balans_line)
    try:
        if foto_id:
            for aid in all_admin_ids():
                try: bot.send_photo(aid,foto_id,caption=admin_text)
                except: pass
        else:
            for aid in all_admin_ids():
                try: bot.send_message(aid,admin_text)
                except: pass
    except: pass
    # Owner receipt
    if owner_tg:
        nasiya_line=""
        if nasiya_summa>0:
            nasiya_line=(f"\n📝 Nasiya: {fmt(nasiya_summa)}"
                         f"\n🔴 Umumiy nasiya qoldig'i: {fmt(jami_nasiya_qoldiq)}")
        receipt=(f"🧾 SAVDO CHEKI\n{'━'*26}\n"
                 f"🏪 Dokon: {data['dokon_nomi']}\n"
                 f"📅 Sana: {now[:10]}\n\n"
                 f"🛍 Mahsulotlar:\n"+"\n".join(lines)+
                 f"\n\n💰 Jami: {fmt(jami)}"+tolov_str+nasiya_line+balans_line)
        try: bot.send_message(owner_tg,receipt)
        except: pass
        if balans_ishlatildi>0:
            try: bot.send_message(owner_tg,f"✅ {fmt(balans_ishlatildi)} so'm balans ishlatildi.\nQolgan balans: {fmt(yangi_balans or 0)}")
            except: pass

@bot.message_handler(func=lambda m:m.text=="💰 Pul olish")
def pul_olish(msg):
    uid=msg.from_user.id; user=get_user(uid)
    if not user: return
    if check_pending(uid): return
    conn=get_db();c=conn.cursor()
    if is_admin(uid):
        c.execute("SELECT id,nomi FROM dokonlar WHERE holat='faol' ORDER BY nomi")
    else:
        c.execute("SELECT id,nomi FROM dokonlar WHERE agent_id=? AND holat='faol' ORDER BY nomi",(uid,))
    dokonlar=c.fetchall(); conn.close()
    if not dokonlar: bot.send_message(uid,"❗ Faol dokon yo'q."); return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for d in dokonlar: kb.add(f"🏪 {d[0]}||{d[1]}")
    kb.add("❌ Bekor qilish")
    set_state(uid,"pul_dokon",{})
    bot.send_message(uid,"🏪 Dokonni tanlang:",reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="pul_dokon")
def s_pul_dokon(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if not msg.text.startswith("🏪 "): return
    try:
        did,dnomi=msg.text.replace("🏪 ","").split("||",1)
        data["dokon_id"]=int(did); data["dokon_nomi"]=dnomi
    except: return
    conn=get_db();c=conn.cursor()
    c.execute("SELECT COALESCE(SUM(qoldiq),0) FROM nasiya WHERE dokon_id=? AND agent_id=? AND qoldiq>0",(int(did),uid))
    nasiya_qoldiq=c.fetchone()[0]; conn.close()
    if nasiya_qoldiq>0:
        data["nasiya_qoldiq"]=nasiya_qoldiq
        set_state(uid,"pul_nasiya_choice",data)
        kb2=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
        kb2.add("✅ Ha, nasiyaga hisoblash","💰 Yo'q, oddiy pul olish","❌ Bekor qilish")
        bot.send_message(uid,
            f"🏪 {dnomi}\n"
            f"🔴 Joriy nasiya: {fmt(nasiya_qoldiq)}\n\n"
            f"Bu to'lov nasiyaga hisoblansinmi?",
            reply_markup=kb2)
    else:
        set_state(uid,"pul_summa",data)
        bot.send_message(uid,f"💰 {dnomi}\nQancha pul oldingiz?",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="pul_nasiya_choice")
def s_pul_nasiya_choice(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text=="✅ Ha, nasiyaga hisoblash":
        set_state(uid,"pul_nasiya_summa",data)
        bot.send_message(uid,
            f"🏪 {data['dokon_nomi']}\n"
            f"🔴 Nasiya qoldiq: {fmt(data['nasiya_qoldiq'])}\n\n"
            f"Qancha pul oldingiz?",
            reply_markup=cancel_kb())
    elif msg.text=="💰 Yo'q, oddiy pul olish":
        set_state(uid,"pul_summa",data)
        bot.send_message(uid,f"💰 {data['dokon_nomi']}\nQancha pul oldingiz?",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="pul_nasiya_summa")
def s_pul_nasiya_summa(msg):
    uid=msg.from_user.id; user=get_user(uid); data=get_state(uid)["data"]
    try:
        summa=int(msg.text.replace(" ","").replace(",",""))
        if summa<=0: raise ValueError
    except: bot.send_message(uid,"❗ Musbat raqam kiriting:"); return
    did=data["dokon_id"]; dnomi=data["dokon_nomi"]; nasiya_qoldiq=data["nasiya_qoldiq"]
    if summa>nasiya_qoldiq:
        ortiqcha=summa-nasiya_qoldiq
        data["ortiqcha_summa"]=summa; data["ortiqcha_diff"]=ortiqcha
        set_state(uid,"pul_nasiya_ortiqcha_confirm",data)
        kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
        kb.add("✅ Tasdiqlash","✏️ Summani to'g'irlash")
        bot.send_message(uid,
            f"⚠️ Siz {fmt(nasiya_qoldiq)}ga qarshi {fmt(summa)} kiritdingiz.\n"
            f"{fmt(ortiqcha)} so'm ORTIQCHA.\n\nTasdiqlaysizmi?",reply_markup=kb); return
    now=datetime.now().isoformat(); remaining=summa
    conn=get_db();c=conn.cursor()
    c.execute("SELECT id,qoldiq FROM nasiya WHERE dokon_id=? AND agent_id=? AND qoldiq>0 ORDER BY created_at",(did,uid))
    for nid,qoldiq in c.fetchall():
        if remaining<=0: break
        pay=min(remaining,qoldiq)
        c.execute("UPDATE nasiya SET tolangan=tolangan+?,qoldiq=qoldiq-?,updated_at=? WHERE id=?",(pay,pay,now,nid))
        remaining-=pay
    c.execute("INSERT INTO pul_olish (dokon_id,agent_id,summa,created_at) VALUES (?,?,?,?)",(did,uid,summa,now))
    conn.commit(); conn.close(); clear_state(uid)
    yangi_qoldiq=nasiya_qoldiq-summa
    nasiya_status="✅ Nasiya to'liq to'landi!" if yangi_qoldiq<=0 else f"🔴 Qolgan nasiya: {fmt(yangi_qoldiq)}"
    bot.send_message(uid,
        f"✅ Pul olish saqlandi!\n\n"
        f"🏪 {dnomi}\n"
        f"💵 Olingan summa: {fmt(summa)}\n"
        f"💳 Nasiyaga hisoblandi: {fmt(summa)}\n"
        f"{nasiya_status}",
        reply_markup=main_kb(user[3]))
    for aid in all_admin_ids():
        try: bot.send_message(aid,
            f"💰 Pul olindi (nasiyaga)!\n\n"
            f"👤 Agent: {user[2]}\n📍 {user[4]}\n"
            f"🏪 Dokon: {dnomi}\n"
            f"💵 Summa: {fmt(summa)}\n"
            f"💳 Nasiyaga: {fmt(summa)}\n"
            f"🔴 Qoldiq: {fmt(yangi_qoldiq)}")
        except: pass

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="pul_nasiya_ortiqcha_confirm")
def s_pul_nasiya_ortiqcha_confirm(msg):
    uid=msg.from_user.id; user=get_user(uid); data=get_state(uid)["data"]
    if msg.text=="✏️ Summani to'g'irlash":
        set_state(uid,"pul_nasiya_summa",data)
        bot.send_message(uid,
            f"🏪 {data['dokon_nomi']}\n"
            f"🔴 Nasiya qoldiq: {fmt(data['nasiya_qoldiq'])}\n\n"
            f"Qancha pul oldingiz?",reply_markup=cancel_kb()); return
    if msg.text!="✅ Tasdiqlash": return
    summa=data["ortiqcha_summa"]; nasiya_qoldiq=data["nasiya_qoldiq"]; ortiqcha=data["ortiqcha_diff"]
    did=data["dokon_id"]; dnomi=data["dokon_nomi"]
    now=datetime.now().isoformat(); remaining=nasiya_qoldiq
    conn=get_db();c=conn.cursor()
    c.execute("SELECT id,qoldiq FROM nasiya WHERE dokon_id=? AND agent_id=? AND qoldiq>0 ORDER BY created_at",(did,uid))
    for nid,qoldiq in c.fetchall():
        if remaining<=0: break
        pay=min(remaining,qoldiq)
        c.execute("UPDATE nasiya SET tolangan=tolangan+?,qoldiq=qoldiq-?,updated_at=? WHERE id=?",(pay,pay,now,nid))
        remaining-=pay
    c.execute("INSERT INTO pul_olish (dokon_id,agent_id,summa,created_at) VALUES (?,?,?,?)",(did,uid,summa,now))
    update_balans_delta(c,did,ortiqcha)
    c.execute("SELECT owner_telegram_id FROM dokonlar WHERE id=?",(did,))
    row=c.fetchone(); owner_tg=row[0] if row else None
    conn.commit(); conn.close(); clear_state(uid)
    bot.send_message(uid,
        f"✅ Pul olish saqlandi!\n\n"
        f"🏪 {dnomi}\n"
        f"💵 Olingan summa: {fmt(summa)}\n"
        f"💳 Nasiyaga hisoblandi: {fmt(nasiya_qoldiq)}\n"
        f"✅ Nasiya to'liq to'landi!\n"
        f"💰 Ortiqcha balansga yozildi: +{fmt(ortiqcha)}",
        reply_markup=main_kb(user[3]))
    for aid in all_admin_ids():
        try: bot.send_message(aid,
            f"💰 Pul olindi (ortiqcha)!\n\n"
            f"👤 Agent: {user[2]}\n📍 {user[4]}\n"
            f"🏪 Dokon: {dnomi}\n"
            f"💵 Summa: {fmt(summa)}\n"
            f"💳 Nasiyaga: {fmt(nasiya_qoldiq)}\n"
            f"💰 Ortiqcha balans: +{fmt(ortiqcha)}")
        except: pass
    if owner_tg:
        try: bot.send_message(owner_tg,f"💰 Sizda {fmt(ortiqcha)} so'm ortiqcha to'lov bor.\nKeyingi tovardan ayiriladi.")
        except: pass

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="pul_summa")
def s_pul_summa(msg):
    uid=msg.from_user.id; user=get_user(uid); data=get_state(uid)["data"]
    try: summa=int(msg.text.replace(" ","").replace(",",""))
    except: bot.send_message(uid,"❗ Raqam kiriting: 500000"); return
    conn=get_db();c=conn.cursor()
    c.execute("INSERT INTO pul_olish (dokon_id,agent_id,summa,created_at) VALUES (?,?,?,?)",
              (data["dokon_id"],uid,summa,datetime.now().isoformat()))
    conn.commit();conn.close();clear_state(uid)
    bot.send_message(uid,f"✅ Pul olish saqlandi!\n🏪 {data['dokon_nomi']}\n💰 {fmt(summa)}",reply_markup=main_kb(user[3]))
    for aid in all_admin_ids():
        try: bot.send_message(aid,
            f"💰 Pul olindi!\n\n"
            f"👤 Agent: {user[2]}\n"
            f"📍 Viloyat: {user[4]}\n"
            f"🏪 Dokon: {data['dokon_nomi']}\n"
            f"💵 Summa: {fmt(summa)}")
        except: pass

def _nasiya_summary_kb(uid, admin_view=False):
    """Step 1: returns (summary_text, store_keyboard) for nasiya.
    admin_view=True → barcha agentlar bo'yicha (admin uchun).
    Aks holda — faqat uid agent o'zinikini ko'radi."""
    conn=get_db();c=conn.cursor()
    if admin_view:
        c.execute("""SELECT d.id,d.nomi,COALESCE(SUM(n.qoldiq),0),COALESCE(u.name,'—')
                     FROM nasiya n JOIN dokonlar d ON d.id=n.dokon_id
                     LEFT JOIN users u ON u.telegram_id=n.agent_id
                     WHERE n.qoldiq>0
                     GROUP BY d.id,d.nomi,u.name ORDER BY SUM(n.qoldiq) DESC""")
        store_rows=c.fetchall()
        c.execute("SELECT COUNT(*) FROM dokonlar WHERE holat='faol'")
        jami_dokon=c.fetchone()[0]
    else:
        c.execute("""SELECT d.id,d.nomi,COALESCE(SUM(n.qoldiq),0),''
                     FROM nasiya n JOIN dokonlar d ON d.id=n.dokon_id
                     WHERE n.agent_id=? AND n.qoldiq>0
                     GROUP BY d.id,d.nomi ORDER BY d.nomi""",(uid,))
        store_rows=c.fetchall()
        c.execute("SELECT COUNT(*) FROM dokonlar WHERE agent_id=? AND holat='faol'",(uid,))
        jami_dokon=c.fetchone()[0]
    conn.close()
    nasiyali_d=len(store_rows)
    nasiyasiz_d=max(0,jami_dokon-nasiyali_d)
    jami_qoldiq=sum(r[2] for r in store_rows)
    title="🗂 NASIYA BOSHQARUV (BARCHA AGENTLAR)" if admin_view else "🗂 NASIYA BOSHQARUV"
    text=(f"{title}\n{'━'*26}\n"
          f"🔴 Jami nasiya: {fmt(jami_qoldiq)}\n"
          f"🏪 Nasiyali dokonlar: {nasiyali_d} ta\n"
          f"✅ Nasiyasiz dokonlar: {nasiyasiz_d} ta")
    if admin_view and store_rows:
        text+=f"\n\n📋 TOP nasiyali dokonlar:\n"
        for did,dnomi,qoldiq,aname in store_rows[:15]:
            text+=f"  • {dnomi} ({aname}) — {fmt(qoldiq)}\n"
        if len(store_rows)>15:
            text+=f"  … +{len(store_rows)-15} ta dokon"
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    if not admin_view:
        for did,dnomi,qoldiq,_ in store_rows:
            kb.add(f"🏪 {did}||{dnomi}")
    kb.add("❌ Bekor qilish")
    return text,kb,store_rows

def _show_nasiya_store(uid,did,dnomi):
    """Step 2: show full sale history for one store."""
    conn=get_db();c=conn.cursor()
    c.execute("""SELECT n.id,n.jami_summa,n.tolangan,n.qoldiq,n.created_at
                 FROM nasiya n WHERE n.dokon_id=? AND n.agent_id=?
                 ORDER BY n.created_at""",(did,uid))
    rows=c.fetchall(); conn.close()
    jami_savdo=sum(r[1] for r in rows)
    jami_qoldiq=sum(r[3] for r in rows)
    text=f"🏪 {dnomi}\n{'━'*26}\n\n📊 Savdo tarixi:\n"
    for nid,jami,tolangan,qoldiq,created_at in rows:
        try: sana=created_at[:10]
        except: sana="—"
        if qoldiq==0:
            text+=f"  • {sana} | {fmt(jami)} | ✅ To'liq to'langan\n"
        else:
            text+=f"  • {sana} | {fmt(jami)} | 🔴 Qoldiq: {fmt(qoldiq)}\n"
    text+=(f"\n{'━'*26}\n"
           f"💰 Umumiy savdo: {fmt(jami_savdo)}\n"
           f"🔴 Jami qarz: {fmt(jami_qoldiq)}")
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    if jami_qoldiq>0:
        kb.add("💳 To'lov qabul qilish")
    kb.add("⬅️ Orqaga"); kb.add("❌ Bekor qilish")
    return text,kb,jami_qoldiq

@bot.message_handler(func=lambda m:m.text=="💳 Nasiya boshqaruv")
def nasiya_boshqaruv(msg):
    uid=msg.from_user.id; user=get_user(uid)
    if not user: return
    if check_pending(uid): return
    admin_view=is_admin(uid)
    text,kb,store_rows=_nasiya_summary_kb(uid, admin_view=admin_view)
    if not store_rows:
        bot.send_message(uid,text+"\n\n✅ Nasiya qarz yo'q!",reply_markup=main_kb(user[3])); return
    if admin_view:
        # Admin faqat ko'radi — to'lov qabul qilish agentniki
        bot.send_message(uid,text,reply_markup=main_kb(user[3])); return
    set_state(uid,"nasiya_store_list",{})
    bot.send_message(uid,text,reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="nasiya_store_list")
def s_nasiya_store_list(msg):
    uid=msg.from_user.id
    if not msg.text.startswith("🏪 "): return
    try:
        parts=msg.text[2:].strip().split("||")
        did=int(parts[0]); dnomi=parts[1]
    except: return
    text,kb,jami_qoldiq=_show_nasiya_store(uid,did,dnomi)
    set_state(uid,"nasiya_store_detail",{"did":did,"dnomi":dnomi,"jami_qoldiq":jami_qoldiq})
    bot.send_message(uid,text,reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="nasiya_store_detail")
def s_nasiya_store_detail(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text=="⬅️ Orqaga":
        text,kb,store_rows=_nasiya_summary_kb(uid)
        set_state(uid,"nasiya_store_list",{})
        bot.send_message(uid,text,reply_markup=kb); return
    if msg.text=="💳 To'lov qabul qilish":
        dnomi=data["dnomi"]; jami_qoldiq=data["jami_qoldiq"]
        set_state(uid,"nasiya_tolov",data)
        bot.send_message(uid,
            f"🏪 {dnomi}\n🔴 Jami qarz: {fmt(jami_qoldiq)}\n\n"
            f"Qancha to'lov qabul qildingiz?\n"
            f"(To'liq to'lash uchun: {fmt(jami_qoldiq)})",
            reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="nasiya_tolov")
def s_nasiya_tolov(msg):
    uid=msg.from_user.id; user=get_user(uid); data=get_state(uid)["data"]
    try:
        summa=int(msg.text.replace(" ","").replace(",",""))
        if summa<=0: raise ValueError
    except: bot.send_message(uid,"❗ Musbat raqam kiriting:"); return
    did=data["did"]; dnomi=data["dnomi"]; jami_qoldiq=data["jami_qoldiq"]
    if summa>jami_qoldiq:
        ortiqcha=summa-jami_qoldiq
        data["ortiqcha_summa"]=summa; data["ortiqcha_diff"]=ortiqcha
        set_state(uid,"nasiya_tolov_ortiqcha_confirm",data)
        kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
        kb.add("✅ Tasdiqlash","✏️ Summani to'g'irlash")
        bot.send_message(uid,
            f"⚠️ Siz {fmt(jami_qoldiq)}ga qarshi {fmt(summa)} kiritdingiz.\n"
            f"{fmt(ortiqcha)} so'm ORTIQCHA.\n\nTasdiqlaysizmi?",reply_markup=kb); return
    # Apply FIFO: pay off oldest unpaid sales first
    remaining=summa; now=datetime.now().isoformat()
    conn=get_db();c=conn.cursor()
    c.execute("SELECT id,qoldiq FROM nasiya WHERE dokon_id=? AND agent_id=? AND qoldiq>0 ORDER BY created_at",(did,uid))
    for nid,qoldiq in c.fetchall():
        if remaining<=0: break
        pay=min(remaining,qoldiq)
        c.execute("UPDATE nasiya SET tolangan=tolangan+?,qoldiq=qoldiq-?,updated_at=? WHERE id=?",(pay,pay,now,nid))
        remaining-=pay
    c.execute("INSERT INTO pul_olish (dokon_id,agent_id,summa,created_at) VALUES (?,?,?,?)",(did,uid,summa,now))
    conn.commit(); conn.close()
    yangi_qoldiq=jami_qoldiq-summa
    status="✅ Barcha qarz to'liq to'landi!" if yangi_qoldiq<=0 else f"🔴 Qolgan qarz: {fmt(yangi_qoldiq)}"
    bot.send_message(uid,
        f"✅ To'lov qabul qilindi!\n\n"
        f"🏪 {dnomi}\n"
        f"💵 Qabul qilindi: {fmt(summa)}\n"
        f"{status}",
        reply_markup=main_kb(user[3]))
    clear_state(uid)
    for aid in all_admin_ids():
        try: bot.send_message(aid,
            f"💳 Nasiya to'lovi!\n\n"
            f"👤 Agent: {user[2]}\n📍 {user[4]}\n"
            f"🏪 Dokon: {dnomi}\n"
            f"💵 To'landi: {fmt(summa)}\n"
            f"🔴 Qoldiq: {fmt(yangi_qoldiq)}")
        except: pass

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="nasiya_tolov_ortiqcha_confirm")
def s_nasiya_tolov_ortiqcha_confirm(msg):
    uid=msg.from_user.id; user=get_user(uid); data=get_state(uid)["data"]
    if msg.text=="✏️ Summani to'g'irlash":
        set_state(uid,"nasiya_tolov",data)
        bot.send_message(uid,
            f"🏪 {data['dnomi']}\n🔴 Jami qarz: {fmt(data['jami_qoldiq'])}\n\n"
            f"Qancha to'lov qabul qildingiz?\n(To'liq: {fmt(data['jami_qoldiq'])})",
            reply_markup=cancel_kb()); return
    if msg.text!="✅ Tasdiqlash": return
    summa=data["ortiqcha_summa"]; jami_qoldiq=data["jami_qoldiq"]; ortiqcha=data["ortiqcha_diff"]
    did=data["did"]; dnomi=data["dnomi"]
    now=datetime.now().isoformat(); remaining=jami_qoldiq
    conn=get_db();c=conn.cursor()
    c.execute("SELECT id,qoldiq FROM nasiya WHERE dokon_id=? AND agent_id=? AND qoldiq>0 ORDER BY created_at",(did,uid))
    for nid,qoldiq in c.fetchall():
        if remaining<=0: break
        pay=min(remaining,qoldiq)
        c.execute("UPDATE nasiya SET tolangan=tolangan+?,qoldiq=qoldiq-?,updated_at=? WHERE id=?",(pay,pay,now,nid))
        remaining-=pay
    c.execute("INSERT INTO pul_olish (dokon_id,agent_id,summa,created_at) VALUES (?,?,?,?)",(did,uid,summa,now))
    update_balans_delta(c,did,ortiqcha)
    c.execute("SELECT owner_telegram_id FROM dokonlar WHERE id=?",(did,))
    row=c.fetchone(); owner_tg=row[0] if row else None
    conn.commit(); conn.close(); clear_state(uid)
    bot.send_message(uid,
        f"✅ To'lov qabul qilindi!\n\n"
        f"🏪 {dnomi}\n"
        f"💵 Qabul qilindi: {fmt(summa)}\n"
        f"✅ Barcha qarz to'liq to'landi!\n"
        f"💰 Ortiqcha balansga yozildi: +{fmt(ortiqcha)}",
        reply_markup=main_kb(user[3]))
    for aid in all_admin_ids():
        try: bot.send_message(aid,
            f"💳 Nasiya to'lovi (ortiqcha)!\n\n"
            f"👤 Agent: {user[2]}\n📍 {user[4]}\n"
            f"🏪 Dokon: {dnomi}\n"
            f"💵 To'landi: {fmt(summa)}\n"
            f"✅ Qarz: to'liq to'landi\n"
            f"💰 Ortiqcha balans: +{fmt(ortiqcha)}")
        except: pass
    if owner_tg:
        try: bot.send_message(owner_tg,f"💰 Sizda {fmt(ortiqcha)} so'm ortiqcha to'lov bor.\nKeyingi tovardan ayiriladi.")
        except: pass

SABAB_MAP={"💸 Narx qimmat":"narx_qimmat","📦 Hozir tovari bor":"tovari_bor","🏢 Boshqa firma":"boshqa_firma","😕 Sifat yoqmadi":"sifat","🚪 Egasi yo'q edi":"egasi_yoq","🕐 Keyin keling dedi":"keyin_keling","🚫 Sotilmaydi dedi":"sotilmaydi","📝 Boshqa sabab":"boshqa"}

@bot.message_handler(func=lambda m:m.text=="❌ Tovar olmadi")
def tovar_olmadi(msg):
    uid=msg.from_user.id; user=get_user(uid)
    if not user: return
    if check_pending(uid): return
    conn=get_db();c=conn.cursor()
    if is_admin(uid):
        c.execute("SELECT id,nomi FROM dokonlar ORDER BY nomi")
    else:
        c.execute("SELECT id,nomi FROM dokonlar WHERE agent_id=? ORDER BY nomi",(uid,))
    dokonlar=c.fetchall(); conn.close()
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for d in dokonlar: kb.add(f"🏪 {d[0]}||{d[1]}")
    kb.add("🆕 Yangi dokon (olmagan)"); kb.add("❌ Bekor qilish")
    set_state(uid,"olmadi_dokon",{})
    bot.send_message(uid,"🏪 Dokonni tanlang:",reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_dokon")
def s_olmadi_dokon(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text=="🆕 Yangi dokon (olmagan)":
        set_state(uid,"olmadi_yangi_nomi",data)
        bot.send_message(uid,"Dokon nomini kiriting:",reply_markup=cancel_kb()); return
    if not msg.text.startswith("🏪 "): return
    try:
        did,dnomi=msg.text.replace("🏪 ","").split("||",1)
        data["dokon_id"]=int(did); data["dokon_nomi"]=dnomi
    except: return
    set_state(uid,"olmadi_sabab",data)
    bot.send_message(uid,f"❓ {dnomi} — sababi:",reply_markup=sabab_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_yangi_nomi")
def s_olmadi_yangi_nomi(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["dokon_id"]=None; data["dokon_nomi"]=msg.text.strip()
    set_state(uid,"olmadi_yangi_egasi",data)
    bot.send_message(uid,"👤 Egasining ismi:",reply_markup=skip_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_yangi_egasi")
def s_olmadi_yangi_egasi(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["egasi"]="" if msg.text=="⏭ O'tkazib yuborish" else msg.text.strip()
    set_state(uid,"olmadi_yangi_tel",data)
    bot.send_message(uid,"📞 Telefon raqami:",reply_markup=skip_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_yangi_tel")
def s_olmadi_yangi_tel(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["telefon"]="" if msg.text=="⏭ O'tkazib yuborish" else msg.text.strip()
    # Yangi dokon: sabab so'ramaymiz, to'g'ri qaytish sanasiga o'tamiz
    data["sabab"]="yangi_dokon"; data["sabab_text"]="🆕 Yangi olmagan dokon"
    set_state(uid,"olmadi_qaytish",data)
    bot.send_message(uid,"📅 Qaytib kirish sanasi (masalan: 25.05.2026):",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_sabab")
def s_olmadi_sabab(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    sabab=SABAB_MAP.get(msg.text)
    if not sabab: bot.send_message(uid,"❗ Sababni tanlang"); return
    data["sabab"]=sabab; data["sabab_text"]=msg.text
    set_state(uid,"olmadi_qaytish",data)
    bot.send_message(uid,"📅 Qaytib kirish sanasi (masalan: 25.05.2026):",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_qaytish")
def s_olmadi_qaytish(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["qaytish_sanasi"]=msg.text.strip()
    set_state(uid,"olmadi_location",data)
    bot.send_message(uid,"📍 Location yuboring:",reply_markup=location_kb())

@bot.message_handler(content_types=["location"],func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_location")
def s_olmadi_loc(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["lat"]=msg.location.latitude; data["lon"]=msg.location.longitude
    set_state(uid,"olmadi_foto",data)
    bot.send_message(uid,"📸 Dokon rasmini yuboring:",reply_markup=skip_kb())

@bot.message_handler(content_types=["photo"],func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_foto")
def s_olmadi_foto_p(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["foto"]=msg.photo[-1].file_id
    _olmadi_confirm(uid,data)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_foto")
def s_olmadi_foto_s(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text!="⏭ O'tkazib yuborish": return
    data["foto"]=None
    _olmadi_confirm(uid,data)

def _olmadi_confirm(uid,data):
    set_state(uid,"olmadi_confirm",data)
    lat=data.get("lat"); lon=data.get("lon")
    maps_line=f"\n🗺 Location: https://maps.google.com/?q={lat},{lon}" if lat and lon else ""
    text=(f"📋 TASDIQLANG:\n{'━'*24}\n"
          f"🏪 Dokon: {data['dokon_nomi']}\n")
    if data.get("dokon_id") is None:
        text+=f"👤 Egasi: {data.get('egasi') or '—'}\n📞 Tel: {data.get('telefon') or '—'}\n"
    text+=(f"❌ Sabab: {data['sabab_text']}\n"
           f"📅 Qaytish: {data.get('qaytish_sanasi','—')}"
           f"{maps_line}\n"
           f"📸 Rasm: {'✅ bor' if data.get('foto') else '—'}\n"
           f"{'━'*24}\nYubormoqchimisiz?")
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    kb.add("✅ Tasdiqlash","❌ Bekor qilish")
    bot.send_message(uid,text,reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="olmadi_confirm")
def s_olmadi_confirm(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text=="✅ Tasdiqlash":
        _save_olmadi(uid,data)
    elif msg.text=="❌ Bekor qilish":
        user=get_user(uid); clear_state(uid)
        bot.send_message(uid,"❌ Bekor qilindi",reply_markup=main_kb(user[3]))

def _save_olmadi(uid,data):
    user=get_user(uid); conn=get_db(); c=conn.cursor()
    dokon_id=data.get("dokon_id")
    egasi=""; telefon=""
    if dokon_id is None:
        egasi=data.get("egasi",""); telefon=data.get("telefon","")
        c.execute("INSERT INTO dokonlar (nomi,egasi,telefon,viloyat,latitude,longitude,foto,agent_id,holat,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (data["dokon_nomi"],egasi,telefon,user[4],data.get("lat"),data.get("lon"),data.get("foto"),uid,"nofaol",datetime.now().isoformat()))
        dokon_id=c.lastrowid
    else:
        c.execute("SELECT egasi,telefon FROM dokonlar WHERE id=?",(dokon_id,))
        r=c.fetchone()
        if r: egasi,telefon=r[0] or "",r[1] or ""
    c.execute("INSERT INTO olmagan_dokonlar (dokon_id,agent_id,sabab,sabab_text,latitude,longitude,qaytish_sanasi,foto,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (dokon_id,uid,data["sabab"],data["sabab_text"],data.get("lat"),data.get("lon"),data.get("qaytish_sanasi"),data.get("foto"),datetime.now().isoformat()))
    conn.commit();conn.close();clear_state(uid)
    qaytish=f"\n📅 Qaytish: {data.get('qaytish_sanasi','')}" if data.get("qaytish_sanasi") else ""
    bot.send_message(uid,f"✅ Yozildi!\n🏪 {data['dokon_nomi']}\n❌ {data['sabab_text']}{qaytish}",reply_markup=main_kb(user[3]))
    lat=data.get("lat"); lon=data.get("lon")
    maps_line=f"\n🗺 Location: https://maps.google.com/?q={lat},{lon}" if lat and lon else ""
    caption=(f"🔔 Tovar olmadi / Qaytib kirish\n\n"
             f"🏪 {data['dokon_nomi']}\n"
             f"👤 Egasi: {egasi or '—'}\n"
             f"📞 Tel: {telefon or '—'}"
             f"{maps_line}\n"
             f"❌ Sabab: {data['sabab_text']}"
             f"{qaytish}\n"
             f"👤 Agent: {user[2]} | 📍 {user[4]}")
    for aid in all_admin_ids():
        try:
            if data.get("foto"):
                bot.send_photo(aid,data["foto"],caption=caption)
            else:
                bot.send_message(aid,caption)
        except: pass

@bot.message_handler(func=lambda m:m.text=="📋 Qaytib kirish kerak")
def qaytib_kirish(msg):
    uid=msg.from_user.id; user=get_user(uid)
    if not user: return
    if check_pending(uid): return
    conn=get_db();c=conn.cursor()
    c.execute("""SELECT d.nomi,d.egasi,d.telefon,o.sabab_text,o.qaytish_sanasi,o.id,o.latitude,o.longitude
        FROM olmagan_dokonlar o JOIN dokonlar d ON o.dokon_id=d.id
        WHERE o.agent_id=? AND o.bajarildi=0 AND o.qaytish_sanasi IS NOT NULL
        ORDER BY o.qaytish_sanasi""",(uid,))
    rows=c.fetchall();conn.close()
    if not rows: bot.send_message(uid,"✅ Qaytib kirish kerak bo'lgan dokon yo'q!",reply_markup=main_kb(user[3])); return
    text="📋 Qaytib kirish kerak:\n\n"
    for r in rows:
        nomi,egasi,telefon,sabab_text,qaytish_sanasi,oid,lat,lon=r
        maps=""
        if lat and lon: maps=f"\n🗺 https://maps.google.com/?q={lat},{lon}"
        text+=(f"🏪 {nomi}\n"
               f"👤 {egasi or '—'}\n"
               f"📞 {telefon or '—'}"
               f"{maps}\n"
               f"❌ {sabab_text}\n"
               f"📅 {qaytish_sanasi}\n"
               f"✅ /bajarildi_{oid}\n\n")
    bot.send_message(uid,text,reply_markup=main_kb(user[3]))

@bot.message_handler(commands=["bajarildi"])
def bajarildi(msg):
    uid=msg.from_user.id
    try:
        oid=int(msg.text.split("_")[1])
        conn=get_db();c=conn.cursor()
        c.execute("UPDATE olmagan_dokonlar SET bajarildi=1 WHERE id=? AND agent_id=?",(oid,uid))
        conn.commit();conn.close()
        bot.send_message(uid,"✅ Bajarildi!")
    except: bot.send_message(uid,"❗ Xato")

TOLOV_LABELS={"naqd":"Naqd ✅","karta":"Karta ✅","nasiya":"Nasiya 🔴","aralash":"Aralash 🔀"}

# ───────────── QIDIRUV (dokon/mijoz) ─────────────
@bot.message_handler(func=lambda m:m.text=="🔍 Qidiruv")
def qidiruv_start(msg):
    uid=msg.from_user.id
    user=get_user(uid)
    if not user: return
    set_state(uid,"qidiruv_input",{"role":user[3]})
    bot.send_message(uid,
        "🔍 QIDIRUV\n\nDokon nomi, egasi yoki telefon raqamini kiriting:\n"
        "Masalan: <code>Fayz</code> yoki <code>Akmal</code> yoki <code>998901234567</code>",
        parse_mode="HTML",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="qidiruv_input")
def qidiruv_query(msg):
    uid=msg.from_user.id
    q=(msg.text or "").strip()
    if not q or q=="❌ Bekor qilish":
        user=get_user(uid)
        set_state(uid,None,{})
        bot.send_message(uid,"Bekor qilindi",reply_markup=main_kb(user[3] if user else "agent")); return
    if len(q)<2:
        bot.send_message(uid,"❗ Kamida 2 ta belgi kiriting."); return
    user=get_user(uid); role=user[3]
    conn=get_db();c=conn.cursor()
    like=f"%{q}%"
    if role=="admin":
        c.execute("""SELECT id,nomi,egasi,viloyat,holat FROM dokonlar
                     WHERE nomi LIKE ? OR egasi LIKE ? OR telefon LIKE ?
                     ORDER BY nomi LIMIT 50""",(like,like,like))
    else:
        c.execute("""SELECT id,nomi,egasi,viloyat,holat FROM dokonlar
                     WHERE agent_id=? AND (nomi LIKE ? OR egasi LIKE ? OR telefon LIKE ?)
                     ORDER BY nomi LIMIT 50""",(uid,like,like,like))
    rows=c.fetchall(); conn.close()
    if not rows:
        bot.send_message(uid,f"❌ '{q}' bo'yicha hech narsa topilmadi.",reply_markup=main_kb(role)); 
        set_state(uid,None,{}); return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for d in rows:
        icon="✅" if d[4]=="faol" else "❌"
        kb.add(f"🏪{d[0]}||{d[1]} ({d[3] or '—'}) {icon}")
    kb.add("❌ Bekor qilish")
    if role=="admin":
        set_state(uid,"admin_dokon_list",{})
    else:
        set_state(uid,"agent_dokon_search_list",{})
    bot.send_message(uid,f"🔍 Topildi: {len(rows)} ta\n\nDokonni tanlang:",reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="agent_dokon_search_list")
def s_agent_dokon_view(msg):
    uid=msg.from_user.id
    if not msg.text.startswith("🏪"):
        if msg.text=="❌ Bekor qilish":
            user=get_user(uid); set_state(uid,None,{})
            bot.send_message(uid,"Bekor qilindi",reply_markup=main_kb(user[3]))
        return
    try: did=int(msg.text[1:].split("||")[0])
    except: return
    conn=get_db();c=conn.cursor()
    c.execute("""SELECT id,nomi,egasi,telefon,viloyat,hudud,latitude,longitude,foto,holat,
                 last_order_date,total_orders,total_sales
                 FROM dokonlar WHERE id=? AND agent_id=?""",(did,uid))
    d=c.fetchone()
    if not d:
        conn.close(); bot.send_message(uid,"❗ Topilmadi yoki sizniki emas."); return
    c.execute("SELECT created_at,jami_summa,tolov_turi FROM savdolar WHERE dokon_id=? ORDER BY created_at DESC LIMIT 5",(did,))
    savdolar=c.fetchall()
    c.execute("SELECT COALESCE(SUM(qoldiq),0) FROM nasiya WHERE dokon_id=? AND qoldiq>0",(did,))
    jami_nasiya=c.fetchone()[0]
    conn.close()
    (_,nomi,egasi,telefon,viloyat,hudud,lat,lon,foto,holat,last_d,total_o,total_s)=d
    holat_txt="✅ Faol" if holat=="faol" else "❌ Nofaol"
    text=(f"🏪 {nomi}  {holat_txt}\n{'━'*26}\n"
          f"👤 Egasi: {egasi or '—'}\n"
          f"📞 Telefon: {telefon or '—'}\n"
          f"📍 {viloyat or '—'} | {hudud or '—'}\n")
    if lat and lon: text+=f"🗺 https://maps.google.com/?q={lat},{lon}\n"
    text+=f"\n{'━'*26}\n📊 OXIRGI 5 SAVDO:\n"
    for s in savdolar:
        sana=s[0][:10] if s[0] else "—"
        tl=TOLOV_LABELS.get(s[2],s[2] or "—")
        text+=f"  • {sana} | {fmt(s[1])} | {tl}\n"
    if not savdolar: text+="  — Savdo yo'q\n"
    text+=(f"\n{'━'*26}\n"
           f"💰 Jami savdo: {fmt(total_s or 0)}\n"
           f"📦 Jami order: {total_o or 0}\n"
           f"🔴 Jami nasiya: {fmt(jami_nasiya)}\n"
           f"📅 Oxirgi: "+(last_d[:10] if last_d else "—"))
    set_state(uid,None,{})
    user=get_user(uid)
    kb=main_kb(user[3])
    if foto:
        try: bot.send_photo(uid,foto,caption=text,reply_markup=kb); return
        except: pass
    bot.send_message(uid,text,reply_markup=kb)

@bot.message_handler(func=lambda m:m.text=="👥 Mijozlar bazasi")
def mijozlar_bazasi(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    conn=get_db();c=conn.cursor()
    c.execute("SELECT COUNT(*) FROM dokonlar"); jami=c.fetchone()[0]
    c.execute("SELECT id,nomi,viloyat,holat FROM dokonlar ORDER BY nomi LIMIT 60")
    dokonlar=c.fetchall(); conn.close()
    if not dokonlar: bot.send_message(uid,"❗ Dokonlar yo'q."); return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for d in dokonlar:
        icon="✅" if d[3]=="faol" else "❌"
        kb.add(f"🏪{d[0]}||{d[1]} ({d[2]}) {icon}")
    kb.add("❌ Bekor qilish")
    set_state(uid,"admin_dokon_list",{})
    bot.send_message(uid,f"👥 Mijozlar bazasi — {jami} ta dokon:\n\nDokonni tanlang:",reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="admin_dokon_list")
def s_admin_dokon_list(msg):
    uid=msg.from_user.id
    if not msg.text.startswith("🏪"): return
    try: did=int(msg.text[1:].split("||")[0])
    except: return
    conn=get_db();c=conn.cursor()
    c.execute("""SELECT id,nomi,egasi,telefon,viloyat,hudud,latitude,longitude,foto,holat,
                 first_order_date,last_order_date,total_orders,repeat_orders,total_sales,avg_repeat_days
                 FROM dokonlar WHERE id=?""",(did,))
    d=c.fetchone()
    if not d: conn.close(); return
    c.execute("SELECT created_at,jami_summa,tolov_turi FROM savdolar WHERE dokon_id=? ORDER BY created_at DESC LIMIT 7",(did,))
    savdolar=c.fetchall()
    c.execute("SELECT COALESCE(SUM(jami_summa),0) FROM savdolar WHERE dokon_id=?",(did,))
    jami_savdo=c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(qoldiq),0) FROM nasiya WHERE dokon_id=? AND qoldiq>0",(did,))
    jami_nasiya=c.fetchone()[0]
    c.execute("SELECT COALESCE(balans,0) FROM mijoz_balans WHERE dokon_id=?",(did,))
    row2=c.fetchone(); mijoz_bal=row2[0] if row2 else 0
    conn.close()
    (_,nomi,egasi,telefon,viloyat,hudud,lat,lon,foto,holat,
     first_d,last_d,total_o,repeat_o,total_s,avg_d)=d
    total_o=total_o or 0; repeat_o=repeat_o or 0; total_s=total_s or 0; avg_d=avg_d or 0.0
    status_lbl,days_since=get_store_status(last_d,avg_d)
    maps_link=f"https://maps.google.com/?q={lat},{lon}" if lat and lon else None
    holat_txt="✅ Faol" if holat=="faol" else "❌ Nofaol"
    text=(f"🏪 {nomi}  {holat_txt}\n{'━'*26}\n"
          f"👤 Egasi: {egasi or '—'}\n"
          f"📞 Telefon: {telefon or '—'}\n"
          f"📍 {viloyat or '—'} | {hudud or '—'}\n")
    if maps_link: text+=f"🗺 Location: {maps_link}\n"
    text+=f"\n{'━'*26}\n📊 SAVDO TARIXI:\n"
    for s in savdolar:
        try: sana=s[0][:10]
        except: sana="—"
        tl=TOLOV_LABELS.get(s[2],s[2] or "—")
        text+=f"  • {sana} | {fmt(s[1])} | {tl}\n"
    if not savdolar: text+="  — Savdo yo'q\n"
    text+=(f"\n{'━'*26}\n"
           f"💰 Jami savdo: {fmt(jami_savdo)}\n"
           f"🔴 Jami nasiya: {fmt(jami_nasiya)}")
    if mijoz_bal>0:
        text+=f"\n💰 Mijoz balansi: +{fmt(mijoz_bal)} (ortiqcha to'lov)"
    text+=(f"\n{'━'*26}\n🔁 REPEAT TAHLIL:\n"
           f"📦 Jami order: {total_o}\n"
           f"🔁 Repeat order: {repeat_o}\n"
           f"⏳ O'rtacha qaytish: {round(avg_d)} kun\n"
           f"📅 Oxirgi: "+(last_d[:10] if last_d else '—')+
           (f" ({days_since} kun oldin)" if days_since is not None else "")+
           f"\n🔥 Status: {status_lbl}")
    set_state(uid,"admin_dokon_view",{"did":did,"nomi":nomi})
    back_kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    back_kb.add("🗑 Dokonni o'chirish")
    back_kb.add("👥 Mijozlar bazasi","❌ Bekor qilish")
    if foto:
        try: bot.send_photo(uid,foto,caption=text,reply_markup=back_kb); return
        except: pass
    bot.send_message(uid,text,reply_markup=back_kb)

@bot.message_handler(func=lambda m:m.text=="🗑 Dokonni o'chirish" and get_state(m.from_user.id)["state"]=="admin_dokon_view")
def dokon_ochir_start(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    data=get_state(uid)["data"]; did=data.get("did")
    if not did: return
    conn=get_db();c=conn.cursor()
    c.execute("SELECT COUNT(*),COALESCE(SUM(jami_summa),0) FROM savdolar WHERE dokon_id=?",(did,))
    sv_n,sv_sum=c.fetchone()
    c.execute("SELECT COALESCE(SUM(qoldiq),0) FROM nasiya WHERE dokon_id=? AND qoldiq>0",(did,))
    nas=c.fetchone()[0]
    conn.close()
    set_state(uid,"admin_dokon_delete_confirm",data)
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("✅ HA, O'CHIRISH"); kb.add("❌ Bekor qilish")
    warn=(f"⚠️ DIQQAT! Dokonni o'chirmoqchimisiz?\n\n"
          f"🏪 {data['nomi']}\n\n"
          f"Quyidagilar HAM o'chiriladi:\n"
          f"  • {sv_n} ta savdo ({fmt(sv_sum)})\n"
          f"  • Barcha nasiya yozuvlari (qoldiq: {fmt(nas)})\n"
          f"  • Pul olish tarixi\n"
          f"  • Olmagan/qaytib kirish yozuvlari\n"
          f"  • Mijoz balansi\n\n"
          f"❗ Bu amalni QAYTARIB BO'LMAYDI!")
    bot.send_message(uid,warn,reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="admin_dokon_delete_confirm")
def dokon_ochir_tasdiq(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text!="✅ HA, O'CHIRISH":
        clear_state(uid)
        user=get_user(uid)
        bot.send_message(uid,"❌ Bekor qilindi",reply_markup=main_kb(user[3])); return
    did=data["did"]; nomi=data["nomi"]
    conn=get_db();c=conn.cursor()
    try:
        c.execute("DELETE FROM savdo_tafsilot WHERE savdo_id IN (SELECT id FROM savdolar WHERE dokon_id=?)",(did,))
        c.execute("DELETE FROM savdolar WHERE dokon_id=?",(did,))
        c.execute("DELETE FROM nasiya WHERE dokon_id=?",(did,))
        c.execute("DELETE FROM pul_olish WHERE dokon_id=?",(did,))
        c.execute("DELETE FROM olmagan_dokonlar WHERE dokon_id=?",(did,))
        c.execute("DELETE FROM revisitlar WHERE dokon_id=?",(did,))
        c.execute("DELETE FROM mijoz_balans WHERE dokon_id=?",(did,))
        c.execute("DELETE FROM dokonlar WHERE id=?",(did,))
        conn.commit()
    except Exception as e:
        conn.close(); clear_state(uid)
        bot.send_message(uid,f"❗ Xato: {e}"); return
    conn.close(); clear_state(uid)
    user=get_user(uid)
    bot.send_message(uid,f"✅ '{nomi}' dokoni va barcha tarixi o'chirildi.",reply_markup=main_kb(user[3]))
    # Notify other admins
    for aid in all_admin_ids():
        if aid==uid: continue
        try: bot.send_message(aid,f"🗑 Admin {user[2]} '{nomi}' dokonini o'chirdi.")
        except: pass

@bot.message_handler(func=lambda m:m.text=="👤 Agent boshqaruv")
def agent_boshqaruv(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    _agent_boshqaruv_list(uid)

def _agent_boshqaruv_list(uid):
    conn=get_db();c=conn.cursor()
    c.execute("SELECT telegram_id,name,viloyat,role FROM users WHERE role IN ('agent','supervisor','blok') ORDER BY name")
    agents=c.fetchall(); conn.close()
    if not agents: bot.send_message(uid,"Agentlar yo'q."); return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for a in agents:
        icon="⭐" if a[3]=="supervisor" else ("🚫" if a[3]=="blok" else "🔰")
        kb.add(f"{icon}{a[0]}||{a[1]} ({a[2]})")
    kb.add("❌ Bekor qilish")
    set_state(uid,"agent_boshqaruv_list",{})
    bot.send_message(uid,f"👤 Agentlar ({len(agents)} ta):\nTanlang:",reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="agent_boshqaruv_list")
def s_agent_boshqaruv_list(msg):
    uid=msg.from_user.id
    if not (msg.text.startswith("🔰") or msg.text.startswith("⭐") or msg.text.startswith("🚫")): return
    try: tid=int(msg.text[1:].split("||")[0])
    except: return
    _show_agent_profile(uid,tid)

def _show_agent_profile(uid,agent_id):
    conn=get_db();c=conn.cursor()
    c.execute("SELECT telegram_id,name,viloyat,role,created_at FROM users WHERE telegram_id=?",(agent_id,))
    a=c.fetchone()
    if not a: conn.close(); return
    c.execute("SELECT COUNT(*) FROM dokonlar WHERE agent_id=? AND holat='faol'",(agent_id,))
    dokon_n=c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(jami_summa),0) FROM savdolar WHERE agent_id=?",(agent_id,))
    jami_savdo=c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(qoldiq),0) FROM nasiya WHERE agent_id=? AND qoldiq>0",(agent_id,))
    jami_nasiya=c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM olmagan_dokonlar WHERE agent_id=? AND bajarildi=0 AND qaytish_sanasi IS NOT NULL",(agent_id,))
    qaytib=c.fetchone()[0]; conn.close()
    rol_map={"agent":"Agent","supervisor":"Supervisor","blok":"🚫 Bloklangan"}
    rol_txt=rol_map.get(a[3],a[3]); sana=a[4][:10] if a[4] else "—"
    text=(f"👤 AGENT: {a[1]}\n{'━'*26}\n"
          f"📍 Viloyat: {a[2]}\n"
          f"🔰 Rol: {rol_txt}\n"
          f"📅 Ro'yxat: {sana}\n\n"
          f"🏪 Dokonlar: {dokon_n} ta\n"
          f"📦 Jami savdo: {fmt(jami_savdo)}\n"
          f"🔴 Jami nasiya: {fmt(jami_nasiya)}\n"
          f"📋 Qaytib kirish kerak: {qaytib} ta")
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    if a[3]=="agent": kb.add("🔼 Supervisorga ko'tarish")
    elif a[3]=="supervisor": kb.add("🔽 Agentga tushirish")
    if a[3]!="blok": kb.add("🚫 Bloklash")
    else: kb.add("✅ Blokdan chiqarish")
    kb.add("📊 Batafsil statistika")
    kb.add("◀️ Orqaga","❌ Bekor qilish")
    set_state(uid,"agent_action",{"agent_id":agent_id,"agent_name":a[1],"agent_role":a[3]})
    bot.send_message(uid,text,reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="agent_action")
def s_agent_action(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    agent_id=data["agent_id"]; agent_name=data["agent_name"]
    if msg.text=="◀️ Orqaga":
        _agent_boshqaruv_list(uid); return
    if msg.text in("🔼 Supervisorga ko'tarish","🔽 Agentga tushirish"):
        new_role="supervisor" if "ko'tarish" in msg.text else "agent"
        conn=get_db();c=conn.cursor()
        c.execute("UPDATE users SET role=? WHERE telegram_id=?",(new_role,agent_id))
        conn.commit();conn.close()
        label="Supervisor" if new_role=="supervisor" else "Agent"
        bot.send_message(uid,f"✅ {agent_name} → {label} qilindi.")
        try: bot.send_message(agent_id,f"ℹ️ Sizning rolingiz o'zgartirildi: {label}")
        except: pass
        _show_agent_profile(uid,agent_id); return
    if msg.text=="🚫 Bloklash":
        conn=get_db();c=conn.cursor()
        c.execute("UPDATE users SET role='blok' WHERE telegram_id=?",(agent_id,))
        conn.commit();conn.close()
        bot.send_message(uid,f"🚫 {agent_name} bloklandi.")
        try: bot.send_message(agent_id,"🚫 Sizning akkauntingiz bloklandi. Admin bilan bog'laning.")
        except: pass
        _show_agent_profile(uid,agent_id); return
    if msg.text=="✅ Blokdan chiqarish":
        conn=get_db();c=conn.cursor()
        c.execute("UPDATE users SET role='agent' WHERE telegram_id=?",(agent_id,))
        conn.commit();conn.close()
        bot.send_message(uid,f"✅ {agent_name} blokdan chiqarildi.")
        try: bot.send_message(agent_id,"✅ Akkauntingiz faollashtirildi. /start bosing.")
        except: pass
        _show_agent_profile(uid,agent_id); return
    if msg.text=="📊 Batafsil statistika":
        _agent_batafsil(uid,agent_id,agent_name); return

def _agent_batafsil(uid,agent_id,agent_name):
    conn=get_db();c=conn.cursor()
    c.execute("""SELECT substr(created_at,1,7) as oy,COALESCE(SUM(jami_summa),0),COUNT(*)
        FROM savdolar WHERE agent_id=? GROUP BY oy ORDER BY oy DESC LIMIT 6""",(agent_id,))
    oylar=c.fetchall()
    c.execute("SELECT COALESCE(SUM(jami_summa),0),COUNT(*) FROM savdolar WHERE agent_id=? AND substr(created_at,1,10)=?",(agent_id,date.today().isoformat()))
    bugungi_s,bugungi_n=c.fetchone()
    c.execute("SELECT COUNT(*) FROM dokonlar WHERE agent_id=? AND holat='faol'",(agent_id,))
    dokon_n=c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(qoldiq),0) FROM nasiya WHERE agent_id=? AND qoldiq>0",(agent_id,))
    nasiya=c.fetchone()[0]; conn.close()
    text=(f"📊 {agent_name} — Batafsil\n{'━'*26}\n"
          f"🏪 Faol dokonlar: {dokon_n} ta\n"
          f"🔴 Nasiya qoldig'i: {fmt(nasiya)}\n"
          f"💰 Bugungi savdo: {fmt(bugungi_s)} ({bugungi_n} ta)\n\n"
          f"📅 Oylik savdolar:\n")
    for oy,summa,n in oylar: text+=f"  • {oy}: {fmt(summa)} ({n} ta)\n"
    if not oylar: text+="  — Savdo yo'q\n"
    bot.send_message(uid,text)

# ── BROADCAST ────────────────────────────────────────────────
def _broadcast_audience_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    kb.add("👥 Barcha agentlarga")
    kb.add("🏪 Barcha dokon egalariga")
    kb.add("👤 Hammaga (agentlar + egalar)")
    kb.add("❌ Bekor qilish")
    return kb

@bot.message_handler(func=lambda m:m.text=="📢 Xabar yuborish")
def broadcast_start(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    set_state(uid,"broadcast_audience",{})
    bot.send_message(uid,"📢 Kimga yubormoqchisiz?",reply_markup=_broadcast_audience_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="broadcast_audience")
def s_broadcast_audience(msg):
    uid=msg.from_user.id
    options={"👥 Barcha agentlarga","🏪 Barcha dokon egalariga","👤 Hammaga (agentlar + egalar)"}
    if msg.text not in options: return
    set_state(uid,"broadcast_text",{"audience":msg.text})
    bot.send_message(uid,
        f"✏️ Xabar matnini yozing:\n_(u yuboriladi: {msg.text})_",
        reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="broadcast_text",
                     content_types=["text","photo","document","video","audio"])
def s_broadcast_text(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    audience=data["audience"]; clear_state(uid)
    conn=get_db();c=conn.cursor()

    recipients=set()
    if audience in("👥 Barcha agentlarga","👤 Hammaga (agentlar + egalar)"):
        c.execute("SELECT telegram_id FROM users WHERE role IN ('agent','supervisor')")
        for r in c.fetchall(): recipients.add(r[0])
    if audience in("🏪 Barcha dokon egalariga","👤 Hammaga (agentlar + egalar)"):
        c.execute("SELECT DISTINCT owner_telegram_id FROM dokonlar WHERE owner_telegram_id IS NOT NULL")
        for r in c.fetchall(): recipients.add(r[0])
    conn.close()

    if not recipients:
        bot.send_message(uid,"❗ Yuborish uchun foydalanuvchi topilmadi.",reply_markup=main_kb("admin")); return

    bot.send_message(uid,f"⏳ {len(recipients)} ta foydalanuvchiga yuborilmoqda...")

    ok=0; fail=0
    for tid in recipients:
        if tid==uid: continue
        try:
            if msg.content_type=="text":
                bot.send_message(tid,msg.text)
            elif msg.content_type=="photo":
                bot.send_photo(tid,msg.photo[-1].file_id,caption=msg.caption or "")
            elif msg.content_type=="document":
                bot.send_document(tid,msg.document.file_id,caption=msg.caption or "")
            elif msg.content_type=="video":
                bot.send_video(tid,msg.video.file_id,caption=msg.caption or "")
            elif msg.content_type=="audio":
                bot.send_audio(tid,msg.audio.file_id,caption=msg.caption or "")
            ok+=1
        except: fail+=1

    report=(f"📢 Xabar yuborish yakunlandi!\n{'━'*26}\n"
            f"✅ Muvaffaqiyatli: {ok} ta\n"
            f"❌ Xato (blok/o'chgan): {fail} ta\n"
            f"👤 Jami: {ok+fail} ta")
    bot.send_message(uid,report,reply_markup=main_kb("admin"))

def _davr_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    kb.add("📆 Bugun","📆 Bu hafta")
    kb.add("📆 Bu oy","📆 O'tgan oy")
    kb.add("📆 O'tgan hafta","🗓 Boshqa sana")
    kb.add("❌ Bekor qilish"); return kb

def _parse_davr(text):
    """Returns (date_from, date_to, label) for a period button."""
    from calendar import monthrange
    today=date.today()
    if text=="📆 Bugun":
        d=today.isoformat(); return d,d,"Bugun"
    if text=="📆 Bu hafta":
        mon=(today-timedelta(days=today.weekday())).isoformat()
        return mon,today.isoformat(),"Bu hafta"
    if text=="📆 Bu oy":
        return today.strftime("%Y-%m-01"),today.isoformat(),"Bu oy"
    if text=="📆 O'tgan oy":
        if today.month==1: y,m=today.year-1,12
        else: y,m=today.year,today.month-1
        last=monthrange(y,m)[1]
        s=f"{y}-{m:02d}"; return f"{s}-01",f"{s}-{last}",f"O'tgan oy ({s})"
    if text=="📆 O'tgan hafta":
        mon=today-timedelta(days=today.weekday()+7)
        sun=mon+timedelta(days=6)
        return mon.isoformat(),sun.isoformat(),"O'tgan hafta"
    return None,None,None

def _send_umumiy_stat(uid,d_from,d_to,label):
    conn=get_db();c=conn.cursor()
    c.execute("SELECT COUNT(*) FROM dokonlar WHERE holat='faol'"); jami_d=c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE role IN ('agent','supervisor')"); jami_a=c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(jami_summa),0),COUNT(*) FROM savdolar WHERE substr(created_at,1,10) BETWEEN ? AND ?",(d_from,d_to))
    jami_savdo,savdo_n=c.fetchone()
    c.execute("SELECT COALESCE(SUM(summa),0) FROM pul_olish WHERE substr(created_at,1,10) BETWEEN ? AND ?",(d_from,d_to))
    jami_pul=c.fetchone()[0]
    c.execute("""SELECT d.viloyat,COALESCE(SUM(s.jami_summa),0)
        FROM savdolar s JOIN dokonlar d ON s.dokon_id=d.id
        WHERE substr(s.created_at,1,10) BETWEEN ? AND ?
        GROUP BY d.viloyat ORDER BY 2 DESC""",(d_from,d_to)); vs=c.fetchall()
    c.execute("""SELECT sabab_text,COUNT(*) FROM olmagan_dokonlar
        WHERE substr(created_at,1,10) BETWEEN ? AND ?
        GROUP BY sabab_text ORDER BY COUNT(*) DESC LIMIT 5""",(d_from,d_to)); sab=c.fetchall()
    c.execute("SELECT COALESCE(SUM(qoldiq),0) FROM nasiya WHERE qoldiq>0"); jami_nasiya=c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT dokon_id) FROM nasiya WHERE qoldiq>0"); nasiyali_d=c.fetchone()[0]
    nasiyasiz_d=max(0,jami_d-nasiyali_d)
    c.execute("""SELECT d.viloyat,COALESCE(SUM(n.qoldiq),0) FROM nasiya n
        JOIN dokonlar d ON d.id=n.dokon_id WHERE n.qoldiq>0 GROUP BY d.viloyat"""); nv=c.fetchall()
    conn.close()
    text=(f"📈 UMUMIY STAT — {label}\n{'━'*26}\n"
          f"🏪 Faol dokonlar: {jami_d} ta\n"
          f"👥 Agentlar: {jami_a} ta\n\n"
          f"💰 Savdo: {fmt(jami_savdo)} ({savdo_n} ta)\n"
          f"💵 Pul olish: {fmt(jami_pul)}\n\n"
          f"📍 Viloyatlar:\n")
    for v in vs: text+=f"  • {v[0]}: {fmt(v[1])}\n"
    if not vs: text+="  — Ma'lumot yo'q\n"
    text+=(f"\n💳 NASIYA HOLATI (joriy)\n{'━'*26}\n"
           f"🔴 Jami nasiya: {fmt(jami_nasiya)}\n"
           f"🏪 Nasiyali dokonlar: {nasiyali_d} ta\n"
           f"✅ Nasiyasiz dokonlar: {nasiyasiz_d} ta\n\n"
           f"📍 Viloyatlar nasiyasi:\n")
    nasiya_map={r[0]:r[1] for r in nv}
    for v,_ in vs:
        n_sum=nasiya_map.get(v,0)
        if n_sum>0: text+=f"  • {v}: {fmt(n_sum)}\n"
    if not nv: text+="  — Nasiya yo'q\n"
    text+="\n❌ Olmagan sabablar:\n"
    for s in sab: text+=f"  • {s[0]}: {s[1]} ta\n"
    if not sab: text+="  — Ma'lumot yo'q\n"
    bot.send_message(uid,text)

@bot.message_handler(func=lambda m:m.text=="📈 Umumiy stat")
def umumiy_stat(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    set_state(uid,"stat_davr",{})
    bot.send_message(uid,"📅 Qaysi davr uchun statistika?",reply_markup=_davr_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="stat_davr")
def s_stat_davr(msg):
    uid=msg.from_user.id
    if msg.text=="🗓 Boshqa sana":
        set_state(uid,"stat_custom",{})
        bot.send_message(uid,"📅 Davr kiriting:\nFormat: 01.05.2026 - 18.05.2026",reply_markup=cancel_kb()); return
    d_from,d_to,label=_parse_davr(msg.text)
    if not d_from: return
    clear_state(uid)
    _send_umumiy_stat(uid,d_from,d_to,label)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="stat_custom")
def s_stat_custom(msg):
    uid=msg.from_user.id
    try:
        parts=[p.strip() for p in msg.text.split("-",1)]
        d_from=datetime.strptime(parts[0],"%d.%m.%Y").strftime("%Y-%m-%d")
        d_to=datetime.strptime(parts[1],"%d.%m.%Y").strftime("%Y-%m-%d")
        label=f"{parts[0]} - {parts[1]}"
    except:
        bot.send_message(uid,"❗ Format: 01.05.2026 - 18.05.2026"); return
    clear_state(uid)
    _send_umumiy_stat(uid,d_from,d_to,label)

@bot.message_handler(func=lambda m:m.text=="👥 Agentlar statistikasi")
def agentlar_stat(msg):
    uid=msg.from_user.id; user=get_user(uid)
    if not user or user[3] not in("supervisor","admin"): return
    conn=get_db();c=conn.cursor()
    bugun=date.today().isoformat(); oy=datetime.now().strftime("%Y-%m")
    c.execute("""
        SELECT u.telegram_id,u.name,u.viloyat,
               COUNT(DISTINCT d.id) as dokon_soni,
               COALESCE(SUM(CASE WHEN substr(s.created_at,1,7)=? THEN s.jami_summa ELSE 0 END),0) as oylik,
               COALESCE(SUM(CASE WHEN substr(s.created_at,1,10)=? THEN s.jami_summa ELSE 0 END),0) as bugungi
        FROM users u
        LEFT JOIN dokonlar d ON d.agent_id=u.telegram_id AND d.holat='faol'
        LEFT JOIN savdolar s ON s.agent_id=u.telegram_id
        WHERE u.role IN ('agent','supervisor')
        GROUP BY u.telegram_id
        ORDER BY oylik DESC, dokon_soni DESC
    """,(oy,bugun))
    rows=c.fetchall()
    if not rows: conn.close(); bot.send_message(uid,"Agentlar yo'q."); return
    # Fetch nasiya and qaytib kirish per agent
    nasiya_map={}; qaytib_map={}
    for r in rows:
        tid=r[0]
        c.execute("SELECT COALESCE(SUM(qoldiq),0) FROM nasiya WHERE agent_id=? AND qoldiq>0",(tid,))
        nasiya_map[tid]=c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM olmagan_dokonlar WHERE agent_id=? AND bajarildi=0 AND qaytish_sanasi IS NOT NULL",(tid,))
        qaytib_map[tid]=c.fetchone()[0]
    conn.close()
    text=f"👥 AGENTLAR STATISTIKASI\n📅 {oy}\n{'━'*28}\n\n"
    for i,r in enumerate(rows,1):
        tid,name,viloyat,dokon_soni,oylik,bugungi=r
        nasiya=nasiya_map.get(tid,0); qaytib=qaytib_map.get(tid,0)
        text+=(f"{i}. {name} ({viloyat})\n"
               f"   🏪 Dokonlar: {dokon_soni} ta\n"
               f"   📦 Oylik savdo: {fmt(oylik)}\n"
               f"   💰 Bugungi: {fmt(bugungi)}\n")
        if nasiya>0: text+=f"   🔴 Nasiya: {fmt(nasiya)}\n"
        if qaytib>0: text+=f"   📋 Qaytib kirish: {qaytib} ta\n"
        text+="\n"
    bot.send_message(uid,text)

def mah_menu_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    kb.add("📋 Mahsulotlar ro'yxati","➕ Mahsulot qo'shish")
    kb.add("✏️ Narx o'zgartirish","🗑 Mahsulot o'chirish")
    kb.add("❌ Bekor qilish"); return kb

def birlik_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=3)
    kb.add("Dona","Kg","Metr"); kb.add("❌ Bekor qilish"); return kb

def tasdiq_kb():
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=2)
    kb.add("✅ Tasdiqlash","❌ Bekor qilish"); return kb

@bot.message_handler(func=lambda m:m.text=="🛍 Mahsulotlar")
def mah_list(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    bot.send_message(uid,"🛍 Mahsulotlar bo'limi:",reply_markup=mah_menu_kb())

@bot.message_handler(func=lambda m:m.text=="📋 Mahsulotlar ro'yxati")
def mah_royxat(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    conn=get_db();c=conn.cursor()
    c.execute("SELECT id,nomi,narx,birlik FROM mahsulotlar WHERE faol=1")
    rows=c.fetchall(); conn.close()
    if not rows: bot.send_message(uid,"❗ Mahsulotlar yo'q.",reply_markup=mah_menu_kb()); return
    text="🛍 Mahsulotlar ro'yxati:\n\n"
    for r in rows: text+=f"  [{r[0]}] {r[1]} — {fmt(r[2])}/{r[3]}\n"
    bot.send_message(uid,text,reply_markup=mah_menu_kb())

@bot.message_handler(func=lambda m:m.text=="➕ Mahsulot qo'shish")
def mah_qosh_start(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    set_state(uid,"mah_qosh_nomi",{})
    bot.send_message(uid,"📝 Mahsulot nomini kiriting:",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="mah_qosh_nomi")
def mah_qosh_nomi(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    data["nomi"]=msg.text.strip(); set_state(uid,"mah_qosh_birlik",data)
    bot.send_message(uid,"📦 Narx turini tanlang:",reply_markup=birlik_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="mah_qosh_birlik")
def mah_qosh_birlik(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text not in("Dona","Kg","Metr"):
        bot.send_message(uid,"❗ Quyidagilardan birini tanlang:",reply_markup=birlik_kb()); return
    data["birlik"]=msg.text.lower(); set_state(uid,"mah_qosh_narx",data)
    bot.send_message(uid,f"💰 {data['nomi']} narxini kiriting (so'mda):",reply_markup=cancel_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="mah_qosh_narx")
def mah_qosh_narx(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    try: narx=int(msg.text.replace(" ","").replace(",",""))
    except: bot.send_message(uid,"❗ Raqam kiriting, masalan: 35000"); return
    data["narx"]=narx; set_state(uid,"mah_qosh_tasdiq",data)
    bot.send_message(uid,
        f"📋 Yangi mahsulot:\n\n"
        f"📝 Nomi: {data['nomi']}\n"
        f"📦 Birlik: {data['birlik']}\n"
        f"💰 Narx: {fmt(narx)}\n\n"
        f"Tasdiqlaysizmi?",reply_markup=tasdiq_kb())

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="mah_qosh_tasdiq")
def mah_qosh_tasdiq(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text!="✅ Tasdiqlash":
        clear_state(uid)
        bot.send_message(uid,"Bekor qilindi.",reply_markup=mah_menu_kb()); return
    conn=get_db();c=conn.cursor()
    c.execute("INSERT INTO mahsulotlar (nomi,narx,birlik) VALUES (?,?,?)",(data["nomi"],data["narx"],data["birlik"]))
    conn.commit();conn.close();clear_state(uid)
    bot.send_message(uid,f"✅ Mahsulot qo'shildi!\n\n📝 {data['nomi']} — {fmt(data['narx'])}/{data['birlik']}",reply_markup=mah_menu_kb())

@bot.message_handler(func=lambda m:m.text=="✏️ Narx o'zgartirish")
def mah_narx_start(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    conn=get_db();c=conn.cursor()
    c.execute("SELECT id,nomi,narx,birlik FROM mahsulotlar WHERE faol=1")
    rows=c.fetchall(); conn.close()
    if not rows: bot.send_message(uid,"❗ Mahsulotlar yo'q.",reply_markup=mah_menu_kb()); return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for r in rows: kb.add(f"✏️{r[0]}|{r[1]} — {fmt(r[2])}/{r[3]}")
    kb.add("❌ Bekor qilish")
    set_state(uid,"mah_narx_tanla",{})
    bot.send_message(uid,"✏️ Narxini o'zgartirish uchun mahsulotni tanlang:",reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="mah_narx_tanla")
def mah_narx_tanla(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text=="❌ Bekor qilish":
        user=get_user(uid); clear_state(uid)
        bot.send_message(uid,"❌ Bekor qilindi",reply_markup=main_kb(user[3])); return
    if not msg.text.startswith("✏️"): return
    try:
        rest=msg.text.lstrip("✏️").lstrip()
        mid=int(rest.split("|")[0])
        nomi=rest.split("|",1)[1].split(" —")[0].strip()
        data["mid"]=mid; data["nomi"]=nomi; set_state(uid,"mah_narx_kirit",data)
        bot.send_message(uid,f"💰 {nomi} uchun yangi narxni kiriting (so'mda):",reply_markup=cancel_kb())
    except Exception as e:
        bot.send_message(uid,f"❗ Mahsulotni qaytadan tanlang ({e})")

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="mah_narx_kirit")
def mah_narx_kirit(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    try: narx=int(msg.text.replace(" ","").replace(",",""))
    except: bot.send_message(uid,"❗ Raqam kiriting, masalan: 40000"); return
    conn=get_db();c=conn.cursor()
    c.execute("UPDATE mahsulotlar SET narx=? WHERE id=?",(narx,data["mid"]))
    conn.commit();conn.close();clear_state(uid)
    bot.send_message(uid,f"✅ Narx yangilandi!\n📝 {data['nomi']} — {fmt(narx)}",reply_markup=mah_menu_kb())

@bot.message_handler(func=lambda m:m.text=="🗑 Mahsulot o'chirish")
def mah_ochir_start(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    conn=get_db();c=conn.cursor()
    c.execute("SELECT id,nomi,narx,birlik FROM mahsulotlar WHERE faol=1")
    rows=c.fetchall(); conn.close()
    if not rows: bot.send_message(uid,"❗ Mahsulotlar yo'q.",reply_markup=mah_menu_kb()); return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True,row_width=1)
    for r in rows: kb.add(f"🗑{r[0]}|{r[1]} — {fmt(r[2])}/{r[3]}")
    kb.add("❌ Bekor qilish")
    set_state(uid,"mah_ochir_tanla",{})
    bot.send_message(uid,"🗑 O'chirish uchun mahsulotni tanlang:",reply_markup=kb)

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="mah_ochir_tanla")
def mah_ochir_tanla(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text=="❌ Bekor qilish":
        user=get_user(uid); clear_state(uid)
        bot.send_message(uid,"❌ Bekor qilindi",reply_markup=main_kb(user[3])); return
    if not msg.text.startswith("🗑"): return
    try:
        rest=msg.text.lstrip("🗑").lstrip()
        mid=int(rest.split("|")[0])
        nomi=rest.split("|",1)[1].split(" —")[0].strip()
        data["mid"]=mid; data["nomi"]=nomi; set_state(uid,"mah_ochir_tasdiq",data)
        bot.send_message(uid,
            f"⚠️ Rostdan ham o'chirasizmi?\n\n📝 {nomi}",
            reply_markup=tasdiq_kb())
    except Exception as e:
        bot.send_message(uid,f"❗ Mahsulotni qaytadan tanlang ({e})")

@bot.message_handler(func=lambda m:get_state(m.from_user.id)["state"]=="mah_ochir_tasdiq")
def mah_ochir_tasdiq(msg):
    uid=msg.from_user.id; data=get_state(uid)["data"]
    if msg.text!="✅ Tasdiqlash":
        clear_state(uid)
        bot.send_message(uid,"Bekor qilindi.",reply_markup=mah_menu_kb()); return
    conn=get_db();c=conn.cursor()
    c.execute("UPDATE mahsulotlar SET faol=0 WHERE id=?",(data["mid"],))
    conn.commit();conn.close();clear_state(uid)
    bot.send_message(uid,f"✅ {data['nomi']} o'chirildi.",reply_markup=mah_menu_kb())

MOTIVATSIYA = [
    "💪 Har bir savdo — g'alaba! Bugun ham hammasi yaxshi bo'ladi!",
    "🚀 Maqsadga qadam qadam yaqinlashamiz. Olg'a, jamoa!",
    "🌟 Kechagi natija — bugungi kuch. Davom eting!",
    "🔥 Eng yaxshi kun — hali oldinda. Ishlang, natija keladi!",
    "🏆 Har bir dokon — yangi imkoniyat. Omad tilaymiz!",
]

def build_bar(value, max_value, width=10):
    if max_value==0: return "░"*width
    filled=round((value/max_value)*width)
    return "▓"*filled+"░"*(width-filled)

@bot.message_handler(commands=["hisobot"])
def hisobot_cmd(msg):
    if not is_admin(msg.from_user.id): return
    send_daily_report(target=msg.from_user.id)

def send_daily_report(target=1261052681):
    try:
        yesterday=(date.today()-timedelta(days=1)).isoformat()
        conn=get_db();c=conn.cursor()
        c.execute("SELECT COALESCE(SUM(jami_summa),0),COUNT(*) FROM savdolar WHERE created_at LIKE ?",(f"{yesterday}%",))
        jami_savdo,savdo_n=c.fetchone()
        c.execute("SELECT COALESCE(SUM(summa),0) FROM pul_olish WHERE created_at LIKE ?",(f"{yesterday}%",))
        jami_pul=c.fetchone()[0]
        c.execute("""SELECT u.name,COALESCE(SUM(s.jami_summa),0) as jami
                     FROM savdolar s JOIN users u ON u.telegram_id=s.agent_id
                     WHERE s.created_at LIKE ?
                     GROUP BY s.agent_id ORDER BY jami DESC LIMIT 3""",(f"{yesterday}%",))
        top3=c.fetchall()
        c.execute("SELECT COUNT(*) FROM dokonlar WHERE created_at LIKE ?",(f"{yesterday}%",))
        yangi_dokon=c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM olmagan_dokonlar WHERE created_at LIKE ?",(f"{yesterday}%",))
        olmagan_n=c.fetchone()[0]
        c.execute("""SELECT sabab_text,COUNT(*) as n FROM olmagan_dokonlar
                     WHERE created_at LIKE ? GROUP BY sabab_text ORDER BY n DESC LIMIT 1""",(f"{yesterday}%",))
        top_sabab=c.fetchone()
        viloyatlar=["Namangan","Farg'ona","Andijon"]
        viloyat_stats=[]
        for v in viloyatlar:
            c.execute("""SELECT COALESCE(SUM(s.jami_summa),0)
                         FROM savdolar s JOIN dokonlar d ON d.id=s.dokon_id
                         WHERE s.created_at LIKE ? AND d.viloyat=?""",(f"{yesterday}%",v))
            viloyat_stats.append((v,c.fetchone()[0]))
        conn.close()
        import random; motiv=random.choice(MOTIVATSIYA)
        text=(f"📊 KUNLIK HISOBOT — {yesterday}\n{'━'*28}\n\n"
              f"💰 Kechagi savdo:\n"
              f"   📦 Jami: {fmt(jami_savdo)} ({savdo_n} ta)\n"
              f"   💵 Pul olish: {fmt(jami_pul)}\n\n"
              f"🏆 Top 3 agent:\n")
        medals=["🥇","🥈","🥉"]
        for i,(name,summa) in enumerate(top3): text+=f"   {medals[i]} {name}: {fmt(summa)}\n"
        if not top3: text+="   — Ma'lumot yo'q\n"
        text+=(f"\n🏪 Yangi dokonlar: {yangi_dokon} ta\n"
               f"❌ Tovar olmagan: {olmagan_n} ta")
        if top_sabab: text+=f" ({top_sabab[0]})"
        text+=f"\n\n📍 Viloyatlar bo'yicha:\n"
        for v,vs in viloyat_stats: text+=f"   • {v}: {fmt(vs)}\n"
        text+=f"\n{motiv}"
        bot.send_message(target,text)
    except Exception as e:
        try: bot.send_message(target,f"❗ Hisobot xatosi: {e}")
        except: pass

@bot.message_handler(commands=["haftalik"])
def haftalik_cmd(msg):
    if not is_admin(msg.from_user.id): return
    try:
        today=date.today()
        days=[(today-timedelta(days=i)).isoformat() for i in range(6,-1,-1)]
        prev_days=[(today-timedelta(days=i)).isoformat() for i in range(13,6,-1)]
        conn=get_db();c=conn.cursor()

        daily=[]
        for d in days:
            c.execute("SELECT COALESCE(SUM(jami_summa),0),COUNT(*) FROM savdolar WHERE created_at LIKE ?",(f"{d}%",))
            s,n=c.fetchone()
            c.execute("SELECT COALESCE(SUM(summa),0) FROM pul_olish WHERE created_at LIKE ?",(f"{d}%",))
            p=c.fetchone()[0]
            daily.append((d,s,n,p))

        jami_savdo=sum(x[1] for x in daily)
        jami_pul=sum(x[3] for x in daily)
        max_savdo=max((x[1] for x in daily),default=1) or 1

        prev_savdo=0
        for d in prev_days:
            c.execute("SELECT COALESCE(SUM(jami_summa),0) FROM savdolar WHERE created_at LIKE ?",(f"{d}%",))
            prev_savdo+=c.fetchone()[0]

        c.execute("""SELECT u.name,COALESCE(SUM(s.jami_summa),0) as jami
                     FROM savdolar s JOIN users u ON u.telegram_id=s.agent_id
                     WHERE s.created_at >= ?
                     GROUP BY s.agent_id ORDER BY jami DESC LIMIT 3""",(days[0],))
        top3=c.fetchall()

        viloyatlar=["Namangan","Farg'ona","Andijon"]
        viloyat_stats=[]
        for v in viloyatlar:
            c.execute("""SELECT COALESCE(SUM(s.jami_summa),0)
                         FROM savdolar s JOIN dokonlar d ON d.id=s.dokon_id
                         WHERE s.created_at >= ? AND d.viloyat=?""",(days[0],v))
            viloyat_stats.append((v,c.fetchone()[0]))
        conn.close()

        best=max(daily,key=lambda x:x[1])
        kun_nomlari={"Mon":"Dush","Tue":"Sesh","Wed":"Chor","Thu":"Pay","Fri":"Jum","Sat":"Shan","Sun":"Yak"}
        wow_diff=jami_savdo-prev_savdo
        wow_pct=f"+{round((wow_diff/prev_savdo)*100)}%" if prev_savdo>0 and wow_diff>=0 else (f"{round((wow_diff/prev_savdo)*100)}%" if prev_savdo>0 else "—")
        wow_icon="📈" if wow_diff>=0 else "📉"

        text=(f"📅 HAFTALIK HISOBOT\n"
              f"{days[0]} — {days[-1]}\n{'━'*28}\n\n"
              f"💰 Jami savdo: {fmt(jami_savdo)}\n"
              f"💵 Jami pul olish: {fmt(jami_pul)}\n"
              f"{wow_icon} O'tgan hafta: {fmt(prev_savdo)} ({wow_pct})\n\n"
              f"📊 Kunlik ko'rsatkich:\n")
        for d,s,n,p in daily:
            from datetime import datetime as dt
            weekday=kun_nomlari.get(dt.strptime(d,"%Y-%m-%d").strftime("%a"),d[-5:])
            bar=build_bar(s,max_savdo)
            text+=f"  {weekday} {bar} {fmt(s)}\n"

        text+=f"\n🏆 Eng yaxshi kun: {best[0]} ({fmt(best[1])})\n\n"
        text+="🥇 Top 3 agent:\n"
        medals=["🥇","🥈","🥉"]
        for i,(name,summa) in enumerate(top3): text+=f"   {medals[i]} {name}: {fmt(summa)}\n"
        if not top3: text+="   — Ma'lumot yo'q\n"
        text+="\n📍 Viloyatlar bo'yicha:\n"
        for v,vs in viloyat_stats: text+=f"   • {v}: {fmt(vs)}\n"
        bot.send_message(msg.from_user.id,text)
    except Exception as e:
        bot.send_message(msg.from_user.id,f"❗ Haftalik hisobot xatosi: {e}")

@bot.message_handler(commands=["oylik"])
def oylik_cmd(msg):
    if not is_admin(msg.from_user.id): return
    try:
        today=date.today()
        oy=today.strftime("%Y-%m")
        from datetime import datetime as dt
        if today.month==1: prev_oy=f"{today.year-1}-12"
        else: prev_oy=f"{today.year}-{str(today.month-1).zfill(2)}"

        conn=get_db();c=conn.cursor()

        c.execute("SELECT COALESCE(SUM(jami_summa),0),COUNT(*) FROM savdolar WHERE created_at LIKE ?",(f"{oy}%",))
        jami_savdo,savdo_n=c.fetchone()
        c.execute("SELECT COALESCE(SUM(summa),0) FROM pul_olish WHERE created_at LIKE ?",(f"{oy}%",))
        jami_pul=c.fetchone()[0]
        c.execute("SELECT COALESCE(SUM(jami_summa),0) FROM savdolar WHERE created_at LIKE ?",(f"{prev_oy}%",))
        prev_savdo=c.fetchone()[0]

        c.execute("""SELECT strftime('%Y-%m-%d',created_at) as kun,
                            COALESCE(SUM(jami_summa),0)
                     FROM savdolar WHERE created_at LIKE ?
                     GROUP BY kun ORDER BY kun""",(f"{oy}%",))
        daily_rows=c.fetchall()

        c.execute("""SELECT u.name,COALESCE(SUM(s.jami_summa),0) as jami
                     FROM savdolar s JOIN users u ON u.telegram_id=s.agent_id
                     WHERE s.created_at LIKE ?
                     GROUP BY s.agent_id ORDER BY jami DESC LIMIT 3""",(f"{oy}%",))
        top3=c.fetchall()

        viloyatlar=["Namangan","Farg'ona","Andijon"]
        viloyat_stats=[]
        for v in viloyatlar:
            c.execute("""SELECT COALESCE(SUM(s.jami_summa),0)
                         FROM savdolar s JOIN dokonlar d ON d.id=s.dokon_id
                         WHERE s.created_at LIKE ? AND d.viloyat=?""",(f"{oy}%",v))
            viloyat_stats.append((v,c.fetchone()[0]))

        c.execute("SELECT COUNT(*) FROM dokonlar WHERE created_at LIKE ?",(f"{oy}%",))
        yangi_dokon=c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM olmagan_dokonlar WHERE created_at LIKE ?",(f"{oy}%",))
        olmagan_n=c.fetchone()[0]
        conn.close()

        wow_diff=jami_savdo-prev_savdo
        wow_pct=f"+{round((wow_diff/prev_savdo)*100)}%" if prev_savdo>0 and wow_diff>=0 else (f"{round((wow_diff/prev_savdo)*100)}%" if prev_savdo>0 else "—")
        wow_icon="📈" if wow_diff>=0 else "📉"
        max_day=max((x[1] for x in daily_rows),default=1) or 1
        best_day=max(daily_rows,key=lambda x:x[1]) if daily_rows else None

        text=(f"📆 OYLIK HISOBOT — {oy}\n{'━'*28}\n\n"
              f"💰 Jami savdo: {fmt(jami_savdo)} ({savdo_n} ta)\n"
              f"💵 Jami pul olish: {fmt(jami_pul)}\n"
              f"{wow_icon} O'tgan oy: {fmt(prev_savdo)} ({wow_pct})\n"
              f"🏪 Yangi dokonlar: {yangi_dokon} ta\n"
              f"❌ Tovar olmagan: {olmagan_n} ta\n\n"
              f"📊 Kunlik ko'rsatkich:\n")
        for d,s in daily_rows:
            bar=build_bar(s,max_day,width=8)
            text+=f"  {d[-2:]} {bar} {fmt(s)}\n"
        if not daily_rows: text+="  — Ma'lumot yo'q\n"

        if best_day: text+=f"\n🏆 Eng yaxshi kun: {best_day[0]} ({fmt(best_day[1])})\n"
        text+="\n🥇 Top 3 agent:\n"
        medals=["🥇","🥈","🥉"]
        for i,(name,summa) in enumerate(top3): text+=f"   {medals[i]} {name}: {fmt(summa)}\n"
        if not top3: text+="   — Ma'lumot yo'q\n"
        text+="\n📍 Viloyatlar bo'yicha:\n"
        for v,vs in viloyat_stats: text+=f"   • {v}: {fmt(vs)}\n"
        bot.send_message(msg.from_user.id,text)
    except Exception as e:
        bot.send_message(msg.from_user.id,f"❗ Oylik hisobot xatosi: {e}")

def _build_multistop_maps_url(items):
    """Build a Google Maps directions URL with multiple waypoints (up to ~9)."""
    coords=[(r[6],r[7]) for r in items if r[6] and r[7]]
    if not coords: return None
    coords=coords[:10]  # Google Maps limit
    if len(coords)==1:
        return f"https://maps.google.com/?q={coords[0][0]},{coords[0][1]}"
    base="https://www.google.com/maps/dir/?api=1"
    dest=f"{coords[-1][0]},{coords[-1][1]}"
    waypoints="|".join(f"{lat},{lon}" for lat,lon in coords[:-1])
    return f"{base}&destination={dest}&waypoints={waypoints}&travelmode=driving"

def _format_agent_section(agent_name, items, with_header=True):
    """Build a per-agent revisit block, grouped by viloyat → hudud."""
    text=""
    if with_header:
        text=f"📋 BUGUN KIRILADIGAN DOKONLAR\n\n👤 Agent: {agent_name}\n{'━'*26}\n"
    else:
        text=f"\n👤 Agent: {agent_name}  ({len(items)} ta)\n{'━'*26}\n"
    # Group by viloyat → hudud
    from collections import defaultdict
    by_vil=defaultdict(lambda: defaultdict(list))
    for r in items:
        vil=r[4] or "—"; hud=r[5] or "—"
        by_vil[vil][hud].append(r)
    idx=0
    for vil, huds in by_vil.items():
        v_total=sum(len(v) for v in huds.values())
        text+=f"\n📍 {vil.upper()} ({v_total} ta)\n"
        for hud, rows in huds.items():
            text+=f"\n  🏘 {hud} ({len(rows)} ta):\n"
            for r in rows:
                idx+=1
                _,_,nomi,egasi,_,_,lat,lon,last_d,_,_ = r
                maps=f"https://maps.google.com/?q={lat},{lon}" if lat and lon else "—"
                last_s=last_d[:10] if last_d else "—"
                text+=(f"    {idx}. 🏪 {nomi}\n"
                       f"       👤 {egasi or '—'} | 📅 {last_s}\n"
                       f"       🗺 {maps}\n")
    # Multi-stop route link
    route=_build_multistop_maps_url(items)
    if route:
        text+=f"\n{'━'*26}\n🚗 MARSHRUT (ko'p to'xtash): {route}\n"
    return text

def _send_long(chat_id, text):
    """Telegram 4096 char limit-ga moslab yuborish."""
    LIM=3800
    while text:
        chunk=text[:LIM]
        # break at newline if possible
        if len(text)>LIM:
            nl=chunk.rfind("\n")
            if nl>1000: chunk=text[:nl]
        try: bot.send_message(chat_id, chunk, disable_web_page_preview=True)
        except: pass
        text=text[len(chunk):]

def send_today_revisits(target_agent=None, target_admin=None):
    """Cron / manual trigger for today's revisit lists.
    - target_agent: agent uchun faqat o'zinikini yuboradi
    - target_admin: admin uchun BARCHA agentlarning to'liq ro'yxatini yuboradi
    - Ikkalasi None bo'lsa: har agentga o'zinikini + har adminga umumiy ro'yxat (cron)
    """
    today=date.today().isoformat()
    conn=get_db();c=conn.cursor()
    if target_agent:
        c.execute("""SELECT r.id, r.dokon_id, d.nomi, d.egasi, d.viloyat, d.hudud, d.latitude, d.longitude,
                            r.last_order_date, u.name, r.agent_id
                     FROM revisitlar r
                     JOIN dokonlar d ON d.id=r.dokon_id
                     LEFT JOIN users u ON u.telegram_id=r.agent_id
                     WHERE r.revisit_date<=? AND r.status='pending' AND r.agent_id=?
                     ORDER BY d.nomi""",(today,target_agent))
    else:
        c.execute("""SELECT r.id, r.dokon_id, d.nomi, d.egasi, d.viloyat, d.hudud, d.latitude, d.longitude,
                            r.last_order_date, u.name, r.agent_id
                     FROM revisitlar r
                     JOIN dokonlar d ON d.id=r.dokon_id
                     LEFT JOIN users u ON u.telegram_id=r.agent_id
                     WHERE r.revisit_date<=? AND r.status='pending'
                     ORDER BY r.agent_id, d.nomi""",(today,))
    rows=c.fetchall(); conn.close()
    if not rows:
        msg_empty="✅ Bugun qayta kiriladigan dokon yo'q."
        if target_agent:
            try: bot.send_message(target_agent,msg_empty)
            except: pass
        if target_admin:
            try: bot.send_message(target_admin,msg_empty)
            except: pass
        if not target_agent and not target_admin:
            for aid in all_admin_ids():
                try: bot.send_message(aid,msg_empty)
                except: pass
        return 0
    # Group by agent
    from collections import defaultdict
    by_agent=defaultdict(list)
    for r in rows: by_agent[r[10]].append(r)
    # If only an admin requested — send them a single consolidated message
    if target_admin:
        text=f"📋 BUGUN KIRILADIGAN DOKONLAR (UMUMIY)\n\n📦 Jami: {len(rows)} ta dokon | 👥 {len(by_agent)} ta agent\n"
        for agent_id, items in by_agent.items():
            agent_name=items[0][9] or f"ID {agent_id}"
            text+=_format_agent_section(agent_name, items, with_header=False)
        _send_long(target_admin, text)
        return len(by_agent)
    # Otherwise: send to each agent (theirs) + to each admin (full list) — for cron / agent-only path
    sent=0
    for agent_id, items in by_agent.items():
        if target_agent and target_agent!=agent_id: continue
        agent_name=items[0][9] or "—"
        text=_format_agent_section(agent_name, items, with_header=True)
        text+=f"\n📦 Jami: {len(items)} ta dokon"
        try:
            _send_long(agent_id, text); sent+=1
        except Exception as e:
            print(f"⚠️ Revisit send failed for {agent_id}: {e}")
    # Cron — also send admins the full consolidated list
    if not target_agent:
        admin_text=f"📋 BUGUN KIRILADIGAN DOKONLAR (UMUMIY)\n\n📦 Jami: {len(rows)} ta dokon | 👥 {len(by_agent)} ta agent\n"
        for agent_id, items in by_agent.items():
            agent_name=items[0][9] or f"ID {agent_id}"
            admin_text+=_format_agent_section(agent_name, items, with_header=False)
        for aid in all_admin_ids():
            _send_long(aid, admin_text)
    return sent

@bot.message_handler(commands=["qayta_kirish"])
def qayta_kirish_cmd(msg):
    """Manual trigger: agent sees own list; admin sees full consolidated list."""
    uid=msg.from_user.id
    if is_admin(uid):
        send_today_revisits(target_admin=uid)
    else:
        send_today_revisits(target_agent=uid)

def run_scheduler():
    # Tashkent vaqti bilan (UTC+5). Eski schedule versiyalari uchun fallback — UTC ekvivalent.
    tz="Asia/Tashkent"
    try:
        schedule.every().day.at("08:00", tz).do(send_daily_report)
        schedule.every().day.at("07:00", tz).do(send_today_revisits)
        schedule.every().monday.at("09:00", tz).do(send_weekly_lost_alert)
        schedule.every().monday.at("09:30", tz).do(send_weekly_old_nasiya_alert)
        schedule.every().day.at("20:00", tz).do(send_monthly_rating_if_last_day)
        print(f"⏰ Scheduler started (TZ={tz}): daily 08:00, revisits 07:00, lost-alert Mon 09:00, old-nasiya Mon 09:30, rating last-day 20:00")
    except TypeError:
        # Old `schedule` lib — convert manually (Tashkent = UTC+5)
        schedule.every().day.at("03:00").do(send_daily_report)   # 08:00 Tashkent
        schedule.every().day.at("02:00").do(send_today_revisits) # 07:00 Tashkent
        schedule.every().monday.at("04:00").do(send_weekly_lost_alert) # 09:00 Tashkent
        schedule.every().monday.at("04:30").do(send_weekly_old_nasiya_alert) # 09:30 Tashkent
        schedule.every().day.at("15:00").do(send_monthly_rating_if_last_day) # 20:00 Tashkent
        print("⏰ Scheduler started (UTC fallback): daily 03:00, revisits 02:00, lost-alert Mon 04:00, old-nasiya Mon 04:30, rating 15:00 UTC")
    while True:
        schedule.run_pending()
        time.sleep(30)

def run_health_server():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/health", "/", "/healthz", "/ping"):
                self.send_response(200)
                self.send_header("Content-Type","text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self,*a): pass
    env_port = os.environ.get("PORT")
    ports = []
    if env_port:
        try: ports.append(int(env_port))
        except ValueError: pass
    ports += [8080, 8443, 9000, 7860, 5050]
    for port in ports:
        try:
            print(f"🌐 Health server listening on port {port} (/health)")
            HTTPServer(("0.0.0.0", port), H).serve_forever()
            break
        except OSError as e:
            print(f"⚠️ Port {port} unavailable ({e}), trying next...")
            continue

def _pdf_safe(s):
    if s is None: return "—"
    return str(s).replace("ʻ","'").replace("ʼ","'").replace("'","'").replace("'","'")

@bot.message_handler(commands=["dokonlar_pdf"])
@bot.message_handler(func=lambda m:m.text=="📄 Dokonlar PDF")
def dokonlar_pdf(msg):
    uid=msg.from_user.id
    if not is_admin(uid): return
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.units import mm
    except Exception as e:
        bot.send_message(uid,f"❗ reportlab kerak: {e}"); return

    conn=get_db(); c=conn.cursor()
    c.execute("""SELECT d.id, d.nomi, d.egasi, d.telefon, d.viloyat,
                        COALESCE(u.name,'—'), d.holat
                 FROM dokonlar d
                 LEFT JOIN users u ON d.agent_id=u.telegram_id
                 ORDER BY d.holat DESC, d.nomi""")
    rows=c.fetchall(); conn.close()
    if not rows:
        bot.send_message(uid,"❗ Dokon yo'q"); return

    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf, pagesize=landscape(A4),
        leftMargin=10*mm, rightMargin=10*mm, topMargin=10*mm, bottomMargin=10*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('t', parent=styles['Title'], fontSize=16, alignment=1)
    sub=ParagraphStyle('s', parent=styles['Normal'], fontSize=10, alignment=1, textColor=colors.grey)

    faol=sum(1 for r in rows if r[6]=='faol')
    story=[
        Paragraph("TOP MART — Dokonlar ro'yxati", title),
        Paragraph(f"Sana: {datetime.now().strftime('%d.%m.%Y %H:%M')}  |  Jami: {len(rows)} ta  |  Faol: {faol}  |  Nofaol: {len(rows)-faol}", sub),
        Spacer(1, 4*mm),
    ]

    header=["№","Nomi","Egasi","Telefon","Viloyat","Agent","Holat"]
    data=[header]
    for i,r in enumerate(rows,1):
        data.append([
            str(i),
            _pdf_safe(r[1])[:35],
            _pdf_safe(r[2])[:25],
            _pdf_safe(r[3])[:20],
            _pdf_safe(r[4])[:20],
            _pdf_safe(r[5])[:25],
            "✓" if r[6]=='faol' else "✗",
        ])

    tbl=Table(data, repeatRows=1,
        colWidths=[10*mm, 60*mm, 45*mm, 35*mm, 35*mm, 50*mm, 15*mm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1e3a8a')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
        ('FONTSIZE',(0,0),(-1,0),10),
        ('FONTSIZE',(0,1),(-1,-1),9),
        ('GRID',(0,0),(-1,-1),0.4,colors.HexColor('#9ca3af')),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white, colors.HexColor('#f3f4f6')]),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(0,-1),'CENTER'),
        ('ALIGN',(-1,0),(-1,-1),'CENTER'),
        ('LEFTPADDING',(0,0),(-1,-1),4),
        ('RIGHTPADDING',(0,0),(-1,-1),4),
        ('TOPPADDING',(0,0),(-1,-1),3),
        ('BOTTOMPADDING',(0,0),(-1,-1),3),
    ]))
    story.append(tbl)
    doc.build(story)
    buf.seek(0)
    fname=f"dokonlar_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    bot.send_document(uid, (fname, buf.read()),
        caption=f"📄 Dokonlar ro'yxati\n🗓 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n📊 Jami: {len(rows)} ta (✓ {faol} faol, ✗ {len(rows)-faol} nofaol)")

if __name__=="__main__":
    init_db()
    threading.Thread(target=run_scheduler,daemon=True).start()
    threading.Thread(target=run_health_server,daemon=True).start()
    print("✅ TOP MART bot ishga tushdi!")
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
