"""Opt-in read-only check of media metadata from the current saved ranking."""
import asyncio
import json
from pathlib import Path

import httpx

from app.durations import read_mp4_duration


async def main():
    root = Path(__file__).resolve().parents[1]
    saved = json.loads((root / 'data/latest.json').read_text(encoding='utf-8'))
    rows = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=8) as client:
        for item in saved['results']:
            try:
                duration = await asyncio.wait_for(read_mp4_duration(client, item['media_url']), timeout=12)
                rows.append({'photo_id': item['photo_id'], 'duration_seconds': duration})
                print(item['photo_id'], duration, flush=True)
            except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
                print(item['photo_id'], type(exc).__name__, flush=True)
    (root / 'tests/duration-live-results.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
    print('Verified:', sum(row['duration_seconds'] is not None for row in rows), '/', len(saved['results']))


asyncio.run(main())
