#!/usr/bin/env python3
"""Partial updates and atomic sorting against an isolated database."""
import tempfile
import unittest
from pathlib import Path
from integration_test import TestServer


class TodoInlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = TestServer(Path(self.temp.name))
        self.server.start()
        self.entry = self.server.json_call('/api/entries', 'POST', {
            'type': 'todo', 'items': ['一', '二', '三']}, 201)['entry']
        self.ids = [item['id'] for item in self.entry['items']]
        self.item_path = f'/api/todo-items/{self.ids[0]}'
        self.order_path = f"/api/entries/{self.entry['id']}/todo-order"

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def read(self):
        return next(e for e in self.server.json_call('/api/entries')['entries']
                    if e['id'] == self.entry['id'])

    def test_partial_update_and_persistent_order(self):
        self.server.json_call(self.item_path, 'PATCH', {'stage': 5})
        result = self.server.json_call(self.item_path, 'PATCH', {'content': ' 新内容 '})
        self.assertEqual(result['item'], {'id': self.ids[0], 'stage': 5, 'content': '新内容'})
        before = self.read()
        ids = list(reversed(self.ids))
        ordered = self.server.json_call(self.order_path, 'PATCH', {'item_ids': ids})['entry']
        self.assertEqual([i['id'] for i in ordered['items']], ids)
        self.assertEqual({i['id']: i for i in before['items']}, {i['id']: i for i in ordered['items']})
        self.assertEqual(ordered['updated_at'], self.entry['updated_at'])
        self.server.stop()
        self.server.start()
        self.assertEqual(self.read(), ordered)

    def test_content_validation_and_utf8_limit(self):
        for payload in ({}, {'content': ''}, {'content': '  '}, {'content': 1},
                        {'stage': True}, {'stage': 6}, {'unknown': 1}):
            self.server.json_call(self.item_path, 'PATCH', payload, 400)
        self.server.json_call(self.item_path, 'PATCH', {'content': 'a' * (200 * 1024 - 6)})
        self.server.json_call(self.item_path, 'PATCH', {'content': 'a' * (200 * 1024 - 5)}, 413)
        self.server.json_call(self.item_path, 'PATCH', {'content': '中' * 70000}, 413)
        self.server.json_call('/api/todo-items/999999', 'PATCH', {'content': 'x'}, 404)

    def test_concurrent_writes_preserve_fields_and_total_limit(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.server.json_call, self.item_path, 'PATCH', payload)
                       for payload in ({'content': 'concurrent'}, {'stage': 4})]
            for future in futures:
                future.result()
        item = self.read()['items'][0]
        self.assertEqual((item['content'], item['stage']), ('concurrent', 4))
        def large_write(item_id):
            import urllib.request
            import urllib.error
            import json
            request = urllib.request.Request(
                self.server.base + f'/api/todo-items/{item_id}',
                data=json.dumps({'content': 'x' * 110000}).encode(),
                method='PATCH', headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                with error:
                    return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(large_write, self.ids[:2]))
        self.assertEqual(sorted(statuses), [200, 413])
        self.assertLessEqual(sum(len(i['content'].encode()) for i in self.read()['items']), 200 * 1024)

    def test_invalid_sort_is_atomic(self):
        for ids in ([], [True], ['1'], [self.ids[0]] * 3):
            self.server.json_call(self.order_path, 'PATCH', {'item_ids': ids}, 400)
        other = self.server.json_call('/api/entries', 'POST', {
            'type': 'todo', 'items': ['other']}, 201)['entry']
        for ids in (self.ids[:-1], self.ids + [999999],
                    [other['items'][0]['id']] + self.ids[1:]):
            self.server.json_call(self.order_path, 'PATCH', {'item_ids': ids}, 409)
            self.assertEqual(self.read(), self.entry)
        self.server.json_call(f"/api/entries/{self.entry['id']}", 'PATCH', {'items': [
            {'id': item['id'], 'content': item['content'], 'stage': item['stage']}
            for item in self.entry['items'][:-1]]})
        self.server.json_call(self.order_path, 'PATCH', {'item_ids': self.ids}, 409)
        text = self.server.json_call('/api/entries', 'POST', {'content': 'text'}, 201)['entry']
        self.server.json_call(f"/api/entries/{text['id']}/todo-order", 'PATCH', {'item_ids': self.ids}, 400)
        self.server.json_call('/api/entries/999999/todo-order', 'PATCH', {'item_ids': self.ids}, 404)


if __name__ == '__main__':
    unittest.main()
