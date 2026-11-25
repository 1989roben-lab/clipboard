#!/usr/bin/env python3
"""Export the latest MongoDB state for a data-preserving rollback to Memory v42."""
import argparse
import shutil
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memory.storage import Repository

def export(repository, template, destination):
    target = Path(destination)
    if target.exists(): raise RuntimeError('Export destination already exists')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, target)
    c = sqlite3.connect(target)
    try:
        with c:
            for table in ('todo_items', 'entries', 'file_uploads'):
                c.execute('DELETE FROM '+table)
            c.execute('DELETE FROM sqlite_sequence')
            columns = [r[1] for r in c.execute('PRAGMA table_info(entries)')]
            for d in repository.entries.find({}).sort('_id', 1):
                if len(d.get('images', [])) > 1:
                    raise RuntimeError('Cannot export a multi-image entry to the v42 SQLite format')
                c.execute('INSERT INTO entries ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', [d.get(k) for k in columns])
                for position, item in enumerate(d.get('items', [])):
                    c.execute('INSERT INTO todo_items (id,entry_id,content,stage,position,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                              (item['id'], d['id'], item['content'], item['stage'], position, item['created_at'], item['updated_at']))
            columns = [r[1] for r in c.execute('PRAGMA table_info(file_uploads)')]
            for d in repository.uploads.find({}):
                c.execute('INSERT INTO file_uploads ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', [d.get(k) for k in columns])
            for name in ('entries', 'todo_items'):
                d = repository.counters.find_one({'_id': name})
                c.execute('DELETE FROM sqlite_sequence WHERE name=?', (name,))
                c.execute('INSERT INTO sqlite_sequence (name,seq) VALUES (?,?)', (name, d['value'] if d else 0))
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise RuntimeError('SQLite integrity check failed')
    finally: c.close()

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--template',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    repo=Repository()
    try: export(repo,a.template,a.output);print('Latest MongoDB state exported to SQLite')
    finally:repo.client.close()
