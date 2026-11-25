import os
import re
import threading
from pathlib import Path

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8016"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(DATA_DIR / "uploads")))
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
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
TODO_ORDER_PATH = re.compile(r"^/api/entries/([1-9][0-9]*)/todo-order$")
TODO_ITEM_PATH = re.compile(r"^/api/todo-items/([1-9][0-9]*)$")
TODO_STRUCTURE_PATH = re.compile(r"^/api/todo-items/([1-9][0-9]*)/structure$")
IMAGE_PATH = re.compile(
    r"^/api/images/([1-9][0-9]*)(/download)?$"
)
IMAGE_ITEM_PATH = re.compile(r"^/api/entries/([1-9][0-9]*)/images/(legacy|[0-9a-f]{32})(/download)?$")
DRAFT_IMAGE_PATH = re.compile(r"^/api/draft-images/([0-9a-f]{32})$")
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
