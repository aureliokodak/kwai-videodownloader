"""Opt-in read-only browser verification against the running local app."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel='chrome', headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        await page.goto('http://127.0.0.1:8000/')
        await page.wait_for_function("async () => !(await (await fetch('/api/status')).json()).running", timeout=60000, polling=2000)
        await page.reload()
        response = await page.request.get('http://127.0.0.1:8000/api/results')
        rows = (await response.json())['items']
        assert rows and all(row.get('duration_seconds') for row in rows)
        await page.locator('[name="duration_seconds_min"]').fill('540')
        await page.locator('[name="duration_seconds_max"]').fill('570')
        expected = [row for row in rows if 540 <= row['duration_seconds'] <= 570]
        assert 0 < len(expected) < 100
        await page.wait_for_function('(n) => document.querySelectorAll(".rowcheck").length === n', arg=len(expected))
        actual = await page.locator('.rowcheck').evaluate_all('(els) => els.map(el => el.dataset.key)')
        assert set(actual) == {row['key'] for row in expected}
        assert '—' not in await page.locator('#resultsBody td:nth-child(7)').all_inner_texts()
        await page.locator('#rankingFilters').scroll_into_view_if_needed()
        await page.screenshot(path=str(Path(__file__).parent / 'duration-live-panel.png'))
        print(f'LIVE PANEL OK: {len(rows)} durations; {len(expected)} videos between 9:00 and 9:30.')
        await browser.close()


asyncio.run(main())
