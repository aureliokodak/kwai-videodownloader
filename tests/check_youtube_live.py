"""Opt-in network smoke check, isolated from the user's saved rankings."""
import asyncio
import json
import tempfile
from pathlib import Path

from app.youtube import YouTubeAnalyzerService

ROOT = Path(__file__).resolve().parents[1]


async def main():
    with tempfile.TemporaryDirectory() as directory:
        service = YouTubeAnalyzerService(Path(directory))
        # Use the application's bundled runtime without copying its binary for this test.
        service.base_dir = ROOT
        try:
            await service.start_analysis('https://www.youtube.com/@YouTube/shorts', 1, 0, None, 'views')
            await service.task
            print(json.dumps({'status': service.status, 'items': service.public_results()}, ensure_ascii=True))
            if service.status['error'] or not service.results:
                raise RuntimeError('Coleta real não concluída')
            await service.start_download([service.results[0].key])
            await service.download_task
            print(json.dumps(service.download_status, ensure_ascii=True))
            assert service.download_status['completed'] == 1
        finally:
            await service.close()


asyncio.run(main())
