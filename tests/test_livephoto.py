from pathlib import Path

import pytest

from photos2live.livephoto import LivePhotoError, live_fps, pick_still
from photos2live.sources import Photo


def photos(n=5, start=1001222):
    return [Photo(name=f"P{start + i}.JPG", path=Path(f"/tmp/P{start + i}.JPG")) for i in range(n)]


class TestLiveFps:
    def test_each_photo_gets_one_frame(self):
        # 104 张塞进 3 秒 -> 35fps,每张正好 1 帧
        assert live_fps(104, 3.0) == 35

    def test_short_sequence(self):
        assert live_fps(30, 3.0) == 10

    def test_longer_duration_lowers_fps(self):
        assert live_fps(104, 10.0) == 10

    def test_clamped_to_sane_range(self):
        assert live_fps(10000, 1.0) == 240  # 上限
        assert live_fps(1, 60.0) == 1  # 下限,不会变 0

    def test_rejects_bad_input(self):
        with pytest.raises(LivePhotoError, match="照片数"):
            live_fps(0, 3.0)
        with pytest.raises(LivePhotoError, match="时长"):
            live_fps(10, 0)


class TestPickStill:
    def test_keywords(self):
        ps = photos(5)
        assert pick_still(ps, "first").name == "P1001222.JPG"
        assert pick_still(ps, "last").name == "P1001226.JPG"
        assert pick_still(ps, "middle").name == "P1001224.JPG"

    def test_explicit_filename(self):
        ps = photos(5)
        assert pick_still(ps, "P1001225.JPG").name == "P1001225.JPG"

    def test_filename_without_extension(self):
        assert pick_still(photos(5), "P1001225").name == "P1001225.JPG"

    def test_unknown_name(self):
        with pytest.raises(LivePhotoError, match="不在照片序列"):
            pick_still(photos(5), "P9999999.JPG")


class TestHelperBuild:
    def test_helper_compiles(self):
        from photos2live.livephoto import ensure_helper

        binary = ensure_helper()
        assert binary.exists() and binary.stat().st_mode & 0o111
