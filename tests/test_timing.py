import pytest

from photos2live.timing import TimingError, allocate, load_durations


def names(n, start=1001222, prefix="P", ext=".JPG"):
    return [f"{prefix}{start + i}{ext}" for i in range(n)]


class TestPhotoFps:
    def test_matching_fps_is_one_frame_each(self):
        a = allocate(104, fps=30, photo_fps=30)
        assert a.frames == (1,) * 104
        assert a.is_uniform

    def test_default_is_12_photos_per_second(self):
        a = allocate(120, fps=24)  # 默认 photo_fps=12 -> 每张 2 帧
        assert a.frames == (2,) * 120
        assert a.total_seconds == pytest.approx(10.0)

    def test_non_divisible_spreads_remainder(self):
        # 12 张/秒 @ 30fps -> 每张 2.5 帧,应该 3,2,3,2... 而不是全 2 或全 3
        a = allocate(104, fps=30, photo_fps=12)
        assert a.total_frames == 260
        assert set(a.frames) == {2, 3}
        assert not a.is_uniform

    def test_too_fast_falls_back_to_one_frame(self):
        a = allocate(10, fps=24, photo_fps=1000)
        assert a.frames == (1,) * 10
        assert any("不足 1 帧" in w for w in a.warnings)


class TestPerPhoto:
    def test_uniform(self):
        a = allocate(50, fps=30, per_photo=0.5)
        assert a.frames == (15,) * 50
        assert a.total_seconds == pytest.approx(25.0)

    def test_no_cumulative_drift(self):
        # 逐张 round() 会漂移;Bresenham 不会
        a = allocate(100, fps=30, per_photo=0.1)
        assert a.total_frames == 300


class TestTotal:
    def test_exact_total(self):
        a = allocate(104, fps=30, total=10.0)
        assert a.total_frames == 300
        assert a.total_seconds == pytest.approx(10.0)

    def test_spreads_remainder(self):
        a = allocate(104, fps=30, total=10.0)
        assert set(a.frames) == {2, 3}
        assert a.frames.count(3) == 300 - 104 * 2

    def test_deterministic(self):
        assert allocate(104, fps=30, total=10.0) == allocate(104, fps=30, total=10.0)

    def test_too_short_gives_min_frames(self):
        a = allocate(104, fps=30, total=1.0)
        assert a.frames == (1,) * 104
        assert any("放不下" in w for w in a.warnings)


class TestOverrides:
    def test_per_photo_mode_override_is_local(self):
        # per_photo 模式下 override 只替换自己,不挤占别人
        nm = names(5)
        a = allocate(5, fps=30, per_photo=0.1, overrides={nm[2]: 2.0}, names=nm)
        assert a.frames == (3, 3, 60, 3, 3)
        assert not a.warnings

    def test_total_mode_override_eats_budget(self):
        nm = names(5)
        a = allocate(5, fps=30, total=4.0, overrides={nm[0]: 2.0}, names=nm)
        assert a.total_frames == 120
        assert a.frames[0] == 60
        assert sum(a.frames[1:]) == 60

    def test_total_mode_override_exceeds_budget(self):
        nm = names(2)
        a = allocate(2, fps=30, total=1.0, overrides={nm[0]: 5.0, nm[1]: 5.0}, names=nm)
        assert a.total_frames == 300
        assert any("不一致" in w for w in a.warnings)

    def test_unknown_names_warn(self):
        nm = names(3)
        a = allocate(3, fps=30, per_photo=1.0, overrides={"NOPE.JPG": 2.0}, names=nm)
        assert a.frames == (30, 30, 30)
        assert any("不在照片序列中" in w for w in a.warnings)

    def test_override_without_names_errors(self):
        with pytest.raises(TimingError, match="names"):
            allocate(3, fps=30, overrides={"a": 1.0})


class TestValidation:
    def test_mutually_exclusive(self):
        with pytest.raises(TimingError, match="互斥"):
            allocate(10, photo_fps=12, total=5.0)

    def test_zero_photos(self):
        with pytest.raises(TimingError, match="照片数"):
            allocate(0)

    def test_bad_values(self):
        with pytest.raises(TimingError):
            allocate(10, fps=0)
        with pytest.raises(TimingError):
            allocate(10, total=-1)


class TestLoadDurations:
    def _write(self, tmp_path, body):
        p = tmp_path / "d.csv"
        p.write_text(body, encoding="utf-8")
        return p

    def test_basic(self, tmp_path):
        p = self._write(tmp_path, "# 注释\n\nP1001222.JPG,2.5\nP1001223.JPG, 1\n")
        assert load_durations(p) == {"P1001222.JPG": 2.5, "P1001223.JPG": 1.0}

    def test_bad_number(self, tmp_path):
        p = self._write(tmp_path, "P1.JPG,abc\n")
        with pytest.raises(TimingError, match="不是数字"):
            load_durations(p)

    def test_empty(self, tmp_path):
        p = self._write(tmp_path, "# 全是注释\n")
        with pytest.raises(TimingError, match="没有有效"):
            load_durations(p)
