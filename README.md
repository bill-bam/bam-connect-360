# BAM.Connect 360 — Buyer sharing update (24 November 2026)

## Upload only these 3 files to the root of the existing GitHub repository

- `server.py` — current successfully working CarHub upload, BAM studio background `01a20b66-75a1-7e00-9aa6-63363169c0dd`, 1080 quality, 152 frames; **plus** signed buyer share endpoints.
- `index.html` — unchanged capture flow and interactive viewer, with `Create buyer viewing link` after successful generation.
- `buyer.html` — buyer-branded public viewer with touch rotation, zoom, fullscreen, auto-rotate and MP4 download.

Do not change `viewer.html`, `render.yaml`, `requirements.txt`, or the existing environment variables. No new dependencies are required.

## How it works

1. Capture 4 photos and generate as before.
2. When the job shows the real 360 result, click **Create buyer viewing link**, then **Copy buyer link** or **Copy direct MP4 link**. To share an older generated job without another AI generation, use **Share an existing job** on the capture page and enter its CarHub job ID.
3. Send the link by WhatsApp or email.
4. The buyer opens `/share/<signed-token>` without entering the BAM trial PIN, rotates the car, and clicks **Download marketing video (MP4)**.
5. The backend retrieves the original CarHub generated frames and encodes the existing Full HD/30fps export on demand. This is not another CarHub 360 generation request, though it does use provider storage/bandwidth and server CPU.

## Important trial limitation

These links are **temporary, not permanent marketing hosting**. They default to expiring in 7 days and rely on CarHub retaining the original generated frames and keeping their URLs accessible. Actual frame availability may be shorter. Render service restarts are handled by the signed token, but key/PIN rotation invalidates old tokens unless you set a dedicated `BAM_SHARE_SECRET` in Render. Use a random long secret and keep it unchanged. For durable dealer delivery, migrate completed 152 frames and one MP4 into private object storage (e.g., S3-compatible or Cloudflare R2) with separate signed access links.

Optional environment variables:

- `BAM_SHARE_SECRET`: a long random server-side string (recommended; never put in GitHub).
- `BAM_PUBLIC_URL`: `https://bam-connect-360-trial.onrender.com` (recommended to avoid proxy URL issues).
- `BAM_SHARE_LINK_DAYS`: number of days (1–30, defaults to 7), **not** a guarantee the provider still retains images.

Sharing is link-based: **anyone with the link can view and download the media**. Use only after the output has been reviewed and approved for customer use.

## Testing without extra AI credits

Generate once, click create link (does not initiate a new generation), open the link in a different browser or incognito tab. Test rotate and video download. A video download may take time because the 152 frames are fetched and compiled.
