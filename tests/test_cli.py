import pytest

from photos2live.cli import build_parser, main
from photos2live.prepare import SOURCE_RESOLUTION, resolve_size


def mkphotos(tmp_path, n=4, start=1001222):
    d = tmp_path / "pics"
    d.mkdir()
    for i in range(n):
        (d / f"P{start + i}.JPG").write_bytes(b"x")
    return d


class TestParser:
    def test_defaults(self):
        a = build_parser().parse_args(["-o", "x.mp4"])
        # resolution 默认 source(不缩放); fit 默认留空,由运行时按模式解析
        assert (a.fps, a.resolution, a.fit, a.codec, a.crf) == (30, SOURCE_RESOLUTION, None, "h264", 18)
        assert a.deflicker == 0 and a.source == "auto"

    def test_source_resolution_returns_source_size(self):
        """resolution='source' 时 resolve_size 直接返回源图尺寸(偶数对齐)。"""
        assert resolve_size(SOURCE_RESOLUTION, "cover", (6000, 4000)) == (6000, 4000)
        assert resolve_size(SOURCE_RESOLUTION, "native", (6001, 4001)) == (6000, 4000)  # 向下取偶

    def test_native_fit_aligns_to_long_side(self):
        """native fit 按长边对齐预设,横竖图均保持原比例,无裁切。"""
        # 竖向源图 4000×6000(用户实际情况); 1080p 长边=1920,竖图高=1920
        assert resolve_size("4k",    "native", (4000, 6000)) == (2560, 3840)
        assert resolve_size("1080p", "native", (4000, 6000)) == (1280, 1920)
        # 横向源图 6000×4000; 1080p 长边=1920,横图宽=1920
        assert resolve_size("4k",    "native", (6000, 4000)) == (3840, 2560)
        assert resolve_size("1080p", "native", (6000, 4000)) == (1920, 1280)

    def test_live_keeps_source_size_by_default(self):
        """实况不显式给 -r 时,默认 resolution=source,输出源图原始尺寸,不缩放不裁切。"""
        a = build_parser().parse_args(["-o", "x.mov", "--live-photo"])
        assert a.fit is None
        assert a.resolution == SOURCE_RESOLUTION
        assert resolve_size(a.resolution, "native", (6000, 4000)) == (6000, 4000)
        # 显式给了 -r 1080p + --fit cover 才缩放
        b = build_parser().parse_args(["-o", "x.mov", "--live-photo", "-r", "1080p", "--fit", "cover"])
        assert b.resolution == "1080p" and b.fit == "cover"
        assert resolve_size(b.resolution, "cover", (6000, 4000)) == (1920, 1080)

    def test_deflicker_optional_value(self):
        p = build_parser()
        assert p.parse_args(["-o", "x", "--deflicker"]).deflicker == 5
        assert p.parse_args(["-o", "x", "--deflicker", "9"]).deflicker == 9

    def test_preview_optional_value(self):
        p = build_parser()
        assert p.parse_args(["-o", "x", "--preview"]).preview == 24
        assert p.parse_args(["-o", "x", "--preview", "8"]).preview == 8

    def test_bad_choices_rejected(self):
        for bad in (["--fit", "nope"], ["--codec", "av1"], ["--source", "magic"]):
            with pytest.raises(SystemExit):
                build_parser().parse_args(["-o", "x.mp4", *bad])


class TestMain:
    def test_auto_output_name(self, tmp_path, capsys):
        """不给 -o 时自动按起止文件名生成输出路径。"""
        d = mkphotos(tmp_path)
        code = main(["--input-dir", str(d), "--per-photo", "1", "--dry-run"])
        out = capsys.readouterr().out
        assert code == 0
        # 起止文件名 P1001222-P1001225
        assert "P1001222-P1001225" in out

    def test_output_dir_generates_name(self, tmp_path, capsys):
        """给了目录作 -o 时,在目录里生成起止文件名。"""
        d = mkphotos(tmp_path)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        code = main(["--input-dir", str(d), "-o", str(out_dir),
                     "--per-photo", "1", "--dry-run"])
        assert code == 0

    def test_dry_run_prints_command(self, tmp_path, capsys):
        d = mkphotos(tmp_path)
        code = main(["--input-dir", str(d), "-o", str(tmp_path / "o.mp4"),
                     "--per-photo", "2", "--dry-run"])
        out = capsys.readouterr().out
        assert code == 0
        assert "照片 4 张" in out and "8.00s / 240 帧" in out
        assert "ffmpeg -nostdin" in out and "-frames:v 240" in out
        assert not (tmp_path / "o.mp4").exists()

    def test_mutually_exclusive_timing_errors(self, tmp_path, capsys):
        d = mkphotos(tmp_path)
        code = main(["--input-dir", str(d), "-o", str(tmp_path / "o.mp4"),
                     "--total", "5", "--photo-fps", "12", "--dry-run"])
        assert code == 1
        assert "互斥" in capsys.readouterr().err

    def test_missing_dir_errors_cleanly(self, tmp_path, capsys):
        code = main(["--input-dir", str(tmp_path / "nope"), "-o", str(tmp_path / "o.mp4"),
                     "--dry-run"])
        assert code == 1 and "不存在" in capsys.readouterr().err

    def test_library_source_needs_range(self, tmp_path, capsys):
        code = main(["--source", "library", "-o", str(tmp_path / "o.mp4"), "--dry-run"])
        assert code == 1 and "--range" in capsys.readouterr().err

    def test_preview_limits_count(self, tmp_path, capsys):
        d = mkphotos(tmp_path, n=10)
        main(["--input-dir", str(d), "-o", str(tmp_path / "o.mp4"), "--preview", "3",
              "--per-photo", "1", "--dry-run"])
        out = capsys.readouterr().out
        assert "只用前 3 张" in out and "照片 3 张" in out

    def test_durations_file_applies(self, tmp_path, capsys):
        d = mkphotos(tmp_path, n=3)
        csv = tmp_path / "d.csv"
        csv.write_text("P1001223.JPG,5\n", encoding="utf-8")
        main(["--input-dir", str(d), "-o", str(tmp_path / "o.mp4"), "--per-photo", "1",
              "--durations", str(csv), "--dry-run"])
        # 2 张 x 1s + 1 张 x 5s = 7s
        assert "7.00s / 210 帧" in capsys.readouterr().out
