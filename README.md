# video-gen

A small CLI for generating videos with Google **Veo** on **Vertex AI** — text-to-video and
image-to-video (first frame, or first+last frame interpolation) — via the `predictLongRunning`
REST API, with no dependency on the `google-genai` SDK or a Cloud Storage bucket for small clips.

See [`SKILL.md`](SKILL.md) for the agent/skill-oriented reference (flags, cost table, auth notes).
This README covers human setup and local usage.

## Why this exists

The official `google-genai` SDK samples for Veo assume the input image already lives in a GCS
bucket. Vertex's REST API actually accepts the image inline as base64 (`bytesBase64Encoded`), and
can return the finished video the same way if you don't pass `--storage-uri` — so for one-off or
small-batch generation you never need to touch Cloud Storage. This tool implements that path
directly against the REST API.

## Setup

1. A GCP project with the Vertex AI API enabled and a service account with `roles/aiplatform.user`,
   key downloaded as JSON.
2. Install [`uv`](https://docs.astral.sh/uv/) (or use `pip install -e .` in a venv).
3. Copy `.env.example` to `.env` and fill in `GOOGLE_CLOUD_PROJECT` and
   `GOOGLE_APPLICATION_CREDENTIALS` (absolute path to the service-account key). Never commit `.env`
   or any `*-key.json` file — both are gitignored.

```bash
cp .env.example .env
# edit .env
uv run video_gen.py "a cat reading a book" -o cat.mp4
```

## Usage

```bash
# Text-to-video
uv run video_gen.py "a cinematic drone shot over a neon-lit city at night" -o city.mp4

# Image-to-video, cheapest settings (Veo 3.1 Lite, 4s, 720p, 9:16)
uv run video_gen.py "she waves and does a quick two-step dance" \
  --image ./portrait.png -o dance.mp4

# Higher-quality final render
uv run video_gen.py "..." --image ./portrait.png \
  --model veo-3.1-generate-001 --resolution 1080p --duration 8 -o final.mp4
```

Run `uv run video_gen.py --help` for the full flag list.

## Cost

Default model is `veo-3.1-lite-generate-001` at `--duration 4`, the cheapest combination
(~$0.12/clip as of writing). See the cost table and caveats in [`SKILL.md`](SKILL.md#model-selection--cost)
before switching models — always confirm current pricing at the
[official pricing page](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing#veo).

## Testing

```bash
uv run --extra dev pytest
```

Unit tests (`tests/test_video_gen.py`) cover request-body construction, validation, and response
parsing with no network calls. There is no automated integration test against the live Veo API
(it costs real money per run) — see `tests/README.md` for the manual integration-test procedure and
its results log.

## Known limitations

- Region is hardcoded to default `us-central1` (overridable via `GOOGLE_CLOUD_LOCATION` /
  `--location`) — that's the only region Veo is currently listed as available in.
- Non-9:16/16:9 input images get server-side resized/center-cropped; use `--resize-mode pad` or
  pre-process the image yourself if a center-crop would cut off something important.
- No retry/backoff on transient API errors yet — a failed request just exits non-zero.

## License

MIT
