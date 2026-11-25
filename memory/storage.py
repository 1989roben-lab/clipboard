"""MongoDB documents, stable numeric IDs, and atomic entry updates."""
import os
import re
import threading
from datetime import datetime, timezone
from pymongo import MongoClient, ReturnDocument

MAX_ENTRIES = 100
LOCK = threading.RLock()  # One application worker; serializes retention and file operations.

class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

def entry_images(doc):
    if 'images' in doc:
        return doc['images']
    if doc.get('entry_type') == 'image' and doc.get('stored_name'):
        return [dict(id='legacy', position=doc.get('image_position') or 0,
                     stored_name=doc['stored_name'], filename=doc.get('filename'),
                     mime_type=doc.get('mime_type'), size_bytes=doc.get('size_bytes'),
                     sha256=doc.get('sha256'))]
    return []

def stored_names(doc):
    names = {image['stored_name'] for image in entry_images(doc) if image.get('stored_name')}
    if doc.get('stored_name'):
        names.add(doc['stored_name'])
    return names

def public_entry(doc):
    keys = ('id', 'content', 'filename', 'mime_type', 'size_bytes', 'image_position', 'created_at', 'updated_at')
    result = {k: doc.get(k) for k in keys}
    result.update(type=doc['entry_type'], pinned=doc.get('pinned_at') is not None)
    result['revision'] = doc.get('_rev', 0)
    result['images'] = [{k: image.get(k) for k in ('id', 'position', 'filename', 'mime_type', 'size_bytes')}
                        for image in entry_images(doc)]
    if doc['entry_type'] == 'todo':
        result['items'] = [{k: item[k] for k in ('id', 'content', 'stage')} for item in doc['items']]
    return result

class Repository:
    def __init__(self, uri=None):
        uri = uri or os.environ.get('MONGO_URI')
        if not uri:
            raise RuntimeError('MONGO_URI is required; SQLite fallback is disabled')
        self.client = MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=10000)
        self.db = self.client.get_default_database()
        self.entries = self.db.entries
        self.uploads = self.db.file_uploads
        self.counters = self.db.counters
        self.drafts = self.db.draft_images
        self.lock = LOCK

    def initialize(self):
        self.db.command('ping')
        self.entries.create_index([('pinned_at', -1), ('updated_at', -1), ('id', -1)])
        self.entries.create_index('items.id')
        self.entries.create_index('client_key', unique=True, sparse=True)
        self.uploads.create_index('created_at')
        self.drafts.create_index('created_at')

    def next_id(self, name):
        return self.counters.find_one_and_update({'_id': name}, {'$inc': {'value': 1}}, upsert=True, return_document=ReturnDocument.AFTER)['value']

    def get(self, entry_id):
        doc = self.entries.find_one({'_id': entry_id})
        if doc is None:
            raise ApiError(404, '这条记录不存在')
        return doc

    def item(self, item_id, missing=404):
        doc = self.entries.find_one({'items.id': item_id})
        if doc is None:
            raise ApiError(missing, '这个待办项目不存在或已变化，请刷新后重试')
        return doc, next(i for i in doc['items'] if i['id'] == item_id)

    def replace(self, doc):
        rev = doc.get('_rev', 0)
        doc['_rev'] = rev + 1
        query = {'_id': doc['_id'], '_rev': rev}
        if self.entries.replace_one(query, doc).matched_count != 1:
            raise ApiError(409, '内容已在别处修改，请刷新后重试')
        return doc

    def new_entry(self, content='', entry_type='text', **fields):
        stamp = now()
        doc = dict(id=self.next_id('entries'), content=content, entry_type=entry_type,
                   created_at=stamp, updated_at=stamp, pinned_at=None, filename=None,
                   stored_name=None, mime_type=None, size_bytes=None, sha256=None,
                   image_position=None, _rev=0)
        doc.update(fields)
        doc['_id'] = doc['id']
        return doc

    def insert(self, doc):
        self.entries.insert_one(doc)
        return doc

    def prune(self, protected_id):
        overflow = self.entries.count_documents({}) - MAX_ENTRIES
        if overflow <= 0:
            return []
        docs = list(self.entries.find({'_id': {'$ne': protected_id}}))
        docs.sort(key=lambda d: (d.get('pinned_at') is not None,
                                 d.get('pinned_at') or d['updated_at'], d['id']))
        removed = docs[:overflow]
        self.entries.delete_many({'_id': {'$in': [d['_id'] for d in removed]}})
        return list({name for doc in removed for name in stored_names(doc)})

    def list_entries(self, query=''):
        query = query.strip()
        if len(query) > 200:
            raise ApiError(400, '搜索关键词不能超过 200 个字符')
        filters = []
        for term in query.split():
            regex = {'$regex': re.escape(term), '$options': 'i'}
            filters.append({'$or': [{field: regex} for field in ('content', 'filename', 'images.filename', 'items.content')]})
        where = {'$and': filters} if filters else {}
        docs = self.entries.find(where).sort([('pinned_at', -1), ('updated_at', -1), ('id', -1)]).limit(MAX_ENTRIES)
        return [public_entry(d) for d in docs]
