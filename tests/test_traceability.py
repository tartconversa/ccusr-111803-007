"""可追溯采集记录的业务规则回归。

用合成 payload 驱动，不依赖网络来源，覆盖：
待核语义、回执补传、重复上传、凭证换版、改派、凭证先到、重启落差补齐、出处追问。
"""
import tempfile
import unittest
from pathlib import Path
from app import records
from app.domain import STATUS_PENDING,STATUS_VERIFIED,fingerprint,handoff_key

def catalog_item(item_id,revision,group='甲',name=None):
    return {'id':item_id,'name':name or f'运单{item_id}','group':group,
            'summary':'摘要','revision':revision}

def catalog_result(items,edition='1'):
    return {'url':f'http://127.0.0.1:8765/catalog?edition={edition}','edition':edition,'items':items}

def detail_result(item_id,revision,content,group='甲',name=None):
    return {'url':f'http://127.0.0.1:8765/detail?id={item_id}&edition={revision}','edition':revision,
            'item':{'id':item_id,'name':name or f'运单{item_id}','group':group,
                    'edition':revision,'revision':revision,'detail':content}}

class TraceabilityTest(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.path=Path(self.temporary.name)/'records.sqlite3'
    def tearDown(self):
        self.temporary.cleanup()
    def _waybill(self,source_id='w-1'):
        return next(w for w in records.waybill_overview(self.path) if w['source_id']==source_id)

    def test_arrival_without_evidence_is_pending_not_unsigned(self):
        records.save_catalog(self.path,catalog_result([catalog_item('w-1','1')]))
        waybill=self._waybill()
        self.assertEqual(waybill['current_handoff']['status'],STATUS_PENDING)
        pending=records.list_pending(self.path)
        self.assertEqual([p['handoff_key'] for p in pending],[handoff_key('w-1','1')])
        # 任何状态文案里都不得出现把缺凭证推断成未签收的结论
        self.assertNotIn('未签收',repr(records.waybill_overview(self.path)))

    def test_late_evidence_verifies_pending_handoff(self):
        records.save_catalog(self.path,catalog_result([catalog_item('w-1','1')]))
        outcome=records.save_detail(self.path,detail_result('w-1','1','签收图片甲'))
        self.assertEqual(outcome['evidence']['outcome'],'verified')
        self.assertEqual(self._waybill()['current_handoff']['status'],STATUS_VERIFIED)
        self.assertEqual(records.list_pending(self.path),[])

    def test_duplicate_upload_is_idempotent(self):
        records.save_catalog(self.path,catalog_result([catalog_item('w-1','1')]))
        first=records.save_detail(self.path,detail_result('w-1','1','签收图片甲'))
        second=records.save_detail(self.path,detail_result('w-1','1','签收图片甲'))
        self.assertEqual(second['evidence']['outcome'],'duplicate')
        self.assertEqual(second['evidence']['evidence_id'],first['evidence']['evidence_id'])
        handoff=records.trace_handoff(self.path,handoff_key('w-1','1'))
        self.assertEqual(len(handoff['evidence_versions']),1)
        # 重复上传不产生第二条原件
        self.assertEqual(len(records.list_receipts(self.path)),2)

    def test_evidence_replacement_does_not_rewrite_verified(self):
        records.save_catalog(self.path,catalog_result([catalog_item('w-1','1')]))
        records.save_detail(self.path,detail_result('w-1','1','签收图片甲'))
        records.save_detail(self.path,detail_result('w-1','1','签收图片修订甲'))
        handoff=records.trace_handoff(self.path,handoff_key('w-1','1'))
        self.assertEqual(len(handoff['evidence_versions']),2)
        versions=handoff['evidence_versions']
        self.assertEqual([v['status'] for v in versions],[STATUS_VERIFIED,STATUS_PENDING])
        self.assertEqual(versions[0]['content'],'签收图片甲')
        self.assertEqual(handoff['status'],STATUS_VERIFIED)
        self.assertEqual(handoff['replacement_pending'],1)
        # 待核列表能追到这次换版
        self.assertTrue(any(p['handoff_key']==handoff['handoff_key'] for p in records.list_pending(self.path)))

    def test_reroute_keeps_old_handoff_and_signature(self):
        records.save_catalog(self.path,catalog_result([catalog_item('w-1','1',group='甲')]))
        records.save_detail(self.path,detail_result('w-1','1','旧路线签名',group='甲'))
        # 改派：新游标、新交接站；旧签名绝不能配给新路线
        records.save_catalog(self.path,catalog_result([catalog_item('w-1','2',group='乙')],edition='2'))
        records.save_detail(self.path,detail_result('w-1','2','新路线签名',group='乙'))
        waybill=self._waybill()
        self.assertEqual(waybill['current_revision'],'2')
        self.assertEqual(waybill['current_station'],'乙')
        self.assertEqual(waybill['handoff_count'],2)
        old=records.trace_handoff(self.path,handoff_key('w-1','1'))
        new=records.trace_handoff(self.path,handoff_key('w-1','2'))
        self.assertEqual(old['station'],'甲')
        self.assertEqual(old['evidence_versions'][0]['content'],'旧路线签名')
        self.assertEqual(new['station'],'乙')
        self.assertEqual(new['evidence_versions'][0]['content'],'新路线签名')

    def test_evidence_arrives_before_catalog(self):
        # 下一趟网络恢复才把到站表补齐：凭证先到，交接自动建为已核实
        records.save_detail(self.path,detail_result('w-1','1','签收图片甲'))
        self.assertEqual(self._waybill()['current_handoff']['status'],STATUS_VERIFIED)
        records.save_catalog(self.path,catalog_result([catalog_item('w-1','1')]))
        handoff=records.trace_handoff(self.path,handoff_key('w-1','1'))
        self.assertEqual(handoff['status'],STATUS_VERIFIED)
        self.assertIsNotNone(handoff['arrival_receipt'])
        self.assertEqual(len(handoff['evidence_versions']),1)

    def test_restart_backfills_gap_between_raw_and_cursor(self):
        # 模拟崩溃：原件已追加但关联未执行
        db=records.connect(self.path)
        try:
            item=catalog_item('w-1','1')
            rid,_=records.append_raw_receipt(db,'catalog',item['id'],'1',item)
            db.commit()
        finally:
            db.close()
        state={p['feed']:p for p in records.progress_state(self.path)}
        self.assertEqual(state['catalog']['gap'],1)
        self.assertIsNone(state['catalog']['last_page'])
        # 重启续跑：落差补齐
        outcomes=records.process_pending(self.path)
        self.assertEqual(len(outcomes),1)
        self.assertEqual(outcomes[0]['handoff_key'],handoff_key('w-1','1'))
        for p in records.progress_state(self.path):self.assertEqual(p['gap'],0)
        self.assertEqual(self._waybill()['current_handoff']['status'],STATUS_PENDING)
        # 再跑一次幂等，不重复关联
        self.assertEqual(records.process_pending(self.path),[])

    def test_trace_returns_full_provenance(self):
        records.save_catalog(self.path,catalog_result([catalog_item('w-1','1',name='运单到站甲')]))
        records.save_detail(self.path,detail_result('w-1','1','签收图片甲'))
        handoff=records.trace_handoff(self.path,handoff_key('w-1','1'))
        # 到站出处：原件收据号、到达时间、当时的原始 payload
        self.assertEqual(handoff['arrival_source']['payload']['name'],'运单到站甲')
        receipt_ids={r['receipt'] for r in records.list_receipts(self.path)}
        self.assertIn(handoff['arrival_receipt'],receipt_ids)
        self.assertIn(handoff['evidence_versions'][0]['receipt_id'],receipt_ids)
        self.assertEqual(handoff['evidence_versions'][0]['content_hash'],fingerprint('签收图片甲'))

    def test_invalid_identity_rejected(self):
        with self.assertRaises(ValueError):
            records.save_catalog(self.path,catalog_result([catalog_item('','1')]))
        with self.assertRaises(ValueError):
            records.save_detail(self.path,detail_result('w-1','9','坏游标'))

if __name__=='__main__':unittest.main()
