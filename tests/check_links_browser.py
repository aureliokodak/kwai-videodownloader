"""Exercise native browser link behavior against local fixtures, without Kwai traffic."""
import asyncio
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from app.kwai import KwaiAnalyzerService

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://www.kwai.com/video/1234567890123456789'


async def main():
    with tempfile.TemporaryDirectory() as directory:
        service = KwaiAnalyzerService(Path(directory))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            context = await browser.new_context()
            await context.route('https://www.kwai.com/**', lambda route: route.fulfill(body='<html>Video fixture</html>', content_type='text/html'))
            for mode in ('anchor', 'window.open', 'history', 'navigation', 'popup', 'trusted'):
                page = await context.new_page()
                baseline_pages = set(context.pages)
                await page.set_content(f'''<style>.card{{display:block;position:relative;width:200px;height:250px}}img{{width:200px;height:250px}}span{{position:absolute;bottom:20px;left:10px}}</style><a class="card" href="{URL}"><img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='250'%3E%3C/svg%3E"><span>100 mil</span></a>''')
                await page.evaluate('() => {window.nativeOpen = window.open.bind(window);}')
                await page.add_script_tag(path=str(ROOT / 'app/browser_engine.js'))
                rows = await page.evaluate('window.__KPA_DOM.collect()')
                assert len(rows) == 1
                await page.evaluate('''({mode,url}) => {
                    document.querySelector('a').onclick = e => {
                        if(mode==='anchor') return;
                        e.preventDefault();
                        if(mode==='window.open') window.open(url);
                        if(mode==='history') history.pushState({},'',url);
                        if(mode==='navigation') location.href=url;
                        if(mode==='popup') window.nativeOpen(url);
                        if(mode==='trusted' && e.isTrusted) {window.wasTrusted=true;window.open(url);}
                    };
                }''', {'mode': mode, 'url': URL})
                # Remove the anchor href for non-anchor modes to allow the click handler to run.
                # Keep stable identity through an explicit photo ID.
                if mode != 'anchor':
                    await page.evaluate("document.querySelector('a').removeAttribute('href');document.querySelector('a').setAttribute('data-photo-id','1234567890123456789')")
                    rows = await page.evaluate('window.__KPA_DOM.collect()')
                found = await service._capture_card_link(page, {'key': rows[0]['key'], 'realClick': mode=='trusted'})
                assert found and found['url'] == URL, (mode, found)
                if mode=='trusted':
                    assert await page.evaluate('window.wasTrusted') is True
                assert set(context.pages) == baseline_pages, (mode, [p.url for p in context.pages])
                await page.close()
                print(f'Link OK: {mode}')
            await browser.close()


asyncio.run(main())
