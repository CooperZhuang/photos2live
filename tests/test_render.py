from pathlib import Path

import pytest

from photos2live.render import TAIL_PAD, RenderError, build_manifest, build_plan
from photos2live.timing import Allocation, allocate


def frames(n):
    return [Path(f"/tmp/f{i}.jpg") for i in range(n)]


class TestManifest:
    def test_durations_are_frame_multiples(self):
        alloc = Allocation(frames=(2, 3), fps=30)
        text = build_manifest(frames(2), alloc)
        assert "duration 0.066667" in text  # 2/30
        assert "duration 0.100000" in text  # 3/30

    def test_tail_duplicate_present(self):
        text = build_manifest(frames(3), Allocation(frames=(1, 1, 1), fps=30))
        lines = [l for l in text.splitlines() if l.startswith("file ")]
        assert len(lines) == 4, "末尾要多一份最后一张做余量"
        assert lines[-1] == lines[-2]
        assert f"duration {TAIL_PAD:.6f}" in text

    def test_quotes_paths_with_spaces(self):
        text = build_manifest([Path("/tmp/a b.jpg")], Allocation(frames=(1,), fps=30))
        assert "'" in text and "a b.jpg'" in text  # /tmp 在 macOS 上会解析成 /private/tmp

    def test_length_mismatch(self):
        with pytest.raises(RenderError, match="帧数不匹配"):
            build_manifest(frames(2), Allocation(frames=(1, 1, 1), fps=30))


class TestBuildPlan:
    def _plan(self, **kw):
        return build_plan(frames(4), allocate(4, fps=30, per_photo=1.0), "out.mp4", **kw)

    def test_caps_frames_exactly(self):
        p = self._plan()
        assert p.cmd[p.cmd.index("-frames:v") + 1] == "120"
        assert p.total_frames == 120

    def test_no_fps_filter(self):
        vf = self._plan().cmd
        chain = vf[vf.index("-vf") + 1]
        assert "fps=" not in chain, "fps 滤镜会丢帧,不该出现"
        assert chain.endswith("format=yuv420p")

    def test_default_is_compatible_h264(self):
        c = self._plan().cmd
        assert c[c.index("-c:v") + 1] == "libx264"
        assert "-crf" in c and "+faststart" in c and "-an" in c

    def test_h265_gets_hvc1_tag(self):
        c = self._plan(codec="h265").cmd
        assert c[c.index("-c:v") + 1] == "libx265"
        assert c[c.index("-tag:v") + 1] == "hvc1"

    def test_hw_switches_encoder_and_drops_crf(self):
        c = self._plan(hw=True).cmd
        assert c[c.index("-c:v") + 1] == "h264_videotoolbox"
        assert "-crf" not in c and "-q:v" in c

    def test_deflicker_in_chain(self):
        c = self._plan(deflicker=5).cmd
        assert "deflicker=size=5" in c[c.index("-vf") + 1]

    def test_audio_adds_fade_and_shortest(self):
        c = self._plan(audio="bgm.mp3").cmd
        assert "-shortest" in c and "-an" not in c
        assert "afade=t=out" in c[c.index("-af") + 1]

    def test_rejects_bad_params(self):
        with pytest.raises(RenderError, match="crf"):
            self._plan(crf=99)
        with pytest.raises(RenderError, match="codec"):
            self._plan(codec="av1")
