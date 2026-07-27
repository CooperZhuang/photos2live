"""照片来源:系统"照片"App 库 / osxphotos 导出 / 普通文件夹。"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp"}
_NUM_RE = re.compile(r"^(?P<prefix>.*?)(?P<num>\d+)$")


class SourceError(RuntimeError):
    """照片来源不可用或找不到照片。"""


@dataclass(frozen=True)
class Photo:
    name: str  # 原始文件名,如 P1001222.JPG
    path: Path  # 磁盘上的实际位置

    @property
    def key(self) -> tuple[str, int]:
        return split_name(self.name)[:2]


def split_name(filename: str) -> tuple[str, int, int]:
    """P1001222.JPG -> ('P', 1001222, 7)。没有数字尾巴则返回 (整名, -1, 0)。"""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    m = _NUM_RE.match(stem)
    if not m:
        return (stem, -1, 0)
    return (m.group("prefix"), int(m.group("num")), len(m.group("num")))


@dataclass(frozen=True)
class Range:
    prefix: str
    start: int
    end: int
    width: int

    def matches(self, filename: str) -> bool:
        p, n, _ = split_name(filename)
        return p == self.prefix and n >= 0 and self.start <= n <= self.end

    def label(self, num: int) -> str:
        return f"{self.prefix}{num:0{self.width}d}"

    @property
    def expected(self) -> int:
        return self.end - self.start + 1

    def __str__(self) -> str:
        return f"{self.label(self.start)}-{self.label(self.end)}"


def parse_range(spec: str) -> Range:
    """解析 `P1001222-P1001325`,右端可简写为 `P1001222-1325` 或 `-325`。"""
    if "-" not in spec:
        raise SourceError(f"--range 格式应为 `起始-结束`,例如 P1001222-P1001325 (得到 {spec!r})")
    left, _, right = spec.partition("-")
    left, right = left.strip(), right.strip()

    lp, lnum, lwidth = split_name(left)
    if lnum < 0:
        raise SourceError(f"--range 起始项 {left!r} 结尾没有数字序号")

    rp, rnum, rwidth = split_name(right)
    if rnum < 0:
        raise SourceError(f"--range 结束项 {right!r} 结尾没有数字序号")
    if rp and rp != lp:
        raise SourceError(f"--range 两端前缀不一致: {lp!r} vs {rp!r}")
    if not rp and rwidth < lwidth:
        # 简写补全:用起始序号的高位填充,P1001222-325 -> P1001325
        head = str(lnum)[: lwidth - rwidth]
        rnum = int(head + str(rnum).zfill(rwidth))

    if rnum < lnum:
        raise SourceError(f"--range 结束序号 {rnum} 小于起始序号 {lnum}")
    return Range(prefix=lp, start=lnum, end=rnum, width=lwidth)


def _finish(
    photos: list[Photo], rng: Range | None, warnings: list[str]
) -> tuple[list[Photo], list[str]]:
    """按序号自然排序 + 检查区间完整性。"""
    photos.sort(key=lambda p: (p.key[0], p.key[1], p.name))
    if not photos:
        raise SourceError("没有找到任何照片")
    if rng is not None and len(photos) != rng.expected:
        got = {p.key[1] for p in photos}
        missing = [rng.label(n) for n in range(rng.start, rng.end + 1) if n not in got]
        if missing:
            shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
            warnings.append(f"区间 {rng} 期望 {rng.expected} 张,实际 {len(photos)} 张,缺少: {shown}")
    return photos, warnings


def from_directory(directory: str | Path, rng: Range | None = None) -> tuple[list[Photo], list[str]]:
    """从普通文件夹读照片(用户自己导出好的)。"""
    root = Path(directory).expanduser()
    if not root.is_dir():
        raise SourceError(f"文件夹不存在: {root}")
    photos = [
        Photo(name=p.name, path=p)
        for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")
        and (rng is None or rng.matches(p.name))
    ]
    if not photos:
        hint = f" (区间 {rng})" if rng else ""
        raise SourceError(f"{root} 里没有匹配的图片{hint}")
    return _finish(photos, rng, [])


def find_library(explicit: str | Path | None = None) -> Path:
    """定位 .photoslibrary。默认取 ~/Pictures 下最近使用的那个。"""
    if explicit:
        lib = Path(explicit).expanduser()
        if not (lib / "database" / "Photos.sqlite").exists():
            raise SourceError(f"{lib} 看起来不是照片图库 (缺 database/Photos.sqlite)")
        return lib
    found = list(Path.home().joinpath("Pictures").glob("*.photoslibrary"))
    cands = sorted(
        (p for p in found if (p / "database" / "Photos.sqlite").exists()),
        key=lambda p: (p / "database" / "Photos.sqlite").stat().st_mtime,
        reverse=True,
    )
    if found and not cands:
        raise SourceError(
            f"找到 {len(found)} 个 .photoslibrary 但都没有可读的 database/Photos.sqlite"
            f" ({', '.join(p.name for p in found)})。用 --library 指定,或用 --input-dir。"
        )
    if not cands:
        raise SourceError(
            "在 ~/Pictures 下没找到照片图库。用 --library 指定路径,"
            "或先从「照片」App 手动导出后用 --input-dir。"
        )
    return cands[0]


def _locate_original(lib: Path, uuid: str, filename: str) -> Path | None:
    """原图存在 originals/<uuid首字符>/<uuid>.<ext>;扩展名可能与库记录不同。"""
    bucket = lib / "originals" / uuid[0]
    ext = Path(filename).suffix
    for cand in (bucket / f"{uuid}{ext}", bucket / f"{uuid}{ext.upper()}",
                 bucket / f"{uuid}{ext.lower()}"):
        if cand.exists():
            return cand
    hits = sorted(bucket.glob(f"{uuid}.*"))
    return hits[0] if hits else None


def from_library(
    rng: Range, library: str | Path | None = None
) -> tuple[list[Photo], list[str]]:
    """直连"照片"库读原图,不复制不导出 —— 最快,但只拿原片(不含 App 里的编辑)。

    需要终端有"完整磁盘访问权限"。
    """
    lib = find_library(library)
    db = lib / "database" / "Photos.sqlite"
    warnings: list[str] = []

    # 拷一份 DB 再读,避开 App 正在写入时的 WAL 状态
    with tempfile.TemporaryDirectory(prefix="p2v-db-") as tmp:
        snap = Path(tmp) / "Photos.sqlite"
        try:
            shutil.copy2(db, snap)
            for side in ("-wal", "-shm"):
                extra = db.with_name(db.name + side)
                if extra.exists():
                    shutil.copy2(extra, snap.with_name(snap.name + side))
        except PermissionError as exc:
            raise SourceError(
                f"读不了照片库数据库 ({exc.strerror})。请在 系统设置 > 隐私与安全性 > "
                "完整磁盘访问权限 里给终端授权,或改用 --input-dir 手动导出的文件夹。"
            ) from exc
        rows = _query_assets(snap, rng)

    if not rows:
        raise SourceError(f"照片库里没有匹配 {rng} 的照片")

    photos: list[Photo] = []
    seen: dict[str, str] = {}
    missing: list[str] = []
    for filename, uuid in rows:
        if filename in seen:
            warnings.append(f"{filename} 在库里有多份,用最早导入的那份")
            continue
        seen[filename] = uuid
        found = _locate_original(lib, uuid, filename)
        if found is None:
            missing.append(filename)
            continue
        photos.append(Photo(name=filename, path=found))

    if missing:
        shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        warnings.append(
            f"{len(missing)} 张照片在库里有记录但本地没有原图(可能开了 iCloud 优化存储,"
            f"需先在「照片」App 里下载原片): {shown}"
        )
    if not photos:
        raise SourceError("匹配到记录但一张原图都没找到,请检查 iCloud 是否已下载原片")
    return _finish(photos, rng, warnings)


def _query_assets(db: Path, rng: Range) -> list[tuple[str, str]]:
    """查 (原始文件名, uuid),按导入时间升序 —— 重复时取最早的。

    注意:ZASSET.ZFILENAME 存的是库内部的 UUID 名,真正的原始文件名
    (P1001222.JPG) 在 ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME。
    """
    uri = f"file:{db}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        acols = {r[1] for r in con.execute("PRAGMA table_info(ZASSET)")}
        bcols = {r[1] for r in con.execute("PRAGMA table_info(ZADDITIONALASSETATTRIBUTES)")}
        if not acols:
            raise SourceError("照片库结构不认识 (没有 ZASSET 表),请改用 --input-dir")
        if "ZORIGINALFILENAME" not in bcols:
            raise SourceError(
                "这个照片库版本里找不到原始文件名字段。请改用 --input-dir "
                "(手动导出),或装 osxphotos 后用 --source osxphotos。"
            )
        link = "ZASSET" if "ZASSET" in bcols else "Z_PK"
        where = ["a.ZUUID IS NOT NULL", "b.ZORIGINALFILENAME LIKE ? ESCAPE '\\'"]
        esc = rng.prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: list[object] = [esc + "%"]
        if "ZTRASHEDSTATE" in acols:
            where.append("a.ZTRASHEDSTATE = 0")
        order = "a.ZDATECREATED" if "ZDATECREATED" in acols else "a.Z_PK"
        sql = (
            "SELECT b.ZORIGINALFILENAME, a.ZUUID FROM ZASSET a "
            f"JOIN ZADDITIONALASSETATTRIBUTES b ON b.{link} = a.Z_PK "
            f"WHERE {' AND '.join(where)} ORDER BY {order}"
        )
        return [(n, u) for n, u in con.execute(sql, params) if n and rng.matches(n)]
    finally:
        con.close()
