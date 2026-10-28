from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import app_paths

from .common import CliError, to_int

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
INLINE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
INLINE_BYTES = 1_400_000
DEFAULT_MAX_DIM = 1024
PDF_DEFAULT_PAGES = 10
PDF_MAX_PAGES = 30
PDF_MAX_BYTES = 50 * 1024 * 1024
OUTPUT_LINES = 20
IMAGE_KEEP_SECONDS = 86400


def _limits() -> tuple:
    from .code_cmds import READ_MAX_CHARS, _clip
    return READ_MAX_CHARS, _clip


def _span(value, total: int, name: str) -> tuple[int, int]:
    text = str(value).strip()
    first_text, dash, last_text = text.partition("-")
    first = to_int(first_text, name)
    last = (to_int(last_text, name) if last_text else total) if dash else first
    return first, last


def read_notebook(text: str, raw: str, flags: dict) -> str:
    cap, clip = _limits()
    try:
        notebook = json.loads(text)
        cells = notebook.get("cells") or []
    except (ValueError, AttributeError):
        raise CliError(f"{raw}: not a valid notebook; read {raw} -raw shows the JSON")
    if not cells:
        return f"# {raw} notebook, 0 cell(s)"
    first, last = 0, len(cells) - 1
    if "cell" in flags:
        first, last = _span(flags["cell"], len(cells) - 1, "cell")
        if first < 0 or last < first or first >= len(cells):
            raise CliError(f"bad -cell {flags['cell']}; notebook has cells 0-{len(cells) - 1}")
        last = min(last, len(cells) - 1)
    language = ((notebook.get("metadata") or {}).get("kernelspec") or {}).get("language") or "?"
    show_outputs = bool(flags.get("outputs"))
    rows: list[str] = []
    used = 0
    stopped = None
    for index in range(first, last + 1):
        cell = cells[index]
        source = cell.get("source", "")
        body = "".join(source) if isinstance(source, list) else str(source)
        kind = cell.get("cell_type", "?")
        count = cell.get("execution_count")
        block = [f"[{index}] {kind}" + (f" ({count})" if kind == "code" and count is not None else "")]
        block.extend(clip(line) for line in body.splitlines())
        if show_outputs and kind == "code":
            block.extend(_cell_outputs(cell.get("outputs") or [], clip))
        size = sum(len(line) + 1 for line in block)
        if rows and used + size > cap:
            stopped = index
            break
        rows.extend(block)
        used += size
    shown_last = (stopped - 1) if stopped is not None else last
    head = f"# {raw} notebook, {len(cells)} cell(s), 0-based, {language}; showing {first}-{shown_last}"
    more = f"\n# more: read {raw} -cell {stopped}-" if stopped is not None else ""
    return head + "\n" + "\n".join(rows) + more


def _cell_outputs(outputs: list, clip) -> list[str]:
    rows: list[str] = []
    for output in outputs:
        kind = output.get("output_type")
        if kind == "stream":
            piece = output.get("text", "")
        elif kind in ("execute_result", "display_data"):
            data = output.get("data") or {}
            piece = data.get("text/plain")
            if piece is None:
                piece = f"[{', '.join(data) or 'no data'}]"
            elif any(key.startswith("image/") for key in data):
                piece = (piece if isinstance(piece, str) else "".join(piece)) + " [image omitted]"
        elif kind == "error":
            piece = f"{output.get('ename', 'error')}: {output.get('evalue', '')}"
        else:
            continue
        text = "".join(piece) if isinstance(piece, list) else str(piece)
        lines = text.splitlines()
        rows.append("  out:")
        rows.extend("  | " + clip(line) for line in lines[:OUTPUT_LINES])
        if len(lines) > OUTPUT_LINES:
            rows.append(f"  | ...[+{len(lines) - OUTPUT_LINES} lines]")
    return rows


def read_pdf(target: Path, raw: str, flags: dict, start: int | None, end: int | None) -> str:
    cap, clip = _limits()
    try:
        from pypdf import PdfReader
    except ImportError:
        raise CliError("pypdf is not installed; pip install pypdf")
    size = target.stat().st_size
    if size > PDF_MAX_BYTES:
        raise CliError(f"{target.name} is {size // 1048576} MB, over the {PDF_MAX_BYTES // 1048576} MB pdf limit")
    try:
        reader = PdfReader(str(target))
        if reader.is_encrypted and not reader.decrypt(""):
            raise CliError(f"{target.name} is password protected")
        total = len(reader.pages)
    except CliError:
        raise
    except Exception as error:
        raise CliError(f"cannot read pdf {target.name}: {error}")
    if total == 0:
        return f"# {raw} (no pages)"
    if "pages" in flags:
        first, last = _span(flags["pages"], total, "pages")
    elif start is not None:
        first, last = start, (end if end is not None else total)
    else:
        first, last = 1, min(total, PDF_DEFAULT_PAGES)
    if first < 1 or last < first or first > total:
        raise CliError(f"bad page range; {target.name} has {total} page(s)")
    last = min(last, total, first + PDF_MAX_PAGES - 1)
    rows: list[str] = []
    used = 0
    shown_last = first - 1
    for number in range(first, last + 1):
        try:
            body = reader.pages[number - 1].extract_text() or ""
        except Exception as error:
            body = f"(page unreadable: {error})"
        block = [f"--- page {number} ---"] + [clip(line) for line in body.splitlines() if line.strip()]
        size = sum(len(line) + 1 for line in block)
        if rows and used + size > cap:
            break
        if not rows and size > cap:
            block = block[: max(2, cap // 80)]
        rows.extend(block)
        used += size
        shown_last = number
    if all(line.startswith("--- page") for line in rows):
        rows.append("(no extractable text; scanned pdf?)")
    more = f"\n# more: read {raw} -pages {shown_last + 1}-" if shown_last < total else ""
    return f"# {raw} pages {first}-{shown_last}/{total}\n" + "\n".join(rows) + more


def _sweep(folder: Path) -> None:
    cutoff = time.time() - IMAGE_KEEP_SECONDS
    try:
        for item in folder.iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
    except OSError:
        pass


def read_image(target: Path, raw: str, flags: dict) -> str:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise CliError("Pillow is not installed; pip install pillow")
    max_dim = to_int(flags.get("max-dim", DEFAULT_MAX_DIM), "max-dim")
    quality = to_int(flags.get("quality", 80), "quality")
    if max_dim < 16 or not 10 <= quality <= 95:
        raise CliError("-max-dim must be at least 16 and -quality 10-95")
    size = target.stat().st_size
    try:
        image = Image.open(target)
        image.load()
    except Exception as error:
        raise CliError(f"cannot read image {target.name}: {error}")
    width, height = image.size
    label = f"{raw} {width}x{height} {(image.format or target.suffix[1:]).lower()} {max(1, size // 1024)} KB"
    if max(width, height) <= max_dim and size <= INLINE_BYTES and target.suffix.lower() in INLINE_SUFFIXES:
        return f"# {label}, sent as is\nok screenshot {target}"
    image = ImageOps.exif_transpose(image)
    image.thumbnail((max_dim, max_dim))
    alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    folder = app_paths.logs_dir() / "read_images"
    folder.mkdir(parents=True, exist_ok=True)
    _sweep(folder)
    stamp = f"{target}|{target.stat().st_mtime_ns}|{max_dim}|{quality}"
    path = folder / (hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:16] + (".png" if alpha else ".jpg"))
    for _ in range(8):
        if alpha:
            image.convert("RGBA").save(path, "PNG", optimize=True)
        else:
            image.convert("RGB").save(path, "JPEG", quality=quality, optimize=True)
        if path.stat().st_size <= INLINE_BYTES:
            break
        if not alpha and quality > 40:
            quality -= 15
        else:
            image = image.resize((max(16, int(image.width * 0.75)), max(16, int(image.height * 0.75))))
    out_width, out_height = image.size
    return f"# {label} -> {out_width}x{out_height} {path.suffix[1:]} {max(1, path.stat().st_size // 1024)} KB\nok screenshot {path}"
