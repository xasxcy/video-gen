---
name: video-gen
description: Generate videos with Google Veo on Vertex AI (text-to-video and image-to-video). Use when the user asks to generate, create, or animate a video from a text prompt or a still image.
version: 0.1.0
metadata:
  requires:
    anyBins:
      - uv
      - python3
---

# Video Generation (Veo on Vertex AI)

Calls Google Veo (`veo-3.1-generate-001`, `veo-3.1-fast-generate-001`, `veo-3.1-lite-generate-001`)
through Vertex AI's `predictLongRunning` REST API. Supports text-to-video and image-to-video
(first frame, optionally first+last frame).

`veo-2.0-generate-001`, `veo-3.0-generate-001` and `veo-3.0-fast-generate-001` are **not** supported —
Google retired all three on 2026-06-30 (confirmed on their model-garden pages) and requests against
them now fail outright. `video_gen.py` rejects them with a clear error rather than silently making a
doomed API call.

## Script Directory

**Agent Execution**:
1. `{baseDir}` = this SKILL.md file's directory
2. Script path = `{baseDir}/video_gen.py`
3. Runtime: `uv run --with-requirements {baseDir}/pyproject.toml {baseDir}/video_gen.py` (uv resolves
   deps on the fly), or `python3 {baseDir}/video_gen.py` if deps are already installed in the active env.

## Step 0: Load Configuration ⛔ BLOCKING

Requires `GOOGLE_CLOUD_PROJECT` and `GOOGLE_APPLICATION_CREDENTIALS` (a Vertex AI service-account key
with `roles/aiplatform.user`). Load order: CLI flags > shell env > `./.env` > `~/.video-gen/.env`.

If neither the shell environment nor either `.env` location has these set, **stop and ask the user**
for the GCP project ID and the path to a service-account key — do not guess or scan the filesystem for one.

## Usage

```bash
# Text-to-video
python3 {baseDir}/video_gen.py "a cinematic drone shot over a neon-lit city at night" -o city.mp4

# Image-to-video (first frame), cheapest settings (4s, 720p, Veo 3.1 Lite)
python3 {baseDir}/video_gen.py "the woman smiles and does a quick hand-wave dance" \
  --image ./portrait.png -o dance.mp4

# Higher quality for a final render
python3 {baseDir}/video_gen.py "..." --image ./portrait.png \
  --model veo-3.1-generate-001 --resolution 1080p --duration 8 -o final.mp4

# First + last frame interpolation
python3 {baseDir}/video_gen.py "morph smoothly between the two poses" \
  --image ./frame_start.png --last-frame ./frame_end.png -o morph.mp4
```

## Options Reference

| Flag | Description | Default |
|------|-------------|---------|
| `prompt` (positional) | Text prompt describing the motion/scene | required |
| `--image` | Local path, first frame (image-to-video) | none (text-to-video) |
| `--last-frame` | Local path, last frame (interpolation) | none |
| `-o`, `--output` | Output video path | `output.mp4` |
| `--model` | Veo model ID (`veo-3.1-generate-001` / `veo-3.1-fast-generate-001` / `veo-3.1-lite-generate-001`) | `veo-3.1-lite-generate-001` (cheapest) |
| `--duration` | Seconds: 4, 6, or 8 | `4` |
| `--aspect-ratio` | `9:16` or `16:9` | `9:16` |
| `--resolution` | `720p`/`1080p`/`4k` (Veo 3.x only) | model default (720p) |
| `--sample-count` | Number of variants, 1-4 | `1` |
| `--negative-prompt` | Content to avoid | none |
| `--person-generation` | `dont_allow` / `allow_adult` / `allowAll` | `allow_adult` |
| `--seed` | uint32 for deterministic output | none |
| `--resize-mode` | `crop` / `pad`, for input images that aren't 9:16/16:9 | API default (`pad`) |
| `--audio` / `--no-audio` | Generate a synced audio track | off (`--no-audio`, matches the video-only cost table) |
| `--storage-uri` | `gs://...` — write output to Cloud Storage instead of returning bytes inline | none |
| `--force` | Overwrite `--output` if it already exists | off — refuses and exits rather than silently clobbering a prior (paid-for) result |

## Model Selection / Cost

Prices are USD per second of *output* video, as listed on the
[official pricing page](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing#veo)
(checked 2026-08-01 — re-verify before assuming these hold, Google revises this page without notice).
Every model has two price tiers: with audio and without. **Veo defaults to generating audio**
(`generateAudio: true` server-side) — you only get the video-only price if you explicitly request it.

| Model | Tier | 720p | 1080p | 4k |
|-------|------|------|-------|-----|
| `veo-3.1-lite-generate-001` | video only | $0.03/s | $0.05/s | — |
| `veo-3.1-lite-generate-001` | + audio | $0.05/s | $0.08/s | — |
| `veo-3.1-fast-generate-001` | video only | $0.08/s | $0.10/s | $0.25/s |
| `veo-3.1-fast-generate-001` | + audio | $0.10/s | $0.12/s | $0.30/s |
| `veo-3.1-generate-001` | video only | $0.20/s | $0.20/s | $0.40/s |
| `veo-3.1-generate-001` | + audio | $0.40/s | $0.40/s | $0.60/s |

`veo-3.1-lite-generate-001` video-only 720p is the cheapest combination available — **default, use
for iteration/testing**. (Veo 2 and the Veo 3.0 line used to be cheaper-but-lower-quality options;
both are retired as of 2026-06-30, see above.)

**To control which tier you're billed at**, this tool maps directly to the two levers above:

| Want | Flags |
|------|-------|
| Cheapest, video only | `--model veo-3.1-lite-generate-001 --resolution 720p` (`--no-audio` is already the default) |
| Video only, higher quality | add `--model veo-3.1-generate-001 --resolution 1080p` (or `4k`) |
| With synced audio | add `--audio` — this alone moves you to the "+ audio" row of whatever model/resolution you picked |

A 4s clip on the default (`veo-3.1-lite-generate-001`, 720p, no audio) costs about **$0.12**. Always
default to that combination for exploratory/test runs; only switch to a pricier model or add `--audio`
for a final render the user explicitly asked for.

## Auth Notes

- Uses a Vertex AI **service account** key (`GOOGLE_APPLICATION_CREDENTIALS`), exchanged for a
  short-lived OAuth access token via `google-auth` — this is different from `GOOGLE_API_KEY`/
  `GEMINI_API_KEY` used by AI Studio-style tools (e.g. `baoyu-image-gen`'s Google provider).
- Region is fixed to `us-central1` by default (`GOOGLE_CLOUD_LOCATION`) — as of the current model
  garden listing, Veo models on Vertex AI are only available in that region.

## Known Constraints (Vertex AI, verified against official docs)

- Input image: `image/png` or `image/jpeg`, ≤20 MB. Non-9:16/16:9 images are `pad`ded by default
  (verified: a 3:4 portrait comes back with letterboxing, not a crop) — pass `--resize-mode crop` if
  you'd rather fill the frame and risk cutting off the edges.
- Output: if `--storage-uri` is omitted, video bytes are returned inline in the operation response and
  written directly to `--output`. If `--storage-uri` is given, the tool writes the `gs://` URI to
  `--output` instead of bytes — download separately with `gcloud storage cp`.
- Long-running operation: submission returns immediately; the tool polls `fetchPredictOperation` every
  `--poll-interval` seconds (default 15) until done or `--timeout` (default 600s) is hit.
