#!/usr/bin/env python3
"""Isolated integration tests for the LAN clipboard server."""

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
            assert len(
                server.json_call("/api/entries")["entries"]
            ) == 4

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
            server.json_call("/api/entries", "DELETE")
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
                        "validation": "ok",
                        "combined_retention": 100,
                        "restart_persistence": "ok",
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            server.stop()


if __name__ == "__main__":
    main()
