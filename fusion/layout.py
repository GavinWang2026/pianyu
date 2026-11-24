# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""统一版式描述（LayoutSpec）—— 一套 IR 同时渲染 Markdown / DOCX / PDF。

R3 交付级文档：封面、目录、章节分页、页眉页脚语义由模板约定；
渲染器各自兑现（MD 用标题锚点，DOCX 用 PAGE 域 + page break，PDF 用强制分页 + 页脚）。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Iterable, Literal
from xml.sax.saxutils import escape as xml_escape

BlockKind = Literal[
    "cover_brand",
    "cover_title",
    "cover_meta",
    "heading",
    "para",
    "toc_item",
    "rule",
    "page_break",
    "blank",
]

PAGEBREAK_SENTINEL = "<<<AVD_PAGEBREAK>>>"


@dataclass
class Block:
    kind: BlockKind
    text: str = ""
    level: int = 1  # heading 1–3；toc 序号用 level 存索引可选
    anchor: str = ""


@dataclass
class LayoutSpec:
    """版式模板：控制封面文案、页脚、是否章节分页。"""
    name: str = "delivery"
    brand: str = "AVD Flagship 知识稿"
    footer_prefix: str = "AVD Flagship"
    chapter_page_break: bool = True
    include_cover: bool = True
    include_toc: bool = True


@dataclass
class LayoutDoc:
    title: str
    blocks: list[Block] = field(default_factory=list)
    spec: LayoutSpec = field(default_factory=LayoutSpec)
    stamp: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "stamp": self.stamp,
            "summary": self.summary,
            "keywords": list(self.keywords),
            "spec": asdict(self.spec),
            "blocks": [asdict(b) for b in self.blocks],
        }


TEMPLATES: dict[str, LayoutSpec] = {
    "delivery": LayoutSpec(
        name="delivery",
        brand="AVD Flagship 知识稿",
        footer_prefix="AVD Flagship",
        chapter_page_break=True,
        include_cover=True,
        include_toc=True,
    ),
    "compact": LayoutSpec(
        name="compact",
        brand="AVD Flagship",
        footer_prefix="AVD",
        chapter_page_break=False,
        include_cover=True,
        include_toc=True,
    ),
    "plain": LayoutSpec(
        name="plain",
        brand="",
        footer_prefix="AVD Flagship",
        chapter_page_break=False,
        include_cover=False,
        include_toc=False,
    ),
}


def resolve_template(name: str | None = None) -> LayoutSpec:
    key = (name or "delivery").strip().lower() or "delivery"
    return TEMPLATES.get(key, TEMPLATES["delivery"])


def _strip_front_matter(body: str) -> str:
    text = body or ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            return text[end + 4:].lstrip("\n")
    return text


def build_booklet(
    collection: str,
    items: list[dict[str, Any]],
    *,
    template: str | LayoutSpec = "delivery",
    stamp: str | None = None,
) -> LayoutDoc:
    """合集材料 → 带封面/目录/章节分页的 LayoutDoc。"""
    spec = template if isinstance(template, LayoutSpec) else resolve_template(template)
    when = stamp or datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"{collection} · 合集成册"
    blocks: list[Block] = []

    if spec.include_cover:
        if spec.brand:
            blocks.append(Block("cover_brand", spec.brand))
        blocks.append(Block("cover_title", title))
        blocks.append(Block("cover_meta", f"由 AVD Flagship 生成 · {when} · 共 {len(items)} 篇"))
        blocks.append(Block("blank"))
        if spec.chapter_page_break:
            blocks.append(Block("page_break"))

    if spec.include_toc:
        blocks.append(Block("heading", "目录", level=1))
        for i, it in enumerate(items, 1):
            tid = str(it.get("id") or i)
            blocks.append(Block(
                "toc_item",
                str(it.get("title") or f"第 {i} 篇"),
                level=i,
                anchor=f"{i}-{tid}",
            ))
        blocks.append(Block("blank"))
        if spec.chapter_page_break:
            blocks.append(Block("page_break"))

    for i, it in enumerate(items, 1):
        tid = str(it.get("id") or i)
        chap_title = f"{i}. {it.get('title') or '未命名'}"
        blocks.append(Block("heading", chap_title, level=1, anchor=f"{i}-{tid}"))
        tags = it.get("tags") or []
        if isinstance(tags, list) and tags:
            blocks.append(Block("para", f"标签：{'、'.join(str(t) for t in tags)}"))
        body = _strip_front_matter(str(it.get("body") or ""))
        paras = [p.strip() for p in body.split("\n") if p.strip()]
        if not paras:
            blocks.append(Block("para", "（无正文）"))
        else:
            for para in paras[:200]:
                blocks.append(Block("para", para[:2000]))
        blocks.append(Block("rule"))
        if spec.chapter_page_break and i < len(items):
            blocks.append(Block("page_break"))

    return LayoutDoc(
        title=title,
        blocks=blocks,
        spec=spec,
        stamp=when,
        summary=f"合集「{collection}」共 {len(items)} 篇",
        keywords=[collection] if collection else [],
    )


def build_from_knowledge(
    meta: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    summary: str = "",
    keywords: Iterable[str] = (),
    template: str | LayoutSpec = "delivery",
    groups: list[dict[str, Any]] | None = None,
) -> LayoutDoc:
    """单篇知识稿 → LayoutDoc（供 DOCX/PDF 同源渲染）。"""
    spec = template if isinstance(template, LayoutSpec) else resolve_template(template)
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = str(meta.get("title") or "知识稿")
    blocks: list[Block] = []

    if spec.include_cover:
        if spec.brand:
            blocks.append(Block("cover_brand", spec.brand))
        blocks.append(Block("cover_title", title))
        blocks.append(Block("cover_meta", f"生成器：AVD Flagship · {when}"))
        blocks.append(Block("blank"))

    if summary:
        blocks.append(Block("heading", "摘要", level=1))
        blocks.append(Block("para", str(summary)))

    kws = [str(k).strip() for k in keywords if str(k).strip()]
    if kws:
        blocks.append(Block("para", "关键词：" + "、".join(kws)))

    chapters = [c for c in (meta.get("chapters") or []) if isinstance(c, dict)]
    if spec.include_toc and chapters:
        blocks.append(Block("heading", "目录", level=1))
        for i, ch in enumerate(chapters[:32], 1):
            blocks.append(Block("toc_item", str(ch.get("title") or f"章节 {i}"), level=i))
        blocks.append(Block("blank"))

    chapter_groups = groups
    if chapter_groups is None:
        chapter_groups = [{"title": "正文", "segments": segments}]

    for gi, group in enumerate(chapter_groups):
        gtitle = str(group.get("title") or "正文")
        if spec.chapter_page_break and gi > 0:
            blocks.append(Block("page_break"))
        blocks.append(Block("heading", gtitle, level=2))
        for seg in group.get("segments") or []:
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            sp = str(seg.get("speaker") or "").strip()
            if sp and str(seg.get("speaker_source") or "") == "heuristic":
                text = f"[{sp}·未校准] {text}"
            elif sp:
                text = f"[{sp}] {text}"
            blocks.append(Block("para", text))

    return LayoutDoc(
        title=title,
        blocks=blocks,
        spec=spec,
        stamp=when,
        summary=str(summary or ""),
        keywords=kws,
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(doc: LayoutDoc) -> str:
    lines: list[str] = []
    for b in doc.blocks:
        if b.kind == "cover_brand":
            lines.append(f"# {b.text}")
            lines.append("")
        elif b.kind == "cover_title":
            if not any(x.startswith("# ") for x in lines[:3]):
                lines.append(f"# {b.text}")
            else:
                lines.append(f"## {b.text}")
            lines.append("")
        elif b.kind == "cover_meta":
            lines.append(f"> {b.text}")
            lines.append("")
        elif b.kind == "heading":
            # 合册章节 / 目录用 ##；子节用 ###
            level = 2 if b.level <= 1 else min(3, b.level + 1)
            prefix = "#" * level
            if b.anchor:
                lines.append(f"{prefix} {b.text} {{#{b.anchor}}}")
            else:
                lines.append(f"{prefix} {b.text}")
            lines.append("")
        elif b.kind == "toc_item":
            if b.anchor:
                lines.append(f"{b.level}. [{b.text}](#{b.anchor})")
            else:
                lines.append(f"{b.level}. {b.text}")
        elif b.kind == "para":
            lines.append(b.text)
            lines.append("")
        elif b.kind == "rule":
            lines.append("---")
            lines.append("")
        elif b.kind == "page_break":
            lines.append("")
        elif b.kind == "blank":
            lines.append("")
    lines.append(f"*{doc.spec.footer_prefix} · {doc.title} · {doc.stamp}*")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 中间行（供 PDF / 兼容旧 render_cjk_pdf）
# ---------------------------------------------------------------------------

def to_pdf_lines(doc: LayoutDoc) -> list[str]:
    """转成 pdf_cjk 可消费的 markdown-ish 行；强制分页用 sentinel。"""
    lines: list[str] = []
    for b in doc.blocks:
        if b.kind == "cover_brand":
            lines.append(f"# {b.text}")
        elif b.kind == "cover_title":
            lines.append(f"# {b.text}")
        elif b.kind == "cover_meta":
            lines.append(b.text)
            lines.append("")
        elif b.kind == "heading":
            if b.level >= 2:
                lines.append(f"## {b.text}")
            else:
                lines.append(f"# {b.text}")
        elif b.kind == "toc_item":
            lines.append(f"{b.level}. {b.text}")
        elif b.kind == "para":
            lines.append(b.text)
            lines.append("")
        elif b.kind == "rule":
            lines.append("---")
        elif b.kind == "page_break":
            lines.append(PAGEBREAK_SENTINEL)
        elif b.kind == "blank":
            lines.append("")
    return lines


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def _docx_p(
    text: str,
    *,
    style: str = "",
    align: str = "",
    italic: bool = False,
) -> str:
    """按样式名出段落；字号/间距/字体全部交给 styles.xml，不再写死内联。"""
    ppr: list[str] = []
    if style:
        ppr.append(f'<w:pStyle w:val="{style}"/>')
    if align:
        ppr.append(f'<w:jc w:val="{align}"/>')
    ppr_xml = f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else ""
    rpr_xml = "<w:rPr><w:i/></w:rPr>" if italic else ""
    return (
        f"<w:p>{ppr_xml}<w:r>{rpr_xml}"
        f"<w:t xml:space=\"preserve\">{xml_escape(text)}</w:t></w:r></w:p>"
    )


def _docx_toc_entry(text: str, level: int) -> str:
    """目录条目：TOC1–3 样式 + 右对齐点线制表位（页码位）。"""
    style = f"TOC{min(3, max(1, level))}"
    return (
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        f'<w:r><w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r>'
        '<w:r><w:tab/><w:t xml:space="preserve"></w:t></w:r></w:p>'
    )


def _docx_toc_field(entries: list[Any]) -> str:
    """真正的 TOC 域：Word 里 F9 / 打开即更新，得到带页码的自动目录。

    第九轮 P2：此前目录只是若干行静态文字，Word 导航窗格与「引用→目录」
    都不认；现在给出 ``TOC \\o "1-3" \\h \\z \\u`` 域，并用 Heading 样式
    的标题作为条目源。
    """
    begin = (
        '<w:p><w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r></w:p>'
    )
    body = "".join(_docx_toc_entry(b.text, b.level or 1) for b in entries)
    end = '<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    return begin + body + end


_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault><w:rPr>
      <w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei"/>
      <w:sz w:val="22"/><w:szCs w:val="22"/>
    </w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr>
      <w:spacing w:before="0" w:after="120" w:line="300" w:lineRule="auto"/>
    </w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/><w:qFormat/>
    <w:pPr><w:jc w:val="both"/></w:pPr>
    <w:rPr><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:jc w:val="center"/><w:spacing w:before="240" w:after="240"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="48"/><w:szCs w:val="48"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:jc w:val="center"/><w:spacing w:after="360"/></w:pPr>
    <w:rPr><w:i/><w:color w:val="595959"/><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="360" w:after="200"/>
      <w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="280" w:after="160"/>
      <w:outlineLvl w:val="1"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/>
      <w:outlineLvl w:val="2"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Caption">
    <w:name w:val="caption"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:jc w:val="center"/><w:spacing w:after="200"/></w:pPr>
    <w:rPr><w:color w:val="767171"/><w:sz w:val="18"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="TOCHeading">
    <w:name w:val="TOC Heading"/><w:basedOn w:val="Heading1"/>
    <w:qFormat/><w:pPr><w:outlineLvl w:val="9"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="TOC1">
    <w:name w:val="toc 1"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:tabs><w:tab w:val="right" w:leader="dot" w:pos="9350"/></w:tabs>
      <w:spacing w:before="120" w:after="40"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="TOC2">
    <w:name w:val="toc 2"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:tabs><w:tab w:val="right" w:leader="dot" w:pos="9350"/></w:tabs>
      <w:ind w:left="420"/><w:spacing w:after="40"/></w:pPr>
    <w:rPr><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="TOC3">
    <w:name w:val="toc 3"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:tabs><w:tab w:val="right" w:leader="dot" w:pos="9350"/></w:tabs>
      <w:ind w:left="840"/><w:spacing w:after="40"/></w:pPr>
    <w:rPr><w:sz w:val="20"/></w:rPr>
  </w:style>
</w:styles>"""

_SETTINGS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:updateFields w:val="true"/>
  <w:defaultTabStop w:val="420"/>
  <w:compat><w:compatSetting w:name="compatibilityMode"
    w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>
</w:settings>"""


def _docx_page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def render_docx(doc: LayoutDoc) -> bytes:
    body: list[str] = []
    blocks = list(doc.blocks)
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind == "toc_item":
            j = i
            entries: list[Any] = []
            while j < len(blocks) and blocks[j].kind == "toc_item":
                entries.append(blocks[j])
                j += 1
            body.append(_docx_toc_field(entries))
            i = j
            continue
        if b.kind == "cover_brand":
            body.append(_docx_p(b.text, style="Caption"))
        elif b.kind == "cover_title":
            body.append(_docx_p(b.text, style="Title"))
        elif b.kind == "cover_meta":
            body.append(_docx_p(b.text, style="Subtitle", italic=True))
        elif b.kind == "heading":
            if b.text.strip() in ("目录", "Contents") and not getattr(b, "anchor", ""):
                # 目录标题本身不进目录（outlineLvl=9），避免自引用
                body.append(_docx_p(b.text, style="TOCHeading"))
            else:
                style = "Heading1" if b.level <= 1 else (
                    "Heading2" if b.level == 2 else "Heading3")
                body.append(_docx_p(b.text, style=style))
        elif b.kind == "para":
            body.append(_docx_p(b.text))
        elif b.kind == "rule":
            body.append(_docx_p("—", style="Caption"))
        elif b.kind == "page_break":
            body.append(_docx_page_break())
        elif b.kind == "blank":
            body.append(_docx_p(""))
        i += 1

    stamp = doc.stamp or datetime.now().strftime("%Y-%m-%d %H:%M")
    footer_prefix = doc.spec.footer_prefix or "AVD Flagship"
    sect = (
        "<w:sectPr>"
        '<w:footerReference w:type="default" r:id="rIdFooter1"/>'
        '<w:headerReference w:type="default" r:id="rIdHeader1"/>'
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1418" w:bottom="1440" w:left="1418"'
        ' w:header="851" w:footer="992" w:gutter="0"/>'
        "</w:sectPr>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{''.join(body)}{sect}</w:body></w:document>"
    )
    footer_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:p><w:pPr><w:jc w:val=\"center\"/></w:pPr>"
        f"<w:r><w:rPr><w:sz w:val=\"18\"/></w:rPr>"
        f"<w:t xml:space=\"preserve\">{xml_escape(footer_prefix)} · </w:t></w:r>"
        "<w:r><w:rPr><w:sz w:val=\"18\"/></w:rPr><w:fldChar w:fldCharType=\"begin\"/></w:r>"
        "<w:r><w:rPr><w:sz w:val=\"18\"/></w:rPr><w:instrText xml:space=\"preserve\"> PAGE </w:instrText></w:r>"
        "<w:r><w:rPr><w:sz w:val=\"18\"/></w:rPr><w:fldChar w:fldCharType=\"end\"/></w:r>"
        "<w:r><w:rPr><w:sz w:val=\"18\"/></w:rPr><w:t xml:space=\"preserve\"> / </w:t></w:r>"
        "<w:r><w:rPr><w:sz w:val=\"18\"/></w:rPr><w:fldChar w:fldCharType=\"begin\"/></w:r>"
        "<w:r><w:rPr><w:sz w:val=\"18\"/></w:rPr><w:instrText xml:space=\"preserve\"> NUMPAGES </w:instrText></w:r>"
        "<w:r><w:rPr><w:sz w:val=\"18\"/></w:rPr><w:fldChar w:fldCharType=\"end\"/></w:r>"
        f"<w:r><w:rPr><w:sz w:val=\"18\"/></w:rPr>"
        f"<w:t xml:space=\"preserve\"> · {xml_escape(stamp)}</w:t></w:r>"
        "</w:p></w:ftr>"
    )
    header_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:p><w:pPr><w:jc w:val=\"right\"/></w:pPr>"
        f"<w:r><w:rPr><w:sz w:val=\"16\"/><w:i/></w:rPr>"
        f"<w:t xml:space=\"preserve\">{xml_escape(doc.title[:80])}</w:t></w:r>"
        "</w:p></w:hdr>"
    )
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
  <Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    doc_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rIdSettings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
  <Relationship Id="rIdFooter1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
  <Relationship Id="rIdHeader1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
</Relationships>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/styles.xml", _STYLES_XML)
        zf.writestr("word/settings.xml", _SETTINGS_XML)
        zf.writestr("word/footer1.xml", footer_xml)
        zf.writestr("word/header1.xml", header_xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def render_pdf(doc: LayoutDoc) -> bytes | None:
    """CJK PDF；无字体时返回 None（调用方回退）。"""
    from . import pdf_cjk
    lines = to_pdf_lines(doc)
    return pdf_cjk.render_cjk_pdf(doc.title, lines)


def render_all(
    doc: LayoutDoc,
    *,
    want_md: bool = True,
    want_docx: bool = True,
    want_pdf: bool = True,
) -> dict[str, Any]:
    """一次渲染多格式；缺字体时 pdf 键为 None 并带 pdf_error。"""
    out: dict[str, Any] = {"title": doc.title, "stamp": doc.stamp, "spec": doc.spec.name}
    if want_md:
        out["md"] = render_markdown(doc)
    if want_docx:
        out["docx"] = render_docx(doc)
    if want_pdf:
        try:
            pdf = render_pdf(doc)
            out["pdf"] = pdf
            if pdf is None:
                out["pdf_error"] = "无可用 CJK 字体"
        except Exception as exc:  # noqa: BLE001
            out["pdf"] = None
            out["pdf_error"] = str(exc)[:200]
    return out
