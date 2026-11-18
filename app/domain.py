"""解释目录与详情的业务身份。

业务约定（见 docs/source.md）：
- 稳定标识 id 是运单业务键，显示名称只作展示，不作业务键。
- group 是交接站，revision（游标）区分同一运单在不同发布期的实际交接。
- 同一运单的每个 revision 都是一次独立交接；最新 revision 代表运单当前路线，
  改派只产生新交接，旧交接与旧凭证必须原样保留。
- 凭证缺席是“待核”，任何组件都不得据此推断为未签收。
"""

STATUS_PENDING='待核'
STATUS_VERIFIED='已核实'
VALID_REVISIONS=('1','2')

def interpret_catalog(row):
    """到站表条目：声明一次交接的路线，此时还没有凭证。"""
    if not row['id'] or not row['name']:raise ValueError('目录身份不完整')
    if row['group'] not in ('甲','乙'):raise ValueError('交接站不完整')
    return {'source_id':row['id'],'group':row['group'],'name':row['name'],
            'revision':row['revision'],'handoff_key':handoff_key(row['id'],row['revision']),
            'status':STATUS_PENDING}

def interpret_detail(row):
    """签收回执：凭证通过运单+游标锚定到具体某一次交接。"""
    if not row['id'] or row['revision'] not in VALID_REVISIONS:raise ValueError('详情身份不完整')
    if not row.get('detail'):raise ValueError('凭证内容不完整')
    return {'source_id':row['id'],'group':row['group'],'name':row['name'],
            'revision':row['revision'],'handoff_key':handoff_key(row['id'],row['revision']),
            'content':row['detail'],'content_hash':fingerprint(row['detail']),
            'status':STATUS_VERIFIED}

def waybill_key(source_id):
    """运单业务键：来源稳定标识，跨发布期不变。"""
    if not source_id:raise ValueError('运单标识不完整')
    return source_id

def handoff_key(source_id,revision):
    """一次实际交接的自然键：运单 + 游标。改派产生新键，绝不复用旧交接。"""
    return f'{waybill_key(source_id)}#r{revision}'

def fingerprint(content):
    """凭证内容指纹：同内容重复上传识别为同一版本，换内容则是新版本。"""
    import hashlib
    return hashlib.sha256(str(content).encode('utf-8')).hexdigest()

def revision_number(revision):
    """游标按数值比较先后；无法解析时退回到字符串排序，保证不崩溃。"""
    try:return int(revision)
    except (TypeError,ValueError):return -1

def is_newer_revision(candidate,current):
    return revision_number(candidate)>revision_number(current)
