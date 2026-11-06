"""v2 PDF 载体（ADR-002 §3.2、LOOK-6876）：PDF 读写在 file-masker 引擎里，这里只做适配。

PyMuPDF 是同步调用，服务只有一个 uvicorn worker：抽字与写回各自放进 asyncio.to_thread，
每次从字节打开、用完即关，不跨线程共用 Document；整体过 pdf_concurrency 信号量，不占推理槽。
"""
from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass

import fitz
from file_masker import engine as fm

from ..settings import settings
from .errors import ApiError

# 页内替换：（起, 止, 替换文字），区间是该页 page_text 的码点下标
Edit = tuple[int, int, str]
# 每处替换涂抹的矩形 [x0, y0, x1, y1]，跨行 / 跨 span 时多条
Boxes = list[list[float]]


@dataclass(frozen=True)
class PdfPage:
    page: int  # 1 起
    scanned: bool
    text: str  # 扫描页为空串


_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _semaphores:
        _semaphores[loop] = asyncio.Semaphore(settings.pdf_concurrency)
    return _semaphores[loop]


def _open(data: bytes) -> fitz.Document:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001  PyMuPDF 对坏文件抛的类型不固定
        raise ApiError("file_unreadable", "file is not a readable PDF") from exc
    if doc.needs_pass:
        doc.close()
        raise ApiError("file_unreadable", "PDF is encrypted")
    return doc


def extract_sync(data: bytes) -> list[PdfPage]:
    if len(data) > settings.max_file_bytes:
        raise ApiError("file_too_large", f"{len(data)} bytes > {settings.max_file_bytes}")
    doc = _open(data)
    try:
        if doc.page_count > settings.max_file_pages:
            raise ApiError("file_too_large", f"{doc.page_count} pages > {settings.max_file_pages}")
        scanned = set(fm.find_scanned_pages(doc))
        return [PdfPage(p.number + 1, p.number + 1 in scanned,
                        "" if p.number + 1 in scanned else fm.page_text(p).text) for p in doc]
    finally:
        doc.close()


def write_sync(data: bytes, edits: dict[int, list[Edit]]) -> tuple[bytes, dict[int, list[Boxes]]]:
    """edits 按页码（1 起）给出；返回新 PDF 与每处替换的矩形（与 edits 一一对应）。没有替换时原样返回。"""
    if not edits:
        return data, {}
    doc = _open(data)
    try:
        boxes: dict[int, list[Boxes]] = {}
        for page_no, page_edits in edits.items():
            page = doc[page_no - 1]
            pt = fm.page_text(page)
            reps = [fm.Replacement(fm.offsets_to_parts(pt, s, e), text) for s, e, text in page_edits]
            boxes[page_no] = [[[round(v, 2) for v in r] for r in rects] for rects in fm.replace_spans(page, reps)]
        return doc.tobytes(garbage=4, clean=True, deflate=True), boxes
    finally:
        doc.close()


async def extract(data: bytes) -> list[PdfPage]:
    async with _semaphore():
        return await asyncio.to_thread(extract_sync, data)


async def write(data: bytes, edits: dict[int, list[Edit]]) -> tuple[bytes, dict[int, list[Boxes]]]:
    async with _semaphore():
        return await asyncio.to_thread(write_sync, data, edits)
