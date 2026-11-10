import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app import browser_video, main, tiktok
from app.kwai import KwaiError
from app.tiktok import TikTokAnalyzerService, audio_candidates, feed_items, has_audio, media_candidates, normalize_tiktok_url, parse_tiktok_item

def stream(width, height, codec, bitrate, name):
    return dict(CodecType=codec, Bitrate=bitrate, PlayAddr=dict(Width=width, Height=height, UrlList=[f'https://v.test/{name}/a', f'https://v.test/{name}/b']))


def raw_item(index=0, views=100, author='canal', **extra):
    return {'id': str(7000000000000000000 + index), 'desc': 'Teste #Humor', 'author': {'uniqueId': author},
            'stats': {'playCount': views, 'diggCount': 10, 'commentCount': 2},
            'statsV2': {'playCount': str(views), 'diggCount': '10', 'commentCount': '2'},
            'textExtra': [{'hashtagName': 'Novela'}],
            'video': {'duration': 43, 'cover': 'https://c.test/1.jpg', 'playAddr': 'https://v.test/play', 'downloadAddr': 'https://v.test/watermark',
                      'bitrateInfo': [stream(720, 1280, 'h264', 1300000, 'h264-720'), stream(1080, 1920, 'h265_hvc1', 1346222, 'h265-1080')]}, **extra}


def page_of(*items, has_more=False, cursor='1'):
    return json.dumps({'statusCode': 0, 'itemList': list(items), 'hasMore': has_more, 'cursor': cursor})


SOUND = b'moov....hdlr\0\0\0\0\0\0\0\0soun'
SILENT = b'moov....hdlr\0\0\0\0\0\0\0\0vide'


class FakeResponse:
    def __init__(self, status=200, content=SOUND, kind='video/mp4'):
        self.ok, self.content, self.headers = status == 200, content, {'content-type': kind}

    async def body(self):
        return self.content


class FakeContext:
    def __init__(self, responses):
        self.responses = responses
        self.request = SimpleNamespace(get=self.get)

    async def get(self, url, headers, timeout):
        answer = self.responses.get(url, FakeResponse(403, b''))
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def close(self):
        pass


class MappingTests(unittest.TestCase):
    def test_profile_urls(self):
        self.assertEqual(normalize_tiktok_url('@canal.oficial'), 'https://www.tiktok.com/@canal.oficial')
        self.assertEqual(normalize_tiktok_url('tiktok.com/@canal/?lang=pt'), 'https://www.tiktok.com/@canal')
        for url in ['https://www.tiktok.com/@canal/video/7000000000000000000', 'https://vm.tiktok.com/abc', 'https://tiktok.com.evil.test/@canal', 'https://example.com/@canal', 'file:///etc/passwd', '']:
            with self.assertRaises(KwaiError):
                normalize_tiktok_url(url)

    def test_item_mapping(self):
        item = parse_tiktok_item(raw_item(views=477900), 3)
        self.assertEqual(item.key, 'tiktok:7000000000000000000')
        self.assertEqual(item.permalink, 'https://www.tiktok.com/@canal/video/7000000000000000000')
        self.assertEqual((item.views, item.likes, item.comments, item.duration_seconds, item.page), (477900, 10, 2, 43.0, 3))
        self.assertEqual(item.hashtags, ['#humor', '#novela'])
        self.assertIsNone(parse_tiktok_item(raw_item(imagePost={'images': []})))
        self.assertIsNone(parse_tiktok_item(raw_item(isAd=True)))

    def test_best_stream_without_watermark(self):
        video = raw_item()['video']
        self.assertEqual(media_candidates(video), ['https://v.test/h265-1080/a', 'https://v.test/h265-1080/b', 'https://v.test/play'])
        video['bitrateInfo'].append(stream(1080, 1920, 'h264', 900000, 'h264-1080'))
        self.assertEqual(media_candidates(video)[0], 'https://v.test/h264-1080/a')
        self.assertNotIn('https://v.test/watermark', media_candidates(video))

    def test_audio_sources_skip_the_silent_stream(self):
        raw = raw_item(music={'playUrl': 'https://m.test/som.mp3'})
        self.assertEqual(audio_candidates(raw), ['https://v.test/play', 'https://v.test/h264-720/a', 'https://v.test/h264-720/b', 'https://m.test/som.mp3'])
        own = raw_item(music={'playUrl': 'https://m.test/som.mp3', 'original': True, 'authorName': 'Canal', 'duration': 43})
        own['author']['nickname'] = 'Canal'
        self.assertEqual(audio_candidates(own)[0], 'https://m.test/som.mp3')
        own['music']['duration'] = 60
        self.assertEqual(audio_candidates(own)[-1], 'https://m.test/som.mp3')
        own['music']['duration'] = 43
        own['music']['authorName'] = 'Outro criador'
        self.assertEqual(audio_candidates(own)[-1], 'https://m.test/som.mp3')
        self.assertTrue(has_audio(SOUND))
        self.assertFalse(has_audio(SILENT))

    def test_feed_helper(self):
        self.assertIsNone(feed_items(''))
        self.assertIsNone(feed_items('{"statusCode": 10101}'))
        self.assertEqual(feed_items('{"hasMore": false}')['itemList'], [])

    def test_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TikTokAnalyzerService(Path(directory))
            service.results = [parse_tiktok_item(raw_item())]
            with patch.object(main, 'tiktok_service', service), TestClient(main.app) as client:
                self.assertEqual(client.get('/api/results?platform=tiktok').json()['items'][0]['source'], 'tiktok')
                self.assertEqual(client.post('/api/analyze?platform=tiktok', json={'profile_url': 'https://example.com'}).status_code, 400)
                self.assertEqual(client.post('/api/browser/start?platform=tiktok', json={'profile_url': 'https://example.com'}).status_code, 400)
                with patch.object(service, 'ensure_browser', AsyncMock(return_value={'ok': True})) as ensure:
                    self.assertEqual(client.post('/api/browser/start?platform=tiktok', json={'profile_url': '@Canal'}).status_code, 200)
                ensure.assert_awaited_once_with('https://www.tiktok.com/@Canal')
                self.assertIn('tiktok-ranking', client.get('/api/export/csv?platform=tiktok').headers['content-disposition'])
                self.assertEqual(client.post('/api/download/cancel?platform=tiktok').status_code, 200)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = TikTokAnalyzerService(Path(self.directory.name))
        self.service._open_profile = AsyncMock()
        self.service._scroll = AsyncMock()
        self.service.ensure_browser = AsyncMock()
        # By default the direct client is refused, which exercises the browser fallback.
        self.handler = lambda request: httpx.Response(403)
        self.service._open_client = AsyncMock(side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def test_opens_the_requested_profile(self):
        await self.service.start_analysis('@Canal', 10, 0, None, 'views')
        self.service.receive_feed(page_of(raw_item()))
        await self.service.task
        self.service._open_profile.assert_awaited_once_with('https://www.tiktok.com/@Canal')

    async def test_paginates_filters_and_persists(self):
        await self.service.start_analysis('@canal', 10, 100, '#novela', 'views')
        self.service.receive_feed(page_of(raw_item(0, 10), raw_item(1, 500), has_more=True))
        self.service.receive_feed('')
        self.service.receive_feed(page_of(raw_item(1, 500), raw_item(2, 900)))
        await self.service.task
        self.assertEqual([v.views for v in self.service.results], [900, 500])
        self.assertEqual(self.service.status['stop_reason'], 'end')
        self.assertTrue(self.service.status['reached_end'])
        self.assertEqual(self.service.status['scanned'], 3)
        restored = TikTokAnalyzerService(Path(self.directory.name))
        restored.load_latest()
        self.assertEqual(restored.results, self.service.results)

    async def test_limit_ranks_whole_profile_and_rejects_other_authors(self):
        await self.service.start_analysis('@canal', 2, 0, None, 'views')
        self.service.receive_feed(page_of(raw_item(0, author='outro'), raw_item(1, 10), raw_item(2, 20), has_more=True))
        self.service.receive_feed(page_of(raw_item(3, 30), raw_item(4, 40)))
        await self.service.task
        self.assertEqual([v.views for v in self.service.results], [40, 30])
        self.assertEqual(self.service.status['scanned'], 4)
        self.assertEqual(self.service.status['rejected_feeds'], 1)
        self.assertEqual(self.service.status['stop_reason'], 'end')

    async def test_browser_failure_reports_error(self):
        self.service._open_profile.side_effect = KwaiError('Não foi possível abrir o navegador do TikTok.')
        await self.service.start_analysis('@canal', 10, 0, None, 'views')
        await self.service.task
        self.assertEqual(self.service.status['stop_reason'], 'error')
        self.assertIn('navegador', self.service.status['message'])
        self.assertFalse(self.service.status['running'])

    async def test_closed_window_stops_collection(self):
        self.service._scroll.side_effect = KwaiError('A janela do navegador foi fechada durante a coleta.')
        await self.service.start_analysis('@canal', 10, 0, None, 'views')
        await self.service.task
        self.assertEqual(self.service.status['stop_reason'], 'error')

    async def test_stall_keeps_partial(self):
        with patch.object(browser_video, 'STALL_TIMEOUT', 0.01):
            await self.service.start_analysis('@canal', 10, 0, None, 'views')
            self.service.receive_feed(page_of(raw_item(), has_more=True))
            await self.service.task
        self.assertEqual(len(self.service.results), 1)
        self.assertEqual(self.service.status['stop_reason'], 'stalled')
        self.assertFalse(self.service.status['reached_end'])

    async def test_cancel_saves_partial(self):
        await self.service.start_analysis('@canal', 10, 0, None, 'views')
        self.service.receive_feed(page_of(raw_item(), has_more=True))
        while not self.service.results:
            await asyncio.sleep(0.01)
        await self.service.cancel()
        self.assertEqual(self.service.status['stop_reason'], 'cancelled')
        self.assertEqual(len(self.service.results), 1)

    async def test_download_skips_existing_tries_next_stream_and_continues(self):
        items = [parse_tiktok_item(raw_item(i)) for i in range(4)]
        self.service.results = items
        keys = [v.key for v in items]
        self.service._media_urls = {keys[1]: ['https://v.test/erro', 'https://v.test/ok'], keys[2]: ['https://v.test/html'], keys[3]: ['https://v.test/caiu', 'https://v.test/ok']}
        self.service.context = FakeContext({'https://v.test/erro': FakeResponse(403, b''), 'https://v.test/ok': FakeResponse(),
                                            'https://v.test/html': FakeResponse(content=b'<html>', kind='text/html'), 'https://v.test/caiu': OSError('rede')})
        existing = self.service.download_dir / f'tiktok-{items[0].photo_id}.mp4'
        existing.write_bytes(SOUND)
        await self.service.start_download(keys + keys)
        await self.service.download_task
        status = self.service.download_status
        self.assertEqual((status['completed'], status['failed'], status['total']), (3, 1, 4))
        self.assertEqual([row['status'] for row in status['items']], ['done', 'done', 'failed', 'done'])
        self.assertEqual(existing.read_bytes(), SOUND)
        self.assertEqual((self.service.download_dir / f'tiktok-{items[1].photo_id}.mp4').read_bytes(), SOUND)
        self.assertEqual(list(self.service.download_dir.glob('*.part')), [])
        self.assertFalse(status['running'])

    async def test_silent_best_stream_gets_audio_and_replaces_old_file(self):
        item = parse_tiktok_item(raw_item())
        self.service.results = [item]
        self.service._media_urls = {item.key: ['https://v.test/mudo']}
        self.service._audio_urls = {item.key: ['https://v.test/erro', 'https://v.test/tambem-mudo', 'https://m.test/som.mp3']}
        self.service.context = FakeContext({'https://v.test/mudo': FakeResponse(content=SILENT), 'https://v.test/tambem-mudo': FakeResponse(content=SILENT),
                                            'https://m.test/som.mp3': FakeResponse(content=b'ID3', kind='audio/mpeg')})
        path = self.service.download_dir / f'tiktok-{item.photo_id}.mp4'
        path.write_bytes(SILENT)

        def mux(ffmpeg, video, audio, output, duration):
            self.assertEqual((video.read_bytes(), audio.read_bytes(), duration), (SILENT, b'ID3', 43.0))
            output.write_bytes(SOUND)
        with patch.object(browser_video, 'find_ffmpeg', return_value='ffmpeg'), patch.object(self.service, '_mux', mux):
            await self.service.start_download([item.key])
            await self.service.download_task
        self.assertEqual(path.read_bytes(), SOUND)
        self.assertIsNone(self.service.download_status['items'][0]['error'])
        self.assertEqual(list(self.service.download_dir.glob('*.part')), [])

    async def test_video_without_any_audio_is_kept_and_flagged(self):
        item = parse_tiktok_item(raw_item())
        self.service.results = [item]
        self.service._media_urls = {item.key: ['https://v.test/mudo']}
        self.service.context = FakeContext({'https://v.test/mudo': FakeResponse(content=SILENT)})
        with patch.object(browser_video, 'find_ffmpeg', return_value='ffmpeg'):
            await self.service.start_download([item.key])
            await self.service.download_task
        row = self.service.download_status['items'][0]
        self.assertEqual(row['status'], 'done')
        self.assertIn('áudio', row['error'])

    async def test_missing_ffmpeg_fails_clearly(self):
        item = parse_tiktok_item(raw_item())
        self.service.results = [item]
        self.service._media_urls = {item.key: ['https://v.test/mudo']}
        self.service.context = FakeContext({'https://v.test/mudo': FakeResponse(content=SILENT)})
        with patch.object(browser_video, 'find_ffmpeg', return_value=None):
            await self.service.start_download([item.key])
            await self.service.download_task
        self.assertIn('FFmpeg', self.service.download_status['items'][0]['error'])

    async def test_big_file_arrives_in_order_from_parallel_pieces(self):
        content = SOUND + bytes(range(256)) * 20000
        seen = []

        def handler(request):
            start, end = map(int, request.headers['Range'].removeprefix('bytes=').split('-'))
            seen.append(start)
            piece = content[start:end + 1]
            return httpx.Response(206, content=piece, headers={'Content-Range': f'bytes {start}-{start + len(piece) - 1}/{len(content)}', 'Content-Type': 'video/mp4'})
        self.handler = handler
        item = parse_tiktok_item(raw_item())
        self.service.results = [item]
        self.service._media_urls = {item.key: ['https://v.test/grande']}
        self.service.context = FakeContext({})
        await self.service.start_download([item.key])
        await self.service.download_task
        self.assertEqual((self.service.download_dir / f'tiktok-{item.photo_id}.mp4').read_bytes(), content)
        self.assertGreater(len(seen), 1)
        self.assertIn('tempo', self.service.download_status['message'])

    async def test_cut_short_file_falls_back_to_browser(self):
        def handler(request):
            return httpx.Response(206, content=b'x', headers={'Content-Range': 'bytes 0-0/5', 'Content-Type': 'video/mp4'})
        self.handler = handler
        item = parse_tiktok_item(raw_item())
        self.service.results = [item]
        self.service._media_urls = {item.key: ['https://v.test/ok']}
        self.service.context = FakeContext({'https://v.test/ok': FakeResponse()})
        await self.service.start_download([item.key])
        await self.service.download_task
        self.assertEqual((self.service.download_dir / f'tiktok-{item.photo_id}.mp4').read_bytes(), SOUND)

    async def test_cancel_download_marks_pending(self):
        async def slow(*_):
            await asyncio.sleep(5)
        self.service.results = [parse_tiktok_item(raw_item())]
        self.service._fetch_media = slow
        await self.service.start_download([self.service.results[0].key])
        await asyncio.sleep(0.01)
        await self.service.cancel_download()
        self.assertEqual(self.service.download_status['items'][0]['status'], 'cancelled')
        self.assertFalse(self.service.download_status['running'])


if __name__ == '__main__':
    unittest.main()
