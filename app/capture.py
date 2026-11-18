"""从本地来源获取目录和单项详情，并提供可续跑的整轮采集。"""
import json
from urllib.parse import urlencode,urlsplit
from urllib.request import urlopen
from .domain import interpret_catalog,interpret_detail
from . import records

def get(origin,path,query):
    url=urlsplit(origin)
    if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost'):raise ValueError('只允许本地演练来源')
    address=origin.rstrip('/')+path+'?'+urlencode(query)
    with urlopen(address,timeout=5) as response:data=json.load(response)
    return address,data

def read_catalog(origin,edition='1'):
    address,data=get(origin,'/catalog',{'edition':edition})
    # 目录条目本身不带游标，发布期 edition 就是这一轮到站交接的游标。
    items=[dict(item,revision=data['edition'],identity=interpret_catalog(dict(item,revision=data['edition'])))
           for item in data['items']]
    return {'url':address,'edition':data['edition'],'items':items}

def read_detail(origin,item_id,edition='1'):
    address,data=get(origin,'/detail',{'id':item_id,'edition':edition})
    return {'url':address,'edition':data['edition'],'item':dict(data,identity=interpret_detail(data))}

def collect(origin,db_path,edition='1'):
    """采集一轮到站表及其全部签收回执。

    所有写入幂等：重复上传按内容指纹识别，凭证换版只追加待新版本。
    先补落差再采集，保证崩溃重启时已存原件不会游离在关联记录之外。
    """
    backfill=records.process_pending(db_path)
    catalog=read_catalog(origin,edition)
    catalog_outcomes=records.save_catalog(db_path,catalog)
    detail_outcomes=[]
    for item in catalog['items']:
        detail_outcomes.append(records.save_detail(db_path,read_detail(origin,item['id'],edition)))
    return {'edition':edition,'backfilled':backfill,
            'catalog':catalog_outcomes,'details':detail_outcomes}
