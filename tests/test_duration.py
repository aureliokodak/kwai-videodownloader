import json
import asyncio
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.kwai import KwaiAnalyzerService, KwaiError, parse_dom_video, parse_feed_video
from app.durations import read_mp4_duration


def video(**metadata):
    return parse_feed_video(dict(photo_id='1234567890123456789', viewCount=100, **metadata), 1)


class DurationTests(unittest.TestCase):
    def test_real_kwai_manifest_and_extra_video_units(self):
        # Structure and values observed on /video/5228829272570100930.
        manifest = {'adaptationSet': [{'id': 1, 'duration': 585886, 'representation': []}]}
        self.assertEqual(video(transcode_manifest_info=manifest,
                               ext_params={'sound': 585725, 'video': 585840}).duration_seconds, 585.886)
        self.assertEqual(video(transcode_manifest_info=json.dumps(manifest)).duration_seconds, 585.886)
        self.assertEqual(video(ext_params={'sound': 585725, 'video': 585840}).duration_seconds, 585.84)
        self.assertIsNone(video(ext_params={'sound': 585725}).duration_seconds)
        self.assertIsNone(video(music={'duration': 999999}, recommendations=[manifest]).duration_seconds)
        self.assertIsNone(video(transcode_manifest_info={'adaptationSet': None}).duration_seconds)
        self.assertEqual(video(transcode_manifest_info='bad JSON', ext_params={'video': 60500}).duration_seconds, 60.5)

    def test_units_and_invalid_metadata(self):
        self.assertEqual(video(photo_duration=60500).duration_seconds, 60.5)
        self.assertEqual(video(video={'durationMs': '12500'}).duration_seconds, 12.5)
        self.assertEqual(video(duration_seconds=3601).duration_seconds, 3601)
        self.assertIsNone(video(music={'durationMs': 30000}).duration_seconds)
        for value in (None, '', True, -1, 0, 'nan', 'inf', 'invalid'):
            self.assertIsNone(video(duration_seconds=value).duration_seconds)
        self.assertEqual(parse_dom_video({'key': 'card', 'views': 1, 'duration_seconds': 90}).duration_seconds, 90)

    def test_merge_and_old_saved_rankings(self):
        with tempfile.TemporaryDirectory() as directory:
            service = KwaiAnalyzerService(Path(directory))
            known = video(duration_seconds=60.5)
            merged = {}
            service._merge_item(merged, known)
            service._merge_item(merged, video())
            service._merge_item(merged, replace(known, source='dom-card', duration_seconds=60))
            self.assertEqual(merged[known.key].duration_seconds, 60.5)
            service.results = list(merged.values())
            service._persist_latest()
            service.load_latest()
            self.assertEqual(service.results[0].duration_seconds, 60.5)
            path = service.data_dir / 'latest.json'
            saved = json.loads(path.read_text(encoding='utf-8'))
            del saved['results'][0]['duration_seconds']
            path.write_text(json.dumps(saved), encoding='utf-8')
            service.load_latest()
            self.assertEqual(len(service.results), 1)
            self.assertIsNone(service.results[0].duration_seconds)


def box(kind, payload):
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_mp4_movie_before_or_after_media_and_64_bit_header(self):
        for version in (0, 1):
            payload = (bytes(12) + struct.pack('>II', 1000, 585886) if version == 0 else
                       bytes([1]) + bytes(19) + struct.pack('>IQ', 1000, 585886)) + bytes(80)
            movie = box(b'moov', box(b'mvhd', payload))
            media = struct.pack('>I4sQ', 1, b'mdat', 16 + 100000) + bytes(100000)
            for parts in ((movie, media), (media, movie)):
                content = box(b'ftyp', b'isom' + bytes(12)) + b''.join(parts)
                ranges = []
                def handler(request):
                    start, end = map(int, request.headers['range'][6:].split('-'))
                    ranges.append((start, end))
                    chunk = content[start:end + 1]
                    return httpx.Response(206, headers={'content-range': f'bytes {start}-{start+len(chunk)-1}/{len(content)}'}, content=chunk)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    self.assertEqual(await read_mp4_duration(client, 'https://example.com/video.mp4'), 585.886)
                self.assertLessEqual(sum(end - start + 1 for start, end in ranges), 256)

    async def test_invalid_media_and_ignored_ranges_fail_safely(self):
        for content in (b'<html>error</html>', bytes(64), box(b'moov', box(b'mvhd', bytes(80)))):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))) as client:
                try:
                    self.assertIsNone(await read_mp4_duration(client, 'https://example.com/bad.mp4'))
                except ValueError:
                    pass

    async def test_update_existing_keeps_ranking_and_survives_unavailable_media(self):
        with tempfile.TemporaryDirectory() as directory:
            service = KwaiAnalyzerService(Path(directory))
            service.results = [replace(video(), media_url='https://example.com/one.mp4'),
                               replace(video(), key='two', media_url='https://example.com/two.mp4'),
                               replace(video(duration_seconds=20), key='known')]
            original = [item.public() for item in service.results]
            service.status['stop_reason'] = 'limit-reached'
            reader = AsyncMock(side_effect=[585.886, httpx.ReadTimeout('unavailable')])
            with patch('app.kwai.read_mp4_duration', reader):
                await service.update_durations()
                with self.assertRaises(KwaiError):
                    await service.update_durations()
                await service.task
            self.assertEqual([x.duration_seconds for x in service.results], [585.886, None, 20])
            self.assertFalse(service.status['running'])
            self.assertEqual(service.status['stop_reason'], 'limit-reached')
            for before, after in zip(original, service.public_results()):
                self.assertEqual({k: v for k, v in before.items() if k != 'duration_seconds'},
                                 {k: v for k, v in after.items() if k != 'duration_seconds'})
            service.load_latest()
            self.assertEqual(service.results[0].duration_seconds, 585.886)

    async def test_cancel_preserves_partial_duration_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            service = KwaiAnalyzerService(Path(directory))
            service.results = [replace(video(), key=str(i), media_url='https://example.com/a.mp4') for i in range(2)]
            async def read(*args):
                await service.cancel()
                return 10
            with patch('app.kwai.read_mp4_duration', side_effect=read) as reader:
                await service.update_durations()
                await service.task
                self.assertEqual(reader.call_count, 1)
            self.assertEqual([x.duration_seconds for x in service.results], [10, None])
