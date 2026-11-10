from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, unquote

import httpx
from .downloads import save_media
from .durations import read_mp4_duration
from playwright.async_api import BrowserContext, CDPSession, Page, Response, async_playwright

PROFILE_FEED_PATH = "/rest/o/w/pc/feed/profile"
CURSOR_KEYS = (
    "pcursor", "nextPcursor", "next_pcursor", "nextCursor", "next_cursor",
    "cursor", "pageCursor", "page_cursor", "pageToken", "page_token",
    "nextPageToken", "next_page_token",
)
REQUEST_CURSOR_KEYS = (
    "pcursor", "cursor", "pageCursor", "page_cursor", "pageToken", "page_token",
    "nextPageToken", "next_page_token",
)
VIEW_KEYS = (
    "viewCount", "view_count", "views", "playCount", "play_count", "playCnt",
    "play_cnt", "viewNum", "view_num", "playNum", "play_num", "viewCountText",
    "playCountText",
)
LIKE_KEYS = ("likeCount", "like_count", "realLikeCount", "likes", "likeNum", "like_num")
COMMENT_KEYS = ("commentCount", "comment_count", "comments", "commentNum", "comment_num")
TEXT_KEYS = (
    "caption", "originCaption", "title", "description", "desc", "text",
    "videoCaption", "photoCaption",
)
COVER_KEYS = (
    "coverUrl", "cover_url", "cover", "thumbnailUrl", "thumbnail_url", "thumbnail",
    "poster", "animatedCoverUrl", "animated_cover_url",
)
MEDIA_KEYS = (
    "photoUrl", "photo_url", "playUrl", "play_url", "videoUrl", "video_url",
    "downloadUrl", "download_url", "src",
)
PAGE_URL_KEYS = ("shareUrl", "share_url", "webUrl", "web_url", "permalink", "detailUrl", "detail_url")
COUNT_RE = re.compile(r"^\s*([0-9][0-9.,]*)(?:\s*)(k|m|b|mil|mi|milh[aã]o|milh[oõ]es)?\s*$", re.I)
HASHTAG_RE = re.compile(r"#[\wÀ-ÿ]+", re.UNICODE)
VIDEO_PAGE_RE = re.compile(r"/(?:video|photo|short-video|shortvideo|play)/[^/?#]+", re.I)


@dataclass
class VideoItem:
    key: str
    photo_id: str | None
    permalink: str | None
    views: int
    likes: int | None
    comments: int | None
    title: str
    hashtags: list[str]
    thumbnail: str | None
    media_url: str | None
    source: str
    link_source: str | None
    page: int | None
    link_attempts: int = 0
    link_error: str | None = None
    duration_seconds: float | None = None

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FeedCapture:
    url: str
    method: str
    headers: dict[str, str]
    body: str
    response_text: str
    source: str
    precision_safe: bool = True
    status: int | None = None


class KwaiError(RuntimeError):
    pass


class VideoIndex(dict[str, VideoItem]):
    """Keep identity lookups constant-time for large profile collections."""

    def __init__(self):
        super().__init__()
        self.ids: dict[str, set[str]] = {}
        self.links: dict[str, set[str]] = {}
        self.thumbnails: dict[str, set[str]] = {}

    def _index(self, key: str, item: VideoItem, remove: bool = False):
        for index, value in ((self.ids, item.photo_id), (self.links, item.permalink),
                             (self.thumbnails, thumb_fingerprint(item.thumbnail))):
            if not value:
                continue
            if remove:
                keys = index.get(value, set())
                keys.discard(key)
                if not keys:
                    index.pop(value, None)
            else:
                index.setdefault(value, set()).add(key)

    def __setitem__(self, key, item):
        if key in self:
            self._index(key, self[key], remove=True)
        super().__setitem__(key, item)
        self._index(key, item)

    def pop(self, key, default=None):
        if key in self:
            self._index(key, self[key], remove=True)
        return super().pop(key, default)

    def candidates(self, item: VideoItem):
        keys = set()
        if item.key in self:
            keys.add(item.key)
        for index, value in ((self.ids, item.photo_id), (self.links, item.permalink),
                             (self.thumbnails, thumb_fingerprint(item.thumbnail))):
            keys.update(index.get(value, ()))
        return [(key, self[key]) for key in sorted(keys)]


def is_profile_feed_url(url: str) -> bool:
    try:
        u = urlparse(url)
        return bool(u.hostname) and (u.hostname == "kwai.com" or u.hostname.endswith(".kwai.com")) and u.path == PROFILE_FEED_PATH
    except Exception:
        return False


def is_video_page_url(value: str | None) -> bool:
    if not value:
        return False
    try:
        u = urlparse(value)
        return bool(u.hostname) and (u.hostname == "kwai.com" or u.hostname.endswith(".kwai.com")) and bool(VIDEO_PAGE_RE.search(u.path))
    except Exception:
        return False


def clean_video_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        u = urlparse(value)
        if u.scheme not in {"http", "https"} or not u.hostname or not (u.hostname == "kwai.com" or u.hostname.endswith(".kwai.com")):
            return None
        if not VIDEO_PAGE_RE.search(u.path):
            return None
        return f"{u.scheme or 'https'}://{u.netloc}{u.path}"
    except Exception:
        return None


def _username_from_profile_url(value: str) -> str | None:
    try:
        u = urlparse(value)
        path = unquote(u.path or "")
        m = re.search(r"(?:^|/)@([A-Za-z0-9._-]{2,80})(?:/|$)", path)
        if m:
            return m.group(1)
        for key, val in parse_qsl(u.query, keep_blank_values=True):
            if key in {"authorKwaiId", "share_item_info"}:
                val = unquote(val or "").strip().lstrip("@")
                if re.fullmatch(r"[A-Za-z0-9._-]{2,80}", val):
                    return val
    except Exception:
        pass
    return None


def normalize_profile_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise KwaiError("Informe a URL do perfil do Kwai.")
    if not re.match(r"^https?://", value, re.I):
        if re.match(r"^(?:[a-z0-9-]+\.)?kwai\.com/", value, re.I):
            value = "https://" + value
        else:
            value = "https://www.kwai.com/@" + value.lstrip("/@")
    u = urlparse(value)
    if not u.hostname or not (u.hostname == "kwai.com" or u.hostname.endswith(".kwai.com")):
        raise KwaiError("A URL precisa pertencer ao domínio kwai.com.")
    username = _username_from_profile_url(value)
    if username:
        return f"https://www.kwai.com/@{username}"
    if not u.path or u.path == "/":
        raise KwaiError("Informe um perfil, por exemplo https://www.kwai.com/@usuario")
    return value


def normalize_hashtag(value: str | None) -> str | None:
    text = (value or "").strip().lstrip("#").strip()
    if not text:
        return None
    text = re.split(r"\s+", text, maxsplit=1)[0]
    text = re.sub(r"[^\wÀ-ÿ]", "", text, flags=re.UNICODE)
    return f"#{text.lower()}" if text else None


def normalize_hashtags(value: str | None) -> frozenset[str]:
    return frozenset(
        tag for token in re.split(r"[\s,;]+", value or "")
        if (tag := normalize_hashtag(token))
    )


def parse_count(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, round(value))
    text = str(value).replace("\xa0", " ").strip().lower()
    m = COUNT_RE.match(text)
    if not m:
        try:
            return max(0, int(float(text.replace(".", "").replace(",", "."))))
        except Exception:
            return None
    raw, suffix = m.group(1), (m.group(2) or "").lower()
    if suffix:
        if "," in raw and "." in raw:
            decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
            thousand = "." if decimal == "," else ","
            raw = raw.replace(thousand, "").replace(decimal, ".")
        else:
            raw = raw.replace(",", ".")
    else:
        raw = re.sub(r"(?<=\d)[.,](?=\d{3}(?:\D|$))", "", raw).replace(",", ".")
    try:
        base = float(raw)
    except ValueError:
        return None
    mult = 1
    if suffix in ("k", "mil"):
        mult = 1_000
    elif suffix in ("m", "mi") or suffix.startswith("milh"):
        mult = 1_000_000
    elif suffix == "b":
        mult = 1_000_000_000
    return max(0, round(base * mult))


def first_value(obj: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def _all_dicts(node: Any, max_depth: int = 7):
    stack = [(node, 0)]
    seen: set[int] = set()
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(current, dict):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
            yield current
            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(current, list):
            for value in current:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))


def extract_feeds(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        direct = data.get("feeds")
        if isinstance(direct, list):
            return [x for x in direct if isinstance(x, dict)]
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ("feeds", "list", "items"):
                value = nested.get(key)
                if isinstance(value, list) and any(isinstance(x, dict) and ("photo_id" in x or "photoId" in x) for x in value):
                    return [x for x in value if isinstance(x, dict)]
    best: list[dict[str, Any]] = []
    for obj in _all_dicts(data, max_depth=6):
        for value in obj.values():
            if not isinstance(value, list):
                continue
            candidates = [x for x in value if isinstance(x, dict) and ("photo_id" in x or "photoId" in x)]
            if len(candidates) > len(best):
                best = candidates
    return best


def find_cursor(data: Any) -> tuple[str, str] | None:
    best: tuple[int, str, str] | None = None
    stack = [(data, 0)]
    seen: set[int] = set()
    while stack:
        node, depth = stack.pop()
        if depth > 10:
            continue
        if isinstance(node, dict):
            marker = id(node)
            if marker in seen:
                continue
            seen.add(marker)
            has_feed = any(isinstance(node.get(k), list) for k in ("feeds", "items", "list", "videos", "posts"))
            for key in CURSOR_KEYS:
                if key in node and not isinstance(node[key], (dict, list)) and node[key] is not None:
                    value = str(node[key])
                    score = (100 if has_feed else 30) + (25 if "next" in key.lower() else 0) + (15 if key == "pcursor" else 0) - depth
                    if best is None or score > best[0]:
                        best = (score, key, value)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
    return (best[1], best[2]) if best else None


def is_end_cursor(value: str | None) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in {"", "null", "none", "end", "ended", "no_more", "nomore", "-1"}


def _try_set_cursor(target: Any, cursor: str) -> bool:
    if not isinstance(target, dict):
        return False
    for key in REQUEST_CURSOR_KEYS:
        if key in target:
            target[key] = cursor
            return True
    return False


def _set_cursor_like_extension(obj: Any, cursor: str) -> bool:
    if not isinstance(obj, dict):
        return False
    if _try_set_cursor(obj.get("variables"), cursor):
        return True
    if _try_set_cursor(obj, cursor):
        return True
    visited: set[int] = set()

    def deep(node: Any, depth: int) -> bool:
        if not isinstance(node, dict) or depth > 5 or id(node) in visited:
            return False
        visited.add(id(node))
        if _try_set_cursor(node, cursor):
            return True
        for key in ("data", "params", "input", "payload", "request", "queryVariables"):
            if deep(node.get(key), depth + 1):
                return True
        return False

    if deep(obj, 0):
        return True
    if re.search(r"visionProfilePhotoList", str(obj.get("operationName", "")), re.I):
        if not isinstance(obj.get("variables"), dict):
            obj["variables"] = {}
        obj["variables"]["pcursor"] = cursor
        return True
    return False


def build_replay(url: str, body: str, cursor: str) -> tuple[str, str, bool]:
    body = body or ""
    changed = False
    new_body = body
    if body:
        try:
            parsed = json.loads(body)
            changed = _set_cursor_like_extension(parsed, cursor)
            new_body = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            try:
                pairs = parse_qsl(body, keep_blank_values=True)
                if pairs:
                    out: list[tuple[str, str]] = []
                    for key, value in pairs:
                        if not changed and key in REQUEST_CURSOR_KEYS:
                            out.append((key, cursor))
                            changed = True
                        else:
                            out.append((key, value))
                    new_body = urlencode(out)
            except Exception:
                pass
    new_url = url
    if not changed:
        try:
            u = urlparse(url)
            pairs = parse_qsl(u.query, keep_blank_values=True)
            out = []
            for key, value in pairs:
                if not changed and key in REQUEST_CURSOR_KEYS:
                    out.append((key, cursor))
                    changed = True
                else:
                    out.append((key, value))
            if changed:
                new_url = u._replace(query=urlencode(out)).geturl()
        except Exception:
            pass
    return new_url, new_body, changed


def capture_starts_at_beginning(capture: FeedCapture) -> bool:
    values = [v for k, v in parse_qsl(urlparse(capture.url).query, keep_blank_values=True) if k in REQUEST_CURSOR_KEYS]
    if capture.body:
        try:
            body = json.loads(capture.body)
            values.extend(obj[k] for obj in _all_dicts(body) for k in REQUEST_CURSOR_KEYS if k in obj)
        except (ValueError, TypeError):
            values.extend(v for k, v in parse_qsl(capture.body, keep_blank_values=True) if k in REQUEST_CURSOR_KEYS)
    return all(v is None or str(v).strip() in {"", "0"} for v in values)


def _valid_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = html.unescape(value).strip()
    return value if value.startswith(("http://", "https://")) else None


def _find_url_by_keys(feed: dict[str, Any], keys: Iterable[str]) -> str | None:
    for obj in [feed, *_all_dicts(feed, max_depth=5)]:
        value = first_value(obj, keys)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                url = _valid_http_url(item)
                if url:
                    return url
            elif isinstance(item, dict):
                for k in ("url", "src", "playUrl", "play_url"):
                    url = _valid_http_url(item.get(k))
                    if url:
                        return url
    return None


def _find_media_url(feed: dict[str, Any]) -> str | None:
    keyed = _find_url_by_keys(feed, MEDIA_KEYS)
    if keyed and re.search(r"\.(?:mp4|m3u8|mov|webm)(?:[?#]|$)", keyed, re.I):
        return keyed
    for obj in _all_dicts(feed, max_depth=7):
        for value in obj.values():
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str):
                    url = _valid_http_url(item)
                    if url and re.search(r"\.(?:mp4|m3u8|mov|webm)(?:[?#]|$)", url, re.I):
                        return url
    return keyed


def _find_thumbnail(feed: dict[str, Any]) -> str | None:
    keyed = _find_url_by_keys(feed, COVER_KEYS)
    if keyed:
        return keyed
    for obj in _all_dicts(feed, max_depth=5):
        for value in obj.values():
            if isinstance(value, str):
                url = _valid_http_url(value)
                if url and re.search(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", url, re.I):
                    return url
    return None


def _find_page_url(feed: dict[str, Any]) -> str | None:
    value = _find_url_by_keys(feed, PAGE_URL_KEYS)
    return clean_video_url(value)


def parse_duration(value: Any, scale: float = 1) -> float | None:
    """Normalize known units; absent/invalid durations remain unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value) / scale
        return seconds if math.isfinite(seconds) and seconds > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def feed_duration(feed: dict[str, Any]) -> float | None:
    # Only inspect video metadata, never nested music/ad durations.
    for obj in (feed, feed.get("photo"), feed.get("video")):
        if not isinstance(obj, dict):
            continue
        for key, scale in (("duration_seconds", 1), ("durationSeconds", 1),
                           ("duration_ms", 1000), ("durationMs", 1000),
                           ("photo_duration", 1000), ("photoDuration", 1000)):
            duration = parse_duration(obj.get(key), scale)
            if duration is not None:
                return duration
        # Real Kwai feeds put milliseconds in the video manifest. Do not
        # recursively inspect every `duration`: music/recommendations differ.
        manifest = obj.get("transcode_manifest_info") or obj.get("transcodeManifestInfo")
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except (ValueError, TypeError):
                manifest = None
        if isinstance(manifest, dict):
            adaptations = manifest.get("adaptationSet")
            for adaptation in adaptations if isinstance(adaptations, list) else []:
                if isinstance(adaptation, dict):
                    duration = parse_duration(adaptation.get("duration"), 1000)
                    if duration is not None:
                        return duration
        extra = obj.get("ext_params")
        if isinstance(extra, dict):
            duration = parse_duration(extra.get("video"), 1000)
            if duration is not None:
                return duration
    return None


def parse_feed_video(feed: dict[str, Any], page_number: int, precision_safe: bool = True) -> VideoItem | None:
    raw_id = feed.get("photo_id", feed.get("photoId"))
    if raw_id is None:
        return None
    photo_id = str(raw_id).strip()
    if not re.fullmatch(r"\d{12,24}", photo_id):
        return None
    views = parse_count(first_value(feed, VIEW_KEYS))
    if views is None:
        return None
    likes = parse_count(first_value(feed, LIKE_KEYS))
    comments = parse_count(first_value(feed, COMMENT_KEYS))
    title = html.unescape(str(first_value(feed, TEXT_KEYS) or "")).strip()
    hashtags = sorted({tag.lower() for tag in HASHTAG_RE.findall(title)})
    explicit = _find_page_url(feed)
    # Only synthesize /video/<photo_id> when the response body is known to be raw text.
    # JS responseType=json can round huge IDs before Python ever sees them.
    permalink = explicit or (f"https://www.kwai.com/video/{photo_id}" if precision_safe else None)
    return VideoItem(
        key=f"id:{photo_id}", photo_id=photo_id if precision_safe else None,
        permalink=permalink, views=views, likes=likes, comments=comments,
        title=title, hashtags=hashtags, thumbnail=_find_thumbnail(feed),
        media_url=_find_media_url(feed), source="feed-api", link_source="photo_id" if permalink and not explicit else ("feed-url" if explicit else None),
        page=page_number, duration_seconds=feed_duration(feed),
    )


def parse_dom_video(raw: dict[str, Any]) -> VideoItem | None:
    try:
        views = int(raw.get("views"))
    except Exception:
        return None
    key = str(raw.get("key") or "").strip()
    if not key:
        return None
    raw_id = str(raw.get("id") or "").strip()
    photo_id = raw_id if re.fullmatch(r"\d{12,24}", raw_id) else None
    url = clean_video_url(raw.get("url"))
    if not url and photo_id:
        url = f"https://www.kwai.com/video/{photo_id}"
    title = str(raw.get("title") or "").strip()
    tags = [normalize_hashtag(x) for x in (raw.get("hashtags") or [])]
    tags = sorted({x for x in tags if x})
    return VideoItem(
        key=key, photo_id=photo_id, permalink=url, views=views, likes=None, comments=None,
        title=title, hashtags=tags, thumbnail=_valid_http_url(raw.get("thumbnail")), media_url=None,
        source="dom-card", link_source="dom" if url else ("dom-id" if photo_id else None), page=None,
        duration_seconds=parse_duration(raw.get("duration_seconds")),
    )


def thumb_fingerprint(url: str | None) -> str | None:
    if not url:
        return None
    try:
        u = urlparse(url)
        return f"{u.scheme}://{u.netloc}{u.path}"
    except Exception:
        return url.split("?", 1)[0]


def sort_items(items: list[VideoItem], sort_by: str) -> list[VideoItem]:
    if sort_by == "likes":
        return sorted(items, key=lambda x: (x.likes is not None, x.likes if x.likes is not None else -1, x.views), reverse=True)
    if sort_by == "comments":
        return sorted(items, key=lambda x: (x.comments is not None, x.comments if x.comments is not None else -1, x.views), reverse=True)
    return sorted(items, key=lambda x: x.views, reverse=True)


def safe_replay_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = re.compile(r"^(?::|cookie|host|content-length|origin|referer|user-agent|accept-encoding|connection|sec-)", re.I)
    return {k: str(v) for k, v in headers.items() if v is not None and not blocked.match(k)}


class KwaiAnalyzerService:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.data_dir = base_dir / "data"
        self.download_dir = base_dir / "downloads"
        self.browser_profile_dir = self.data_dir / "browser-profile"
        self.browser_engine_path = base_dir / "app" / "browser_engine.js"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)

        self._pw = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.resolver_page: Page | None = None
        self.cdp: CDPSession | None = None
        self._cdp_page: Page | None = None
        self._cdp_requests: dict[str, dict[str, Any]] = {}
        self._feed_queue: asyncio.Queue[FeedCapture] = asyncio.Queue(maxsize=32)
        self._cdp_tasks: set[asyncio.Task] = set()
        self.task: asyncio.Task | None = None
        self.link_task: asyncio.Task | None = None
        self.download_task: asyncio.Task | None = None
        self.cancel_event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.results: list[VideoItem] = []
        self.last_profile_url: str | None = None
        self.status: dict[str, Any] = self._empty_status()
        self.download_status: dict[str, Any] = self._empty_download_status()

    def _empty_status(self) -> dict[str, Any]:
        return {
            "running": False, "browser_open": False, "phase": None,
            "message": "Pronto.", "engine": None, "pages": 0, "scanned": 0,
            "matched": 0, "links": 0, "link_target": 0, "cursor": None,
            "links_pending": 0, "link_rounds": 0, "link_warning": None,
            "api_end_observed": False, "end_evidence": None,
            "reached_end": False, "stop_reason": None, "rejected_feeds": 0, "error": None, "started_at": None, "finished_at": None,
            "profile_url": None, "requested_profile_url": None, "resolved_profile_url": None,
            "current_browser_url": None, "feed_requests_seen": 0, "feed_responses_seen": 0,
            "feed_source": None, "feed_precision_safe": None, "api_last_reason": None,
            "network_requests_seen": 0, "network_responses_seen": 0,
            "dom_rounds": 0, "dom_scroll_target": None, "dom_seen": 0,
            "network_samples": [], "feed_samples": [],
        }

    def _empty_download_status(self) -> dict[str, Any]:
        return {"running": False, "total": 0, "completed": 0, "failed": 0, "current": None, "message": "Nenhum download em andamento.", "items": []}

    def _remember_network_sample(self, kind: str, url: str) -> None:
        try:
            u = urlparse(url)
            if not u.hostname or "kwai" not in u.hostname.lower():
                return
            row = {"kind": kind, "host": u.hostname, "path": u.path}
            samples = self.status.setdefault("network_samples", [])
            if row not in samples and len(samples) < 40:
                samples.append(row)
        except Exception:
            pass

    async def ensure_browser(self, open_url: str | None = None) -> dict[str, Any]:
        async with self.lock:
            if self.context is None:
                if self._pw is None:
                    self._pw = await async_playwright().start()
                launch_kwargs = dict(
                    user_data_dir=str(self.browser_profile_dir), headless=False,
                    viewport={"width": 1440, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                errors = []
                for channel in ("chrome", "msedge"):
                    try:
                        self.context = await self._pw.chromium.launch_persistent_context(channel=channel, **launch_kwargs)
                        break
                    except Exception as exc:
                        errors.append(f"{channel}: {exc}")
                if self.context is None:
                    try:
                        self.context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
                    except Exception as exc:
                        errors.append(f"playwright: {exc}")
                        await self._pw.stop()
                        self._pw = None
                        raise KwaiError("Não foi possível abrir Chrome/Edge controlado pelo app.") from exc
                active_context = self.context
                def on_context_closed(*_):
                    if self.context is active_context:
                        self.context = None
                        self.page = None
                        self.resolver_page = None
                        self.cdp = None
                        self._cdp_page = None
                        self.status["browser_open"] = False
                self.context.on("close", on_context_closed)
                # The extension worked because its hook ran at document_start. Do the same here.
                if self.browser_engine_path.exists():
                    await self.context.add_init_script(path=str(self.browser_engine_path))
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            if self.page is None or self.page.is_closed():
                self.page = await self.context.new_page()
            await self._ensure_cdp()
            normalized = None
            if open_url:
                try:
                    normalized = normalize_profile_url(open_url)
                except Exception:
                    normalized = open_url
                try:
                    await self.page.goto(normalized, wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    pass
            self.status["browser_open"] = True
            self.status["current_browser_url"] = self.page.url
            return {"ok": True, "browser_open": True, "url": self.page.url, "profile_url": normalized or self.page.url}

    async def _ensure_cdp(self) -> None:
        if self.context is None or self.page is None:
            return
        if self.cdp is not None and self._cdp_page is self.page and not self.page.is_closed():
            return
        try:
            if self.cdp:
                await self.cdp.detach()
        except Exception:
            pass
        self.cdp = await self.context.new_cdp_session(self.page)
        self._cdp_page = self.page
        await self.cdp.send("Network.enable", {"maxTotalBufferSize": 100_000_000, "maxResourceBufferSize": 20_000_000})

        def on_request(params: dict[str, Any]):
            req = params.get("request") or {}
            url = str(req.get("url") or "")
            self.status["network_requests_seen"] = int(self.status.get("network_requests_seen") or 0) + 1
            self._remember_network_sample("request", url)
            if not is_profile_feed_url(url):
                return
            self.status["feed_requests_seen"] = int(self.status.get("feed_requests_seen") or 0) + 1
            request_id = str(params.get("requestId") or "")
            self._cdp_requests[request_id] = {
                "url": url, "method": str(req.get("method") or "POST"),
                "headers": {str(k): str(v) for k, v in (req.get("headers") or {}).items()},
                "body": str(req.get("postData") or ""), "response_status": None,
            }
            if req.get("hasPostData") and not req.get("postData"):
                task = asyncio.create_task(self._get_post_data(request_id))
                self._track_task(task)

        def on_response(params: dict[str, Any]):
            resp = params.get("response") or {}
            url = str(resp.get("url") or "")
            self.status["network_responses_seen"] = int(self.status.get("network_responses_seen") or 0) + 1
            self._remember_network_sample("response", url)
            if not is_profile_feed_url(url):
                return
            self.status["feed_responses_seen"] = int(self.status.get("feed_responses_seen") or 0) + 1
            request_id = str(params.get("requestId") or "")
            if request_id in self._cdp_requests:
                self._cdp_requests[request_id]["response_status"] = int(resp.get("status") or 0)

        def on_loading_finished(params: dict[str, Any]):
            request_id = str(params.get("requestId") or "")
            if request_id in self._cdp_requests:
                task = asyncio.create_task(self._collect_cdp_body(request_id))
                self._track_task(task)

        self.cdp.on("Network.requestWillBeSent", on_request)
        self.cdp.on("Network.responseReceived", on_response)
        self.cdp.on("Network.loadingFinished", on_loading_finished)

    def _track_task(self, task: asyncio.Task) -> None:
        self._cdp_tasks.add(task)
        task.add_done_callback(lambda t: self._cdp_tasks.discard(t))

    async def _get_post_data(self, request_id: str) -> None:
        if not self.cdp or request_id not in self._cdp_requests:
            return
        try:
            data = await self.cdp.send("Network.getRequestPostData", {"requestId": request_id})
            self._cdp_requests[request_id]["body"] = str(data.get("postData") or "")
        except Exception:
            pass

    async def _collect_cdp_body(self, request_id: str) -> None:
        if not self.cdp:
            return
        meta = self._cdp_requests.pop(request_id, None)
        if not meta:
            return
        try:
            if not meta.get("body") and str(meta.get("method") or "").upper() not in {"GET", "HEAD"}:
                try:
                    post = await self.cdp.send("Network.getRequestPostData", {"requestId": request_id})
                    meta["body"] = str(post.get("postData") or "")
                except Exception:
                    pass
            body_data = await self.cdp.send("Network.getResponseBody", {"requestId": request_id})
            text = str(body_data.get("body") or "")
            if body_data.get("base64Encoded"):
                text = base64.b64decode(text).decode("utf-8", "replace")
            if not text:
                return
            capture = FeedCapture(
                url=meta["url"], method=meta["method"], headers=meta["headers"], body=meta.get("body") or "",
                response_text=text, source="cdp-network", precision_safe=True, status=meta.get("response_status"),
            )
            if self._feed_queue.full():
                self._feed_queue.get_nowait()
            self._feed_queue.put_nowait(capture)
            self.status["feed_source"] = "cdp-network"
            self.status["feed_precision_safe"] = True
        except Exception as exc:
            samples = self.status.setdefault("feed_samples", [])
            if len(samples) < 10:
                samples.append({"source": "cdp-network", "error": type(exc).__name__})

    async def _take_hook_feed(self) -> FeedCapture | None:
        if self.page is None or self.page.is_closed():
            return None
        try:
            raw = await self.page.evaluate("() => window.__KPA?.takeFeed?.() || null")
        except Exception:
            return None
        if not isinstance(raw, dict) or not raw.get("responseText"):
            return None
        headers = {}
        for pair in raw.get("headers") or []:
            if isinstance(pair, list) and len(pair) >= 2:
                headers[str(pair[0])] = str(pair[1])
        source = str(raw.get("source") or "page-hook")
        return FeedCapture(
            url=str(raw.get("url") or ""), method=str(raw.get("method") or "POST"), headers=headers,
            body=str(raw.get("bodyText") or ""), response_text=str(raw.get("responseText") or ""),
            source=source, precision_safe=(source != "xhr-json-fallback"), status=200,
        )

    def _drain_feed_queue(self) -> None:
        while True:
            try:
                self._feed_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _wait_feed_capture(self, seconds: float) -> FeedCapture | None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.cancel_event.is_set():
                return None
            try:
                cap = self._feed_queue.get_nowait()
                if cap.response_text:
                    return cap
            except asyncio.QueueEmpty:
                pass
            hook = await self._take_hook_feed()
            if hook and hook.response_text:
                self.status["feed_source"] = hook.source
                self.status["feed_precision_safe"] = hook.precision_safe
                return hook
            await asyncio.sleep(0.08)
        return None

    async def _capture_first_feed(self, profile_url: str) -> FeedCapture | None:
        await self.ensure_browser()
        assert self.page is not None
        await self._ensure_cdp()
        page = self.page
        self._drain_feed_queue()
        self._cdp_requests.clear()
        self.status.update({
            "network_requests_seen": 0, "network_responses_seen": 0, "feed_requests_seen": 0,
            "feed_responses_seen": 0, "feed_source": None, "feed_precision_safe": None,
            "network_samples": [], "feed_samples": [], "dom_rounds": 0,
        })
        try:
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=10_000)
        except Exception:
            pass
        self.status["message"] = "Abrindo o perfil com o hook equivalente à extensão…"
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=45_000)
        self.status["current_browser_url"] = page.url
        self.status["message"] = "Perfil aberto. Capturando fetch/XHR + Network antes de usar o DOM…"
        capture = await self._wait_feed_capture(2.8)
        if capture:
            return capture

        # Same idea as the extension Turbo fallback: jump to the end of the current batch
        # to force the site's infinite-scroll request, while our hook is already installed.
        try:
            info = await page.evaluate("() => window.__KPA_DOM?.chooseScrollTarget?.() || null")
            if isinstance(info, dict):
                self.status["dom_scroll_target"] = info
        except Exception:
            pass
        for step in range(1, 4):
            if self.cancel_event.is_set():
                return None
            self.status["message"] = f"Feed real ainda não capturado. Disparando lote do perfil… {step}/3"
            try:
                await page.evaluate("() => window.__KPA_DOM?.scrollBottom?.() || null")
            except Exception:
                try:
                    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
            capture = await self._wait_feed_capture(1.0)
            if capture:
                return capture
        return None

    async def _replay_feed(self, capture: FeedCapture, cursor: str) -> tuple[str, FeedCapture]:
        assert self.page is not None
        url, body, changed = build_replay(capture.url, capture.body, cursor)
        if not changed:
            raise KwaiError("O primeiro lote foi capturado, mas o cursor não existe no POST/URL para paginação direta.")
        payload = {"url": url, "method": capture.method or "POST", "headers": safe_replay_headers(capture.headers), "body": body}
        result = await self.page.evaluate(
            """
            async (p) => {
              const controller = new AbortController();
              const timeout = setTimeout(() => controller.abort(), 15000);
              try {
                const init={method:p.method||'POST',headers:p.headers||{},credentials:'include',cache:'no-store',signal:controller.signal};
                if(!/^(GET|HEAD)$/i.test(init.method) && p.body) init.body=p.body;
                const r=await fetch(p.url,init);
                return {ok:r.ok,status:r.status,text:await r.text()};
              } catch(e) { return {ok:false,status:0,text:'',error:String(e&&e.message||e)}; }
              finally { clearTimeout(timeout); }
            }
            """, payload,
        )
        if not isinstance(result, dict) or not result.get("ok") or not result.get("text"):
            raise KwaiError(str((result or {}).get("error") or f"HTTP {(result or {}).get('status', 0)} ao paginar o feed."))
        next_cap = FeedCapture(url=url, method=capture.method, headers=capture.headers, body=body, response_text=str(result["text"]), source="python-replay", precision_safe=True, status=int(result.get("status") or 200))
        return str(result["text"]), next_cap

    def _match_item(self, item: VideoItem, hashtag: str | None, min_views: int) -> bool:
        tags = normalize_hashtags(hashtag)
        return item.views >= min_views and (not tags or not tags.isdisjoint(item.hashtags))

    def _merge_item(self, target: dict[str, VideoItem], item: VideoItem) -> None:
        # Never merge distinct identities merely because captions or counts match.
        candidates = target.candidates(item) if isinstance(target, VideoIndex) else target.items()
        existing_key = item.key if item.key in target else None
        if item.photo_id:
            for k, old in candidates:
                if old.photo_id == item.photo_id:
                    existing_key = k; break
        if existing_key is None and item.permalink:
            for k, old in candidates:
                if old.permalink and old.permalink == item.permalink:
                    existing_key = k; break
        fp = thumb_fingerprint(item.thumbnail)
        if existing_key is None and fp:
            for k, old in candidates:
                if (not old.photo_id or not item.photo_id) and not (old.permalink and item.permalink and old.permalink != item.permalink) and thumb_fingerprint(old.thumbnail) == fp:
                    existing_key = k; break
        if existing_key is None:
            target[item.key] = item
            return
        old = target[existing_key]
        # A DOM card must never overwrite an API video's cover with a different card.
        if old.source == "feed-api" and item.source != "feed-api" and old.thumbnail and item.thumbnail and thumb_fingerprint(old.thumbnail) != thumb_fingerprint(item.thumbnail):
            return
        merged = VideoItem(
            key=(f"id:{item.photo_id}" if item.photo_id else old.key), photo_id=item.photo_id or old.photo_id,
            permalink=item.permalink or old.permalink, views=max(old.views, item.views),
            likes=item.likes if item.likes is not None else old.likes,
            comments=item.comments if item.comments is not None else old.comments,
            title=item.title or old.title, hashtags=sorted(set(old.hashtags) | set(item.hashtags)),
            thumbnail=item.thumbnail or old.thumbnail, media_url=item.media_url or old.media_url,
            source=item.source if item.source == "feed-api" else old.source,
            link_source=item.link_source or old.link_source, page=item.page if item.page is not None else old.page,
            link_attempts=max(old.link_attempts, item.link_attempts),
            duration_seconds=(item.duration_seconds if item.duration_seconds is not None and
                              (item.source == "feed-api" or old.duration_seconds is None)
                              else old.duration_seconds),
            link_error=None if item.permalink or old.permalink else item.link_error or old.link_error,
        )
        if merged.key != existing_key:
            target.pop(existing_key, None)
        target[merged.key] = merged

    async def start_analysis(self, profile_url: str, limit: int, min_views: int, hashtag: str | None, sort_by: str, max_pages: int = 500, top_n: int = 20, auto_links: bool = True, turbo: bool = True):
        if any(t and not t.done() for t in (self.task, self.link_task)):
            raise KwaiError("Aguarde ou interrompa a operação atual antes de iniciar outra análise.")
        requested = profile_url
        profile_url = normalize_profile_url(profile_url)
        self.cancel_event = asyncio.Event()
        self.status.update({"running": True, "phase": "scan"})
        self.task = asyncio.create_task(self._analyze(profile_url, requested, max(1, min(int(limit), 20_000)), max(0, int(min_views)), hashtag, sort_by, max(1, min(int(max_pages), 2000)), max(1, min(int(top_n), 1000)), bool(auto_links), bool(turbo)))

    async def _analyze(self, profile_url: str, requested: str, limit: int, min_views: int, hashtag: str | None, sort_by: str, max_pages: int, top_n: int, auto_links: bool, turbo: bool):
        self.results = []
        self.last_profile_url = profile_url
        self.status = self._empty_status()
        self.status.update({"running": True, "phase": "scan", "browser_open": self.context is not None, "message": "Preparando análise híbrida…", "engine": "hybrid", "started_at": time.time(), "profile_url": profile_url, "requested_profile_url": requested, "resolved_profile_url": profile_url, "link_target": min(top_n, limit), "limit": limit})
        seen = VideoIndex()
        matched = VideoIndex()
        api_ok = False
        api_reason = None
        try:
            capture = await self._capture_first_feed(profile_url) if turbo else None
            if capture and not capture_starts_at_beginning(capture):
                try:
                    _, initial_capture = await self._replay_feed(capture, "")
                    initial_data = json.loads(initial_capture.response_text)
                    if extract_feeds(initial_data):
                        capture = initial_capture
                except Exception:
                    pass
            if capture:
                self.status["engine"] = "turbo-api"
                self.status["feed_source"] = capture.source
                self.status["feed_precision_safe"] = capture.precision_safe
                self.status["message"] = f"Feed capturado por {capture.source}. Paginando como a extensão Turbo…"
                text = capture.response_text
                current_capture = capture
                previous_cursor = None
                repeated = 0
                stagnant = 0
                for page_number in range(1, max_pages + 1):
                    if self.cancel_event.is_set():
                        break
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError as exc:
                        api_reason = f"json-inválido-página-{page_number}"
                        break
                    feeds = extract_feeds(data)
                    # Uma captura sem feeds não é uma página vazia do perfil: é captura
                    # falsa/incompatível. A extensão v0.14 funcionava melhor justamente
                    # porque, nessa situação, continuava pelo DOM em vez de declarar fim.
                    if not feeds:
                        api_reason = f"empty-or-false-feed-page-{page_number}"
                        break
                    fresh = 0
                    for feed in feeds:
                        item = parse_feed_video(feed, page_number, precision_safe=current_capture.precision_safe)
                        if not item:
                            self.status["rejected_feeds"] += 1
                            continue
                        before = len(seen)
                        self._merge_item(seen, item)
                        if len(seen) > before:
                            fresh += 1
                        if self._match_item(item, hashtag, min_views):
                            self._merge_item(matched, item)
                    cursor_info = find_cursor(data)
                    next_cursor = cursor_info[1] if cursor_info else None
                    self.status.update({"pages": page_number, "scanned": len(seen), "matched": len(matched), "cursor": next_cursor, "message": f"Turbo/API: {len(matched)}/{limit} no ranking • {len(seen)} vídeos • página {page_number}."})
                    self.results = sort_items(list(matched.values()), sort_by)[:limit]
                    if len(matched) >= limit:
                        api_ok = True; api_reason = "limit-reached"; break
                    if not feeds or fresh == 0:
                        stagnant += 1
                    else:
                        stagnant = 0
                    if stagnant >= 2:
                        api_reason = "stagnant"; break
                    if cursor_info is None:
                        api_reason = "missing-cursor"
                        break
                    if is_end_cursor(next_cursor):
                        self.status["api_end_observed"] = bool(seen) and not self.status["rejected_feeds"]
                        # Só aceitamos "fim" depois de ler ao menos um vídeo real.
                        if seen and not self.status["rejected_feeds"] and capture_starts_at_beginning(capture):
                            self.status["reached_end"] = True; api_ok = True; api_reason = "end"
                        else:
                            api_reason = "empty-feed-no-cursor"
                        break
                    if previous_cursor is not None and next_cursor == previous_cursor:
                        repeated += 1
                    else:
                        repeated = 0
                    if repeated >= 2:
                        api_reason = "cursor-repeated"; break
                    previous_cursor = next_cursor
                    try:
                        text, current_capture = await self._replay_feed(capture, next_cursor or "")
                    except Exception as exc:
                        api_reason = f"replay-failed:{exc}"
                        break
                    await asyncio.sleep(0.08)
                if len(matched) >= limit or (self.status["reached_end"] and len(seen) > 0):
                    api_ok = True
                self.status["api_last_reason"] = api_reason

            if self.cancel_event.is_set():
                self.status["message"] = "Análise cancelada pelo usuário."
            elif not api_ok and len(matched) < limit:
                # Preserve useful API rows and continue with the exact DOM/card logic that made v0.14 useful.
                self.status["engine"] = "extension-dom"
                self.status["message"] = f"Feed direto indisponível ({api_reason or 'não capturado'}); usando o motor visual da extensão…"
                await self._scan_dom(profile_url, limit, min_views, hashtag, sort_by, seen, matched)

            self.results = sort_items(list(matched.values()), sort_by)[:limit]
            if auto_links and self.results and not self.cancel_event.is_set():
                self.status["phase"] = "links"
                try:
                    await self._resolve_links_internal(top_n)
                except Exception as exc:
                    self.status["link_warning"] = f"Não foi possível concluir os links: {exc}"
            self.status.update({"running": False, "phase": None, "matched": len(self.results), "links": sum(bool(x.permalink) for x in self.results), "finished_at": time.time()})
            reason = ("cancelled" if self.cancel_event.is_set() else "limit-reached" if len(self.results) >= limit else "end" if self.status["reached_end"] else self.status.get("stop_reason") or "unconfirmed")
            self.status["stop_reason"] = reason
            label = {"cancelled": "Coleta interrompida; resultado parcial", "limit-reached": "Limite de coleta atingido; pode haver mais vídeos", "end": "Fim dos vídeos disponíveis na grade", "dom-stalled": "A página parou de carregar; fim do perfil não confirmado", "round-limit": "Limite de tentativas atingido; resultado parcial"}.get(reason, "Coleta parcial; fim do perfil não confirmado")
            self.status["message"] = f"{label}: {len(self.results)} vídeos • {self.status['scanned']} verificados • {self.status['links']} links disponíveis."
            if self.status.get("link_warning"):
                self.status["message"] += " " + self.status["link_warning"]
            self._persist_latest()
        except asyncio.CancelledError:
            self.status.update({"running": False, "phase": None, "message": "Análise cancelada.", "finished_at": time.time()})
            self._persist_latest()
            raise
        except Exception as exc:
            self.status.update({"running": False, "phase": None, "stop_reason": "error", "error": str(exc), "message": f"Coleta parcial devido a erro: {exc}", "finished_at": time.time()})
            self._persist_latest()

    async def _collect_background_feed(self) -> list[VideoItem]:
        """Read the real profile responses produced by scrolling, not only API replay."""
        captures = []
        while not self._feed_queue.empty():
            captures.append(self._feed_queue.get_nowait())
        hook = await self._take_hook_feed()
        if hook:
            captures.append(hook)
        items = []
        for capture in captures:
            if not is_profile_feed_url(capture.url) or not capture.precision_safe:
                continue
            try:
                data = json.loads(capture.response_text)
            except (ValueError, TypeError):
                continue
            feeds = extract_feeds(data)
            parsed = [item for feed in feeds if (item := parse_feed_video(feed, self.status.get("pages", 0), True))]
            cursor = find_cursor(data)
            # Empty explicit feeds with terminal cursor are valid; unrelated/error JSON is not.
            explicit_feeds = isinstance(data, dict) and (isinstance(data.get("feeds"), list) or isinstance(data.get("data"), dict) and isinstance(data["data"].get("feeds"), list))
            if cursor and is_end_cursor(cursor[1]) and len(parsed) == len(feeds) and (parsed or explicit_feeds):
                self.status["api_end_observed"] = True
            items.extend(parsed)
        return items

    def _confirm_dom_end(self, state: dict, stagnant: int, seen_count: int, record: bool = True) -> bool:
        if state.get("loading") or not state.get("atBottom"):
            return False
        evidence = None
        if state.get("marker"):
            evidence = "page-marker"
        elif self.status.get("api_end_observed") and stagnant >= 2:
            evidence = "api-terminal-and-bottom"
        elif seen_count and float(state.get("max") or 0) > 5 and stagnant >= 4:
            evidence = "stable-visible-bottom"
        if not evidence:
            return False
        if record:
            self.status.update({"reached_end": True, "end_evidence": evidence, "stop_reason": "end"})
        return True

    async def _scan_dom(self, profile_url: str, limit: int, min_views: int, hashtag: str | None, sort_by: str, seen: dict[str, VideoItem], matched: dict[str, VideoItem]) -> None:
        await self.ensure_browser()
        assert self.page is not None
        page = self.page
        needs_nav = True
        try:
            if "kwai.com" in (page.url or ""):
                needs_nav = normalize_profile_url(page.url) != profile_url
        except Exception:
            needs_nav = True
        if needs_nav:
            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                pass
        try:
            target_info = await page.evaluate("() => window.__KPA_DOM?.chooseScrollTarget?.() || null")
            self.status["dom_scroll_target"] = target_info
            await page.evaluate("() => window.__KPA_DOM?.scrollTop?.() || null")
        except Exception:
            target_info = None
        await asyncio.sleep(0.18)
        # O content script da extensão rodava em document_idle; no Playwright podemos
        # chegar alguns milissegundos antes dos cards. Aguarda a primeira grade aparecer.
        first_items = []
        for _ in range(100):
            if self.cancel_event.is_set():
                return
            try:
                first_items = await page.evaluate("() => window.__KPA_DOM?.collect?.() || []")
            except Exception:
                first_items = []
            if first_items:
                break
            await asyncio.sleep(0.14)
        await page.evaluate("() => window.__KPA_DOM?.chooseScrollTarget?.() || null")
        stagnant = 0
        for round_no in range(1, 3001):
            if self.cancel_event.is_set():
                return
            try:
                raw_items = await page.evaluate("() => window.__KPA_DOM?.collect?.() || []")
            except Exception as exc:
                raise KwaiError(f"O motor visual da extensão não pôde ler os cards: {exc}") from exc
            for raw in raw_items or []:
                if not isinstance(raw, dict):
                    continue
                item = parse_dom_video(raw)
                if not item:
                    continue
                self._merge_item(seen, item)
                if self._match_item(item, hashtag, min_views):
                    self._merge_item(matched, item)
            for item in await self._collect_background_feed():
                self._merge_item(seen, item)
                if self._match_item(item, hashtag, min_views):
                    self._merge_item(matched, item)
            self.results = sort_items(list(matched.values()), sort_by)[:limit]
            self.status.update({"dom_rounds": round_no, "dom_seen": len(seen), "scanned": len(seen), "matched": len(matched), "message": f"Turbo/rolagem: {len(matched)}/{limit} no ranking • {len(seen)} vídeos vistos • lote {round_no}."})
            if len(matched) >= limit:
                return
            try:
                before = await page.evaluate("() => window.__KPA_DOM?.getScrollInfo?.() || null")
                await page.evaluate("() => window.__KPA_DOM?.scrollBottom?.() || null")
            except Exception:
                before = None
            previous_seen = len(seen)
            previous_height = int((before or {}).get("scrollHeight") or 0)
            grew = False
            end = time.monotonic() + min(2.0 + stagnant, 6.0)
            while time.monotonic() < end:
                if self.cancel_event.is_set():
                    return
                await asyncio.sleep(0.12)
                try:
                    raw_more = await page.evaluate("() => window.__KPA_DOM?.collect?.() || []")
                    info = await page.evaluate("() => window.__KPA_DOM?.getScrollInfo?.() || null")
                except Exception:
                    continue
                for raw in raw_more or []:
                    item = parse_dom_video(raw) if isinstance(raw, dict) else None
                    if not item:
                        continue
                    self._merge_item(seen, item)
                    if self._match_item(item, hashtag, min_views):
                        self._merge_item(matched, item)
                if len(seen) > previous_seen:
                    grew = True
                    break
            if grew:
                stagnant = 0
            else:
                try:
                    info = await page.evaluate("() => window.__KPA_DOM?.getScrollInfo?.() || null")
                    at_bottom = bool(info) and (float(info.get("max") or 0) <= 5 or float(info.get("top") or 0) >= float(info.get("max") or 0) - 4)
                except Exception:
                    at_bottom = True
                stagnant += 1
                state = await page.evaluate("() => window.__KPA_DOM?.endState?.() || {}")
                if self._confirm_dom_end(state, stagnant, len(seen)):
                    return
                self.status["message"] = f"Aguardando mais vídeos ({stagnant}/8 tentativas) • {len(seen)} coletados."
                # Re-select after lazy rendering and nudge upward to retrigger intersection observers.
                await page.evaluate("() => { window.__KPA_DOM?.chooseScrollTarget?.(); return window.__KPA_DOM?.nudge?.(); }")
                if stagnant >= 8:
                    self.status["stop_reason"] = "dom-stalled"
                    return
        self.status["stop_reason"] = "round-limit"

    async def resolve_links(self, top_n: int = 20) -> None:
        if any(t and not t.done() for t in (self.task, self.link_task)):
            raise KwaiError("Aguarde ou interrompa a operação atual antes de coletar links.")
        if not self.results:
            raise KwaiError("Crie o ranking primeiro.")
        self.cancel_event = asyncio.Event()
        self.status.update({"running": True, "phase": "links"})
        self.link_task = asyncio.create_task(self._resolve_links_task(top_n))

    async def _resolve_links_task(self, top_n: int) -> None:
        self.status.update({"running": True, "phase": "links", "error": None})
        try:
            await self._resolve_links_internal(top_n)
            available = sum(bool(x.permalink) for x in self.results[:top_n])
            pending = min(top_n, len(self.results)) - available
            self.status["message"] = f"{'Busca interrompida' if self.cancel_event.is_set() else 'Busca finalizada'}: {available} links no Top • {pending} pendentes."
        except asyncio.CancelledError:
            self.status["message"] = "Busca de links interrompida; progresso salvo."
            raise
        except Exception as exc:
            self.status.update({"error": str(exc), "message": f"Busca de links parcial: {exc}"})
        finally:
            self.status.update({"running": False, "phase": None, "links": sum(bool(x.permalink) for x in self.results), "finished_at": time.time()})
            self._persist_latest()

    async def _capture_card_link(self, page: Page, target: dict) -> dict | None:
        """Listen before clicking; always release hooks and any created popup pages."""
        popups: list[Page] = []
        def on_popup(popup):
            popups.append(popup)
        page.on("popup", on_popup)
        old_url = page.url
        try:
            await page.evaluate("t => window.__KPA?.armLink?.(t)", f"link-{time.time_ns()}")
            try:
                clicked = await page.evaluate("t => window.__KPA_DOM?.clickForTarget?.(t)", target)
            except Exception:
                # Full-page navigation can destroy the execution context before click returns.
                if page.is_closed():
                    return None
                clicked = {"ok": True}
            if not clicked or not clicked.get("ok"):
                return None
            if target.get("realClick") and "x" in clicked:
                await page.mouse.click(clicked["x"], clicked["y"])
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and not self.cancel_event.is_set():
                # Read navigation first: the click may already have changed the page.
                if page.url != old_url and clean_video_url(page.url):
                    return {"url": page.url, "kind": "navigation"}
                for popup in popups:
                    if not popup.is_closed() and clean_video_url(popup.url):
                        return {"url": popup.url, "kind": "popup"}
                try:
                    found = await page.evaluate("() => window.__KPA?.takeLink?.() || null")
                    if isinstance(found, dict) and clean_video_url(found.get("url")):
                        return found
                except Exception:
                    if page.is_closed():
                        return None
                await asyncio.sleep(0.08)
            return None
        finally:
            page.remove_listener("popup", on_popup)
            try:
                await page.evaluate("() => window.__KPA?.disarmLink?.()")
            except Exception:
                pass
            for popup in popups:
                try:
                    await popup.close()
                except Exception:
                    pass

    def _record_link(self, item: VideoItem, url: str, source: str) -> bool:
        url = clean_video_url(url)
        if not url:
            return False
        identifier = re.search(r"/(?:video|photo)/(\d{12,24})(?:$|/)", url)
        if identifier and item.photo_id and identifier.group(1) != item.photo_id:
            item.link_error = "O link encontrado pertence a outro vídeo."
            return False
        item.permalink = url
        item.link_source = source
        item.link_error = None
        if identifier and not item.photo_id:
            item.photo_id = identifier.group(1)
        return True

    async def _resolve_links_internal(self, top_n: int) -> None:
        targets = self.results[:min(max(1, int(top_n)), len(self.results))]
        self.status["link_target"] = len(targets)
        # Repair known exact IDs without loading or clicking the profile again.
        for item in targets:
            if not item.permalink and item.photo_id and re.fullmatch(r"\d{12,24}", item.photo_id):
                self._record_link(item, f"https://www.kwai.com/video/{item.photo_id}", "photo_id")
        unresolved = [item for item in targets if not item.permalink]
        self.status["links"] = sum(bool(item.permalink) for item in self.results)
        if not unresolved:
            return
        await self.ensure_browser(self.last_profile_url)
        assert self.page is not None
        page = self.page
        await page.evaluate("() => {window.__KPA_DOM?.chooseScrollTarget?.(); window.__KPA_DOM?.scrollTop?.();}")
        attempts = {item.key: 0 for item in unresolved}
        stagnant = 0
        started = time.monotonic()
        last_progress = started
        visited_cards = set()
        budget = min(90, 20 + len(unresolved) * 3)
        for round_no in range(1, 301):
            if time.monotonic() - started >= budget or time.monotonic() - last_progress >= 12:
                self.status["links_stop_reason"] = "time-budget"
                break
            keys = await page.evaluate("() => window.__KPA_DOM?.gridKeys?.() || []") or []
            fresh_cards = set(keys) - visited_cards
            visited_cards.update(keys)
            if fresh_cards:
                last_progress = time.monotonic()
            if not unresolved or self.cancel_event.is_set():
                break
            pending = [item for item in unresolved if attempts[item.key] < 2]
            if not pending:
                break
            api_items = await self._collect_background_feed()
            by_thumb = {}
            for candidate in api_items:
                fp = thumb_fingerprint(candidate.thumbnail)
                if fp and candidate.permalink:
                    by_thumb.setdefault(fp, {})[candidate.photo_id] = candidate
            for item in list(pending):
                candidates = by_thumb.get(thumb_fingerprint(item.thumbnail), {})
                if len(candidates) == 1:
                    candidate = next(iter(candidates.values()))
                    if self._record_link(item, candidate.permalink, "profile-feed"):
                        unresolved.remove(item)
                        pending.remove(item)
            if not pending:
                self.status["links"] = sum(bool(row.permalink) for row in self.results)
                if not unresolved:
                    break
                continue
            targets_by_key = {item.key: item for item in pending}
            descriptors = [{"key": item.key, "photo_id": item.photo_id,
                            "thumbnailFingerprint": thumb_fingerprint(item.thumbnail)} for item in pending]
            # One DOM snapshot per batch, instead of scanning every card for every target.
            matches = await page.evaluate("targets => window.__KPA_DOM?.findTargets?.(targets) || []", descriptors)
            recovered = 0
            for match in matches:
                if self.cancel_event.is_set() or time.monotonic() - started >= budget:
                    break
                item = targets_by_key.get(match.get("key"))
                if not item or not match.get("ok"):
                    if item and match.get("reason") == "ambiguous":
                        item.link_error = "Mais de um card corresponde a este vídeo."
                    continue
                found = {"url": match.get("url"), "kind": "dom-link"}
                if not clean_video_url(found["url"]):
                    attempts[item.key] += 1
                    item.link_attempts += 1
                    before_url = page.url
                    position = await page.evaluate("() => window.__KPA_DOM?.getScrollInfo?.()")
                    try:
                        found = await self._capture_card_link(page, {"key": item.key, "photo_id": item.photo_id,
                            "thumbnailFingerprint": thumb_fingerprint(item.thumbnail), "realClick": True})
                    except Exception as exc:
                        item.link_error = f"Falha no clique: {type(exc).__name__}"
                        found = None
                    if page.url != before_url:
                        # Return to the previous grid, preserving its loaded history when possible.
                        try:
                            await page.go_back(wait_until="domcontentloaded", timeout=15000)
                            if page.url != before_url:
                                await page.goto(before_url, wait_until="domcontentloaded", timeout=25000)
                            await page.evaluate("top => {window.__KPA_DOM?.chooseScrollTarget?.(); window.__KPA_DOM?.scrollTo?.(top);}", (position or {}).get("top", 0))
                        except Exception:
                            pass
                if found and self._record_link(item, found.get("url"), str(found.get("kind") or "click")):
                    unresolved.remove(item)
                    recovered += 1
                    last_progress = time.monotonic()
                elif not item.link_error:
                    item.link_error = "O card não forneceu um link nesta tentativa."
                self.status["links"] = sum(bool(row.permalink) for row in self.results)
            self.status.update({"link_rounds": round_no, "links_pending": len(unresolved),
                "message": f"Links: {len(targets)-len(unresolved)}/{len(targets)} • {len(unresolved)} pendentes."})
            if not unresolved:
                break
            before = await page.evaluate("() => window.__KPA_DOM?.getScrollInfo?.()")
            await page.evaluate("() => window.__KPA_DOM?.scrollBottom?.()")
            await asyncio.sleep(min(0.35 + stagnant * 0.4, 2.5))
            after = await page.evaluate("() => window.__KPA_DOM?.getScrollInfo?.()")
            moved = float((after or {}).get("top", 0)) > float((before or {}).get("top", 0)) + 2 or int((after or {}).get("scrollHeight", 0)) > int((before or {}).get("scrollHeight", 0)) + 4
            stagnant = 0 if fresh_cards or recovered else stagnant + 1
            state = await page.evaluate("() => window.__KPA_DOM?.endState?.() || {}")
            exhausted_visible = all(attempts[item.key] >= 2 for item in unresolved if any(match.get("key") == item.key and match.get("ok") for match in matches))
            if exhausted_visible and self._confirm_dom_end(state, stagnant, len(self.results), record=False):
                break
            if stagnant >= 8:
                break
            if stagnant:
                await page.evaluate("() => window.__KPA_DOM?.chooseScrollTarget?.()")
        for item in unresolved:
            if not item.link_error:
                item.link_error = "Busca interrompida." if self.cancel_event.is_set() else "Card não localizado na grade carregada; tente novamente."
        self.status["links_pending"] = len(unresolved)

    async def cancel(self):
        self.cancel_event.set()
        return {"ok": True}

    async def update_durations(self):
        if any(t and not t.done() for t in (self.task, self.link_task)) or self.status.get("running"):
            raise KwaiError("Aguarde ou interrompa a operação atual antes de atualizar as durações.")
        if not self.results:
            raise KwaiError("Faça uma coleta antes de atualizar as durações.")
        self.cancel_event = asyncio.Event()
        self.status.update(running=True, phase="durations", message="Lendo durações dos vídeos…")
        self.task = asyncio.create_task(self._update_durations())

    async def _update_durations(self):
        pending = [item for item in self.results if item.duration_seconds is None]
        updated = 0
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=8,
                                        headers={"User-Agent": "Mozilla/5.0"}) as client:
                for index, item in enumerate(pending, 1):
                    if self.cancel_event.is_set():
                        break
                    self.status["message"] = f"Durações: verificando {index}/{len(pending)} • {updated} atualizadas."
                    if not item.media_url:
                        continue
                    try:
                        duration = await asyncio.wait_for(read_mp4_duration(client, item.media_url), timeout=12)
                        if duration is not None:
                            item.duration_seconds = duration
                            updated += 1
                    except (httpx.HTTPError, ValueError, asyncio.TimeoutError):
                        continue
        finally:
            missing = sum(item.duration_seconds is None for item in self.results)
            prefix = "Atualização interrompida" if self.cancel_event.is_set() else "Durações atualizadas"
            self.status.update(running=False, phase=None,
                               message=f"{prefix}: {updated} preenchidas • {missing} sem duração disponível.")
            self._persist_latest()

    def _persist_latest(self) -> None:
        payload = {"association_version": 2, "profile_url": self.last_profile_url, "status": self.status, "results": [x.public() for x in self.results]}
        path = self.data_dir / "latest.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def load_latest(self) -> None:
        path = self.data_dir / "latest.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.last_profile_url = data.get("profile_url")
            self.results = [VideoItem(**item) for item in data.get("results", [])]
            quarantined = 0
            if data.get("association_version", 0) < 2:
                for item in self.results:
                    if item.thumbnail:
                        item.permalink = None
                        item.photo_id = None
                        item.media_url = None
                        item.link_source = None
                        item.link_error = "Link antigo requer nova associação com a thumbnail."
                        if item.thumbnail:
                            item.key = "thumb:" + thumb_fingerprint(item.thumbnail)
                        quarantined += 1
            old = data.get("status") or {}
            self.status = self._empty_status(); self.status.update(old); self.status["running"] = False; self.status["phase"] = None; self.status["browser_open"] = False
            self.status["links"] = sum(bool(item.permalink) for item in self.results)
            self.status["message"] = f"Último ranking restaurado: {len(self.results)} vídeos."
            if quarantined:
                self.status["message"] += f" {quarantined} associações antigas requerem nova coleta de links."
        except Exception:
            pass

    def public_results(self) -> list[dict[str, Any]]:
        return [x.public() for x in self.results]

    async def start_download(self, keys: list[str]) -> None:
        if self.download_task and not self.download_task.done():
            raise KwaiError("Já existe uma fila de downloads em andamento.")
        wanted = {str(x) for x in keys}
        items = [x for x in self.results if x.key in wanted]
        if not items:
            raise KwaiError("Selecione pelo menos um vídeo.")
        self.download_task = asyncio.create_task(self._download_items(items))

    async def _resolve_media_url(self, item: VideoItem) -> str | None:
        if item.media_url and re.search(r"\.mp4(?:[?#]|$)", item.media_url, re.I):
            return item.media_url
        if not item.permalink:
            return None
        await self.ensure_browser()
        assert self.context is not None
        if self.resolver_page is None or self.resolver_page.is_closed():
            self.resolver_page = await self.context.new_page()
        page = self.resolver_page
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()

        def callback(response: Response):
            if future.done():
                return
            url = response.url
            if re.search(r"\.mp4(?:[?#]|$)", url, re.I):
                future.set_result(url)

        page.on("response", callback)
        try:
            await page.goto(item.permalink, wait_until="domcontentloaded", timeout=30_000)
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=8)
            except asyncio.TimeoutError:
                try:
                    src = await page.locator("video").first.evaluate("el => el.currentSrc || el.src || ''", timeout=2_000)
                    return src if src and ".mp4" in src.lower() else None
                except Exception:
                    return None
        finally:
            page.remove_listener("response", callback)

    async def _download_items(self, items: list[VideoItem]) -> None:
        self.download_status = {"running": True, "total": len(items), "completed": 0, "failed": 0, "current": None, "message": "Iniciando downloads…", "items": []}
        try:
            await self.ensure_browser()
            cookies = await self.context.cookies() if self.context else []
            jar = httpx.Cookies()
            for c in cookies:
                try:
                    jar.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path") or "/")
                except Exception:
                    pass
            async with httpx.AsyncClient(cookies=jar, follow_redirects=True, timeout=60, headers={"User-Agent": "Mozilla/5.0"}) as client:
                for index, item in enumerate(items, 1):
                    row = {"key": item.key, "photo_id": item.photo_id, "status": "resolving", "file": None, "error": None}
                    self.download_status["items"].append(row)
                    self.download_status["current"] = item.photo_id or item.key
                    self.download_status["message"] = f"Download {index}/{len(items)}: resolvendo mídia…"
                    try:
                        media = await self._resolve_media_url(item)
                        if not media:
                            raise KwaiError("MP4 direto não encontrado.")
                        row["status"] = "downloading"
                        stem = item.photo_id or hashlib.sha1(item.key.encode()).hexdigest()[:16]
                        path = self.download_dir / f"kwai-{stem}.mp4"
                        await save_media(client, media, path)
                        row["status"] = "done"; row["file"] = path.name
                        self.download_status["completed"] += 1
                    except Exception as exc:
                        row["status"] = "failed"; row["error"] = str(exc)
                        self.download_status["failed"] += 1
            self.download_status["message"] = f"Downloads concluídos: {self.download_status['completed']} • falhas: {self.download_status['failed']}."
        except asyncio.CancelledError:
            self.download_status["message"] = "Downloads interrompidos."
            raise
        except Exception as exc:
            self.download_status["message"] = f"Não foi possível iniciar os downloads: {exc}"
        finally:
            self.download_status["running"] = False
            self.download_status["current"] = None

    def diagnostic(self) -> dict[str, Any]:
        return {
            "appVersion": "2.3.2", "profile_url": self.last_profile_url,
            "status": self.status, "download_status": self.download_status,
            "result_count": len(self.results), "sample": self.public_results()[:30],
        }

    async def close(self) -> None:
        self.cancel_event.set()
        tasks = [task for task in [*self._cdp_tasks, self.task, self.link_task, self.download_task] if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.cdp:
                await self.cdp.detach()
        except Exception:
            pass
        self.cdp = None
        if self.context:
            await self.context.close(); self.context = None
        if self._pw:
            await self._pw.stop(); self._pw = None
