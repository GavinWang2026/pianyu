# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""标准库 CJK PDF：从系统 TrueType 字体子集嵌入，正文可选中、可搜索。"""

from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path

# 常见系统字体（优先含 CJK 的 TrueType，跳过 bitmap / TTC）
_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),  # 可能是 ttc，下面会跳过
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simsun.ttc"),
    Path("C:/Windows/Fonts/arialuni.ttf"),
)


def find_cjk_font() -> Path | None:
    for path in _FONT_CANDIDATES:
        if not path.is_file():
            continue
        try:
            head = path.read_bytes()[:4]
        except OSError:
            continue
        # 只要 TrueType（含 glyf），不要 TTC / bitmap
        if head in (b"\x00\x01\x00\x00", b"true"):
            return path
    return None


def render_cjk_pdf(title: str, lines: list[str], *, font_path: Path | None = None) -> bytes | None:
    """成功返回 PDF 字节；找不到可用字体时返回 None。

    版式：标题 20pt / 小标题 14pt / 正文 11pt；段前约 3pt、段后约 6pt；
    页脚「第 N / M 页 · 生成于 …」。
    """
    font = Path(font_path) if font_path else find_cjk_font()
    if font is None or not font.is_file():
        return None
    pages = _paginate(title, lines)
    gen_label = time.strftime("%Y-%m-%d %H:%M")
    n_pages = max(1, len(pages))
    # 先算全部页脚字符串再并入子集，避免「·」「-」「:」落 .notdef
    footers = [
        f"第 {i} / {n_pages} 页 · 生成于 {gen_label}"
        for i in range(1, n_pages + 1)
    ]
    used_chars = [title or "", *footers, "第页生成于年月日时分秒 /·-:"]
    for page in pages:
        for role, text in page:
            used_chars.append(text)
    used = "".join(used_chars)
    try:
        subset, gid_of, units_per_em, advances = subset_ttf(font, used)
    except Exception:
        return None
    return _build_pdf(
        pages, subset, gid_of, units_per_em, advances,
        gen_label=gen_label,
    )


# 统一版式强制分页标记（fusion.layout.PAGEBREAK_SENTINEL）
_PAGEBREAK = "<<<AVD_PAGEBREAK>>>"


def _role_for_line(line: str) -> str:
    s = (line or "").strip()
    if not s:
        return "blank"
    if s == _PAGEBREAK:
        return "pagebreak"
    if s.startswith("### "):
        return "h3"
    if s.startswith("## "):
        return "h2"
    if s.startswith("# "):
        return "h1"
    if s.startswith("---"):
        return "blank"
    return "body"


def _strip_md(line: str) -> str:
    s = line or ""
    for prefix in ("### ", "## ", "# "):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def _wrap(text: str, width: int) -> list[str]:
    """按宽度折行；ASCII 词尽量在空格/标点处回退，避免 English wo/rds。"""
    if width < 4:
        width = 4
    if len(text) <= width:
        return [text]
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + width, n)
        if end >= n:
            out.append(text[i:])
            break
        chunk = text[i:end]
        # 若切点落在 ASCII 词中间，回退到最近空格/标点
        if end < n and text[end - 1].isascii() and text[end].isascii() and text[end] not in " \t":
            cut = -1
            for j in range(len(chunk) - 1, max(0, len(chunk) - 16) - 1, -1):
                if chunk[j] in " \t-/.,;:!?）】》\"'":
                    cut = j + 1
                    break
            if cut > 0:
                end = i + cut
                chunk = text[i:end]
        out.append(chunk.rstrip() if chunk.endswith(" ") else chunk)
        i = end
        while i < n and text[i] == " ":
            i += 1
    return [p for p in out if p]


def _paginate(
    title: str,
    lines: list[str],
    *,
    max_units: float = 720.0,
) -> list[list[tuple[str, str]]]:
    """按视觉高度分页。每项为 (role, text)。"""
    # 行高估算（pt）：blank 提到 12，拉开段距
    heights = {"title": 28.0, "h1": 22.0, "h2": 18.0, "h3": 16.0, "body": 17.0, "blank": 12.0}
    widths = {"title": 28, "h1": 32, "h2": 36, "h3": 38, "body": 42, "blank": 42}

    items: list[tuple[str, str]] = []
    if title:
        for part in _wrap(title.strip(), widths["title"]):
            items.append(("title", part))
        items.append(("blank", ""))
    for raw in lines:
        role = _role_for_line(raw)
        if role == "pagebreak":
            items.append(("pagebreak", ""))
            continue
        text = _strip_md(raw)
        if role == "blank":
            items.append(("blank", ""))
            continue
        for part in _wrap(text, widths.get(role, 42)):
            items.append((role, part))
        # 段后间距：非 blank 后插一空行（body/标题后）
        if role in ("body", "h1", "h2", "h3"):
            items.append(("blank", ""))
    if not items:
        items = [("body", "(empty)")]

    pages: list[list[tuple[str, str]]] = [[]]
    used = 0.0
    for role, text in items:
        if role == "pagebreak":
            if pages[-1]:
                pages.append([])
                used = 0.0
            continue
        h = heights.get(role, 14.0)
        if pages[-1] and used + h > max_units:
            pages.append([])
            used = 0.0
        pages[-1].append((role, text))
        used += h
    # 去掉尾部空页
    while len(pages) > 1 and not pages[-1]:
        pages.pop()
    if not pages:
        pages = [[("body", "(empty)")]]
    return pages


# ---------------------------------------------------------------------------
# 最小 TrueType 子集
# ---------------------------------------------------------------------------

def _read_tables(data: bytes) -> dict[str, tuple[int, int]]:
    n = struct.unpack(">H", data[4:6])[0]
    out: dict[str, tuple[int, int]] = {}
    off = 12
    for _ in range(n):
        tag = data[off:off + 4].decode("latin-1")
        _csum, offset, length = struct.unpack(">III", data[off + 4:off + 16])
        out[tag] = (offset, length)
        off += 16
    return out


def _table(data: bytes, tables: dict[str, tuple[int, int]], tag: str) -> bytes:
    off, length = tables[tag]
    return data[off:off + length]


def _parse_cmap(cmap: bytes) -> dict[int, int]:
    """unicode codepoint -> glyph id（优先 format 12，其次 format 4）。"""
    num = struct.unpack(">H", cmap[2:4])[0]
    records = []
    for i in range(num):
        plat, enc, offset = struct.unpack(">HHI", cmap[4 + i * 8:4 + (i + 1) * 8])
        records.append((plat, enc, offset))
    # 3,10 / 0,4 = full unicode; 3,1 / 0,3 = bmp
    preferred = [(3, 10), (0, 4), (3, 1), (0, 3), (0, 1)]
    chosen = None
    for want in preferred:
        for plat, enc, offset in records:
            if (plat, enc) == want:
                chosen = offset
                break
        if chosen is not None:
            break
    if chosen is None and records:
        chosen = records[0][2]
    if chosen is None:
        return {}
    sub = cmap[chosen:]
    fmt = struct.unpack(">H", sub[0:2])[0]
    mapping: dict[int, int] = {}
    if fmt == 4:
        seg_count = struct.unpack(">H", sub[6:8])[0] // 2
        end = list(struct.unpack(f">{seg_count}H", sub[14:14 + 2 * seg_count]))
        start_off = 16 + 2 * seg_count
        start = list(struct.unpack(f">{seg_count}H", sub[start_off:start_off + 2 * seg_count]))
        delta_off = start_off + 2 * seg_count
        delta = list(struct.unpack(f">{seg_count}h", sub[delta_off:delta_off + 2 * seg_count]))
        range_off = delta_off + 2 * seg_count
        range_shift = list(struct.unpack(f">{seg_count}H", sub[range_off:range_off + 2 * seg_count]))
        glyph_id_array = sub[range_off + 2 * seg_count:]
        for i in range(seg_count):
            for cp in range(start[i], end[i] + 1):
                if range_shift[i] == 0:
                    gid = (cp + delta[i]) & 0xFFFF
                else:
                    idx = range_shift[i] // 2 + (cp - start[i]) - (seg_count - i)
                    if idx < 0 or idx * 2 + 2 > len(glyph_id_array):
                        gid = 0
                    else:
                        g = struct.unpack(">H", glyph_id_array[idx * 2:idx * 2 + 2])[0]
                        gid = ((g + delta[i]) & 0xFFFF) if g else 0
                if gid:
                    mapping[cp] = gid
    elif fmt == 12:
        n_groups = struct.unpack(">I", sub[12:16])[0]
        pos = 16
        for _ in range(n_groups):
            start_cp, end_cp, start_gid = struct.unpack(">III", sub[pos:pos + 12])
            pos += 12
            for i, cp in enumerate(range(start_cp, end_cp + 1)):
                mapping[cp] = start_gid + i
    return mapping


def _glyph_deps(glyf: bytes, loca: list[int], gid: int) -> set[int]:
    """收集复合字形依赖。"""
    deps = {gid}
    stack = [gid]
    while stack:
        g = stack.pop()
        if g + 1 >= len(loca):
            continue
        start, end = loca[g], loca[g + 1]
        if end <= start:
            continue
        data = glyf[start:end]
        if len(data) < 10:
            continue
        num_contours = struct.unpack(">h", data[0:2])[0]
        if num_contours >= 0:
            continue
        pos = 10
        while pos + 4 <= len(data):
            flags, comp = struct.unpack(">HH", data[pos:pos + 4])
            pos += 4
            if comp not in deps:
                deps.add(comp)
                stack.append(comp)
            if flags & 0x0001:  # ARG_1_AND_2_ARE_WORDS
                pos += 4
            else:
                pos += 2
            if flags & 0x0008:
                pos += 2
            elif flags & 0x0040:
                pos += 4
            elif flags & 0x0080:
                pos += 8
            if not (flags & 0x0020):  # MORE_COMPONENTS
                break
    return deps


def subset_ttf(font_path: Path, text: str) -> tuple[bytes, dict[int, int], int, dict[int, int]]:
    """返回 (subset_ttf_bytes, codepoint->new_gid, units_per_em, new_gid->advance)."""
    data = font_path.read_bytes()
    tables = _read_tables(data)
    for need in ("glyf", "loca", "head", "hmtx", "hhea", "maxp", "cmap"):
        if need not in tables:
            raise RuntimeError(f"字体缺少表 {need}")

    head = _table(data, tables, "head")
    units = struct.unpack(">H", head[18:20])[0]
    index_to_loc = struct.unpack(">H", head[50:52])[0]
    maxp = _table(data, tables, "maxp")
    num_glyphs = struct.unpack(">H", maxp[4:6])[0]
    loca_raw = _table(data, tables, "loca")
    if index_to_loc == 0:
        loca = [x * 2 for x in struct.unpack(f">{len(loca_raw)//2}H", loca_raw)]
    else:
        loca = list(struct.unpack(f">{len(loca_raw)//4}I", loca_raw))
    glyf = bytearray(_table(data, tables, "glyf"))
    hhea = _table(data, tables, "hhea")
    num_h = struct.unpack(">H", hhea[34:36])[0]
    hmtx = _table(data, tables, "hmtx")
    advances_old: list[int] = []
    for i in range(num_glyphs):
        if i < num_h:
            adv = struct.unpack(">H", hmtx[i * 4:i * 4 + 2])[0]
        else:
            adv = advances_old[-1] if advances_old else units
        advances_old.append(adv)

    cmap_map = _parse_cmap(_table(data, tables, "cmap"))
    needed_old: set[int] = {0}
    cp_to_old: dict[int, int] = {}
    for ch in text:
        cp = ord(ch)
        if cp < 32 and cp not in (9, 10, 13):
            continue
        gid = cmap_map.get(cp, 0)
        if gid:
            cp_to_old[cp] = gid
            needed_old |= _glyph_deps(bytes(glyf), loca, gid)
        else:
            cp_to_old[cp] = 0

    old_gids = sorted(needed_old)
    old_to_new = {old: i for i, old in enumerate(old_gids)}
    new_glyf = bytearray()
    new_loca = [0]
    new_advances: dict[int, int] = {}

    for old in old_gids:
        start, end = loca[old], loca[old + 1] if old + 1 < len(loca) else loca[old]
        chunk = bytearray(glyf[start:end])
        if chunk and struct.unpack(">h", chunk[0:2])[0] < 0:
            # 重写复合字形引用到新 gid
            pos = 10
            while pos + 4 <= len(chunk):
                flags, comp = struct.unpack(">HH", chunk[pos:pos + 4])
                new_comp = old_to_new.get(comp, 0)
                chunk[pos + 2:pos + 4] = struct.pack(">H", new_comp)
                pos += 4
                if flags & 0x0001:
                    pos += 4
                else:
                    pos += 2
                if flags & 0x0008:
                    pos += 2
                elif flags & 0x0040:
                    pos += 4
                elif flags & 0x0080:
                    pos += 8
                if not (flags & 0x0020):
                    break
        # 4 字节对齐
        while len(new_glyf) % 4:
            new_glyf.append(0)
        new_loca[-1] = len(new_glyf)
        new_glyf.extend(chunk)
        new_loca.append(len(new_glyf))
        new_gid = old_to_new[old]
        new_advances[new_gid] = advances_old[old] if old < len(advances_old) else units

    # 补齐 loca 末尾
    new_loca[-1] = len(new_glyf)
    while len(new_glyf) % 4:
        new_glyf.append(0)
        new_loca[-1] = len(new_glyf)

    new_num = len(old_gids)
    # cmap format 4（BMP 足够覆盖常用中文；超 BMP 回退 .notdef）
    cps = sorted(cp for cp in cp_to_old if cp <= 0xFFFF)
    # 为每个码点单独一段（简单可靠）
    segments: list[tuple[int, int, int]] = []
    for cp in cps:
        old = cp_to_old[cp]
        segments.append((cp, cp, old_to_new.get(old, 0)))
    # 加上 0xFFFF 哨兵
    segments.append((0xFFFF, 0xFFFF, 0))
    seg_count = len(segments)
    end_code = [s[1] for s in segments]
    start_code = [s[0] for s in segments]
    id_delta = [(s[2] - s[0]) & 0xFFFF for s in segments]
    id_range = [0] * seg_count
    # format 4 长度
    length = 16 + seg_count * 8
    cmap4 = bytearray()
    cmap4 += struct.pack(">HH", 4, length)
    cmap4 += struct.pack(">H", 0)  # language
    cmap4 += struct.pack(">H", seg_count * 2)
    # searchRange / entrySelector / rangeShift
    search = 2
    entry = 0
    while search * 2 <= seg_count * 2:
        search *= 2
        entry += 1
    search //= 2
    cmap4 += struct.pack(">HHH", search, entry, seg_count * 2 - search)
    cmap4 += struct.pack(f">{seg_count}H", *end_code)
    cmap4 += struct.pack(">H", 0)  # reserved
    cmap4 += struct.pack(f">{seg_count}H", *start_code)
    cmap4 += struct.pack(f">{seg_count}H", *id_delta)
    cmap4 += struct.pack(f">{seg_count}H", *id_range)
    cmap_table = bytearray()
    cmap_table += struct.pack(">HH", 0, 1)  # version, numTables
    cmap_table += struct.pack(">HHI", 3, 1, 12)  # platform 3 enc 1
    cmap_table += cmap4

    # loca / maxp / hmtx / head
    if new_loca[-1] > 0x1FFFF:
        loca_fmt = 1
        loca_bytes = struct.pack(f">{len(new_loca)}I", *new_loca)
    else:
        loca_fmt = 0
        loca_bytes = struct.pack(f">{len(new_loca)}H", *[x // 2 for x in new_loca])
    new_head = bytearray(head)
    new_head[50:52] = struct.pack(">H", loca_fmt)
    new_maxp = bytearray(maxp)
    new_maxp[4:6] = struct.pack(">H", new_num)
    new_hhea = bytearray(hhea)
    new_hhea[34:36] = struct.pack(">H", new_num)
    hmtx_bytes = b"".join(struct.pack(">Hh", new_advances.get(i, units), 0) for i in range(new_num))

    keep = {
        "head": bytes(new_head),
        "hhea": bytes(new_hhea),
        "maxp": bytes(new_maxp),
        "cmap": bytes(cmap_table),
        "loca": loca_bytes,
        "glyf": bytes(new_glyf),
        "hmtx": hmtx_bytes,
    }
    for optional in ("cvt ", "fpgm", "prep", "gasp"):
        if optional in tables:
            keep[optional] = _table(data, tables, optional)
    # 最小 name / post
    keep["name"] = (
        b"\x00\x00\x00\x01\x00\x00\x00\x00"
        + b"\x00\x01\x00\x00\x00\x00\x00\x00"
        + b"AVD"
    )
    # 修一下 name 表为合法结构
    name_str = "AVDSub".encode("utf-16-be")
    keep["name"] = (
        struct.pack(">HHH", 0, 1, 18)
        + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(name_str), 0)
        + name_str
    )
    keep["post"] = struct.pack(">iiiHHhhh", 0x00030000, 0, 0, 0, 0, 0, 0, 0) + b"\x00" * 4

    # 组装 sfnt
    tags = sorted(keep.keys())
    num = len(tags)
    offset = 12 + 16 * num
    records = []
    body = bytearray()
    for tag in tags:
        raw = keep[tag]
        # 4 字节对齐
        pad = (4 - (len(raw) % 4)) % 4
        # checksum
        padded = raw + b"\x00" * pad
        csum = 0
        for i in range(0, len(padded), 4):
            csum = (csum + struct.unpack(">I", padded[i:i + 4])[0]) & 0xFFFFFFFF
        records.append((tag, csum, offset + len(body), len(raw)))
        body.extend(padded)
    # searchRange 等
    sr = 1
    es = 0
    while sr * 2 <= num:
        sr *= 2
        es += 1
    sr = sr * 16
    rs = num * 16 - sr
    out = bytearray()
    out += struct.pack(">IHHHH", 0x00010000, num, sr, es, rs)
    for tag, csum, off, length in records:
        out += tag.encode("latin-1")
        out += struct.pack(">III", csum, off, length)
    out.extend(body)

    # 修正 head checksum adjustment
    tables2 = _read_tables(bytes(out))
    head_off, head_len = tables2["head"]
    # 先清 adjustment
    out[head_off + 8:head_off + 12] = b"\x00\x00\x00\x00"
    total = 0
    padded_all = bytes(out) + b"\x00" * ((4 - (len(out) % 4)) % 4)
    for i in range(0, len(padded_all), 4):
        total = (total + struct.unpack(">I", padded_all[i:i + 4])[0]) & 0xFFFFFFFF
    adjustment = (0xB1B0AFBA - total) & 0xFFFFFFFF
    out[head_off + 8:head_off + 12] = struct.pack(">I", adjustment)

    cp_to_new = {cp: old_to_new.get(gid, 0) for cp, gid in cp_to_old.items()}
    return bytes(out), cp_to_new, units, new_advances


# ---------------------------------------------------------------------------
# PDF 组装（Identity-H CIDFontType2）
# ---------------------------------------------------------------------------

def _build_pdf(
    pages: list[list[tuple[str, str]]],
    font_bytes: bytes,
    gid_of: dict[int, int],
    units_per_em: int,
    advances: dict[int, int],
    *,
    gen_label: str | None = None,
) -> bytes:
    font_stream = zlib.compress(font_bytes)
    # W 数组：按 gid 给宽度（PDF 单位 = 1/1000 em）
    scale = 1000.0 / float(units_per_em or 1000)
    w_parts: list[str] = []
    for gid in sorted(advances):
        w_parts.append(f"{gid} [{max(1, int(advances[gid] * scale))}]")
    w_array = " ".join(w_parts) if w_parts else "0 [500]"

    # ToUnicode CMap
    bfchar = []
    for cp, gid in sorted(gid_of.items(), key=lambda x: x[1]):
        if gid == 0:
            continue
        bfchar.append(f"<{gid:04X}> <{cp:04X}>")
    # CMap 分段，每段最多 100
    cmap_chunks: list[str] = []
    for i in range(0, len(bfchar), 100):
        part = bfchar[i:i + 100]
        cmap_chunks.append(f"{len(part)} beginbfchar\n" + "\n".join(part) + "\nendbfchar\n")
    to_unicode = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> def\n"
        "/CMapName /AVD-Identity-UCS def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        + "".join(cmap_chunks)
        + "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("ascii")
    to_unicode_z = zlib.compress(to_unicode)

    def _hex_line(text: str) -> str:
        encoded = bytearray()
        for ch in text:
            gid = gid_of.get(ord(ch), 0)
            encoded.extend(struct.pack(">H", gid))
        return encoded.hex().upper()

    sizes = {"title": 20, "h1": 16, "h2": 14, "h3": 12, "body": 11, "blank": 11}
    leadings = {"title": 28, "h1": 22, "h2": 18, "h3": 16, "body": 14, "blank": 12}
    if not gen_label:
        gen_label = time.strftime("%Y-%m-%d %H:%M")
    n_pages = max(1, len(pages))
    page_streams: list[bytes] = []
    for page_i, lines in enumerate(pages, start=1):
        parts = ["BT", "50 800 Td"]
        cur_size = -1
        for role, text in lines:
            size = sizes.get(role, 11)
            leading = leadings.get(role, 14)
            if size != cur_size:
                parts.append(f"/F1 {size} Tf")
                parts.append(f"{leading} TL")
                cur_size = size
            else:
                parts.append(f"{leading} TL")
            if role == "blank" or not text:
                parts.append("T*")
                continue
            parts.append(f"<{_hex_line(text)}> Tj")
            parts.append("T*")
        parts.append("ET")
        # 页脚（独立文本对象，固定底部）
        footer = f"第 {page_i} / {n_pages} 页 · 生成于 {gen_label}"
        parts.append("BT")
        parts.append("/F1 9 Tf")
        parts.append("50 36 Td")
        parts.append(f"<{_hex_line(footer)}> Tj")
        parts.append("ET")
        raw = "\n".join(parts).encode("ascii")
        page_streams.append(zlib.compress(raw))

    # 对象布局：
    # 1 catalog, 2 pages, 3 font-desc, 4 cidfont, 5 type0, 6 fontfile,
    # 7 tounicode, 然后每页 content + page
    objs: list[bytes] = []

    def add(obj: bytes) -> int:
        objs.append(obj)
        return len(objs)

    # 先占位，稍后回填
    # 我们按已知顺序构建
    n_pages = len(pages)
    # ids:
    # 1 Catalog
    # 2 Pages
    # 3 FontDescriptor
    # 4 CIDFont
    # 5 Type0 Font
    # 6 FontFile2
    # 7 ToUnicode
    # then for i in pages: content_i, page_i

    kids = []
    content_ids = []
    page_ids = []
    base = 8
    for i in range(n_pages):
        content_ids.append(base + i * 2)
        page_ids.append(base + i * 2 + 1)
        kids.append(f"{page_ids[-1]} 0 R")

    catalog = b"<< /Type /Catalog /Pages 2 0 R >>"
    pages_obj = (
        f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n_pages} >>"
    ).encode("ascii")
    font_desc = (
        b"<< /Type /FontDescriptor /FontName /AVDSub /Flags 4 "
        b"/FontBBox [0 -200 1000 900] /Ascent 800 /Descent -200 "
        b"/CapHeight 700 /StemV 80 /FontFile2 6 0 R >>"
    )
    cidfont = (
        f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /AVDSub "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        f"/FontDescriptor 3 0 R /W [{w_array}] /DW 500 /CIDToGIDMap /Identity >>"
    ).encode("ascii")
    type0 = (
        b"<< /Type /Font /Subtype /Type0 /BaseFont /AVDSub "
        b"/Encoding /Identity-H /DescendantFonts [4 0 R] /ToUnicode 7 0 R >>"
    )
    fontfile = (
        f"<< /Length {len(font_stream)} /Length1 {len(font_bytes)} /Filter /FlateDecode >>\n"
        f"stream\n".encode("ascii")
        + font_stream
        + b"\nendstream"
    )
    touni = (
        f"<< /Length {len(to_unicode_z)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
        + to_unicode_z
        + b"\nendstream"
    )

    objs = [catalog, pages_obj, font_desc, cidfont, type0, fontfile, touni]
    for i, stream in enumerate(page_streams):
        content = (
            f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Contents {content_ids[i]} 0 R "
            f"/Resources << /Font << /F1 5 0 R >> >> >>"
        ).encode("ascii")
        objs.append(content)
        objs.append(page)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(out)
