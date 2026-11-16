import os,sqlite3,json,time
from pathlib import Path
from app.errors import RuleError

SCHEMA={
    'raw':'BLOB',
    'status':"TEXT NOT NULL DEFAULT 'official'",
    'superseded_by':'TEXT',
    'superseded_at':'TEXT',
}

def connect():
    path=os.environ.get('DB_PATH','/tmp/labclock.sqlite3')
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=15);con.row_factory=sqlite3.Row
    con.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, reviewed INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL)')
    columns={row[1] for row in con.execute('PRAGMA table_info(runs)')}
    for name,declaration in SCHEMA.items():
        if name not in columns:con.execute(f'ALTER TABLE runs ADD COLUMN {name} {declaration}')
    con.execute('CREATE TABLE IF NOT EXISTS corrections(key TEXT PRIMARY KEY, base_id TEXT NOT NULL, new_id TEXT NOT NULL, patch TEXT NOT NULL, note TEXT NOT NULL DEFAULT \'\', created TEXT NOT NULL)')
    con.execute("CREATE TABLE IF NOT EXISTS calendar_state(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL, updated TEXT NOT NULL)")
    con.commit()
    return con

def _decorate(row):
    out=json.loads(row['payload'])
    out['version']=row['version'];out['reviewed']=bool(row['reviewed'])
    out['status']=row['status'] if 'status' in row.keys() else 'official'
    out['superseded_by']=row['superseded_by'] if 'superseded_by' in row.keys() else None
    out['superseded_at']=row['superseded_at'] if 'superseded_at' in row.keys() else None
    return out

def save(bundle,raw=None):
    rid=bundle['manifest']['run_id']
    con=connect()
    try:
        con.execute('BEGIN IMMEDIATE')
        exists=con.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
        if not exists:
            con.execute("UPDATE runs SET status='prior' WHERE status='official'")
            con.execute('INSERT INTO runs(id,payload,created,raw,status) VALUES(?,?,?,?,?)',(rid,json.dumps(bundle,ensure_ascii=False),bundle['manifest']['created_at'],sqlite3.Binary(raw) if raw is not None else None,'official'))
        else:
            con.execute("UPDATE runs SET status='prior' WHERE status='official' AND id<>?",(rid,))
            con.execute("UPDATE runs SET status='official',superseded_by=NULL,superseded_at=NULL WHERE id=?",(rid,))
            if raw is not None:con.execute('UPDATE runs SET raw=COALESCE(raw,?) WHERE id=?',(sqlite3.Binary(raw),rid))
        con.commit()
        row=con.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
    except Exception:
        con.rollback();raise
    finally:
        con.close()
    return _decorate(row)

def get(rid):
    with connect() as c:row=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
    return _decorate(row) if row else None

def raw(rid):
    with connect() as c:row=c.execute('SELECT raw FROM runs WHERE id=?',(rid,)).fetchone()
    return bytes(row['raw']) if row and row['raw'] is not None else None

def latest():
    with connect() as c:row=c.execute("SELECT * FROM runs ORDER BY (status='official') DESC, rowid DESC LIMIT 1").fetchone()
    return _decorate(row) if row else None

def official():
    with connect() as c:row=c.execute("SELECT * FROM runs WHERE status='official' ORDER BY rowid DESC LIMIT 1").fetchone()
    return _decorate(row) if row else None

def history():
    with connect() as c:
        rows=c.execute('SELECT id,version,reviewed,created,status,superseded_by,superseded_at FROM runs ORDER BY (status=\'official\') DESC, rowid DESC').fetchall()
    return [dict(r) for r in rows]

def review(rid,version):
    with connect() as c:
        cur=c.execute("UPDATE runs SET version=version+1,reviewed=1 WHERE id=? AND version=? AND status='official'",(rid,version))
        if cur.rowcount!=1:raise RuleError('STALE','记录已变更，请刷新后复核',409)
    return {'run_id':rid,'version':version+1,'reviewed':True}

def correction(key):
    with connect() as c:row=c.execute('SELECT * FROM corrections WHERE key=?',(key,)).fetchone()
    return dict(row) if row else None

def active_calendar():
    with connect() as c:row=c.execute('SELECT value,updated FROM calendar_state WHERE id=1').fetchone()
    return (json.loads(row['value']),row['updated']) if row else (None,None)

def corrections():
    with connect() as c:rows=c.execute('SELECT * FROM corrections ORDER BY rowid DESC').fetchall()
    out=[]
    for row in rows:
        item=dict(row);item['patch']=json.loads(item['patch']);out.append(item)
    return out

def _record_correction(c,key,base_id,new_id,patch,note,created):
    c.execute('INSERT INTO corrections(key,base_id,new_id,patch,note,created) VALUES(?,?,?,?,?,?)',(key,base_id,new_id,json.dumps(patch,ensure_ascii=False,sort_keys=True),note,created))

def backfill_raw(candidates):
    """Attach raw bytes to legacy rows only when a candidate hashes to the stored digest."""
    import hashlib
    known={hashlib.sha256(data).hexdigest():data for data in candidates}
    with connect() as c:
        rows=c.execute('SELECT id,payload FROM runs WHERE raw IS NULL').fetchall()
        for row in rows:
            digest=json.loads(row['payload'])['manifest']['sha256']
            data=known.get(digest)
            if data is not None:c.execute('UPDATE runs SET raw=? WHERE id=?',(sqlite3.Binary(data),row['id']))
    return len(rows)

def apply_correction(base_id,new_bundle,key,patch,note,raw_bytes,expected_version=None,calendar=None):
    """Atomically publish the corrected bundle and supersede the old official run.

    The whole switch happens in one BEGIN IMMEDIATE transaction: either the new
    official result, the correction ledger row and the supersession marker all
    land, or none of them do and the previous official result keeps serving.
    """
    nid=new_bundle['manifest']['run_id'];created=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    con=connect()
    try:
        con.execute('BEGIN IMMEDIATE')
        existing=con.execute('SELECT * FROM corrections WHERE key=?',(key,)).fetchone()
        if existing:
            con.commit();return get(existing['new_id']),True
        base=con.execute('SELECT * FROM runs WHERE id=?',(base_id,)).fetchone()
        if not base:raise RuleError('NOT_FOUND','当前批次不存在',404)
        if base['status']!='official':raise RuleError('STALE','该批复已被更正，请刷新后复核',409)
        current=con.execute("SELECT id FROM runs WHERE status='official' ORDER BY rowid DESC LIMIT 1").fetchone()
        if not current or current['id']!=base_id:raise RuleError('STALE','存在更新的正式结论，请刷新后复核',409)
        if expected_version is not None and int(base['version'])!=int(expected_version):raise RuleError('STALE','记录已变更，请刷新后复核',409)
        stored=con.execute('SELECT status FROM runs WHERE id=?',(nid,)).fetchone()
        if stored:
            con.execute("UPDATE runs SET status='official',superseded_by=NULL,superseded_at=NULL WHERE id=?",(nid,))
        else:
            con.execute('INSERT INTO runs(id,payload,created,raw,status) VALUES(?,?,?,?,?)',(nid,json.dumps(new_bundle,ensure_ascii=False),created,sqlite3.Binary(raw_bytes),'official'))
        con.execute("UPDATE runs SET status='superseded',superseded_by=?,superseded_at=? WHERE id=? AND status='official'",(nid,created,base_id))
        if calendar is not None:
            con.execute('INSERT INTO calendar_state(id,value,updated) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value,updated=excluded.updated',(json.dumps(calendar,sort_keys=True,ensure_ascii=False),created))
        _record_correction(con,key,base_id,nid,patch,note,created)
        con.commit()
    except Exception:
        con.rollback();raise
    finally:
        con.close()
    return get(nid),False
