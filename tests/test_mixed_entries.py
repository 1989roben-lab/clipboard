"""Multi-image entries against isolated MongoDB databases and temporary files."""
import os
import sqlite3
import tempfile
import unittest
import uuid
import urllib.parse
from pathlib import Path

from integration_test import TestServer, decoded_images


class MixedEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = TestServer(Path(self.temp.name))
        self.server.start()
        self.image = decoded_images()['test.png'][1]

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def stage(self, name):
        token = uuid.uuid4().hex
        data, _ = self.server.call('/api/draft-images/' + token, 'PUT', self.image,
            {'Content-Type': 'image/png', 'X-Filename': urllib.parse.quote(name)})
        self.assertIn(token, data.decode())
        return token

    def test_create_edit_search_and_remove_images(self):
        first, second = self.stage('第一张.png'), self.stage('第二张.png')
        payload = {'content': '开头\n中间\n结尾', 'images': [
            {'id': first, 'position': 3}, {'id': second, 'position': 6}],
            'client_key': uuid.uuid4().hex}
        entry = self.server.json_call('/api/entries', 'POST', payload, 201)['entry']
        self.assertEqual(len(entry['images']), 2)
        self.assertEqual(self.server.json_call('/api/entries', 'POST', payload, 201)['entry']['id'], entry['id'])
        for token in (first, second):
            data, _ = self.server.call(f'/api/entries/{entry["id"]}/images/{token}')
            self.assertEqual(data, self.image)
        results = self.server.json_call('/api/entries?q=' + urllib.parse.quote('第二张'))['entries']
        self.assertEqual([item['id'] for item in results], [entry['id']])
        from memory.storage import Repository
        from scripts.export_sqlite import export
        template = Path(self.temp.name) / 'v42.db'
        with sqlite3.connect(template) as connection:
            connection.execute('CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT)')
            connection.execute('CREATE TABLE todo_items (id INTEGER)')
            connection.execute('CREATE TABLE file_uploads (id INTEGER)')
        repository = Repository(self.server.mongo_uri)
        try:
            with self.assertRaisesRegex(RuntimeError, 'multi-image'):
                export(repository, template, Path(self.temp.name) / 'unsupported.db')
        finally:
            repository.client.close()
        update = {'content': '新文字', 'images': [{'id': second, 'position': 3}],
            'expected_revision': entry['revision']}
        changed = self.server.json_call(f'/api/entries/{entry["id"]}', 'PATCH', update)['entry']
        self.assertEqual(len(changed['images']), 1)
        self.assertEqual(changed['images'][0]['id'], second)
        self.server.call(f'/api/entries/{entry["id"]}/images/{first}', expected=404)
        self.assertEqual(len(list((Path(self.temp.name) / 'uploads').iterdir())), 1)
        retry = self.server.json_call(f'/api/entries/{entry["id"]}', 'PATCH', update)['entry']
        self.assertEqual(retry['revision'], changed['revision'])
        self.server.json_call(f'/api/entries/{entry["id"]}', 'PATCH',
            {**update, 'content': '冲突内容'}, 409)
        self.server.stop()
        self.server.start()
        recovered = self.server.json_call('/api/entries')['entries'][0]
        self.assertEqual(recovered['images'], changed['images'])
        self.server.json_call(f'/api/entries/{entry["id"]}', 'DELETE')
        self.assertEqual(len(list((Path(self.temp.name) / 'uploads').iterdir())), 0)

    def test_legacy_image_and_text_can_be_edited(self):
        old = self.server.upload('old.png', 'image/png', self.image, content='前后', image_position=1)['entry']
        self.assertEqual(old['images'][0]['id'], 'legacy')
        token = self.stage('new.png')
        changed = self.server.json_call(f'/api/entries/{old["id"]}', 'PATCH', {
            'content': '前中后', 'images': [{'id': 'legacy', 'position': 1}, {'id': token, 'position': 2}],
            'expected_revision': old['revision']})['entry']
        self.assertEqual(len(changed['images']), 2)
        self.assertEqual(self.server.call(f'/api/images/{old["id"]}')[0], self.image)
        text = self.server.json_call('/api/entries', 'POST', {'content': '纯文字'}, 201)['entry']
        token2 = self.stage('text.png')
        converted = self.server.json_call(f'/api/entries/{text["id"]}', 'PATCH', {
            'content': '纯文字', 'images': [{'id': token2, 'position': 3}],
            'expected_revision': text['revision']})['entry']
        self.assertEqual(converted['type'], 'image')
        self.assertEqual(len(converted['images']), 1)
        self.server.json_call('/api/entries', 'DELETE')
        self.assertEqual(len(list((Path(self.temp.name) / 'uploads').iterdir())), 0)

    def test_retention_removes_every_image_of_evicted_entry(self):
        first, second = self.stage('retention-a.png'), self.stage('retention-b.png')
        entry = self.server.json_call('/api/entries', 'POST', {
            'content': '将被淘汰', 'images': [{'id': first, 'position': 0}, {'id': second, 'position': 4}],
            'client_key': uuid.uuid4().hex}, 201)['entry']
        for number in range(100):
            self.server.json_call('/api/entries', 'POST', {'content': f'后续记录 {number}'}, 201)
        self.assertEqual(len(self.server.json_call('/api/entries')['entries']), 100)
        self.server.call(f'/api/entries/{entry["id"]}/images/{first}', expected=404)
        self.assertEqual(len(list((Path(self.temp.name) / 'uploads').iterdir())), 0)


if __name__ == '__main__':
    unittest.main()
