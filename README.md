# BAM.Connect 360 — CarHub BAM Studio test (20 November 2026)

This builds on the **existing Render-deployed BAM.Connect 360 trial** and does not add dealer booking or any production BAM integration.

## New render settings

The app can now request the BAM showroom background using asset ID:
`01a1f8d3-6dcd-7ba9-ad18-6ece95c24d53`

Default settings: **1080p · 152 frames · 16:9 · JPG**. The background option can be turned off to compare results, and frame count / resolution can be changed for testing. The API key is kept server-side.

The server sends the four ordered photos as repeated `images` multipart file fields plus a **JSON-stringified `params` field** that matches the nested settings Aurel supplied:

```json
{
  "params": {
    "background": {
      "asset_id": "01a1f8d3-6dcd-7ba9-ad18-6ece95c24d53",
      "image_ratio": "16:9"
    },
    "quality": 1080,
    "frame_count": 152,
    "output_format": "jpg"
  }
}
```

**Caveat:** The JSON `params` multipart field is inferred from Aurel's illustrative request. The exact multipart field encoding has not been validated against a live request for the newly released background feature. If CarHub returns 400 or 422, verify the field encoding with Aurel before changing it or re-submitting (to avoid unexpected charges).

## What else changed

- Raised previous 72-frame truncation limit to **152 frames** throughout frame retrieval and export.
- MP4 now uses all returned frames in a single revolution at **1920×1080, 30fps** instead of 540p / 12fps. A 152-frame clip runs about **5.07 seconds**.
- Exports download the provider's signed frame URLs into temporary files in batches of six, reducing peak memory use compared with holding all 152 files in RAM.
- The PWA's service worker cache version is incremented.
- `CARHUB_BACKGROUND_ASSET_ID` can override the showroom asset later without modifying source code.

## Update the live Render trial

Your current trial URL is **https://bam-connect-360-trial.onrender.com/**. This ZIP is **not** a live deployment.

1. Go to your GitHub repository **bill-bam/bam-connect-360** and upload the **contents of this ZIP** into the repository root, overwriting the old files. Crucial changes are `server.py`, `index.html`, `sw.js`, `render.yaml` and this `README.md`.
2. Commit the changes to `main`. Render should deploy the update automatically. If not, on Render choose **Manual Deploy → Deploy latest commit** (or trigger Blueprint sync if required).
3. Do **not** paste `CARHUB_API_KEY` into GitHub or the browser. Your existing `CARHUB_API_KEY` and `BAM_TRIAL_PIN` settings on Render should stay intact. If your Render Blueprint is configured to manage variables, check that the new `CARHUB_BACKGROUND_ASSET_ID` is present (the code also defaults to the correct ID).
4. In the deployed trial open `/api/health` and check `"version":"studio-152-v2"` and `"default_frames":152`. Hard-refresh your browser if an old page is cached.
5. Capture the four photos, leave **Apply BAM showroom** on, choose 1080p and 152 frames and submit one test. Check that CarHub's returned frames show the BAM background throughout.
6. Export MP4 or ZIP. High-resolution 152-frame files can be large and MP4 transcoding can be CPU-intensive on Render's 512 MB instance; if it fails, examine Render logs and test a lower resolution or upgrade the host.

## Safety / limitations

The locally enforced generation cap (`BAM_MAX_GENERATIONS`, default 5) resets on restarts. The server has no database; job identifiers and generated files are not retained after restart. Signed output URLs can expire. The PWA is a temporary development prototype, **not production-secure**. AI-generated frames may hallucinate vehicle details; do not market a vehicle without checking them against the actual photos.

This package has been tested for code integrity and with a mock CarHub response; it has **not** been tested against your private CarHub account or charged real API credits.
