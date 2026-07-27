"""可选后端:用 osxphotos 导出。

比直连库慢(要复制文件),但走的是官方支持的路子,而且能拿到在「照片」App
里编辑过的版本。装法: uv add osxphotos
"""

from __future__ import annotations

from pathlib import Path

from .sources import Photo, Range, SourceError, _finish


def from_osxphotos(
    rng: Range, library: str | Path | None = None, dest: str | Path = ".p2v-cache/export"
) -> tuple[list[Photo], list[str]]:
    try:
        import osxphotos  # noqa: PLC0415
    except ImportError as exc:
        raise SourceError(
            "没装 osxphotos。装一下: uv add osxphotos  "
            "(或者不用它 —— 默认的 --source library 直连照片库更快)"
        ) from exc

    db = osxphotos.PhotosDB(str(library)) if library else osxphotos.PhotosDB()
    hits = [p for p in db.photos() if p.original_filename and rng.matches(p.original_filename)]
    if not hits:
        raise SourceError(f"osxphotos 在库里没找到匹配 {rng} 的照片")

    out = Path(dest).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    photos: list[Photo] = []
    warnings: list[str] = []
    edited = 0

    for ph in sorted(hits, key=lambda p: p.original_filename):
        try:
            got = ph.export(str(out), ph.original_filename, edited=ph.hasadjustments,
                            overwrite=False)
        except Exception as exc:  # osxphotos 会抛各种自定义异常
            warnings.append(f"{ph.original_filename} 导出失败: {type(exc).__name__}: {exc}")
            continue
        if not got:
            warnings.append(f"{ph.original_filename} 导出没产出文件 (原片可能没下载)")
            continue
        edited += bool(ph.hasadjustments)
        photos.append(Photo(name=ph.original_filename, path=Path(got[0])))

    if not photos:
        raise SourceError("osxphotos 一张都没导出成功")
    if edited:
        warnings.append(f"其中 {edited} 张用了「照片」App 里编辑后的版本")
    return _finish(photos, rng, warnings)
