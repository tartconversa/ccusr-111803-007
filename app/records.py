"""可追溯采集记录：原件只追加，交接与凭证按自然键关联，不覆盖已核实记录。

表结构围绕三层身份组织：
- raw_receipts：每一次来源读取的原始收据，只追加，永不更新；重启后可按收据补齐关联。
- waybills：运单当前路线（最新游标指向的交接），是客服看到的“现在状态”。
- handoffs：每次实际交接（运单+游标），到站与凭证各自带来出处收据；已核实交接不被改写。
- evidence：交接下的凭证版本，按内容指纹去重；换内容追加新版本并标记待核，不替换旧版。
- progress：各采集来源的最新游标与已关联到的原件收据，重启据此补齐落差。
"""
import json
import sqlite3
from pathlib import Path
from .domain import (fingerprint,handoff_key,is_newer_revision,
                     STATUS_PENDING,STATUS_VERIFIED)

SCHEMA_VERSION='2'

def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS raw_receipts (receipt INTEGER PRIMARY KEY AUTOINCREMENT,feed TEXT NOT NULL,source_id TEXT NOT NULL,revision TEXT NOT NULL,payload TEXT NOT NULL,content_hash TEXT NOT NULL,received_at TEXT NOT NULL DEFAULT (datetime(\'now\')),processed_receipt INTEGER)')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_identity ON raw_receipts (feed,content_hash)')
    db.execute('CREATE TABLE IF NOT EXISTS waybills (source_id TEXT PRIMARY KEY,name TEXT NOT NULL,current_revision TEXT NOT NULL,current_station TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT (datetime(\'now\')))')
    db.execute("CREATE TABLE IF NOT EXISTS handoffs (handoff_key TEXT PRIMARY KEY,source_id TEXT NOT NULL,revision TEXT NOT NULL,station TEXT NOT NULL,status TEXT NOT NULL DEFAULT '待核',arrival_receipt INTEGER,evidence_receipt INTEGER,replacement_pending INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT (datetime('now')),FOREIGN KEY (source_id) REFERENCES waybills(source_id))")
    db.execute('CREATE TABLE IF NOT EXISTS evidence (evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,handoff_key TEXT NOT NULL,content_hash TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,receipt_id INTEGER NOT NULL,received_at TEXT NOT NULL DEFAULT (datetime(\'now\')),FOREIGN KEY (handoff_key) REFERENCES handoffs(handoff_key),FOREIGN KEY (receipt_id) REFERENCES raw_receipts(receipt))')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_version ON evidence (handoff_key,content_hash)')
    db.execute('CREATE TABLE IF NOT EXISTS progress (feed TEXT PRIMARY KEY,last_page TEXT NOT NULL,last_revision TEXT NOT NULL,processed_receipt INTEGER NOT NULL DEFAULT 0)')
    db.execute('INSERT OR IGNORE INTO schema_meta VALUES (?,?)',('schema_version',SCHEMA_VERSION))
    db.commit()
    return db

def _payload_hash(payload):
    return fingerprint(json.dumps(payload,ensure_ascii=False,sort_keys=True))

def append_raw_receipt(db,feed,source_id,revision,payload):
    """追加原件。同来源同内容的重复上传返回既有收据，不产生第二条原件。"""
    digest=_payload_hash(payload)
    row=db.execute('SELECT receipt FROM raw_receipts WHERE feed=? AND content_hash=?',(feed,digest)).fetchone()
    if row is not None:return row[0],False
    cur=db.execute('INSERT INTO raw_receipts (feed,source_id,revision,payload,content_hash) VALUES (?,?,?,?,?)',
                   (feed,source_id,str(revision),json.dumps(payload,ensure_ascii=False),digest))
    return cur.lastrowid,True

def _upsert_waybill(db,source_id,name,revision,station):
    row=db.execute('SELECT current_revision FROM waybills WHERE source_id=?',(source_id,)).fetchone()
    if row is None:
        db.execute('INSERT INTO waybills (source_id,name,current_revision,current_station) VALUES (?,?,?,?)',
                   (source_id,name,revision,station))
    elif is_newer_revision(revision,row[0]):
        db.execute('UPDATE waybills SET name=?,current_revision=?,current_station=?,updated_at=datetime(\'now\') WHERE source_id=?',
                   (name,revision,station,source_id))

def _upsert_handoff(db,identity,arrival_receipt=None):
    """到站登记：交接已存在时只补空缺出处，绝不改路线、不改状态、不动已核实凭证。"""
    key=identity['handoff_key']
    row=db.execute('SELECT handoff_key FROM handoffs WHERE handoff_key=?',(key,)).fetchone()
    if row is None:
        db.execute('INSERT INTO handoffs (handoff_key,source_id,revision,station,status,arrival_receipt) VALUES (?,?,?,?,?,?)',
                   (key,identity['source_id'],identity['revision'],identity['group'],STATUS_PENDING,arrival_receipt))
        return 'created'
    if arrival_receipt is not None:
        db.execute('UPDATE handoffs SET arrival_receipt=COALESCE(arrival_receipt,?) WHERE handoff_key=?',
                   (arrival_receipt,key))
    return 'existing'

def _attach_evidence(db,identity,receipt_id):
    """凭证关联到交接。同指纹=重复上传（忽略）；换内容=新版本待核，旧版保留为已核实。"""
    key=identity['handoff_key']
    dup=db.execute('SELECT evidence_id,status FROM evidence WHERE handoff_key=? AND content_hash=?',
                   (key,identity['content_hash'])).fetchone()
    if dup is not None:
        return {'outcome':'duplicate','evidence_id':dup[0],'status':dup[1]}
    already_verified=db.execute('SELECT 1 FROM evidence WHERE handoff_key=? AND status=?',
                                (key,STATUS_VERIFIED)).fetchone() is not None
    if already_verified:
        version_status=STATUS_PENDING
        db.execute('UPDATE handoffs SET replacement_pending=1 WHERE handoff_key=?',(key,))
    else:
        version_status=STATUS_VERIFIED
        db.execute('UPDATE handoffs SET status=?,evidence_receipt=? WHERE handoff_key=?',
                   (STATUS_VERIFIED,receipt_id,key))
    cur=db.execute('INSERT INTO evidence (handoff_key,content_hash,content,status,receipt_id) VALUES (?,?,?,?,?)',
                   (key,identity['content_hash'],identity['content'],version_status,receipt_id))
    return {'outcome':('replacement_pending' if already_verified else 'verified'),
            'evidence_id':cur.lastrowid,'status':version_status}

def link_catalog_receipt(db,receipt_id):
    """把一条到站原件解释并关联到运单/交接。"""
    row=db.execute('SELECT payload FROM raw_receipts WHERE receipt=?',(receipt_id,)).fetchone()
    if row is None:raise ValueError(f'原件收据 {receipt_id} 不存在')
    from .domain import interpret_catalog
    item=json.loads(row[0]);identity=interpret_catalog(item)
    _upsert_waybill(db,identity['source_id'],identity['name'],identity['revision'],identity['group'])
    handoff=_upsert_handoff(db,identity,arrival_receipt=receipt_id)
    db.execute('UPDATE raw_receipts SET processed_receipt=? WHERE receipt=?',(receipt_id,receipt_id))
    return {'receipt_id':receipt_id,'source_id':identity['source_id'],
            'handoff_key':identity['handoff_key'],'handoff':handoff}

def link_detail_receipt(db,receipt_id):
    """把一条凭证原件解释并关联到具体交接；凭证先于到站时自动建待核交接。"""
    row=db.execute('SELECT payload FROM raw_receipts WHERE receipt=?',(receipt_id,)).fetchone()
    if row is None:raise ValueError(f'原件收据 {receipt_id} 不存在')
    from .domain import interpret_detail
    result=json.loads(row[0]);identity=interpret_detail(result['item'])
    _upsert_waybill(db,identity['source_id'],identity['name'],identity['revision'],identity['group'])
    _upsert_handoff(db,identity)
    evidence=_attach_evidence(db,identity,receipt_id)
    db.execute('UPDATE raw_receipts SET processed_receipt=? WHERE receipt=?',(receipt_id,receipt_id))
    return {'receipt_id':receipt_id,'source_id':identity['source_id'],
            'handoff_key':identity['handoff_key'],'evidence':evidence}

_LINKERS={'catalog':link_catalog_receipt,'detail':link_detail_receipt}

def process_pending(path):
    """补齐落差：把所有已存原件中尚未关联的收据逐条解释入账，返回补齐结果。

    每条收据独立事务，崩溃只影响当前一条；重复执行安全（已关联的原件会跳过）。
    """
    db=connect(path);outcomes=[]
    try:
        rows=db.execute('SELECT receipt,feed FROM raw_receipts WHERE processed_receipt IS NULL ORDER BY receipt').fetchall()
        for receipt_id,feed in rows:
            linker=_LINKERS.get(feed)
            if linker is None:continue
            try:
                outcome=linker(db,receipt_id);db.commit();outcomes.append(outcome)
            except Exception:
                db.rollback();raise
        for feed, in db.execute('SELECT DISTINCT feed FROM raw_receipts').fetchall():
            row=db.execute('SELECT MAX(receipt) FROM raw_receipts WHERE feed=? AND processed_receipt IS NOT NULL',(feed,)).fetchone()
            if row and row[0] is not None:
                revision=db.execute('SELECT revision FROM raw_receipts WHERE receipt=?',(row[0],)).fetchone()[0]
                db.execute('''INSERT INTO progress (feed,last_page,last_revision,processed_receipt)
                              VALUES (?,?,?,?)
                              ON CONFLICT(feed) DO UPDATE SET
                              last_revision=excluded.last_revision,
                              processed_receipt=MAX(progress.processed_receipt,excluded.processed_receipt)''',
                           (feed,'(backfill)',revision,row[0]))
        db.commit()
        return outcomes
    finally:
        db.close()

def _advance_progress(db,feed,page,revision,processed_receipt):
    db.execute('''INSERT INTO progress (feed,last_page,last_revision,processed_receipt) VALUES (?,?,?,?)
                  ON CONFLICT(feed) DO UPDATE SET last_page=excluded.last_page,last_revision=excluded.last_revision,
                  processed_receipt=MAX(progress.processed_receipt,excluded.processed_receipt)''',
               (feed,page,str(revision),processed_receipt))

def save_catalog(path,result):
    """到站表整批入账：原件只追加，交接只新建或补出处，返回逐条处理结果。"""
    db=connect(path);outcomes=[]
    try:
        for item in result['items']:
            receipt_id,_=append_raw_receipt(db,'catalog',item['id'],item['revision'],item)
            outcomes.append(link_catalog_receipt(db,receipt_id))
            _advance_progress(db,'catalog',result['url'],result['edition'],receipt_id)
        db.commit()
    except Exception:
        db.rollback();raise
    finally:
        db.close()
    return outcomes

def save_detail(path,result):
    """凭证入账：同内容重复上传幂等，换内容追加待新版本，已核实交接不被改写。"""
    db=connect(path)
    try:
        item=result['item']
        receipt_id,_=append_raw_receipt(db,'detail',item['id'],item['revision'],result)
        outcome=link_detail_receipt(db,receipt_id)
        _advance_progress(db,'detail',result['url'],result['edition'],receipt_id)
        db.commit()
    except Exception:
        db.rollback();raise
    finally:
        db.close()
    return outcome

def waybill_overview(path):
    """客服视图：运单当前路线 + 最新交接状态；缺凭证显示待核，不显示未签收。"""
    db=connect(path)
    try:
        waybills=[dict(r) for r in db.execute('SELECT * FROM waybills ORDER BY source_id').fetchall()]
        for w in waybills:
            key=handoff_key(w['source_id'],w['current_revision'])
            h=db.execute('SELECT status,station,revision,replacement_pending FROM handoffs WHERE handoff_key=?',(key,)).fetchone()
            w['current_handoff']=dict(h) if h else None
            w['handoff_count']=db.execute('SELECT COUNT(*) FROM handoffs WHERE source_id=?',(w['source_id'],)).fetchone()[0]
        return waybills
    finally:
        db.close()

def trace_handoff(path,handoff_key_value):
    """追问某一次交接的真实出处：路线、状态、全部凭证版本及各自原件收据。"""
    db=connect(path)
    try:
        h=db.execute('SELECT * FROM handoffs WHERE handoff_key=?',(handoff_key_value,)).fetchone()
        if h is None:return None
        handoff=dict(h)
        handoff['evidence_versions']=[dict(r) for r in db.execute(
            'SELECT evidence_id,content_hash,content,status,receipt_id,received_at FROM evidence WHERE handoff_key=? ORDER BY evidence_id',(handoff_key_value,))]
        if handoff.get('arrival_receipt'):
            row=db.execute('SELECT payload,received_at FROM raw_receipts WHERE receipt=?',(handoff['arrival_receipt'],)).fetchone()
            handoff['arrival_source']={'receipt':handoff['arrival_receipt'],'received_at':row[1],'payload':json.loads(row[0])} if row else None
        return handoff
    finally:
        db.close()

def list_pending(path):
    """所有待核交接：凭证缺席或凭证换版待核，供客服逐单追问。"""
    db=connect(path)
    try:
        rows=db.execute('SELECT handoff_key,source_id,revision,station,status,replacement_pending FROM handoffs WHERE status=? OR replacement_pending=1 ORDER BY source_id,revision',(STATUS_PENDING,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

def list_receipts(path):
    """原件流水：每次来源读取的不可变收据，按到达顺序返回。"""
    db=connect(path)
    try:
        return [dict(receipt=r[0],feed=r[1],source_id=r[2],revision=r[3],received_at=r[4],
                     processed=r[5] is not None,payload=json.loads(r[6]))
                for r in db.execute('SELECT receipt,feed,source_id,revision,received_at,processed_receipt,payload FROM raw_receipts ORDER BY receipt')]
    finally:
        db.close()

def progress_state(path):
    """游标状态：已存原件与已关联原件的位置，落差一眼可见。"""
    db=connect(path)
    try:
        out=[]
        for feed, in db.execute('SELECT DISTINCT feed FROM raw_receipts ORDER BY feed').fetchall():
            last_stored=db.execute('SELECT MAX(receipt) FROM raw_receipts WHERE feed=?',(feed,)).fetchone()[0]
            p=db.execute('SELECT last_page,last_revision,processed_receipt FROM progress WHERE feed=?',(feed,)).fetchone()
            out.append({'feed':feed,'last_stored_receipt':last_stored,
                        'last_page':p[0] if p else None,'last_revision':p[1] if p else None,
                        'processed_receipt':p[2] if p else 0,
                        'gap':(last_stored or 0)-(p[2] if p else 0)})
        return out
    finally:
        db.close()
