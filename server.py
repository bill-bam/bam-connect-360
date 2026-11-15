"""BAM.Connect 360 trial: server-side CarHub proxy. No API secret in browser code."""
from __future__ import annotations
import asyncio
import io
import json
import os
import secrets
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

ROOT = Path(__file__).resolve().parent
API_URL = os.getenv("CARHUB_API_URL", "https://api.trycarhub.com").rstrip("/")
KEY = os.getenv("CARHUB_API_KEY", "").strip()
PIN = os.getenv("BAM_TRIAL_PIN", "").strip()
MAX_JOBS = max(1, int(os.getenv("BAM_MAX_GENERATIONS", "5")))
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_FRAMES = 72
jobs_started = 0
# Trial only: in-memory authorisation. Server restarts discard authorised job IDs.
known_jobs: dict[str, float] = {}
app = FastAPI(title="BAM.Connect 360 Trial", docs_url=None, redoc_url=None)


def auth(request: Request):
    if not PIN:
        raise HTTPException(503, "Server trial PIN has not been configured")
    supplied = request.headers.get("X-BAM-Trial-PIN", "")
    if not secrets.compare_digest(supplied, PIN):
        raise HTTPException(401, "Incorrect trial access code")


def configured():
    return bool(KEY) and bool(PIN)


def carhub_headers(request_id: str | None = None):
    headers = {"Authorization": f"Bearer {KEY}"}
    if request_id:
        headers["Idempotency-Key"] = request_id
    return headers


def unwrap_job(obj):
    if isinstance(obj, dict):
        return obj.get("job") if isinstance(obj.get("job"), dict) else obj
    return {}


def get_frames(job: dict):
    obj = unwrap_job(job)
    result = obj.get("result")
    if not isinstance(result, dict):
        return []
    frames = result.get("frames")
    if not isinstance(frames, list):
        return []
    out = []
    for item in frames[:MAX_FRAMES]:
        if not isinstance(item, dict):
            continue
        img = item.get("image", {})
        url = img.get("url") if isinstance(img, dict) else None
        if isinstance(url, str) and url.startswith("https://"):
            out.append((item.get("angle", 0), url))
    return out


def public_error(response: httpx.Response):
    try:
        err = response.json()
        if isinstance(err, dict):
            e = err.get("error")
            if isinstance(e, dict):
                return e.get("message") or e.get("code") or f"CarHub error {response.status_code}"
            return err.get("message") or err.get("detail") or f"CarHub error {response.status_code}"
    except ValueError:
        pass
    return f"CarHub returned HTTP {response.status_code}"


@app.get("/")
async def homepage():
    return FileResponse(ROOT / "index.html", media_type="text/html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(ROOT / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def sw():
    return FileResponse(ROOT / "sw.js", media_type="application/javascript")


@app.get("/icon.svg")
async def icon():
    return FileResponse(ROOT / "icon.svg", media_type="image/svg+xml")


@app.get("/api/health")
async def health():
    # Never return the key, only a coarse status.
    prefix = "test" if KEY.startswith("chk_test_") else "staging" if KEY.startswith("chk_stg_") else "live" if KEY.startswith("chk_live_") else "unknown"
    return {"ready": configured(), "environment": prefix if KEY else "unconfigured", "remaining_local_jobs": max(0, MAX_JOBS - jobs_started)}


@app.post("/api/generate")
async def generate(request: Request, images: list[UploadFile] = File(...), frame_count: int = Form(36)):
    global jobs_started
    auth(request)
    if not KEY:
        raise HTTPException(503, "Set CARHUB_API_KEY on the server")
    if jobs_started >= MAX_JOBS:
        raise HTTPException(429, "Trial generation cap reached. Server owner must increase BAM_MAX_GENERATIONS.")
    if len(images) != 4:
        raise HTTPException(422, "Exactly four images are required (front, left, rear, right)")
    if frame_count not in (24, 36, 72):
        raise HTTPException(422, "frame_count must be 24, 36 or 72")
    files = []
    for i, img in enumerate(images):
        data = await img.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Each photo must be non-empty and no larger than 20 MiB")
        mime = img.content_type
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(415, "Use JPEG, PNG, or WebP photos")
        files.append(("images", (f"{['front','left','rear','right'][i]}.{ {'image/jpeg':'jpg','image/png':'png','image/webp':'webp'}[mime] }", data, mime)))
    # API documentation confirms repeated `images` parts and optional frame settings.
    for name, value in [("frame_count", str(frame_count)), ("output_format", "jpg"), ("viewer", "false")]:
        files.append((name, (None, value)))
    raw_id = request.headers.get("X-BAM-Request-ID", "")
    try:
        idem = str(uuid.UUID(raw_id)) if raw_id else str(uuid.uuid4())
    except ValueError:
        raise HTTPException(400, "Invalid idempotency request ID")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
            response = await client.post(f"{API_URL}/v1/render/360", headers=carhub_headers(idem), files=files)
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach CarHub. Check connectivity and API hostname.")
    if not response.is_success:
        return JSONResponse({"error": public_error(response), "provider_status": response.status_code}, status_code=400 if response.status_code in (400, 403, 404, 422) else 502)
    try:
        body = response.json()
    except ValueError:
        raise HTTPException(502, "CarHub returned an unreadable response")
    job = unwrap_job(body)
    job_id = job.get("id") or job.get("job_id")
    if not isinstance(job_id, str):
        raise HTTPException(502, "CarHub accepted the request but returned no job ID. Check raw API response in provider dashboard.")
    jobs_started += 1
    known_jobs[job_id] = time.time()
    return {"job_id": job_id, "status": job.get("status", "queued"), "provider_status": response.status_code}


async def provider_job(job_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
            response = await client.get(f"{API_URL}/v1/jobs/{job_id}", headers=carhub_headers())
    except httpx.HTTPError:
        raise HTTPException(502, "Could not retrieve CarHub job")
    if not response.is_success:
        raise HTTPException(502, public_error(response))
    try:
        return response.json()
    except ValueError:
        raise HTTPException(502, "CarHub returned unreadable job data")


def check_job(request: Request, job_id: str):
    auth(request)
    if job_id not in known_jobs:
        raise HTTPException(404, "Job not found in this trial session (server restart clears saved IDs)")


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, request: Request):
    check_job(request, job_id)
    body = await provider_job(job_id)
    # Keep full provider job JSON so the UI can use real result/frames fields.
    return JSONResponse(body)


async def download_frames(job_id: str):
    job = unwrap_job(await provider_job(job_id))
    if job.get("status") != "succeeded":
        raise HTTPException(409, "CarHub job has not succeeded")
    frames = get_frames(job)
    if not frames:
        raise HTTPException(404, "No downloadable frames found in CarHub result")
    blobs = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
        for _, url in frames:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.hostname in ("localhost", "127.0.0.1"):
                raise HTTPException(502, "Invalid frame URL returned by provider")
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError:
                raise HTTPException(502, "A signed frame URL is unavailable or expired")
            if len(response.content) > 15 * 1024 * 1024:
                raise HTTPException(502, "Provider frame larger than trial download limit")
            blobs.append(response.content)
    return frames, blobs


@app.get("/api/jobs/{job_id}/frames.zip")
async def frames_zip(job_id: str, request: Request):
    check_job(request, job_id)
    frames, blobs = await download_frames(job_id)
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for idx, (frame, blob) in enumerate(zip(frames, blobs)):
            archive.writestr(f"BAM_360_{idx:02d}_{round(float(frame[0])):03d}deg.jpg", blob)
    return JSONResponse({"message": "Use video or per-frame image links to save files"}) if not output.getvalue() else __zip_response(output.getvalue(), job_id)


def __zip_response(data: bytes, job_id: str):
    from fastapi.responses import Response
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="BAM_360_{job_id}.zip"'})


def make_mp4(images: list[bytes], output_path: str):
    from PIL import Image, ImageOps
    import imageio_ffmpeg
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-vcodec", "rawvideo", "-s", "960x540", "-pix_fmt", "rgb24", "-r", "12", "-i", "pipe:0", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", output_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for raw in images * 2:
            with Image.open(io.BytesIO(raw)) as src:
                image = ImageOps.contain(src.convert("RGB"), (960, 540), method=Image.Resampling.LANCZOS)
                background = Image.new("RGB", (960, 540), "#f1f1f1")
                background.paste(image, ((960-image.width)//2, (540-image.height)//2))
                proc.stdin.write(background.tobytes())
        proc.stdin.close()
        error = proc.stderr.read()
        proc.wait(timeout=120)
        if proc.returncode:
            raise RuntimeError(f"Video encoder failed: {error[-220:].decode(errors='ignore')}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@app.get("/api/jobs/{job_id}/video.mp4")
async def video_mp4(job_id: str, request: Request):
    check_job(request, job_id)
    _, blobs = await download_frames(job_id)
    handle, path = tempfile.mkstemp(suffix=".mp4", prefix="bam360_")
    os.close(handle)
    try:
        await asyncio.to_thread(make_mp4, blobs, path)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise HTTPException(500, "Could not encode MP4 on this test server")
    return FileResponse(path, media_type="video/mp4", filename=f"BAM_360_{job_id}.mp4", background=BackgroundTask(lambda: Path(path).unlink(missing_ok=True)))
