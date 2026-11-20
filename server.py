"""BAM.Connect 360 trial: server-side CarHub proxy. No API secret in browser code."""
from __future__ import annotations
import asyncio
import ipaddress
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
MAX_FRAMES = 152
# Saved BAM showroom asset (not a secret). Override on Render for future studio variants.
BACKGROUND_ASSET_ID = os.getenv("CARHUB_BACKGROUND_ASSET_ID", "01a1f8d3-6dcd-7ba9-ad18-6ece95c24d53").strip()
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
    return sorted(out, key=lambda f: float(f[0] or 0))


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
    return FileResponse(ROOT / "index.html", media_type="text/html", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(ROOT / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def sw():
    return FileResponse(ROOT / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/icon.svg")
async def icon():
    return FileResponse(ROOT / "icon.svg", media_type="image/svg+xml")


@app.get("/api/health")
async def health():
    # Never return the key, only a coarse status.
    prefix = "test" if KEY.startswith("chk_test_") else "staging" if KEY.startswith("chk_stg_") else "live" if KEY.startswith("chk_live_") else "unknown"
    return {"ready": configured(), "environment": prefix if KEY else "unconfigured", "remaining_local_jobs": max(0, MAX_JOBS - jobs_started), "background_configured": bool(BACKGROUND_ASSET_ID), "background_asset_id": BACKGROUND_ASSET_ID, "default_quality": 1080, "default_frames": 152, "video_fps": 30, "request_modes": ["canonical", "studio", "legacy"], "accepts_older_client": True, "version": "role-v7", "capture_roles": ["front", "left", "rear", "right"]}


@app.post("/api/check-upload")
async def check_upload(request: Request, images: list[UploadFile] = File(...)):
    """No-cost diagnostic: validate PWA -> BAM proxy upload, NEVER call CarHub."""
    auth(request)
    if len(images) != 4:
        raise HTTPException(422, f"BAM received {len(images)} photographs; exactly four required")
    from PIL import Image
    positions = ("front", "left", "rear", "right")
    received = []
    for index, img in enumerate(images):
        contents = await img.read(MAX_IMAGE_BYTES + 1)
        if not contents or len(contents) > MAX_IMAGE_BYTES:
            raise HTTPException(413, f"Photo {index + 1}: empty or larger than 20 MiB")
        if img.content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(415, f"Photo {index + 1}: unsupported image type")
        try:
            with Image.open(io.BytesIO(contents)) as im:
                im.verify()
        except Exception:
            raise HTTPException(422, f"Photo {index + 1}: unable to decode image")
        received.append({"position": positions[index], "bytes": len(contents), "mime": img.content_type})
    return {"ok": True, "count": len(received), "received": received, "outbound_roles": ["front", "left", "rear", "right"],
            "notice": "BAM received four valid images. CarHub was NOT contacted and no generation was charged."}


@app.post("/api/generate")
async def generate(request: Request, images: list[UploadFile] = File(...), frame_count: int = Form(152), quality: int = Form(1080), use_background: bool = Form(True), request_format: str = Form("studio")):
    global jobs_started
    auth(request)
    if not KEY:
        raise HTTPException(503, "Set CARHUB_API_KEY on the server")
    if jobs_started >= MAX_JOBS:
        raise HTTPException(429, "Trial generation cap reached. Server owner must increase BAM_MAX_GENERATIONS.")
    if len(images) != 4:
        raise HTTPException(422, "Exactly four images are required (front, left, rear, right)")
    if frame_count not in (24, 36, 72, 96, 152):
        raise HTTPException(422, "frame_count must be 24, 36, 72, 96 or 152")
    if quality not in (480, 768, 1080):
        raise HTTPException(422, "quality must be 480, 768 or 1080")
    # CarHub's canonical API v2 contract (API conventions, 20 November 2026):
    # `inputs` (one JSON ARRAY) maps four named multipart photo parts through
    # each object's `file` property, and `params` is one JSON OBJECT. Both
    # fields are included once with Content-Type: application/json.
    # v4 diagnostic browser sessions may still send `legacy` or `studio` even
    # after the server has deployed v5/v6. Never reject these *client-side*
    # switches: the provider payload is always the documented canonical form.
    # Preserve the client's own frame-count, quality and background selections;
    # a legacy baseline should remain 36 frames with background disabled.
    request_format = (request_format or "canonical").strip().lower()
    if request_format not in ("canonical", "studio", "legacy"):
        raise HTTPException(422, "Unsupported trial request format. Refresh the browser to update the PWA.")
    if use_background and not BACKGROUND_ASSET_ID:
        raise HTTPException(503, "CARHUB_BACKGROUND_ASSET_ID is not configured")
    input_roles = ("front", "left", "rear", "right")
    inputs = []
    photo_parts = []
    for i, img in enumerate(images):
        data = await img.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Each photo must be non-empty and no larger than 20 MiB")
        mime = img.content_type
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(415, "Use JPEG, PNG, or WebP photos")
        part_name = f"photo_{i + 1}"
        extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
        # The provider uses `role` to map each image to a required 360 view.
        # `client_id` alone is insufficient and caused HTTP 400:
        # "must identify front, left, rear, or right".
        inputs.append({"file": part_name, "role": input_roles[i], "client_id": f"bam_{input_roles[i]}"})
        photo_parts.append((part_name, (f"{part_name}.{extension}", data, mime)))
    params = {"quality": quality, "frame_count": frame_count, "output_format": "jpg", "viewer": False}
    if use_background:
        params["background"] = {"asset_id": BACKGROUND_ASSET_ID, "image_ratio": "16:9"}
    files = [
        ("inputs", (None, json.dumps(inputs, separators=(",", ":")), "application/json")),
        ("params", (None, json.dumps(params, separators=(",", ":")), "application/json")),
        *photo_parts,
    ]
    request_info = {
        "format": "role-v7", "received_client_mode": request_format, "photo_count": len(images),
        "multipart_file_fields": [part[0] for part in photo_parts],
        "metadata_fields": ["inputs", "params"],
    }
    raw_id = request.headers.get("X-BAM-Request-ID", "")
    try:
        idem = str(uuid.UUID(raw_id)) if raw_id else str(uuid.uuid4())
    except ValueError:
        raise HTTPException(400, "Invalid idempotency request ID")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
            response = await client.post(
                f"{API_URL}/v1/render/360",
                headers=carhub_headers(idem),
                files=files,
            )
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach CarHub. Check connectivity and API hostname.")
    if not response.is_success:
        return JSONResponse({"error": public_error(response), "provider_status": response.status_code,
                             "request_info": request_info},
                            status_code=400 if response.status_code in (400, 403, 404, 422) else 502)
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
    return {"job_id": job_id, "status": job.get("status", "queued"), "provider_status": response.status_code, "submitted_params": params, "request_info": request_info}


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
    """Download signed CarHub frames in bounded parallel batches to temporary files."""
    job = unwrap_job(await provider_job(job_id))
    if str(job.get("status", "")).lower() != "succeeded":
        raise HTTPException(409, "CarHub job has not succeeded")
    frames = get_frames(job)
    if not frames:
        raise HTTPException(404, "No downloadable frames found in CarHub result")
    working = tempfile.TemporaryDirectory(prefix="bam360_frames_")
    directory = Path(working.name)
    paths = [directory / f"frame_{idx:03d}.jpg" for idx in range(len(frames))]
    gate = asyncio.Semaphore(6)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
            async def fetch_one(index: int, url: str):
                parsed = urlparse(url)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise HTTPException(502, "Invalid CarHub frame URL")
                hostname = parsed.hostname or ""
                if hostname.lower() in ("localhost",) or hostname.lower().endswith(".local"):
                    raise HTTPException(502, "Unsafe CarHub frame URL")
                try:
                    ip = ipaddress.ip_address(hostname)
                except ValueError:
                    pass
                else:
                    if not ip.is_global:
                        raise HTTPException(502, "Unsafe CarHub frame URL")
                async with gate:
                    try:
                        response = await client.get(url)
                        response.raise_for_status()
                    except httpx.HTTPError:
                        raise HTTPException(502, "A signed CarHub frame URL is unavailable or expired")
                    if len(response.content) > 15 * 1024 * 1024:
                        raise HTTPException(502, "CarHub frame exceeds download limit")
                    paths[index].write_bytes(response.content)
            await asyncio.gather(*(fetch_one(i, url) for i, (_, url) in enumerate(frames)))
        return frames, working, paths
    except Exception:
        working.cleanup()
        raise


def cleanup_outputs(path, directory):
    Path(path).unlink(missing_ok=True)
    directory.cleanup()


@app.get("/api/jobs/{job_id}/frames.zip")
async def frames_zip(job_id: str, request: Request):
    check_job(request, job_id)
    frames, working, paths = await download_frames(job_id)
    fd, output_path = tempfile.mkstemp(prefix="bam360_export_", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for i, (frame, path) in enumerate(zip(frames, paths)):
                archive.write(path, arcname=f"BAM_360_{i:03d}_{round(float(frame[0] or 0)):03d}deg.jpg")
    except Exception:
        cleanup_outputs(output_path, working)
        raise HTTPException(500, "Could not package generated frames")
    return FileResponse(output_path, media_type="application/zip", filename=f"BAM_360_{job_id}.zip", background=BackgroundTask(lambda: cleanup_outputs(output_path, working)))


def make_mp4(paths: list[Path], output_path: str):
    """Encode one 360-degree revolution at 1920×1080 / 30 fps."""
    from PIL import Image, ImageOps
    import imageio_ffmpeg
    width, height = 1920, 1080
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-vcodec", "rawvideo", "-s", f"{width}x{height}", "-pix_fmt", "rgb24", "-r", "30", "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-threads", "2", "-pix_fmt", "yuv420p", "-movflags", "+faststart", output_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for source_path in paths:
            with Image.open(source_path) as source:
                image = ImageOps.contain(source.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", (width, height), "#161616")
                canvas.paste(image, ((width-image.width)//2, (height-image.height)//2))
                try:
                    proc.stdin.write(canvas.tobytes())
                except BrokenPipeError:
                    break
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        error = proc.stderr.read()
        proc.wait(timeout=180)
        if proc.returncode:
            raise RuntimeError(f"FFmpeg returned {proc.returncode}: {error[-250:].decode(errors='ignore')}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@app.get("/api/jobs/{job_id}/video.mp4")
async def video_mp4(job_id: str, request: Request):
    check_job(request, job_id)
    _, working, paths = await download_frames(job_id)
    fd, output_path = tempfile.mkstemp(suffix=".mp4", prefix="bam360_export_")
    os.close(fd)
    try:
        await asyncio.to_thread(make_mp4, paths, output_path)
    except Exception:
        cleanup_outputs(output_path, working)
        raise HTTPException(500, "Could not encode 1080p MP4 on the trial host; check Render logs and available memory")
    return FileResponse(output_path, media_type="video/mp4", filename=f"BAM_360_{job_id}_1080p_30fps.mp4", background=BackgroundTask(lambda: cleanup_outputs(output_path, working)))
