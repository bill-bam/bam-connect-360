# BAM.Connect 360 — diagnostic v4 (20 November 2026)

This update investigates the CarHub error `must contain 4 to 4 inputs` without pretending that the new API shape is verified. It does not automatically retry paid generation attempts.

## Deploy
Upload extracted files to the root of the same private GitHub repository, commit and let Render redeploy. Existing `CARHUB_API_KEY`, `BAM_TRIAL_PIN`, `CARHUB_BACKGROUND_ASSET_ID` remain on Render; do not put them into GitHub. Check `/api/health` for `version: diagnostic-v4`.

## Test sequence

1. Select the four photos, enter your private PIN, click **Check 4-photo upload — FREE**. This calls ONLY BAM `/api/check-upload`; it validates four actual image bytes and never contacts CarHub.
2. If the free test passes, do not assume CarHub will accept them. The experimental **New studio settings** request includes BAM background + 1080p + 152 frame metadata as a `params` multipart text field; the provider format still needs its exact supported request example.
3. **Original 4-photo format (without studio)** sends the flat-field multipart shape that worked in earlier tests: 4 `images` file parts, `frame_count=36`, `output_format=jpg`, `viewer=false`. This may consume a real CarHub generation and deliberately omits the new BAM showroom asset. Use only if you wish to isolate whether the new `params` block causes the validation error.
4. Every CarHub response error shows which mode was sent, image count, and HTTP status, without exposing the key or image data. Never send another request blindly.

## What to ask Aurel
Please provide a complete working cURL example for sending four local image files along with `params.background.asset_id`, `quality=1080`, `frame_count=152`, and `output_format=jpg` to `/v1/render/360`. Specifically, confirm whether `params` is a JSON-encoded multipart field, an application/json part, or is supplied in a separate JSON request after uploading the four image assets. The server currently receives a validation error `must contain 4 to 4 inputs` while forwarding four `images` multipart file parts.

This is a troubleshooting tool, **not** a verified fix for CarHub studio generation.
