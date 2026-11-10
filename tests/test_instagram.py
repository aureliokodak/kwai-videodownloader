import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app import main
from app.instagram import (InstagramAnalyzerService, dash_streams, find_sources, media_sources, normalize_instagram_url,
                           parse_instagram_media, parse_reels_payload)
from app.kwai import KwaiError

SOUND = b'moov....hdlr\0\0\0\0\0\0\0\0soun'
MPD = ('<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'
       '<AdaptationSet contentType="video">'
       '<Representation mimeType="video/mp4" codecs="vp09.00.40.08" width="1080" height="1920" bandwidth="2000000"><BaseURL>https://cdn.test/vp9-1080.mp4</BaseURL></Representation>'
       '<Representation mimeType="video/mp4" codecs="avc1.64001f" width="1080" height="1920" bandwidth="1800000"><BaseURL>https://cdn.test/avc-1080.mp4</BaseURL></Representation>'
       '<Representation mimeType="video/mp4" codecs="avc1.64001e" width="480" height="854" bandwidth="400000"><BaseURL>https://cdn.test/avc-480.mp4</BaseURL></Representation>'
       '</AdaptationSet><AdaptationSet contentType="audio">'
       '<Representation mimeType="audio/mp4" codecs="mp4a.40.5" bandwidth="64000"><BaseURL>https://cdn.test/a64.mp4</BaseURL></Representation>'
       '<Representation mimeType="audio/mp4" codecs="mp4a.40.2" bandwidth="128000"><BaseURL>https://cdn.test/a128.mp4</BaseURL></Representation>'
       '</AdaptationSet></Period></MPD>')
REELS = 'xdt_api__v1__clips__user__connection_v2'
POSTS = 'xdt_api__v1__feed__user_timeline_graphql_connection'


def media(code='ABCDEF1', views=100, owner='canal', **extra):
    return {'code': code, 'pk': '1', 'media_type': 2, 'product_type': 'clips', 'play_count': views, 'like_count': 10,
            'comment_count': 2, 'caption': {'text': 'Reel #Humor'}, 'video_duration': 30.5, 'user': {'username': owner},
            'image_versions2': {'candidates': [{'url': 'https://cdn.test/t.jpg'}]},
            'video_versions': [{'width': 720, 'height': 1280, 'url': 'https://cdn.test/p720.mp4'},
                               {'width': 480, 'height': 854, 'url': 'https://cdn.test/p480.mp4'}],
            'video_dash_manifest': MPD, **extra}


def connection(*items, more=False, key=REELS):
    return json.dumps({'data': {key: {'edges': [{'node': {'media': m}} for m in items],
                                      'page_info': {'has_next_page': more, 'end_cursor': 'c'}}}})


class FakeResponse:
    def __init__(self, content=SOUND, kind='video/mp4'):
        self.ok, self.content, self.headers = True, content, {'content-type': kind}

    async def body(self):
        return self.content


class MappingTests(unittest.TestCase):
    def test_profile_urls(self):
        self.assertEqual(normalize_instagram_url('@canal.ok'), 'https://www.instagram.com/canal.ok/')
        self.assertEqual(normalize_instagram_url('instagram.com/canal/reels/?hl=pt'), 'https://www.instagram.com/canal/')
        self.assertEqual(normalize_instagram_url('https://www.instagram.com/Canal_1'), 'https://www.instagram.com/Canal_1/')
        for url in ['https://www.instagram.com/p/ABCDEF1/', 'https://www.instagram.com/reel/ABCDEF1/', 'https://www.instagram.com/stories/canal/1/',
                    'https://www.instagram.com/explore/', 'https://instagram.com.evil.test/canal', 'https://example.com/canal', 'file:///etc', '']:
            with self.assertRaises(KwaiError):
                normalize_instagram_url(url)

    def test_best_quality_first_and_audio_for_silent_dash(self):
        videos, audio = media_sources(media())
        # Same resolution: H.264 beats VP9 so the file opens in common editors.
        self.assertEqual(videos, ['https://cdn.test/avc-1080.mp4', 'https://cdn.test/vp9-1080.mp4', 'https://cdn.test/p720.mp4',
                                  'https://cdn.test/p480.mp4', 'https://cdn.test/avc-480.mp4'])
        self.assertEqual(audio, ['https://cdn.test/a128.mp4', 'https://cdn.test/a64.mp4', 'https://cdn.test/p720.mp4', 'https://cdn.test/p480.mp4'])
        # A progressive file already has sound, so it wins a tie.
        same = media(video_versions=[{'width': 1080, 'height': 1920, 'url': 'https://cdn.test/p1080.mp4'}])
        self.assertEqual(media_sources(same)[0][0], 'https://cdn.test/p1080.mp4')
        self.assertEqual(media_sources(media(video_dash_manifest=None)), (['https://cdn.test/p720.mp4', 'https://cdn.test/p480.mp4'],) * 2)

    def test_manifest_safety(self):
        self.assertEqual(dash_streams('<!DOCTYPE x [<!ENTITY a "b">]><MPD/>'), ([], []))
        self.assertEqual(dash_streams('não é xml'), ([], []))

    def test_item_mapping(self):
        item = parse_instagram_media(media(views=4500, like_count=None), 2)
        self.assertEqual((item.key, item.photo_id, item.permalink), ('instagram:ABCDEF1', 'ABCDEF1', 'https://www.instagram.com/reel/ABCDEF1/'))
        self.assertEqual((item.views, item.likes, item.comments, item.duration_seconds, item.page), (4500, None, 2, 30.5, 2))
        self.assertEqual((item.title, item.hashtags, item.thumbnail), ('Reel #Humor', ['#humor'], 'https://cdn.test/t.jpg'))
        self.assertEqual(item.media_url, 'https://cdn.test/avc-1080.mp4')
        self.assertEqual(parse_instagram_media(media(play_count=None, ig_play_count=None, view_count=77)).views, 77)
        photo = {'code': 'FOTO123', 'media_type': 1, 'product_type': 'feed'}
        self.assertIsNone(parse_instagram_media(photo))
        self.assertIsNone(parse_instagram_media({'code': 'CARRO12', 'media_type': 8, 'product_type': 'carousel_container'}))

    def test_payload_paging_owner_and_formats(self):
        page = parse_reels_payload(connection(media('AAAAA1'), media('BBBBB2', owner='outro'), more=True), 'canal', 1)
        self.assertEqual([entry[0].photo_id for entry in page.entries], ['AAAAA1'])
        self.assertEqual((page.rejected, page.has_more, page.cursor), (1, True, 'c'))
        # The posts grid ending says nothing about the Reels tab.
        self.assertIsNone(parse_reels_payload(connection(more=False, key=POSTS), 'canal', 1))
        self.assertIsNone(parse_reels_payload(connection(media('AAAAA1'), more=False, key=POSTS), 'canal', 1).has_more)
        self.assertIs(parse_reels_payload(connection(more=False), 'canal', 1).has_more, False)
        v1 = 'for (;;);' + json.dumps({'items': [{'media': media('CCCCC3')}], 'paging_info': {'more_available': False, 'max_id': 'm'}})
        page = parse_reels_payload(v1, 'canal', 1)
        self.assertEqual((page.has_more, page.cursor, page.entries[0][0].photo_id), (False, 'm', 'CCCCC3'))
        streamed = json.dumps({'data': {'x': 1}}) + '\n' + connection(media('DDDDD4'), more=True)
        self.assertEqual(parse_reels_payload(streamed, 'canal', 1).entries[0][0].photo_id, 'DDDDD4')
        self.assertIsNone(parse_reels_payload(json.dumps({'data': {'viewer': {'id': 1}}}), 'canal', 1))
        self.assertIsNone(parse_reels_payload('<html>', 'canal', 1))
        self.assertEqual(find_sources(connection(media('EEEEE5')), 'EEEEE5')[0][0], 'https://cdn.test/avc-1080.mp4')
        self.assertIsNone(find_sources(connection(media('EEEEE5')), 'OUTRO99'))

    def test_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            service = InstagramAnalyzerService(Path(directory))
            service.results = [parse_instagram_media(media())]
            with patch.object(main, 'instagram_service', service), TestClient(main.app) as client:
                self.assertEqual(client.get('/api/results?platform=instagram').json()['items'][0]['source'], 'instagram')
                self.assertEqual(client.post('/api/analyze?platform=instagram', json={'profile_url': 'https://www.instagram.com/p/ABCDEF1/'}).status_code, 400)
                self.assertEqual(client.post('/api/browser/start?platform=instagram', json={'profile_url': 'https://example.com'}).status_code, 400)
                with patch.object(service, 'ensure_browser', AsyncMock(return_value={'ok': True})) as ensure:
                    self.assertEqual(client.post('/api/browser/start?platform=instagram', json={'profile_url': '@Canal'}).status_code, 200)
                    self.assertEqual(client.post('/api/browser/start?platform=instagram', json={}).status_code, 200)
                self.assertEqual([call.args[0] for call in ensure.await_args_list],
                                 ['https://www.instagram.com/Canal/', 'https://www.instagram.com/accounts/login/'])
                self.assertIn('instagram-ranking', client.get('/api/export/csv?platform=instagram').headers['content-disposition'])
                self.assertEqual(client.post('/api/download/cancel?platform=instagram').status_code, 200)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = InstagramAnalyzerService(Path(self.directory.name))
        self.service._open_profile = AsyncMock()
        self.service._scroll = AsyncMock()
        self.service._initial_payloads = AsyncMock(return_value=[])
        self.service.ensure_browser = AsyncMock()
        self.service._open_client = AsyncMock(side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))))

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def test_embedded_page_then_feed_until_reels_end(self):
        bare = media('AAAAA1', views=50, like_count=None, video_versions=None, video_dash_manifest=None)
        self.service._initial_payloads.return_value = [connection(bare, media('BBBBB2', views=900), more=True)]
        await self.service.start_analysis('@canal', 10, 0, None, 'views')
        self.service.receive_feed(json.dumps({'data': {'viewer': {}}}))
        self.service.receive_feed(connection(media('AAAAA1', views=50, like_count=33), media('CCCCC3', views=300)))
        await self.service.task
        self.service._open_profile.assert_awaited_once_with('https://www.instagram.com/canal/')
        self.assertEqual(self.service.page_url('https://www.instagram.com/canal/'), 'https://www.instagram.com/canal/reels/')
        self.assertEqual([v.photo_id for v in self.service.results], ['BBBBB2', 'CCCCC3', 'AAAAA1'])
        # The second sighting filled what the first one lacked.
        first = self.service.results[2]
        self.assertEqual((first.likes, first.media_url), (33, 'https://cdn.test/avc-1080.mp4'))
        self.assertEqual(self.service._audio_urls['instagram:AAAAA1'][0], 'https://cdn.test/a128.mp4')
        self.assertEqual((self.service.status['stop_reason'], self.service.status['scanned'], self.service.status['pages']), ('end', 3, 2))

    async def test_stops_when_scrolling_brings_nothing_new(self):
        self.service.IDLE_END = 0.01
        await self.service.start_analysis('@canal', 10, 0, None, 'views')
        self.service.receive_feed(connection(media(), more=True))
        await self.service.task
        self.assertEqual(self.service.status['stop_reason'], 'idle')
        self.assertFalse(self.service.status['reached_end'])
        self.assertEqual(len(self.service.results), 1)

    async def test_download_asks_the_reel_page_when_links_were_not_kept(self):
        item = parse_instagram_media(media())
        self.service.results = [item]
        self.service._resolve_sources = AsyncMock(return_value=(['https://cdn.test/novo.mp4'], []))
        calls = []

        async def get(url, headers, timeout):
            calls.append(url)
            return FakeResponse()
        self.service.context = SimpleNamespace(request=SimpleNamespace(get=get), close=AsyncMock())
        await self.service.start_download([item.key])
        await self.service.download_task
        self.service._resolve_sources.assert_awaited_once_with(item)
        self.assertEqual(calls, ['https://cdn.test/novo.mp4'])
        self.assertEqual((self.service.download_dir / 'instagram-ABCDEF1.mp4').read_bytes(), SOUND)
        self.assertIn('downloads/instagram', self.service.download_status['message'])


if __name__ == '__main__':
    unittest.main()
