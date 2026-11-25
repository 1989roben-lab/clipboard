"""Entry and todo operations. Each todo list is one atomic MongoDB document."""
from .storage import ApiError, now, public_entry, stored_names
MAX_CONTENT_BYTES = 200 * 1024
MAX_NOTE_BYTES = 12 * 1024

def require(condition, message, status=400):
    if not condition:
        raise ApiError(status, message)

def check_total(items):
    require(sum(len(i['content'].encode('utf-8')) for i in items) <= MAX_CONTENT_BYTES, '待办项目合计不能超过 200 KB', 413)

def todo(doc):
    require(doc['entry_type'] == 'todo', '只能操作待办清单')

class EntryService:
    def __init__(self, repository, unlink):
        self.repo, self.unlink = repository, unlink

    def insert(self, doc):
        self.repo.insert(doc)
        self.unlink(self.repo.prune(doc['id']))
        return public_entry(doc)

    def create(self, payload):
        kind = payload.get('type', 'text')
        require(kind in ('text', 'todo'), '不支持的记录类型')
        if kind == 'text':
            content = payload.get('content')
            require(isinstance(content, str), '内容必须是文本')
            require(bool(content.strip()), '请输入要保存的内容')
            require(len(content.encode()) <= MAX_CONTENT_BYTES, '单条内容不能超过 200 KB', 413)
            return self.insert(self.repo.new_entry(content=content))
        raw = payload.get('items')
        require(isinstance(raw, list) and bool(raw), '请至少添加一个待办项目')
        require(all(isinstance(i, str) and i.strip() for i in raw), '每个待办项目都必须是非空文本')
        items = [dict(content=i.strip(), stage=1) for i in raw]
        check_total(items)
        stamp = now()
        for i in items:
            i.update(id=self.repo.next_id('todo_items'), created_at=stamp, updated_at=stamp)
        return self.insert(self.repo.new_entry(entry_type='todo', items=items))

    def update(self, entry_id, payload):
        doc = self.repo.get(entry_id)
        if 'pinned' in payload:
            require(set(payload) == {'pinned'} and type(payload['pinned']) is bool, '置顶状态必须单独提交，且为布尔值')
            if payload['pinned'] and doc.get('pinned_at') is None:
                doc['pinned_at'] = now()
            elif not payload['pinned']:
                doc['pinned_at'] = None
        elif doc['entry_type'] == 'todo':
            raw = payload.get('items')
            require(isinstance(raw, list) and bool(raw), '待办清单至少需要一个项目')
            old = {i['id']: i for i in doc['items']}
            seen, items = set(), []
            for item in raw:
                require(isinstance(item, dict), '待办项目格式无效')
                content, stage, item_id = item.get('content'), item.get('stage'), item.get('id')
                require(isinstance(content, str) and bool(content.strip()), '待办项目内容不能为空')
                require(type(stage) is int and 1 <= stage <= 5, 'Stage 必须是 1 到 5')
                if item_id is not None:
                    require(type(item_id) is int and item_id in old and item_id not in seen, '待办项目 ID 无效或重复')
                    seen.add(item_id)
                items.append(dict(id=item_id, content=content.strip(), stage=stage))
            check_total(items)
            stamp = now()
            for i in items:
                previous = old.get(i['id'], {})
                i.update(id=i['id'] or self.repo.next_id('todo_items'), created_at=previous.get('created_at', stamp), updated_at=stamp)
            doc.update(items=items, updated_at=stamp)
        else:
            content = payload.get('content')
            require(isinstance(content, str), '内容必须是文本')
            if doc['entry_type'] == 'text':
                require(bool(content.strip()), '请输入要保存的内容')
            limit = MAX_CONTENT_BYTES if doc['entry_type'] == 'text' else MAX_NOTE_BYTES
            require(len(content.encode()) <= limit, '单条内容不能超过 200 KB' if limit == MAX_CONTENT_BYTES else '图片或附件说明不能超过 12 KB', 413)
            doc.update(content=content, updated_at=now())
            if doc['entry_type'] == 'image':
                doc['image_position'] = min(doc.get('image_position') or 0, len(content))
                if 'images' in doc:
                    for image in doc['images']:
                        image['position'] = min(image['position'], len(content))
        return public_entry(self.repo.replace(doc))

    def update_item(self, item_id, payload):
        require(bool(payload) and set(payload) <= {'content', 'stage'}, '请提交内容或 Stage')
        if 'stage' in payload:
            require(type(payload['stage']) is int and 1 <= payload['stage'] <= 5, 'Stage 必须是 1 到 5')
        if 'content' in payload:
            require(isinstance(payload['content'], str) and bool(payload['content'].strip()), '待办项目内容不能为空')
            payload['content'] = payload['content'].strip()
        doc, item = self.repo.item(item_id)
        item.update(payload, updated_at=now())
        check_total(doc['items'])
        self.repo.replace(doc)
        return {'entry_id': doc['id'], 'item': {k: item[k] for k in ('id', 'content', 'stage')}}

    def reorder(self, entry_id, payload):
        ids = payload.get('item_ids')
        require(isinstance(ids, list) and bool(ids) and all(type(i) is int and i > 0 for i in ids) and len(set(ids)) == len(ids), '待办项目 ID 无效或重复')
        doc = self.repo.get(entry_id)
        todo(doc)
        old = {i['id']: i for i in doc['items']}
        require(set(ids) == set(old), '清单项目已变化，请刷新后重试', 409)
        doc['items'] = [old[i] for i in ids]
        return public_entry(self.repo.replace(doc))

    def structure(self, item_id, payload):
        action = payload.get('action')
        require(action in ('split', 'delete', 'insert'), '无效的待办操作')
        parts = [payload.get('before'), payload.get('after')] if action == 'split' else [payload.get('content')] if action == 'insert' else []
        require(all(isinstance(p, str) for p in parts), '待办内容无效')
        parts = [p.strip() for p in parts]
        if action == 'insert':
            require(bool(parts[0]) and payload.get('side') in ('before', 'after'), '待办内容或插入位置无效')
        doc, item = self.repo.item(item_id, missing=409)
        require(action == 'insert' or payload.get('expected') == item['content'], '待办内容已在别处修改，请取消编辑并刷新后重试', 409)
        index = doc['items'].index(item)
        focus = item_id
        if action == 'delete' or (action == 'split' and not any(parts)):
            doc['items'].pop(index)
        elif action == 'split':
            item.update(content=parts[0] or parts[1], updated_at=now())
            if all(parts):
                focus = self.repo.next_id('todo_items')
                doc['items'].insert(index + 1, dict(id=focus, content=parts[1], stage=1, created_at=now(), updated_at=now()))
        else:
            focus = self.repo.next_id('todo_items')
            doc['items'].insert(index + (payload['side'] == 'after'), dict(id=focus, content=parts[0], stage=1, created_at=now(), updated_at=now()))
        check_total(doc['items'])
        if doc['items']:
            result = public_entry(self.repo.replace(doc))
        else:
            require(self.repo.entries.delete_one({'_id': doc['_id'], '_rev': doc['_rev']}).deleted_count == 1, '清单已变化，请刷新后重试', 409)
            result = None
        return {'entry': result, 'focus_id': focus}

    def delete(self, entry_id):
        doc = self.repo.get(entry_id)
        self.repo.entries.delete_one({'_id': entry_id})
        self.unlink(stored_names(doc))
        return {'deleted': 1}

    def clear(self):
        # Remove only files whose metadata was deleted, never unrelated files.
        files = [name for d in self.repo.entries.find({}) for name in stored_names(d)]
        files += [d['stored_name'] for d in self.repo.uploads.find({})]
        files += [d['stored_name'] for d in self.repo.drafts.find({})]
        deleted = self.repo.entries.delete_many({}).deleted_count
        cancelled = self.repo.uploads.delete_many({}).deleted_count
        self.repo.drafts.delete_many({})
        self.unlink(files)
        return {'deleted': deleted, 'cancelled_uploads': cancelled}
