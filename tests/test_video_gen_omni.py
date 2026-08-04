import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import video_gen_omni

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "omni"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        prompt="a cat reading a book",
        image=None,
        output="output.mp4",
        model="gemini-omni-flash-preview",
        duration=3,
        aspect_ratio="9:16",
        storage_uri=None,
        background=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# build_interaction_body
# ---------------------------------------------------------------------------


def test_build_interaction_body_text_only():
    body = video_gen_omni.build_interaction_body(make_args())
    assert body["model"] == "gemini-omni-flash-preview"
    assert body["input"] == [{"type": "text", "text": "a cat reading a book"}]
    assert body["response_format"][0]["type"] == "video"
    assert body["response_format"][0]["duration"] == "3s"
    assert body["response_format"][0]["aspect_ratio"] == "9:16"
    assert body["response_format"][0]["delivery"] == "inline"
    assert "gcs_uri" not in body["response_format"][0]
    assert body["generation_config"]["video_config"]["task"] == "text_to_video"
    assert "background" not in body


def test_build_interaction_body_with_image(tmp_path):
    png = tmp_path / "frame.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    body = video_gen_omni.build_interaction_body(make_args(image=str(png)))
    assert len(body["input"]) == 2
    image_item = body["input"][1]
    assert image_item["type"] == "image"
    assert image_item["mime_type"] == "image/png"
    assert base64.b64decode(image_item["data"]) == png.read_bytes()
    assert body["generation_config"]["video_config"]["task"] == "image_to_video"


def test_build_interaction_body_storage_uri_sets_delivery_uri():
    body = video_gen_omni.build_interaction_body(make_args(storage_uri="gs://bucket/out/"))
    rf = body["response_format"][0]
    assert rf["delivery"] == "uri"
    assert rf["gcs_uri"] == "gs://bucket/out/"


def test_build_interaction_body_background_flag():
    body = video_gen_omni.build_interaction_body(make_args(background=True))
    assert body["background"] is True


def test_build_interaction_body_rejects_bad_image_type(tmp_path):
    bad = tmp_path / "frame.gif"
    bad.write_bytes(b"GIF89a")
    with pytest.raises(ValueError, match="unsupported image type"):
        video_gen_omni.build_interaction_body(make_args(image=str(bad)))


# ---------------------------------------------------------------------------
# validate_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("duration", [0, 1, 2, 11, 20])
def test_validate_duration_rejects_out_of_range(duration):
    with pytest.raises(ValueError, match="out of range"):
        video_gen_omni.validate_duration(duration)


@pytest.mark.parametrize("duration", [3, 5, 10])
def test_validate_duration_accepts_in_range(duration):
    video_gen_omni.validate_duration(duration)


def test_build_interaction_body_propagates_duration_validation():
    with pytest.raises(ValueError, match="out of range"):
        video_gen_omni.build_interaction_body(make_args(duration=99))


# ---------------------------------------------------------------------------
# save_video
# ---------------------------------------------------------------------------


def _payload_with_video(video_item: dict) -> dict:
    return {
        "status": "completed",
        "steps": [
            {"type": "model_output", "content": [video_item]},
        ],
    }


def test_save_video_gcs_uri(tmp_path):
    payload = _payload_with_video({"type": "video", "uri": "gs://bucket/out/sample.mp4"})
    out = tmp_path / "output.mp4"
    saved = video_gen_omni.save_video(payload, out)
    assert saved == out
    assert out.read_text() == "gs://bucket/out/sample.mp4"


def test_save_video_inline_base64(tmp_path):
    payload = _payload_with_video(
        {"type": "video", "mime_type": "video/mp4", "data": base64.b64encode(b"fakevideo").decode()}
    )
    out = tmp_path / "output.mp4"
    saved = video_gen_omni.save_video(payload, out)
    assert saved == out
    assert out.read_bytes() == b"fakevideo"


def test_save_video_raises_when_no_steps():
    with pytest.raises(RuntimeError, match="no steps"):
        video_gen_omni.save_video({"status": "completed"}, Path("output.mp4"))


def test_save_video_raises_when_no_video_content():
    payload = {
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "some model commentary"}]}],
    }
    with pytest.raises(RuntimeError, match="no video content"):
        video_gen_omni.save_video(payload, Path("output.mp4"))


def test_save_video_refuses_to_overwrite_existing_file(tmp_path):
    out = tmp_path / "output.mp4"
    out.write_bytes(b"previous result, cost real money")
    payload = _payload_with_video({"type": "video", "uri": "gs://bucket/out/sample.mp4"})
    with pytest.raises(FileExistsError, match="already exists"):
        video_gen_omni.save_video(payload, out)
    assert out.read_bytes() == b"previous result, cost real money"


def test_save_video_overwrites_with_force(tmp_path):
    out = tmp_path / "output.mp4"
    out.write_bytes(b"stale")
    payload = _payload_with_video(
        {"type": "video", "mime_type": "video/mp4", "data": base64.b64encode(b"fresh").decode()}
    )
    saved = video_gen_omni.save_video(payload, out, force=True)
    assert saved == out
    assert out.read_bytes() == b"fresh"


# ---------------------------------------------------------------------------
# submit_interaction
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, ok=True, content=b""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.ok = ok
        self.content = content or b"{}"
        self.text = self.content.decode("utf-8", errors="replace")

    def json(self):
        return self._json_data


def test_submit_interaction_prints_name_immediately(monkeypatch, capsys):
    def fake_post(url, headers=None, data=None, timeout=None):
        return _FakeResponse(json_data={"id": "abc123", "status": "completed"})

    monkeypatch.setattr(video_gen_omni.requests, "post", fake_post)
    result = video_gen_omni.submit_interaction("proj", "global", "tok", {"model": "m"})
    assert result["name"] == "projects/proj/locations/global/interactions/abc123"
    captured = capsys.readouterr()
    assert "interaction: projects/proj/locations/global/interactions/abc123" in captured.err


def test_submit_interaction_uses_name_field_if_present(monkeypatch):
    def fake_post(url, headers=None, data=None, timeout=None):
        return _FakeResponse(
            json_data={"name": "projects/proj/locations/global/interactions/xyz", "status": "completed"}
        )

    monkeypatch.setattr(video_gen_omni.requests, "post", fake_post)
    result = video_gen_omni.submit_interaction("proj", "global", "tok", {"model": "m"})
    assert result["name"] == "projects/proj/locations/global/interactions/xyz"


def test_submit_interaction_error_does_not_leak_full_body(monkeypatch):
    huge_body = ("x" * 50000).encode()

    def fake_post(url, headers=None, data=None, timeout=None):
        return _FakeResponse(status_code=500, ok=False, content=huge_body, json_data={})

    monkeypatch.setattr(video_gen_omni.requests, "post", fake_post)
    with pytest.raises(RuntimeError) as exc_info:
        video_gen_omni.submit_interaction("proj", "global", "tok", {"model": "m"})
    message = str(exc_info.value)
    assert "500" in message
    # Must not contain the huge raw body verbatim.
    assert "x" * 50000 not in message
    assert len(message) < 1000


def test_submit_interaction_error_extracts_message_field(monkeypatch):
    def fake_post(url, headers=None, data=None, timeout=None):
        return _FakeResponse(
            status_code=400,
            ok=False,
            content=b'{"error": {"message": "invalid prompt"}}',
            json_data={"error": {"message": "invalid prompt"}},
        )

    monkeypatch.setattr(video_gen_omni.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="invalid prompt"):
        video_gen_omni.submit_interaction("proj", "global", "tok", {"model": "m"})


# ---------------------------------------------------------------------------
# poll_interaction
# ---------------------------------------------------------------------------


def test_poll_interaction_completed(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(json_data={"status": "completed", "steps": []})

    monkeypatch.setattr(video_gen_omni.requests, "get", fake_get)
    payload = video_gen_omni.poll_interaction(
        "proj", "global", "tok", "projects/proj/locations/global/interactions/abc", 1, 60
    )
    assert payload["status"] == "completed"


def test_poll_interaction_failed(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(json_data={"status": "failed", "error": {"message": "generation failed"}})

    monkeypatch.setattr(video_gen_omni.requests, "get", fake_get)
    with pytest.raises(RuntimeError, match="generation failed"):
        video_gen_omni.poll_interaction(
            "proj", "global", "tok", "projects/proj/locations/global/interactions/abc", 1, 60
        )


def test_poll_interaction_times_out(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse(json_data={"status": "in_progress"})

    sleep_calls = []
    monkeypatch.setattr(video_gen_omni.requests, "get", fake_get)
    monkeypatch.setattr(video_gen_omni.time, "sleep", lambda s: sleep_calls.append(s))

    # monotonic() called once to compute deadline, then repeatedly to check against it;
    # force it to blow past the deadline on the very first status check.
    times = iter([0, 100, 100, 100, 100])
    monkeypatch.setattr(video_gen_omni.time, "monotonic", lambda: next(times, 100))

    with pytest.raises(TimeoutError, match="did not finish within"):
        video_gen_omni.poll_interaction(
            "proj", "global", "tok", "projects/proj/locations/global/interactions/abc", 1, 10
        )
    assert calls["n"] >= 1


def test_poll_interaction_error_does_not_leak_full_body(monkeypatch):
    huge_body = ("y" * 50000).encode()

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(status_code=503, ok=False, content=huge_body, json_data={})

    monkeypatch.setattr(video_gen_omni.requests, "get", fake_get)
    with pytest.raises(RuntimeError) as exc_info:
        video_gen_omni.poll_interaction(
            "proj", "global", "tok", "projects/proj/locations/global/interactions/abc", 1, 60
        )
    message = str(exc_info.value)
    assert "503" in message
    assert "y" * 50000 not in message


# ---------------------------------------------------------------------------
# 基于步骤5真实调用产出的脱敏 fixture 的测试（tests/fixtures/omni/）
#
# 这些 fixture 来自三次真实的 Interactions API 调用（同步 completed、异步 submit、异步
# poll completed），视频内容已替换成占位 base64/uri，但顶层结构、字段名、step 类型均保留
# 真实形状。用于确保测试基于已核实的真实响应，而不是凭空编造的 mock 结构。
# ---------------------------------------------------------------------------


def test_save_video_matches_real_sync_completed_fixture(tmp_path):
    payload = load_fixture("sync_completed_response.json")
    assert payload["status"] == "completed"
    assert "name" not in payload or payload["name"]  # name 由 submit_interaction 补写，非 API 原生字段
    out = tmp_path / "output.mp4"
    saved = video_gen_omni.save_video(payload, out)
    assert saved == out
    # 真实响应里内联视频用 "data" (base64) 字段表达，不是 "uri"
    assert out.read_bytes() == base64.b64decode("RkFLRV9CQVNFNjRfUExBQ0VIT0xERVI=")


def test_save_video_matches_real_async_poll_completed_fixture(tmp_path):
    payload = load_fixture("async_poll_completed_response.json")
    assert payload["status"] == "completed"
    # 真实的异步完成态响应比同步响应多一个 type=="user_input" 的 step，
    # save_video 必须只挑 type=="model_output" 的 step，不受额外 step 影响。
    step_types = [s["type"] for s in payload["steps"]]
    assert "user_input" in step_types
    assert "model_output" in step_types
    out = tmp_path / "output.mp4"
    saved = video_gen_omni.save_video(payload, out)
    assert saved == out
    assert len(out.read_bytes()) > 0


def test_async_submit_fixture_has_no_name_field_only_id():
    """真实调用确认：interactions.create 的响应从不带顶层 "name" 字段，只有 "id"。
    submit_interaction 必须能从裸 "id" 构造出正确的资源路径（已用真实 GET 调用验证过该构造）。"""
    payload = load_fixture("async_submit_response.json")
    assert "name" not in payload
    assert payload["id"]
    assert payload["status"] == "in_progress"


# ---------------------------------------------------------------------------
# --interaction resume mode (main())
# ---------------------------------------------------------------------------


def test_main_interaction_resume_mode_skips_submit(monkeypatch, tmp_path):
    out = tmp_path / "output.mp4"

    monkeypatch.setattr(video_gen_omni, "load_env_file", lambda path: None)
    monkeypatch.setattr(video_gen_omni, "get_access_token", lambda creds: "tok")

    submit_called = []
    monkeypatch.setattr(
        video_gen_omni,
        "submit_interaction",
        lambda *a, **k: submit_called.append(1) or {},
    )

    poll_called = {}

    def fake_poll(project, location, token, interaction_name, poll_interval, timeout):
        poll_called["interaction_name"] = interaction_name
        return _payload_with_video({"type": "video", "uri": "gs://bucket/out/sample.mp4"})

    monkeypatch.setattr(video_gen_omni, "poll_interaction", fake_poll)

    rc = video_gen_omni.main(
        [
            "unused prompt",
            "--project", "proj",
            "--credentials", "creds.json",
            "--interaction", "projects/proj/locations/global/interactions/resume-me",
            "-o", str(out),
        ]
    )
    assert rc == 0
    assert submit_called == []
    assert poll_called["interaction_name"] == "projects/proj/locations/global/interactions/resume-me"
    assert out.read_text() == "gs://bucket/out/sample.mp4"


def test_main_requires_project(monkeypatch):
    # Avoid the repo's real .env (which sets GOOGLE_CLOUD_PROJECT) leaking into this check.
    monkeypatch.setattr(video_gen_omni, "load_env_file", lambda path: None)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    rc = video_gen_omni.main(["prompt", "--credentials", "creds.json"])
    assert rc == 2
