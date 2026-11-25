"""BAM.Connect 360 trial: server-side CarHub proxy. No API secret in browser code.

Preserves the validated CarHub multipart request; restores the BAM studio asset and 1080/152 output.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
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
from urllib.parse import urlparse, quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

ROOT = Path(__file__).resolve().parent
API_URL = os.getenv("CARHUB_API_URL", "https://api.trycarhub.com").rstrip("/")
KEY = os.getenv("CARHUB_API_KEY", "").strip()
PIN = os.getenv("BAM_TRIAL_PIN", "").strip()
MAX_JOBS = max(1, int(os.getenv("BAM_MAX_GENERATIONS", "5")))
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_FRAMES = 152
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


@app.get("/360")
@app.get("/viewer")
async def interactive_vehicle_demo():
    """Public example of the current BAM 360 presentation; no API credentials are exposed."""
    return FileResponse(ROOT / "viewer.html", media_type="text/html", headers={"Cache-Control": "public, max-age=600"})


# Share links are stateless, signed references to successful CarHub jobs.
# They remain valid across server restarts provided API key/PIN (or BAM_SHARE_SECRET)
# remain unchanged, but depend on CarHub continuing to retain the frame assets.
# Do not expose the generation PIN or secret in buyer links.
SHARE_TTL_SECONDS = max(3600, min(30 * 86400, int(os.getenv("BAM_SHARE_LINK_DAYS", "7")) * 86400))


def _share_secret() -> bytes:
    dedicated = os.getenv("BAM_SHARE_SECRET", "").strip()
    if dedicated:
        return dedicated.encode("utf-8")
    # Existing deployment works without further setup. If either credential
    # changes, previously signed links will cease to validate.
    return hashlib.sha256(("BAM360-share-v1\0" + KEY + "\0" + PIN).encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def clean_vehicle_details(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    limits = {"make": 80, "model": 120, "registration": 24, "vin": 40, "bam_reference": 80}
    result = {}
    for key, limit in limits.items():
        raw = value.get(key)
        if not isinstance(raw, str):
            continue
        text = " ".join(raw.split())[:limit]
        if text:
            result[key] = text.upper() if key in ("registration", "vin") else text
    return result


def make_share_token(job_id: str, exp: int, vehicle_details=None) -> str:
    data = {"v": 1, "j": job_id, "exp": exp}
    metadata = clean_vehicle_details(vehicle_details)
    if metadata:
        data["vehicle"] = metadata
    payload = _b64(json.dumps(data, separators=(",", ":")).encode())
    digest = hmac.new(_share_secret(), payload.encode("ascii"), hashlib.sha256).digest()
    return payload + "." + _b64(digest)


def share_token_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 2 or len(parts[0]) > 4096 or len(parts[1]) > 128:
            raise ValueError("Invalid token")
        expected = _b64(hmac.new(_share_secret(), parts[0].encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(parts[1], expected):
            raise ValueError("Bad signature")
        data = json.loads(_unb64(parts[0]))
        if data.get("v") != 1 or not isinstance(data.get("j"), str) or not data["j"] or len(data["j"]) > 200:
            raise ValueError("Bad payload")
        if not isinstance(data.get("exp"), int) or time.time() >= data["exp"]:
            raise HTTPException(410, "This buyer link has expired. Ask the sender for a new link.")
        return data
    except HTTPException:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(404, "Buyer link not found")


def verify_share_token(token: str) -> str:
    return share_token_payload(token)["j"]


def share_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow, noarchive", "Referrer-Policy": "no-referrer"}


@app.get("/share/{token}")
async def buyer_view(token: str):
    verify_share_token(token)
    return FileResponse(ROOT / "buyer.html", media_type="text/html", headers=share_headers())


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
    # CarHub 360 official contract (24 Nov 2026):
    # multipart/form-data with exactly one JSON `inputs` ARRAY, exactly one
    # JSON `params` OBJECT, and four distinct named image file parts.
    # Each manifest item must reference its file part and state its `role`.
    # The roles MUST be front, left, rear, right in this order.
    # https://docs.trycarhub.com/guides/spin-360
    # https://docs.trycarhub.com/guides/uploads
    roles = ("front", "left", "rear", "right")
    manifest = []
    image_parts = []
    for index, img in enumerate(images):
        data = await img.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Each photo must be non-empty and no larger than 20 MiB")
        mime = img.content_type
        extensions = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        if mime not in extensions:
            raise HTTPException(415, "Use JPEG, PNG, or WebP photos")
        role = roles[index]
        manifest.append({"file": role, "role": role, "client_id": f"bam360-{role}"})
        image_parts.append((role, (f"{role}.{extensions[mime]}", data, mime)))

    # BAM.Connect 360: use the previously supplied CarHub studio background asset.
    # The existing PWA still submits `frame_count=72` in its form; for this trial,
    # request 152 provider frames explicitly without changing the working multipart
    # inputs/params structure or the browser capture form.
    params = {
        "viewer": False,
        "quality": 1080,
        "background": {
            "asset_id": "01a20b66-75a1-7e00-9aa6-63363169c0dd",
            "image_ratio": "16:9",
        },
        "frame_count": 152,
        "output_format": "jpg",
    }
    files = [
        ("inputs", (None, json.dumps(manifest, separators=(",", ":")), "application/json")),
        ("params", (None, json.dumps(params, separators=(",", ":")), "application/json")),
        *image_parts,
    ]
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


@app.post("/api/jobs/{job_id}/share")
async def create_buyer_link(job_id: str, request: Request):
    # A PIN-authorised operator can share an existing successful job as well,
    # even after a Render deployment has cleared in-memory known_jobs.
    auth(request)
    if not 6 <= len(job_id) <= 160 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in job_id):
        raise HTTPException(400, "Invalid CarHub job ID")
    job = unwrap_job(await provider_job(job_id))
    if str(job.get("status", "")).lower() != "succeeded" or not get_frames(job):
        raise HTTPException(409, "This job has not finished generating its 360 frames")
    expires_at = int(time.time()) + SHARE_TTL_SECONDS
    vehicle_details = {}
    if "application/json" in request.headers.get("content-type", ""):
        if int(request.headers.get("content-length", "0") or 0) > 8192:
            raise HTTPException(413, "Vehicle details are too large")
        try:
            raw = await request.body()
            if len(raw) > 8192:
                raise HTTPException(413, "Vehicle details are too large")
            body = json.loads(raw)
            vehicle_details = clean_vehicle_details(body.get("vehicle_details")) if isinstance(body, dict) else {}
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid vehicle details")
    token = make_share_token(job_id, expires_at, vehicle_details)
    base = (os.getenv("BAM_PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not base:
        # Use the incoming deployment domain when no canonical public URL is configured.
        base = str(request.base_url).rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise HTTPException(500, "Invalid BAM_PUBLIC_URL configured")
    url = f"{base}/share/{token}"
    return JSONResponse({"share_url": url, "download_url": f"{base}/api/share/{token}/video.mp4", "expires_at": expires_at, "temporary": True}, headers=share_headers())


@app.get("/api/share/{token}/frames")
async def buyer_frames(token: str):
    job_id = verify_share_token(token)
    try:
        job = unwrap_job(await provider_job(job_id))
    except HTTPException:
        raise HTTPException(502, "The 360 is temporarily unavailable from CarHub")
    if str(job.get("status", "")).lower() != "succeeded":
        raise HTTPException(409, "This 360 is not currently available")
    frames = get_frames(job)
    if not frames:
        raise HTTPException(410, "The generated images are no longer available. Ask for a new export.")
    # Expose only frame URLs and angles, not the upstream provider job metadata.
    return JSONResponse({"job_id": job_id, "vehicle_details": clean_vehicle_details(share_token_payload(token).get("vehicle")), "frame_count": len(frames), "frames": [{"angle": angle, "url": url} for angle, url in frames]}, headers=share_headers())


@app.get("/api/share/{token}/video.mp4")
async def buyer_video(token: str):
    job_id = verify_share_token(token)
    _, blobs = await download_frames(job_id)
    handle, path = tempfile.mkstemp(suffix=".mp4", prefix="bam360_shared_")
    os.close(handle)
    try:
        await asyncio.to_thread(make_mp4, blobs, path)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise HTTPException(500, "Could not prepare the video. Please try again later.")
    return FileResponse(path, media_type="video/mp4", filename=f"BAM_360_{job_id}.mp4",
                        headers=share_headers(), background=BackgroundTask(lambda: Path(path).unlink(missing_ok=True)))


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
    """Full-HD 30fps MP4 at near-master quality, sourced from the CarHub frame images.

    Each source angle is repeated at 30fps for a comfortable ~4.8s revolution when
    there are 72 images, repeated for two turns. Does not create new angles or detail.
    """
    from PIL import Image, ImageOps
    import imageio_ffmpeg
    width, height = 1920, 1080
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner','-loglevel','error','-y',
        '-f','rawvideo','-vcodec','rawvideo','-s',f'{width}x{height}',
        '-pix_fmt','rgb24','-r','30','-i','pipe:0', '-an',
        '-c:v','libx264','-preset','veryfast','-crf','18',
        '-pix_fmt','yuv420p','-threads','2','-movflags','+faststart', output_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # Keep duration near five seconds per revolution, for either 24/36/72 frames.
        # Duplicating source angles prevents frame blending or invented vehicle details.
        repeats = max(1, round(150 / len(images)))
        for _ in range(2):
            for raw in images:
                with Image.open(io.BytesIO(raw)) as src:
                    image = ImageOps.contain(src.convert('RGB'), (width, height), method=Image.Resampling.LANCZOS)
                    background = Image.new('RGB', (width, height), '#f1f1f1')
                    background.paste(image, ((width-image.width)//2,(height-image.height)//2))
                    frame = background.tobytes()
                for _ in range(repeats):
                    proc.stdin.write(frame)
        proc.stdin.close()
        error = proc.stderr.read()
        proc.wait(timeout=180)
        if proc.returncode:
            raise RuntimeError(f"Video encoding failed: {error[-220:].decode(errors='ignore')}")
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
