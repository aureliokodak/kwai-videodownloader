import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import main
from app.kwai import KwaiAnalyzerService, parse_feed_video


class ApiTests(unittest.TestCase):
    def test_page_results_exports_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = KwaiAnalyzerService(Path(directory))
            service.results = [parse_feed_video({'photo_id': '1234567890123456789', 'viewCount': 1, 'caption': '=1+1'}, 1)]
            with patch.object(main, 'service', service), TestClient(main.app) as client:
                self.assertIn('rankingFilters', client.get('/').text)
                self.assertEqual(client.get('/api/status').headers['cache-control'], 'no-store')
                self.assertRegex(client.get('/').text, r'app.js\?v=[a-f0-9]{16}')
                self.assertEqual(client.get('/api/results').json()['count'], 1)
                self.assertEqual(client.get('/api/export/json').json()['items'][0]['photo_id'], '1234567890123456789')
                self.assertIn("'=1+1", client.get('/api/export/csv').text)
                with patch.object(service, 'update_durations', new_callable=AsyncMock) as update:
                    self.assertEqual(client.post('/api/durations').status_code, 200)
                    update.assert_awaited_once()
                self.assertEqual(client.post('/api/analyze', json={'profile_url': '@test', 'limit': 0}).status_code, 422)
                self.assertEqual(client.post('/api/browser/start', json={'profile_url': 'https://example.com'}).status_code, 400)
                service.status['running'] = True
                self.assertEqual(client.post('/api/durations').status_code, 400)
                self.assertEqual(client.post('/api/browser/start', json={'profile_url': '@test'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
