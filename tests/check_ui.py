"""Browser smoke test with isolated sample data; never changes the saved ranking."""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        items = [{"key": f"id:{i}", "photo_id": str(1234567890123456789 + i),
                  "views": i * 1000, "likes": i if i % 2 else None, "comments": 0,
                  "duration_seconds": i if i % 2 else None,
                  "title": f"Vídeo {i} #novela", "hashtags": ["#novela"],
                  "source": "feed-api", "permalink": f"https://www.kwai.com/video/{1234567890123456789+i}"}
                 for i in range(1, 206)]

        async def route(request):
            path = request.request.url.split("http://localhost")[1].split('?')[0]
            if path == "/":
                await request.fulfill(body=(ROOT / "app/templates/index.html").read_text(encoding="utf-8"), content_type="text/html")
            elif path.startswith("/static/"):
                file = ROOT / "app" / path.lstrip("/")
                await request.fulfill(body=file.read_text(encoding="utf-8"), content_type="text/css" if path.endswith(".css") else "application/javascript")
            elif path == "/api/results":
                await request.fulfill(json={"items": [{**items[0], "key": "youtube:abcdefgh000", "source": "youtube"}] if "platform=youtube" in request.request.url else items})
            elif path == "/api/status":
                await request.fulfill(json={"running": False, "results_count": (1 if "platform=youtube" in request.request.url else len(items)), "message": "Ranking de teste", "last_profile_url": "https://www.kwai.com/@teste"})
            else:
                await request.fulfill(json={"running": False, "items": []})

        await page.route("http://localhost/**", route)
        await page.goto("http://localhost/")
        await page.wait_for_function("document.querySelectorAll('#resultsBody tr').length === 100")
        assert await page.locator("#pageMeta").inner_text() == "Página 1 de 3"
        await page.locator("#nextPage").click()
        assert await page.locator("#resultsBody tr td:nth-child(2)").first.inner_text() == "101"
        await page.locator('[name="views_min"]').fill("200000")
        assert await page.locator("#resultsBody tr").count() == 6
        await page.locator("#selectAll").click()
        assert "6 selecionados" in await page.locator("#selectionCount").inner_text()
        await page.locator('[name="views_max"]').fill("201000")
        assert "4 fora dos filtros" in await page.locator("#selectionCount").inner_text()
        async with page.expect_download() as download:
            await page.locator("#exportJson").click()
        path = await (await download.value).path()
        exported = json.loads(Path(path).read_text(encoding="utf-8"))
        assert len(exported["items"]) == 2
        await page.locator("#search").fill("inexistente")
        assert "Nenhum vídeo atende" in await page.locator("#resultsBody").inner_text()
        await page.locator("#resetFilters").click()
        assert await page.locator("#resultsBody tr").count() == 100
        await page.locator('[name="duration_seconds_min"]').fill('60')
        await page.locator('[name="duration_seconds_max"]').fill('65')
        assert await page.locator("#resultsBody tr").count() == 3
        assert await page.locator("#resultsBody tr td:nth-child(7)").first.inner_text() == '1:05'
        await page.locator('#clearSelection').click()
        await page.locator('#selectAll').click()
        assert '3 selecionados' in await page.locator('#selectionCount').inner_text()
        async with page.expect_download() as download:
            await page.locator('#exportJson').click()
        exported = json.loads(Path(await (await download.value).path()).read_text(encoding='utf-8'))
        assert [row['duration_seconds'] for row in exported['items']] == [65, 63, 61]
        await page.locator('[name="views_min"]').fill('64000')
        assert await page.locator("#resultsBody tr").count() == 1
        async with page.expect_request('**/api/durations') as duration_request:
            await page.locator('#updateDurations').click()
        assert (await duration_request.value).method == 'POST'
        assert await page.locator('[name="duration_seconds_min"]').input_value() == '60'
        assert await page.locator("#resultsBody tr").count() == 1
        await page.locator('#resetFilters').click()
        assert await page.locator('[name="duration_seconds_min"]').input_value() == ''
        assert await page.locator("#resultsBody tr").count() == 100
        await page.locator('#minViews').select_option('250000')
        await page.locator('#hashtag').fill('#novela')
        await page.locator('#sortBy').select_option('likes')
        async with page.expect_request('**/api/analyze') as analysis:
            await page.locator('#analyze').click()
        payload = (await analysis.value).post_data_json
        assert payload['min_views'] == 250000
        assert payload['hashtag'] == '#novela'
        assert payload['sort_by'] == 'likes'
        await page.screenshot(path=str(ROOT / "tests/ranking-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(ROOT / "tests/ranking-mobile.png"), full_page=True)
        await page.locator('#platform').select_option('youtube')
        await page.wait_for_function("document.querySelector('#resultMeta').textContent.includes('1 vídeos')")
        assert await page.locator('#openBrowser').is_hidden()
        assert await page.locator('#kwaiOptions').is_hidden()
        assert await page.locator('#platformHint').is_visible()
        assert '0 selecionados' in await page.locator('#selectionCount').inner_text()
        await page.locator('#selectAll').click()
        async with page.expect_request('**/api/download?platform=youtube') as youtube_download:
            await page.locator('#downloadSelected').click()
        assert (await youtube_download.value).post_data_json['keys'] == ['youtube:abcdefgh000']
        await page.screenshot(path=str(ROOT / 'tests/youtube-mobile.png'), full_page=True)
        await page.set_viewport_size({'width': 1440, 'height': 1000})
        await page.screenshot(path=str(ROOT / 'tests/youtube-desktop.png'), full_page=True)
        await page.locator('#platform').select_option('kwai')
        await page.wait_for_function("document.querySelectorAll('#resultsBody tr').length === 100")
        assert await page.locator('#openBrowser').is_visible()
        assert '0 selecionados' in await page.locator('#selectionCount').inner_text()
        assert not errors, errors
        await page.set_viewport_size({"width": 1000, "height": 800})
        await page.set_content('''<style>body{margin:0;overflow:hidden}#feed{height:400px;overflow:auto;width:300px}.card{position:relative;width:200px;height:250px}.card img{width:200px;height:250px}.metric{position:absolute;bottom:20px;left:10px}</style><div id="feed"></div>''')
        await page.add_script_tag(path=str(ROOT / "app/browser_engine.js"))
        initial = await page.evaluate("window.__KPA_DOM.chooseScrollTarget()")
        assert initial["max"] == 0
        await page.evaluate('''() => {document.querySelector('#feed').innerHTML=Array.from({length:20},(_,i)=>`<a class="card" style="display:block" href="https://www.kwai.com/video/${1234567890123456789n+BigInt(i)}"><img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='250'%3E%3C/svg%3E"><span class="metric">${i+1} mil</span></a>`).join('');}''')
        target = await page.evaluate("window.__KPA_DOM.chooseScrollTarget()")
        assert target["items"] == 20, target
        matches = await page.evaluate("() => window.__KPA_DOM.findTargets(window.__KPA_DOM.collect().map(x=>({key:x.key})))")
        assert len(matches) == 20 and all(row['ok'] and row['url'] for row in matches)
        matches = await page.evaluate("() => window.__KPA_DOM.findTargets([{key:'unknown',views:1000,title:'same title'}])")
        assert matches[0]['ok'] is False
        assert not target["isDoc"] and target["max"] > 0, target
        scrolled = await page.evaluate("window.__KPA_DOM.scrollBottom()")
        assert 0 < scrolled["top"] < scrolled["max"], scrolled
        await page.evaluate("() => {document.querySelector('#feed').insertAdjacentHTML('beforeend','<p>No more videos</p>');window.__KPA_DOM.scrollTo(999999);}")
        state=await page.evaluate("window.__KPA_DOM.endState()")
        assert state['atBottom'] and state['marker']=='No more videos' and not state['loading'], state
        await page.evaluate("() => {document.querySelector('#feed').insertAdjacentHTML('beforeend','<div role=\"progressbar\">Carregando</div>');window.__KPA_DOM.scrollTo(999999);}")
        assert (await page.evaluate("window.__KPA_DOM.endState()"))['loading']
        await page.set_content('''<style>.row{display:flex}.card{position:relative;width:200px;height:250px}.card img{width:200px;height:250px}.metric{position:absolute;bottom:20px;left:10px}</style><div class="row" data-video-id="9999999999999999999"><div class="card"><a href="https://www.kwai.com/video/1234567890123456789"><img src="http://localhost/cover-one.jpg"></a><span class="metric">100 mil</span></div><div class="card"><img src="http://localhost/cover-two.jpg"><span class="metric">200 mil</span></div></div>''')
        cards=await page.evaluate('window.__KPA_DOM.collect()')
        assert len(cards)==2, cards
        by_views={row['views']:row for row in cards}
        assert by_views[100000]['url']=='https://www.kwai.com/video/1234567890123456789'
        assert by_views[200000]['url'] is None, 'Sibling link leaked into unlinked card'
        assert all(row['id'] is None for row in cards), 'Grid identity leaked into card'
        await page.evaluate("document.querySelector('.card').insertAdjacentHTML('beforeend', '<span class=\"duration\">1:05</span>')")
        durations = await page.evaluate('window.__KPA_DOM.collect()')
        by_duration_views = {row['views']: row['duration_seconds'] for row in durations}
        assert by_duration_views == {100000: 65, 200000: None}, by_duration_views
        wrong=await page.evaluate("t=>window.__KPA_DOM.findTargets([t])", {'key':by_views[100000]['key'],'thumbnailFingerprint':by_views[200000]['thumbnailFingerprint']})
        assert not wrong[0]['ok'], 'Key must not override conflicting thumbnail'
        await browser.close()
        print("UI OK: pagination, combined filters, selection, JSON export, empty state, reset, mobile layout, dynamic nested scroll target, incremental scrolling; no JavaScript errors.")


asyncio.run(main())
