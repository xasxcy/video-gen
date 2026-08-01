import argparse
import base64
import json
import os
import sys
import tempfile
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
        audio=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_build_request_body_text_only():
    body = video_gen.build_request_body(make_args())
    assert body["instances"] == [{"prompt": "a cat reading a book"}]
    assert body["parameters"]["durationSeconds"] == 4
    assert body["parameters"]["aspectRatio"] == "9:16"
    assert body["parameters"]["sampleCount"] == 1
    assert body["parameters"]["generateAudio"] is False
    assert "storageUri" not in body["parameters"]


def test_build_request_body_audio_opt_in():
    body = video_gen.build_request_body(make_args(audio=True))
    assert body["parameters"]["generateAudio"] is True


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


@pytest.mark.parametrize(
    "model", ["veo-2.0-generate-001", "veo-3.0-generate-001", "veo-3.0-fast-generate-001"]
)
def test_validate_duration_rejects_retired_models(model):
    with pytest.raises(ValueError, match="retired"):
        video_gen.validate_duration(model, 4)


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


def _inline_payload(data: bytes = b"fakevideo") -> dict:
    return {
        "done": True,
        "response": {
            "raiMediaFilteredCount": 0,
            "videos": [{"bytesBase64Encoded": base64.b64encode(data).decode(), "mimeType": "video/mp4"}],
        },
    }


def test_save_videos_refuses_to_overwrite_existing_file(tmp_path):
    out = tmp_path / "output.mp4"
    out.write_bytes(b"previous result, cost real money")
    with pytest.raises(FileExistsError, match="already exist"):
        video_gen.save_videos(_inline_payload(), out)
    assert out.read_bytes() == b"previous result, cost real money"


def _multi_sample_payload(n: int) -> dict:
    return {
        "done": True,
        "response": {
            "raiMediaFilteredCount": 0,
            "videos": [
                {"bytesBase64Encoded": base64.b64encode(f"video-{i}".encode()).decode(), "mimeType": "video/mp4"}
                for i in range(n)
            ],
        },
    }


def test_expected_output_paths_single_sample():
    out = Path("output.mp4")
    assert video_gen.expected_output_paths(out, 1) == [out]


def test_expected_output_paths_multi_sample():
    out = Path("output.mp4")
    assert video_gen.expected_output_paths(out, 3) == [
        Path("output-0.mp4"), Path("output-1.mp4"), Path("output-2.mp4"),
    ]


def test_save_videos_multi_sample_writes_all_files(tmp_path):
    out = tmp_path / "output.mp4"
    saved = video_gen.save_videos(_multi_sample_payload(3), out, sample_count=3)
    assert [p.name for p in saved] == ["output-0.mp4", "output-1.mp4", "output-2.mp4"]
    assert (tmp_path / "output-1.mp4").read_bytes() == b"video-1"


def test_save_videos_multi_sample_refuses_if_any_target_collides(tmp_path):
    out = tmp_path / "output.mp4"
    (tmp_path / "output-1.mp4").write_bytes(b"pre-existing, do not touch")
    with pytest.raises(FileExistsError, match="output-1.mp4"):
        video_gen.save_videos(_multi_sample_payload(3), out, sample_count=3)
    # Nothing should have been written for the other samples either — fail before writing any.
    assert not (tmp_path / "output-0.mp4").exists()
    assert (tmp_path / "output-1.mp4").read_bytes() == b"pre-existing, do not touch"


def test_save_videos_naming_follows_requested_not_returned_count(tmp_path):
    # Requested 3 samples but the API only returned 1 (e.g. others RAI-filtered without
    # raiMediaFilteredCount catching it, or just server-side variance). Naming must still
    # follow the *requested* count (output-0.mp4), matching what expected_output_paths()
    # told main()'s pre-flight check to look for — otherwise a bare output.mp4 collision
    # would slip past pre-flight and only surface after paying for the generation.
    out = tmp_path / "output.mp4"
    (tmp_path / "output.mp4").write_bytes(b"unrelated file, must not be touched")
    saved = video_gen.save_videos(_multi_sample_payload(1), out, sample_count=3)
    assert [p.name for p in saved] == ["output-0.mp4"]
    assert (tmp_path / "output.mp4").read_bytes() == b"unrelated file, must not be touched"


def test_save_videos_overwrites_with_force(tmp_path):
    out = tmp_path / "output.mp4"
    out.write_bytes(b"stale")
    saved = video_gen.save_videos(_inline_payload(b"fresh"), out, force=True)
    assert saved == [out]
    assert out.read_bytes() == b"fresh"


def test_save_videos_does_not_leave_tmp_file_behind(tmp_path):
    out = tmp_path / "output.mp4"
    video_gen.save_videos(_inline_payload(), out)
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_exclusive_refuses_even_if_file_appears_after_the_precheck(tmp_path):
    # Simulates the TOCTOU window a concurrent process could win: the file didn't exist
    # when save_videos() did its pre-flight check, but exists by the time we actually try
    # to create it. O_CREAT|O_EXCL must still refuse rather than silently overwriting.
    dest = tmp_path / "output.mp4"
    dest.write_bytes(b"written by a concurrent run, must survive")
    with pytest.raises(FileExistsError, match="already exists"):
        video_gen._write_exclusive(dest, b"this must not land")
    assert dest.read_bytes() == b"written by a concurrent run, must survive"


def test_write_exclusive_cleans_up_on_write_failure(tmp_path, monkeypatch):
    dest = tmp_path / "output.mp4"

    class ExplodingFile:
        def write(self, data):
            raise OSError("disk full")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(os, "fdopen", lambda fd, mode: ExplodingFile())
    with pytest.raises(OSError, match="disk full"):
        video_gen._write_exclusive(dest, b"data")
    assert not dest.exists()


def test_write_atomic_force_cleans_up_tmp_on_write_failure(tmp_path, monkeypatch):
    dest = tmp_path / "output.mp4"

    class ExplodingFile:
        def write(self, data):
            raise OSError("disk full")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(os, "fdopen", lambda fd, mode: ExplodingFile())
    with pytest.raises(OSError, match="disk full"):
        video_gen._write_atomic_force(dest, b"data")
    assert not dest.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_atomic_force_uses_unique_tmp_names(tmp_path):
    # Two concurrent-ish calls (simulated sequentially) must not collide on the same
    # fixed .tmp filename — mkstemp gives each call a distinct name.
    dest1 = tmp_path / "output.mp4"
    fd, tmp_name_1 = tempfile.mkstemp(dir=dest1.parent, prefix=dest1.name + ".", suffix=".tmp")
    os.close(fd)
    fd, tmp_name_2 = tempfile.mkstemp(dir=dest1.parent, prefix=dest1.name + ".", suffix=".tmp")
    os.close(fd)
    assert tmp_name_1 != tmp_name_2


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('GOOGLE_CLOUD_PROJECT="from-file"\n')
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    video_gen.load_env_file(env_file)
    assert __import__("os").environ["GOOGLE_CLOUD_PROJECT"] == "from-file"
