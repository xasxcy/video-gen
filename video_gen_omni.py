#!/usr/bin/env python3
"""CLI for generating videos with Gemini Omni Flash (Interactions API).

与 video_gen.py（Veo，走 predictLongRunning）是完全独立的脚本：Omni Flash 走
Interactions API，请求体结构、能力边界都不一样，只共用 _auth.py 里的鉴权逻辑。
"""

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

from _auth import SCOPES, get_access_token, load_env_file

DEFAULT_MODEL = os.environ.get("VIDEO_GEN_OMNI_MODEL", "gemini-omni-flash-preview")

# Omni Flash 固定挂在 locations/global 下，与 Veo 默认的 us-central1 不同 —— 用独立的环境
# 变量名，避免和 video_gen.py 共用的 GOOGLE_CLOUD_LOCATION（默认 us-central1）互相污染。
DEFAULT_LOCATION = os.environ.get("VIDEO_GEN_OMNI_LOCATION", "global")

MIN_DURATION = 3
MAX_DURATION = 10


def guess_mime_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime not in ("image/png", "image/jpeg"):
        raise ValueError(
            f"unsupported image type for {path} (Omni Flash only accepts image/png or image/jpeg)"
        )
    return mime


def encode_image(path: Path) -> dict:
    data = path.read_bytes()
    return {
        "type": "image",
        "mime_type": guess_mime_type(path),
        "data": base64.b64encode(data).decode("ascii"),
    }


def validate_duration(duration: int) -> None:
    if not (MIN_DURATION <= duration <= MAX_DURATION):
        raise ValueError(
            f"--duration={duration} out of range: Omni Flash only supports {MIN_DURATION}-{MAX_DURATION} seconds"
        )


def build_interaction_body(args: argparse.Namespace) -> dict:
    validate_duration(args.duration)

    input_items: list[dict] = [{"type": "text", "text": args.prompt}]
    task = "text_to_video"
    if args.image:
        input_items.append(encode_image(Path(args.image)))
        task = "image_to_video"

    response_format: dict = {
        "type": "video",
        "duration": f"{args.duration}s",
    }
    if args.aspect_ratio:
        response_format["aspect_ratio"] = args.aspect_ratio

    # 步骤5已实测确认：不给 gcs_uri 时 delivery="inline" 是正确取值（真实调用返回了内联
    # base64 视频数据，见 tests/fixtures/omni/sync_completed_response.json）。delivery="uri"
    # 分支未被真实调用覆盖（未消耗额外费用测试该分支），仍按调研笔记推断实现。
    if args.storage_uri:
        response_format["delivery"] = "uri"
        response_format["gcs_uri"] = args.storage_uri
    else:
        response_format["delivery"] = "inline"

    body: dict = {
        "model": args.model,
        "input": input_items,
        "response_format": [response_format],
        "generation_config": {"video_config": {"task": task}},
    }
    if args.background:
        body["background"] = True
    return body


def _safe_error_message(resp: requests.Response) -> str:
    """截取错误信息用于异常/日志，禁止把完整响应体拼进去（Omni 响应体可能带内联视频数据）。"""
    try:
        data = resp.json()
    except ValueError:
        return f"<non-JSON response, {len(resp.content)} bytes>"
    message = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
        if message is None:
            message = data.get("message")
    if message is None:
        return f"<no error/message field found, {len(resp.content)} bytes>"
    return str(message)[:500]


def submit_interaction(project: str, location: str, token: str, body: dict) -> dict:
    url = f"https://aiplatform.googleapis.com/v1beta1/projects/{project}/locations/{location}/interactions"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=json.dumps(body),
            timeout=60,
        )
    except requests.exceptions.RequestException as exc:
        # A network-level failure here (timeout, connection reset) doesn't tell us whether
        # the server ever received/accepted the request — it may already be running (and
        # billing) as an interaction we have no id for, so --interaction can't recover it.
        # Don't retry blindly (risks a duplicate paid submission); surface the uncertainty
        # instead so the caller can check the Vertex AI console before deciding what to do.
        raise RuntimeError(
            f"interactions.create request failed before a response was received ({exc}); "
            "submission outcome is unknown — check the Vertex AI Interactions console for "
            "this project/location before retrying, an interaction may already be running "
            "and billing without a captured id"
        ) from exc
    if not resp.ok:
        raise RuntimeError(f"interactions.create failed ({resp.status_code}): {_safe_error_message(resp)}")
    result = resp.json()

    # 步骤5已实测确认：真实响应（同步/异步提交/轮询完成态，共三次真实调用）里始终没有顶层
    # "name" 字段，只有 "id"（不带 "projects/..." 前缀的短字符串）。用 "id" 拼出资源路径
    # projects/{project}/locations/{location}/interactions/{id} 这个构造方式已用它去调
    # 真实的 GET https://aiplatform.googleapis.com/v1beta1/{interaction_name} 端点验证成功
    # （HTTP 200，返回 completed 状态与视频数据）。这里的 "name" 兜底逻辑保留是为了兼容万一
    # API 未来直接返回 "name" 字段的情况。
    interaction_name = result.get("name")
    if not interaction_name:
        interaction_id = result.get("id")
        if interaction_id:
            interaction_name = f"projects/{project}/locations/{location}/interactions/{interaction_id}"
    if not interaction_name:
        raise RuntimeError("interactions.create succeeded but no interaction name/id found in response")

    # 提交成功后立刻打印资源名 —— 这是 --interaction 恢复模式的前提，不能等轮询/保存完才打印。
    print(f"interaction: {interaction_name}", file=sys.stderr)
    result.setdefault("name", interaction_name)
    return result


def poll_interaction(
    project: str,
    location: str,
    token: str,
    interaction_name: str,
    poll_interval: int,
    timeout: int,
) -> dict:
    url = f"https://aiplatform.googleapis.com/v1beta1/{interaction_name}"
    deadline = time.monotonic() + timeout
    while True:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        if not resp.ok:
            raise RuntimeError(f"interactions.get failed ({resp.status_code}): {_safe_error_message(resp)}")
        payload = resp.json()
        status = payload.get("status")
        if status == "completed":
            return payload
        if status == "failed":
            error = payload.get("error")
            message = None
            if isinstance(error, dict):
                message = error.get("message")
            raise RuntimeError(f"interaction {interaction_name} failed: {message or '<no error message>'}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"interaction {interaction_name} did not finish within {timeout}s")
        print(f"  ...status={status}, polling again in {poll_interval}s", file=sys.stderr)
        time.sleep(poll_interval)


def _write_exclusive(dest: Path, data: bytes | str, *, text: bool = False) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w" if text else "wb") as f:
            f.write(data)
        try:
            os.link(tmp, dest)
        except FileExistsError:
            raise FileExistsError(
                f"{dest} already exists; pass --force to overwrite (a completed generation costs "
                "real money, so this tool refuses to silently clobber a prior result)"
            ) from None
    finally:
        tmp.unlink(missing_ok=True)


def _write_atomic_force(dest: Path, data: bytes | str, *, text: bool = False) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w" if text else "wb") as f:
            f.write(data)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# 步骤5已实测确认：steps 数组里 type=="model_output" 的 step、其 content 数组里
# type=="video" 的条目、以及内联视频用 "data" (base64) + "mime_type": "video/mp4" 表达，
# 这些都用三次真实调用（同步 completed、异步 submit、异步 poll completed）验证过，
# 见 tests/fixtures/omni/*.json。异步完成态额外带了一个 type=="user_input" 的 step
# （同步响应里没有），当前实现按 type=="model_output" 过滤，不受影响。
# "uri" 分支（gcs_uri 投递）未被真实调用覆盖，仍按调研笔记推断实现。
def save_video(payload: dict, output: Path, force: bool = False) -> Path:
    steps = payload.get("steps")
    if not steps:
        raise RuntimeError(f"interaction has no steps in response (top-level keys: {list(payload.keys())})")

    content_items: list[dict] = []
    for step in steps:
        if step.get("type") == "model_output":
            content_items.extend(step.get("content", []))

    video_item = None
    for item in content_items:
        if item.get("type") == "video":
            video_item = item
            break
    if video_item is None:
        raise RuntimeError("no video content found in interaction's model_output step")

    if not force and output.exists():
        raise FileExistsError(f"{output} already exists; pass --force to overwrite")

    if "uri" in video_item:
        if force:
            _write_atomic_force(output, video_item["uri"], text=True)
        else:
            _write_exclusive(output, video_item["uri"], text=True)
        print(
            f"video stored in Cloud Storage, wrote its URI to {output} "
            f"(download separately with: gcloud storage cp '{video_item['uri']}' <local-path>)",
            file=sys.stderr,
        )
        return output

    if "data" in video_item:
        data = base64.b64decode(video_item["data"])
        if force:
            _write_atomic_force(output, data)
        else:
            _write_exclusive(output, data)
        return output

    raise RuntimeError(f"unrecognized video content shape, keys: {list(video_item.keys())}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="text prompt describing the desired video / motion")
    parser.add_argument("--image", help="local path to an image (base64-inlined, image-to-video)")
    parser.add_argument("-o", "--output", default="output.mp4", help="output video path (default: output.mp4)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Omni Flash model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--duration", type=int, default=3, help="clip length in seconds, 3-10 (default: 3, cheapest)")
    parser.add_argument("--aspect-ratio", default="9:16", choices=["9:16", "16:9"], help="default: 9:16 (portrait/TikTok)")
    parser.add_argument("--storage-uri", default=os.environ.get("VIDEO_GEN_STORAGE_URI"), help="gs:// prefix to store output instead of returning bytes inline")
    parser.add_argument("--force", action="store_true", help="overwrite --output if it already exists (default: refuse)")
    parser.add_argument("--background", action="store_true", help="submit asynchronously instead of waiting inline (result kept 14 days)")
    parser.add_argument("--interaction", default=None, help="resume mode: an interaction resource name from a prior --background submission, skip submit and poll/fetch its result directly")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--location", default=DEFAULT_LOCATION, help=f"default: {DEFAULT_LOCATION}")
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
    if not args.force and output.exists():
        print(f"error: {output} already exists; pass --force to overwrite", file=sys.stderr)
        return 2

    print(f"requesting access token via service account {args.credentials} ...", file=sys.stderr)
    token = get_access_token(args.credentials)

    if args.interaction:
        interaction_name = args.interaction
    else:
        try:
            body = build_interaction_body(args)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        print(f"submitting {args.model} interaction ({args.duration}s, {args.aspect_ratio}) ...", file=sys.stderr)
        result = submit_interaction(args.project, args.location, token, body)

        if not args.background:
            # 同步模式：submit_interaction 拿到的就是完成态结果，无需再轮询。
            try:
                saved = save_video(result, output, force=args.force)
            except (RuntimeError, FileExistsError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(str(saved))
            return 0

        # --background 是"异步提交"：submit_interaction 已经打印了 interaction 资源名，这里
        # 直接返回，不在本进程里阻塞轮询——否则和文档承诺的"instead of waiting inline"矛盾，
        # 也会让网络中断/本地超时表现得像提交失败。真正等结果用 --interaction 恢复模式。
        print(
            f"submitted for background processing (result kept 14 days); "
            f"fetch it later with: --interaction {result['name']} -o {output}",
            file=sys.stderr,
        )
        return 0

    # 只有 --interaction 恢复模式会走到这里：拿一个已提交任务的资源名，等它跑完再保存。
    payload = poll_interaction(
        args.project, args.location, token,
        interaction_name, args.poll_interval, args.timeout,
    )

    try:
        saved = save_video(payload, output, force=args.force)
    except (RuntimeError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(str(saved))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
