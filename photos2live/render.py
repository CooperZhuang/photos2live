"""合成:把中间帧按分配的帧数拼成视频。"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .timing import Allocation


TAIL_PAD = 1.0
"""清单末尾的余量秒数,配合 -frames:v 保证帧精确。见 build_manifest。"""


class RenderError(RuntimeError):
    """合成失败。"""


@dataclass(frozen=True)
class RenderPlan:
    cmd: list[str]
    manifest: str | None
    total_frames: int
    fps: int
    seconds: float

    def pretty(self) -> str:
        return " ".join(shlex.quote(c) for c in self.cmd)


def build_manifest(frames: list[Path], alloc: Allocation) -> str:
    """concat demuxer 清单。

    demuxer 每张照片只吐 1 个 packet,末尾那个 packet 的时长不足以撑到分配的帧数,
    -r 补帧就会少 1 帧甚至更多 (实测 300 帧只出 299 帧)。做法是末尾再追加一份
    最后一张、给足 TAIL_PAD 秒余量,多出来的由 build_plan 里的 -frames:v 截掉 ——
    这样 8 种时长组合实测都是帧精确的。
    """
    if len(frames) != len(alloc.frames):
        raise RenderError(f"帧数不匹配: {len(frames)} 个文件 vs {len(alloc.frames)} 份时长")
    lines = ["ffconcat version 1.0"]
    for path, n in zip(frames, alloc.frames):
        lines.append(f"file {shlex.quote(str(path.resolve()))}")
        lines.append(f"duration {n / alloc.fps:.6f}")
    lines.append(f"file {shlex.quote(str(frames[-1].resolve()))}")
    lines.append(f"duration {TAIL_PAD:.6f}")
    return "\n".join(lines) + "\n"


def _video_filters(alloc: Allocation, deflicker: int, extra: str | None,
                   color_range: int = 2) -> list[str]:
    # 不要在这里加 fps= 滤镜:清单里的 duration 已经是 1/fps 的整数倍,
    # 再过一遍 fps 重采样反而会丢帧 (实测 30 帧变 29 帧)。输出帧率靠 -r 指定。
    chain: list[str] = []
    if deflicker:
        chain.append(f"deflicker=size={deflicker}:mode=pm")
    if extra:
        chain.append(extra)
    # scale=iw:ih 是无损尺寸透传；in_range/out_range 明确声明颜色范围，
    # 避免 swscaler 对 yuvj420p 中间帧报 "deprecated pixel format" 警告。
    rng = "full" if color_range == 2 else "limited"
    chain.append(f"scale=iw:ih:in_range={rng}:out_range={rng},format=yuv420p")
    return chain


def build_plan(
    frames: list[Path],
    alloc: Allocation,
    output: str | Path,
    *,
    manifest_path: str | Path | None = None,
    crf: int = 18,
    preset: str = "medium",
    codec: str = "h265",
    hw: bool = True,
    deflicker: int = 0,
    extra_vf: str | None = None,
    audio: str | Path | None = None,
    color_range: int = 2,
) -> RenderPlan:
    """拼出完整 ffmpeg 命令(不执行)。"""
    if not frames:
        raise RenderError("没有帧可合成")
    if not 0 <= crf <= 51:
        raise RenderError(f"--crf 应在 0-51 之间: {crf}")

    manifest = build_manifest(frames, alloc)
    mpath = Path(manifest_path) if manifest_path else Path(output).with_suffix(".concat.txt")

    if codec not in ("h264", "h265"):
        raise RenderError(f"--codec 只支持 h264 / h265: {codec!r}")
    if hw:
        enc = "hevc_videotoolbox" if codec == "h265" else "h264_videotoolbox"
        qflags = ["-q:v", str(max(1, min(100, 100 - crf * 2)))]
    else:
        enc = "libx265" if codec == "h265" else "libx264"
        qflags = ["-crf", str(crf), "-preset", preset]

    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner",
           "-f", "concat", "-safe", "0", "-i", str(mpath)]
    if audio:
        cmd += ["-i", str(Path(audio).expanduser())]

    cmd += ["-vf", ",".join(_video_filters(alloc, deflicker, extra_vf, color_range))]
    # 精确到帧:concat 末尾会多带 1 帧,截断到分配的总帧数,总时长才和预期一致
    cmd += ["-frames:v", str(alloc.total_frames)]
    cmd += ["-c:v", enc, *qflags, "-r", str(alloc.fps)]
    if codec == "h265":
        cmd += ["-tag:v", "hvc1"]  # hev1(VideoToolbox 默认) → hvc1，Photos.app 才能配对
    cmd += ["-movflags", "+faststart", "-pix_fmt", "yuv420p", "-color_range", str(color_range)]

    if audio:
        fade = max(0.1, min(3.0, alloc.total_seconds / 10))
        start = max(0.0, alloc.total_seconds - fade)
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest",
                "-af", f"afade=t=out:st={start:.3f}:d={fade:.3f}"]
    else:
        cmd += ["-an"]
    cmd += [str(output)]

    return RenderPlan(cmd=cmd, manifest=manifest, total_frames=alloc.total_frames,
                      fps=alloc.fps, seconds=alloc.total_seconds)


def run(plan: RenderPlan, manifest_path: str | Path, *, quiet: bool = False) -> None:
    """写清单 + 执行。"""
    mpath = Path(manifest_path)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    if plan.manifest is not None:
        mpath.write_text(plan.manifest, encoding="utf-8")
    out = subprocess.run(plan.cmd, capture_output=quiet, text=True)
    if out.returncode != 0:
        detail = (out.stderr or "")[-600:] if quiet else "(见上方 ffmpeg 输出)"
        raise RenderError(f"ffmpeg 合成失败 (exit {out.returncode}): {detail}")


def probe_output(path: str | Path) -> dict[str, str]:
    """用 ffprobe 校验产物,返回 时长/帧数/分辨率/帧率。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames,width,height,r_frame_rate:format=duration",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RenderError(f"校验产物失败: {out.stderr.strip()[:200]}")
    info = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()
    return info
