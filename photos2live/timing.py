"""时长模型:把用户意图换算成"每张照片占多少输出帧"。

设计要点:
1. 一切以 **整数帧** 为单位,不给 ffmpeg 传浮点秒 —— 避免 concat demuxer
   量化导致某几张多/少一帧的抖动。
2. 三种互斥的总量表达 (photo_fps / per_photo / total) 统一先算出"目标总帧数",
   再用 Bresenham 均匀分摊到每张照片。这样总时长精确,且余数帧是散开的
   (3,3,3,2,3,3,3,2...) 而不是堆在开头。
3. overrides (逐张指定秒数) 的语义随模式变化:
   - total 模式:硬预算,override 的帧从总预算里扣。
   - per_photo / photo_fps 模式:每张独立,override 只替换自己。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MIN_FRAMES = 1


class TimingError(ValueError):
    """时长参数不合法。"""


@dataclass(frozen=True)
class Allocation:
    """分配结果。frames[i] 是第 i 张照片占的输出帧数。"""

    frames: tuple[int, ...]
    fps: int
    warnings: tuple[str, ...] = field(default=())

    @property
    def total_frames(self) -> int:
        return sum(self.frames)

    @property
    def total_seconds(self) -> float:
        return self.total_frames / self.fps

    @property
    def durations(self) -> tuple[float, ...]:
        return tuple(n / self.fps for n in self.frames)

    @property
    def is_uniform(self) -> bool:
        return len(set(self.frames)) <= 1


def _bresenham(target: int, count: int) -> list[int]:
    """把 target 帧均匀摊到 count 份,总和恰好 == target,余数散开。"""
    out = []
    prev = 0
    for i in range(1, count + 1):
        cur = target * i // count
        out.append(cur - prev)
        prev = cur
    return out


def allocate(
    count: int,
    *,
    fps: int = 30,
    photo_fps: float | None = None,
    per_photo: float | None = None,
    total: float | None = None,
    overrides: dict[str, float] | None = None,
    names: list[str] | None = None,
) -> Allocation:
    """把时长意图换算成每张照片的帧数。

    photo_fps / per_photo / total 三者互斥,都不给时默认 photo_fps=12。
    """
    if count <= 0:
        raise TimingError("照片数必须 > 0")
    if fps <= 0:
        raise TimingError("输出帧率必须 > 0")

    given = [k for k, v in (("photo-fps", photo_fps), ("per-photo", per_photo),
                            ("total", total)) if v is not None]
    if len(given) > 1:
        raise TimingError(f"--photo-fps / --per-photo / --total 互斥,不能同时给: {', '.join(given)}")
    if not given:
        photo_fps = 12.0

    for label, val in (("--photo-fps", photo_fps), ("--per-photo", per_photo), ("--total", total)):
        if val is not None and val <= 0:
            raise TimingError(f"{label} 必须 > 0")

    warnings: list[str] = []
    overrides = overrides or {}
    if overrides and names is None:
        raise TimingError("使用 overrides 时必须同时提供 names")
    if names is not None and len(names) != count:
        raise TimingError(f"names 长度 ({len(names)}) 与照片数 ({count}) 不一致")

    # 只保留真正命中序列的 override
    fixed: dict[int, int] = {}
    if overrides:
        assert names is not None
        hit = set()
        for i, nm in enumerate(names):
            if nm in overrides:
                fixed[i] = max(MIN_FRAMES, round(overrides[nm] * fps))
                hit.add(nm)
        unknown = sorted(set(overrides) - hit)
        if unknown:
            shown = ", ".join(unknown[:5]) + (" ..." if len(unknown) > 5 else "")
            warnings.append(f"逐张时长清单里有 {len(unknown)} 个文件名不在照片序列中,已忽略: {shown}")

    free = [i for i in range(count) if i not in fixed]
    frames = [0] * count
    for i, n in fixed.items():
        frames[i] = n

    if total is not None:
        target = max(MIN_FRAMES, round(total * fps))
        reserved = sum(fixed.values())
        remaining = target - reserved
        if not free:
            if remaining != 0:
                warnings.append(
                    f"逐张时长已覆盖全部照片,总时长由清单决定 "
                    f"({reserved / fps:.2f}s),与 --total {total:.2f}s 不一致"
                )
        elif remaining < len(free) * MIN_FRAMES:
            warnings.append(
                f"--total {total:.2f}s 放不下:扣掉逐张指定的 {reserved / fps:.2f}s 后 "
                f"剩余帧不够 {len(free)} 张照片各 1 帧,已按每张 1 帧处理"
            )
            for i in free:
                frames[i] = MIN_FRAMES
        else:
            for i, n in zip(free, _bresenham(remaining, len(free))):
                frames[i] = n
    else:
        # per_photo / photo_fps:每张独立,不共享预算
        base = per_photo if per_photo is not None else 1.0 / photo_fps  # type: ignore[operator]
        target = round(len(free) * base * fps)
        if free and target < len(free) * MIN_FRAMES:
            warnings.append(
                f"每张 {base * 1000:.1f}ms 在 {fps}fps 下不足 1 帧,已按每张 1 帧处理"
                f"(实际会比预期慢)"
            )
            for i in free:
                frames[i] = MIN_FRAMES
        elif free:
            for i, n in zip(free, _bresenham(target, len(free))):
                frames[i] = n

    return Allocation(frames=tuple(frames), fps=fps, warnings=tuple(warnings))


def load_durations(path: str | Path) -> dict[str, float]:
    """读逐张时长清单 (CSV: 文件名,秒数)。# 开头是注释。"""
    out: dict[str, float] = {}
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.replace("\t", ",").split(",") if p.strip()]
        if len(parts) < 2:
            raise TimingError(f"{path}:{lineno} 格式应为 `文件名,秒数`: {raw!r}")
        try:
            secs = float(parts[1])
        except ValueError as exc:
            raise TimingError(f"{path}:{lineno} 秒数不是数字: {parts[1]!r}") from exc
        if secs <= 0:
            raise TimingError(f"{path}:{lineno} 秒数必须 > 0: {secs}")
        out[parts[0]] = secs
    if not out:
        raise TimingError(f"{path} 里没有有效的时长记录")
    return out
