"""预处理:把大照片并行缩放到目标分辨率,带缓存。

为什么要单独一步:直接把 6000x4000 的原图喂给合成用的 ffmpeg,解码+缩放
是单进程串行的,会成为瓶颈。先并行缩放成目标尺寸的中间帧,合成阶段就只是
拼接,快很多;而且缓存命中后重复渲染几乎是瞬时的。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .sources import Photo

PRESETS: dict[str, tuple[int, int]] = {
    "4k": (3840, 2160),
    "2k": (2560, 1440),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
}
FIT_MODES = ("cover", "contain", "blur", "native")
SOURCE_RESOLUTION = "source"  # 不缩放,直接用源图原始尺寸


class PrepareError(RuntimeError):
    """缩放/探测失败。"""


def _even(n: int) -> int:
    """yuv420p 要求宽高都是偶数。"""
    return int(n) // 2 * 2


def _jpeg_orientation(path: Path) -> int:
    """读 JPEG EXIF Orientation tag (0x0112)。读不到返回 1(无旋转)。"""
    try:
        with open(path, "rb") as f:
            data = f.read(65536)
        i = 2  # 跳过 SOI
        while i < len(data) - 4:
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
                tiff = data[i + 10:]
                bo = "little" if tiff[:2] == b"II" else "big"
                ifd_off = int.from_bytes(tiff[4:8], bo)
                n = int.from_bytes(tiff[ifd_off:ifd_off + 2], bo)
                for j in range(n):
                    entry = tiff[ifd_off + 2 + j * 12: ifd_off + 14 + j * 12]
                    if int.from_bytes(entry[:2], bo) == 0x0112:
                        return int.from_bytes(entry[8:10], bo)
                break
            i += 2 + seg_len
    except Exception:
        pass
    return 1


def probe_size(path: Path) -> tuple[int, int]:
    """返回 (宽, 高),已考虑 EXIF 旋转,给出实际显示尺寸。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise PrepareError(f"读不出图片尺寸: {path.name} ({out.stderr.strip()[:120]})")
    try:
        stream = json.loads(out.stdout)["streams"][0]
        w, h = int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, ValueError) as exc:
        raise PrepareError(f"读不出图片尺寸: {path.name}") from exc

    # JPEG EXIF Orientation 5-8 含 90°旋转,显示时宽高互换
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg", ".heic", ".heif"):
        if _jpeg_orientation(path) in (5, 6, 7, 8):
            w, h = h, w
    return w, h


def resolve_size(
    resolution: str, fit: str, source: tuple[int, int]
) -> tuple[int, int]:
    """算出最终输出尺寸。

    resolution='source' 时直接用源图尺寸(只做 yuv420p 偶数对齐,不缩放不裁切)。
    native fit 模式沿用源宽高比,只对齐预设的宽度。
    """
    if resolution.lower() == SOURCE_RESOLUTION:
        sw, sh = source
        return _even(sw), _even(sh)

    if resolution.lower() in PRESETS:
        tw, th = PRESETS[resolution.lower()]
    elif "x" in resolution.lower():
        try:
            a, b = resolution.lower().split("x", 1)
            tw, th = int(a), int(b)
        except ValueError as exc:
            raise PrepareError(f"分辨率格式应为 WxH 或 {'/'.join(PRESETS)}: {resolution!r}") from exc
    else:
        raise PrepareError(
            f"未知分辨率 {resolution!r},可用: source / {', '.join(PRESETS)} / 1920x1080"
        )
    if tw <= 0 or th <= 0:
        raise PrepareError(f"分辨率必须为正: {resolution!r}")

    if fit == "native":
        sw, sh = source
        # 按长边对齐:取预设较大边作为长边目标,短边按比例推导,横竖图均适用
        max_side = max(tw, th)
        if sw >= sh:  # 横向源图,宽是长边
            return _even(max_side), _even(round(max_side * sh / sw))
        else:  # 竖向源图,高是长边
            return _even(round(max_side * sw / sh)), _even(max_side)
    return _even(tw), _even(th)


def fit_filter(fit: str, w: int, h: int) -> str:
    """生成把任意尺寸源图铺到 w x h 的 ffmpeg 滤镜链。"""
    if fit not in FIT_MODES:
        raise PrepareError(f"未知 --fit {fit!r},可用: {', '.join(FIT_MODES)}")
    if fit == "cover":
        # 等比放大到刚好盖满,再居中裁掉多余 —— 无黑边,会切掉边缘
        return (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={w}:{h}")
    if fit == "native":
        # w×h 已是长边对齐、保持源比例的目标尺寸,直接精确缩放,无裁切无黑边
        return f"scale={w}:{h}:flags=lanczos"
    if fit == "contain":
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
    # blur:模糊放大版做背景,原图完整居中叠上去
    return (
        f"split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase:flags=fast_bilinear,"
        f"crop={w}:{h},gblur=sigma=30[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
    )


@dataclass(frozen=True)
class Prepared:
    """缩放结果:frames[i] 对应第 i 张照片的中间帧文件。"""

    frames: tuple[Path, ...]
    width: int
    height: int
    cached: int
    scaled: int


def _cache_key(path: Path, w: int, h: int, fit: str, quality: int) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{w}x{h}|{fit}|q{quality}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _scale_one(src: Path, dst: Path, vf: str, quality: int) -> None:
    tmp = dst.with_suffix(".part.jpg")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src)]
    cmd += ["-filter_complex" if "[" in vf else "-vf", vf]
    cmd += ["-q:v", str(quality), "-frames:v", "1", "-color_range", "1", str(tmp)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise PrepareError(f"缩放失败 {src.name}: {out.stderr.strip()[:200]}")
    tmp.replace(dst)  # 原子替换,中断后不会留下半张图当缓存


def prepare(
    photos: list[Photo],
    cache_dir: str | Path,
    *,
    resolution: str = SOURCE_RESOLUTION,
    fit: str = "cover",
    quality: int = 2,
    workers: int | None = None,
    on_progress=None,
) -> Prepared:
    """并行把照片缩放成统一尺寸的中间帧。返回顺序与 photos 一致。"""
    if not photos:
        raise PrepareError("没有照片可处理")
    if not 1 <= quality <= 31:
        raise PrepareError(f"--quality 应在 1-31 之间 (1 最好): {quality}")

    w, h = resolve_size(resolution, fit, probe_size(photos[0].path))
    # source 模式:不缩放不裁切,只做 yuv420p 像素格式对齐
    if resolution.lower() == SOURCE_RESOLUTION:
        vf = "format=yuv420p"
    else:
        vf = fit_filter(fit, w, h)
    root = Path(cache_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    targets = [root / f"{_cache_key(p.path, w, h, fit, quality)}.jpg" for p in photos]
    todo = [(p.path, d) for p, d in zip(photos, targets) if not d.exists()]
    cached = len(photos) - len(todo)

    done = 0
    if todo:
        # ffmpeg 是子进程,瓶颈在它自己,线程池够用且开销更小
        with ThreadPoolExecutor(max_workers=workers or min(12, (len(todo) or 1))) as pool:
            futures = [pool.submit(_scale_one, s, d, vf, quality) for s, d in todo]
            for f in futures:
                f.result()  # 有异常就抛出来,不静默跳过
                done += 1
                if on_progress:
                    on_progress(done, len(todo))

    return Prepared(frames=tuple(targets), width=w, height=h, cached=cached, scaled=done)


def clear_cache(cache_dir: str | Path) -> int:
    """清掉中间帧缓存,返回删除的文件数。"""
    root = Path(cache_dir).expanduser()
    if not root.is_dir():
        return 0
    n = sum(1 for _ in root.glob("*.jpg"))
    shutil.rmtree(root)
    return n
