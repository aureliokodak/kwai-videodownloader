import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.kwai import KwaiAnalyzerService, parse_feed_video


def item():
    return parse_feed_video({'photo_id': '1234567890123456789', 'viewCount': 10}, 1)


class LinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = KwaiAnalyzerService(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    async def test_known_id_does_not_open_browser(self):
        self.service.results = [replace(item(), permalink=None)]
        self.service.ensure_browser = AsyncMock()
        await self.service._resolve_links_internal(20)
        self.service.ensure_browser.assert_not_awaited()
        self.assertEqual(self.service.results[0].permalink, item().permalink)

    def test_wrong_video_link_rejected(self):
        row = replace(item(), permalink=None)
        self.assertFalse(self.service._record_link(row, 'https://www.kwai.com/video/1234567890123456790', 'test'))
        self.assertIsNone(row.permalink)
        self.assertIsNotNone(row.link_error)

    def test_legacy_associations_are_quarantined_without_modifying_file(self):
        row = replace(item(), thumbnail='https://example.com/cover.jpg')
        path = self.service.data_dir / 'latest.json'
        original=json.dumps({'results':[row.public()]})
        path.write_text(original, encoding='utf-8')
        self.service.load_latest()
        self.assertIsNone(self.service.results[0].permalink)
        self.assertIsNone(self.service.results[0].photo_id)
        self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_dom_cannot_replace_api_cover_with_other_thumbnail(self):
        original=replace(item(), thumbnail='https://example.com/right.jpg')
        target={original.key:original}
        wrong=replace(original, source='dom-card', thumbnail='https://example.com/wrong.jpg')
        self.service._merge_item(target, wrong)
        self.assertEqual(target[original.key].thumbnail, original.thumbnail)

    async def test_navigation_during_click_is_captured(self):
        page = MagicMock()
        page.url = 'https://www.kwai.com/@test'
        async def evaluate(script, *args):
            if 'clickForTarget' in script:
                page.url = item().permalink
                return {'ok': True}
        page.evaluate = AsyncMock(side_effect=evaluate)
        result = await self.service._capture_card_link(page, {'key': 'test'})
        self.assertEqual(result['kind'], 'navigation')
        page.remove_listener.assert_called_once()
        self.assertIn('disarmLink', page.evaluate.call_args.args[0])

    async def test_cancellation_cleans_popup(self):
        page = MagicMock()
        popup = MagicMock()
        popup.close = AsyncMock()
        page.url = 'https://www.kwai.com/@test'
        async def evaluate(script, *args):
            if 'clickForTarget' in script:
                page.on.call_args.args[1](popup)
                self.service.cancel_event.set()
                return {'ok': True}
        page.evaluate = AsyncMock(side_effect=evaluate)
        self.assertIsNone(await self.service._capture_card_link(page, {'key': 'test'}))
        popup.close.assert_awaited_once()
        page.remove_listener.assert_called_once()

    async def test_partial_link_progress_saved_on_failure(self):
        self.service.results = [item()]
        self.service._resolve_links_internal = AsyncMock(side_effect=RuntimeError('browser closed'))
        await self.service._resolve_links_task(20)
        self.assertFalse(self.service.status['running'])
        restored = KwaiAnalyzerService(Path(self.directory.name))
        restored.load_latest()
        self.assertEqual(restored.results[0].permalink, item().permalink)

    async def test_batch_links_resolved_without_clicking(self):
        self.service.results = [replace(item(), key=f'card:{i}', photo_id=None, permalink=None) for i in range(100)]
        self.service.ensure_browser = AsyncMock()
        self.service.page = MagicMock()
        async def evaluate(script, *args):
            if 'findTargets' in script:
                return [{'key': row['key'], 'ok': True, 'url': f'https://www.kwai.com/video/{1234567890123456789+i}'} for i, row in enumerate(args[0])]
        self.service.page.evaluate = AsyncMock(side_effect=evaluate)
        self.service._capture_card_link = AsyncMock()
        await self.service._resolve_links_internal(100)
        self.assertEqual(sum(bool(row.permalink) for row in self.service.results), 100)
        self.service._capture_card_link.assert_not_awaited()
        self.assertEqual(sum('findTargets' in call.args[0] for call in self.service.page.evaluate.call_args_list), 1)

    async def test_scroll_motion_without_new_cards_does_not_loop(self):
        self.service.results = [replace(item(), key='thumb:missing', photo_id=None, permalink=None)]
        self.service.ensure_browser = AsyncMock()
        self.service.page = MagicMock()
        position = 0
        async def evaluate(script, *args):
            nonlocal position
            if 'gridKeys' in script:
                return ['same-card']
            if 'findTargets' in script:
                return []
            if 'getScrollInfo' in script:
                position += 100
                return {'top': position, 'scrollHeight': position+1000}
            if 'endState' in script:
                return {'atBottom':False}
        self.service.page.evaluate = AsyncMock(side_effect=evaluate)
        with patch('app.kwai.asyncio.sleep', new=AsyncMock()):
            await self.service._resolve_links_internal(20)
        self.assertLessEqual(self.service.status['link_rounds'], 9)
        self.assertEqual(self.service.status['links_pending'], 1)


if __name__ == '__main__':
    unittest.main()
