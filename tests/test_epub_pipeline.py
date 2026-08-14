import json
import base64
import tempfile
import unittest
import zipfile
from pathlib import Path

from epub_pipeline import (
    EPUB_MIMETYPE,
    CHAPTER_MARKER_RE,
    EpubError,
    convert_epub_to_markdown,
    export_markdown_to_epub,
    inspect_epub,
    restore_epub_chapter_markers,
    normalize_epub_image_wrapper_links,
    _normalize_epub_svg_cover_links,
    is_epub_markdown,
    is_epub_markdown_path,
    repair_epub_markdown_attributes,
    validate_epub,
)
from PB_layout import SourcePreviewProvider


CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="bookid">urn:isbn:1234567890</dc:identifier>
  <dc:title>Test Book</dc:title><dc:creator>Alice Author</dc:creator>
  <dc:language>en</dc:language><dc:publisher>Example Press</dc:publisher>
  <dc:rights>Copyright Alice</dc:rights>
 </metadata>
 <manifest>
  <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
  <item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
  <item id="css" href="styles/book.css" media-type="text/css"/>
  <item id="cover" href="images/cover.svg" media-type="image/svg+xml" properties="cover-image"/>
  <item id="pixel" href="images/pixel.png" media-type="image/png"/>
 </manifest>
 <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""

NAV = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><ol>
<li><a href="text/ch1.xhtml">First chapter</a></li>
<li><a href="text/ch2.xhtml">Second chapter</a></li>
</ol></nav></body></html>"""


def make_epub(path: Path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", EPUB_MIMETYPE, compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("EPUB/package.opf", OPF)
        archive.writestr("EPUB/nav.xhtml", NAV)
        archive.writestr("EPUB/styles/book.css", "body { font-family: serif; }")
        archive.writestr("EPUB/images/cover.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="30"><rect width="20" height="30" fill="blue"/></svg>')
        archive.writestr("EPUB/images/pixel.png", base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
        archive.writestr("EPUB/text/ch1.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>First chapter</h1><p>Hello <em>world</em>.</p><img src="../images/pixel.png" alt="Pixel"/><p id="note">Footnote text.</p></body></html>')
        archive.writestr("EPUB/text/ch2.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Second chapter</h1><p>Formula <math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi><mo>=</mo><mn>1</mn></math>.</p></body></html>')


class EpubPipelineTests(unittest.TestCase):
    def test_inspect_parse_export_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.epub"
            make_epub(source)
            book = inspect_epub(source)
            self.assertEqual(book.metadata["title"], "Test Book")
            self.assertEqual([item["title"] for item in book.chapters], ["First chapter", "Second chapter"])
            self.assertEqual(book.cover_href, "EPUB/images/cover.svg")

            output = root / "parsed"
            markdown_path = convert_epub_to_markdown(source, output)
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertEqual(len(CHAPTER_MARKER_RE.findall(markdown)), 2)
            self.assertIn("Hello *world*", markdown)
            image_match = __import__("re").search(r"!\[Pixel\]\((images/[^)]+\.png)\)", markdown)
            self.assertIsNotNone(image_match)
            self.assertTrue((output / image_match.group(1)).is_file())
            self.assertNotIn("original-image-src", markdown)
            metadata = json.loads((output / "epub_source.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["metadata"]["publisher"], "Example Press")
            self.assertTrue((output / source.name).is_file())

            translated = output / "full.zh.md"
            translated.write_text(markdown.replace("Hello", "你好").replace("Second chapter", "第二章"), encoding="utf-8")
            exported = root / "translated.epub"
            warnings = export_markdown_to_epub(translated, exported, target_language="zh-CN", translated=True)
            self.assertIsInstance(warnings, list)
            report = validate_epub(exported)
            self.assertTrue(report["valid"], report)
            exported_book = inspect_epub(exported)
            self.assertEqual(exported_book.metadata["language"], "zh-CN")
            self.assertIn("Alice Author", exported_book.metadata["authors"])
            self.assertNotEqual(exported_book.metadata["identifier"], "urn:isbn:1234567890")

    def test_original_preview_embeds_epub_images(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.epub"
            make_epub(source)
            preview = SourcePreviewProvider(Path(__file__).resolve().parents[1]).convert_original_with_pandoc(source)
            self.assertIsNotNone(preview)
            text = preview.read_text(encoding="utf-8", errors="replace")
            self.assertIn("litmtrans-epub-original-preview-v2-embedded-resources", text)
            self.assertIn("data:image/png;base64,", text)
            self.assertNotIn('src="../images/pixel.png"', text)

    def test_rejects_path_traversal_member(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.epub"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("mimetype", EPUB_MIMETYPE, compress_type=zipfile.ZIP_STORED)
                archive.writestr("../escape", b"bad")
            with self.assertRaises(EpubError):
                inspect_epub(path)

    def test_rejects_drm_rights_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "drm.epub"
            make_epub(path)
            rewritten = Path(temp) / "rewritten.epub"
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(rewritten, "w") as target:
                for item in source.infolist():
                    target.writestr(item, source.read(item.filename))
                target.writestr("META-INF/rights.xml", "<rights/>")
            with self.assertRaisesRegex(EpubError, "DRM"):
                inspect_epub(rewritten)

    def test_validator_reports_missing_xhtml_resource(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "broken.epub"
            make_epub(path)
            rewritten = Path(temp) / "broken-links.epub"
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(rewritten, "w") as target:
                for item in source.infolist():
                    data = source.read(item.filename)
                    if item.filename == "EPUB/text/ch1.xhtml":
                        data = data.replace(b"</body>", b'<img src="missing.png"/></body>')
                    target.writestr(item, data)
            report = validate_epub(rewritten)
            self.assertFalse(report["valid"])
            self.assertTrue(any("missing.png" in error for error in report["errors"]))

    def test_validator_does_not_fail_on_source_only_anchor_wrappers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.epub"
            make_epub(source)
            parsed = root / "parsed"
            markdown = convert_epub_to_markdown(source, parsed)
            text = markdown.read_text(encoding="utf-8")
            text += '\n[旧图链接](missing-image.jpg.id-123.wrap-0.html.html "linked image")\n'
            markdown.write_text(text, encoding="utf-8")
            output = root / "anchor-warning.epub"
            warnings = export_markdown_to_epub(markdown, output)
            self.assertTrue(validate_epub(output)["valid"])
            self.assertTrue(any("锚点链接失效" in warning for warning in warnings))

    def test_restores_chapter_markers_dropped_by_translator(self):
        marker1 = '<!-- LITMTRANS_EPUB_CHAPTER {"index":1,"idref":"c1","href":"c1.xhtml","path":"c1.xhtml","title":"One"} -->'
        marker2 = '<!-- LITMTRANS_EPUB_CHAPTER {"index":2,"idref":"c2","href":"c2.xhtml","path":"c2.xhtml","title":"Two"} -->'
        source = f"{marker1}\n\n# One\n\nText\n\n{marker2}\n\n# Two\n\nText"
        translated = "# 一\n\n译文\n\n# 二\n\n译文"
        restored = restore_epub_chapter_markers(source, translated)
        self.assertEqual(len(CHAPTER_MARKER_RE.findall(restored)), 2)
        second_marker = list(CHAPTER_MARKER_RE.finditer(restored))[1]
        self.assertLess(second_marker.start(), restored.index("# 二"))

    def test_normalizes_gutenberg_image_wrapper_links(self):
        source = '[![Illo](images/thumb.jpg)](5528502404215350175_i001.jpg.id-123.wrap-0.html.html "linked image")'
        normalized = normalize_epub_image_wrapper_links(source)
        self.assertEqual(normalized, '[![Illo](images/thumb.jpg)](images/5528502404215350175_i001.jpg)')

    def test_repairs_dangling_gutenberg_title_attributes(self):
        source = '<!-- LITMTRANS_EPUB_CHAPTER {"index":1} -->\n# 原标题 {#pg-title-no-subtitle lang="zh"} {#pg-header-heading title=""}'
        repaired = repair_epub_markdown_attributes(source)
        self.assertIn('# [原标题]{#pg-title-no-subtitle lang="zh"} {#pg-header-heading title=""}', repaired)
        self.assertNotIn('# 原标题 {#pg-title-no-subtitle', repaired)
        self.assertTrue(is_epub_markdown(source))

    def test_identifies_epub_parsed_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "epub_source.json").write_text('{"format":"epub"}', encoding="utf-8")
            self.assertTrue(is_epub_markdown_path(folder / "full.cleaned.md"))
            other = folder / "other"
            other.mkdir()
            self.assertFalse(is_epub_markdown_path(other / "other.md"))

    def test_replaces_svg_image_wrapper_with_raster_asset(self):
        with tempfile.TemporaryDirectory() as temp:
            images = Path(temp) / "images"
            images.mkdir()
            (images / "cover.svg").write_text('<svg><image xlink:href="cover.jpeg"/></svg>', encoding="utf-8")
            (images / "cover.jpeg").write_bytes(b"jpeg")
            result = _normalize_epub_svg_cover_links("![](images/cover.svg)", images)
            self.assertEqual(result, "![](images/cover.jpeg)")


if __name__ == "__main__":
    unittest.main()
