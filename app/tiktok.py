"""TikTok adapter: reads the profile feed the page receives in the app's browser."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from .browser_video import BrowserVideoService, FeedPage, has_audio  # noqa: F401 (has_audio is part of this module's API)
from .kwai import HASHTAG_RE, KwaiError, VideoItem, parse_count

FEED_PATH = '/api/post/item_list'
USERNAME_RE = re.compile(r'[A-Za-z0-9._]{2,24}')
VIDEO_ID_RE = re.compile(r'\d{10,25}')


def normalize_tiktok_url(value: str) -> str:
    value = (value or '').strip()
    if value.startswith('@'):
        value = 'https://www.tiktok.com/' + value
    elif value.startswith(('tiktok.com/', 'www.tiktok.com/', 'm.tiktok.com/')):
        value = 'https://' + value
    try:
        u = urlparse(value)
        port = u.port
    except ValueError as exc:
        raise KwaiError('O endereço do TikTok é inválido.') from exc
    if u.scheme not in ('http', 'https') or u.hostname not in ('tiktok.com', 'www.tiktok.com', 'm.tiktok.com') or u.username or u.password or port:
        raise KwaiError('Informe o perfil do TikTok (@usuario ou https://www.tiktok.com/@usuario).')
    match = re.fullmatch(r'/@([^/?#]+)/?', u.path)
    if not match or not USERNAME_RE.fullmatch(match[1]):
        raise KwaiError('Informe o perfil do TikTok, não um vídeo avulso ou link encurtado.')
    return 'https://www.tiktok.com/@' + match[1]


def profile_username(url: str) -> str:
    return url.rsplit('/@', 1)[1].lower()


def stream_rank(stream: dict):
    """Highest resolution first, then H.264."""
    play = stream.get('PlayAddr') or {}
    return (min(play.get('Width') or 0, play.get('Height') or 0),
            str(stream.get('CodecType') or '').startswith('h264'),
            stream.get('Bitrate') or 0)


def video_streams(video: dict) -> list[dict]:
    streams = [s for s in video.get('bitrateInfo') or [] if isinstance(s, dict) and (s.get('PlayAddr') or {}).get('UrlList')]
    return sorted(streams, key=stream_rank, reverse=True)


def media_candidates(video: dict) -> list[str]:
    """Stream URLs of the best quality first.

    downloadAddr is left out on purpose: it serves the watermarked copy.
    """
    streams = video_streams(video)
    urls = list(streams[0]['PlayAddr']['UrlList']) if streams else []
    if video.get('playAddr'):
        urls.append(video['playAddr'])
    return list(dict.fromkeys(url for url in urls if isinstance(url, str) and url.startswith('http')))


def audio_candidates(raw: dict) -> list[str]:
    """Where to look for sound when the best stream is silent, as 1080p ones can be."""
    video = raw.get('video') or {}
    streams = video_streams(video)
    urls = [video.get('playAddr')]
    for stream in sorted(streams[1:], key=lambda s: str(s.get('CodecType') or '').startswith('h264'), reverse=True):
        urls.extend(stream['PlayAddr']['UrlList'])
    # The author's own sound is this video's audio at a higher bitrate than the streams carry.
    # Anything else may be a song the video only partly uses, so it stays as the last resort.
    music = raw.get('music') or {}
    own_sound = (music.get('original') and music.get('authorName') == (raw.get('author') or {}).get('nickname')
                 and abs((music.get('duration') or 0) - (video.get('duration') or 0)) <= 1)
    urls.insert(0 if own_sound else len(urls), music.get('playUrl'))
    best = streams[0]['PlayAddr']['UrlList'] if streams else []
    return list(dict.fromkeys(url for url in urls if isinstance(url, str) and url.startswith('http') and url not in best))


def parse_tiktok_item(raw: dict, page: int | None = None) -> VideoItem | None:
    video = raw.get('video')
    video_id = str(raw.get('id') or '')
    username = (raw.get('author') or {}).get('uniqueId') if isinstance(raw.get('author'), dict) else None
    # Photo posts have no video stream, and ads do not belong to the profile.
    if not VIDEO_ID_RE.fullmatch(video_id) or not isinstance(video, dict) or not username or raw.get('imagePost') or raw.get('isAd'):
        return None
    stats = {**(raw.get('stats') or {}), **(raw.get('statsV2') or {})}
    title = raw.get('desc') or ''
    tags = {tag.lower() for tag in HASHTAG_RE.findall(title)}
    tags.update('#' + str(extra['hashtagName']).lower() for extra in raw.get('textExtra') or []
                if isinstance(extra, dict) and extra.get('hashtagName'))
    urls = media_candidates(video)
    duration = video.get('duration')
    return VideoItem(key='tiktok:' + video_id, photo_id=video_id,
                     permalink=f'https://www.tiktok.com/@{username}/video/{video_id}',
                     views=parse_count(stats.get('playCount')) or 0, likes=parse_count(stats.get('diggCount')),
                     comments=parse_count(stats.get('commentCount')), title=title, hashtags=sorted(tags),
                     thumbnail=video.get('originCover') or video.get('cover'), media_url=urls[0] if urls else None,
                     source='tiktok', link_source='tiktok-feed', page=page,
                     duration_seconds=float(duration) if isinstance(duration, (float, int)) and duration > 0 else None)


def feed_items(text: str) -> dict | None:
    """Return a usable feed page; TikTok answers refusals with 200 and an empty body."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('statusCode') not in (0, None):
        return None
    if not isinstance(data.get('itemList'), list):
        # The last page of a profile may omit the list altogether.
        return {**data, 'itemList': []} if 'hasMore' in data else None
    return data


class TikTokAnalyzerService(BrowserVideoService):
    NAME = 'TikTok'
    SLUG = 'tiktok'
    LOGIN_URL = 'https://www.tiktok.com/login'
    REFERER = 'https://www.tiktok.com/'

    def normalize_url(self, value: str) -> str:
        return normalize_tiktok_url(value)

    def profile_username(self, url: str) -> str:
        return profile_username(url)

    def is_feed_response(self, response) -> bool:
        return FEED_PATH in response.url

    def parse_payload(self, text: str, username: str, page: int) -> FeedPage | None:
        data = feed_items(text)
        if data is None:
            return None
        result = FeedPage(has_more=bool(data.get('hasMore')), cursor=data.get('cursor'))
        for raw in data['itemList']:
            item = parse_tiktok_item(raw, page) if isinstance(raw, dict) else None
            if item is None or item.permalink.split('/')[3].lower() != '@' + username:
                result.rejected += 1
                continue
            result.entries.append((item, media_candidates(raw['video']), audio_candidates(raw)))
        return result
