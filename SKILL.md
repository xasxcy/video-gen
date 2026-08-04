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

> Google has renamed **Vertex AI** to **Gemini Enterprise Agent Platform** (docs now live under
> `docs.cloud.google.com/gemini-enterprise-agent-platform/...`); the `aiplatform.googleapis.com`
> endpoint itself is unchanged. This doc still says "Vertex AI" throughout since that's still the
> commonly-used name and matches the API host — just noting the rename here rather than doing a
> full find-replace.

Calls Google Veo (`veo-3.1-generate-001`, `veo-3.1-fast-generate-001`, `veo-3.1-lite-generate-001`)
through Vertex AI's `predictLongRunning` REST API. Supports text-to-video and image-to-video
(first frame, optionally first+last frame).

For short clips (≤10s) or image-to-video with no need for interpolation, see
[Gemini Omni Flash](#gemini-omni-flash-short-clip-alternative) below — a separate, simpler script.

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

> **调用方约束（时长判断）**：调用方（Agent/用户）在生成视频前，**必须自行评估动作或动作链的预计所需时长**，选择最合适的秒数（4秒、6秒或8秒）。切勿盲目依赖默认的 4 秒——若动作过于复杂或包含多个连贯步骤，4 秒会导致动作未完成即被截断，或被分割/快进成多段片段。
| `--resolution` | `720p`/`1080p`/`4k` (Veo 3.x only) | model default (720p) |
| `--sample-count` | Number of variants, 1-4 | `1` |
| `--negative-prompt` | Content to avoid | none |
| `--person-generation` | `dont_allow` / `allow_adult` / `allowAll` | `allow_adult` |
| `--seed` | uint32 for deterministic output | none |
| `--resize-mode` | `crop` / `pad`, for input images that aren't 9:16/16:9 | API default (`pad`) |
| `--audio` / `--no-audio` | Generate a synced audio track | off (`--no-audio`, default) |
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

## Gemini Omni Flash (short-clip alternative)

`video_gen_omni.py` calls Google's **Gemini Omni Flash** (`gemini-omni-flash-preview`) through the
Interactions API (`POST .../v1beta1/projects/{project}/locations/global/interactions`) — a
completely different endpoint/request shape from Veo's `predictLongRunning`, so it's a separate
script rather than a `--model` branch on `video_gen.py`. It only shares `_auth.py` (service-account
token exchange) with `video_gen.py`.

**Agent Execution**: script path = `{baseDir}/video_gen_omni.py`; same runtime options as
`video_gen.py` above (`uv run` or `python3` directly).

### When to use Omni instead of Veo

- You need a clip **10 seconds or under**.
- You're doing image-to-video and just need the reference image to drive the first frame (no
  interpolation, no first+last-frame morph).

For anything longer than 10s, 1080p/4k, first+last-frame interpolation, or Veo's multi-reference-image
subject consistency, use `video_gen.py` (Veo) instead — Omni Flash doesn't support any of those.

### v1 scope — explicitly NOT implemented

Omni Flash's API supports these capabilities, but **this CLI does not implement them yet**:

- **Multi-turn conversational editing** (chaining `steps` from a prior interaction into the next
  request's `input` to iteratively edit a video). This needs client-side conversation-state
  management that `video_gen_omni.py` doesn't have.
- **`reference_to_video`** (regenerating a video driven by a reference image for subject
  consistency, as distinct from plain image-to-video). The request-body shape for this task type
  hasn't been implemented or verified.

Don't imply either of these works when describing this tool — they're real Omni Flash capabilities,
just not ones this script exposes.

### Usage

```bash
# Text-to-video, cheapest (3s, the minimum Omni allows)
python3 {baseDir}/video_gen_omni.py "a cat reading a book by a window" -o cat.mp4

# Image-to-video (first frame drives the clip; image is inlined as base64, not GCS)
python3 {baseDir}/video_gen_omni.py "the woman smiles and does a quick hand-wave" \
  --image ./portrait.png -o dance.mp4 --duration 5

# Async submission (result kept 14 days), then recover it later without resubmitting/re-paying
python3 {baseDir}/video_gen_omni.py "..." --background -o clip.mp4
# ... prints "interaction: projects/P/locations/global/interactions/ID" to stderr; later:
python3 {baseDir}/video_gen_omni.py "unused-when---interaction-is-set" \
  --interaction projects/P/locations/global/interactions/ID -o clip.mp4
```

### Options Reference

| Flag | Description | Default |
|------|-------------|---------|
| `prompt` (positional) | Text prompt describing the desired video | required |
| `--image` | Local path, inlined as base64 (image-to-video, first frame only) | none (text-to-video) |
| `-o`, `--output` | Output video path | `output.mp4` |
| `--model` | Omni Flash model ID | `gemini-omni-flash-preview` |
| `--duration` | Seconds, **3-10** (hard cap, no exceptions) | `3` (cheapest) |
| `--aspect-ratio` | `9:16` or `16:9` | `9:16` |
| `--storage-uri` | `gs://...` — deliver output to Cloud Storage instead of inline base64 | none (inline) |
| `--force` | Overwrite `--output` if it already exists | off |
| `--background` | Submit asynchronously instead of waiting inline; result is retrievable for 14 days | off (synchronous) |
| `--interaction` | Resume mode: an interaction resource name from a prior `--background` submission — skips submit, goes straight to poll/fetch | none |
| `--project` / `--location` | GCP project / region — **default location is `global`**, not `video_gen.py`'s `us-central1` | `GOOGLE_CLOUD_PROJECT` / `global` |
| `--credentials` | Service-account key path | `GOOGLE_APPLICATION_CREDENTIALS` |
| `--poll-interval` / `--timeout` | Polling cadence / max wait — **only applies to `--interaction` resume mode**; a fresh `--background` submission returns immediately after printing the interaction name and never polls | `15s` / `600s` |

Not offered (deliberately, given current model limits): `--resolution` (fixed 720p, no 1080p/4k),
`--audio`/`--no-audio` (video output always includes audio, no way to disable), `--last-frame` (no
first+last-frame interpolation support), `--sample-count` (only one video per interaction is
supported by this CLI; the API's per-prompt count parameter isn't wired up).

### Known limits

- **10-second hard cap** on `--duration` — `video_gen_omni.py` rejects anything outside 3-10s before
  making a request.
- **Fixed 720p** — no resolution flag exists because there's nothing to choose between.
- **No frame interpolation** — Omni Flash has no first+last-frame morphing feature.
- **Video output always has audio** — there is no video-only tier/flag, unlike Veo.
- **Mixed billing**: $0.10/s video output (720p, with audio) *plus* separate input/output token
  charges (text/image/video/audio input $1.50/M tokens, text output $9/M tokens) — costs aren't a
  flat per-second number the way Veo's video-only tier is. See the pricing page linked in the Veo
  cost section above (Omni Flash pricing is on the same page).
- **`delivery: "uri"` (GCS output) is unverified** — actual real-API testing so far only exercised
  inline base64 delivery (`--storage-uri` unset). The GCS-URI code path exists but hasn't been
  exercised against a live response.
- **`status: "failed"` responses are unverified** — polling/error-handling for a failed interaction
  is implemented defensively but has never been observed against a real failed request.
- **A lost submission response is unrecoverable** — if `submit_interaction`'s POST fails at the
  network layer (timeout, connection reset) *after* Vertex has already accepted and started billing
  the request, this CLI has no interaction id to reconcile against and `--interaction` cannot help,
  since it never received one. The Interactions API has no documented client-supplied idempotency/
  correlation key to recover from this, so `submit_interaction` only surfaces the uncertainty in its
  error message (check the Vertex AI console) — it does not, and currently cannot, actually recover
  the job. Do not blindly retry after this specific error; it may create a duplicate paid generation.
