"""Read MP4 movie metadata with bounded range requests; never download the movie."""
import re
import struct

import httpx


async def read_mp4_duration(client: httpx.AsyncClient, url: str) -> float | None:
    async def read(offset: int, length: int) -> bytes:
        async with client.stream('GET', url, headers={
            'Range': f'bytes={offset}-{offset + length - 1}', 'Accept-Encoding': 'identity',
        }) as response:
            response.raise_for_status()
            if response.status_code == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(?:\d+|\*)', response.headers.get('content-range', ''))
                if not match or int(match[1]) != offset:
                    raise ValueError('Invalid byte range')
            elif response.status_code != 200 or offset != 0:
                raise ValueError('Server does not support byte ranges')
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=length):
                data.extend(chunk[:length - len(data)])
                if len(data) >= length:
                    break
            return bytes(data)

    offset = 0
    movie_end = None
    # Skip large mdat boxes by their declared size, reading at most 2 KB total.
    for _ in range(32):
        header = await read(offset, 64)
        if len(header) < 8:
            return None
        size, kind = struct.unpack('>I4s', header[:8])
        header_size = 8
        if size == 1:
            if len(header) < 16:
                return None
            size = struct.unpack('>Q', header[8:16])[0]
            header_size = 16
        if size < header_size or (movie_end is not None and offset + size > movie_end):
            return None
        if kind == b'moov' and movie_end is None:
            movie_end = offset + size
            offset += header_size
            continue
        if kind == b'mvhd' and movie_end is not None:
            payload = header[header_size:]
            if len(payload) < 32:
                return None
            if payload[0] == 0:
                if size < header_size + 20:
                    return None
                scale, duration = struct.unpack('>II', payload[12:20])
                unknown = 0xffffffff
            elif payload[0] == 1:
                if size < header_size + 32:
                    return None
                scale, duration = struct.unpack('>IQ', payload[20:32])
                unknown = 0xffffffffffffffff
            else:
                return None
            return duration / scale if scale and 0 < duration < unknown else None
        offset += size
        if movie_end is not None and offset >= movie_end:
            return None
    return None
