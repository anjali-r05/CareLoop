import os, re, sqlite3, json, secrets, functools, hashlib, csv, io, uuid
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, g, render_template, request, redirect, url_for, flash, jsonify, session
from dotenv import load_dotenv
ROOT=Path(__file__).resolve().parent
# Load this project's own .env regardless of the shell's current directory.
# On first run, create a local template; the user must still add their own key.
ENV_FILE = ROOT / '.env'
if not ENV_FILE.exists() and (ROOT / '.env.example').exists():
    import shutil
    shutil.copyfile(ROOT / '.env.example', ENV_FILE)
load_dotenv(dotenv_path=ENV_FILE, override=False)
GNANI_LAST_SUCCESS = False
(ROOT/'instance').mkdir(exist_ok=True)
DB=ROOT/'instance'/'careloop.db'
AUTH_DB=ROOT/'instance'/'accounts.db'
USERS_DIR=ROOT/'instance'/'workspaces'
USERS_DIR.mkdir(exist_ok=True)
app=Flask(__name__)
app.secret_key=os.getenv('SECRET_KEY') or 'careloop-development-key-change-before-deployment-2026'
app.config['TEMPLATES_AUTO_RELOAD']=True
app.config['SESSION_COOKIE_HTTPONLY']=True
app.config['SESSION_COOKIE_SAMESITE']='Lax'
app.permanent_session_lifetime=timedelta(hours=12)

SCHEMA='''
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,category TEXT NOT NULL DEFAULT 'Care',due_at TEXT NOT NULL,owner TEXT NOT NULL DEFAULT 'Me',priority TEXT NOT NULL DEFAULT 'Normal',status TEXT NOT NULL DEFAULT 'Open',acknowledged INTEGER NOT NULL DEFAULT 0,source TEXT NOT NULL DEFAULT 'Manual',created_at TEXT NOT NULL, recurrence TEXT NOT NULL DEFAULT 'None', recurrence_parent_id INTEGER);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,body TEXT NOT NULL,tag TEXT NOT NULL DEFAULT 'Observation',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER,title TEXT NOT NULL,detail TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Open',created_at TEXT NOT NULL,FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_activity(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,event_type TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS action_steps(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,title TEXT NOT NULL,is_done INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,completed_at TEXT,FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS task_dependencies(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,depends_on_id INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(task_id,depends_on_id),FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,FOREIGN KEY(depends_on_id) REFERENCES tasks(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS task_updates(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,update_type TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS journeys(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,journey_type TEXT NOT NULL,goal TEXT NOT NULL DEFAULT '',start_date TEXT NOT NULL,target_date TEXT,status TEXT NOT NULL DEFAULT 'Active',created_at TEXT NOT NULL,completed_at TEXT);
CREATE TABLE IF NOT EXISTS journey_events(id INTEGER PRIMARY KEY AUTOINCREMENT,journey_id INTEGER NOT NULL,event_type TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(journey_id) REFERENCES journeys(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS intakes(id INTEGER PRIMARY KEY AUTOINCREMENT,request_text TEXT NOT NULL,need_type TEXT NOT NULL,requested_by TEXT NOT NULL,preferred_by TEXT NOT NULL DEFAULT '',constraints TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'New',created_at TEXT NOT NULL,converted_task_id INTEGER);
CREATE TABLE IF NOT EXISTS handoffs(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,from_person TEXT NOT NULL,to_person TEXT NOT NULL,summary TEXT NOT NULL,watch_items TEXT NOT NULL DEFAULT '',next_steps TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'Pending',created_at TEXT NOT NULL,acknowledged_at TEXT);
CREATE TABLE IF NOT EXISTS handoff_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,handoff_id INTEGER NOT NULL,task_id INTEGER NOT NULL,FOREIGN KEY(handoff_id) REFERENCES handoffs(id) ON DELETE CASCADE,FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,UNIQUE(handoff_id,task_id));
CREATE TABLE IF NOT EXISTS handoff_events(id INTEGER PRIMARY KEY AUTOINCREMENT,handoff_id INTEGER NOT NULL,event_type TEXT NOT NULL,actor TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,FOREIGN KEY(handoff_id) REFERENCES handoffs(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS automation_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
'''
def connect():
    if 'db' not in g:
        uid=session.get('user_id')
        path=(USERS_DIR/f'{int(uid)}.db') if uid else DB
        g.db=sqlite3.connect(path); g.db.row_factory=sqlite3.Row; g.db.execute('PRAGMA foreign_keys=ON'); g.db.executescript(SCHEMA); ensure_workspace_schema(g.db)
    return g.db

def account_db():
    c=sqlite3.connect(AUTH_DB); c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,created_at TEXT NOT NULL)')
    c.commit(); return c

def login_required(fn):
    @functools.wraps(fn)
    def wrapped(*args,**kwargs):
        if not session.get('user_id'): return redirect(url_for('login',next=request.path))
        return fn(*args,**kwargs)
    return wrapped

@app.before_request
def require_authentication():
    public={'home','login','register','logout','static'}
    if request.endpoint and request.endpoint not in public and not session.get('user_id'):
        return redirect(url_for('login',next=request.path))
@app.teardown_appcontext
def close_db(exc=None):
    d=g.pop('db',None)
    if d: d.close()
def stamp(): return datetime.now().replace(microsecond=0).isoformat(timespec='minutes')
def init_db():
    c=account_db(); c.close()
    c=sqlite3.connect(DB); c.executescript(SCHEMA); c.commit(); c.close()

def ensure_workspace_schema(db):
    # Additive migration: existing user workspaces and records are preserved.
    cols={r['name'] for r in db.execute('PRAGMA table_info(tasks)').fetchall()}
    for name,decl in [('recurrence',"TEXT NOT NULL DEFAULT 'None'"),('recurrence_parent_id','INTEGER'),('blocked_reason',"TEXT NOT NULL DEFAULT ''"),('completion_note',"TEXT NOT NULL DEFAULT ''"),('acknowledged_at','TEXT'),('completed_at','TEXT'),('last_touched_at','TEXT')]:
        if name not in cols: db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {decl}')
    db.execute("CREATE TABLE IF NOT EXISTS task_activity(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,event_type TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE)")
    db.execute("CREATE TABLE IF NOT EXISTS action_steps(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,title TEXT NOT NULL,is_done INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,completed_at TEXT,FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE)")
    db.execute("CREATE TABLE IF NOT EXISTS task_dependencies(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,depends_on_id INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(task_id,depends_on_id),FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,FOREIGN KEY(depends_on_id) REFERENCES tasks(id) ON DELETE CASCADE)")
    db.execute("CREATE TABLE IF NOT EXISTS task_updates(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,update_type TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE)")
    if 'journey_id' not in cols: db.execute('ALTER TABLE tasks ADD COLUMN journey_id INTEGER')
    note_cols={r['name'] for r in db.execute('PRAGMA table_info(notes)').fetchall()}
    if 'task_id' not in note_cols: db.execute('ALTER TABLE notes ADD COLUMN task_id INTEGER')
    if 'journey_id' not in note_cols: db.execute('ALTER TABLE notes ADD COLUMN journey_id INTEGER')
    db.execute("CREATE TABLE IF NOT EXISTS journeys(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,journey_type TEXT NOT NULL,goal TEXT NOT NULL DEFAULT '',start_date TEXT NOT NULL,target_date TEXT,status TEXT NOT NULL DEFAULT 'Active',created_at TEXT NOT NULL,completed_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS journey_events(id INTEGER PRIMARY KEY AUTOINCREMENT,journey_id INTEGER NOT NULL,event_type TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(journey_id) REFERENCES journeys(id) ON DELETE CASCADE)")
    db.execute("CREATE TABLE IF NOT EXISTS intakes(id INTEGER PRIMARY KEY AUTOINCREMENT,request_text TEXT NOT NULL,need_type TEXT NOT NULL,requested_by TEXT NOT NULL,preferred_by TEXT NOT NULL DEFAULT '',constraints TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'New',created_at TEXT NOT NULL,converted_task_id INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS handoffs(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,from_person TEXT NOT NULL,to_person TEXT NOT NULL,summary TEXT NOT NULL,watch_items TEXT NOT NULL DEFAULT '',next_steps TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'Pending',created_at TEXT NOT NULL,acknowledged_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS handoff_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,handoff_id INTEGER NOT NULL,task_id INTEGER NOT NULL,FOREIGN KEY(handoff_id) REFERENCES handoffs(id) ON DELETE CASCADE,FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,UNIQUE(handoff_id,task_id))")
    handoff_cols={r['name'] for r in db.execute('PRAGMA table_info(handoffs)').fetchall()}
    for name,decl in [('priority',"TEXT NOT NULL DEFAULT 'Normal'"),('due_at','TEXT'),('viewed_at','TEXT'),('started_at','TEXT'),('completed_at','TEXT'),('last_updated_at','TEXT')]:
        if name not in handoff_cols: db.execute(f'ALTER TABLE handoffs ADD COLUMN {name} {decl}')
    db.execute("CREATE TABLE IF NOT EXISTS handoff_events(id INTEGER PRIMARY KEY AUTOINCREMENT,handoff_id INTEGER NOT NULL,event_type TEXT NOT NULL,actor TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,FOREIGN KEY(handoff_id) REFERENCES handoffs(id) ON DELETE CASCADE)")
    db.execute("CREATE TABLE IF NOT EXISTS automation_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL)")
    db.commit()

def record_task_event(task_id,event_type,detail):
    db=connect(); db.execute('INSERT INTO task_activity(task_id,event_type,detail,created_at) VALUES(?,?,?,?)',(task_id,event_type,detail[:1000],stamp())); db.commit()

def audit(action,detail):
    connect().execute('INSERT INTO audit(action,detail,created_at) VALUES(?,?,?)',(action,detail,stamp())); connect().commit()
def all_tasks(): return connect().execute("SELECT * FROM tasks ORDER BY CASE status WHEN 'Open' THEN 0 ELSE 1 END,due_at").fetchall()
def gaps():
    """Explainable coordination checks over saved task records (not clinical inference)."""
    tasks=all_tasks(); out=[]; pending=[t for t in tasks if t['status']=='Open']
    for t in pending:
        title=t['title'].lower()
        if t['owner'].strip().lower() in ('','unassigned','none','tbd'):
            out.append({'kind':'Ownership','level':'Review','title':'No accountable owner','detail':f"{t['title']} has no confirmed owner in the care plan.",'task_id':t['id'],'action':'Assign a person who has agreed to own this action.'})
        if any(w in title for w in ('appointment','doctor','clinic','hospital','checkup','visit')):
            same=[x for x in pending if x['id']!=t['id'] and x['due_at'][:10]==t['due_at'][:10] and any(w in x['title'].lower() for w in ('prepare','transport','ride','document','question','bring','confirm'))]
            if not same:
                out.append({'kind':'Preparation','level':'Consider','title':'Visit preparation not linked','detail':f"{t['title']} has no obvious preparation, transport or document task on the same date.",'task_id':t['id'],'action':'Create a preparation action if it applies to this visit.'})
        remaining=connect().execute('SELECT COUNT(*) FROM action_steps WHERE task_id=? AND is_done=0',(t['id'],)).fetchone()[0]
        if remaining: out.append({'kind':'Checklist','level':'Consider','title':'Action has unfinished steps','detail':f"{t['title']} has {remaining} checklist step(s) remaining.",'task_id':t['id'],'action':'Review remaining steps and confirm who will complete them.'})
        waiting=connect().execute("SELECT COUNT(*) FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_id WHERE d.task_id=? AND p.status!='Done'",(t['id'],)).fetchone()[0]
        if waiting: out.append({'kind':'Dependency','level':'Review','title':'Prerequisite action still open','detail':f"{t['title']} is waiting on {waiting} prerequisite action(s).",'task_id':t['id'],'action':'Review prerequisite owner and timing before proceeding.'})
        if t['priority']=='High' and not t['acknowledged']:
            out.append({'kind':'Acknowledgement','level':'Review','title':'High-priority action not acknowledged','detail':f"{t['title']} is marked high priority, but the plan has no acknowledgement recorded.",'task_id':t['id'],'action':'Ask the assigned owner to acknowledge the task.'})
        try:
            due=datetime.fromisoformat(t['due_at'])
            if due < datetime.now()-timedelta(days=7):
                out.append({'kind':'Staleness','level':'Review','title':'Open action may be stale','detail':f"{t['title']} is still open and its due time is more than seven days old.",'task_id':t['id'],'action':'Confirm whether this action is still needed, then update or close it.'})
        except (ValueError, TypeError):
            pass
    # Same owner and same exact time can create a direct collision.
    for i,a in enumerate(pending):
        for b in pending[i+1:]:
            if a['owner'].strip().lower()==b['owner'].strip().lower() and a['due_at'][:16]==b['due_at'][:16]:
                out.append({'kind':'Schedule','level':'Review','title':'Owner has overlapping actions','detail':f"{a['title']} and {b['title']} share the same owner and scheduled time.",'task_id':a['id'],'action':'Confirm the schedule or reassign one of the actions.'})
    # Duplicate detection is based on normalized titles and reports the actual matching record.
    seen={}
    for t in pending:
        normalized=re.sub(r'[^a-z0-9]+',' ',t['title'].lower()).strip()
        if normalized and normalized in seen:
            other=seen[normalized]
            out.append({'kind':'Duplicate','level':'Consider','title':'Possible duplicate action','detail':f"{t['title']} closely matches another open action (#{other['id']}).",'task_id':t['id'],'action':'Compare both actions and keep one if they represent the same work.'})
        else:
            seen[normalized]=t
    # Capacity signal: three or more open actions assigned to one person on the same date.
    buckets={}
    for t in pending:
        owner=t['owner'].strip().lower()
        if owner in ('','unassigned','none','tbd'): continue
        key=(owner,t['due_at'][:10]); buckets.setdefault(key,[]).append(t)
    for (owner,day),items in buckets.items():
        if len(items)>=3:
            out.append({'kind':'Load','level':'Consider','title':'Daily load may need balancing','detail':f"{items[0]['owner']} has {len(items)} open actions scheduled for {day}.",'task_id':items[0]['id'],'action':'Review priorities and whether another willing care partner can take an action.'})
    return out

def _extract_request_datetime(original):
    """Extract explicit English/Hindi dates and times; return ISO, hints, and date-only value."""
    low=original.lower(); now=datetime.now(); hints=[]; day=None; time_value=None
    month_map={name.lower():i for i,name in enumerate(['January','February','March','April','May','June','July','August','September','October','November','December'],1)}
    month_pattern='|'.join(month_map)
    patterns=[
        (rf'\b(\d{{1,2}})\s+({month_pattern})\s+(20\d{{2}})\b','dmy'),
        (rf'\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b','mdy'),
    ]
    for pattern,kind in patterns:
        m=re.search(pattern,low,re.I)
        if m:
            try:
                if kind=='dmy': d,mon,y=int(m.group(1)),month_map[m.group(2).lower()],int(m.group(3))
                else: mon,d,y=month_map[m.group(1).lower()],int(m.group(2)),int(m.group(3))
                day=datetime(y,mon,d); break
            except ValueError: hints.append('The written calendar date is invalid. Please correct it.'); break
    if day is None:
        m=re.search(r'(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)',original)
        if m:
            try: day=datetime(int(m.group(1)),int(m.group(2)),int(m.group(3)))
            except ValueError: hints.append('The written calendar date is invalid. Please correct it.')
    if day is None:
        m=re.search(r'(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](20\d{2})(?!\d)',original)
        if m:
            try: day=datetime(int(m.group(3)),int(m.group(2)),int(m.group(1)))
            except ValueError: hints.append('The written calendar date is invalid. Please correct it.')
    if day is None:
        if re.search(r'(?<![\w])(?:today|aaj|आज)(?![\w])',low): day=now.replace(hour=0,minute=0,second=0,microsecond=0)
        elif re.search(r'\btomorrow\b|(?<![\w])(?:kal|कल)(?![\w])',low):
            day=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            if not re.search(r'\btomorrow\b',low): hints.append("'Kal' is interpreted as tomorrow for this future-oriented request. Please verify.")
        elif re.search(r'\bday after tomorrow\b|(?<![\w])(?:parso|परसों)(?![\w])',low):
            day=(now+timedelta(days=2)).replace(hour=0,minute=0,second=0,microsecond=0)
            if 'parso' in low or 'परसों' in low: hints.append("'Parso' can be ambiguous; verify the suggested date.")
    # Explicit clock time: 11:30 AM, 18:00, 10 baje / 10 बजे.
    tm=re.search(r'(?<!\d)(\d{1,2})\s*[:.]\s*(\d{2})\s*(am|pm)?\b|(?<!\d)(\d{1,2})\s*(am|pm|baje|बजे)\b',low,re.I)
    if tm:
        if tm.group(1): h=int(tm.group(1)); minute=int(tm.group(2)); mer=(tm.group(3) or '').lower()
        else: h=int(tm.group(4)); minute=0; mer=(tm.group(5) or '').lower()
        if mer=='pm' and h<12: h+=12
        if mer=='am' and h==12: h=0
        if mer in ('baje','बजे',''):
            if any(x in low for x in ['evening','shaam','शाम','raat','रात','night','dopahar','दोपहर']):
                if 1<=h<=11: h+=12
        if 0<=h<=23 and 0<=minute<=59: time_value=(h,minute)
        else: hints.append('The time looks invalid. Please enter a valid time.')
    elif any(x in low for x in ['morning','subah','सुबह']): time_value=(9,0)
    elif any(x in low for x in ['afternoon','dopahar','दोपहर']): time_value=(14,0)
    elif any(x in low for x in ['evening','shaam','शाम']): time_value=(18,0)
    elif any(x in low for x in ['night','raat','रात']): time_value=(20,0)
    if not day: hints.append('No date was found. Select the due date before saving.')
    if day and not time_value:
        time_value=(10,0); hints.append('No exact time was found; 10:00 AM is only a placeholder. Please verify it.')
    iso=day.replace(hour=time_value[0],minute=time_value[1]).strftime('%Y-%m-%dT%H:%M') if day and time_value else ''
    return iso,hints,day,time_value

def _classify_request(text):
    low=text.lower()
    if any(x in low for x in ['appointment','doctor','clinic','hospital','checkup','डॉक्टर','अस्पताल','अपॉइंटमेंट','मुलाकात']): category='Appointment'
    elif any(x in low for x in ['medicine','medication','dawai','दवाई','दवा','गोली']): category='Routine'
    elif any(x in low for x in ['report','document','documents','prepare','preparation','कागज','दस्तावेज','रिपोर्ट','फाइल']): category='Preparation'
    elif any(x in low for x in ['call','phone','contact','caregiver','inform','notify','baat','फोन','कॉल','बताएं','सूचित']): category='Follow-up'
    elif any(x in low for x in ['transport','pickup','drop','यातायात','गाड़ी','ले जाना','छोड़ना']): category='Coordination'
    else: category='Care'
    urgent=['urgent','asap','immediately','jaldi','turant','तुरंत','जल्दी','अति आवश्यक','बहुत जरूरी']
    if any(x in low for x in urgent): priority='High'
    elif any(x in low for x in ['not urgent','no rush','low priority','कम प्राथमिकता']): priority='Low'
    else: priority='Normal'
    return category,priority

def parse_request(text):
    """Explainable bilingual intent extraction with month-name dates and multi-action breakdown."""
    original=(text or '').strip(); low=original.lower(); category,priority=_classify_request(original)
    due,hints,base_day,clock=_extract_request_datetime(original)
    # Identify concise, concrete actions from common care-coordination language.
    actions=[]
    patient_match=re.search(r'\b(?:mr\.?|mrs\.?|ms\.?|grandmother|grandfather|mother|mummy|mom|father|dad)\s+([A-Z][a-z]+)\b',original,re.I)
    patient=(patient_match.group(0).strip() if patient_match else 'the patient')
    appointment_context=any(x in low for x in ['appointment','checkup','check-up','doctor','clinic','hospital'])
    before_day=base_day-timedelta(days=1) if base_day and any(x in low for x in ['one day before','day before','previous day','ek din pehle','एक दिन पहले']) else base_day
    before_due=before_day.replace(hour=10,minute=0).strftime('%Y-%m-%dT%H:%M') if before_day else ''
    if any(x in low for x in ['transport','transportation','pickup','drop','गाड़ी','यातायात']):
        actions.append(('Arrange transportation for '+(patient+"'s " if appointment_context else '')+'appointment','Coordination',priority,due or before_due))
    if any(x in low for x in ['blood test report','blood reports','test reports','medical reports','reports','रिपोर्ट']):
        actions.append(('Collect and organise the latest medical reports','Preparation',priority,before_due or due))
    if any(x in low for x in ['medication list','medicine list','medicines list','current medicines','prescription list','दवाइयों की सूची']):
        actions.append(('Prepare the current medication and prescription list','Preparation',priority,before_due or due))
    if any(x in low for x in ['caregiver','family caregiver','inform the family','notify the family','inform caregiver','सूचित','बताएं']):
        actions.append(('Inform the family caregiver about the appointment','Follow-up',priority,before_due or due))

    # General care-coordination intents: keep titles concise and split distinct
    # responsibilities instead of copying the entire spoken paragraph into one task.
    call_match=re.search(r'\b(?:call|phone|contact|speak to|check in with)\s+(?:my |the )?(grandmother|grandfather|grandma|grandpa|mother|mom|father|dad|family|caregiver|son|daughter|brother|sister)\b|(?:फोन|कॉल)\s*(?:करें|करना|लगाएं|लगाना)?',original,re.I)
    if call_match and not any('Inform the family caregiver' in a[0] for a in actions):
        person=(call_match.group(1).lower() if call_match.lastindex else '')
        if person:
            person_label={'grandma':'grandmother','grandpa':'grandfather','mom':'mother','dad':'father'}.get(person,person)
            call_title=f'Call {person_label} and check in'
        else:
            call_title='Make the requested care-related phone call'
        actions.append((call_title,'Follow-up',priority,due))
    if any(x in low for x in ['grocer','grocery','groceries','food supplies','ration','किराना','राशन','सब्जी','सामान']):
        actions.append(('Check grocery and essential-supply needs','Coordination',priority,due))
    if any(x in low for x in ['care notes','family notes','update the notes','update notes','नोट्स अपडेट','जानकारी अपडेट']):
        actions.append(('Update the family care notes','Care',priority,due))
    if appointment_context and not actions:
        actions.append((f'Prepare for {patient}’s health checkup appointment','Appointment',priority,due))
    # If request contains several explicit work items, add the ones identified above; otherwise one focused action.
    if not actions:
        title=original
        is_status_question=bool(re.search(r'\b(?:is|are|has|have|did|done|ready|completed|status)\b',original,re.I) or original.strip().endswith('?') or any(x in original.lower() for x in ['ho gaye','ho gaya','ready hai','हो गया','हो गए']))
        title=re.sub(r'^\s*(please\s+)?(remind me(?:\s+to\b)?|remember\s+to|mujhe yaad dilana hai ki|yaad dilana hai ki|yaad dilana)\s*','',title,flags=re.I)
        title=re.sub(r'\s+',' ',title).strip(' ,.-:;')
        title=re.sub(r'(?i)\b(?:on\s+)?\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}\b','',title)
        title=re.sub(r'(?i)\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+20\d{2}\b','',title)
        title=re.sub(r'(?i)\b(?:today|tomorrow|day after tomorrow|yesterday|aaj|kal|parso)\b','',title)
        title=re.sub(r'(आज|कल|परसों|सुबह|दोपहर|शाम|रात)','',title)
        title=re.sub(r'(?i)\b\d{1,2}(?::|\.)\d{2}\s*(?:am|pm)?\b|\b\d{1,2}\s*(?:am|pm|baje|बजे)\b','',title)
        title=re.sub(r'(?i)\b(?:at|around|on|by)\b','',title)
        title=re.sub(r'\s+',' ',title).strip(' ,.-:;')
        if is_status_question:
            title='Check status: '+title[:150]
        if len(title)<5: title='Review care request and confirm next action'
        actions=[(title[:180],category,priority,due)]
    if len(actions)>1:
        hints.append(f'CarePilot identified {len(actions)} separate work items. Review each title, owner and due time before saving.')
    if not due: hints.append('A clear date is required for the main action. Add or select a due date before saving.')
    # Deduplicate hints.
    hints=list(dict.fromkeys(hints))
    primary=actions[0]
    return {'title':primary[0], 'category':primary[1], 'priority':primary[2], 'hints':hints, 'original':original, 'due_at':primary[3], 'actions':[{'title':a[0],'category':a[1],'priority':a[2],'due_at':a[3] or due} for a in actions], 'engine':'CarePilot structured bilingual intent & scheduling (local extraction; Gnani Prisma is audio transcription only)'}

@app.route('/voice/transcribe', methods=['POST'])
@login_required
def voice_transcribe():
    """Transcribe a short clip through Gnani's documented Prisma v3 REST API."""
    global GNANI_LAST_SUCCESS
    GNANI_LAST_SUCCESS = False
    api_key = os.getenv('GNANI_API_KEY', '').strip()
    if not api_key:
        return jsonify({'ok': False, 'error': 'GNANI_API_KEY is empty. Open this project folder .env, add your Gnani API key after GNANI_API_KEY=, save, and restart CareLoop.'}), 400

    audio = request.files.get('audio_file')
    if not audio or not audio.filename:
        return jsonify({'ok': False, 'error': 'Choose an audio file first.'}), 400

    filename = Path(audio.filename).name
    ext = Path(filename).suffix.lower()
    allowed = {'.wav', '.mp3', '.ogg', '.flac', '.m4a', '.aac'}
    if ext not in allowed:
        return jsonify({'ok': False, 'error': 'Unsupported filename extension. Use WAV, MP3, OGG, FLAC, M4A or AAC.'}), 400

    content = audio.read()
    if not content:
        return jsonify({'ok': False, 'error': 'The selected audio file is empty. Record or export the audio again.'}), 400
    if len(content) > 15 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Audio file is larger than 15 MB. Export a shorter or smaller recording.'}), 400

    # Keep the REST request minimal and aligned with Gnani's published API
    # contract. Do not set Content-Type manually: requests must add the multipart boundary.
    import mimetypes
    import requests
    language_code = os.getenv('GNANI_LANGUAGE_CODE', 'indic-auto').strip() or 'indic-auto'
    model = os.getenv('GNANI_PRISMA_MODEL', 'prisma-v3.0').strip() or 'prisma-v3.0'
    output_format = os.getenv('GNANI_PRISMA_FORMAT', 'codemix').strip() or 'codemix'
    mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    request_id = str(uuid.uuid4())

    try:
        response = requests.post(
            'https://api.vachana.ai/stt/v3',
            headers={'X-API-Key-ID': api_key, 'X-API-Request-ID': request_id},
            files={'audio_file': (filename, io.BytesIO(content), mime_type)},
            data={'model': model, 'language_code': language_code, 'format': output_format},
            timeout=(10, 60),
        )
        try:
            body = response.json()
        except ValueError:
            body = {'raw_response': (response.text or '')[:1000]}

        if not response.ok:
            # Gnani documents STT errors as {"error": {"type": ..., "message": ...}}
            # while validation/auth failures may use {"detail": ...}.
            detail = ''
            error_obj = body.get('error') if isinstance(body, dict) else None
            if isinstance(error_obj, dict):
                code = str(error_obj.get('type') or '')
                detail = str(error_obj.get('message') or '')
            else:
                code = ''
                detail_obj = body.get('detail') if isinstance(body, dict) else None
                if isinstance(detail_obj, dict):
                    code = str(detail_obj.get('error_code') or '')
                    detail = str(detail_obj.get('message') or detail_obj)
                elif detail_obj:
                    detail = str(detail_obj)
                elif isinstance(body, dict):
                    detail = str(body.get('message') or body.get('raw_response') or body)
            status_code = response.status_code
            guidance = {
                'UNSUPPORTED_AUDIO_FORMAT': 'Gnani cannot recognise this audio file extension or encoding. Export it as a real MP3 or WAV file.',
                'AUDIO_CONVERSION_ERROR': 'Gnani could not decode this recording. Re-export it as MP3 or WAV and retry.',
                'MAX_AUDIO_DURATION_EXCEEDED': 'This Prisma v3 recording is longer than 30 seconds. Use a clip of 30 seconds or less.',
                'EMPTY_AUDIO': 'The uploaded file contains no audio data. Record it again.',
                'INVALID_LANGUAGE_CODE': 'The language setting is invalid. Keep GNANI_LANGUAGE_CODE=indic-auto.',
                'INVALID_FORMAT': 'The output format is invalid. Keep GNANI_PRISMA_FORMAT=codemix.',
                'INVALID_MODEL': 'The model setting is invalid. Keep GNANI_PRISMA_MODEL=prisma-v3.0.',
                'INVALID_API_KEY': 'Gnani rejected this API key. Verify the key in the Gnani dashboard.',
                'MISSING_API_KEY': 'Gnani did not receive an API key. Check the project .env file.',
                'SCOPE_NOT_ALLOWED': 'This API key does not have STT permission enabled in Gnani.',
                'RATE_LIMITED': 'Gnani is processing another request. Wait a few seconds and retry.',
            }.get(code, '')
            if status_code in (400, 422):
                message = 'Gnani rejected the audio request.'
            elif status_code == 401:
                message = 'Gnani authentication failed.'
            elif status_code == 402:
                message = 'Gnani account credits or billing need attention.'
            elif status_code == 403:
                message = 'Gnani denied STT access for this API key.'
            elif status_code == 429:
                message = 'Gnani rate limit reached. Wait and retry.'
            elif status_code == 504:
                message = 'Gnani Prisma timed out. Try a shorter, clearer recording.'
            else:
                message = f'Gnani API returned HTTP {status_code}.'
            if guidance:
                message += ' ' + guidance
            if detail:
                message += f' Provider detail: {detail}'
            app.logger.warning('Gnani STT request %s failed (%s %s): %s', request_id, status_code, code, detail[:500])
            return jsonify({'ok': False, 'error': message, 'http_status': status_code, 'provider_code': code, 'request_id': request_id}), 502

        transcript = (body.get('transcript') or '').strip() if isinstance(body, dict) else ''
        if not transcript:
            return jsonify({'ok': False, 'error': 'Gnani received the recording but returned no speech text. Record a clearer clip with audible speech.', 'request_id': request_id}), 502

        GNANI_LAST_SUCCESS = True
        return jsonify({
            'ok': True,
            'transcript': transcript,
            'provider': 'Gnani Prisma',
            'model': body.get('model', model),
            'language': body.get('detected_language_code', ''),
            'request_id': body.get('request_id', request_id),
        })
    except requests.Timeout:
        return jsonify({'ok': False, 'error': 'Gnani request timed out. Try a shorter recording and check your internet connection.'}), 504
    except requests.RequestException as exc:
        app.logger.exception('Gnani Prisma network request failed')
        return jsonify({'ok': False, 'error': f'Could not reach Gnani. Check internet connectivity and retry. Technical detail: {str(exc)[:300]}'}), 502
@app.route('/login', methods=['GET','POST'])
def login():
    if session.get('user_id'): return redirect(url_for('dashboard'))
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        c=account_db(); user=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone(); c.close()
        if user and __import__('werkzeug.security',fromlist=['check_password_hash']).check_password_hash(user['password_hash'],password):
            session.clear(); session['user_id']=user['id']; session['user_name']=user['name']; session['user_email']=user['email']; session.permanent=True
            dest=request.args.get('next',''); return redirect(dest if dest.startswith('/') and not dest.startswith('//') else url_for('dashboard'))
        flash('Email or password is incorrect. Please try again.','error')
    return render_template('auth.html',mode='login')

@app.route('/register', methods=['GET','POST'])
def register():
    if session.get('user_id'): return redirect(url_for('dashboard'))
    if request.method=='POST':
        name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); password=request.form.get('password',''); confirm=request.form.get('confirm','')
        if len(name)<2 or '@' not in email: flash('Enter your name and a valid email address.','error')
        elif len(password)<8: flash('Choose a password with at least 8 characters.','error')
        elif password!=confirm: flash('The passwords do not match.','error')
        else:
            from werkzeug.security import generate_password_hash
            c=account_db()
            try:
                c.execute('INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)',(name,email,generate_password_hash(password),stamp())); c.commit()
                user=c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone(); session.clear(); session['user_id']=user['id']; session['user_name']=name; session['user_email']=email; session.permanent=True
                flash('Your CareLoop account is ready. Welcome in.','success'); return redirect(url_for('dashboard'))
            except sqlite3.IntegrityError: flash('An account with this email already exists. Sign in instead.','error')
            finally: c.close()
    return render_template('auth.html',mode='register')

@app.route('/logout',methods=['POST'])
def logout():
    session.clear(); flash('You have been signed out safely.','info'); return redirect(url_for('login'))

@app.route('/')
def home():
    # Public landing page remains accessible even while signed in; dashboard navigation stays separate.
    return render_template('landing.html', signed_in=bool(session.get('user_id')))

@app.route('/dashboard')
@login_required
def dashboard():
    run_automation_cycle()
    ts=all_tasks(); pending=[t for t in ts if t['status']=='Open']; done=[t for t in ts if t['status']=='Done']; now=datetime.now().isoformat(timespec='minutes'); overdue=[t for t in pending if t['due_at']<now]; recent=connect().execute('SELECT * FROM audit ORDER BY id DESC LIMIT 6').fetchall(); findings=gaps(); return render_template('dashboard.html',tasks=ts,pending=pending,done=done,overdue=overdue,findings=findings,recent=recent,now=now)
def rank_workspace_context(query, db, limit=5):
    """Transparent lexical relevance ranking over saved workspace records; not an AI inference."""
    stop={'the','and','for','with','from','that','this','need','please','have','has','are','was','were','to','of','a','an','in','on','at','by','is','it','me','my','we','our','hai','ka','ki','ke','ko','karna','hai'}
    tokens={x for x in re.findall(r'[a-z0-9]+',query.lower()) if len(x)>2 and x not in stop}
    if not tokens: return []
    candidates=[]
    sources=[('Action',db.execute("SELECT id,title,category AS detail FROM tasks WHERE status IN ('Open','Blocked') ORDER BY id DESC LIMIT 40").fetchall()),('Note',db.execute('SELECT id,title,tag AS detail FROM notes ORDER BY id DESC LIMIT 40').fetchall()),('Journey',db.execute("SELECT id,title,journey_type AS detail FROM journeys WHERE status IN ('Active','Paused') ORDER BY id DESC LIMIT 30").fetchall())]
    for kind, rows in sources:
        for row in rows:
            words={x for x in re.findall(r'[a-z0-9]+',f"{row['title']} {row['detail'] or ''}".lower()) if len(x)>2 and x not in stop}
            overlap=tokens & words
            if overlap:
                score=len(overlap)/max(1,len(tokens))
                candidates.append({'kind':kind,'title':row['title'],'reason':f"Matched: {', '.join(sorted(overlap)[:4])}",'score':score,'id':row['id']})
    return sorted(candidates,key=lambda x:(-x['score'],x['kind']))[:limit]

@app.route('/voice',methods=['GET','POST'])
def voice():
    draft=None
    if request.method=='POST':
        text=request.form.get('utterance','').strip()
        if not text: flash('Enter or dictate a request first.','error')
        else: draft=parse_request(text); flash('Request analyzed. Review the suggested action and confirm missing details.','success')
    db=connect()
    context_tasks=db.execute("SELECT id,title,category,owner,status,due_at FROM tasks WHERE status IN ('Open','Blocked') ORDER BY due_at LIMIT 12").fetchall()
    context_journeys=db.execute("SELECT id,title,status,target_date FROM journeys WHERE status IN ('Active','Paused') ORDER BY CASE status WHEN 'Active' THEN 0 ELSE 1 END,id DESC").fetchall()
    context_notes=db.execute('SELECT id,title,tag,created_at FROM notes ORDER BY id DESC LIMIT 6').fetchall()
    duplicate_candidates=[]; matched_context=[]
    if draft:
        matched_context=rank_workspace_context(draft['original'],db)
        duplicate_candidates=[{'id':m['id'],'title':m['title'],'reason':m['reason']} for m in matched_context if m['kind']=='Action' and m['score']>=0.45][:3]
    return render_template('voice.html',draft=draft,now=datetime.now().strftime('%Y-%m-%dT%H:%M'),context_tasks=context_tasks,context_journeys=context_journeys,context_notes=context_notes,duplicate_candidates=duplicate_candidates,matched_context=matched_context,gnani_ready=bool(os.getenv('GNANI_API_KEY')))
@app.route('/voice/save',methods=['POST'])
def voice_save():
    owner=request.form.get('owner','Me').strip() or 'Me'; source=request.form.get('source','Voice draft')
    db=connect(); journey_raw=request.form.get('journey_id','').strip(); journey_id=None
    if journey_raw:
        if not journey_raw.isdigit() or not db.execute("SELECT id FROM journeys WHERE id=? AND status IN ('Active','Paused')",(int(journey_raw),)).fetchone():
            flash('Choose a valid active journey or leave the journey link empty.','error'); return redirect(url_for('voice'))
        journey_id=int(journey_raw)
    count=request.form.get('action_count','').strip()
    rows=[]
    if count.isdigit() and 1<=int(count)<=12:
        for i in range(int(count)):
            title=request.form.get(f'action_title_{i}','').strip()
            due=request.form.get(f'action_due_{i}','').strip()
            category=request.form.get(f'action_category_{i}','Care')
            priority=request.form.get(f'action_priority_{i}','Normal')
            if not title: continue
            try: parsed_due=datetime.fromisoformat(due)
            except (ValueError,TypeError): flash(f'Choose a valid due date and time for action {i+1}.','error'); return redirect(url_for('voice'))
            if parsed_due < datetime.now(): flash(f'Action {i+1} is scheduled in the past. Please update it.','error'); return redirect(url_for('voice'))
            if category not in {'Care','Appointment','Preparation','Coordination','Routine','Follow-up'}: category='Care'
            if priority not in {'Low','Normal','High'}: priority='Normal'
            rows.append((title[:180],due,category,priority))
    else:
        title=request.form.get('title','').strip(); due=request.form.get('due_at','').strip(); category=request.form.get('category','Care'); priority=request.form.get('priority','Normal')
        try: parsed_due=datetime.fromisoformat(due)
        except (ValueError,TypeError): flash('Choose a valid due date and time before saving.','error'); return redirect(url_for('voice'))
        if parsed_due < datetime.now(): flash('Choose a future review time, or update the request before saving.','error'); return redirect(url_for('voice'))
        if not title or len(title)>180: flash('Use an action title between 1 and 180 characters.','error'); return redirect(url_for('voice'))
        if category not in {'Care','Appointment','Preparation','Coordination','Routine','Follow-up'}: category='Care'
        if priority not in {'Low','Normal','High'}: priority='Normal'
        rows=[(title,due,category,priority)]
    if not rows: flash('No valid actions were selected. Please analyse the request again.','error'); return redirect(url_for('voice'))
    created=[]
    for title,due,category,priority in rows:
        cur=db.execute('INSERT INTO tasks(title,category,due_at,owner,priority,source,created_at,journey_id) VALUES(?,?,?,?,?,?,?,?)',(title,category,due,owner[:100],priority,source,stamp(),journey_id))
        task_id=cur.lastrowid; created.append(task_id)
        record_task_event(task_id,'Created',f'Created from {source}. Owner: {owner[:100]}; due: {due}; priority: {priority}.')
        db.execute('INSERT INTO task_updates(task_id,update_type,body,created_at) VALUES(?,?,?,?)',(task_id,'CarePilot source',request.form.get('original_request','Reviewed request draft')[:1000],stamp()))
        if journey_id: db.execute('INSERT INTO journey_events(journey_id,event_type,detail,created_at) VALUES(?,?,?,?)',(journey_id,'CarePilot action linked',f'New action created: {title[:160]}',stamp()))
    db.commit(); audit('Confirmed CarePilot actions created',f'{len(created)} action(s): '+ '; '.join(r[0] for r in rows))
    flash(f'{len(created)} reviewed action(s) added to your care plan'+(' and linked to the selected journey.' if journey_id else '.'),'success')
    return redirect(url_for('tasks'))
@app.route('/tasks')
def tasks():
    q=request.args.get('q','').strip()
    status=request.args.get('status','All')
    priority=request.args.get('priority','All')
    category=request.args.get('category','All')
    rows=all_tasks()
    if q:
        needle=q.casefold()
        rows=[t for t in rows if needle in t['title'].casefold() or needle in t['owner'].casefold() or needle in t['category'].casefold()]
    if status in ('Open','Done','Blocked'):
        rows=[t for t in rows if t['status']==status]
    if priority in ('Low','Normal','High'):
        rows=[t for t in rows if t['priority']==priority]
    if category!='All': rows=[t for t in rows if t['category']==category]
    db=connect()
    activity={t['id']:db.execute('SELECT event_type,detail,created_at FROM task_activity WHERE task_id=? ORDER BY id DESC LIMIT 8',(t['id'],)).fetchall() for t in rows}
    steps={t['id']:db.execute('SELECT * FROM action_steps WHERE task_id=? ORDER BY id',(t['id'],)).fetchall() for t in rows}
    dependencies={t['id']:db.execute('SELECT d.id,d.depends_on_id,p.title,p.status FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_id WHERE d.task_id=? ORDER BY d.id',(t['id'],)).fetchall() for t in rows}
    updates={t['id']:db.execute('SELECT * FROM task_updates WHERE task_id=? ORDER BY id DESC LIMIT 5',(t['id'],)).fetchall() for t in rows}
    candidates=[t for t in all_tasks() if t['status']!='Done']
    # Explainable operational attention labels derived from each action's saved fields.
    attention={}
    now_dt=datetime.now()
    for t in rows:
        reasons=[]
        if t['status']=='Done':
            attention[t['id']]={'level':'Completed','reasons':[]}
            continue
        try: due_dt=datetime.fromisoformat(t['due_at'])
        except (ValueError,TypeError): due_dt=None
        if t['status']=='Blocked': reasons.append('Blocked — review the recorded blocker')
        if due_dt and due_dt < now_dt and t['status']!='Done': reasons.append('Past due')
        elif due_dt and due_dt <= now_dt+timedelta(hours=24) and t['status']!='Done': reasons.append('Due within 24 hours')
        if t['priority']=='High' and not t['acknowledged']: reasons.append('High priority without acknowledgement')
        if t['owner'].strip().lower() in ('','unassigned','none','tbd'): reasons.append('No accountable owner')
        if dependencies[t['id']] and any(d['status']!='Done' for d in dependencies[t['id']]): reasons.append('Prerequisite still open')
        if steps[t['id']] and any(not st['is_done'] for st in steps[t['id']]): reasons.append('Checklist incomplete')
        attention[t['id']]={'level':'Review' if reasons else 'On track','reasons':reasons}
    return render_template('tasks.html',tasks=rows,activity=activity,steps=steps,dependencies=dependencies,updates=updates,candidates=candidates,attention=attention,now=datetime.now().strftime('%Y-%m-%dT%H:%M'),q=q,status=status,priority=priority,category=category)
@app.route('/tasks/add',methods=['POST'])
def add_task():
    title=request.form.get('title','').strip(); due=request.form.get('due_at','').strip(); owner=request.form.get('owner','Me').strip() or 'Me'; category=request.form.get('category','Care'); priority=request.form.get('priority','Normal'); recurrence=request.form.get('recurrence','None')
    try: datetime.fromisoformat(due)
    except ValueError: flash('Choose a valid due date and time.','error'); return redirect(url_for('tasks'))
    if recurrence not in {'None','Daily','Weekly','Monthly'}: flash('Choose a valid recurrence.','error'); return redirect(url_for('tasks'))
    if not title: flash('Enter a task title.','error'); return redirect(url_for('tasks'))
    cur=connect().execute('INSERT INTO tasks(title,category,due_at,owner,priority,created_at,recurrence) VALUES(?,?,?,?,?,?,?)',(title[:180],category,due,owner,priority,stamp(),recurrence)); connect().commit(); record_task_event(cur.lastrowid,'Created',f'Manually created. Owner: {owner}; due: {due}; priority: {priority}.'); audit('Task created',title); flash('Task added.','success'); return redirect(url_for('tasks'))
def next_recurrence_due(due_value, frequency):
    due=datetime.fromisoformat(due_value)
    if frequency=='Daily': return due+timedelta(days=1)
    if frequency=='Weekly': return due+timedelta(days=7)
    if frequency=='Monthly':
        # Calendar-aware month increment; clamp day to the destination month.
        import calendar
        year=due.year+(1 if due.month==12 else 0); month=1 if due.month==12 else due.month+1
        day=min(due.day,calendar.monthrange(year,month)[1])
        return due.replace(year=year,month=month,day=day)
    return None

@app.route('/tasks/<int:task_id>/done',methods=['POST'])
def task_done(task_id):
    db=connect(); t=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not t: flash('Action not found.','error')
    elif t['status']=='Blocked': flash('This action is blocked. Review its blocker before completing it.','error')
    elif t['status']=='Done':
        db.execute("UPDATE tasks SET status='Open',completed_at=NULL,completion_note='',last_touched_at=? WHERE id=?",(stamp(),task_id)); db.commit(); record_task_event(task_id,'Reopened','Action returned to Open for further work.'); flash('Action reopened.','success')
    else:
        pending_steps=db.execute('SELECT COUNT(*) FROM action_steps WHERE task_id=? AND is_done=0',(task_id,)).fetchone()[0]
        pending_dependencies=db.execute("SELECT COUNT(*) FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_id WHERE d.task_id=? AND p.status!='Done'",(task_id,)).fetchone()[0]
        if pending_steps or pending_dependencies:
            flash(f'Complete blocked: {pending_steps} checklist step(s) and {pending_dependencies} prerequisite action(s) remain.','error'); return redirect(request.referrer or url_for('tasks'))
        note=request.form.get('completion_note','').strip()
        if not note:
            flash('Add a short outcome note before completing this action.','error'); return redirect(request.referrer or url_for('tasks'))
        db.execute("UPDATE tasks SET status='Done',completed_at=?,completion_note=?,last_touched_at=? WHERE id=?",(stamp(),note[:1000],stamp(),task_id)); db.commit(); record_task_event(task_id,'Completed','Outcome: '+note)
        if t['recurrence'] in {'Daily','Weekly','Monthly'}:
            due=next_recurrence_due(t['due_at'],t['recurrence'])
            # Idempotency: only one future open occurrence for a recurring series.
            exists=db.execute("SELECT id FROM tasks WHERE recurrence_parent_id=? AND status IN ('Open','Blocked')",(task_id,)).fetchone()
            if due and not exists:
                cur=db.execute('INSERT INTO tasks(title,category,due_at,owner,priority,source,created_at,recurrence,recurrence_parent_id) VALUES(?,?,?,?,?,?,?,?,?)',(t['title'],t['category'],due.isoformat(timespec='minutes'),t['owner'],t['priority'],'Recurring follow-up',stamp(),t['recurrence'],task_id)); db.commit(); record_task_event(cur.lastrowid,'Generated','Next '+t['recurrence'].lower()+' occurrence generated after prior completion.')
                flash('Action completed. The next recurring occurrence has been scheduled for '+due.strftime('%d %b %Y %H:%M')+'.','success')
            else: flash('Action completed. A future occurrence is already open, so no duplicate was created.','success')
        else: flash('Action marked complete.','success')
    return redirect(request.referrer or url_for('tasks'))

@app.route('/tasks/<int:task_id>/ack',methods=['POST'])
def task_ack(task_id):
    t=connect().execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if t:
        value=0 if t['acknowledged'] else 1
        connect().execute('UPDATE tasks SET acknowledged=?,acknowledged_at=?,last_touched_at=? WHERE id=?',(value,stamp() if value else None,stamp(),task_id)); connect().commit(); record_task_event(task_id,'Acknowledged' if value else 'Acknowledgement cleared','Owner acknowledgement recorded.' if value else 'Acknowledgement cleared for re-confirmation.')
    audit('Acknowledgement updated',f'Task #{task_id}'); return redirect(request.referrer or url_for('tasks'))

@app.route('/tasks/<int:task_id>/update', methods=['POST'])
def update_task(task_id):
    """Edit the action record without losing its audit trail."""
    db=connect(); task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task:
        flash('That action no longer exists.','error'); return redirect(url_for('tasks'))
    title=request.form.get('title','').strip(); due=request.form.get('due_at','').strip()
    owner=request.form.get('owner','').strip() or 'Unassigned'
    category=request.form.get('category','Care').strip(); priority=request.form.get('priority','Normal').strip(); recurrence=request.form.get('recurrence',task['recurrence'] or 'None').strip()
    allowed_categories={'Care','Appointment','Preparation','Coordination','Routine','Follow-up'}
    if not title or len(title)>180:
        flash('Use an action title between 1 and 180 characters.','error'); return redirect(url_for('tasks'))
    try: datetime.fromisoformat(due)
    except (ValueError, TypeError):
        flash('Choose a valid due date and time.','error'); return redirect(url_for('tasks'))
    if category not in allowed_categories or priority not in {'Low','Normal','High'} or recurrence not in {'None','Daily','Weekly','Monthly'}:
        flash('Choose a valid category and priority.','error'); return redirect(url_for('tasks'))
    db.execute('UPDATE tasks SET title=?,due_at=?,owner=?,category=?,priority=?,recurrence=?,last_touched_at=? WHERE id=?',(title,due,owner[:100],category,priority,recurrence,stamp(),task_id)); db.commit()
    record_task_event(task_id,'Details updated',f'Title: {title}; owner: {owner}; due: {due}; category: {category}; priority: {priority}.')
    audit('Action details updated',f"{task['title']} → {title}"); flash('Action details updated.','success')
    return redirect(url_for('tasks'))

@app.route('/tasks/<int:task_id>/block', methods=['POST'])
def toggle_block(task_id):
    db=connect(); task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if task:
        new_status='Open' if task['status']=='Blocked' else 'Blocked'
        reason=request.form.get('blocked_reason','').strip() if new_status=='Blocked' else ''
        if new_status=='Blocked' and not reason: flash('Add a short blocker reason before pausing this action.','error'); return redirect(request.referrer or url_for('tasks'))
        db.execute('UPDATE tasks SET status=?,blocked_reason=?,last_touched_at=? WHERE id=?',(new_status,reason[:1000],stamp(),task_id)); db.commit()
        record_task_event(task_id,'Blocked' if new_status=='Blocked' else 'Unblocked',reason or 'Blocker cleared; action returned to Open.')
        audit('Action status changed',f"{task['title']} → {new_status}")
        flash('Action is blocked. Add context in your care notes and revisit it when ready.' if new_status=='Blocked' else 'Action returned to the open queue.','success')
    else: flash('Action not found.','error')
    return redirect(url_for('tasks'))

@app.route('/tasks/<int:task_id>/snooze', methods=['POST'])
def snooze_task(task_id):
    db=connect(); task=db.execute("SELECT * FROM tasks WHERE id=? AND status IN ('Open','Blocked')",(task_id,)).fetchone()
    if task:
        try: new_due=datetime.fromisoformat(task['due_at'])+timedelta(days=1)
        except ValueError: new_due=datetime.now()+timedelta(days=1)
        db.execute('UPDATE tasks SET due_at=?,last_touched_at=? WHERE id=?',(new_due.isoformat(timespec='minutes'),stamp(),task_id)); db.commit()
        record_task_event(task_id,'Rescheduled',f"Due time moved from {task['due_at']} to {new_due.isoformat(timespec='minutes')} (+24h).")
        audit('Action rescheduled by one day',task['title']); flash('Due time moved forward by 24 hours. Please verify the new timing.','success')
    else: flash('Only an open or blocked action can be rescheduled.','error')
    return redirect(url_for('tasks'))

@app.route('/tasks/<int:task_id>/delete',methods=['POST'])
def delete_task(task_id):
    db=connect()
    task=db.execute('SELECT title FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task:
        flash('That action could not be found. It may already have been removed.','error')
    else:
        db.execute('DELETE FROM tasks WHERE id=?',(task_id,)); db.commit()
        audit('Action removed',task['title']); flash('Action removed from the care plan.','success')
    return redirect(url_for('tasks'))

@app.route('/tasks/<int:task_id>/activity')
def task_activity(task_id):
    task=connect().execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task: return jsonify({'error':'Action not found'}),404
    events=connect().execute('SELECT event_type,detail,created_at FROM task_activity WHERE task_id=? ORDER BY id DESC LIMIT 50',(task_id,)).fetchall()
    return jsonify({'task':task['title'],'events':[dict(e) for e in events]})

@app.route('/tasks/<int:task_id>/steps', methods=['POST'])
def add_action_step(task_id):
    title=request.form.get('step_title','').strip(); db=connect()
    if not db.execute('SELECT id FROM tasks WHERE id=?',(task_id,)).fetchone(): flash('Action not found.','error')
    elif not title or len(title)>180: flash('Enter a checklist step (maximum 180 characters).','error')
    else:
        db.execute('INSERT INTO action_steps(task_id,title,created_at) VALUES(?,?,?)',(task_id,title,stamp())); db.commit(); record_task_event(task_id,'Checklist step added',title); flash('Checklist step added.','success')
    return redirect(request.referrer or url_for('tasks'))

@app.route('/steps/<int:step_id>/toggle', methods=['POST'])
def toggle_action_step(step_id):
    db=connect(); row=db.execute('SELECT * FROM action_steps WHERE id=?',(step_id,)).fetchone()
    if row:
        done=0 if row['is_done'] else 1; db.execute('UPDATE action_steps SET is_done=?,completed_at=? WHERE id=?',(done,stamp() if done else None,step_id)); db.commit(); record_task_event(row['task_id'],'Checklist progress changed',row['title']); flash('Checklist updated.','success')
    return redirect(request.referrer or url_for('tasks'))

@app.route('/tasks/<int:task_id>/dependency', methods=['POST'])
def add_dependency(task_id):
    db=connect()
    try: parent=int(request.form.get('depends_on_id','0'))
    except ValueError: parent=0
    if task_id==parent or not db.execute('SELECT id FROM tasks WHERE id=?',(task_id,)).fetchone() or not db.execute('SELECT id FROM tasks WHERE id=?',(parent,)).fetchone(): flash('Choose a different existing prerequisite.','error')
    elif db.execute('SELECT 1 FROM task_dependencies WHERE task_id=? AND depends_on_id=?',(task_id,parent)).fetchone(): flash('Prerequisite already linked.','info')
    else:
        cycle=db.execute('WITH RECURSIVE c(id) AS (SELECT depends_on_id FROM task_dependencies WHERE task_id=? UNION SELECT d.depends_on_id FROM task_dependencies d JOIN c ON d.task_id=c.id) SELECT 1 FROM c WHERE id=?',(parent,task_id)).fetchone()
        if cycle: flash('That link would create a circular dependency.','error')
        else: db.execute('INSERT INTO task_dependencies(task_id,depends_on_id,created_at) VALUES(?,?,?)',(task_id,parent,stamp())); db.commit(); record_task_event(task_id,'Prerequisite linked',f'Action #{parent} must be completed first.'); flash('Prerequisite linked; completion is gated until it is done.','success')
    return redirect(request.referrer or url_for('tasks'))

@app.route('/dependencies/<int:dependency_id>/remove', methods=['POST'])
def remove_dependency(dependency_id):
    db=connect(); row=db.execute('SELECT task_id FROM task_dependencies WHERE id=?',(dependency_id,)).fetchone()
    if row: db.execute('DELETE FROM task_dependencies WHERE id=?',(dependency_id,)); db.commit(); record_task_event(row['task_id'],'Prerequisite removed',f'Link #{dependency_id} removed.'); flash('Prerequisite removed.','success')
    return redirect(request.referrer or url_for('tasks'))

@app.route('/tasks/<int:task_id>/update-note', methods=['POST'])
def add_task_update(task_id):
    db=connect(); body=request.form.get('update_body','').strip(); kind=request.form.get('update_type','Progress')
    if not db.execute('SELECT id FROM tasks WHERE id=?',(task_id,)).fetchone(): flash('Action not found.','error')
    elif not body: flash('Write an update first.','error')
    elif kind not in {'Progress','Handoff','Decision','Context'}: flash('Invalid update type.','error')
    else: db.execute('INSERT INTO task_updates(task_id,update_type,body,created_at) VALUES(?,?,?,?)',(task_id,kind,body[:1500],stamp())); db.commit(); record_task_event(task_id,'Progress update',f'{kind}: {body[:500]}'); flash('Update saved to the action record.','success')
    return redirect(request.referrer or url_for('tasks'))

@app.route('/tasks/export.csv')
def export_tasks():
    output=io.StringIO(newline='')
    writer=csv.writer(output)
    writer.writerow(['ID','Action','Category','Due at','Owner','Priority','Status','Acknowledged','Source','Created at','Acknowledged at','Completed at','Completion note','Blocked reason'])
    for t in all_tasks():
        writer.writerow([t['id'],t['title'],t['category'],t['due_at'],t['owner'],t['priority'],t['status'],'Yes' if t['acknowledged'] else 'No',t['source'],t['created_at'],t['acknowledged_at'],t['completed_at'],t['completion_note'],t['blocked_reason']])
    from flask import Response
    return Response(output.getvalue(), mimetype='text/csv; charset=utf-8', headers={'Content-Disposition':'attachment; filename=careloop-care-plan.csv'})
@app.route('/journeys', methods=['GET','POST'])
def journeys():
    db=connect()
    if request.method=='POST':
        title=request.form.get('title','').strip(); kind=request.form.get('journey_type','Custom coordination'); goal=request.form.get('goal','').strip(); start=request.form.get('start_date','').strip(); target=request.form.get('target_date','').strip()
        if not title or len(title)>120 or not start:
            flash('Add a journey name and a valid start date.','error'); return redirect(url_for('journeys'))
        try:
            from datetime import date
            date.fromisoformat(start)
            if target: date.fromisoformat(target)
        except ValueError:
            flash('Use valid journey dates.','error'); return redirect(url_for('journeys'))
        cur=db.execute('INSERT INTO journeys(title,journey_type,goal,start_date,target_date,created_at) VALUES(?,?,?,?,?,?)',(title,kind,goal[:800],start,target or None,stamp())); db.commit()
        jid=cur.lastrowid; db.execute('INSERT INTO journey_events(journey_id,event_type,detail,created_at) VALUES(?,?,?,?)',(jid,'Journey created',f'{kind}: {goal}',stamp())); db.commit()
        audit('Care journey created',title); flash('Care journey created. Add milestones and connect actions to make the plan actionable.','success'); return redirect(url_for('journey_detail',journey_id=jid))
    rows=db.execute("SELECT j.*,COUNT(t.id) AS total_tasks,SUM(CASE WHEN t.status='Done' THEN 1 ELSE 0 END) AS done_tasks,SUM(CASE WHEN t.status='Blocked' THEN 1 ELSE 0 END) AS blocked_tasks,SUM(CASE WHEN t.status!='Done' AND t.due_at<? THEN 1 ELSE 0 END) AS overdue_tasks FROM journeys j LEFT JOIN tasks t ON t.journey_id=j.id GROUP BY j.id ORDER BY CASE j.status WHEN 'Active' THEN 0 WHEN 'Paused' THEN 1 ELSE 2 END,j.id DESC",(datetime.now().isoformat(timespec='minutes'),)).fetchall()
    return render_template('journeys.html',journeys=rows,today=datetime.now().date().isoformat())

@app.route('/journeys/<int:journey_id>')
def journey_detail(journey_id):
    db=connect(); journey=db.execute('SELECT * FROM journeys WHERE id=?',(journey_id,)).fetchone()
    if not journey: flash('Journey not found.','error'); return redirect(url_for('journeys'))
    tasks=db.execute('SELECT * FROM tasks WHERE journey_id=? ORDER BY CASE status WHEN \'Blocked\' THEN 0 WHEN \'Open\' THEN 1 ELSE 2 END,due_at',(journey_id,)).fetchall()
    events=db.execute('SELECT * FROM journey_events WHERE journey_id=? ORDER BY id DESC LIMIT 30',(journey_id,)).fetchall()
    total=len(tasks); done=sum(1 for t in tasks if t['status']=='Done'); blocked=sum(1 for t in tasks if t['status']=='Blocked'); pct=round(done*100/total) if total else 0
    return render_template('journey_detail.html',journey=journey,tasks=tasks,events=events,total=total,done=done,blocked=blocked,pct=pct,today=datetime.now().strftime('%Y-%m-%dT%H:%M'))

@app.route('/journeys/<int:journey_id>/milestones',methods=['POST'])
def journey_milestone(journey_id):
    db=connect(); journey=db.execute('SELECT * FROM journeys WHERE id=?',(journey_id,)).fetchone()
    if not journey: flash('Journey not found.','error'); return redirect(url_for('journeys'))
    title=request.form.get('title','').strip(); due=request.form.get('due_at','').strip(); owner=request.form.get('owner','').strip() or 'Unassigned'; priority=request.form.get('priority','Normal')
    try: datetime.fromisoformat(due)
    except ValueError: flash('Choose a valid milestone due date.','error'); return redirect(url_for('journey_detail',journey_id=journey_id))
    if not title: flash('Milestone title is required.','error'); return redirect(url_for('journey_detail',journey_id=journey_id))
    cur=db.execute('INSERT INTO tasks(title,category,due_at,owner,priority,status,source,created_at,journey_id) VALUES(?,?,?,?,?,?,?,?,?)',(title[:180],'Coordination',due,owner[:100],priority,'Open','Journey milestone',stamp(),journey_id)); db.commit(); record_task_event(cur.lastrowid,'Journey milestone created',f'Linked to journey: {journey["title"]}')
    db.execute('INSERT INTO journey_events(journey_id,event_type,detail,created_at) VALUES(?,?,?,?)',(journey_id,'Milestone added',title,stamp())); db.commit(); flash('Milestone added to this journey.','success'); return redirect(url_for('journey_detail',journey_id=journey_id))

@app.route('/journeys/<int:journey_id>/status',methods=['POST'])
def journey_status(journey_id):
    db=connect(); j=db.execute('SELECT * FROM journeys WHERE id=?',(journey_id,)).fetchone()
    if not j: flash('Journey not found.','error'); return redirect(url_for('journeys'))
    action=request.form.get('action')
    if action=='pause' and j['status']=='Active': new='Paused'
    elif action=='resume' and j['status']=='Paused': new='Active'
    elif action=='complete':
        open_count=db.execute("SELECT COUNT(*) FROM tasks WHERE journey_id=? AND status!='Done'",(journey_id,)).fetchone()[0]
        if open_count: flash(f'{open_count} milestone(s) are not complete. Resolve or explicitly remove them before closing this journey.','error'); return redirect(url_for('journey_detail',journey_id=journey_id))
        new='Completed'
    else: flash('That journey transition is not available.','error'); return redirect(url_for('journey_detail',journey_id=journey_id))
    db.execute('UPDATE journeys SET status=?,completed_at=? WHERE id=?',(new,stamp() if new=='Completed' else None,journey_id)); db.execute('INSERT INTO journey_events(journey_id,event_type,detail,created_at) VALUES(?,?,?,?)',(journey_id,'Status changed',f'{j["status"]} → {new}',stamp())); db.commit(); audit('Journey status changed',f'{j["title"]} → {new}'); flash(f'Journey marked {new.lower()}.','success'); return redirect(url_for('journey_detail',journey_id=journey_id))

@app.route('/journeys/<int:journey_id>/delete',methods=['POST'])
def delete_journey(journey_id):
    db=connect(); j=db.execute('SELECT * FROM journeys WHERE id=?',(journey_id,)).fetchone()
    if not j: flash('Journey not found.','error')
    else:
        # Preserve the actions: deleting a journey only unlinks them; it never deletes care-plan records.
        db.execute('UPDATE tasks SET journey_id=NULL WHERE journey_id=?',(journey_id,)); db.execute('DELETE FROM journeys WHERE id=?',(journey_id,)); db.commit(); audit('Journey removed',j['title']); flash('Journey removed. Its actions remain safely in your care plan.','success')
    return redirect(url_for('journeys'))

@app.route('/gaps')
def gaps_page(): return redirect(url_for('sentinel'))
@app.route('/gaps/<int:task_id>/prep',methods=['POST'])
def prep(task_id):
    t=connect().execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if t:
        due=datetime.fromisoformat(t['due_at'])-timedelta(days=1)
        if due<datetime.now(): due=datetime.now()+timedelta(hours=1)
        cur=connect().execute('INSERT INTO tasks(title,category,due_at,owner,priority,source,created_at) VALUES(?,?,?,?,?,?,?)',(f"Prepare for: {t['title']}",'Preparation',due.isoformat(timespec='minutes'),'Me','Normal','CareGap suggestion',stamp())); connect().commit(); record_task_event(cur.lastrowid,'Created','Accepted preparation suggestion from CareGap Radar.'); audit('Preparation suggestion accepted',t['title']); flash('Suggested preparation task created. Review its timing.','success')
    return redirect(url_for('gaps_page'))
def record_automation(event_key, event_type, detail):
    """Persist an idempotent, human-readable automation event."""
    db=connect()
    cur=db.execute('INSERT OR IGNORE INTO automation_events(event_key,event_type,detail,created_at) VALUES(?,?,?,?)',(event_key[:240],event_type[:100],detail[:1200],stamp()))
    db.commit()
    return cur.rowcount > 0

def run_automation_cycle():
    """Run safe operational automations; never changes clinical information or silently completes care actions."""
    db=connect(); now=datetime.now(); changed=0
    # Handoff ageing: create one review item after 24 hours without acceptance.
    pending=db.execute("SELECT * FROM handoffs WHERE status IN ('Pending','Sent','Viewed')").fetchall()
    for h in pending:
        try: age=now-datetime.fromisoformat(h['created_at'])
        except (ValueError,TypeError): continue
        if age >= timedelta(hours=24):
            title=f'Handoff awaiting acknowledgement · #{h["id"]}'
            detail=f'{h["title"]} has been awaiting acknowledgement from {h["to_person"]} for over 24 hours. Contact the recipient and confirm whether they accept the transfer.'
            exists=db.execute("SELECT id FROM alerts WHERE title=? AND status='Open'",(title,)).fetchone()
            if not exists:
                db.execute('INSERT INTO alerts(task_id,title,detail,created_at) VALUES(NULL,?,?,?)',(title,detail,stamp())); changed+=1
                record_automation(f'handoff-aging-{h["id"]}', 'Handoff follow-up', f'Reminder created for pending handoff #{h["id"]}: {h["title"]}.')
    # Journey intelligence: calculate progress and emit a milestone event when a journey reaches 100%.
    journeys=db.execute("SELECT * FROM journeys WHERE status='Active'").fetchall()
    for j in journeys:
        stats=db.execute("SELECT COUNT(*) AS total,SUM(CASE WHEN status='Done' THEN 1 ELSE 0 END) AS done,SUM(CASE WHEN status='Blocked' THEN 1 ELSE 0 END) AS blocked FROM tasks WHERE journey_id=?",(j['id'],)).fetchone()
        total=stats['total'] or 0; done=stats['done'] or 0; blocked=stats['blocked'] or 0
        if total and done==total:
            event_key=f'journey-ready-{j["id"]}'
            if record_automation(event_key,'Journey ready to close',f'All {total} milestones in “{j["title"]}” are complete. Review the journey and close it when appropriate.'):
                db.execute('INSERT INTO journey_events(journey_id,event_type,detail,created_at) VALUES(?,?,?,?)',(j['id'],'All milestones complete','Automation detected that every linked action is complete. Human confirmation is still required to close the journey.',stamp())); changed+=1
        # Flag a journey target that has passed while milestones remain open.
        if j['target_date'] and j['target_date'] < now.date().isoformat() and done < total:
            event_key=f'journey-target-{j["id"]}-{j["target_date"]}'
            if record_automation(event_key,'Journey target passed',f'“{j["title"]}” passed its target date with {total-done} milestone(s) unfinished and {blocked} blocked.'):
                db.execute('INSERT INTO alerts(task_id,title,detail,created_at) VALUES(NULL,?,?,?)',(f'Journey target passed · #{j["id"]}',f'“{j["title"]}” is past its target date. Review the remaining milestones, update the target, or pause the journey.',stamp())); changed+=1
    db.commit()
    return changed

def run_sentinel_scan():
    """Create deduplicated review items from live operational evidence."""
    db=connect(); created=0; now=datetime.now(); horizon=now+timedelta(hours=24)
    run_automation_cycle()
    for t in all_tasks():
        if t['status'] not in ('Open','Blocked'): continue
        checks=[]
        try: due=datetime.fromisoformat(t['due_at'])
        except (ValueError,TypeError): due=None
        if t['status']=='Blocked': checks.append(('Blocked action',f"{t['title']} is marked blocked. Record the blocker and agree the next step."))
        if due and due < now: checks.append(('Overdue action',f"{t['title']} is past its due time and remains open. Confirm the status with its owner."))
        elif due and due <= horizon: checks.append(('Due within 24 hours',f"{t['title']} is due within 24 hours. Confirm owner and readiness."))
        if t['priority']=='High' and not t['acknowledged']: checks.append(('Acknowledgement missing',f"{t['title']} is high priority, but no acknowledgement is recorded."))
        if t['owner'].strip().lower() in ('','unassigned','none','tbd'): checks.append(('Owner not assigned',f"{t['title']} has no accountable owner. Assign a willing owner."))
        unfinished=db.execute('SELECT COUNT(*) FROM action_steps WHERE task_id=? AND is_done=0',(t['id'],)).fetchone()[0]
        if unfinished and due and due<=horizon: checks.append(('Checklist incomplete',f"{t['title']} has {unfinished} unfinished checklist step(s) and is due soon."))
        blocked_by=db.execute("SELECT COUNT(*) FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_id WHERE d.task_id=? AND p.status!='Done'",(t['id'],)).fetchone()[0]
        if blocked_by: checks.append(('Prerequisite incomplete',f"{t['title']} has {blocked_by} prerequisite action(s) not yet completed."))
        for title,detail in checks:
            exists=db.execute("SELECT id FROM alerts WHERE task_id=? AND title=? AND status='Open'",(t['id'],title)).fetchone()
            if not exists:
                db.execute('INSERT INTO alerts(task_id,title,detail,created_at) VALUES(?,?,?,?)',(t['id'],title,detail,stamp())); created+=1
                record_automation(f'task-alert-{t["id"]}-{title}', 'Sentinel alert', f'{title}: {t["title"]}. {detail}')
    db.commit()
    return created

@app.route('/sentinel',methods=['GET','POST'])
def sentinel():
    created=run_sentinel_scan()
    if request.method=='POST':
        audit('Sentinel review run',f'{created} new review item(s)'); flash(f'Live scan complete: {created} new review item(s). Existing open items were retained.','success' if created else 'info'); return redirect(url_for('sentinel'))
    db=connect()
    alerts=db.execute("SELECT a.id,a.task_id,a.title,a.detail,a.status,a.created_at,t.title AS task_title FROM alerts a LEFT JOIN tasks t ON t.id=a.task_id ORDER BY CASE a.status WHEN 'Open' THEN 0 ELSE 1 END,a.id DESC").fetchall()
    findings=gaps(); active=[t for t in all_tasks() if t['status'] in ('Open','Blocked')]
    automation_events=db.execute('SELECT * FROM automation_events ORDER BY id DESC LIMIT 12').fetchall()
    blocked=sum(1 for t in active if t['status']=='Blocked'); overdue=sum(1 for t in active if t['due_at'] and t['due_at'][:10]<datetime.now().date().isoformat())
    open_alerts=sum(1 for x in alerts if x['status']=='Open')
    return render_template('sentinel.html',alerts=alerts,findings=findings,active_count=len(active),blocked_count=blocked,overdue_count=overdue,open_alerts=open_alerts,automation_events=automation_events)
@app.route('/alerts/<int:alert_id>/resolve',methods=['POST'])
def resolve(alert_id): connect().execute("UPDATE alerts SET status='Resolved' WHERE id=?",(alert_id,)); connect().commit(); audit('Review item resolved',f'Alert #{alert_id}'); return redirect(url_for('sentinel'))
@app.route('/memory',methods=['GET','POST'])
def memory():
    db=connect()
    if request.method=='POST':
        title=request.form.get('title','').strip(); body=request.form.get('body','').strip(); tag=request.form.get('tag','Observation')
        task_raw=request.form.get('task_id','').strip(); journey_raw=request.form.get('journey_id','').strip(); task_id=None; journey_id=None
        if task_raw:
            if not task_raw.isdigit() or not db.execute('SELECT id FROM tasks WHERE id=?',(int(task_raw),)).fetchone(): flash('Choose a valid linked action.','error'); return redirect(url_for('memory'))
            task_id=int(task_raw)
        if journey_raw:
            if not journey_raw.isdigit() or not db.execute('SELECT id FROM journeys WHERE id=?',(int(journey_raw),)).fetchone(): flash('Choose a valid linked journey.','error'); return redirect(url_for('memory'))
            journey_id=int(journey_raw)
        if title and body:
            db.execute('INSERT INTO notes(title,body,tag,created_at,task_id,journey_id) VALUES(?,?,?,?,?,?)',(title[:120],body[:3000],tag,stamp(),task_id,journey_id)); db.commit(); audit('Care memory note added',title); flash('Note saved with its timestamp and linked context.','success')
        else: flash('Add both a title and note.','error')
        return redirect(url_for('memory'))
    q=request.args.get('q','').strip()
    notes=db.execute('SELECT n.*,t.title AS task_title,j.title AS journey_title FROM notes n LEFT JOIN tasks t ON t.id=n.task_id LEFT JOIN journeys j ON j.id=n.journey_id WHERE n.title LIKE ? OR n.body LIKE ? OR n.tag LIKE ? ORDER BY n.id DESC',(f'%{q}%',f'%{q}%',f'%{q}%')).fetchall()
    task_options=db.execute("SELECT id,title FROM tasks WHERE status IN ('Open','Blocked') ORDER BY due_at").fetchall(); journey_options=db.execute("SELECT id,title FROM journeys WHERE status IN ('Active','Paused') ORDER BY id DESC").fetchall()
    return render_template('memory.html',notes=notes,q=q,task_options=task_options,journey_options=journey_options)
@app.route('/memory/<int:note_id>/delete', methods=['POST'])
def delete_note(note_id):
    db=connect()
    note=db.execute('SELECT title FROM notes WHERE id=?',(note_id,)).fetchone()
    if not note:
        flash('That note could not be found. It may already have been removed.','error')
    else:
        db.execute('DELETE FROM notes WHERE id=?',(note_id,)); db.commit()
        audit('Care memory note removed',note['title'])
        flash('Note removed from Care Memory.','success')
    return redirect(url_for('memory'))

@app.route('/visit-brief')
def visit_brief():
    db=connect(); notes=db.execute('SELECT n.*,t.title AS task_title,j.title AS journey_title FROM notes n LEFT JOIN tasks t ON t.id=n.task_id LEFT JOIN journeys j ON j.id=n.journey_id ORDER BY n.id DESC LIMIT 12').fetchall()
    tasks=[t for t in all_tasks() if t['status'] in ('Open','Blocked')]
    task_steps={t['id']:db.execute('SELECT * FROM action_steps WHERE task_id=? ORDER BY id',(t['id'],)).fetchall() for t in tasks}
    task_updates={t['id']:db.execute('SELECT * FROM task_updates WHERE task_id=? ORDER BY id DESC LIMIT 3',(t['id'],)).fetchall() for t in tasks}
    recently_done=db.execute("SELECT * FROM tasks WHERE status='Done' AND completed_at>=? ORDER BY completed_at DESC LIMIT 8",((datetime.now()-timedelta(days=7)).isoformat(timespec='minutes'),)).fetchall()
    return render_template('brief.html',notes=notes,tasks=tasks,task_steps=task_steps,task_updates=task_updates,recently_done=recently_done,generated=stamp())
@app.route('/demo/reset',methods=['POST'])
def reset_demo():
    c=connect()
    existing=c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    if existing:
        flash('Your existing care plan was preserved. The example workflow only loads into an empty plan.','info')
        return redirect(url_for('dashboard'))
    now=datetime.now().replace(second=0,microsecond=0)
    sample=[('Confirm transport for clinic appointment','Appointment',(now+timedelta(days=2)).isoformat(timespec='minutes'),'Unassigned','High'),('Prepare appointment questions','Preparation',(now+timedelta(days=2)).replace(hour=9,minute=0).isoformat(timespec='minutes'),'Me','Normal'),('Collect documents for visit','Preparation',(now-timedelta(hours=3)).isoformat(timespec='minutes'),'Me','Normal'),('Call family care partner','Coordination',(now+timedelta(days=1)).isoformat(timespec='minutes'),'Me','Normal')]
    c.executemany('INSERT INTO tasks(title,category,due_at,owner,priority,source,created_at) VALUES(?,?,?,?,?,?,?)',[(a,b,d,e,f,'Example workflow',stamp()) for a,b,d,e,f in sample])
    ids=[r[0] for r in c.execute('SELECT id FROM tasks ORDER BY id DESC LIMIT 4').fetchall()][::-1]
    if len(ids)==4:
        c.execute('INSERT INTO task_dependencies(task_id,depends_on_id,created_at) VALUES(?,?,?)',(ids[0],ids[1],stamp()))
        c.executemany('INSERT INTO action_steps(task_id,title,created_at) VALUES(?,?,?)',[(ids[0],'Confirm the appointment time from the original message',stamp()),(ids[0],'Record the confirmed transport arrangement',stamp()),(ids[1],'Write down questions to take to the visit',stamp())])
        c.executemany('INSERT INTO task_updates(task_id,update_type,body,created_at) VALUES(?,?,?,?)',[(ids[0],'Context','Example only: transport details still need confirmation.',stamp()),(ids[1],'Progress','Example only: draft questions have been started.',stamp())])
    c.execute('INSERT INTO notes(title,body,tag,created_at) VALUES(?,?,?,?)',('Example visit note','Questions to ask: what should we bring? Who is the transport contact? Confirm appointment time from the clinic message.','Visit prep',stamp()))
    c.commit(); audit('Example workflow loaded','Illustrative records created in an empty care plan')
    flash('Example workflow loaded. These are illustrative records; edit or remove them as needed.','success')
    return redirect(url_for('dashboard'))

@app.route('/intake', methods=['GET','POST'])
def intake():
    db=connect()
    if request.method=='POST':
        text=request.form.get('request_text','').strip(); kind=request.form.get('need_type','Coordination'); by=request.form.get('requested_by','').strip() or 'Not specified'; preferred=request.form.get('preferred_by','').strip(); constraints=request.form.get('constraints','').strip()
        if len(text)<12: flash('Describe the request in at least 12 characters so it can be reviewed safely.','error')
        elif kind not in {'Appointment','Transport','Documents','Daily support','Coordination','Other'}: flash('Choose a valid request type.','error')
        else:
            cur=db.execute('INSERT INTO intakes(request_text,need_type,requested_by,preferred_by,constraints,created_at) VALUES(?,?,?,?,?,?)',(text[:2000],kind,by[:100],preferred[:120],constraints[:1000],stamp())); db.commit(); audit('New care request received',f'Intake #{cur.lastrowid}: {kind}'); flash('Request captured in the intake queue. Review it before converting it into an action.','success')
        return redirect(url_for('intake'))
    rows=db.execute('SELECT i.*,t.title AS task_title FROM intakes i LEFT JOIN tasks t ON t.id=i.converted_task_id ORDER BY i.id DESC').fetchall()
    return render_template('intake.html',intakes=rows)

@app.route('/intake/<int:intake_id>/convert',methods=['POST'])
def convert_intake(intake_id):
    db=connect(); item=db.execute('SELECT * FROM intakes WHERE id=?',(intake_id,)).fetchone()
    if not item: flash('Request not found.','error')
    elif item['converted_task_id']: flash('This request has already been converted.','info')
    else:
        title=request.form.get('title','').strip() or item['request_text'][:180]
        owner=request.form.get('owner','').strip() or 'Unassigned'; due=request.form.get('due_at','').strip(); priority=request.form.get('priority','Normal')
        try: datetime.fromisoformat(due)
        except (ValueError,TypeError): flash('Choose a due date before converting this request.','error'); return redirect(url_for('intake'))
        if priority not in {'Low','Normal','High'}: priority='Normal'
        category='Appointment' if item['need_type']=='Appointment' else ('Preparation' if item['need_type']=='Documents' else 'Coordination')
        cur=db.execute('INSERT INTO tasks(title,category,due_at,owner,priority,source,created_at) VALUES(?,?,?,?,?,?,?)',(title[:180],category,due,owner[:100],priority,'Reviewed intake #'+str(intake_id),stamp()))
        db.execute("UPDATE intakes SET status='Converted',converted_task_id=? WHERE id=?",(cur.lastrowid,intake_id)); db.commit(); record_task_event(cur.lastrowid,'Created from intake',f'Reviewed request #{intake_id}: {item["request_text"][:500]}'); audit('Intake converted to action',f'#{intake_id} → task #{cur.lastrowid}'); flash('Reviewed request converted into a trackable action.','success')
    return redirect(url_for('intake'))

def handoff_event(handoff_id,event_type,actor,detail=''):
    db=connect(); db.execute('INSERT INTO handoff_events(handoff_id,event_type,actor,detail,created_at) VALUES(?,?,?,?,?)',(handoff_id,event_type,actor[:100],detail[:1000],stamp())); db.commit()

@app.route('/handoffs',methods=['GET','POST'])
def handoffs():
    db=connect()
    if request.method=='POST':
        title=request.form.get('title','').strip(); sender=request.form.get('from_person','').strip(); recipient=request.form.get('to_person','').strip(); summary=request.form.get('summary','').strip(); watch=request.form.get('watch_items','').strip(); next_steps=request.form.get('next_steps','').strip(); task_ids=request.form.getlist('task_ids'); priority=request.form.get('priority','Normal'); due=request.form.get('due_at','').strip() or None
        if not all([title,sender,recipient,summary]): flash('Add a title, both people and a handoff summary.','error')
        elif sender.casefold()==recipient.casefold(): flash('Choose a different receiving person for the handoff.','error')
        elif priority not in {'Low','Normal','High','Urgent'}: flash('Choose a valid handoff priority.','error')
        else:
            cur=db.execute("INSERT INTO handoffs(title,from_person,to_person,summary,watch_items,next_steps,status,created_at,priority,due_at,last_updated_at) VALUES(?,?,?,?,?,?,'Sent',?,?,?,?)",(title[:160],sender[:100],recipient[:100],summary[:3000],watch[:1500],next_steps[:1500],stamp(),priority,due,stamp()))
            hid=cur.lastrowid
            for raw in task_ids:
                if raw.isdigit() and db.execute('SELECT id FROM tasks WHERE id=?',(int(raw),)).fetchone(): db.execute('INSERT OR IGNORE INTO handoff_tasks(handoff_id,task_id) VALUES(?,?)',(hid,int(raw)))
            db.commit(); handoff_event(hid,'Sent',sender,'Handoff sent to '+recipient); audit('Care handoff sent',title); flash('Handoff sent and awaiting recipient review.','success')
        return redirect(url_for('handoffs'))
    rows=db.execute("SELECT * FROM handoffs ORDER BY CASE status WHEN 'Sent' THEN 0 WHEN 'Viewed' THEN 1 WHEN 'Acknowledged' THEN 2 WHEN 'In Progress' THEN 3 ELSE 4 END, id DESC").fetchall()
    enriched=[]; links={}; histories={}; workload={}
    for row in rows:
        h=dict(row);
        try: h['age_hours']=max(0,int((datetime.now()-datetime.fromisoformat(h['created_at'])).total_seconds()//3600))
        except Exception: h['age_hours']=0
        h['is_overdue']=bool(h.get('due_at') and h['status'] not in ('Completed','Cancelled') and h['due_at'] < stamp())
        enriched.append(h); hid=h['id']
        links[hid]=db.execute('SELECT t.id,t.title,t.status,t.due_at,t.owner FROM handoff_tasks h JOIN tasks t ON t.id=h.task_id WHERE h.handoff_id=?',(hid,)).fetchall()
        histories[hid]=db.execute('SELECT * FROM handoff_events WHERE handoff_id=? ORDER BY id DESC',(hid,)).fetchall()
        recipient=h['to_person']; workload[recipient]=db.execute("SELECT COUNT(*) total,SUM(CASE WHEN status='Blocked' THEN 1 ELSE 0 END) blocked,SUM(CASE WHEN status='Open' AND due_at<? THEN 1 ELSE 0 END) overdue FROM tasks WHERE lower(owner)=lower(?)",(stamp(),recipient)).fetchone()
    open_tasks=[t for t in all_tasks() if t['status'] in ('Open','Blocked')]
    stats={'active':sum(h['status'] not in ('Completed','Cancelled') for h in enriched),'awaiting':sum(h['status'] in ('Sent','Viewed') for h in enriched),'overdue':sum(h['is_overdue'] for h in enriched),'completed':sum(h['status']=='Completed' for h in enriched)}
    return render_template('handoffs.html',handoffs=enriched,links=links,tasks=open_tasks,histories=histories,workload=workload,stats=stats)

@app.route('/handoffs/<int:handoff_id>/<action>',methods=['POST'])
def handoff_transition(handoff_id,action):
    db=connect(); row=db.execute('SELECT * FROM handoffs WHERE id=?',(handoff_id,)).fetchone()
    if not row: flash('Handoff not found.','error'); return redirect(url_for('handoffs'))
    transitions={'view':('Sent','Viewed','Recipient opened the handoff.'),'ack':(('Sent','Viewed'),'Acknowledged','Recipient explicitly accepted responsibility.'),'start':('Acknowledged','In Progress','Recipient started the transferred work.'),'complete':('In Progress','Completed','Recipient marked the handoff complete after review.')}
    if action not in transitions: flash('Unknown handoff action.','error'); return redirect(url_for('handoffs'))
    expected,target,detail=transitions[action]; allowed=expected if isinstance(expected,tuple) else (expected,)
    if row['status'] not in allowed: flash(f'This action is not available while the handoff is {row["status"].lower()}.','info'); return redirect(url_for('handoffs'))
    now=stamp(); fields={'status':target,'last_updated_at':now}
    if action=='view': fields['viewed_at']=now
    if action=='ack': fields['acknowledged_at']=now
    if action=='start': fields['started_at']=now
    if action=='complete': fields['completed_at']=now
    db.execute('UPDATE handoffs SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',(*fields.values(),handoff_id)); db.execute("UPDATE alerts SET status='Resolved' WHERE title=? AND status='Open'",(f'Handoff awaiting acknowledgement · #{handoff_id}',)); db.commit()
    actor=row['to_person'] if action!='complete' else row['to_person']; handoff_event(handoff_id,target,actor,detail); audit('Handoff '+target.lower(),row['title']+' · '+actor); flash('Handoff status updated: '+target+'.','success'); return redirect(url_for('handoffs'))

@app.route('/readiness')
def readiness():
    db=connect(); tasks=all_tasks(); active=[t for t in tasks if t['status'] in ('Open','Blocked')]
    total=len(active); assigned=sum(1 for t in active if t['owner'].strip().lower() not in ('','unassigned','none','tbd')); dated=sum(1 for t in active if t['due_at']); acknowledged=sum(1 for t in active if t['acknowledged']); unblocked=sum(1 for t in active if t['status']!='Blocked')
    components=[('Ownership',assigned,total,'Every open action needs a willing accountable owner.'),('Timing',dated,total,'Actions need a reviewable due date.'),('Acknowledgement',acknowledged,total,'High-priority actions should be explicitly acknowledged.'),('Unblocked work',unblocked,total,'Blocked actions need a documented resolution path.')]
    score=round(sum((n/den if den else 1) for _,n,den,_ in components)/len(components)*100) if components else 100
    due_soon=[t for t in active if t['due_at'] and t['due_at'][:10]<=datetime.now().date().isoformat()]
    blocked=[t for t in active if t['status']=='Blocked']
    journeys=db.execute('SELECT * FROM journeys WHERE status=\'Active\' ORDER BY target_date').fetchall()
    return render_template('readiness.html',score=score,components=components,active=active,due_soon=due_soon,blocked=blocked,journeys=journeys)

@app.route('/api/status')
def status():
    credentials_present=bool(os.getenv('GNANI_API_KEY'))
    return jsonify({'app':'CareLoop AI','version':'V21','status':'ok','gnani_provider':'Prisma v3 STT','gnani_credentials_present':credentials_present,'gnani_endpoint_configured':True,'gnani_integration_tested':GNANI_LAST_SUCCESS,'gnani_connected':GNANI_LAST_SUCCESS,'capabilities':{'audio_transcription':'Gnani Prisma STT','typed_request_analysis':'CarePilot bilingual local intent engine'},'message':('Gnani Prisma completed a live transcription successfully in this server session.' if GNANI_LAST_SUCCESS else 'Gnani credentials are configured, but a successful live transcription has not yet been verified. Prisma is speech-to-text, not a text-generation model.')})
@app.context_processor
def common():
    try: return {'nav_open_count':connect().execute("SELECT COUNT(*) FROM tasks WHERE status='Open'").fetchone()[0],'nav_alert_count':connect().execute("SELECT COUNT(*) FROM alerts WHERE status='Open'").fetchone()[0],'gnani_configured':bool(os.getenv('GNANI_API_KEY','').strip()),'current_user':{'name':session.get('user_name','Care coordinator'),'email':session.get('user_email','')}}
    except Exception: return {'nav_open_count':0,'gnani_configured':bool(os.getenv('GNANI_API_KEY','').strip())}
init_db()
if __name__=='__main__': app.run(host='127.0.0.1',port=int(os.getenv('PORT','5002')),debug=False)
