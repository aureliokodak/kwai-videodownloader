"""Stream media to a temporary file, publishing only complete downloads."""
from pathlib import Path

import httpx


async def save_media(client: httpx.AsyncClient, url: str, destination: Path) -> int:
    partial = destination.with_suffix(destination.suffix + '.part')
    size = 0
    try:
        async with client.stream('GET', url) as response:
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').lower()
            if content_type.startswith('text/') or 'json' in content_type:
                raise ValueError('O servidor retornou uma página de erro em vez do vídeo.')
            with partial.open('wb') as output:
                async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                    output.write(chunk)
                    size += len(chunk)
        if not size:
            raise ValueError('O servidor retornou um vídeo vazio.')
        partial.replace(destination)
        return size
    finally:
        partial.unlink(missing_ok=True)
