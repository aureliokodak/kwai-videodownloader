import tempfile
import unittest
from pathlib import Path

import httpx

from app.downloads import save_media


class DownloadsTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_and_publishes_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            data = b'video' * 100000
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=data, headers={'content-type': 'video/mp4'}))) as client:
                self.assertEqual(await save_media(client, 'https://example.com/video', path), len(data))
            self.assertEqual(path.read_bytes(), data)
            self.assertFalse(path.with_suffix('.mp4.part').exists())

    async def test_error_or_empty_response_preserves_previous_file(self):
        for content, mime in [(b'<html>Error</html>', 'text/html'), (b'', 'video/mp4')]:
            with self.subTest(mime=mime), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'video.mp4'
                path.write_bytes(b'original')
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=content, headers={'content-type': mime}))) as client:
                    with self.assertRaises(ValueError):
                        await save_media(client, 'https://example.com/video', path)
                self.assertEqual(path.read_bytes(), b'original')
                self.assertFalse(path.with_suffix('.mp4.part').exists())
