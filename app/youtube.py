"""YouTube adapter using the same ranking schema and persistence as Kwai.

yt-dlp runs in-process (no subprocess per video) so the player JS, HTTP
connections and Python startup are paid once. Collection is two-staged:
the Shorts tab is listed with flat extraction (one request per ~30 videos)
and each listed video is enriched with full metadata by a pool of workers.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse

from .kwai import HASHTAG_RE, KwaiAnalyzerService, KwaiError, VideoItem, normalize_hashtags, parse_count

logger = logging.getLogger(__name__)

METADATA_WORKERS = 6
MAX_CONCURRENT_DOWNLOADS = 4
# Flat listings round view counts ("4.1K"); skip only videos clearly below the minimum.
FLAT_VIEWS_TOLERANCE = 0.9
# Best resolution first; among equal resolutions prefer H.264/AAC so the MP4 opens in any editor.
VIDEO_FORMAT = 'bv*+ba/b'
FORMAT_SORT = ['res', 'fps', 'vcodec:h264', 'acodec:aac']
BOT_CHECK_RE = re.compile(r"sign in to confirm|not a bot|login required|use --cookies", re.I)
VIDEO_ID_RE = re.compile(r'[A-Za-z0-9_-]{11}')
_END = object()


def find_ffmpeg(base_dir: Path) -> str | None:
    for candidate in (base_dir / 'runtime' / 'ffmpeg.exe', base_dir / 'runtime' / 'ffmpeg' / 'bin' / 'ffmpeg.exe'):
        if candidate.is_file():
            return str(candidate)
    return shutil.which('ffmpeg')


def normalize_youtube_url(value: str) -> str:
    value = value.strip()
    if value.startswith('@'):
        value = 'https://www.youtube.com/' + value
    elif value.startswith(('youtube.com/', 'www.youtube.com/', 'm.youtube.com/', 'youtu.be/')):
        value = 'https://' + value
    try:
        u = urlparse(value)
        port = u.port
    except ValueError as exc:
        raise KwaiError('O endereço do YouTube é inválido.') from exc
    if u.scheme not in ('http', 'https') or u.hostname not in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be') or u.username or u.password or port:
        raise KwaiError('Informe o canal do YouTube para coletar apenas Shorts.')
    if u.hostname != 'youtu.be' and re.fullmatch(r'/(?:@[^/?#]+|(?:channel|c|user)/[^/?#]+)(?:/(?:videos|shorts|streams))?/?', u.path):
        path = re.sub(r'/(videos|shorts|streams)$', '', u.path.rstrip('/'))
        return 'https://www.youtube.com' + path + '/shorts'
    raise KwaiError('Esta opção baixa apenas Shorts. Informe o canal (@nome ou URL do canal), não playlists ou vídeos avulsos.')


def is_shorts_tab(value: str) -> bool:
    try:
        return value.rstrip('/') == normalize_youtube_url(value)
    except (KwaiError, ValueError, AttributeError):
        return False


def confirmed_short(item: VideoItem) -> bool:
    return bool(item.source == 'youtube' and item.link_source == 'youtube-shorts-tab'
                and VIDEO_ID_RE.fullmatch(item.photo_id or '')
                and item.key == 'youtube:' + item.photo_id
                and item.permalink == 'https://www.youtube.com/shorts/' + item.photo_id)


def _extract_thumbnail(info: dict, video_id: str) -> str | None:
    """Try multiple sources for a usable thumbnail URL."""
    thumb = info.get('thumbnail')
    if thumb:
        return thumb
    thumbs = info.get('thumbnails')
    if isinstance(thumbs, list):
        for entry in reversed(thumbs):
            url = entry.get('url') if isinstance(entry, dict) else None
            if url:
                return url
    return f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'


def parse_video(info: dict) -> VideoItem | None:
    # Membership in the Shorts tab is the evidence, not duration, shape or #shorts.
    playlist_url = info.get('playlist_webpage_url') or info.get('webpage_url') or ''
    if playlist_url and not is_shorts_tab(playlist_url):
        return None
    video_id = str(info.get('id', ''))
    if not VIDEO_ID_RE.fullmatch(video_id) or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
        return None
    title = info.get('title') or ''
    tags = sorted({tag.lower() for tag in HASHTAG_RE.findall(title + ' ' + (info.get('description') or ''))})
    duration = info.get('duration')
    views = parse_count(info.get('view_count'))
    if views is None:
        views = 0
    return VideoItem(key='youtube:' + video_id, photo_id=video_id,
                     permalink='https://www.youtube.com/shorts/' + video_id,
                     views=views, likes=parse_count(info.get('like_count')),
                     comments=parse_count(info.get('comment_count')), title=title, hashtags=tags,
                     thumbnail=_extract_thumbnail(info, video_id), media_url=None, source='youtube',
                     link_source='youtube-shorts-tab', page=None,
                     duration_seconds=float(duration) if isinstance(duration, (float, int)) and duration > 0 else None)


def _clean_error(exc: Exception) -> str:
    text = re.sub(r'^ERROR:\s*', '', str(exc).strip())
    return re.sub(r'^\[youtube[^\]]*\]\s*[\w-]+:\s*', '', text) or 'O YouTube não retornou o vídeo.'


class YouTubeAnalyzerService(KwaiAnalyzerService):
    def __init__(self, base_dir: Path):
        super().__init__(base_dir)
        self.data_dir = base_dir / 'data' / 'youtube'
        self.download_dir = base_dir / 'downloads' / 'youtube'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        # Signals worker threads, which cannot be cancelled like asyncio tasks.
        self._scan_stop = threading.Event()
        self._download_stop = threading.Event()

    def load_latest(self):
        super().load_latest()
        before = len(self.results)
        self.results = [item for item in self.results if confirmed_short(item)]
        self.status.update(matched=len(self.results), links=len(self.results))
        if before != len(self.results):
            self.status.update(reached_end=False, message='Ranking antigo do YouTube ocultado: refaça a coleta para incluir apenas Shorts confirmados.')

    # ── yt-dlp configuration ──────────────────────────────────────────────

    @staticmethod
    def _require_ytdlp():
        if importlib.util.find_spec('yt_dlp') is None:
            raise KwaiError('Componente YouTube ausente. Execute ATUALIZAR-YOUTUBE.bat e reabra o aplicativo.')

    def _ffmpeg(self) -> str | None:
        return find_ffmpeg(self.base_dir)

    def _cookie_options(self) -> dict:
        """Cookies are a fallback for bot checks only: stale cookies make YouTube
        reject requests ("The page needs to be reloaded") and hide HD formats.

        Resolution order:
          1. YTDLP_COOKIES_PATH  → file path  (default: ./cookies.txt)
          2. YTDLP_COOKIES_BROWSER → browser name (chrome, firefox, edge…)
        """
        cookie_path = Path(os.environ.get('YTDLP_COOKIES_PATH', self.base_dir / 'cookies.txt'))
        if cookie_path.is_file():
            return {'cookiefile': str(cookie_path)}
        browser = os.environ.get('YTDLP_COOKIES_BROWSER', '').strip()
        if browser:
            return {'cookiesfrombrowser': (browser,)}
        return {}

    def _options(self, cookies: bool = False, **extra) -> dict:
        portable_node = self.base_dir / 'runtime' / 'node.exe'
        options = dict(quiet=True, no_warnings=True, noprogress=True, socket_timeout=25,
                       retries=5, fragment_retries=5, extractor_retries=3,
                       js_runtimes={'node': {'path': str(portable_node)} if portable_node.exists() else {}})
        if cookies:
            options.update(self._cookie_options())
        options.update(extra)
        return options

    def _with_cookie_fallback(self, action):
        """Run action(cookies=False); on a bot check retry once with cookies."""
        from yt_dlp.utils import DownloadError
        try:
            return action(False)
        except DownloadError as exc:
            if not BOT_CHECK_RE.search(str(exc)) or not self._cookie_options():
                raise
            logger.info('yt-dlp: verificação anti-bot, repetindo com cookies')
            return action(True)

    # ── Blocking yt-dlp calls (run in worker threads) ─────────────────────

    def _list_shorts(self, url: str, emit, stop: threading.Event) -> None:
        """Stream flat entries of the Shorts tab; pages are fetched lazily."""
        from yt_dlp import YoutubeDL

        def run(cookies):
            with YoutubeDL(self._options(cookies, extract_flat='in_playlist', lazy_playlist=True)) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
                for entry in info.get('entries') or ():
                    if stop.is_set():
                        return
                    if isinstance(entry, dict):
                        emit(entry)
        self._with_cookie_fallback(run)

    def _video_info(self, video_id: str) -> dict:
        from yt_dlp import YoutubeDL

        def run(cookies):
            # process=False skips format selection; only metadata is needed here.
            with YoutubeDL(self._options(cookies)) as ydl:
                return ydl.extract_info('https://www.youtube.com/shorts/' + video_id, download=False, process=False)
        info = self._with_cookie_fallback(run)
        info.pop('formats', None)
        return info

    def _download_video(self, url: str, ffmpeg: str | None, stop: threading.Event) -> Path:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadCancelled

        def check_cancel(_progress):
            if stop.is_set():
                raise DownloadCancelled()

        def run(cookies):
            # The resolution in the name keeps old low-quality files from being reused.
            options = self._options(cookies, format=VIDEO_FORMAT if ffmpeg else 'b', format_sort=FORMAT_SORT,
                                    outtmpl=str(self.download_dir / 'youtube-%(id)s-%(resolution)s.%(ext)s'),
                                    noplaylist=True, concurrent_fragment_downloads=4,
                                    progress_hooks=[check_cancel])
            if ffmpeg:
                options.update(ffmpeg_location=ffmpeg, merge_output_format='mp4')
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
            downloads = (info or {}).get('requested_downloads') or []
            return Path(downloads[-1]['filepath']) if downloads and downloads[-1].get('filepath') else None
        path = self._with_cookie_fallback(run)
        if path is None or not path.is_file() or path.stat().st_size == 0:
            raise KwaiError('O download não gerou um arquivo completo.')
        return path

    # ── Collection ────────────────────────────────────────────────────────

    async def start_analysis(self, profile_url, limit, min_views, hashtag, sort_by, **kwargs):
        if self.status['running']:
            raise KwaiError('Aguarde ou interrompa a coleta atual.')
        url = normalize_youtube_url(profile_url)
        self._require_ytdlp()
        self.last_profile_url = url
        self.results = []
        self.status = self._empty_status()
        self.status.update(running=True, phase='scan', engine='YouTube Shorts', feed_source='Aba Shorts',
                           limit=limit, message='Listando os Shorts do canal…')
        self._scan_stop = threading.Event()
        self.task = asyncio.create_task(self._collect(url, limit, min_views, hashtag, sort_by, self._scan_stop))

    async def _collect(self, url, limit, min_views, hashtag, sort_by, stop: threading.Event):
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        tags = normalize_hashtags(hashtag)
        seen: set[str] = set()
        counters = dict(listed=0, partial=0)

        def emit(entry):
            loop.call_soon_threadsafe(queue.put_nowait, entry)

        def partial_note():
            return f" • {counters['partial']} sem métricas completas" if counters['partial'] else ''

        def report():
            extra = partial_note()
            self.status.update(matched=len(self.results), links=len(self.results),
                               message=f"YouTube: {counters['listed']} listados • {self.status['scanned']} verificados "
                                       f"• {len(self.results)} aprovados{extra}.")

        def accept(item: VideoItem):
            if stop.is_set() or (min_views and item.views < min_views) or (tags and tags.isdisjoint(item.hashtags)):
                return
            self.results.append(item)
            self.results.sort(key=lambda x: (getattr(x, sort_by) is not None, getattr(x, sort_by) or 0), reverse=True)
            if len(self.results) >= limit:
                self.status['stop_reason'] = 'limit'
                stop.set()
                queue.put_nowait(_END)

        async def worker():
            while not stop.is_set():
                entry = await queue.get()
                if entry is _END:
                    queue.put_nowait(_END)
                    return
                video_id = str(entry.get('id') or '')
                if not VIDEO_ID_RE.fullmatch(video_id) or video_id in seen:
                    continue
                seen.add(video_id)
                counters['listed'] += 1
                flat_views = parse_count(entry.get('view_count'))
                if min_views and flat_views is not None and flat_views < min_views * FLAT_VIEWS_TOLERANCE:
                    self.status['scanned'] += 1
                    report()
                    continue
                try:
                    full = await asyncio.to_thread(self._video_info, video_id)
                except Exception as exc:
                    # Keep the flat metrics (views, title) when one video fails.
                    logger.warning('YouTube: metadados de %s indisponíveis: %s', video_id, exc)
                    counters['partial'] += 1
                    full = {}
                if stop.is_set():
                    return
                item = parse_video({**entry, **full, 'id': video_id, 'playlist_webpage_url': url})
                self.status['scanned'] += 1
                if item is not None:
                    accept(item)
                report()

        listing = asyncio.create_task(asyncio.to_thread(self._list_shorts, url, emit, stop))
        listing.add_done_callback(lambda _: queue.put_nowait(_END))
        workers = [asyncio.create_task(worker()) for _ in range(METADATA_WORKERS)]
        try:
            await asyncio.gather(*workers)
            if self.status['stop_reason'] == 'limit':
                self.status['message'] = f'Limite atingido: {len(self.results)} vídeos. O ranking considera os vídeos verificados{partial_note()}.'
            else:
                await listing
                self.status.update(stop_reason='end', reached_end=True,
                                   message=f"Coleta concluída: {len(self.results)} aprovados de {self.status['scanned']} vídeos disponíveis verificados{partial_note()}.")
        except asyncio.CancelledError:
            self.status.update(stop_reason='cancelled', message=f'Coleta interrompida: {len(self.results)} vídeos salvos.')
        except Exception as exc:
            error = _clean_error(exc)
            self.status.update(stop_reason='error', error=error, message=f'Coleta parcial salva. YouTube: {error}')
        finally:
            stop.set()
            for task in workers:
                task.cancel()
            self.status.update(running=False, phase=None)
            self._persist_latest()

    async def cancel(self):
        self._scan_stop.set()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            if self.status['running']:
                self.status.update(running=False, phase=None, stop_reason='cancelled', message='Coleta interrompida.')
                self._persist_latest()
        return {'ok': True}

    async def resolve_links(self, top_n):
        raise KwaiError('Os links do YouTube já são coletados junto com o ranking.')

    async def update_durations(self):
        raise KwaiError('As durações do YouTube são consultadas na coleta. Refaça a coleta para atualizar.')

    # ── Downloads ─────────────────────────────────────────────────────────

    async def start_download(self, keys):
        if self.download_status['running']:
            raise KwaiError('Já existe uma fila de downloads em andamento.')
        self._require_ytdlp()
        wanted = set(keys)
        chosen = [item for item in self.results if item.key in wanted]
        if any(not confirmed_short(item) for item in chosen):
            raise KwaiError('A seleção contém vídeos sem confirmação de Shorts. Refaça a coleta do canal.')
        if not chosen:
            raise KwaiError('Selecione pelo menos um vídeo do YouTube.')
        self.download_status = dict(running=True, total=len(chosen), completed=0, failed=0,
                                    current=None, message='Iniciando downloads do YouTube…', items=[])
        self._download_stop = threading.Event()
        self.download_task = asyncio.create_task(self._download_youtube(chosen, self._download_stop))

    async def _download_youtube(self, items, stop: threading.Event):
        sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        rows = [dict(key=item.key, photo_id=item.photo_id, status='queued', file=None, error=None) for item in items]
        self.download_status['items'].extend(rows)
        ffmpeg = self._ffmpeg()
        # Without FFmpeg only single-file formats exist, and YouTube caps those at 360p.
        quality_note = '' if ffmpeg else ' • FFmpeg não encontrado: qualidade limitada a 360p'

        def progress_message():
            done = self.download_status['completed'] + self.download_status['failed']
            return (f"YouTube: {done}/{len(items)} processados • {self.download_status['completed']} ok "
                    f"• {self.download_status['failed']} falhas{quality_note}")

        async def download_one(item: VideoItem, row: dict):
            async with sem:
                row['status'] = 'downloading'
                self.download_status['current'] = item.photo_id or item.key
                try:
                    if not confirmed_short(item):
                        raise KwaiError('Download bloqueado: vídeo não confirmado na aba Shorts.')
                    # Formats are resolved at download time: signed media URLs expire.
                    path = await asyncio.to_thread(self._download_video, item.permalink, ffmpeg, stop)
                    row.update(status='done', file=path.name)
                    self.download_status['completed'] += 1
                except Exception as exc:
                    row.update(status='failed', error=_clean_error(exc))
                    self.download_status['failed'] += 1
                finally:
                    self.download_status['current'] = next((r['photo_id'] or r['key'] for r in rows if r['status'] == 'downloading' and r is not row), None)
                    self.download_status['message'] = progress_message()

        self.download_status['message'] = 'Baixando em alta qualidade…' + quality_note
        try:
            await asyncio.gather(*(download_one(item, row) for item, row in zip(items, rows)))
            self.download_status['message'] = (
                f"Downloads concluídos: {self.download_status['completed']}"
                f" • falhas: {self.download_status['failed']}. Pasta: downloads/youtube{quality_note}")
        except asyncio.CancelledError:
            stop.set()
            for row in rows:
                if row['status'] in ('queued', 'downloading'):
                    row['status'] = 'cancelled'
            self.download_status['message'] = 'Downloads interrompidos. Os arquivos completos foram preservados.'
        finally:
            self.download_status.update(running=False, current=None)

    async def cancel_download(self):
        # Worker threads stop at their next progress update.
        self._download_stop.set()
        if self.download_task and not self.download_task.done():
            self.download_task.cancel()
            await asyncio.gather(self.download_task, return_exceptions=True)
            self.download_status.update(running=False, current=None,
                                        message='Downloads interrompidos. Os arquivos completos foram preservados.')
        return {'ok': True}

    async def close(self):
        self._scan_stop.set()
        self._download_stop.set()
        await super().close()
