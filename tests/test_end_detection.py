import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.kwai import KwaiAnalyzerService, FeedCapture


class EndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = KwaiAnalyzerService(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    def test_api_terminal_requires_bottom_and_settling(self):
        self.service.status['api_end_observed'] = True
        self.assertFalse(self.service._confirm_dom_end({'atBottom': False}, 8, 85))
        self.assertFalse(self.service._confirm_dom_end({'atBottom': True}, 1, 85))
        self.assertTrue(self.service._confirm_dom_end({'atBottom': True}, 2, 85))
        self.assertEqual(self.service.status['end_evidence'], 'api-terminal-and-bottom')

    def test_loading_is_not_end(self):
        self.service.status['api_end_observed'] = True
        self.assertFalse(self.service._confirm_dom_end({'atBottom': True, 'loading': True}, 8, 85))

    def test_stable_scrollable_bottom_and_empty_initial_grid(self):
        self.assertFalse(self.service._confirm_dom_end({'atBottom': True, 'max': 0}, 8, 0))
        self.assertFalse(self.service._confirm_dom_end({'atBottom': True, 'max': 100}, 3, 85))
        self.assertTrue(self.service._confirm_dom_end({'atBottom': True, 'max': 100}, 4, 85))

    def test_link_search_does_not_change_collection_completion(self):
        self.assertTrue(self.service._confirm_dom_end({'atBottom': True, 'marker': 'No more videos'}, 1, 20, record=False))
        self.assertFalse(self.service.status['reached_end'])

    async def test_background_feed_provides_link_and_terminal_signal(self):
        data = {'feeds': [{'photo_id': '1234567890123456789', 'viewCount': 100}], 'pcursor': 'no_more'}
        self.service._feed_queue.put_nowait(FeedCapture('https://www.kwai.com/rest/o/w/pc/feed/profile','POST',{},'',json.dumps(data),'test'))
        self.service._take_hook_feed = AsyncMock(return_value=None)
        items = await self.service._collect_background_feed()
        self.assertEqual(items[0].permalink, 'https://www.kwai.com/video/1234567890123456789')
        self.assertTrue(self.service.status['api_end_observed'])

    async def test_error_without_feeds_is_not_terminal(self):
        self.service._feed_queue.put_nowait(FeedCapture('https://www.kwai.com/rest/o/w/pc/feed/profile','POST',{},'',json.dumps({'error': 'blocked','pcursor':'no_more'}),'test'))
        self.service._take_hook_feed = AsyncMock(return_value=None)
        self.assertEqual(await self.service._collect_background_feed(), [])
        self.assertFalse(self.service.status['api_end_observed'])
