"""Persistent file operations; MongoDB stores metadata, never file bytes."""
import os
import re
import time
import uuid
import hashlib
from pathlib import Path
from urllib.parse import unquote
from .config import *
from .entries import require
from .storage import ApiError, entry_images, public_entry, stored_names, now

def detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if (
        len(data) >= 12
        and data.startswith(b"RIFF")
        and data[8:12] == b"WEBP"
    ):
        return "image/webp"
    return None


def clean_filename(raw_filename: str, default_name: str) -> str:
    filename = Path(unquote(raw_filename)).name
    filename = "".join(
        character
        for character in filename
        if character >= " " and character not in "\r\n"
    ).strip()
    if not filename:
        filename = default_name
    return filename[:180]


def clean_mime_type(raw_mime_type: object) -> str:
    if not isinstance(raw_mime_type, str):
        return "application/octet-stream"
    mime_type = raw_mime_type.split(";", 1)[0].strip().lower()
    if (
        not mime_type
        or len(mime_type) > 120
        or any(character < " " for character in mime_type)
    ):
        return "application/octet-stream"
    return mime_type



class AttachmentService:
    def __init__(self, repository, entries):
        self.repo, self.entries = repository, entries

    def path(self, name):
        if not name or Path(name).name != name:
            raise ApiError(409, '附件文件名无效')
        return UPLOAD_DIR / name

    def unlink(self, names):
        for name in names:
            # Never remove a file still referenced by a committed record or upload.
            if (self.repo.entries.find_one({'$or': [{'stored_name': name}, {'images.stored_name': name}]})
                    or self.repo.uploads.find_one({'stored_name': name})
                    or self.repo.drafts.find_one({'stored_name': name})):
                continue
            try:
                self.path(name).unlink(missing_ok=True)
            except OSError:
                print('Attachment cleanup deferred', flush=True)

    def reconcile(self):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.expire()
        self.expire_drafts()
        for path in UPLOAD_DIR.iterdir():
            if path.is_file() and path.stat().st_mtime < time.time() - FILE_UPLOAD_TTL_SECONDS:
                self.unlink([path.name])

    def expire(self):
        for row in self.repo.uploads.find({'created_at': {'$lt': time.time() - FILE_UPLOAD_TTL_SECONDS}}):
            self.repo.uploads.delete_one({'_id': row['_id']})
            self.unlink([row['stored_name'], row['_id']])

    def expire_drafts(self):
        cutoff = time.time() - FILE_UPLOAD_TTL_SECONDS
        for row in self.repo.drafts.find({'created_at': {'$lt': cutoff}}):
            if self.repo.entries.find_one({'images.id': row['_id']}):
                continue
            self.repo.drafts.delete_one({'_id': row['_id']})
            self.unlink([row['stored_name']])

    def stage_image(self, token, data, filename, claimed_type):
        self.expire_drafts()
        require(0 < len(data) <= MAX_IMAGE_BYTES, '单张图片不能超过 10 MB', 413)
        detected = detect_image_type(data)
        require(detected in IMAGE_FORMATS, '仅支持 PNG、JPEG、WebP 和 GIF 图片', 415)
        require(not claimed_type or claimed_type in (detected, 'application/octet-stream'), '图片类型与实际内容不一致', 415)
        suffix, allowed = IMAGE_FORMATS[detected]
        filename = clean_filename(filename, 'image' + suffix)
        require(Path(filename).suffix.lower() in allowed, '文件扩展名与实际图片格式不一致', 415)
        digest = hashlib.sha256(data).hexdigest()
        committed = self.repo.entries.find_one({'images.id': token})
        if committed:
            image = next(image for image in committed['images'] if image['id'] == token)
            require(image['sha256'] == digest and image['size_bytes'] == len(data), '图片令牌已用于其他文件', 409)
            return {'id': token, 'filename': image['filename'], 'size_bytes': image['size_bytes']}
        existing = self.repo.drafts.find_one({'_id': token})
        if existing:
            require(existing['sha256'] == digest and existing['size_bytes'] == len(data), '图片令牌已用于其他文件', 409)
            if self.path(existing['stored_name']).is_file():
                return {'id': token, 'filename': existing['filename'], 'size_bytes': existing['size_bytes']}
        name = '.draft-' + token + suffix
        temporary = self.path('.upload-' + uuid.uuid4().hex)
        temporary.write_bytes(data)
        os.replace(temporary, self.path(name))
        if existing is None:
            self.repo.drafts.insert_one(dict(_id=token, stored_name=name, filename=filename,
                mime_type=detected, size_bytes=len(data), sha256=digest, created_at=time.time()))
        return {'id': token, 'filename': filename, 'size_bytes': len(data)}

    def save_body(self, entry_id, payload):
        content = payload.get('content')
        raw_images = payload.get('images')
        require(isinstance(content, str), '内容必须是文本')
        require(len(content.encode('utf-8')) <= MAX_CONTENT_BYTES, '单条内容不能超过 200 KB', 413)
        require(isinstance(raw_images, list), '图片列表无效')
        require(bool(content.strip()) or bool(raw_images), '内容不能为空')
        require(len(raw_images) <= 100, '单条记录图片不能超过 100 张', 413)
        doc = self.repo.get(entry_id) if entry_id is not None else None
        if doc:
            require(doc['entry_type'] in ('text', 'image'), '只能编辑文字或图文记录')
            expected = payload.get('expected_revision')
            require(type(expected) is int and expected >= 0, '记录版本无效')
            if expected != doc.get('_rev', 0):
                current = entry_images(doc)
                if (content == doc['content'] and
                    [(i['id'], i['position']) for i in current] ==
                    [(i.get('id'), i.get('position')) for i in raw_images]):
                    for image in current:
                        self.repo.drafts.delete_one({'_id': image['id']})
                    return public_entry(doc)
                raise ApiError(409, '记录已在别处修改，请刷新后重试')
        else:
            key = payload.get('client_key')
            require(isinstance(key, str) and re.fullmatch(r'[0-9a-f]{32}', key), '保存令牌无效')
            existing = self.repo.entries.find_one({'client_key': key})
            if existing:
                current = entry_images(existing)
                require(content == existing['content'] and
                    [(i['id'], i['position']) for i in current] ==
                    [(i.get('id'), i.get('position')) for i in raw_images],
                    '这条记忆已保存但内容又发生变化，请刷新后继续编辑', 409)
                for image in current:
                    self.repo.drafts.delete_one({'_id': image['id']})
                return public_entry(existing)
        known = {image['id']: image for image in entry_images(doc)} if doc else {}
        images, seen = [], set()
        for item in raw_images:
            require(isinstance(item, dict), '图片信息无效')
            image_id, position = item.get('id'), item.get('position')
            require(isinstance(image_id, str) and (image_id == 'legacy' or re.fullmatch(r'[0-9a-f]{32}', image_id))
                    and image_id not in seen, '图片 ID 无效或重复')
            require(type(position) is int and 0 <= position <= len(content), '图片位置超出文字范围')
            seen.add(image_id)
            source = known.get(image_id) or self.repo.drafts.find_one({'_id': image_id})
            require(source is not None and self.path(source['stored_name']).is_file(), '图片尚未上传完成', 409)
            images.append({k: source[k] for k in ('stored_name', 'filename', 'mime_type', 'size_bytes', 'sha256')} |
                          {'id': image_id, 'position': position})
        before = stored_names(doc) if doc else set()
        if doc is None:
            doc = self.repo.new_entry(content=content, entry_type='image' if images else 'text', client_key=key)
        else:
            doc['content'] = content
            doc['updated_at'] = now()
        doc['images'] = images
        doc['entry_type'] = 'image' if images else 'text'
        first = images[0] if images else None
        for field in ('stored_name', 'filename', 'mime_type', 'size_bytes', 'sha256'):
            doc[field] = first[field] if first else None
        doc['image_position'] = first['position'] if first else None
        if entry_id is None:
            self.repo.insert(doc)
            self.entries.unlink(self.repo.prune(doc['id']))
        else:
            self.repo.replace(doc)
        for image in images:
            self.repo.drafts.delete_one({'_id': image['id']})
        self.unlink(before - stored_names(doc))
        return public_entry(doc)

    def create_image(self, data, filename, detected_type, content, position, suffix):
        name = uuid.uuid4().hex + suffix
        final = self.path(name)
        temporary = self.path('.upload-' + uuid.uuid4().hex)
        temporary.write_bytes(data)
        os.replace(temporary, final)
        doc = self.repo.new_entry(content=content, entry_type='image', filename=filename, stored_name=name,
            mime_type=detected_type, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), image_position=position)
        # Leave the complete file if MongoDB's acknowledgement is ambiguous; reconciliation handles orphans.
        return self.entries.insert(doc)

    def create_upload(self, payload):
        filename, total, content = payload.get('filename'), payload.get('size'), payload.get('content', '')
        require(isinstance(filename, str), '附件名称无效')
        require(type(total) is int and total >= 0, '附件大小无效')
        require(total <= MAX_FILE_BYTES, '单个附件不能超过 100 MB', 413)
        require(isinstance(content, str), '附件说明必须是文本')
        require(len(content.encode()) <= MAX_IMAGE_CONTENT_BYTES, '附件说明不能超过 12 KB', 413)
        self.expire()
        upload_id = uuid.uuid4().hex
        name = '.upload-' + upload_id
        self.path(name).touch(exist_ok=False)
        self.repo.uploads.insert_one(dict(_id=upload_id, upload_id=upload_id, stored_name=name,
            filename=clean_filename(filename, '附件'), mime_type=clean_mime_type(payload.get('mime_type')),
            content=content, total_size=total, received_size=0, created_at=time.time()))
        return {'upload_id': upload_id, 'received': 0, 'total': total, 'chunk_size': MAX_FILE_CHUNK_BYTES}

    def append(self, upload_id, offset, data):
        row = self.repo.uploads.find_one({'_id': upload_id})
        require(row is not None, '附件上传任务不存在或已过期', 404)
        if offset != row['received_size']:
            return 409, {'error': '附件分片位置不一致', 'received': row['received_size']}
        require(not row.get('entry_id'), '附件正在完成，请重试完成操作', 409)
        received = offset + len(data)
        require(received <= row['total_size'], '附件内容超过声明大小', 413)
        path = self.path(row['stored_name'])
        require(path.is_file() and path.stat().st_size >= offset, '附件临时文件状态不一致，请重新选择', 409)
        # A previous failed DB acknowledgement may have left extra bytes; a retry safely rewrites the same offset.
        with path.open('r+b') as f:
            f.truncate(offset)
            f.seek(offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.repo.uploads.update_one({'_id': upload_id}, {'$set': {'received_size': received}})
        return 200, {'received': received}

    def complete(self, upload_id):
        row = self.repo.uploads.find_one({'_id': upload_id})
        # Completing twice after a lost HTTP response returns the same committed entry.
        if row is None:
            doc = self.repo.entries.find_one({'upload_id': upload_id})
            require(doc is not None, '附件上传任务不存在或已过期', 404)
            return public_entry(doc)
        if row['received_size'] != row['total_size']:
            raise ApiError(409, '附件尚未上传完整')
        source, final = self.path(row['stored_name']), self.path(upload_id)
        if not row.get('entry_id'):
            require(source.is_file() and source.stat().st_size == row['total_size'], '附件临时文件状态不一致，请重新选择', 409)
            row['entry_id'] = self.repo.next_id('entries')
            self.repo.uploads.update_one({'_id': upload_id}, {'$set': {'entry_id': row['entry_id']}})
        if source.exists():
            os.replace(source, final)
        require(final.is_file() and final.stat().st_size == row['total_size'], '附件临时文件状态不一致，请重新选择', 409)
        doc = self.repo.entries.find_one({'_id': row['entry_id']})
        if doc is None:
            doc = self.repo.new_entry(content=row['content'], entry_type='file', filename=row['filename'],
                stored_name=upload_id, mime_type=row['mime_type'], size_bytes=row['total_size'], upload_id=upload_id)
            # The preallocated ID makes a completion retry idempotent. Gaps in the counter are harmless.
            doc.update(_id=row['entry_id'], id=row['entry_id'])
            self.repo.insert(doc)
        self.repo.uploads.delete_one({'_id': upload_id})
        self.unlink(self.repo.prune(doc['id']))
        return public_entry(doc)

    def cancel(self, upload_id):
        row = self.repo.uploads.find_one_and_delete({'_id': upload_id})
        if row:
            self.unlink([row['stored_name'], upload_id])
        return {'cancelled': row is not None}
