"""Search behavior and migration invariants against isolated real MongoDB databases."""
import copy
import hashlib
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from integration_test import TestServer
from memory.storage import Repository, ApiError
from scripts.migrate_sqlite import migrate, read_source

class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = TestServer(Path(self.temp.name))
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def create(self, payload):
        return self.server.json_call('/api/entries', 'POST', payload, 201)['entry']

    def search(self, q):
        return self.server.json_call('/api/entries?q=' + urllib.parse.quote(q))['entries']

    def test_chinese_english_terms_and_literal_symbols(self):
        note = self.create({'content':'上海客户 MongoDB a.* [你好]'})
        todo = self.create({'type':'todo','items':['下周拜访上海客户','检查备份']})
        self.assertEqual(len(self.search('上海')), 2)
        self.assertEqual([e['id'] for e in self.search('MONGODB 上海')], [note['id']])
        self.assertEqual([e['id'] for e in self.search('上海 备份')], [todo['id']])
        self.assertEqual([e['id'] for e in self.search('a.*')], [note['id']])
        self.assertEqual(self.search('.*不存在'), [])
        self.assertEqual(len(self.search('   ')), 2)
        self.server.json_call('/api/entries?q='+urllib.parse.quote('中'*201), expected=400)
        self.assertEqual(len(self.search('')), 2)

    def test_filename_caption_and_updates(self):
        entry = self.server.upload_file('年度报告.PDF', 'application/pdf', b'document bytes', '预算 Review')['entry']
        self.assertEqual([e['id'] for e in self.search('报告 review')], [entry['id']])
        self.assertEqual(len(self.search('pdf')), 1)
        self.server.json_call('/api/entries/'+str(entry['id']), 'PATCH', {'content':'财务新说明'})
        self.assertEqual(self.search('预算'), [])
        self.assertEqual(len(self.search('财务 报告')), 1)
        self.server.json_call('/api/entries/'+str(entry['id']), 'DELETE')
        self.assertEqual(self.search('报告'), [])

    def test_search_keeps_pin_order_and_entire_todo(self):
        first = self.create({'type':'todo','items':['命中关键词','其他项目']})
        self.create({'content':'命中关键词'})
        self.server.json_call('/api/entries/'+str(first['id']), 'PATCH', {'pinned':True})
        result = self.search('命中')
        self.assertEqual(result[0]['id'], first['id'])
        self.assertEqual(len(result[0]['items']), 2)

    def test_compare_and_swap_rejects_stale_document(self):
        e = self.create({'content':'原内容'})
        repo = Repository(self.server.mongo_uri)
        try:
            old = repo.get(e['id'])
            newer = copy.deepcopy(old); newer['content'] = '另一设备'
            repo.replace(newer)
            old['content'] = '不应覆盖'
            with self.assertRaises(ApiError): repo.replace(old)
            self.assertEqual(repo.get(e['id'])['content'], '另一设备')
        finally: repo.client.close()

class MigrationTests(unittest.TestCase):
    def test_real_snapshot_and_refuse_overwrite(self):
        snapshot = os.environ.get('MEMORY_MIGRATION_FIXTURE')
        if not snapshot: self.skipTest('Set MEMORY_MIGRATION_FIXTURE to an offline snapshot directory')
        source = Path(snapshot)
        server = TestServer(source)
        repo = Repository(server.mongo_uri)
        try:
            before = hashlib.sha256((source/'preflight.db').read_bytes()).hexdigest()
            result = migrate(source/'preflight.db', source/'uploads', repo)
            self.assertTrue(result['verified'])
            docs, _, counters = read_source(source/'preflight.db')
            self.assertEqual(result['entries'], len(docs))
            with self.assertRaises(RuntimeError): migrate(source/'preflight.db', source/'uploads', repo)
            self.assertEqual(migrate(source/'preflight.db', source/'uploads', repo, True), result)
            # A rollback export preserves data written after migration, not just the old snapshot.
            from scripts.export_sqlite import export
            repo.entries.update_one({'_id': docs[0]['id']}, {'$set': {'content':'迁移后新增修改'}})
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'rollback.db'
                export(repo, source/'preflight.db', output)
                exported, _, _ = read_source(output)
                self.assertEqual(exported[0]['content'], '迁移后新增修改')
            self.assertGreater(repo.next_id('entries'), counters['entries'])
            self.assertGreater(repo.next_id('todo_items'), counters['todo_items'])
            self.assertEqual(hashlib.sha256((source/'preflight.db').read_bytes()).hexdigest(), before)
        finally: repo.client.close()



class AttachmentRecoveryTests(unittest.TestCase):
    def test_failed_acknowledgements_are_retryable(self):
        from unittest.mock import patch
        from pymongo.errors import AutoReconnect
        import memory.attachments as module
        from memory.attachments import AttachmentService
        from memory.entries import EntryService
        with tempfile.TemporaryDirectory() as directory:
            server = TestServer(Path(directory))
            repo = Repository(server.mongo_uri)
            repo.initialize()
            entries = EntryService(repo, lambda names: attachments.unlink(names))
            attachments = AttachmentService(repo, entries)
            try:
                with patch.object(module, 'UPLOAD_DIR', Path(directory)):
                    task = attachments.create_upload({'filename':'sample.txt','size':4})
                    uid = task['upload_id']
                    with patch.object(repo.uploads, 'update_one', side_effect=AutoReconnect('test outage')):
                        with self.assertRaises(AutoReconnect): attachments.append(uid, 0, b'data')
                    self.assertEqual(attachments.append(uid, 0, b'data')[0], 200)
                    insert = repo.entries.insert_one
                    def lost_reply(doc):
                        insert(doc)
                        raise AutoReconnect('acknowledgement lost')
                    with patch.object(repo.entries, 'insert_one', side_effect=lost_reply):
                        with self.assertRaises(AutoReconnect): attachments.complete(uid)
                    entry = attachments.complete(uid)
                    self.assertEqual(attachments.complete(uid)['id'], entry['id'])
                    self.assertEqual(repo.entries.count_documents({}), 1)
                    self.assertEqual((Path(directory)/uid).read_bytes(), b'data')
            finally: repo.client.close()

    def test_http_database_outage_returns_503(self):
        from pymongo.errors import AutoReconnect
        from http.server import ThreadingHTTPServer
        from server import ClipboardHandler
        import threading
        import urllib.request
        import urllib.error
        class Down:
            def command(self, *args): raise AutoReconnect('outage')
            def list_entries(self, *args): raise AutoReconnect('outage')
        http = ThreadingHTTPServer(('127.0.0.1',0), ClipboardHandler)
        repo = Down(); repo.db = repo; http.repository = repo
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        try:
            for path in ('/health','/api/entries?q=中文'):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen('http://127.0.0.1:'+str(http.server_port)+urllib.parse.quote(path,safe='/?='))
                self.assertEqual(caught.exception.code, 503)
                caught.exception.close()
        finally: http.shutdown(); http.server_close(); thread.join()

if __name__ == '__main__': unittest.main()
