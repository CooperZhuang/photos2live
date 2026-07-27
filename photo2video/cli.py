"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .livephoto import (
    DEFAULT_LIVE_SECONDS,
    LivePhotoError,
    import_to_photos,
    live_fps,
    pair,
    pick_still,
)
from .prepare import (
    FIT_MODES,
    PRESETS,
    SOURCE_RESOLUTION,
    PrepareError,
    clear_cache,
    prepare,
    probe_size,
    resolve_size,
)
from .render import RenderError, build_plan, probe_output, run
from .sources import SourceError, from_directory, from_library, parse_range
from .timing import TimingError, allocate, load_durations

DEFAULT_CACHE = ".p2v-cache"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="photo2video",
        description="把有序照片合成延时/幻灯片视频",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  # 从「照片」App 直接读,12 张/秒的延时
  photo2video --range P1001222-P1001325 -o out/timelapse.mp4 --photo-fps 12

  # 幻灯片:每张 3 秒,1080p 模糊填充
  photo2video --input-dir ~/Desktop/pics -o out/slides.mp4 --per-photo 3 --fit blur

  # 卡总时长:整段正好 20 秒
  photo2video --range P1001222-325 -o out/v.mp4 --total 20

  # 先看清单和 ffmpeg 命令,不真跑
  photo2video --range P1001222-325 -o out/v.mp4 --total 20 --dry-run
""",
    )
    p.add_argument("--version", action="version", version=f"photo2video {__version__}")

    src = p.add_argument_group("照片来源")
    src.add_argument("--range", dest="range_spec", metavar="起始-结束",
                     help="文件名区间,如 P1001222-P1001325 (右端可简写 -325)")
    src.add_argument("--input-dir", metavar="目录", help="从文件夹读(自己导出好的照片)")
    src.add_argument("--library", metavar="路径", help="指定 .photoslibrary,默认自动找")
    src.add_argument("--source", choices=("auto", "library", "osxphotos", "dir"), default="auto",
                     help="来源方式,默认 auto:给了 --input-dir 用 dir,否则读照片库")

    t = p.add_argument_group("时长控制 (前三个互斥)")
    t.add_argument("--photo-fps", type=float, metavar="N", help="每秒放几张照片 (延时首选,默认 12)")
    t.add_argument("--per-photo", type=float, metavar="秒", help="每张照片显示几秒 (幻灯片首选)")
    t.add_argument("--total", type=float, metavar="秒", help="整段视频总时长,均分给所有照片")
    t.add_argument("--durations", metavar="CSV", help="逐张指定时长的清单 (每行:文件名,秒数)")
    t.add_argument("--fps", type=int, default=30, metavar="N", help="输出视频帧率,默认 30")

    v = p.add_argument_group("画面")
    v.add_argument("-r", "--resolution", default=SOURCE_RESOLUTION, metavar="规格",
                   help=f"source(默认,原图尺寸不缩放) / {'/'.join(PRESETS)} / 1920x1080")
    v.add_argument("--fit", choices=FIT_MODES, default=None,
                   help="native 保持原比例(实况默认) / cover 裁切铺满(普通视频默认)"
                        " / contain 加黑边 / blur 模糊填充")
    v.add_argument("--quality", type=int, default=2, metavar="1-31",
                   help="中间帧质量,1 最好,默认 2")
    v.add_argument("--deflicker", type=int, nargs="?", const=5, default=0, metavar="N",
                   help="消除延时摄影的亮度闪烁,N 是参与平均的帧数 (默认 5)")

    e = p.add_argument_group("编码")
    e.add_argument("-o", "--output", metavar="文件",
                   help="输出视频路径;不填则按起止文件名自动生成 (如 P1001222-P1001325.mov)")
    e.add_argument("--codec", choices=("h264", "h265"), default="h264", help="默认 h264,兼容性最好")
    e.add_argument("--crf", type=int, default=18, metavar="0-51", help="画质,越小越好,默认 18")
    e.add_argument("--preset", default="medium", help="libx264 preset,默认 medium")
    e.add_argument("--hw", action="store_true", help="用 VideoToolbox 硬件编码 (快,同码率画质略差)")
    e.add_argument("--audio", metavar="文件", help="背景音乐,自动裁到视频长度并淡出")

    lp = p.add_argument_group("实况照片 (Live Photo)")
    lp.add_argument("--live-photo", action="store_true",
                    help="生成 iPhone 实况照片(静态图 + 配对视频),不是普通视频")
    lp.add_argument("--live-still", default="first", metavar="哪张",
                    help="静态图取哪张: first(默认)/middle/last 或具体文件名")
    lp.add_argument("--live-duration", type=float, default=DEFAULT_LIVE_SECONDS, metavar="秒",
                    help=f"实况时长,默认 {DEFAULT_LIVE_SECONDS}s (iPhone 原生就是 3s 左右)")
    lp.add_argument("--live-split", type=int, default=0, metavar="N",
                    help="每 N 张照片生成一个实况照片(按顺序切分),默认不拆分")
    lp.add_argument("--import-to-photos", action="store_true",
                    help="生成后自动导入「照片」App(开了 iCloud 就会同步到 iPhone)")

    m = p.add_argument_group("其它")
    m.add_argument("--dry-run", action="store_true", help="只打印计划和 ffmpeg 命令,不执行")
    m.add_argument("--preview", type=int, nargs="?", const=24, metavar="N",
                   help="只取前 N 张 (默认 24) 快速出片验证效果")
    m.add_argument("--cache-dir", default=DEFAULT_CACHE, metavar="目录", help=f"中间帧缓存,默认 {DEFAULT_CACHE}")
    m.add_argument("--clear-cache", action="store_true", help="清空中间帧缓存后退出")
    m.add_argument("--workers", type=int, metavar="N", help="并行缩放线程数,默认自动")
    m.add_argument("-q", "--quiet", action="store_true", help="少输出")
    return p


def _collect(args) -> tuple[list, list[str]]:
    """按 --source 取照片。"""
    rng = parse_range(args.range_spec) if args.range_spec else None
    mode = args.source
    if mode == "auto":
        mode = "dir" if args.input_dir else "library"

    if mode == "dir":
        if not args.input_dir:
            raise SourceError("--source dir 需要同时给 --input-dir")
        return from_directory(args.input_dir, rng)
    if rng is None:
        raise SourceError("从照片库读取时必须给 --range (否则不知道要哪些照片)")
    if mode == "osxphotos":
        from .osxphotos_source import from_osxphotos  # 延迟导入:可选依赖

        return from_osxphotos(rng, library=args.library, dest=Path(args.cache_dir) / "export")
    return from_library(rng, library=args.library)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    def say(*a):
        if not args.quiet:
            print(*a)

    if args.clear_cache:
        n = clear_cache(args.cache_dir)
        say(f"已清空缓存 {args.cache_dir} ({n} 个中间帧)")
        if not args.output:
            return 0

    try:
        photos, warnings = _collect(args)
        if args.preview:
            photos = photos[: args.preview]
            say(f"预览模式:只用前 {len(photos)} 张")

        # 分组 (--live-split 仅在 --live-photo 时生效)
        split_n = args.live_split if args.live_photo and args.live_split > 0 else 0
        if split_n:
            chunks: list[list] = [photos[i:i + split_n]
                                   for i in range(0, len(photos), split_n)]
            say(f"拆分为 {len(chunks)} 组 (每组 {split_n} 张"
                f",最后一组 {len(chunks[-1])} 张)")
        else:
            chunks = [photos]

        fit = args.fit or ("native" if args.live_photo else "cover")
        if args.live_photo and args.fit is None:
            say("实况模式:fit=native,按长边对齐保持原比例(不裁切)")

        say(f"照片 {len(photos)} 张: {photos[0].name} ... {photos[-1].name}")

        # 确定输出目录 / 基础路径
        ext = ".mov" if args.live_photo else ".mp4"
        multi = len(chunks) > 1
        if args.output and not Path(args.output).is_dir():
            # 显式指定了具体文件:单组时用它,多组时用其父目录自动命名
            fixed_out: Path | None = Path(args.output) if not multi else None
            out_dir = Path(args.output).parent
        else:
            fixed_out = None
            out_dir = Path(args.output) if args.output else Path(".")
        out_dir.mkdir(parents=True, exist_ok=True)

        def chunk_out_path(chunk) -> Path:
            if fixed_out is not None:
                p = fixed_out
            else:
                first_stem = Path(chunk[0].name).stem
                last_stem  = Path(chunk[-1].name).stem
                p = out_dir / f"{first_stem}-{last_stem}{ext}"
            if args.live_photo and p.suffix.lower() != ".mov":
                p = p.with_suffix(".mov")
            return p

        if args.dry_run:
            for ci, chunk in enumerate(chunks):
                chunk_out = chunk_out_path(chunk)
                if multi:
                    say(f"\n=== 第 {ci + 1}/{len(chunks)} 组: "
                        f"{chunk[0].name} … {chunk[-1].name} ===")
                # 实况自动 fps
                g_fps, g_photo_fps = args.fps, args.photo_fps
                if args.live_photo and not any((args.photo_fps, args.per_photo, args.total)):
                    g_fps = g_photo_fps = live_fps(len(chunk), args.live_duration)
                    say(f"{len(chunk)} 张 / {args.live_duration}s -> {g_fps}fps")
                alloc = allocate(
                    len(chunk), fps=g_fps, photo_fps=g_photo_fps,
                    per_photo=args.per_photo, total=args.total,
                    overrides=load_durations(args.durations) if args.durations else None,
                    names=[p.name for p in chunk],
                )
                say(f"时长 {alloc.total_seconds:.2f}s / {alloc.total_frames} 帧 @ {g_fps}fps")
                manifest_path = chunk_out.with_suffix(".concat.txt")
                plan = build_plan(
                    [p.path for p in chunk], alloc, chunk_out,
                    manifest_path=manifest_path,
                    crf=args.crf, preset=args.preset, codec=args.codec,
                    hw=args.hw, deflicker=args.deflicker, audio=args.audio,
                )
                if args.resolution.lower() == SOURCE_RESOLUTION:
                    say("画面: source(源图原始尺寸,不缩放)")
                else:
                    w0, h0 = resolve_size(args.resolution, fit, probe_size(chunk[0].path))
                    say(f"画面: {args.resolution} -> {w0}x{h0} / fit={fit}")
                say(f"输出: {chunk_out}\nffmpeg 命令:\n{plan.pretty()}")
            say("\n(--dry-run:没有实际渲染)")
            return 0

        def progress(done, total):
            if not args.quiet:
                print(f"\r  缩放 {done}/{total}", end="", flush=True)

        # prepare 一次性对所有照片缩放(并行+缓存复用)
        pre = prepare(photos, args.cache_dir, resolution=args.resolution, fit=fit,
                      quality=args.quality, workers=args.workers, on_progress=progress)
        if not args.quiet and pre.scaled:
            print()
        say(f"画面 {pre.width}x{pre.height} fit={fit}"
            f" (新缩放 {pre.scaled} 张,缓存命中 {pre.cached} 张)")

        frame_offset = 0
        for ci, chunk in enumerate(chunks):
            chunk_frames = pre.frames[frame_offset: frame_offset + len(chunk)]
            frame_offset += len(chunk)

            chunk_out = chunk_out_path(chunk)
            chunk_out.parent.mkdir(parents=True, exist_ok=True)

            if multi:
                say(f"\n=== 第 {ci + 1}/{len(chunks)} 组: "
                    f"{chunk[0].name} … {chunk[-1].name} ===")

            # 每组独立的时长分配 (实况自动 fps 按本组张数计算)
            g_fps, g_photo_fps = args.fps, args.photo_fps
            if args.live_photo and not any((args.photo_fps, args.per_photo, args.total)):
                g_fps = g_photo_fps = live_fps(len(chunk), args.live_duration)
                say(f"{len(chunk)} 张 / {args.live_duration}s -> {g_fps}fps,每张 1 帧")

            overrides = load_durations(args.durations) if args.durations else None
            alloc = allocate(
                len(chunk), fps=g_fps, photo_fps=g_photo_fps,
                per_photo=args.per_photo, total=args.total,
                overrides=overrides, names=[p.name for p in chunk],
            )
            warnings += list(alloc.warnings)
            say(f"时长 {alloc.total_seconds:.2f}s / {alloc.total_frames} 帧 @ {g_fps}fps"
                f" (每张 {min(alloc.frames)}-{max(alloc.frames)} 帧)")

            manifest_path = chunk_out.with_suffix(".concat.txt")
            plan = build_plan(
                list(chunk_frames), alloc, chunk_out,
                manifest_path=manifest_path,
                crf=args.crf, preset=args.preset, codec=args.codec,
                hw=args.hw, deflicker=args.deflicker, audio=args.audio,
            )
            run(plan, manifest_path, quiet=args.quiet)

            info = probe_output(chunk_out)
            got  = int(info.get("nb_read_frames", 0))
            size_mb = chunk_out.stat().st_size / 1024 / 1024
            say(f"完成 {chunk_out.name} — {info.get('width')}x{info.get('height')}, "
                f"{got} 帧 / {float(info.get('duration', 0)):.2f}s, {size_mb:.1f} MB")
            if got != alloc.total_frames:
                warnings.append(
                    f"{chunk_out.name}: 实际帧数 {got} 与预期 {alloc.total_frames} 不一致")

            if args.live_photo:
                still_photo = pick_still(chunk, args.live_still)
                say(f"静态图取 {still_photo.name}")
                live_dir = chunk_out.parent / "live"
                res = pair(still_photo.path, chunk_out, live_dir,
                           width=pre.width, height=pre.height)
                say(f"实况配对完成 (uuid {res.uuid}):\n  {res.still}\n  {res.video}")
                if args.import_to_photos:
                    res = import_to_photos(res)
                    say("已导入「照片」App —— 开了 iCloud 照片就会同步到 iPhone")
                else:
                    say("导入方式:加 --import-to-photos 自动导入,"
                        "或把上面两个文件一起拖进「照片」App")

        for w in warnings:
            print(f"  ⚠ {w}", file=sys.stderr)
        return 0

    except (SourceError, TimingError, PrepareError, RenderError, LivePhotoError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
