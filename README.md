# BAM.Connect 360 — CarHub real-API trial

A focused, mobile-first PWA for a real four-photo-to-360 CarHub trial. **Dealer booking, BAM job integration, billing, user accounts and notifications have intentionally been removed.**

## What it does

1. Pick **front → side-left → rear → side-right** and capture/upload the real vehicle photos (JPEG, PNG or WebP, maximum 20 MiB each).
2. Enter the private trial access code (not the CarHub secret).
3. Tap **Generate my 360° spin**. The server forwards the four photos, in order, as repeated `images` multipart fields to `POST /v1/render/360`, with `frame_count=36`, `output_format=jpg`, `viewer=false`, and an `Idempotency-Key`.
4. The app polls `GET /v1/jobs/{job_id}` through your private server until `succeeded`, `failed`, `expired` or `canceled`.
5. It shows CarHub's returned signed frame URLs in a drag-to-rotate viewer. You can export the frames in a ZIP, or assemble a 6-second MP4 from the frames on the server — **not** a separate CarHub video API call.

The app has an **Original photo preview** that works without CarHub. This is distinctly labelled as **not AI-generated**.

## Setup (requires a server — no API key in the browser)

**Local server**

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# macOS/Linux:
export CARHUB_API_KEY='chk_stg_or_chk_live_your_actual_private_key'
export BAM_TRIAL_PIN='choose-an-access-code'
export BAM_MAX_GENERATIONS=5
uvicorn server:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000`. Other devices need network access to that machine, and a phone camera/PWA requires HTTPS except for localhost.

**Online trial — Render.com**

1. Unzip and upload this folder into a **private** GitHub repository.
2. In Render, create a Python web service using the repo (or use the bundled `render.yaml`).
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
5. Under **Environment**, set `CARHUB_API_KEY` to the secret key from your CarHub dashboard and set `BAM_TRIAL_PIN` to a private testing code. Optionally set `BAM_MAX_GENERATIONS=5`.
6. Open Render's HTTPS URL and enter **the trial PIN** in the app. Do not paste the actual CarHub API key into the app or share it in chat.

Other Python/FastAPI hosts work too. Restrict your test site to invited testers; PIN protection is an initial test gate, **not** a full authentication system.

## Permissions and costs

CarHub's documented API host is `https://api.trycarhub.com`. The 360 render endpoint is listed at **$1.20 per vehicle** on the public pricing page (verify your live account catalogue before use). The endpoint may require beta entitlement. `chk_test_` keys return **sample**, not real AI inference; live/staging keys can perform actual inference if your organisation has access and credit. Staging and production may use the same credit pool.

The server limits accepted generation submissions to five per running instance by default (`BAM_MAX_GENERATIONS`); this is a lightweight test safeguard, not a durable quota and it resets on restart. Downloading MP4 and frames may consume server bandwidth/compute but not another CarHub render call.

## Known boundaries

- CarHub currently supplies generated frames, not necessarily a permanent hosted viewer. Our viewer works with the returned signed frame URLs. Those URLs expire; export promptly.
- This is not production security: no persistent database, user identities, durable API usage cap, webhook endpoint, signed-user access or rate limiting by IP. Trial job IDs are kept in memory and are lost if the server restarts.
- The same exact vehicle must appear in every photo. AI-generated viewpoints may invent unobserved details. Always check marketing output against source photos before publishing.
- The server needs outbound HTTPS to the CarHub API and to the signed asset URLs. It cannot call a live CarHub account until you supply the server environment variable.
- FastAPI/HTTPX/Pillow/imageio-ffmpeg install on the server. HTML can be previewed alone, but **real generation requires a running Python backend**.

## Primary documentation

- [CarHub generate a 360 spin](https://docs.trycarhub.com/guides/spin-360)
- [CarHub asynchronous jobs](https://docs.trycarhub.com/guides/jobs)
- [CarHub API overview](https://docs.trycarhub.com/api-reference/overview)
- [CarHub pricing](https://trycarhub.com/pricing)
- [CarHub terms](https://trycarhub.com/terms) (requires server-side keys)
