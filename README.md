# BAM.Connect 360 — canonical CarHub API v2 fix (20 November 2026)

Corrects CarHub 400: **Provide inputs and params exactly once as JSON form fields**.

## What changed
The server sends exactly 6 multipart fields:
- `inputs`: JSON array of 4 objects, each with `file` naming its corresponding photo part and a unique `client_id`, Content-Type `application/json`.
- `params`: JSON object once, Content-Type `application/json`, optionally including your BAM background and 1080p/152 frames.
- `photo_1`, `photo_2`, `photo_3`, `photo_4`: four file parts in the required front, side-left, rear, side-right order.

This matches CarHub's [API conventions](https://docs.trycarhub.com/api-reference/conventions) and [360 Generation API](https://docs.trycarhub.com/api-reference/image-production/360-generation). It is structurally verified with a local mock, **not yet confirmed with a chargeable live CarHub render**.

## Deploy
The simplest path is to replace `server.py`, `index.html` and `sw.js` in your private GitHub repo `bill-bam/bam-connect-360`, commit to `main`, and let Render redeploy. You can also upload all extracted files, replacing existing ones. No API key or Render environment-variable changes required.

Confirm [health endpoint](https://bam-connect-360-trial.onrender.com/api/health) reports `version: canonical-v5`. Hard refresh the webpage if it still says Diagnostic v4.

Use **Check 4-photo upload — FREE** before the one live generation, then start 1080p/152 with the BAM showroom asset. CarHub may charge higher-resolution surcharges. Never upload your API key to GitHub.
