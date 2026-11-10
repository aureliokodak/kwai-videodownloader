"""Instagram adapter: reads the Reels tab the page receives in the app's browser.

Instagram sends the same media object in several envelopes (embedded page
JSON, GraphQL and v1 API responses), so payloads are searched for media
objects instead of being read through one fixed path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import urlparse
from xml.etree import ElementTree

from .browser_video import BrowserVideoService, FeedPage
from .kwai import HASHTAG_RE, KwaiError, VideoItem, parse_count

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r'[A-Za-z0-9._]{1,30}')
SHORTCODE_RE = re.compile(r'[A-Za-z0-9_-]{5,40}')
HOSTS = ('instagram.com', 'www.instagram.com', 'm.instagram.com')
# First path segments that are Instagram pages, not profiles.
RESERVED = {'p', 'reel', 'reels', 'tv', 'stories', 'explore', 'accounts', 'direct', 'about', 'developer', 'legal',
            'api', 'graphql', 'web', 'challenge', 'emails', 'session', 'privacy', 'terms', 'login', 'ar', 'topics'}
# Instagram media types: 1 photo, 2 video, 8 carousel.
VIDEO_TYPE = 2
RESOLVE_TIMEOUT = 20
EMBEDDED_JSON_SCRIPT = """() => Array.from(document.querySelectorAll('script[type="application/json"]'), s => s.textContent)
  .filter(t => t.includes('"code"') && (t.includes('video_versions') || t.includes('play_count') || t.includes('video_dash_manifest')))"""


def normalize_instagram_url(value: str) -> str:
    value = (value or '').strip()
    if value.startswith('@'):
        value = 'https://www.instagram.com/' + value[1:]
    elif value.startswith(tuple(host + '/' for host in HOSTS)):
        value = 'https://' + value
    try:
        u = urlparse(value)
        port = u.port
    except ValueError as exc:
        raise KwaiError('O endereço do Instagram é inválido.') from exc
    if u.scheme not in ('http', 'https') or u.hostname not in HOSTS or u.username or u.password or port:
        raise KwaiError('Informe o perfil do Instagram (@usuario ou https://www.instagram.com/usuario).')
    match = re.fullmatch(r'/([^/?#]+)(?:/reels)?/?', u.path)
    if not match or match[1].lower() in RESERVED or not USERNAME_RE.fullmatch(match[1]):
        raise KwaiError('Informe o perfil do Instagram, não um post, reel ou story avulso.')
    return f'https://www.instagram.com/{match[1]}/'


def profile_username(url: str) -> str:
    return url.rstrip('/').rsplit('/', 1)[1].lower()


def load_payloads(text: str) -> list:
    """Responses may be one JSON value, a guarded one, or several separated by newlines."""
    text = (text or '').strip().removeprefix('for (;;);')
    try:
        return [json.loads(text)]
    except (TypeError, ValueError):
        pass
    values = []
    for line in text.splitlines():
        try:
            values.append(json.loads(line))
        except ValueError:
            continue
    return values


def walk(node, key=None, depth=0):
    """Yield (key it was found under, dict) for every dict in a JSON value."""
    if depth > 60:
        return
    if isinstance(node, dict):
        yield key, node
        for child_key, child in node.items():
            if isinstance(child, (dict, list)):
                yield from walk(child, child_key, depth + 1)
    elif isinstance(node, list):
        for child in node:
            if isinstance(child, (dict, list)):
                yield from walk(child, key, depth + 1)


def is_media(node: dict) -> bool:
    code = node.get('code')
    return (isinstance(code, str) and SHORTCODE_RE.fullmatch(code) is not None
            and any(name in node for name in ('media_type', 'video_versions', 'product_type', 'play_count')))


def owners(media: dict) -> set[str]:
    names = {(media.get(name) or {}).get('username') for name in ('user', 'owner') if isinstance(media.get(name), dict)}
    names.update(person.get('username') for person in media.get('coauthor_producers') or [] if isinstance(person, dict))
    return {str(name).lower() for name in names if name}


def belongs_to(media: dict, username: str) -> bool:
    # Reels tab entries sometimes leave the owner out; they come from the profile being read.
    found = owners(media)
    return not found or username in found


def is_video(media: dict) -> bool:
    return (media.get('media_type') == VIDEO_TYPE or bool(media.get('video_versions'))
            or bool(media.get('video_dash_manifest')) or media.get('product_type') == 'clips')


def dash_streams(manifest) -> tuple[list[dict], list[dict]]:
    """(video, audio) representations of a DASH manifest, each {url, width, height, bandwidth, codecs}."""
    if not isinstance(manifest, str) or '<!DOCTYPE' in manifest or '<!ENTITY' in manifest:
        return [], []
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError:
        return [], []
    videos, audios = [], []
    for adaptation in root.iter():
        if not adaptation.tag.endswith('AdaptationSet'):
            continue
        for rep in adaptation:
            if not rep.tag.endswith('Representation'):
                continue
            kind = (adaptation.get('contentType') or (rep.get('mimeType') or adaptation.get('mimeType') or '').split('/')[0])
            base = next((node.text.strip() for node in rep.iter() if node.tag.endswith('BaseURL') and node.text), '')
            if not base.startswith('http'):
                continue
            stream = dict(url=base, width=int(rep.get('width') or 0), height=int(rep.get('height') or 0),
                          bandwidth=int(rep.get('bandwidth') or 0), codecs=rep.get('codecs') or adaptation.get('codecs') or '')
            if kind == 'video':
                videos.append(stream)
            elif kind == 'audio':
                audios.append(stream)
    return videos, audios


def media_sources(media: dict) -> tuple[list[str], list[str]]:
    """(video URLs best first, audio URLs).

    Progressive files carry sound; DASH video can go higher but is silent and
    is completed with the best DASH audio. At equal resolution the progressive
    file wins, then H.264, then bitrate.
    """
    candidates = []
    for version in media.get('video_versions') or []:
        if isinstance(version, dict) and str(version.get('url') or '').startswith('http'):
            width, height = version.get('width') or 0, version.get('height') or 0
            candidates.append(((min(width, height), True, True, 0), version['url']))
    dash_video, dash_audio = dash_streams(media.get('video_dash_manifest'))
    for stream in dash_video:
        candidates.append(((min(stream['width'], stream['height']), False, stream['codecs'].startswith('avc1'), stream['bandwidth']), stream['url']))
    videos = list(dict.fromkeys(url for _, url in sorted(candidates, key=lambda c: c[0], reverse=True)))
    progressive = [url for rank, url in sorted(candidates, key=lambda c: c[0], reverse=True) if rank[1]]
    audio = [stream['url'] for stream in sorted(dash_audio, key=lambda s: s['bandwidth'], reverse=True)] + progressive
    return videos, list(dict.fromkeys(audio))


def caption_text(media: dict) -> str:
    caption = media.get('caption')
    if isinstance(caption, dict):
        return caption.get('text') or ''
    if isinstance(caption, str):
        return caption
    edges = (media.get('edge_media_to_caption') or {}).get('edges') or []
    return ((edges[0] or {}).get('node') or {}).get('text') or '' if edges else ''


def parse_instagram_media(media: dict, page: int | None = None) -> VideoItem | None:
    if not is_media(media) or not is_video(media):
        return None
    code = media['code']
    views = next((value for value in (parse_count(media.get(name)) for name in
                                      ('play_count', 'ig_play_count', 'view_count', 'video_view_count')) if value is not None), 0)
    title = caption_text(media)
    candidates = (media.get('image_versions2') or {}).get('candidates') or []
    thumbnail = next((c.get('url') for c in candidates if isinstance(c, dict) and c.get('url')), None)
    duration = media.get('video_duration')
    videos, _ = media_sources(media)
    return VideoItem(key='instagram:' + code, photo_id=code, permalink=f'https://www.instagram.com/reel/{code}/',
                     views=views, likes=parse_count(media.get('like_count')), comments=parse_count(media.get('comment_count')),
                     title=title, hashtags=sorted({tag.lower() for tag in HASHTAG_RE.findall(title)}),
                     thumbnail=thumbnail or media.get('display_url') or media.get('thumbnail_src'),
                     media_url=videos[0] if videos else None, source='instagram', link_source='instagram-feed', page=page,
                     duration_seconds=float(duration) if isinstance(duration, (float, int)) and duration > 0 else None)


def is_reels_connection(key, node: dict) -> bool:
    """Only the Reels list decides when the profile ends; the posts grid has its own paging."""
    named = key is None or any(word in str(key).lower() for word in ('clips', 'reel'))
    return named and ((isinstance(node.get('edges'), list) and isinstance(node.get('page_info'), dict))
                      or (isinstance(node.get('items'), list) and ('more_available' in node or isinstance(node.get('paging_info'), dict))))


def parse_reels_payload(text: str, username: str, page: int) -> FeedPage | None:
    result = FeedPage()
    relevant = False
    for data in load_payloads(text):
        for key, node in walk(data):
            if is_reels_connection(key, node):
                listed = node.get('edges') if 'edges' in node else node.get('items')
                found = [m for _, m in walk(listed) if is_media(m)]
                # A collab reel by another account may sit in the tab, so one owned video is enough.
                if any(belongs_to(m, username) for m in found) or (not found and key is not None):
                    paging = node.get('page_info') or node.get('paging_info') or node
                    more = paging.get('has_next_page', paging.get('more_available'))
                    if isinstance(more, bool):
                        result.has_more = more
                        result.cursor = paging.get('end_cursor') or paging.get('max_id')
                        relevant = True
            # Repeated sightings are kept: a later one may carry fields the first lacked.
            if not is_media(node):
                continue
            item = parse_instagram_media(node, page)
            if item is None or not belongs_to(node, username):
                result.rejected += 1
                continue
            videos, audio = media_sources(node)
            result.entries.append((item, videos, audio))
            relevant = True
    return result if relevant else None


def find_sources(text: str, code: str) -> tuple[list[str], list[str]] | None:
    for data in load_payloads(text):
        for _, node in walk(data):
            if is_media(node) and node['code'] == code:
                videos, audio = media_sources(node)
                if videos:
                    return videos, audio
    return None


class InstagramAnalyzerService(BrowserVideoService):
    NAME = 'Instagram'
    SLUG = 'instagram'
    LOGIN_URL = 'https://www.instagram.com/accounts/login/'
    REFERER = 'https://www.instagram.com/'
    IDLE_END = 45

    def __init__(self, base_dir):
        super().__init__(base_dir)
        self._resolving = asyncio.Semaphore(2)

    def normalize_url(self, value: str) -> str:
        return normalize_instagram_url(value)

    def profile_username(self, url: str) -> str:
        return profile_username(url)

    def page_url(self, url: str) -> str:
        return url + 'reels/'

    def is_feed_response(self, response) -> bool:
        url = response.url
        return (urlparse(url).hostname or '').endswith('instagram.com') and ('/graphql' in url or '/api/v1/' in url)

    def parse_payload(self, text: str, username: str, page: int) -> FeedPage | None:
        return parse_reels_payload(text, username, page)

    async def _initial_payloads(self) -> list[str]:
        try:
            return await self.page.evaluate(EMBEDDED_JSON_SCRIPT)
        except Exception as exc:
            logger.warning('Instagram: dados da página indisponíveis: %s', exc)
            return []

    async def _resolve_sources(self, item: VideoItem) -> tuple[list[str], list[str]] | None:
        """Open the reel in a background tab and read its media from what the page loads."""
        async with self._resolving:
            page = await self.context.new_page()
            texts: list[str] = []

            async def read(response):
                try:
                    texts.append(await response.text())
                except Exception:
                    pass

            page.on('response', lambda response: self._track_task(asyncio.create_task(read(response)))
                    if self.is_feed_response(response) else None)
            try:
                await page.goto(item.permalink, wait_until='domcontentloaded', timeout=45_000)
                texts.extend(await page.evaluate(EMBEDDED_JSON_SCRIPT))
                deadline = time.monotonic() + RESOLVE_TIMEOUT
                while time.monotonic() < deadline:
                    for text in texts:
                        found = find_sources(text, item.photo_id)
                        if found:
                            return found
                    await asyncio.sleep(0.5)
                return None
            except Exception as exc:
                logger.warning('Instagram: reel %s não abriu: %s', item.photo_id, exc)
                return None
            finally:
                await page.close()
