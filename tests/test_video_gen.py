import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import video_gen


def make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        prompt="a cat reading a book",
        image=None,
        last_frame=None,
        output="output.mp4",
        model="veo-3.1-lite-generate-001",
        duration=4,
        aspect_ratio="9:16",
        resolution=None,
        sample_count=1,
        negative_prompt=None,
        person_generation="allow_adult",
        seed=None,
        resize_mode=None,
        storage_uri=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_build_request_body_text_only():
    body = video_gen.build_request_body(make_args())
    assert body["instances"] == [{"prompt": "a cat reading a book"}]
    assert body["parameters"]["durationSeconds"] == 4
    assert body["parameters"]["aspectRatio"] == "9:16"
    assert body["parameters"]["sampleCount"] == 1
    assert "storageUri" not in body["parameters"]


def test_build_request_body_with_image(tmp_path):
    png = tmp_path / "frame.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    body = video_gen.build_request_body(make_args(image=str(png)))
    image = body["instances"][0]["image"]
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["bytesBase64Encoded"]) == png.read_bytes()


def test_build_request_body_optional_params():
    body = video_gen.build_request_body(
        make_args(
            negative_prompt="blurry",
            seed=42,
            resolution="1080p",
            resize_mode="pad",
            storage_uri="gs://bucket/out/",
        )
    )
    params = body["parameters"]
    assert params["negativePrompt"] == "blurry"
    assert params["seed"] == 42
    assert params["resolution"] == "1080p"
    assert params["resizeMode"] == "pad"
    assert params["storageUri"] == "gs://bucket/out/"


def test_encode_image_rejects_unsupported_type(tmp_path):
    bad = tmp_path / "frame.gif"
    bad.write_bytes(b"GIF89a")
    with pytest.raises(ValueError, match="unsupported image type"):
        video_gen.encode_image(bad)


def test_encode_image_rejects_oversized(tmp_path, monkeypatch):
    big = tmp_path / "frame.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"0" * (21 * 1024 * 1024))
    with pytest.raises(ValueError, match="exceeds Veo's 20 MB"):
        video_gen.encode_image(big)


def test_validate_duration_rejects_unsupported_value():
    with pytest.raises(ValueError, match="does not support duration"):
        video_gen.validate_duration("veo-3.1-lite-generate-001", 5)


def test_validate_duration_accepts_supported_value():
    video_gen.validate_duration("veo-3.1-lite-generate-001", 4)


def test_save_videos_inline_bytes(tmp_path):
    payload = {
        "done": True,
        "response": {
            "raiMediaFilteredCount": 0,
            "videos": [{"bytesBase64Encoded": base64.b64encode(b"fakevideo").decode(), "mimeType": "video/mp4"}],
        },
    }
    out = tmp_path / "output.mp4"
    saved = video_gen.save_videos(payload, out)
    assert saved == [out]
    assert out.read_bytes() == b"fakevideo"


def test_save_videos_gcs_uri(tmp_path):
    payload = {
        "done": True,
        "response": {
            "raiMediaFilteredCount": 0,
            "videos": [{"gcsUri": "gs://bucket/out/sample_0.mp4", "mimeType": "video/mp4"}],
        },
    }
    out = tmp_path / "output.mp4"
    saved = video_gen.save_videos(payload, out)
    assert saved[0].read_text() == "gs://bucket/out/sample_0.mp4"


def test_save_videos_raises_on_rai_filtering():
    payload = {
        "done": True,
        "response": {"raiMediaFilteredCount": 1, "raiMediaFilteredReasons": ["policy"]},
    }
    with pytest.raises(RuntimeError, match="filtered by responsible-AI"):
        video_gen.save_videos(payload, Path("output.mp4"))


def test_save_videos_raises_on_empty_response():
    with pytest.raises(RuntimeError, match="no response payload"):
        video_gen.save_videos({"done": True}, Path("output.mp4"))


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('GOOGLE_CLOUD_PROJECT="from-file"\n')
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    video_gen.load_env_file(env_file)
    assert __import__("os").environ["GOOGLE_CLOUD_PROJECT"] == "from-file"
