"""本地演练来源的读取回归（来源行为未改，只适配新的采集入口）。"""
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen
from app.site import DATA,start_server
from app.capture import read_catalog,read_detail,collect
from app.records import list_receipts

class SourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=start_server();cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start();cls.origin=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2)
    def test_catalog_and_detail(self):
        catalog=read_catalog(self.origin)
        self.assertEqual(len(catalog['items']),len(DATA['items']))
        for item in catalog['items']:
            self.assertEqual(item['revision'],'1')
            self.assertEqual(read_detail(self.origin,item['id'])['item']['id'],item['id'])
    def test_revision_and_page(self):
        key=DATA['items'][0]['id']
        self.assertNotEqual(read_detail(self.origin,key,'1')['item']['detail'],read_detail(self.origin,key,'2')['item']['detail'])
        with urlopen(self.origin+'/catalog.html?edition=2') as response:self.assertIn(DATA['title'],response.read().decode('utf-8'))
    def test_collect_writes_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'records.sqlite3'
            summary=collect(self.origin,path)
            self.assertEqual(len(summary['catalog']),2)
            self.assertEqual(len(summary['details']),2)
            # 每张运单一次到站原件 + 一条凭证原件
            self.assertEqual(len(list_receipts(path)),4)
    def test_remote_rejected(self):
        with self.assertRaises(ValueError):read_catalog('https://example.com')
