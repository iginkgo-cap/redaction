"""zip 附件展开：只接受 PDF 条目，其余（加密、嵌套 zip、其他格式、超限）一律报错，由调用方整封扣留。"""
from __future__ import annotations

import io
import posixpath
import zipfile

MAX_ENTRIES = 50
MAX_TOTAL_BYTES = 100 * 1024 * 1024
UTF8_FLAG = 0x800
ENCRYPTED_FLAG = 0x1


class ArchiveError(Exception):
    pass


def _ignored(name: str) -> bool:
    return name.endswith("/") or name.startswith("__MACOSX/") or posixpath.basename(name) == ".DS_Store"


def _entry_name(info: zipfile.ZipInfo) -> str:
    """没有 UTF-8 标志位时 zipfile 按 cp437 解码；中文 Windows 打的包实际是 GBK。"""
    if info.flag_bits & UTF8_FLAG:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def expand_zip(data: bytes) -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ArchiveError("damaged zip") from e
    with zf:
        infos = [i for i in zf.infolist() if not _ignored(i.filename)]
        if not infos:
            raise ArchiveError("no PDF in zip")
        if len(infos) > MAX_ENTRIES:
            raise ArchiveError(f"more than {MAX_ENTRIES} entries")
        out, total = [], 0
        for info in infos:
            name = posixpath.basename(_entry_name(info))
            if info.flag_bits & ENCRYPTED_FLAG:
                raise ArchiveError("encrypted entry")
            if not name.lower().endswith(".pdf"):
                raise ArchiveError("non-PDF entry")
            remaining = MAX_TOTAL_BYTES - total
            with zf.open(info) as f:
                content = f.read(remaining + 1)
            if len(content) > remaining:
                raise ArchiveError("zip too large after extraction")
            if not content.startswith(b"%PDF-"):
                raise ArchiveError("entry is not a valid PDF")
            total += len(content)
            out.append((name, content))
        return out
