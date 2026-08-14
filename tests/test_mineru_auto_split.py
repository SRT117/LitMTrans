from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pypdfium2 as pdfium

from LS_pipeline import (
    choose_mineru_split_end,
    merge_mineru_part_results,
    mineru_result_organization_message,
    rebase_page_indices,
    temporary_mineru_upload_parts,
)


class MinerUAutoSplitTests(unittest.TestCase):
    def test_progress_message_reflects_whether_upload_was_split(self):
        self.assertEqual(
            mineru_result_organization_message(1),
            "正在整理 MinerU 单文件解析结果与资源文件...",
        )
        self.assertEqual(
            mineru_result_organization_message(2),
            "正在合并 MinerU 分片结果并重建全局页码与资源编号...",
        )

    def test_boundary_prefers_new_section_without_exceeding_limit(self):
        texts = [
            "body",
            "sentence ends.",
            "2 Methods\nDetails",
            "more body",
            "tail",
        ]

        self.assertEqual(choose_mineru_split_end(texts, 0, 4, search_window=3), 2)

    def test_boundary_never_increases_the_minimum_part_count(self):
        texts = ["1 Introduction", "2 Methods", "3 Results", "4 Conclusion"]

        self.assertEqual(choose_mineru_split_end(texts, 0, 2, search_window=2), 2)

    def test_pdf_parts_preserve_all_pages_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            document = pdfium.PdfDocument.new()
            try:
                for _ in range(5):
                    document.new_page(612, 792)
                document.save(str(source))
            finally:
                document.close()

            with temporary_mineru_upload_parts(source, page_limit=3) as parts:
                self.assertEqual(
                    [(part.start_page, part.end_page) for part in parts],
                    [(1, 3), (4, 5)],
                )
                self.assertEqual(
                    [len(pdfium.PdfDocument(str(part.path))) for part in parts],
                    [3, 2],
                )

    def test_merge_rebases_pages_and_namespaces_same_named_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            part_results = []
            for index, image_bytes in enumerate((b"first-image", b"second-image"), start=1):
                extract_dir = root / f"extract-{index}"
                images_dir = extract_dir / "images"
                images_dir.mkdir(parents=True)
                (images_dir / "shared.jpg").write_bytes(image_bytes)
                (extract_dir / "layout.json").write_text(
                    json.dumps(
                        {
                            "pdf_info": [
                                {
                                    "page_idx": 0,
                                    "page_size": [600, 800],
                                    "preproc_blocks": [
                                        {
                                            "type": "image",
                                            "lines": [
                                                {
                                                    "spans": [
                                                        {
                                                            "type": "image",
                                                            "image_path": "shared.jpg",
                                                        }
                                                    ]
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
                (extract_dir / f"part-{index}_model.json").write_text(
                    json.dumps([{"page_idx": 0, "label": f"part-{index}"}]),
                    encoding="utf-8",
                )
                (extract_dir / f"part-{index}_content_list_v2.json").write_text(
                    json.dumps([[{"type": "paragraph", "content": {"text": f"part-{index}"}}]]),
                    encoding="utf-8",
                )
                part_results.append(
                    {
                        "start_page": index,
                        "end_page": index,
                        "markdown": f"Equation ({index})\n\n![figure](images/shared.jpg)\n",
                        "extract_dir": extract_dir,
                    }
                )

            raw, cleaned, records, extract_root = merge_mineru_part_results(
                part_results,
                output_dir,
                lambda _message: None,
            )

            self.assertIn("Equation (1)", raw)
            self.assertIn("Equation (2)", raw)
            self.assertIn("![IMAGE_001](images/image_001.jpg)", cleaned)
            self.assertIn("![IMAGE_002](images/image_002.jpg)", cleaned)
            self.assertEqual((output_dir / "images" / "image_001.jpg").read_bytes(), b"first-image")
            self.assertEqual((output_dir / "images" / "image_002.jpg").read_bytes(), b"second-image")
            self.assertEqual((extract_root / "images" / "image_001.jpg").read_bytes(), b"first-image")
            self.assertEqual((extract_root / "images" / "image_002.jpg").read_bytes(), b"second-image")
            self.assertEqual([record["id"] for record in records], ["IMAGE_001", "IMAGE_002"])

            layout = json.loads((extract_root / "layout.json").read_text(encoding="utf-8"))
            self.assertEqual([page["page_idx"] for page in layout["pdf_info"]], [0, 1])
            self.assertEqual(
                [
                    page["preproc_blocks"][0]["lines"][0]["spans"][0]["image_path"]
                    for page in layout["pdf_info"]
                ],
                ["image_001.jpg", "image_002.jpg"],
            )
            model = json.loads((extract_root / "merged_model.json").read_text(encoding="utf-8"))
            self.assertEqual([page["page_idx"] for page in model], [0, 1])
            content = json.loads(
                (extract_root / "merged_content_list_v2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(content), 2)

    def test_rebase_changes_only_internal_page_indices(self):
        payload = {
            "page_idx": 0,
            "page_index": 1,
            "page_number": 7,
            "text": "Figure 1 and Equation (2)",
        }

        self.assertEqual(
            rebase_page_indices(payload, 200),
            {
                "page_idx": 200,
                "page_index": 201,
                "page_number": 7,
                "text": "Figure 1 and Equation (2)",
            },
        )


if __name__ == "__main__":
    unittest.main()
