#!/usr/bin/env python3
"""Memory HTTP API; persistence and attachments are separate services."""
from __future__ import annotations
import json
import os
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, parse_qs
from pymongo.errors import PyMongoError
from memory.config import *
from memory.storage import Repository, ApiError, entry_images
from memory.entries import EntryService, require
from memory.attachments import AttachmentService, detect_image_type, clean_filename

class ClipboardHandler(BaseHTTPRequestHandler):
    server_version = "Memory/45"

    def dispatch(self, method):
        try:
            if method == 'GET':
                self.get()
            else:
                # Only one Memory worker is supported. CAS revisions additionally reject stale document writes.
                with self.server.repository.lock:
                    self.mutate(method)
        except ApiError as error:
            self.send_json(error.status, {'error': error.message})
        except PyMongoError:
            self.send_json(503, {'error': '数据库暂时无法连接，请稍后重试'})
        except OSError:
            self.send_json(503, {'error': '文件存储暂时不可用，请稍后重试'})

    def do_GET(self): self.dispatch('GET')
    def do_POST(self): self.dispatch('POST')
    def do_PUT(self): self.dispatch('PUT')
    def do_PATCH(self): self.dispatch('PATCH')
    def do_DELETE(self): self.dispatch('DELETE')

    def get(self):
        url = urlsplit(self.path)
        path = url.path
        if path == '/':
            self.send_bytes(200, STATIC_PATH.read_bytes(), 'text/html; charset=utf-8', cache_control='no-store, max-age=0')
            return
        if path in STATIC_ROUTES:
            file, content_type, cache = STATIC_ROUTES[path]
            self.send_bytes(200, file.read_bytes(), content_type, cache_control=cache)
            return
        if path == '/health':
            self.server.repository.db.command('ping')
            require(UPLOAD_DIR.is_dir() and os.access(UPLOAD_DIR, os.R_OK | os.W_OK), '附件目录不可用', 503)
            self.send_json(200, {'status': 'ok'})
            return
        if path == '/api/entries':
            q = parse_qs(url.query, keep_blank_values=True).get('q', [''])[0]
            self.send_json(200, {'entries': self.server.repository.list_entries(q)})
            return
        image_item = IMAGE_ITEM_PATH.fullmatch(path)
        if image_item:
            doc = self.server.repository.entries.find_one({'_id': int(image_item[1])})
            image = next((i for i in entry_images(doc) if i['id'] == image_item[2]), None) if doc else None
            require(image is not None, '图片不存在', 404)
            self.send_stored_upload(image, bool(image_item[3]))
            return
        for regex, kind in [(IMAGE_PATH, 'image'), (FILE_PATH, 'file')]:
            match = regex.fullmatch(path)
            if match:
                self.send_upload(int(match[1]), kind, kind == 'file' or bool(match[2]))
                return
        raise ApiError(404, '未找到该页面')

    def mutate(self, method):
        path = self.path
        service, attachments = self.server.entries, self.server.attachments
        draft = DRAFT_IMAGE_PATH.fullmatch(path)
        if draft and method == 'PUT':
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ApiError(400, '请求长度无效')
            require(0 < length <= MAX_IMAGE_BYTES, '单张图片不能超过 10 MB', 413)
            data = self.rfile.read(length)
            require(len(data) == length, '图片上传不完整')
            result = attachments.stage_image(draft[1], data, self.headers.get('X-Filename', ''),
                self.headers.get('Content-Type', '').split(';')[0])
            self.send_json(200, result)
            return
        if method == 'POST' and path == '/api/images':
            self.create_image_entry()
            return
        upload = FILE_UPLOAD_PATH.fullmatch(path)
        if upload and method == 'DELETE':
            self.send_json(200, attachments.cancel(upload[1])); return
        if upload and method == 'POST' and not upload[2]:
            try:
                length = int(self.headers.get('Content-Length', '0'))
                offset = int(self.headers.get('X-Upload-Offset', '-1'))
            except ValueError: raise ApiError(400, '附件分片参数无效')
            require(length > 0, '附件分片不能为空')
            require(length <= MAX_FILE_CHUNK_BYTES, '附件分片不能超过 8 MB', 413)
            data = self.rfile.read(length)
            require(len(data) == length, '附件分片上传不完整')
            status, result = attachments.append(upload[1], offset, data)
            self.send_json(status, result); return
        if upload and method == 'POST' and upload[2]:
            self.send_json(201, {'entry': attachments.complete(upload[1])}); return
        if method == 'DELETE' and path == '/api/entries':
            self.send_json(200, service.clear()); return
        entry = ENTRY_PATH.fullmatch(path)
        if method == 'DELETE' and entry:
            self.send_json(200, service.delete(int(entry[1]))); return
        if method not in ('POST', 'PATCH'): raise ApiError(404, '未找到该接口')
        known = (method == 'POST' and path in ('/api/entries', '/api/file-uploads')) or (method == 'PATCH' and (entry or TODO_ORDER_PATH.fullmatch(path) or TODO_ITEM_PATH.fullmatch(path) or TODO_STRUCTURE_PATH.fullmatch(path)))
        require(bool(known), '未找到该接口', 404)
        payload = self.read_json_body()
        if payload is None: return
        if method == 'POST' and path == '/api/entries':
            result = attachments.save_body(None, payload) if 'images' in payload else service.create(payload)
            self.send_json(201, {'entry': result}); return
        if method == 'POST' and path == '/api/file-uploads':
            self.send_json(201, attachments.create_upload(payload)); return
        if entry:
            result = attachments.save_body(int(entry[1]), payload) if 'images' in payload else service.update(int(entry[1]), payload)
            self.send_json(200, {'entry': result}); return
        for regex, action in [(TODO_ORDER_PATH, service.reorder), (TODO_STRUCTURE_PATH, service.structure), (TODO_ITEM_PATH, service.update_item)]:
            match = regex.fullmatch(path)
            if match:
                result = action(int(match[1]), payload)
                self.send_json(200, {'entry': result} if regex == TODO_ORDER_PATH else result)
                return
        raise ApiError(404, '未找到该接口')

    def create_image_entry(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "请求长度无效"},
            )
            return

        if content_length <= 0:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "图片不能为空"},
            )
            return
        if content_length > MAX_IMAGE_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "单张图片不能超过 10 MB"},
            )
            return

        data = self.rfile.read(content_length)
        if len(data) != content_length:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "图片上传不完整"},
            )
            return

        detected_type = detect_image_type(data)
        if detected_type not in IMAGE_FORMATS:
            self.send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "仅支持 PNG、JPEG、WebP 和 GIF 图片"},
            )
            return

        suffix, allowed_suffixes = IMAGE_FORMATS[detected_type]
        filename = clean_filename(
            self.headers.get("X-Filename", ""),
            f"image{suffix}",
        )
        filename_suffix = Path(filename).suffix.lower()
        if filename_suffix not in allowed_suffixes:
            self.send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "文件扩展名与实际图片格式不一致"},
            )
            return

        claimed_type = self.headers.get("Content-Type", "").split(";")[0]
        if (
            claimed_type
            and claimed_type != "application/octet-stream"
            and claimed_type != detected_type
        ):
            self.send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "图片类型与实际内容不一致"},
            )
            return

        content = unquote(self.headers.get("X-Content", ""))
        if len(content.encode("utf-8")) > MAX_IMAGE_CONTENT_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "图片附带文字不能超过 12 KB"},
            )
            return

        raw_image_position = self.headers.get("X-Image-Position", "0")
        try:
            image_position = int(raw_image_position)
        except ValueError:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "图片位置无效"},
            )
            return
        if image_position < 0 or image_position > len(content):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "图片位置超出文字范围"},
            )
            return

        entry = self.server.attachments.create_image(data, filename, detected_type, content, image_position, suffix)
        self.send_json(HTTPStatus.CREATED, {"entry": entry})

    def send_upload(
        self,
        entry_id: int,
        entry_type: str,
        download: bool,
    ) -> None:
        row = self.server.repository.entries.find_one({'_id': entry_id, 'entry_type': entry_type})
        if row is None:
            self.send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "文件不存在"},
            )
            return

        self.send_stored_upload(row, download)

    def send_stored_upload(self, row, download):

        path = self.server.attachments.path(row["stored_name"])
        if path is None or not path.is_file():
            self.send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "文件不存在"},
            )
            return

        disposition = "attachment" if download else "inline"
        encoded_filename = quote(
            row["filename"],
            safe="",
        )
        try:
            file_size = path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                row["mime_type"] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(file_size))
            self.send_header(
                "Content-Disposition",
                f"{disposition}; filename*=UTF-8''{encoded_filename}",
            )
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            with path.open("rb") as upload_file:
                while chunk := upload_file.read(1024 * 1024):
                    self.wfile.write(chunk)
        except OSError:
            return

    def read_json_body(self) -> dict[str, Any] | None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "请求长度无效"},
            )
            return None

        if content_length <= 0:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "请求内容不能为空"},
            )
            return None
        if content_length > MAX_JSON_REQUEST_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "请求内容过大"},
            )
            return None

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "请求格式无效"},
            )
            return None

        if not isinstance(payload, dict):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "请求格式无效"},
            )
            return None
        return payload

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src 'self' data: blob:; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            f'{self.address_string()} - [{self.log_date_time_string()}] '
            f"{fmt % args}",
            flush=True,
        )

def main():
    repository = Repository()
    repository.initialize()
    server = ThreadingHTTPServer((HOST, PORT), ClipboardHandler)
    server.repository = repository
    server.entries = EntryService(repository, lambda names: server.attachments.unlink(names))
    server.attachments = AttachmentService(repository, server.entries)
    with repository.lock:
        server.attachments.reconcile()
    print(f"Memory v45 listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        repository.client.close()

if __name__ == '__main__':
    main()
