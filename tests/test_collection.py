import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from app.kwai import (FeedCapture, KwaiAnalyzerService, KwaiError, VideoIndex, build_replay,
                      capture_starts_at_beginning, normalize_profile_url,
                      parse_feed_video, sort_items)


def video(identifier="1234567890123456789", **kwargs):
    return parse_feed_video({"photo_id": identifier, "viewCount": 10,
                             "caption": "Mesmo título #novela", **kwargs}, 1)


def capture(data, body='{"pcursor":""}'):
    return FeedCapture("https://www.kwai.com/rest/o/w/pc/feed/profile", "POST", {},
                       body, json.dumps(data), "test")


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = KwaiAnalyzerService(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    def test_distinct_ids_same_title_views_cover(self):
        result = {}
        first = replace(video(), thumbnail="https://example.com/shared.jpg")
        second = replace(video("1234567890123456790"), thumbnail=first.thumbnail)
        self.service._merge_item(result, first)
        self.service._merge_item(result, second)
        self.assertEqual(len(result), 2)

    def test_dom_enriches_exact_video_without_duplicate(self):
        result = VideoIndex()
        first = video()
        self.service._merge_item(result, first)
        self.service._merge_item(result, replace(first, key="dom-key", source="dom-card", views=12))
        self.assertEqual(len(result), 1)
        self.assertEqual(next(iter(result.values())).views, 12)

    def test_index_rekeys_dom_card_when_id_is_discovered(self):
        result = VideoIndex()
        first = replace(video(), photo_id=None, permalink=None, key='thumb:cover', thumbnail='https://example.com/cover.jpg')
        self.service._merge_item(result, first)
        self.service._merge_item(result, replace(video(), thumbnail=first.thumbnail))
        self.assertEqual(list(result), [video().key])
        self.assertEqual(result.thumbnails[first.thumbnail], {video().key})

    def test_precision_and_counts(self):
        self.assertEqual(video().photo_id, "1234567890123456789")
        self.assertEqual(video(viewCount="1,2 mil").views, 1200)
        self.assertEqual(sort_items([replace(video(), likes=None), replace(video('1234567890123456790'), likes=0)], 'likes')[0].likes, 0)

    def test_replay_nested_and_initial_capture(self):
        url, body, changed = build_replay('https://www.kwai.com/rest/o/w/pc/feed/profile', '{"variables":{"pcursor":""}}', 'next-token')
        self.assertTrue(changed)
        self.assertEqual(json.loads(body)['variables']['pcursor'], 'next-token')
        self.assertFalse(capture_starts_at_beginning(capture({}, body)))
        self.assertTrue(capture_starts_at_beginning(capture({})))

    def test_profile_inputs(self):
        for value in ['@usuario', 'usuario', 'www.kwai.com/@usuario', 'https://www.kwai.com/@usuario']:
            self.assertEqual(normalize_profile_url(value), 'https://www.kwai.com/@usuario')

    async def run_analysis(self, cap):
        self.service._capture_first_feed = AsyncMock(return_value=cap)
        self.service._scan_dom = AsyncMock()
        await self.service._analyze('https://www.kwai.com/@test', '@test', 100, 0, None, 'views', 5, 10, False, True)

    async def test_missing_cursor_falls_back(self):
        await self.run_analysis(capture({'feeds': [{'photo_id': '1234567890123456789', 'viewCount': 10}]}))
        self.service._scan_dom.assert_awaited_once()
        self.assertFalse(self.service.status['reached_end'])
        self.assertEqual(len(self.service.results), 1)

    async def test_explicit_end_is_complete(self):
        await self.run_analysis(capture({'feeds': [{'photo_id': '1234567890123456789', 'viewCount': 10}], 'pcursor': 'no_more'}))
        self.service._scan_dom.assert_not_awaited()
        self.assertTrue(self.service.status['reached_end'])

    async def test_collection_filters_continue_until_matching_video(self):
        first = capture({'feeds': [
            {'photo_id': '1234567890123456789', 'viewCount': 99999, 'caption': '#novela'},
            {'photo_id': '1234567890123456790', 'viewCount': 200000, 'caption': '#novelas'},
        ], 'pcursor': 'page2'})
        second = capture({'feeds': [
            {'photo_id': '1234567890123456791', 'viewCount': 100000, 'caption': '#novela'},
        ], 'pcursor': 'no_more'})
        self.service._capture_first_feed = AsyncMock(return_value=first)
        self.service._replay_feed = AsyncMock(return_value=(second.response_text, second))
        await self.service._analyze('https://www.kwai.com/@test', '@test', 1, 100000, '#novela', 'views', 5, 10, False, True)
        self.service._replay_feed.assert_awaited_once()
        self.assertEqual(self.service.status['scanned'], 3)
        self.assertEqual([item.photo_id for item in self.service.results], ['1234567890123456791'])

    def test_visual_items_use_same_collection_filters(self):
        from app.kwai import parse_dom_video
        item = parse_dom_video({'key': 'card1', 'views': 100000, 'hashtags': ['#NOVELA']})
        self.assertTrue(self.service._match_item(item, '#novela', 100000))
        self.assertFalse(self.service._match_item(item, '#novelas', 100000))
        self.assertFalse(self.service._match_item(item, '#novela', 100001))

    async def test_multiple_collection_hashtags_match_any_exact_tag(self):
        first = capture({'feeds': [
            {'photo_id': '1234567890123456789', 'viewCount': 99999, 'caption': '#humor'},
            {'photo_id': '1234567890123456790', 'viewCount': 200000, 'caption': '#novelas #humorista'},
        ], 'pcursor': 'page2'})
        second = capture({'feeds': [
            {'photo_id': '1234567890123456791', 'viewCount': 100000, 'caption': '#HUMOR'},
            {'photo_id': '1234567890123456792', 'viewCount': 110000, 'caption': '#novela'},
            {'photo_id': '1234567890123456793', 'viewCount': 120000, 'caption': '#novela #humor'},
        ], 'pcursor': 'no_more'})
        self.service._capture_first_feed = AsyncMock(return_value=first)
        self.service._replay_feed = AsyncMock(return_value=(second.response_text, second))
        self.service._scan_dom = AsyncMock()
        await self.service.start_analysis('@test', 3, 100000, '#NOVELA, humor; #novela', 'views', auto_links=False)
        await self.service.task
        self.service._replay_feed.assert_awaited_once()
        self.service._scan_dom.assert_not_awaited()
        self.assertEqual(self.service.status['scanned'], 5)
        self.assertEqual([item.photo_id for item in self.service.results], [
            '1234567890123456793', '1234567890123456792', '1234567890123456791',
        ])

    def test_visual_multiple_hashtags_and_empty_filter(self):
        from app.kwai import parse_dom_video
        item = parse_dom_video({'key': 'card1', 'views': 100000, 'hashtags': ['#AÇÃO']})
        for separator in (' ', ',', ';', '\n', ', '):
            with self.subTest(separator=separator):
                self.assertTrue(self.service._match_item(item, separator.join(['#novela', 'AÇÃO']), 100000))
        self.assertFalse(self.service._match_item(item, '#ações #novela', 0))
        self.assertFalse(self.service._match_item(item, '#ação #novela', 100001))
        without_tags = replace(item, hashtags=[])
        self.assertFalse(self.service._match_item(without_tags, '#ação #novela', 0))
        for empty in (None, '', '  ', ', ; '):
            self.assertTrue(self.service._match_item(without_tags, empty, 100000))

    async def test_late_capture_checks_earlier_cards(self):
        await self.run_analysis(capture({'feeds': [{'photo_id': '1234567890123456789', 'viewCount': 10}], 'pcursor': 'no_more'}, '{"pcursor":"page2"}'))
        self.service._scan_dom.assert_awaited_once()

    async def test_late_capture_restarts_api_instead_of_full_dom_rescan(self):
        late = capture({'feeds': [{'photo_id': '1234567890123456790', 'viewCount': 10}], 'pcursor': 'no_more'}, '{"pcursor":"page2"}')
        complete = capture({'feeds': [
            {'photo_id': '1234567890123456789', 'viewCount': 20},
            {'photo_id': '1234567890123456790', 'viewCount': 10},
        ], 'pcursor': 'no_more'})
        self.service._replay_feed = AsyncMock(return_value=(complete.response_text, complete))
        await self.run_analysis(late)
        self.service._scan_dom.assert_not_awaited()
        self.assertEqual(len(self.service.results), 2)
        self.assertTrue(self.service.status['reached_end'])

    async def test_invalid_json_falls_back(self):
        cap = capture({})
        cap.response_text = '<html>temporary error</html>'
        await self.run_analysis(cap)
        self.service._scan_dom.assert_awaited_once()

    async def test_concurrent_link_operation_rejected(self):
        self.service.task = asyncio.create_task(asyncio.sleep(30))
        try:
            with self.assertRaises(KwaiError):
                await self.service.resolve_links()
        finally:
            self.service.task.cancel()
            await asyncio.gather(self.service.task, return_exceptions=True)

    def test_atomic_persistence_round_trip(self):
        self.service.results = [video()]
        self.service._persist_latest()
        restored = KwaiAnalyzerService(Path(self.directory.name))
        restored.load_latest()
        self.assertEqual(restored.results[0].photo_id, video().photo_id)
        self.assertFalse(restored.status['browser_open'])


if __name__ == '__main__':
    unittest.main()
