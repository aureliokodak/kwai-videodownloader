"""Shared engine for platforms collected through the app's own browser window.

The app drives a visible window with a persistent profile: the user signs in
there once, the app opens the profile, scrolls it and reads the feed the page
receives. Media is fetched with that session, in parallel pieces, and a silent
top-quality stream gets its sound from another source of the same video.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from .kwai import KwaiAnalyzerService, KwaiError, VideoItem, normalize_hashtags, sort_items
from .youtube import find_ffmpeg

logger = logging.getLogger(__name__)

MAX_CONCURRENT_DOWNLOADS = 5
# Stream URLs are paced near playback speed per connection, so files come in parallel pieces.
MAX_CONNECTIONS = 16
CHUNK_BYTES = 512 * 1024
# Long enough to sign in or answer a verification in the window.
STALL_TIMEOUT = 300
WAITING_NOTICE = 15
DOWNLOAD_TIMEOUT = 180
# An MP4 audio track declares the handler type 'soun' in its hdlr box.
AUDIO_TRACK_RE = re.compile(rb'hdlr.{8}soun', re.S)
MERGED_FIELDS = ('likes', 'comments', 'duration_seconds', 'thumbnail')


def has_audio(data: bytes) -> bool:
    return AUDIO_TRACK_RE.search(data) is not None


@dataclass
class FeedPage:
    """One feed response: each entry is (item, video URLs best first, audio URLs)."""
    entries: list[tuple[VideoItem, list[str], list[str]]] = field(default_factory=list)
    rejected: int = 0
    # None when the response says nothing about further pages.
    has_more: bool | None = None
    cursor: str | None = None


class BrowserVideoService(KwaiAnalyzerService):
    NAME = ''
    SLUG = ''
    LOGIN_URL = ''
    REFERER = ''
    # Some feeds never state their last page: stop after this long without news.
    IDLE_END: float | None = None

    def __init__(self, base_dir: Path):
        super().__init__(base_dir)
        self.data_dir = base_dir / 'data' / self.SLUG
        self.download_dir = base_dir / 'downloads' / self.SLUG
        self.browser_profile_dir = self.data_dir / 'browser-profile'
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._media_urls: dict[str, list[str]] = {}
        self._audio_urls: dict[str, list[str]] = {}
        self._feeds: asyncio.Queue = asyncio.Queue()
        self._feed_listener = None
        self._client: httpx.AsyncClient | None = None
        self._connections = asyncio.Semaphore(MAX_CONNECTIONS)

    # ── Platform hooks ────────────────────────────────────────────────────

    def normalize_url(self, value: str) -> str:
        raise NotImplementedError

    def profile_username(self, url: str) -> str:
        raise NotImplementedError

    def page_url(self, url: str) -> str:
        """Where the collection navigates for a normalized profile URL."""
        return url

    def is_feed_response(self, response) -> bool:
        raise NotImplementedError

    def parse_payload(self, text: str, username: str, page: int) -> FeedPage | None:
        """None for responses that are not this profile's feed."""
        raise NotImplementedError

    async def _initial_payloads(self) -> list[str]:
        """Feed data already embedded in the page when it loads."""
        return []

    async def _resolve_sources(self, item: VideoItem) -> tuple[list[str], list[str]] | None:
        """Fresh video and audio URLs for an item collected without them."""
        return None

    @property
    def waiting_message(self) -> str:
        return f'Aguardando o {self.NAME}. Se a janela pedir login ou verificação, resolva por lá que a coleta continua.'

    @property
    def stalled_message(self) -> str:
        return f'O {self.NAME} parou de carregar vídeos. Confira a janela do navegador (login ou verificação) e tente de novo'

    # ── Browser ───────────────────────────────────────────────────────────

    async def ensure_browser(self, open_url: str | None = None) -> dict:
        """Visible window with its own profile, so the login made in it persists."""
        async with self.lock:
            if self.context is None:
                if self._pw is None:
                    self._pw = await async_playwright().start()
                options = dict(user_data_dir=str(self.browser_profile_dir), headless=False,
                               viewport={'width': 1440, 'height': 900})
                last_error = None
                for channel in ('chrome', 'msedge', None):
                    try:
                        self.context = await self._pw.chromium.launch_persistent_context(
                            **({'channel': channel} if channel else {}), **options)
                        break
                    except Exception as exc:
                        last_error = exc
                if self.context is None:
                    await self._pw.stop()
                    self._pw = None
                    raise KwaiError(f'Não foi possível abrir o navegador do {self.NAME}.') from last_error
                active_context = self.context

                def on_context_closed(*_):
                    if self.context is active_context:
                        self.context = None
                        self.page = None
                        self.status['browser_open'] = False
                self.context.on('close', on_context_closed)
            if self.page is None or self.page.is_closed():
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            if open_url:
                try:
                    await self.page.goto(open_url, wait_until='domcontentloaded', timeout=45_000)
                except Exception as exc:
                    logger.warning('%s: página não abriu: %s', self.NAME, exc)
            self.status['browser_open'] = True
            return {'ok': True, 'browser_open': True, 'url': self.page.url, 'profile_url': open_url or self.page.url}

    async def open_window(self, url: str | None) -> dict:
        """Panel button: shows the profile, or the login page when none was given."""
        return {**await self.ensure_browser(url or self.LOGIN_URL), 'profile_url': url}

    def receive_feed(self, text: str) -> None:
        self._feeds.put_nowait(text)

    async def _read_feed(self, response) -> None:
        try:
            self.receive_feed(await response.text())
        except Exception as exc:
            logger.warning('%s: resposta do feed ilegível: %s', self.NAME, exc)

    async def _open_profile(self, url: str) -> None:
        await self.ensure_browser()

        def on_response(response):
            if self.is_feed_response(response):
                self._track_task(asyncio.create_task(self._read_feed(response)))
        self.page.on('response', on_response)
        self._feed_listener = on_response
        try:
            await self.page.goto(self.page_url(url), wait_until='domcontentloaded', timeout=45_000)
        except Exception as exc:
            # A verification or a slow load can still be sorted out in the window.
            logger.warning('%s: perfil não abriu: %s', self.NAME, exc)
            self.status['message'] = self.waiting_message

    def _stop_listening(self) -> None:
        if self._feed_listener and self.page and not self.page.is_closed():
            self.page.remove_listener('response', self._feed_listener)
        self._feed_listener = None

    async def _scroll(self) -> None:
        if self.page is None or self.page.is_closed():
            raise KwaiError('A janela do navegador foi fechada durante a coleta.')
        # Scrolling to where the page already is does nothing, so step back first:
        # otherwise a page sitting at the bottom never asks for the next videos.
        await self.page.evaluate('window.scrollBy(0, -800)')
        await self.page.wait_for_timeout(200)
        await self.page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')

    # ── Collection ────────────────────────────────────────────────────────

    async def start_analysis(self, profile_url, limit, min_views, hashtag, sort_by, **kwargs):
        if self.task and not self.task.done():
            raise KwaiError('Aguarde ou interrompa a coleta atual.')
        url = self.normalize_url(profile_url)
        self.last_profile_url = url
        self.results = []
        self._media_urls = {}
        self._audio_urls = {}
        self._feeds = asyncio.Queue()
        self.status = self._empty_status()
        self.status.update(running=True, phase='scan', engine=self.NAME, feed_source='Navegador do app', limit=limit,
                           profile_url=url, browser_open=self.context is not None, started_at=time.time(),
                           message='Abrindo o perfil no navegador do app…')
        self.task = asyncio.create_task(self._collect(url, limit, min_views, hashtag, sort_by))

    async def _collect(self, url, limit, min_views, hashtag, sort_by):
        tags = normalize_hashtags(hashtag)
        username = self.profile_username(url)
        found: dict[str, VideoItem] = {}
        page_number = 0

        def passes(item: VideoItem) -> bool:
            return (not min_views or item.views >= min_views) and (not tags or not tags.isdisjoint(item.hashtags))

        def ingest(page: FeedPage) -> None:
            self.status['rejected_feeds'] += page.rejected
            for item, videos, audios in page.entries:
                known = found.get(item.key)
                if known is None:
                    found[item.key] = item
                    self._media_urls[item.key], self._audio_urls[item.key] = videos, audios
                    continue
                # The same video can show up in several responses, some with fewer fields.
                if not known.views and item.views:
                    known.views = item.views
                known.title = known.title or item.title
                for name in MERGED_FIELDS:
                    if getattr(known, name) is None:
                        setattr(known, name, getattr(item, name))
                if videos and not self._media_urls.get(item.key):
                    self._media_urls[item.key], self._audio_urls[item.key] = videos, audios
                    known.media_url = known.media_url or videos[0]
            self.results = sort_items([item for item in found.values() if passes(item)], sort_by)[:limit]
            self.status.update(pages=page_number, scanned=len(found), matched=len(self.results), links=len(self.results),
                               cursor=page.cursor, message=f'{self.NAME}: {len(self.results)} no ranking • {len(found)} vídeos • página {page_number}.')

        try:
            await self._open_profile(url)
            # What the page already holds comes first, ahead of responses that may report the end.
            pending = list(await self._initial_payloads())
            last_feed = time.monotonic()
            while True:
                try:
                    text = pending.pop(0) if pending else await asyncio.wait_for(self._feeds.get(), timeout=1)
                except asyncio.TimeoutError:
                    waited = time.monotonic() - last_feed
                    if self.IDLE_END and page_number and waited > self.IDLE_END:
                        self.status['stop_reason'] = 'idle'
                        break
                    if waited > STALL_TIMEOUT:
                        self.status['stop_reason'] = 'stalled'
                        break
                    if waited > WAITING_NOTICE:
                        self.status['message'] = self.waiting_message
                    await self._scroll()
                    continue
                page = self.parse_payload(text, username, page_number + 1)
                if page is None:
                    continue
                last_feed = time.monotonic()
                page_number += 1
                ingest(page)
                # The limit only trims the ranking: stopping early would rank just the newest videos.
                if page.has_more is False:
                    self.status.update(stop_reason='end', reached_end=True)
                    break
            label = {'end': 'Fim dos vídeos do perfil', 'idle': 'Nenhum vídeo novo apareceu ao rolar o perfil'}.get(
                self.status['stop_reason'], self.stalled_message)
            self.status['message'] = f'{label}: {len(self.results)} no ranking • {len(found)} verificados.'
        except asyncio.CancelledError:
            self.status.update(stop_reason='cancelled', message=f'Coleta interrompida: {len(self.results)} vídeos salvos.')
        except Exception as exc:
            self.status.update(stop_reason='error', error=str(exc), message=f'Coleta parcial salva. {self.NAME}: {exc}')
        finally:
            self._stop_listening()
            self.status.update(running=False, phase=None, finished_at=time.time())
            self._persist_latest()

    async def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        return {'ok': True}

    async def resolve_links(self, top_n):
        raise KwaiError(f'Os links do {self.NAME} já são coletados junto com o ranking.')

    async def update_durations(self):
        raise KwaiError(f'As durações do {self.NAME} são lidas na coleta. Refaça a coleta para atualizar.')

    # ── Downloads ─────────────────────────────────────────────────────────

    async def _open_client(self) -> httpx.AsyncClient:
        """Direct client carrying the browser's session, as the Kwai downloads do."""
        jar = httpx.Cookies()
        for cookie in await self.context.cookies():
            jar.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''), path=cookie.get('path', '/'))
        agent = await self.page.evaluate('navigator.userAgent')
        return httpx.AsyncClient(cookies=jar, follow_redirects=True, timeout=60,
                                 headers={'User-Agent': agent, 'Referer': self.REFERER},
                                 limits=httpx.Limits(max_connections=MAX_CONNECTIONS))

    async def _chunk(self, url: str, start: int) -> httpx.Response:
        async with self._connections:
            return await self._client.get(url, headers={'Range': f'bytes={start}-{start + CHUNK_BYTES - 1}'})

    async def _get_in_parts(self, url: str) -> tuple[bytes, str] | None:
        first = await self._chunk(url, 0)
        kind = first.headers.get('content-type', '')
        if first.status_code == 200:
            return first.content, kind
        match = re.fullmatch(r'bytes \d+-\d+/(\d+)', first.headers.get('content-range', ''))
        if first.status_code != 206 or not match:
            return None
        total = int(match[1])
        parts = await asyncio.gather(*(self._chunk(url, start) for start in range(CHUNK_BYTES, total, CHUNK_BYTES)))
        if any(part.status_code != 206 for part in parts):
            return None
        body = b''.join([first.content, *(part.content for part in parts)])
        return (body, kind) if len(body) == total else None

    async def _get(self, url: str) -> tuple[bytes, str] | None:
        """Parallel pieces first; the browser context remains as the slower fallback."""
        try:
            result = await self._get_in_parts(url)
            if result and result[0] and not result[1].startswith('text/'):
                return result
        except httpx.HTTPError as exc:
            logger.info('%s: download em partes indisponível: %s', self.NAME, exc)
        try:
            response = await self.context.request.get(url, headers={'Referer': self.REFERER},
                                                      timeout=DOWNLOAD_TIMEOUT * 1000)
            body = await response.body()
        except Exception as exc:
            logger.warning('%s: stream indisponível: %s', self.NAME, exc)
            return None
        kind = response.headers.get('content-type', '')
        return (body, kind) if response.ok and body and not kind.startswith('text/') else None

    async def _find_audio(self, urls: list[str]) -> tuple[bytes, str] | None:
        for url in urls:
            sound = await self._get(url)
            if sound and (has_audio(sound[0]) or sound[1].startswith('audio/')):
                return sound
        return None

    def _mux(self, ffmpeg: str, video: Path, audio: Path, output: Path, duration: float | None) -> None:
        """Copy both tracks untouched; the length is capped so a longer song cannot extend the video."""
        command = [ffmpeg, '-y', '-v', 'error', '-i', str(video), '-i', str(audio), '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy']
        if duration:
            command += ['-t', str(duration)]
        result = subprocess.run([*command, '-movflags', '+faststart', '-f', 'mp4', str(output)], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise KwaiError(f'Não foi possível juntar o áudio: {result.stderr.strip()[-200:]}')

    async def _fetch_media(self, urls: list[str], audio_urls: list[str], path: Path, duration: float | None = None) -> bool:
        """Save the best stream with sound; returns False when no source had any.

        Audio is fetched alongside the video: most top streams are silent, and
        waiting to find that out would add the two download times together.
        """
        audio = asyncio.create_task(self._find_audio(audio_urls))
        partial = path.with_suffix('.mp4.part')
        video_part, audio_part = path.with_suffix('.video.part'), path.with_suffix('.audio.part')
        try:
            video = None
            for url in urls:
                video = await self._get(url)
                if video:
                    break
            if not video:
                raise KwaiError(f'O {self.NAME} não liberou este vídeo. Refaça a coleta e tente de novo.')
            if has_audio(video[0]):
                partial.write_bytes(video[0])
                partial.replace(path)
                return True
            ffmpeg = find_ffmpeg(self.base_dir)
            if not ffmpeg:
                raise KwaiError('O stream de maior qualidade vem sem som e o FFmpeg, necessário para juntar o áudio, não foi encontrado.')
            sound = await audio
            if not sound:
                partial.write_bytes(video[0])
                partial.replace(path)
                return False
            video_part.write_bytes(video[0])
            audio_part.write_bytes(sound[0])
            await asyncio.to_thread(self._mux, ffmpeg, video_part, audio_part, partial, duration)
            partial.replace(path)
            return True
        finally:
            audio.cancel()
            for leftover in (video_part, audio_part, partial):
                leftover.unlink(missing_ok=True)

    async def start_download(self, keys):
        if self.download_task and not self.download_task.done():
            raise KwaiError('Já existe uma fila de downloads em andamento.')
        wanted = set(keys)
        chosen = [item for item in self.results if item.key in wanted]
        if not chosen:
            raise KwaiError(f'Selecione pelo menos um vídeo do {self.NAME}.')
        self.download_status = dict(running=True, total=len(chosen), completed=0, failed=0, current=None,
                                    message='Baixando pelo navegador do app…', items=[])
        self.download_task = asyncio.create_task(self._download_videos(chosen))

    async def _sources(self, item: VideoItem) -> tuple[list[str], list[str]]:
        urls = self._media_urls.get(item.key)
        if urls:
            return urls, self._audio_urls.get(item.key) or []
        # Links are kept only in memory, so a ranking loaded from disk asks the platform again.
        resolved = await self._resolve_sources(item)
        if resolved and resolved[0]:
            self._media_urls[item.key], self._audio_urls[item.key] = resolved
            return resolved
        return ([item.media_url] if item.media_url else []), []

    async def _download_videos(self, items):
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        started = time.monotonic()
        rows = [dict(key=item.key, photo_id=item.photo_id, status='queued', file=None, error=None) for item in items]
        self.download_status['items'].extend(rows)

        async def download_one(item: VideoItem, row: dict):
            async with semaphore:
                row['status'] = 'downloading'
                self.download_status['current'] = item.photo_id
                path = self.download_dir / f'{self.SLUG}-{item.photo_id}.mp4'
                try:
                    # Files saved before audio was handled may be silent: those are fetched again.
                    if not (path.is_file() and has_audio(path.read_bytes())):
                        urls, audio_urls = await self._sources(item)
                        if not urls:
                            raise KwaiError('Sem link de vídeo. Refaça a coleta.')
                        if not await self._fetch_media(urls, audio_urls, path, item.duration_seconds):
                            row['error'] = f'Nenhuma fonte do {self.NAME} trouxe áudio para este vídeo.'
                    row.update(status='done', file=path.name)
                    self.download_status['completed'] += 1
                except Exception as exc:
                    row.update(status='failed', error=str(exc))
                    self.download_status['failed'] += 1
                done = self.download_status['completed'] + self.download_status['failed']
                self.download_status['message'] = (f"{self.NAME}: {done}/{len(items)} processados • {self.download_status['completed']} ok "
                                                   f"• {self.download_status['failed']} falhas")

        try:
            await self.ensure_browser()
            self._client = await self._open_client()
            await asyncio.gather(*(download_one(item, row) for item, row in zip(items, rows)))
            self.download_status['message'] = (f"Downloads concluídos: {self.download_status['completed']}"
                                               f" • falhas: {self.download_status['failed']} • tempo: {time.monotonic() - started:.0f}s. Pasta: downloads/{self.SLUG}")
        except asyncio.CancelledError:
            for row in rows:
                if row['status'] in ('queued', 'downloading'):
                    row['status'] = 'cancelled'
            self.download_status['message'] = 'Downloads interrompidos. Os arquivos completos foram preservados.'
        except Exception as exc:
            self.download_status['message'] = f'Não foi possível iniciar os downloads: {exc}'
        finally:
            if self._client:
                await self._client.aclose()
                self._client = None
            self.download_status.update(running=False, current=None)

    async def cancel_download(self):
        if self.download_task and not self.download_task.done():
            self.download_task.cancel()
            await asyncio.gather(self.download_task, return_exceptions=True)
        return {'ok': True}
