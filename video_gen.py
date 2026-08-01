#!/usr/bin/env python3
"""CLI for generating videos with Google Veo on Vertex AI (predictLongRunning)."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import tempfile
import time
from pathlib import Path

import requests
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# veo-2.0-generate-001, veo-3.0-generate-001 and veo-3.0-fast-generate-001 are not listed here:
# per Google's own model-garden pages they were retired 2026-06-30, and requests against them now
# fail outright. Only the currently-live Veo 3.1 family is supported.
DURATION_RANGES = {
    "veo-3.1-generate-001": (4, 6, 8),
    "veo-3.1-fast-generate-001": (4, 6, 8),
    "veo-3.1-lite-generate-001": (4, 6, 8),
}

DEFAULT_MODEL = os.environ.get("VIDEO_GEN_MODEL", "veo-3.1-lite-generate-001")


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_access_token(credentials_path: str) -> str:
    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    creds.refresh(AuthRequest())
    return creds.token


def guess_mime_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime not in ("image/png", "image/jpeg"):
        raise ValueError(
            f"unsupported image type for {path} (Veo only accepts image/png or image/jpeg)"
        )
    return mime


def encode_image(path: Path) -> dict:
    data = path.read_bytes()
    max_bytes = 20 * 1024 * 1024
    if len(data) > max_bytes:
        raise ValueError(
            f"{path} is {len(data) / 1024 / 1024:.1f} MB, exceeds Veo's 20 MB image-to-video limit"
        )
    return {
        "bytesBase64Encoded": base64.b64encode(data).decode("ascii"),
        "mimeType": guess_mime_type(path),
    }


def build_request_body(args: argparse.Namespace) -> dict:
    instance: dict = {"prompt": args.prompt}
    if args.image:
        instance["image"] = encode_image(Path(args.image))
    if args.last_frame:
        instance["lastFrame"] = encode_image(Path(args.last_frame))

    parameters: dict = {
        "sampleCount": args.sample_count,
        "aspectRatio": args.aspect_ratio,
        "durationSeconds": args.duration,
        "personGeneration": args.person_generation,
        "generateAudio": args.audio,
    }
    if args.storage_uri:
        parameters["storageUri"] = args.storage_uri
    if args.negative_prompt:
        parameters["negativePrompt"] = args.negative_prompt
    if args.seed is not None:
        parameters["seed"] = args.seed
    if args.resolution:
        parameters["resolution"] = args.resolution
    if args.resize_mode:
        parameters["resizeMode"] = args.resize_mode

    return {"instances": [instance], "parameters": parameters}


RETIRED_MODELS = {
    "veo-2.0-generate-001": "2026-06-30",
    "veo-3.0-generate-001": "2026-06-30",
    "veo-3.0-fast-generate-001": "2026-06-30",
}


def validate_duration(model: str, duration: int) -> None:
    if model in RETIRED_MODELS:
        raise ValueError(
            f"model {model} was retired on {RETIRED_MODELS[model]} and no longer accepts requests; "
            "use one of the veo-3.1 models instead"
        )
    allowed = DURATION_RANGES.get(model)
    if allowed is not None and duration not in allowed:
        raise ValueError(f"model {model} does not support duration={duration}s (allowed: {list(allowed)})")


def submit_generation(
    project: str, location: str, model: str, token: str, body: dict
) -> str:
    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{location}/publishers/google/models/{model}:predictLongRunning"
    )
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps(body),
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"predictLongRunning failed ({resp.status_code}): {resp.text}")
    operation_name = resp.json().get("name")
    if not operation_name:
        raise RuntimeError(f"no operation name in response: {resp.text}")
    return operation_name


def poll_operation(
    project: str,
    location: str,
    model: str,
    token: str,
    operation_name: str,
    poll_interval: int,
    timeout: int,
) -> dict:
    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{location}/publishers/google/models/{model}:fetchPredictOperation"
    )
    deadline = time.monotonic() + timeout
    while True:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=json.dumps({"operationName": operation_name}),
            timeout=60,
        )
        if not resp.ok:
            raise RuntimeError(f"fetchPredictOperation failed ({resp.status_code}): {resp.text}")
        payload = resp.json()
        if payload.get("done"):
            return payload
        if time.monotonic() > deadline:
            raise TimeoutError(f"video generation did not finish within {timeout}s")
        print(f"  ...still generating, polling again in {poll_interval}s", file=sys.stderr)
        time.sleep(poll_interval)


def expected_output_paths(output: Path, sample_count: int) -> list[Path]:
    """Derive the per-sample destination paths save_videos() will write to.

    Naming depends only on the *requested* sample_count, never on how many videos the
    API actually returns (which can be fewer, e.g. some filtered by RAI checks) — that
    keeps this function and save_videos() using the exact same paths, so a pre-flight
    check here can't miss a collision that save_videos() would hit after paying for
    the generation.
    """
    if sample_count <= 1:
        return [output]
    return [output.with_name(output.stem + f"-{i}" + output.suffix) for i in range(sample_count)]


def _write_exclusive(dest: Path, data: bytes | str, *, text: bool = False) -> None:
    """Create dest only if it doesn't already exist, atomically (no check-then-write gap).

    Uses O_EXCL so two concurrent calls targeting the same dest can't both "pass" a
    prior os.path.exists() check and then race each other via a temp-file replace —
    the OS guarantees only one open() with O_CREAT|O_EXCL succeeds.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(dest, flags)
    except FileExistsError:
        raise FileExistsError(
            f"{dest} already exists; pass --force to overwrite (a completed generation costs real "
            "money, so this tool refuses to silently clobber a prior result)"
        ) from None
    try:
        with os.fdopen(fd, "w" if text else "wb") as f:
            f.write(data)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def _write_atomic_force(dest: Path, data: bytes | str, *, text: bool = False) -> None:
    """Overwrite dest via a unique temp file + atomic rename (force=True path)."""
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w" if text else "wb") as f:
            f.write(data)
        tmp.replace(dest)  # atomic on POSIX; avoids leaving a truncated dest on a write failure
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_videos(payload: dict, output: Path, sample_count: int = 1, force: bool = False) -> list[Path]:
    response = payload.get("response")
    if not response:
        raise RuntimeError(f"operation finished with no response payload: {json.dumps(payload)[:2000]}")

    filtered = response.get("raiMediaFilteredCount", 0)
    if filtered:
        reasons = response.get("raiMediaFilteredReasons", [])
        raise RuntimeError(f"{filtered} video(s) filtered by responsible-AI checks: {reasons}")

    videos = response.get("videos", [])
    if not videos:
        raise RuntimeError(f"no videos in response: {json.dumps(response)[:2000]}")

    dests = expected_output_paths(output, sample_count)[: len(videos)]

    # Fail before writing anything if *any* target collides — otherwise a collision on
    # a later sample would leave earlier (paid-for) samples written and the rest lost.
    # (This is a best-effort early exit for the common case; _write_exclusive() below is
    # what actually guarantees no overwrite, since this check alone has a TOCTOU gap.)
    if not force:
        existing = [d for d in dests if d.exists()]
        if existing:
            raise FileExistsError(
                f"{len(existing)} of {len(dests)} target file(s) already exist "
                f"({', '.join(str(d) for d in existing)}); pass --force to overwrite"
            )

    saved = []
    for dest, video in zip(dests, videos):
        if "bytesBase64Encoded" in video:
            data = base64.b64decode(video["bytesBase64Encoded"])
            if force:
                _write_atomic_force(dest, data)
            else:
                _write_exclusive(dest, data)
        elif "gcsUri" in video:
            if force:
                _write_atomic_force(dest, video["gcsUri"], text=True)
            else:
                _write_exclusive(dest, video["gcsUri"], text=True)
            print(
                f"video stored in Cloud Storage, wrote its URI to {dest} "
                f"(download separately with: gcloud storage cp '{video['gcsUri']}' <local-path>)",
                file=sys.stderr,
            )
            saved.append(dest)
            continue
        else:
            raise RuntimeError(f"unrecognized video entry shape: {json.dumps(video)[:500]}")
        saved.append(dest)
    return saved


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="text prompt describing the desired video / motion")
    parser.add_argument("--image", help="local path to a first-frame image (image-to-video)")
    parser.add_argument("--last-frame", help="local path to a last-frame image (first/last-frame interpolation)")
    parser.add_argument("-o", "--output", default="output.mp4", help="output video path (default: output.mp4)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Veo model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--duration", type=int, default=4, help="clip length in seconds (default: 4, cheapest)")
    parser.add_argument("--aspect-ratio", default="9:16", choices=["9:16", "16:9"], help="default: 9:16 (portrait/TikTok)")
    parser.add_argument("--resolution", choices=["720p", "1080p", "4k"], default=None, help="Veo 3.x only; default: model default (720p)")
    parser.add_argument("--sample-count", type=int, default=1, help="number of video variants to generate (1-4)")
    parser.add_argument("--negative-prompt", default=None)
    parser.add_argument("--person-generation", default="allow_adult", choices=["dont_allow", "allow_adult", "allowAll"])
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--resize-mode", choices=["crop", "pad"], default=None, help="how to fit an input image that isn't 9:16/16:9 (API default: pad)")
    parser.add_argument("--audio", action=argparse.BooleanOptionalAction, default=False, help="generate an audio track (default: off, matches the video-only cost table)")
    parser.add_argument("--storage-uri", default=os.environ.get("VIDEO_GEN_STORAGE_URI"), help="gs:// prefix to store output instead of returning bytes inline")
    parser.add_argument("--force", action="store_true", help="overwrite --output if it already exists (default: refuse)")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument("--credentials", default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    parser.add_argument("--poll-interval", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_env_file(Path.cwd() / ".env")
    load_env_file(Path.home() / ".video-gen" / ".env")
    args = parse_args(argv)

    if not args.project:
        print("error: --project or GOOGLE_CLOUD_PROJECT is required", file=sys.stderr)
        return 2
    if not args.credentials:
        print("error: --credentials or GOOGLE_APPLICATION_CREDENTIALS is required", file=sys.stderr)
        return 2

    output = Path(args.output)
    if not args.force:
        existing = [p for p in expected_output_paths(output, args.sample_count) if p.exists()]
        if existing:
            print(
                f"error: {', '.join(str(p) for p in existing)} already exist(s); pass --force to overwrite",
                file=sys.stderr,
            )
            return 2

    try:
        validate_duration(args.model, args.duration)
        body = build_request_body(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"requesting access token via service account {args.credentials} ...", file=sys.stderr)
    token = get_access_token(args.credentials)

    print(f"submitting {args.model} generation ({args.duration}s, {args.aspect_ratio}) ...", file=sys.stderr)
    operation_name = submit_generation(args.project, args.location, args.model, token, body)
    print(f"operation: {operation_name}", file=sys.stderr)

    payload = poll_operation(
        args.project, args.location, args.model, token,
        operation_name, args.poll_interval, args.timeout,
    )

    try:
        saved = save_videos(payload, output, sample_count=args.sample_count, force=args.force)
    except (RuntimeError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for path in saved:
        print(str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
