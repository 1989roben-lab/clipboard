#!/usr/bin/env python3
"""Memory: a private place for thoughts, images, and files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
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
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_PATH = STATIC_DIR / "index.html"
STATIC_ROUTES = {
    "/manifest.webmanifest": (
        STATIC_DIR / "manifest.webmanifest",
        "application/manifest+json; charset=utf-8",
        "no-cache",
    ),
    "/service-worker.js": (
        STATIC_DIR / "service-worker.js",
        "application/javascript; charset=utf-8",
        "no-cache",
    ),
    "/icons/icon-192.png": (
        STATIC_DIR / "icons" / "icon-192.png",
        "image/png",
        "public, max-age=31536000, immutable",
    ),
    "/icons/icon-512.png": (
        STATIC_DIR / "icons" / "icon-512.png",
        "image/png",
        "public, max-age=31536000, immutable",
    ),
    "/icons/icon-maskable-512.png": (
        STATIC_DIR / "icons" / "icon-maskable-512.png",
        "image/png",
        "public, max-age=31536000, immutable",
    ),
    "/icons/apple-touch-icon.png": (
        STATIC_DIR / "icons" / "apple-touch-icon.png",
        "image/png",
        "public, max-age=31536000, immutable",
    ),
}
MAX_ENTRIES = 100
MAX_CONTENT_BYTES = 200 * 1024
MAX_IMAGE_CONTENT_BYTES = 12 * 1024
MAX_JSON_REQUEST_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_FILE_CHUNK_BYTES = 8 * 1024 * 1024
FILE_UPLOAD_TTL_SECONDS = 24 * 60 * 60
ENTRY_PATH = re.compile(r"^/api/entries/([1-9][0-9]*)$")
TODO_ITEM_PATH = re.compile(r"^/api/todo-items/([1-9][0-9]*)$")
IMAGE_PATH = re.compile(
    r"^/api/images/([1-9][0-9]*)(/download)?$"
)
FILE_PATH = re.compile(
    r"^/api/files/([1-9][0-9]*)(/download)?$"
)
FILE_UPLOAD_PATH = re.compile(
    r"^/api/file-uploads/([0-9a-f]{32})(/complete)?$"
)
UPLOAD_LOCK = threading.Lock()
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
    connection.execute("PRAGMA foreign_keys = ON")
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
                ),
                updated_at TEXT NOT NULL DEFAULT (
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
            "updated_at": (
                "ALTER TABLE entries ADD COLUMN updated_at TEXT"
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
            UPDATE entries
            SET updated_at = created_at
            WHERE updated_at IS NULL OR updated_at = ''
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS entries_created_at_idx
            ON entries(created_at DESC, id DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS entries_updated_at_idx
            ON entries(updated_at DESC, id DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS file_uploads (
                upload_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content TEXT NOT NULL,
                total_size INTEGER NOT NULL,
                received_size INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS todo_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                stage INTEGER NOT NULL DEFAULT 1
                    CHECK (stage BETWEEN 1 AND 5),
                position INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                updated_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                FOREIGN KEY (entry_id) REFERENCES entries(id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS todo_items_entry_position_idx
            ON todo_items(entry_id, position, id)
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
            WHERE entry_type IN ('image', 'file')
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
        connection.execute("DELETE FROM file_uploads")

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


def expire_file_uploads() -> None:
    cutoff = time.time() - FILE_UPLOAD_TTL_SECONDS
    with UPLOAD_LOCK, connect_db() as connection:
        rows = connection.execute(
            """
            SELECT stored_name
            FROM file_uploads
            WHERE created_at < ?
            """,
            (cutoff,),
        ).fetchall()
        connection.execute(
            "DELETE FROM file_uploads WHERE created_at < ?",
            (cutoff,),
        )
    unlink_uploads([row["stored_name"] for row in rows])


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


def public_entry(
    row: sqlite3.Row,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    entry = {
        "id": row["id"],
        "type": row["entry_type"],
        "content": row["content"],
        "filename": row["filename"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "image_position": row["image_position"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["entry_type"] == "todo":
        if connection is None:
            raise ValueError("todo entries require a database connection")
        items = connection.execute(
            """
            SELECT id, content, stage
            FROM todo_items
            WHERE entry_id = ?
            ORDER BY position, id
            """,
            (row["id"],),
        ).fetchall()
        entry["items"] = [
            {
                "id": item["id"],
                "content": item["content"],
                "stage": item["stage"],
            }
            for item in items
        ]
    return entry


class ClipboardHandler(BaseHTTPRequestHandler):
    server_version = "Memory/1.0"

    def do_GET(self) -> None:
        if self.path.partition("?")[0] == "/":
            try:
                body = STATIC_PATH.read_bytes()
            except OSError:
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "页面文件不可用"},
                )
                return
            self.send_bytes(
                HTTPStatus.OK,
                body,
                "text/html; charset=utf-8",
                cache_control="no-store, max-age=0",
            )
            return

        static_route = STATIC_ROUTES.get(self.path.partition("?")[0])
        if static_route is not None:
            path, content_type, cache_control = static_route
            try:
                body = path.read_bytes()
            except OSError:
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "静态资源不可用"},
                )
                return
            self.send_bytes(
                HTTPStatus.OK,
                body,
                content_type,
                cache_control=cache_control,
            )
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
                        mime_type, size_bytes, image_position, created_at,
                        updated_at
                    FROM entries
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (MAX_ENTRIES,),
                ).fetchall()
            self.send_json(
                HTTPStatus.OK,
                {
                    "entries": [
                        public_entry(row, connection) for row in rows
                    ]
                },
            )
            return

        image_match = IMAGE_PATH.fullmatch(self.path)
        if image_match:
            self.send_upload(
                int(image_match.group(1)),
                entry_type="image",
                download=bool(image_match.group(2)),
            )
            return

        file_match = FILE_PATH.fullmatch(self.path)
        if file_match:
            self.send_upload(
                int(file_match.group(1)),
                entry_type="file",
                download=True,
            )
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "未找到该页面"})

    def do_POST(self) -> None:
        if self.path == "/api/entries":
            self.create_entry()
            return
        if self.path == "/api/images":
            self.create_image_entry()
            return
        if self.path == "/api/file-uploads":
            self.create_file_upload()
            return
        upload_match = FILE_UPLOAD_PATH.fullmatch(self.path)
        if upload_match:
            upload_id = upload_match.group(1)
            if upload_match.group(2):
                self.complete_file_upload(upload_id)
            else:
                self.append_file_chunk(upload_id)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "未找到该接口"})

    def do_PATCH(self) -> None:
        todo_item_match = TODO_ITEM_PATH.fullmatch(self.path)
        if todo_item_match:
            self.update_todo_stage(int(todo_item_match.group(1)))
            return
        match = ENTRY_PATH.fullmatch(self.path)
        if match:
            self.update_entry(int(match.group(1)))
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "未找到该接口"})

    def update_entry(self, entry_id: int) -> None:
        payload = self.read_json_body()
        if payload is None:
            return

        with connect_db() as connection:
            existing = connection.execute(
                """
                SELECT entry_type, image_position
                FROM entries
                WHERE id = ?
                """,
                (entry_id,),
            ).fetchone()
            if existing is None:
                self.send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "这条记录不存在"},
                )
                return
            entry_type = existing["entry_type"]
            if entry_type == "todo":
                self.update_todo_entry(connection, entry_id, payload)
                return

            content = payload.get("content")
            if not isinstance(content, str):
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "内容必须是文本"},
                )
                return
            if entry_type == "text" and not content.strip():
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "请输入要保存的内容"},
                )
                return
            content_limit = (
                MAX_CONTENT_BYTES
                if entry_type == "text"
                else MAX_IMAGE_CONTENT_BYTES
            )
            if len(content.encode("utf-8")) > content_limit:
                self.send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {
                        "error": (
                            "单条内容不能超过 200 KB"
                            if entry_type == "text"
                            else "图片或附件说明不能超过 12 KB"
                        )
                    },
                )
                return
            image_position = existing["image_position"]
            if entry_type == "image":
                image_position = max(
                    0,
                    min(image_position or 0, len(content)),
                )
            connection.execute(
                """
                UPDATE entries
                SET content = ?, image_position = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (content, image_position, entry_id),
            )
            row = connection.execute(
                """
                SELECT
                    id, entry_type, content, filename,
                    mime_type, size_bytes, image_position, created_at,
                    updated_at
                FROM entries
                WHERE id = ?
                """,
                (entry_id,),
            ).fetchone()
        self.send_json(
            HTTPStatus.OK,
            {"entry": public_entry(row, connection)},
        )

    def create_entry(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return

        entry_type = payload.get("type", "text")
        if entry_type == "todo":
            self.create_todo_entry(payload)
            return
        if entry_type != "text":
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "不支持的记录类型"},
            )
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
                INSERT INTO entries (
                    content, entry_type, created_at, updated_at
                )
                VALUES (
                    ?, 'text',
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                """,
                (content,),
            )
            entry_id = cursor.lastrowid
            expired_uploads = prune_entries(connection)
            row = connection.execute(
                """
                SELECT
                    id, entry_type, content, filename,
                    mime_type, size_bytes, image_position, created_at,
                    updated_at
                FROM entries
                WHERE id = ?
                """,
                (entry_id,),
            ).fetchone()
        unlink_uploads(expired_uploads)
        self.send_json(
            HTTPStatus.CREATED,
            {"entry": public_entry(row, connection)},
        )

    def create_todo_entry(self, payload: dict[str, Any]) -> None:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "待办项目必须是列表"},
            )
            return
        items = [
            item.strip()
            for item in raw_items
            if isinstance(item, str) and item.strip()
        ]
        if len(items) != len(raw_items):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "每个待办项目都必须是非空文本"},
            )
            return
        if not items:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "请至少添加一个待办项目"},
            )
            return
        if sum(len(item.encode("utf-8")) for item in items) > MAX_CONTENT_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "待办项目合计不能超过 200 KB"},
            )
            return

        with connect_db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO entries (
                    content, entry_type, created_at, updated_at
                )
                VALUES (
                    '', 'todo',
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                """
            )
            entry_id = cursor.lastrowid
            connection.executemany(
                """
                INSERT INTO todo_items (entry_id, content, stage, position)
                VALUES (?, ?, 1, ?)
                """,
                [
                    (entry_id, content, position)
                    for position, content in enumerate(items)
                ],
            )
            expired_uploads = prune_entries(connection)
            row = connection.execute(
                """
                SELECT
                    id, entry_type, content, filename,
                    mime_type, size_bytes, image_position, created_at,
                    updated_at
                FROM entries
                WHERE id = ?
                """,
                (entry_id,),
            ).fetchone()
            entry = public_entry(row, connection)
        unlink_uploads(expired_uploads)
        self.send_json(HTTPStatus.CREATED, {"entry": entry})

    def update_todo_entry(
        self,
        connection: sqlite3.Connection,
        entry_id: int,
        payload: dict[str, Any],
    ) -> None:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "待办清单至少需要一个项目"},
            )
            return

        normalized: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        total_bytes = 0
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "待办项目格式无效"},
                )
                return
            content = raw_item.get("content")
            stage = raw_item.get("stage")
            item_id = raw_item.get("id")
            if not isinstance(content, str) or not content.strip():
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "待办项目内容不能为空"},
                )
                return
            if (
                not isinstance(stage, int)
                or isinstance(stage, bool)
                or stage < 1
                or stage > 5
            ):
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Stage 必须是 1 到 5"},
                )
                return
            if item_id is not None:
                if (
                    not isinstance(item_id, int)
                    or isinstance(item_id, bool)
                    or item_id <= 0
                    or item_id in seen_ids
                ):
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "待办项目 ID 无效或重复"},
                    )
                    return
                seen_ids.add(item_id)
            content = content.strip()
            total_bytes += len(content.encode("utf-8"))
            normalized.append(
                {"id": item_id, "content": content, "stage": stage}
            )

        if total_bytes > MAX_CONTENT_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "待办项目合计不能超过 200 KB"},
            )
            return

        existing_ids = {
            row["id"]
            for row in connection.execute(
                "SELECT id FROM todo_items WHERE entry_id = ?",
                (entry_id,),
            ).fetchall()
        }
        if not seen_ids.issubset(existing_ids):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "待办项目不属于这张清单"},
            )
            return

        removed_ids = existing_ids - seen_ids
        if removed_ids:
            placeholders = ",".join("?" for _ in removed_ids)
            connection.execute(
                f"DELETE FROM todo_items WHERE id IN ({placeholders})",
                tuple(removed_ids),
            )
        for position, item in enumerate(normalized):
            if item["id"] is None:
                connection.execute(
                    """
                    INSERT INTO todo_items (entry_id, content, stage, position)
                    VALUES (?, ?, ?, ?)
                    """,
                    (entry_id, item["content"], item["stage"], position),
                )
            else:
                connection.execute(
                    """
                    UPDATE todo_items
                    SET content = ?, stage = ?, position = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ? AND entry_id = ?
                    """,
                    (
                        item["content"],
                        item["stage"],
                        position,
                        item["id"],
                        entry_id,
                    ),
                )
        connection.execute(
            """
            UPDATE entries
            SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (entry_id,),
        )
        row = connection.execute(
            """
            SELECT
                id, entry_type, content, filename,
                mime_type, size_bytes, image_position, created_at,
                updated_at
            FROM entries
            WHERE id = ?
            """,
            (entry_id,),
        ).fetchone()
        self.send_json(
            HTTPStatus.OK,
            {"entry": public_entry(row, connection)},
        )

    def update_todo_stage(self, item_id: int) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        stage = payload.get("stage")
        if (
            not isinstance(stage, int)
            or isinstance(stage, bool)
            or stage < 1
            or stage > 5
        ):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Stage 必须是 1 到 5"},
            )
            return
        with connect_db() as connection:
            row = connection.execute(
                """
                SELECT id, entry_id, content, stage
                FROM todo_items
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                self.send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "这个待办项目不存在"},
                )
                return
            connection.execute(
                """
                UPDATE todo_items
                SET stage = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (stage, item_id),
            )
            item = {
                "id": row["id"],
                "content": row["content"],
                "stage": stage,
            }
        self.send_json(
            HTTPStatus.OK,
            {"entry_id": row["entry_id"], "item": item},
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
                        mime_type, size_bytes, sha256, image_position,
                        created_at, updated_at
                    )
                    VALUES (
                        ?, 'image', ?, ?, ?, ?, ?, ?,
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
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
                        mime_type, size_bytes, image_position, created_at,
                        updated_at
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

    def create_file_upload(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return

        filename_value = payload.get("filename")
        total_size = payload.get("size")
        content = payload.get("content", "")
        if not isinstance(filename_value, str):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "附件名称无效"},
            )
            return
        if (
            not isinstance(total_size, int)
            or isinstance(total_size, bool)
            or total_size < 0
        ):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "附件大小无效"},
            )
            return
        if total_size > MAX_FILE_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "单个附件不能超过 100 MB"},
            )
            return
        if not isinstance(content, str):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "附件说明必须是文本"},
            )
            return
        if len(content.encode("utf-8")) > MAX_IMAGE_CONTENT_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "附件说明不能超过 12 KB"},
            )
            return

        expire_file_uploads()
        upload_id = uuid.uuid4().hex
        stored_name = f".upload-{upload_id}"
        upload_path = UPLOAD_DIR / stored_name
        filename = clean_filename(filename_value, "附件")
        mime_type = clean_mime_type(payload.get("mime_type"))
        try:
            upload_path.touch(exist_ok=False)
            with connect_db() as connection:
                connection.execute(
                    """
                    INSERT INTO file_uploads (
                        upload_id, filename, stored_name, mime_type,
                        content, total_size, received_size, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        upload_id,
                        filename,
                        stored_name,
                        mime_type,
                        content,
                        total_size,
                        time.time(),
                    ),
                )
        except (OSError, sqlite3.Error):
            upload_path.unlink(missing_ok=True)
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "无法开始附件上传，请重试"},
            )
            return

        self.send_json(
            HTTPStatus.CREATED,
            {
                "upload_id": upload_id,
                "received": 0,
                "total": total_size,
                "chunk_size": MAX_FILE_CHUNK_BYTES,
            },
        )

    def append_file_chunk(self, upload_id: str) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            offset = int(self.headers.get("X-Upload-Offset", "-1"))
        except ValueError:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "附件分片参数无效"},
            )
            return
        if content_length <= 0:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "附件分片不能为空"},
            )
            return
        if content_length > MAX_FILE_CHUNK_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "附件分片不能超过 8 MB"},
            )
            return

        data = self.rfile.read(content_length)
        if len(data) != content_length:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "附件分片上传不完整"},
            )
            return

        try:
            with UPLOAD_LOCK, connect_db() as connection:
                row = connection.execute(
                    """
                    SELECT stored_name, total_size, received_size
                    FROM file_uploads
                    WHERE upload_id = ?
                    """,
                    (upload_id,),
                ).fetchone()
                if row is None:
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "附件上传任务不存在或已过期"},
                    )
                    return
                if offset != row["received_size"]:
                    self.send_json(
                        HTTPStatus.CONFLICT,
                        {
                            "error": "附件分片位置不一致",
                            "received": row["received_size"],
                        },
                    )
                    return
                received_size = offset + content_length
                if received_size > row["total_size"]:
                    self.send_json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": "附件内容超过声明大小"},
                    )
                    return
                upload_path = safe_upload_path(row["stored_name"])
                if (
                    upload_path is None
                    or not upload_path.is_file()
                    or upload_path.stat().st_size != offset
                ):
                    self.send_json(
                        HTTPStatus.CONFLICT,
                        {"error": "附件临时文件状态不一致，请重新选择"},
                    )
                    return
                with upload_path.open("ab") as upload_file:
                    upload_file.write(data)
                connection.execute(
                    """
                    UPDATE file_uploads
                    SET received_size = ?
                    WHERE upload_id = ?
                    """,
                    (received_size, upload_id),
                )
        except (OSError, sqlite3.Error):
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "附件分片保存失败，请重试"},
            )
            return

        self.send_json(
            HTTPStatus.OK,
            {"received": received_size},
        )

    def complete_file_upload(self, upload_id: str) -> None:
        final_path: Path | None = None
        upload_path: Path | None = None
        try:
            with UPLOAD_LOCK, connect_db() as connection:
                row = connection.execute(
                    """
                    SELECT
                        filename, stored_name, mime_type, content,
                        total_size, received_size
                    FROM file_uploads
                    WHERE upload_id = ?
                    """,
                    (upload_id,),
                ).fetchone()
                if row is None:
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "附件上传任务不存在或已过期"},
                    )
                    return
                if row["received_size"] != row["total_size"]:
                    self.send_json(
                        HTTPStatus.CONFLICT,
                        {
                            "error": "附件尚未上传完整",
                            "received": row["received_size"],
                            "total": row["total_size"],
                        },
                    )
                    return
                upload_path = safe_upload_path(row["stored_name"])
                if (
                    upload_path is None
                    or not upload_path.is_file()
                    or upload_path.stat().st_size != row["total_size"]
                ):
                    self.send_json(
                        HTTPStatus.CONFLICT,
                        {"error": "附件临时文件状态不一致，请重新选择"},
                    )
                    return

                stored_name = uuid.uuid4().hex
                final_path = UPLOAD_DIR / stored_name
                os.replace(upload_path, final_path)
                cursor = connection.execute(
                    """
                    INSERT INTO entries (
                        content, entry_type, filename, stored_name,
                        mime_type, size_bytes, created_at, updated_at
                    )
                    VALUES (
                        ?, 'file', ?, ?, ?, ?,
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
                    """,
                    (
                        row["content"],
                        row["filename"],
                        stored_name,
                        row["mime_type"],
                        row["total_size"],
                    ),
                )
                entry_id = cursor.lastrowid
                connection.execute(
                    "DELETE FROM file_uploads WHERE upload_id = ?",
                    (upload_id,),
                )
                expired_uploads = prune_entries(connection)
                entry = connection.execute(
                    """
                    SELECT
                        id, entry_type, content, filename,
                        mime_type, size_bytes, image_position, created_at,
                        updated_at
                    FROM entries
                    WHERE id = ?
                    """,
                    (entry_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            if (
                final_path is not None
                and final_path.exists()
                and upload_path is not None
            ):
                try:
                    os.replace(final_path, upload_path)
                except OSError:
                    pass
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "附件保存失败，请重试"},
            )
            return

        unlink_uploads(expired_uploads)
        self.send_json(
            HTTPStatus.CREATED,
            {"entry": public_entry(entry)},
        )

    def cancel_file_upload(self, upload_id: str) -> None:
        with UPLOAD_LOCK, connect_db() as connection:
            row = connection.execute(
                """
                SELECT stored_name
                FROM file_uploads
                WHERE upload_id = ?
                """,
                (upload_id,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "DELETE FROM file_uploads WHERE upload_id = ?",
                    (upload_id,),
                )
        if row is not None:
            unlink_uploads([row["stored_name"]])
        self.send_json(HTTPStatus.OK, {"cancelled": row is not None})

    def do_DELETE(self) -> None:
        upload_match = FILE_UPLOAD_PATH.fullmatch(self.path)
        if upload_match and not upload_match.group(2):
            self.cancel_file_upload(upload_match.group(1))
            return

        if self.path == "/api/entries":
            with UPLOAD_LOCK, connect_db() as connection:
                entry_rows = connection.execute(
                    """
                    SELECT stored_name
                    FROM entries
                    WHERE stored_name IS NOT NULL
                    """
                ).fetchall()
                upload_rows = connection.execute(
                    """
                    SELECT stored_name
                    FROM file_uploads
                    """
                ).fetchall()
                cursor = connection.execute("DELETE FROM entries")
                cancelled_uploads = connection.execute(
                    "DELETE FROM file_uploads"
                ).rowcount
            unlink_uploads(
                [row["stored_name"] for row in entry_rows]
                + [row["stored_name"] for row in upload_rows]
            )
            self.send_json(
                HTTPStatus.OK,
                {
                    "deleted": cursor.rowcount,
                    "cancelled_uploads": cancelled_uploads,
                },
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

    def send_upload(
        self,
        entry_id: int,
        entry_type: str,
        download: bool,
    ) -> None:
        with connect_db() as connection:
            row = connection.execute(
                """
                SELECT filename, stored_name, mime_type, size_bytes
                FROM entries
                WHERE id = ? AND entry_type = ?
                """,
                (entry_id, entry_type),
            ).fetchone()
        if row is None:
            self.send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "文件不存在"},
            )
            return

        path = safe_upload_path(row["stored_name"])
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


def main() -> None:
    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), ClipboardHandler)
    print(f"Memory listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
