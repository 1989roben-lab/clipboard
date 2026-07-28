#!/usr/bin/env python3
"""A tiny LAN clipboard server using only the Python standard library."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote


HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8016"))
DB_PATH = Path(os.environ.get("DB_PATH", "/data/clipboard.db"))
UPLOAD_DIR = DB_PATH.parent / "uploads"
STATIC_PATH = Path(__file__).resolve().parent / "static" / "index.html"
MAX_ENTRIES = 100
MAX_CONTENT_BYTES = 200 * 1024
MAX_IMAGE_CONTENT_BYTES = 12 * 1024
MAX_JSON_REQUEST_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ENTRY_PATH = re.compile(r"^/api/entries/([1-9][0-9]*)$")
IMAGE_PATH = re.compile(
    r"^/api/images/([1-9][0-9]*)(/download)?$"
)
IMAGE_FORMATS = {
    "image/png": (".png", {".png"}),
    "image/jpeg": (".jpg", {".jpg", ".jpeg"}),
    "image/webp": (".webp", {".webp"}),
    "image/gif": (".gif", {".gif"}),
}


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(entries)"
            ).fetchall()
        }
        migrations = {
            "entry_type": (
                "ALTER TABLE entries ADD COLUMN "
                "entry_type TEXT NOT NULL DEFAULT 'text'"
            ),
            "filename": (
                "ALTER TABLE entries ADD COLUMN filename TEXT"
            ),
            "stored_name": (
                "ALTER TABLE entries ADD COLUMN stored_name TEXT"
            ),
            "mime_type": (
                "ALTER TABLE entries ADD COLUMN mime_type TEXT"
            ),
            "size_bytes": (
                "ALTER TABLE entries ADD COLUMN size_bytes INTEGER"
            ),
            "sha256": (
                "ALTER TABLE entries ADD COLUMN sha256 TEXT"
            ),
            "image_position": (
                "ALTER TABLE entries ADD COLUMN image_position INTEGER"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)
        connection.execute(
            """
            UPDATE entries
            SET image_position = 0
            WHERE entry_type = 'image' AND image_position IS NULL
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS entries_created_at_idx
            ON entries(created_at DESC, id DESC)
            """
        )
    reconcile_uploads()


def safe_upload_path(stored_name: str | None) -> Path | None:
    if not stored_name or Path(stored_name).name != stored_name:
        return None
    return UPLOAD_DIR / stored_name


def unlink_uploads(stored_names: list[str]) -> None:
    for stored_name in stored_names:
        path = safe_upload_path(stored_name)
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            print(
                f"Unable to remove upload {stored_name}: {error}",
                flush=True,
            )


def reconcile_uploads() -> None:
    missing_ids: list[int] = []
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT id, stored_name
            FROM entries
            WHERE entry_type = 'image'
            """
        ).fetchall()
        referenced: set[str] = set()
        for row in rows:
            path = safe_upload_path(row["stored_name"])
            if path is None or not path.is_file():
                missing_ids.append(row["id"])
            else:
                referenced.add(row["stored_name"])
        if missing_ids:
            placeholders = ",".join("?" for _ in missing_ids)
            connection.execute(
                f"DELETE FROM entries WHERE id IN ({placeholders})",
                missing_ids,
            )

    for path in UPLOAD_DIR.iterdir():
        if (
            path.is_file()
            and path.name not in referenced
            and not path.name.startswith(".upload-")
        ):
            try:
                path.unlink()
            except OSError as error:
                print(
                    f"Unable to remove orphan upload {path.name}: {error}",
                    flush=True,
                )
        elif path.is_file() and path.name.startswith(".upload-"):
            try:
                path.unlink()
            except OSError:
                pass


def prune_entries(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT id, stored_name
        FROM entries
        ORDER BY id DESC
        LIMIT -1 OFFSET ?
        """,
        (MAX_ENTRIES,),
    ).fetchall()
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    stored_names = [
        row["stored_name"]
        for row in rows
        if row["stored_name"]
    ]
    placeholders = ",".join("?" for _ in ids)
    connection.execute(
        f"DELETE FROM entries WHERE id IN ({placeholders})",
        ids,
    )
    return stored_names


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


def clean_filename(raw_filename: str, suffix: str) -> str:
    filename = Path(unquote(raw_filename)).name
    filename = "".join(
        character
        for character in filename
        if character >= " " and character not in "\r\n"
    ).strip()
    if not filename:
        filename = f"image{suffix}"
    return filename[:180]


def public_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["entry_type"],
        "content": row["content"],
        "filename": row["filename"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "image_position": row["image_position"],
        "created_at": row["created_at"],
    }


class ClipboardHandler(BaseHTTPRequestHandler):
    server_version = "LanClipboard/2.0"

    def do_GET(self) -> None:
        if self.path == "/":
            try:
                body = STATIC_PATH.read_bytes()
            except OSError:
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "页面文件不可用"},
                )
                return
            self.send_bytes(HTTPStatus.OK, body, "text/html; charset=utf-8")
            return

        if self.path == "/health":
            try:
                with connect_db() as connection:
                    connection.execute("SELECT 1").fetchone()
                UPLOAD_DIR.stat()
            except (sqlite3.Error, OSError):
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "unhealthy"},
                )
                return
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return

        if self.path == "/api/entries":
            with connect_db() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        id, entry_type, content, filename,
                        mime_type, size_bytes, image_position, created_at
                    FROM entries
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (MAX_ENTRIES,),
                ).fetchall()
            self.send_json(
                HTTPStatus.OK,
                {"entries": [public_entry(row) for row in rows]},
            )
            return

        image_match = IMAGE_PATH.fullmatch(self.path)
        if image_match:
            self.send_image(
                int(image_match.group(1)),
                download=bool(image_match.group(2)),
            )
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "未找到该页面"})

    def do_POST(self) -> None:
        if self.path == "/api/entries":
            self.create_text_entry()
            return
        if self.path == "/api/images":
            self.create_image_entry()
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "未找到该接口"})

    def create_text_entry(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return

        content = payload.get("content")
        if not isinstance(content, str):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "内容必须是文本"},
            )
            return
        if not content.strip():
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "请输入要保存的内容"},
            )
            return
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "单条内容不能超过 200 KB"},
            )
            return

        with connect_db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO entries (content, entry_type)
                VALUES (?, 'text')
                """,
                (content,),
            )
            entry_id = cursor.lastrowid
            expired_uploads = prune_entries(connection)
            row = connection.execute(
                """
                SELECT
                    id, entry_type, content, filename,
                    mime_type, size_bytes, image_position, created_at
                FROM entries
                WHERE id = ?
                """,
                (entry_id,),
            ).fetchone()
        unlink_uploads(expired_uploads)
        self.send_json(
            HTTPStatus.CREATED,
            {"entry": public_entry(row)},
        )

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
            suffix,
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

        stored_name = f"{uuid.uuid4().hex}{suffix}"
        final_path = UPLOAD_DIR / stored_name
        temporary_path = UPLOAD_DIR / f".upload-{uuid.uuid4().hex}"
        try:
            temporary_path.write_bytes(data)
            os.replace(temporary_path, final_path)
            with connect_db() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO entries (
                        content, entry_type, filename, stored_name,
                        mime_type, size_bytes, sha256, image_position
                    )
                    VALUES (?, 'image', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        content,
                        filename,
                        stored_name,
                        detected_type,
                        len(data),
                        hashlib.sha256(data).hexdigest(),
                        image_position,
                    ),
                )
                entry_id = cursor.lastrowid
                expired_uploads = prune_entries(connection)
                row = connection.execute(
                    """
                    SELECT
                        id, entry_type, content, filename,
                        mime_type, size_bytes, image_position, created_at
                    FROM entries
                    WHERE id = ?
                    """,
                    (entry_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "图片保存失败，请重试"},
            )
            return

        unlink_uploads(expired_uploads)
        self.send_json(
            HTTPStatus.CREATED,
            {"entry": public_entry(row)},
        )

    def do_DELETE(self) -> None:
        if self.path == "/api/entries":
            with connect_db() as connection:
                rows = connection.execute(
                    """
                    SELECT stored_name
                    FROM entries
                    WHERE stored_name IS NOT NULL
                    """
                ).fetchall()
                cursor = connection.execute("DELETE FROM entries")
            unlink_uploads(
                [row["stored_name"] for row in rows]
            )
            self.send_json(
                HTTPStatus.OK,
                {"deleted": cursor.rowcount},
            )
            return

        match = ENTRY_PATH.fullmatch(self.path)
        if match:
            entry_id = int(match.group(1))
            with connect_db() as connection:
                row = connection.execute(
                    """
                    SELECT stored_name
                    FROM entries
                    WHERE id = ?
                    """,
                    (entry_id,),
                ).fetchone()
                if row is None:
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "这条记录不存在"},
                    )
                    return
                connection.execute(
                    "DELETE FROM entries WHERE id = ?",
                    (entry_id,),
                )
            if row["stored_name"]:
                unlink_uploads([row["stored_name"]])
            self.send_json(HTTPStatus.OK, {"deleted": 1})
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "未找到该接口"})

    def send_image(self, entry_id: int, download: bool) -> None:
        with connect_db() as connection:
            row = connection.execute(
                """
                SELECT filename, stored_name, mime_type, size_bytes
                FROM entries
                WHERE id = ? AND entry_type = 'image'
                """,
                (entry_id,),
            ).fetchone()
        if row is None:
            self.send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "图片不存在"},
            )
            return

        path = safe_upload_path(row["stored_name"])
        if path is None or not path.is_file():
            self.send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "图片文件不存在"},
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
            self.send_header("Content-Type", row["mime_type"])
            self.send_header("Content-Length", str(file_size))
            self.send_header(
                "Content-Disposition",
                f"{disposition}; filename*=UTF-8''{encoded_filename}",
            )
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            with path.open("rb") as image_file:
                while chunk := image_file.read(64 * 1024):
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
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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


def main() -> None:
    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), ClipboardHandler)
    print(f"LAN clipboard listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
