import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import main, youtube
from app.kwai import KwaiError
from app.youtube import YouTubeAnalyzerService, normalize_youtube_url, parse_video


def flat(data):
    """The Shorts tab listing exposes only id, title, views and thumbnails."""
    return {k: data[k] for k in ('id', 'title', 'view_count')}


def patch_scan(service, videos, fail=None, block=None):
    """Feed listing entries and serve full metadata from the given videos."""
    full = {v['id']: v for v in videos}
    def list_shorts(url, emit, stop):
        for data in videos:
            if stop.is_set():
                return
            emit(flat(data))
        if block:
            block.wait(5)
        if fail:
            raise fail
    def video_info(video_id):
        return {k: v for k, v in full[video_id].items() if k != 'playlist_webpage_url'}
    return patch.multiple(service, _list_shorts=list_shorts, _video_info=video_info)


def video(index=0, **values):
    return dict(id=f'abcdefgh{index:03}', title='Teste #Humor', description='#Novela',
                playlist_webpage_url='https://www.youtube.com/@canal/shorts', view_count=100, like_count=10, comment_count=2, duration=65, **values)


class YouTubeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = YouTubeAnalyzerService(Path(self.directory.name))

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def test_filter_limit_sort_and_persistence(self):
        videos = []
        for index, views in enumerate([10, 200, 300, 400]):
            videos.append({**video(index), 'view_count': views})
        with patch.object(youtube, 'METADATA_WORKERS', 1), patch_scan(self.service, videos):
            await self.service.start_analysis('@canal', 2, 100, '#novela', 'views')
            await self.service.task
        # '#novela' only exists in the description, so full metadata was fetched.
        self.assertEqual([v.views for v in self.service.results], [300, 200])
        self.assertEqual(self.service.results[0].likes, 10)
        self.assertEqual(self.service.results[0].duration_seconds, 65)
        self.assertEqual(self.service.status['scanned'], 3)
        self.assertEqual(self.service.status['stop_reason'], 'limit')
        self.assertFalse(self.service.status['reached_end'])
        restored = YouTubeAnalyzerService(Path(self.directory.name))
        restored.load_latest()
        self.assertEqual(restored.results, self.service.results)
        self.assertFalse((Path(self.directory.name) / 'data/latest.json').exists())

    async def test_cancel_saves_partial_and_rejects_overlap(self):
        block = threading.Event()
        with patch_scan(self.service, [video()], block=block):
            await self.service.start_analysis('@canal', 10, 0, None, 'likes')
            while not self.service.results:
                await asyncio.sleep(0.01)
            with self.assertRaises(KwaiError):
                await self.service.start_analysis('@outro', 10, 0, None, 'likes')
            await self.service.cancel()
            block.set()
        self.assertEqual(len(self.service.results), 1)
        self.assertEqual(self.service.status['stop_reason'], 'cancelled')
        self.assertFalse(self.service.status['running'])

    async def test_download_failure_continues_and_deduplicates(self):
        self.service.results = [parse_video(video(i)) for i in range(2)]
        calls = []
        def download(url, ffmpeg, stop):
            calls.append(url)
            if len(calls) == 1:
                raise KwaiError('Indisponível')
            path = self.service.download_dir / 'video.mp4'
            path.write_bytes(b'complete')
            return path
        with patch.object(youtube, 'MAX_CONCURRENT_DOWNLOADS', 1), patch.object(self.service, '_download_video', download):
            keys = [v.key for v in self.service.results]
            await self.service.start_download(keys + keys)
            with self.assertRaises(KwaiError):
                await self.service.start_download(keys)
            await self.service.download_task
        self.assertEqual(self.service.download_status['completed'], 1)
        self.assertEqual(self.service.download_status['failed'], 1)
        self.assertEqual(len(calls), 2)

    async def test_ordinary_videos_rejected_even_if_short_and_tagged(self):
        regular = video()
        regular.update(playlist_webpage_url='https://www.youtube.com/@canal/videos',
                       duration=15, title='#shorts', width=1080, height=1920)
        self.assertIsNone(parse_video(regular))
        regular.pop('playlist_webpage_url')
        regular['webpage_url'] = 'https://www.youtube.com/@canal/videos'
        self.assertIsNone(parse_video(regular))

    async def test_legacy_ranking_cannot_download(self):
        item = parse_video(video())
        item.link_source = 'youtube-id'
        self.service.results = [item]
        self.service._persist_latest()
        with self.assertRaises(KwaiError):
            await self.service.start_download([item.key])
        self.service.load_latest()
        self.assertEqual(self.service.results, [])
        self.assertIn('refaça', self.service.status['message'])
        stored = json.loads((self.service.data_dir / 'latest.json').read_text(encoding='utf-8'))
        self.assertEqual(len(stored['results']), 1)

    async def test_flat_metrics_kept_when_metadata_fails(self):
        def failing(video_id):
            raise KwaiError('Indisponível')
        with patch_scan(self.service, [video()]), patch.object(self.service, '_video_info', failing):
            await self.service.start_analysis('@canal', 10, 0, None, 'views')
            await self.service.task
        self.assertEqual([(v.views, v.likes) for v in self.service.results], [(100, None)])
        self.assertIn('sem métricas completas', self.service.status['message'])

    async def test_min_views_prefilter_skips_metadata(self):
        fetched = []
        videos = [{**video(0), 'view_count': 50}, {**video(1), 'view_count': 5000}]
        with patch_scan(self.service, videos):
            original = self.service._video_info
            def tracked(video_id):
                fetched.append(video_id)
                return original(video_id)
            with patch.object(self.service, '_video_info', tracked):
                await self.service.start_analysis('@canal', 10, 1000, None, 'views')
                await self.service.task
        self.assertEqual(fetched, [videos[1]['id']])
        self.assertEqual(self.service.status['scanned'], 2)
        self.assertEqual(self.service.status['stop_reason'], 'end')

    async def test_partial_on_error(self):
        with patch_scan(self.service, [video()], fail=KwaiError('Bloqueio do YouTube')):
            await self.service.start_analysis('@canal', 10, 0, None, 'comments')
            await self.service.task
        self.assertEqual(self.service.status['stop_reason'], 'error')
        self.assertEqual(len(self.service.results), 1)


class YouTubeMappingTests(unittest.TestCase):
    def test_urls_and_unknown_metrics(self):
        self.assertEqual(normalize_youtube_url('@canal'), 'https://www.youtube.com/@canal/shorts')
        self.assertEqual(normalize_youtube_url('https://youtube.com/@canal/videos'), 'https://www.youtube.com/@canal/shorts')
        self.assertTrue(normalize_youtube_url('https://youtube.com/@canal/shorts').endswith('/shorts'))
        for url in ['https://youtu.be/abcdefgh000', 'https://youtube.com/playlist?list=PL123', 'https://youtube.com/watch?v=abcdefgh000', 'https://youtube.com/shorts/abcdefgh000', 'file:///etc/passwd', 'https://youtube.com.evil.test/watch?v=abcdefgh000', 'https://example.com', 'https://youtube.com/redirect?q=x']:
            with self.assertRaises(KwaiError):
                normalize_youtube_url(url)
        data = video()
        data['view_count'] = None
        result = parse_video(data)
        self.assertEqual(result.views, 0)
        self.assertEqual(result.hashtags, ['#humor', '#novela'])
        self.assertEqual(result.duration_seconds, 65)
        self.assertIsNone(parse_video(video(live_status='is_live')))

    def test_platform_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = YouTubeAnalyzerService(Path(directory))
            service.results = [parse_video(video())]
            with patch.object(main, 'youtube_service', service), TestClient(main.app) as client:
                response = client.get('/api/results?platform=youtube')
                self.assertEqual(response.json()['items'][0]['source'], 'youtube')
                self.assertEqual(client.get('/api/status?platform=invalid').status_code, 422)
                self.assertEqual(client.post('/api/analyze?platform=youtube', json={'profile_url': 'https://example.com'}).status_code, 400)
                self.assertEqual(client.get('/api/export/json?platform=youtube').json()['items'][0]['source'], 'youtube')


if __name__ == '__main__':
    unittest.main()
