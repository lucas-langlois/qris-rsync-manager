"""Build the beginner tutorial PDF from WINDOWS_BEGINNER_TUTORIAL.md.

Run with the project environment after installing the packaging extras:

    .\envs\qris-rsync-manager\python.exe packaging\build_tutorial_pdf.py

The PDF is written to dist\QRIS_Rsync_Manager_Windows_Guide.pdf, beside the
executable, so both release files come from the same build folder.
"""
from pathlib import Path
import html
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, PageBreak,
    Preformatted, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "WINDOWS_BEGINNER_TUTORIAL.md"
OUTPUT = ROOT / "dist" / "QRIS_Rsync_Manager_Windows_Guide.pdf"
FONT_DIR = Path("C:/Windows/Fonts")
for name, filename in [("Guide", "segoeui.ttf"), ("GuideBold", "segoeuib.ttf"),
                       ("GuideItalic", "segoeuii.ttf"), ("GuideMono", "consola.ttf")]:
    pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename)))
pdfmetrics.registerFontFamily("Guide", normal="Guide", bold="GuideBold", italic="GuideItalic")

NAVY = colors.HexColor("#16324a")
TEAL = colors.HexColor("#087d83")
INK = colors.HexColor("#243747")
PALE = colors.HexColor("#eef5f7")
RULE = colors.HexColor("#cadadd")
PAGE_W, PAGE_H = A4
MARGIN = 43
WIDTH = PAGE_W - 2 * MARGIN
styles = getSampleStyleSheet()
styles.add(ParagraphStyle("GuideBody", fontName="Guide", fontSize=10.2, leading=14.2,
                         textColor=INK, spaceAfter=7))
styles.add(ParagraphStyle("GuideTitle", fontName="GuideBold", fontSize=31, leading=36,
                         textColor=NAVY, spaceBefore=13, spaceAfter=16))
styles.add(ParagraphStyle("GuideH2", fontName="GuideBold", fontSize=20, leading=25,
                         textColor=NAVY, spaceBefore=8, spaceAfter=13, keepWithNext=True))
styles.add(ParagraphStyle("GuideH3", fontName="GuideBold", fontSize=12, leading=16,
                         textColor=TEAL, spaceBefore=9, spaceAfter=7, keepWithNext=True))
styles.add(ParagraphStyle("GuideList", parent=styles["GuideBody"], leftIndent=17,
                         firstLineIndent=-17, spaceAfter=5))
styles.add(ParagraphStyle("GuideTable", fontName="Guide", fontSize=9.2, leading=12.4,
                         textColor=INK, spaceAfter=0))
styles.add(ParagraphStyle("GuideTableHead", parent=styles["GuideTable"], fontName="GuideBold",
                         textColor=colors.white))
styles.add(ParagraphStyle("GuideCode", fontName="GuideMono", fontSize=8.2, leading=10.7,
                         textColor=NAVY, spaceAfter=0))


def inline(text):
    chunks = []
    # Protect code fragments before applying prose markup.
    def code(match):
        chunks.append('<font name="GuideMono" size="9">' + html.escape(match[1]) + '</font>')
        return f"ZZCODE{len(chunks) - 1}ZZ"
    text = re.sub(r"`([^`]+)`", code, text)
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                  lambda m: f'<link href="{m[2]}" color="#087d83"><u>{m[1]}</u></link>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)
    for i, chunk in enumerate(chunks):
        text = text.replace(f"ZZCODE{i}ZZ", chunk)
    return text


class TutorialDoc(BaseDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "GuideH2":
            title = flowable.getPlainText()
            key = f"section-{len(self._bookmarks)}"
            self._bookmarks.append(key)
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, level=0)


def page_decoration(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(TEAL)
    canvas.rect(MARGIN, PAGE_H - 32, 23, 3, fill=1, stroke=0)
    canvas.setFont("GuideBold", 8)
    canvas.setFillColor(NAVY)
    canvas.drawString(MARGIN + 32, PAGE_H - 32, "QRIS RSYNC MANAGER  /  WINDOWS GUIDE")
    canvas.setStrokeColor(RULE)
    canvas.line(MARGIN, 34, PAGE_W - MARGIN, 34)
    canvas.setFont("Guide", 8)
    canvas.drawString(MARGIN, 21, "Setup and first transfers  |  v1.1.0  |  11 November 2026")
    canvas.drawRightString(PAGE_W - MARGIN, 21, str(doc.page))
    canvas.restoreState()


def build():
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    story = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line == "<!-- pagebreak -->":
            story.append(PageBreak())
            i += 1
            continue
        if line.startswith("```"):
            label = line[3:]
            i += 1
            block = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            style = ParagraphStyle("CodeAdjusted", parent=styles["GuideCode"])
            max_width = max(pdfmetrics.stringWidth(s, "GuideMono", style.fontSize) for s in block)
            if max_width > WIDTH - 20:
                style.fontSize *= (WIDTH - 20) / max_width
                style.leading = style.fontSize * 1.30
            if style.fontSize < 7.5:
                raise ValueError(f"Code needs shorter lines: {block}")
            code = Preformatted("\n".join(block), style)
            box = Table([[code]], colWidths=[WIDTH])
            box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), PALE),
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ]))
            story.extend([box, Spacer(1, 9)])
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [x.strip() for x in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            data = [[Paragraph(inline(c), styles["GuideTableHead" if ri == 0 else "GuideTable"])
                     for c in row] for ri, row in enumerate(rows)]
            table = Table(data, colWidths=[WIDTH * 0.33, WIDTH * 0.67], repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, NAVY),
            ]))
            story.extend([table, Spacer(1, 9)])
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            title = line[level:].strip()
            sty = "GuideTitle" if level == 1 else "GuideH2" if level == 2 else "GuideH3"
            story.append(Paragraph(inline(title), styles[sty]))
            i += 1
            continue
        list_match = re.match(r"^(\d+\. |[-] )(.*)", line)
        if list_match:
            prefix, text = list_match.groups()
            if prefix == "- ":
                prefix = "&#8226; "
            story.append(Paragraph(prefix + inline(text), styles["GuideList"]))
            i += 1
            continue
        paragraph = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||```|<!--|\d+\. |[-] )", lines[i]):
            paragraph.append(lines[i].strip())
            i += 1
        story.append(Paragraph(inline(" ".join(paragraph)), styles["GuideBody"]))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = TutorialDoc(str(OUTPUT), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                      topMargin=48, bottomMargin=45, title="QRIS Rsync Manager: Windows setup and first transfers",
                      author="QRIS Rsync Manager", pageCompression=1)
    doc._bookmarks = []
    frame = Frame(MARGIN, 45, WIDTH, PAGE_H - 93, leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id="guide", frames=frame, onPage=page_decoration))
    doc.build(story)
    print(OUTPUT)


if __name__ == "__main__":
    build()
