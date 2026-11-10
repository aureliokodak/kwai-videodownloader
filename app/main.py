from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .kwai import KwaiAnalyzerService, KwaiError, normalize_profile_url

from .browser_video import BrowserVideoService
from .instagram import InstagramAnalyzerService
from .tiktok import TikTokAnalyzerService
from .youtube import YouTubeAnalyzerService

BASE_DIR = Path(__file__).resolve().parents[1]
app = FastAPI(title="Kwai Analyzer Local", version="2.3.2")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
service = KwaiAnalyzerService(BASE_DIR)
service.load_latest()
youtube_service = YouTubeAnalyzerService(BASE_DIR)
youtube_service.load_latest()
tiktok_service = TikTokAnalyzerService(BASE_DIR)
tiktok_service.load_latest()
instagram_service = InstagramAnalyzerService(BASE_DIR)
instagram_service.load_latest()


def active_service(platform: Literal["kwai", "youtube", "tiktok", "instagram"] = "kwai"):
    return {"youtube": youtube_service, "tiktok": tiktok_service, "instagram": instagram_service}.get(platform, service)


def export_name(service) -> str:
    if isinstance(service, BrowserVideoService):
        return service.SLUG
    return "youtube" if isinstance(service, YouTubeAnalyzerService) else "kwai"


@app.middleware("http")
async def fresh_local_state(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith('/api/') or request.url.path == '/' else "no-cache"
    return response


class BrowserRequest(BaseModel):
    profile_url: str | None = None


class AnalyzeRequest(BaseModel):
    profile_url: str
    limit: int = Field(default=100, ge=1, le=20_000)
    min_views: int = Field(default=0, ge=0)
    hashtag: str | None = None
    sort_by: str = Field(default="views", pattern="^(views|likes|comments)$")
    max_pages: int = Field(default=500, ge=1, le=2_000)
    top_n: int = Field(default=20, ge=1, le=1_000)
    auto_links: bool = True
    turbo: bool = True


class LinksRequest(BaseModel):
    top_n: int = Field(default=20, ge=1, le=1_000)


class DownloadRequest(BaseModel):
    keys: list[str]



@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Asset content changes invalidate browser caches without requiring a hard refresh.
    digest = hashlib.sha256()
    for name in ("app.css", "ranking.js", "app.js"):
        digest.update((BASE_DIR / "app/static" / name).read_bytes())
    return templates.TemplateResponse(request=request, name="index.html", context={"asset_version": digest.hexdigest()[:16]})


@app.get("/api/status")
async def status(service=Depends(active_service)):
    return {**service.status, "results_count": len(service.results), "last_profile_url": service.last_profile_url}


@app.get("/api/results")
async def results(service=Depends(active_service)):
    return {"items": service.public_results(), "count": len(service.results)}


@app.post("/api/browser/start")
async def browser_start(payload: BrowserRequest, service=Depends(active_service)):
    try:
        if service.status.get("running"):
            raise KwaiError("Aguarde ou interrompa a coleta antes de abrir outro perfil.")
        if isinstance(service, YouTubeAnalyzerService):
            raise KwaiError("A coleta do YouTube não precisa abrir o navegador. Use Iniciar coleta.")
        if isinstance(service, BrowserVideoService):
            return await service.open_window(service.normalize_url(payload.profile_url) if payload.profile_url else None)
        url = normalize_profile_url(payload.profile_url) if payload.profile_url else None
        return await service.ensure_browser(url)
    except KwaiError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/analyze")
async def analyze(payload: AnalyzeRequest, service=Depends(active_service)):
    try:
        await service.start_analysis(
            profile_url=payload.profile_url,
            limit=payload.limit,
            min_views=payload.min_views,
            hashtag=payload.hashtag,
            sort_by=payload.sort_by,
            max_pages=payload.max_pages,
            top_n=payload.top_n,
            auto_links=payload.auto_links,
            turbo=payload.turbo,
        )
        return {"ok": True}
    except KwaiError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/links")
async def links(payload: LinksRequest, service=Depends(active_service)):
    try:
        await service.resolve_links(payload.top_n)
        return {"ok": True}
    except KwaiError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/cancel")
async def cancel(service=Depends(active_service)):
    return await service.cancel()


@app.post("/api/durations")
async def durations(service=Depends(active_service)):
    try:
        await service.update_durations()
        return {"ok": True}
    except KwaiError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/export/csv")
async def export_csv(service=Depends(active_service)):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["rank", "key", "photo_id", "views", "likes", "comments", "title", "hashtags", "permalink", "thumbnail", "media_url", "source", "link_source", "api_page"])
    for rank, item in enumerate(service.results, 1):
        writer.writerow([
            rank, item.key, item.photo_id or "", item.views,
            item.likes if item.likes is not None else "", item.comments if item.comments is not None else "",
            ("'" + item.title if item.title.lstrip().startswith(("=", "+", "-", "@")) else item.title), " ".join(item.hashtags), item.permalink or "", item.thumbnail or "", item.media_url or "",
            item.source, item.link_source or "", item.page if item.page is not None else "",
        ])
    data = "\ufeff" + output.getvalue()
    return Response(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={export_name(service)}-ranking.csv"})


@app.get("/api/export/json")
async def export_json(service=Depends(active_service)):
    data = json.dumps({"profile_url": service.last_profile_url, "status": service.status, "items": service.public_results()}, ensure_ascii=False, indent=2)
    return Response(data, media_type="application/json; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={export_name(service)}-ranking.json"})


@app.post("/api/download")
async def download(payload: DownloadRequest, service=Depends(active_service)):
    try:
        await service.start_download(payload.keys)
        return {"ok": True}
    except KwaiError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/download/cancel")
async def cancel_download(service=Depends(active_service)):
    if isinstance(service, (YouTubeAnalyzerService, BrowserVideoService)):
        return await service.cancel_download()
    raise HTTPException(400, "Interrupção de downloads disponível para YouTube, TikTok e Instagram.")


@app.get("/api/download/status")
async def download_status(service=Depends(active_service)):
    return service.download_status


@app.get("/api/diagnostic")
async def diagnostic(service=Depends(active_service)):
    return JSONResponse(service.diagnostic(), headers={"Content-Disposition": "attachment; filename=kwai-webapp-diagnostico-v2.json"})


@app.on_event("shutdown")
async def shutdown_event():
    await service.close()
    await youtube_service.close()
    await tiktok_service.close()
    await instagram_service.close()
