#!/usr/bin/env python3
"""Explicit offline migration; never overwrite a populated MongoDB target."""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memory.storage import Repository

def read_source(path):
    c = sqlite3.connect('file:' + str(Path(path).resolve()) + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    documents = []
    for row in c.execute('SELECT * FROM entries ORDER BY id'):
        raw = dict(row)
        d = dict(entry_type='text', filename=None, stored_name=None, mime_type=None, size_bytes=None, sha256=None, image_position=None, pinned_at=None, updated_at=raw['created_at'])
        d.update(raw)
        d.update(_id=d['id'], _rev=0)
        if d['entry_type'] == 'todo':
            d['items'] = []
            for item in c.execute('SELECT * FROM todo_items WHERE entry_id = ? ORDER BY position, id', (d['id'],)):
                i = dict(item)
                i.pop('entry_id'); i.pop('position')
                d['items'].append(i)
        documents.append(d)
    uploads = [dict(r) for r in c.execute('SELECT * FROM file_uploads ORDER BY upload_id')] if 'file_uploads' in tables else []
    for d in uploads:
        d['_id'] = d['upload_id']
    seq = dict(c.execute('SELECT name, seq FROM sqlite_sequence'))
    counters = {'entries': max(seq.get('entries', 0), max((d['id'] for d in documents), default=0)),
                'todo_items': max(seq.get('todo_items', 0), max((i['id'] for d in documents for i in d.get('items', [])), default=0))}
    c.close()
    return documents, uploads, counters

def manifest(documents, uploads, folder):
    result = {}
    for d in documents + uploads:
        name = d.get('stored_name')
        if not name: continue
        if Path(name).name != name: raise RuntimeError('Unsafe attachment path')
        p = Path(folder) / name
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        size = p.stat().st_size
        if d.get('sha256') and digest != d['sha256']: raise RuntimeError('Attachment hash mismatch')
        if d.get('size_bytes') is not None and size != d['size_bytes']: raise RuntimeError('Attachment size mismatch')
        if 'received_size' in d and size != d['received_size']: raise RuntimeError('Incomplete upload metadata mismatch')
        result[name] = {'size': size, 'sha256': digest}
    return result

def migrate(path, uploads_dir, repository, verify_only=False):
    documents, uploads, counters = read_source(path)
    files = manifest(documents, uploads, uploads_dir)
    if not verify_only:
        if repository.db.list_collection_names():
            raise RuntimeError('Target database is not empty; refusing to overwrite. Use --verify-only to compare.')
        # Mark first, so an interrupted migration also refuses a blind rerun.
        repository.db.migrations.insert_one({'_id': 'sqlite-v43', 'status': 'running'})
        if documents: repository.entries.insert_many(documents)
        if uploads: repository.uploads.insert_many(uploads)
        repository.counters.insert_many([{'_id': k, 'value': v} for k, v in counters.items()])
    actual = list(repository.entries.find({}).sort('_id', 1))
    actual_uploads = list(repository.uploads.find({}).sort('_id', 1))
    if actual != documents or actual_uploads != uploads:
        raise RuntimeError('MongoDB documents differ from SQLite source')
    if any(repository.counters.find_one({'_id': k})['value'] != v for k, v in counters.items()):
        raise RuntimeError('ID counter mismatch')
    if not verify_only:
        repository.initialize()
        repository.db.migrations.update_one({'_id': 'sqlite-v43'}, {'$set': {'status': 'complete', 'files': files}})
    return {'entries': len(documents), 'todo_items': sum(len(d.get('items', [])) for d in documents),
            'uploads': len(uploads), 'files': len(files), 'attachment_bytes': sum(f['size'] for f in files.values()), 'verified': True}

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--sqlite', required=True)
    p.add_argument('--uploads', required=True)
    p.add_argument('--verify-only', action='store_true')
    args = p.parse_args()
    repo = Repository()
    try:
        print(json.dumps(migrate(args.sqlite, args.uploads, repo, args.verify_only)))
    finally:
        repo.client.close()
