# -*- coding: utf-8 -*-
"""原子写入工具。

账本类文件（state.json 等）原来用 `open(path, "w")` 直接覆盖写，写到一半
崩溃/断电会把文件写坏（目录里出现过 state.corrupted_by_probability.json）。
这里统一改成「写临时文件 → fsync → os.replace 原子替换」：
要么是旧内容，要么是新内容，不会出现半截文件。
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """把 text 原子地写到 path（同目录临时文件 + os.replace）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding=encoding, newline="\n") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, target)


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    indent: int | None = 2,
    encoding: str = "utf-8",
) -> None:
    """把 payload 以 JSON 原子写到 path。"""
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    atomic_write_text(path, text, encoding=encoding)


def tail_lines(path: str | Path, limit: int, *, encoding: str = "utf-8") -> list[str]:
    """只读文件末尾 limit 行，避免启动时把超大追加日志整份读进内存。

    先按块从尾部读，凑够行数就停；文件小于块大小时退化为整份读。
    """
    target = Path(path)
    if not target.exists() or limit <= 0:
        return []
    block = 64 * 1024
    chunks: list[bytes] = []
    newline_count = 0
    with open(target, "rb") as file:
        file.seek(0, os.SEEK_END)
        position = file.tell()
        while position > 0 and newline_count <= limit:
            size = min(block, position)
            position -= size
            file.seek(position)
            chunk = file.read(size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    lines = data.decode(encoding, errors="ignore").splitlines()
    return lines[-limit:]


def rotate_by_size(
    path: str | Path,
    *,
    max_bytes: int,
    backup_count: int,
) -> Path | None:
    """文件超过 max_bytes 时重命名归档（带时间戳），并只保留最近 backup_count 份。"""
    target = Path(path)
    if max_bytes <= 0 or not target.exists() or target.stat().st_size <= max_bytes:
        return None
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = target.with_name(f"{target.name}.{stamp}")
    suffix = 0
    while archive.exists():
        suffix += 1
        archive = target.with_name(f"{target.name}.{stamp}.{suffix}")
    try:
        os.replace(target, archive)
    except OSError as exc:  # pragma: no cover - 文件被占用时放弃轮转
        logger.warning("轮转 %s 失败：%s", target.name, exc)
        return None
    if backup_count >= 0:
        archives = sorted(
            (p for p in target.parent.glob(f"{target.name}.*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        for stale in archives[: max(0, len(archives) - backup_count)]:
            try:
                stale.unlink()
            except OSError as exc:  # pragma: no cover
                logger.warning("清理旧归档 %s 失败：%s", stale.name, exc)
    return archive
