import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

import LS_pipeline
import PB_layout


def make_layout_fixture(tmp_path: Path) -> Path:
    markdown_path = tmp_path / "full.cleaned.md"
    markdown_path.write_text("![IMAGE_001](images/image_001.jpg)\n", encoding="utf-8")
    (tmp_path / "image_map.json").write_text(
        json.dumps(
            [
                {
                    "id": "IMAGE_001",
                    "original_target": "images/source-hash.jpg",
                    "clean_target": "images/image_001.jpg",
                    "saved_file": str(tmp_path / "images" / "image_001.jpg"),
                }
            ]
        ),
        encoding="utf-8",
    )
    mineru_result = tmp_path / "mineru_result"
    mineru_result.mkdir()
    (mineru_result / "layout.json").write_text(
        json.dumps(
            {
                "pdf_info": [
                    {
                        "page_size": [600, 800],
                        "preproc_blocks": [
                            {
                                "type": "image",
                                "bbox": [60, 100, 210, 250],
                                "blocks": [
                                    {
                                        "type": "image_body",
                                        "bbox": [60, 100, 210, 250],
                                        "lines": [
                                            {
                                                "spans": [
                                                    {
                                                        "type": "image",
                                                        "bbox": "60 100 210 250",
                                                        "image_path": "source-hash.jpg",
                                                    }
                                                ]
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return markdown_path


class ImageLayoutSizingTests(unittest.TestCase):
    def test_stream_image_width_uses_source_page_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            markdown_path = make_layout_fixture(Path(directory))

            widths = PB_layout.layout_image_width_percentages(markdown_path)
            rendered = PB_layout.normalize_markdown_for_export(
                markdown_path.read_text(encoding="utf-8"),
                None,
                widths,
            )

        self.assertEqual(widths["images/image_001.jpg"], 25.0)
        self.assertEqual(rendered, "![](images/image_001.jpg){width=25%}\n")

    def test_body_merge_rejects_new_figure_intrusion(self):
        left_of_figure = {
            "bbox": [100, 300, 360, 370],
            "kind": "text",
            "debug_role": "body_candidate",
        }
        below_figure = [100, 375, 500, 460]

        self.assertTrue(
            PB_layout.body_merge_creates_barrier_intrusion(
                [left_of_figure],
                below_figure,
                [[390, 290, 500, 365]],
            )
        )

    def test_body_stream_beside_figure_is_not_expanded_to_column_width(self):
        stream = {
            "bbox": [100, 300, 360, 390],
            "debug_role": "merged_body",
            "column_key": "left",
            "items": [{"kind": "text", "debug_role": "merged_body", "column_key": "left"}],
        }

        PB_layout.expand_narrow_text_stream_to_column(
            stream,
            612.0,
            {"left": 500.0, "left_left": 100.0},
            [[390, 320, 500, 370]],
        )

        self.assertEqual(stream["bbox"], [100, 300, 360, 390])

    def test_export_width_falls_back_when_layout_metadata_is_missing(self):
        rendered = PB_layout.normalize_markdown_for_export(
            "![figure](images/unmapped.jpg)\n",
            "45%",
            {},
        )

        self.assertEqual(rendered, "![figure](images/unmapped.jpg){width=45%}\n")

    def test_word_export_separates_an_image_from_adjacent_body_text(self):
        with tempfile.TemporaryDirectory() as directory:
            markdown = "资料图。3. ![figure](images/figure.png){width=45%}图后说明继续。\n"
            markdown_path = Path(directory) / "full.cleaned.md"
            markdown_path.write_text(markdown, encoding="utf-8")

            export_path, _ = PB_layout.make_word_export_markdown(markdown_path)
            rendered = export_path.read_text(encoding="utf-8")

        self.assertEqual(rendered, "资料图。3. \n\n![figure](images/figure.png){width=45%}\n\n图后说明继续。\n")

    def test_export_html_embeds_local_images_for_portability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_dir = root / "images"
            image_dir.mkdir()
            image_bytes = b"\x89PNG\r\n\x1a\nportable-image"
            (image_dir / "figure.png").write_bytes(image_bytes)
            html_path = root / "export.html"
            html_path.write_text('<img src="images/figure.png" alt="figure">', encoding="utf-8")

            embedded = PB_layout.inline_local_images_in_html(html_path, root)
            rendered = html_path.read_text(encoding="utf-8")

        self.assertEqual(embedded, 1)
        self.assertIn("src=\"data:image/png;base64,iVBORw0KGgpwb3J0YWJsZS1pbWFnZQ==\"", rendered)

    def test_docx_postprocess_never_enlarges_source_sized_images(self):
        root = ET.fromstring(
            """
            <root xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
                  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <wp:extent cx="1000" cy="500"/>
              <a:ext cx="1000" cy="500"/>
              <wp:extent cx="3000" cy="1500"/>
              <a:ext cx="3000" cy="1500"/>
            </root>
            """
        )

        LS_pipeline.set_docx_image_width(root, target_cx=2000)
        wp_extents = root.findall(".//wp:extent", LS_pipeline.OOXML_NS)
        graphic_extents = root.findall(".//a:ext", LS_pipeline.OOXML_NS)

        expected = [("1000", "500"), ("2000", "1000")]
        self.assertEqual([(item.get("cx"), item.get("cy")) for item in wp_extents], expected)
        self.assertEqual([(item.get("cx"), item.get("cy")) for item in graphic_extents], expected)
