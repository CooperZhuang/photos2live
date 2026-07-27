"""实况照片 (Live Photo) 生成 + 导入「照片」App。

实况照片不是普通视频,是「静态图 + 配对视频」靠一个 UUID 绑定:
  静态图 JPEG: EXIF MakerApple 第 17 号键 = UUID
  配对 MOV:    com.apple.quicktime.content.identifier = 同一个 UUID
               外加一条 com.apple.quicktime.still-image-time 元数据轨

这些字段是从本机图库里真实 iPhone 实况照片上逆向确认的。写元数据轨必须用
AVAssetWriter (ffmpeg 做不到),所以有个配套的 Swift 小工具 swift/livephoto。
"""

from __future__ import annotations

import shutil
import subprocess
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path

from .sources import Photo

HELPER_SRC = Path(__file__).resolve().parent.parent / "swift" / "livephoto.swift"
HELPER_BIN = HELPER_SRC.with_suffix("")
DEFAULT_LIVE_SECONDS = 3.0
STILL_CHOICES = ("first", "middle", "last")


class LivePhotoError(RuntimeError):
    """实况照片生成失败。"""


@dataclass(frozen=True)
class LiveResult:
    still: Path
    video: Path
    uuid: str
    imported: bool = False


def ensure_helper(rebuild: bool = False) -> Path:
    """按需编译 Swift 小工具。源码比二进制新时自动重编。"""
    if not HELPER_SRC.exists():
        raise LivePhotoError(f"找不到 Swift 源码 {HELPER_SRC}")
    fresh = (HELPER_BIN.exists()
             and HELPER_BIN.stat().st_mtime >= HELPER_SRC.stat().st_mtime)
    if fresh and not rebuild:
        return HELPER_BIN
    if not shutil.which("swiftc"):
        raise LivePhotoError("没有 swiftc,装一下 Xcode Command Line Tools: xcode-select --install")
    out = subprocess.run(
        ["swiftc", "-O", str(HELPER_SRC), "-o", str(HELPER_BIN)],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not HELPER_BIN.exists():
        raise LivePhotoError(f"编译 Swift 小工具失败:\n{out.stderr[-500:]}")
    return HELPER_BIN


def pick_still(photos: list[Photo], choice: str) -> Photo:
    """选静态图。choice 可以是 first/middle/last 或具体文件名。"""
    if choice == "first":
        return photos[0]
    if choice == "last":
        return photos[-1]
    if choice == "middle":
        return photos[len(photos) // 2]
    for p in photos:
        if p.name == choice or Path(p.name).stem == choice:
            return p
    raise LivePhotoError(
        f"--live-still {choice!r} 不在照片序列里 (也可以用 {'/'.join(STILL_CHOICES)})"
    )


def live_fps(count: int, seconds: float = DEFAULT_LIVE_SECONDS) -> int:
    """实况照片的帧率:让每张照片正好占 1 帧,总时长最接近目标。"""
    if count <= 0:
        raise LivePhotoError("照片数必须 > 0")
    if seconds <= 0:
        raise LivePhotoError("实况时长必须 > 0")
    return max(1, min(240, round(count / seconds)))


def make_still(src: Path, dst: Path, width: int, height: int) -> Path:
    """把静态图缩放成和视频完全一致的尺寸 —— 尺寸不一致 Photos 可能不认配对。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
         "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width}:{height}",
         "-q:v", "2", "-frames:v", "1", str(dst)],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not dst.exists():
        raise LivePhotoError(f"生成静态图失败: {out.stderr.strip()[:200]}")
    return dst


def pair(
    still_src: Path,
    video_src: Path,
    out_dir: str | Path,
    *,
    width: int,
    height: int,
    still_time: float = 0.0,
    content_uuid: str | None = None,
) -> LiveResult:
    """把普通视频 + 一张照片打包成实况照片配对文件。

    静态图由 Swift helper 的 ImageIO 缩放完成(保留原图完整 EXIF),不经过 ffmpeg。
    """
    helper = ensure_helper()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cid = content_uuid or str(uuidlib.uuid4()).upper()

    cmd = [str(helper),
           "--still", str(still_src),
           "--video", str(video_src),
           "--out-dir", str(out_dir),
           "--uuid", cid,
           "--still-time", f"{still_time:.3f}",
           "--width", str(width),
           "--height", str(height)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise LivePhotoError(f"打包实况照片失败:\n{out.stderr.strip()[:400]}")

    parsed = dict(
        line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
    )
    still = Path(parsed.get("still", ""))
    video = Path(parsed.get("video", ""))
    if not still.exists() or not video.exists():
        raise LivePhotoError(f"小工具没产出配对文件:\n{out.stdout}")
    return LiveResult(still=still, video=video, uuid=parsed.get("uuid", cid))


def delete_from_library(names: list[str], timeout: int = 300) -> int:
    """把指定文件名的照片移入「照片」App 的最近删除（30 天内可恢复）。

    返回实际移入最近删除的数量。
    """
    if not names:
        return 0
    names_as = "{" + ", ".join(f'"{n}"' for n in names) + "}"
    script = f"""with timeout of {timeout} seconds
tell application "Photos"
    set toDelete to {{}}
    set nameList to {names_as}
    repeat with fname in nameList
        set found to (every media item whose filename is fname)
        set toDelete to toDelete & found
    end repeat
    if toDelete is not {{}} then
        delete toDelete
    end if
    return count of toDelete
end tell
end timeout"""
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if out.returncode != 0:
        raise LivePhotoError(f"删除原图失败: {out.stderr.strip()[:300]}")
    return int(out.stdout.strip() or "0")


def import_to_photos(result: LiveResult, timeout: int = 600) -> LiveResult:
    """用 AppleScript 让「照片」App 导入配对文件。

    走 Photos.app 自己的导入而不是 PHAssetCreationRequest —— 后者需要 TCC 授权,
    命令行二进制没有 app bundle 会被直接拒掉 (实测返回 denied 且不弹窗)。
    Photos.app 本来就有图库权限,把两个文件一起交给它,它会配对成 1 个实况照片。
    """
    script = f"""with timeout of {timeout} seconds
tell application "Photos"
 set f to {{POSIX file "{result.still}", POSIX file "{result.video}"}}
 set r to import f skip check duplicates true
 return count of r
end tell
end timeout"""
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if out.returncode != 0:
        raise LivePhotoError(
            f"导入「照片」App 失败: {out.stderr.strip()[:300]}\n"
            f"可以手动把这两个文件一起拖进「照片」App:\n"
            f"  {result.still}\n  {result.video}"
        )
    n = out.stdout.strip()
    if n != "1":
        raise LivePhotoError(
            f"导入返回 {n} 个项目 (期望 1 个配对好的实况照片)。"
            f"如果是 2,说明静态图和视频没配对上,检查 UUID 是否写入成功。"
        )
    return LiveResult(still=result.still, video=result.video, uuid=result.uuid, imported=True)
