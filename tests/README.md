# Tests

## Unit tests

```bash
uv run --extra dev pytest
```

Pure logic only (request-body construction, validation, response parsing) — no network calls, no
GCP credentials required, free to run.

## Manual integration test

The live Veo API costs real money per call, so it is not run automatically. Procedure for a manual
check after changing anything that touches the request/response shape:

1. Ensure `.env` (or the shell env) has a valid `GOOGLE_CLOUD_PROJECT` and
   `GOOGLE_APPLICATION_CREDENTIALS`.
2. Run the cheapest possible request to confirm auth + request/response shape are still correct:
   ```bash
   uv run video_gen.py "a static shot of a red apple on a wooden table" \
     --model veo-3.1-lite-generate-001 --duration 4 --resolution 720p -o /tmp/smoke.mp4
   ```
3. Confirm `/tmp/smoke.mp4` exists and plays.
4. Log the run below with date, model, cost estimate, and outcome.

## Run log

| Date | Command | Model | Est. cost | Result |
|------|---------|-------|-----------|--------|
