#!/usr/bin/env python3
"""Isolated integration tests for Memory."""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
IMAGES = {
    "test.png": (
        "image/png",
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
    ),
    "test.jpg": (
        "image/jpeg",
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////"
        "////////////////////////////////////////////////"
        "////////////////////2wBDAf//////////////////////"
        "////////////////////////////////////////////////"
        "////////////////////wAARCAABAAEDASIAAhEBAxEB/8QA"
        "FQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAA"
        "AAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAA"
        "AAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAA"
        "AAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAA"
        "AP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/a"
        "AAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgB"
        "AQABPyF//9oADAMBAAIAAwAAABD/xAAUEQEAAAAAAAAAAAAA"
        "AAAAAAAA/9oACAEDAQE/EB//xAAUEQEAAAAAAAAAAAAAAAAA"
        "AAAA/9oACAECAQE/EB//xAAUEAEAAAAAAAAAAAAAAAAAAAAA"
        "/9oACAEBAAE/EB//2Q==",
    ),
    "test.webp": (
        "image/webp",
        "UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADMDO"
        "JaQAA3AA/vuUAAA=",
    ),
    "test.gif": (
        "image/gif",
        "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==",
    ),
}


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class TestServer:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "DB_PATH": str(self.data_dir / "clipboard.db"),
                "HOST": "127.0.0.1",
                "PORT": str(self.port),
            }
        )
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            try:
                self.call("/health")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("test server did not become healthy")

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None

    def call(
        self,
        path: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        expected: int = 200,
    ) -> tuple[bytes, Any]:
        request = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers=headers or {},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                data = response.read()
                response_headers = response.headers
        except urllib.error.HTTPError as error:
            status = error.code
            data = error.read()
            response_headers = error.headers
        assert status == expected, (path, status, data[:300])
        return data, response_headers

    def json_call(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        expected: int = 200,
    ) -> dict[str, Any]:
        body = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        data, _ = self.call(
            path,
            method,
            body,
            {"Content-Type": "application/json"} if body else {},
            expected,
        )
        return json.loads(data)

    def upload(
        self,
        name: str,
        mime_type: str,
        data: bytes,
        content: str = "",
        image_position: int = 0,
        expected: int = 201,
    ) -> dict[str, Any]:
        payload, _ = self.call(
            "/api/images",
            "POST",
            data,
            {
                "Content-Type": mime_type,
                "X-Filename": urllib.parse.quote(name),
                "X-Content": urllib.parse.quote(content),
                "X-Image-Position": str(image_position),
            },
            expected,
        )
        return json.loads(payload)

    def upload_file(
        self,
        name: str,
        mime_type: str,
        data: bytes,
        content: str = "",
    ) -> dict[str, Any]:
        initialized = self.json_call(
            "/api/file-uploads",
            "POST",
            {
                "filename": name,
                "mime_type": mime_type,
                "size": len(data),
                "content": content,
            },
            201,
        )
        upload_id = initialized["upload_id"]
        offset = 0
        for end in range(3, len(data) + 3, 3):
            chunk = data[offset : min(end, len(data))]
            if not chunk:
                break
            payload, _ = self.call(
                f"/api/file-uploads/{upload_id}",
                "POST",
                chunk,
                {
                    "Content-Type": "application/octet-stream",
                    "X-Upload-Offset": str(offset),
                },
            )
            offset = json.loads(payload)["received"]
        return self.json_call(
            f"/api/file-uploads/{upload_id}/complete",
            "POST",
            {},
            201,
        )


def decoded_images() -> dict[str, tuple[str, bytes]]:
    return {
        name: (mime_type, base64.b64decode(encoded))
        for name, (mime_type, encoded) in IMAGES.items()
    }


def main() -> None:
    with tempfile.TemporaryDirectory(
        prefix="lan-clipboard-images-"
    ) as temporary_directory:
        root = Path(temporary_directory)
        server = TestServer(root)
        images = decoded_images()
        server.start()
        try:
            page, page_headers = server.call("/")
            page_text = page.decode("utf-8")
            assert "interactive-widget=resizes-content" in page_text
            assert "user-scalable=no" not in page_text
            assert 'rel="manifest"' in page_text
            assert 'id="install-button"' in page_text
            assert 'id="force-refresh"' in page_text
            assert '<h1>Memory</h1>' in page_text
            assert 'id="choose-image"' in page_text
            assert "添加文件" in page_text
            assert 'id="choose-attachment"' not in page_text
            assert 'id="attachment-input"' not in page_text
            assert "同一局域网" not in page_text
            assert "Windows" not in page_text
            assert 'id="install-modal"' in page_text
            assert 'loadEntries({ force: true })' in page_text
            assert 'register("/service-worker.js?v=17"' in page_text
            assert "border: none;" in page_text
            assert "backdrop-filter: blur(22px) saturate(180%);" in page_text
            assert ".image-preview-link" in page_text
            assert "background: #fff;" in page_text
            assert 'apple-touch-icon.png?v=14' in page_text
            assert 'updateViaCache: "none"' in page_text
            assert 'window.location.reload()' in page_text
            assert 'registration.update()' in page_text
            assert page_headers.get_content_type() == "text/html"
            assert page_headers["Cache-Control"] == "no-store, max-age=0"
            versioned_page, versioned_page_headers = server.call(
                "/?app=v17"
            )
            assert versioned_page == page
            assert (
                versioned_page_headers["Cache-Control"]
                == "no-store, max-age=0"
            )

            manifest_body, manifest_headers = server.call(
                "/manifest.webmanifest"
            )
            manifest = json.loads(manifest_body)
            assert manifest["display"] == "standalone"
            assert manifest["name"] == "Memory"
            assert manifest["start_url"] == "/?app=v17"
            assert all("?v=14" in icon["src"] for icon in manifest["icons"])
            assert {icon["sizes"] for icon in manifest["icons"]} >= {
                "192x192",
                "512x512",
            }
            assert any(
                "maskable" in icon.get("purpose", "")
                for icon in manifest["icons"]
            )
            assert (
                manifest_headers.get_content_type()
                == "application/manifest+json"
            )

            worker, worker_headers = server.call("/service-worker.js")
            worker_text = worker.decode("utf-8")
            assert 'url.pathname.startsWith("/api/")' in worker_text
            assert 'request.mode === "navigate"' in worker_text
            assert '.catch(() => caches.match("/?app=v17"))' in worker_text
            assert 'memory-shell-v17' in worker_text
            assert 'ACTIVATE_UPDATE' in worker_text
            assert worker_headers.get_content_type() == "application/javascript"
            assert worker_headers["Cache-Control"] == "no-cache"
            versioned_worker, _ = server.call("/service-worker.js?v=1")
            assert versioned_worker == worker

            for icon_path in (
                "/icons/icon-192.png",
                "/icons/icon-512.png",
                "/icons/icon-maskable-512.png",
                "/icons/apple-touch-icon.png",
            ):
                icon, icon_headers = server.call(icon_path)
                assert icon.startswith(b"\x89PNG\r\n\x1a\n")
                assert icon_headers.get_content_type() == "image/png"
                assert "immutable" in icon_headers["Cache-Control"]

            created: list[dict[str, Any]] = []
            for name, (mime_type, raw) in images.items():
                caption = "前😀后" if name == "test.png" else ""
                position = 2 if name == "test.png" else 0
                entry = server.upload(
                    name,
                    mime_type,
                    raw,
                    content=caption,
                    image_position=position,
                )["entry"]
                assert entry["content"] == caption
                assert entry["image_position"] == position
                created.append(entry)
                downloaded, headers = server.call(
                    f"/api/images/{entry['id']}"
                )
                assert downloaded == raw
                assert headers.get_content_type() == mime_type
                _, download_headers = server.call(
                    f"/api/images/{entry['id']}/download"
                )
                assert download_headers[
                    "Content-Disposition"
                ].startswith("attachment;")
            attachment_data = b"arbitrary-file-content"
            attachment = server.upload_file(
                "资料.tar",
                "application/x-tar",
                attachment_data,
                content="测试附件",
            )["entry"]
            assert attachment["type"] == "file"
            assert attachment["filename"] == "资料.tar"
            assert attachment["size_bytes"] == len(attachment_data)
            downloaded, headers = server.call(
                f"/api/files/{attachment['id']}/download"
            )
            assert downloaded == attachment_data
            assert headers.get_content_type() == "application/x-tar"
            assert headers["Content-Disposition"].startswith("attachment;")
            assert len(
                server.json_call("/api/entries")["entries"]
            ) == 5

            editable = server.json_call(
                "/api/entries",
                "POST",
                {"content": "修改前"},
                201,
            )["entry"]
            updated = server.json_call(
                f"/api/entries/{editable['id']}",
                "PATCH",
                {"content": "修改后\n第二行"},
            )["entry"]
            assert updated["content"] == "修改后\n第二行"
            assert updated["created_at"] == editable["created_at"]
            assert server.json_call(
                "/api/entries"
            )["entries"][0]["content"] == "修改后\n第二行"
            empty_update = server.json_call(
                f"/api/entries/{editable['id']}",
                "PATCH",
                {"content": "   "},
                400,
            )
            assert "请输入" in empty_update["error"]
            image_update = server.json_call(
                f"/api/entries/{created[0]['id']}",
                "PATCH",
                {"content": "图片说明已修改"},
            )["entry"]
            assert image_update["content"] == "图片说明已修改"
            assert image_update["image_position"] == 2
            file_update = server.json_call(
                f"/api/entries/{attachment['id']}",
                "PATCH",
                {"content": "附件说明已修改"},
            )["entry"]
            assert file_update["content"] == "附件说明已修改"
            cleared_image_note = server.json_call(
                f"/api/entries/{created[0]['id']}",
                "PATCH",
                {"content": ""},
            )["entry"]
            assert cleared_image_note["content"] == ""
            assert cleared_image_note["image_position"] == 0
            server.json_call(
                f"/api/entries/{editable['id']}",
                "DELETE",
            )

            oversized = server.json_call(
                "/api/file-uploads",
                "POST",
                {
                    "filename": "too-large.bin",
                    "mime_type": "application/octet-stream",
                    "size": 100 * 1024 * 1024 + 1,
                    "content": "",
                },
                413,
            )
            assert "100 MB" in oversized["error"]

            maximum = server.json_call(
                "/api/file-uploads",
                "POST",
                {
                    "filename": "maximum.bin",
                    "mime_type": "application/octet-stream",
                    "size": 100 * 1024 * 1024,
                    "content": "",
                },
                201,
            )
            server.json_call(
                f"/api/file-uploads/{maximum['upload_id']}",
                "DELETE",
            )

            server.upload(
                "fake.png",
                "image/png",
                b"not an image",
                expected=415,
            )
            server.upload(
                "wrong.jpg",
                "image/png",
                images["test.png"][1],
                expected=415,
            )
            server.upload(
                "bad-position.png",
                "image/png",
                images["test.png"][1],
                content="短文字",
                image_position=99,
                expected=400,
            )

            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.port,
                timeout=10,
            )
            connection.putrequest("POST", "/api/images")
            connection.putheader(
                "Content-Length",
                str(10 * 1024 * 1024 + 1),
            )
            connection.putheader("Content-Type", "image/png")
            connection.putheader("X-Filename", "large.png")
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 413
            response.read()
            connection.close()

            first_id = created[0]["id"]
            server.json_call(
                f"/api/entries/{first_id}",
                "DELETE",
            )
            server.call(
                f"/api/images/{first_id}",
                expected=404,
            )
            unfinished = server.json_call(
                "/api/file-uploads",
                "POST",
                {
                    "filename": "unfinished.bin",
                    "mime_type": "application/octet-stream",
                    "size": 5,
                    "content": "",
                },
                201,
            )
            server.call(
                f"/api/file-uploads/{unfinished['upload_id']}",
                "POST",
                b"x",
                {
                    "Content-Type": "application/octet-stream",
                    "X-Upload-Offset": "0",
                },
            )
            cleared = server.json_call("/api/entries", "DELETE")
            assert cleared["deleted"] == 4
            assert cleared["cancelled_uploads"] == 1
            assert not list((root / "uploads").iterdir())

            old_image = server.upload(
                "test.png",
                "image/png",
                images["test.png"][1],
            )["entry"]
            for index in range(99):
                server.json_call(
                    "/api/entries",
                    "POST",
                    {"content": f"text-{index}"},
                    201,
                )
            assert len(
                server.json_call("/api/entries")["entries"]
            ) == 100
            server.json_call(
                "/api/entries",
                "POST",
                {"content": "text-100"},
                201,
            )
            entries = server.json_call("/api/entries")["entries"]
            assert len(entries) == 100
            assert all(
                item["id"] != old_image["id"]
                for item in entries
            )
            server.call(
                f"/api/images/{old_image['id']}",
                expected=404,
            )
            assert not list((root / "uploads").iterdir())

            server.json_call("/api/entries", "DELETE")
            persisted = server.upload(
                "test.webp",
                "image/webp",
                images["test.webp"][1],
            )["entry"]
            server.stop()
            server.start()
            entries = server.json_call("/api/entries")["entries"]
            assert len(entries) == 1
            assert entries[0]["id"] == persisted["id"]
            downloaded, _ = server.call(
                f"/api/images/{persisted['id']}"
            )
            assert downloaded == images["test.webp"][1]
            print(
                json.dumps(
                    {
                        "formats": 4,
                        "attachments": "100 MB chunked",
                        "clear_all": "records and pending uploads",
                        "validation": "ok",
                        "combined_retention": 100,
                        "restart_persistence": "ok",
                        "pwa": "installable shell-only cache",
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            server.stop()


if __name__ == "__main__":
    main()
