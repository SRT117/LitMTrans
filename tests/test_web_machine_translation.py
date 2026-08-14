import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import machine_translate as mt


class BingWebTranslationTests(unittest.TestCase):
    def test_bing_request_includes_translator_referrer(self):
        translator = mt.WebMachineTranslator(mt.BING_PROVIDER, "简体中文", source_language="英文")
        with (
            patch.object(translator, "_bing_sid", return_value=("https://www.bing.com/", "ig", "iid", "key", "token")),
            patch.object(translator, "_open_text", return_value=json.dumps([{"translations": [{"text": "译文"}]}])) as open_text,
        ):
            self.assertEqual(translator._translate_bing_once("source"), "译文")

        request = open_text.call_args.args[0]
        self.assertEqual(request.get_header("Referer"), "https://www.bing.com/translator")
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")

    def test_network_machine_translation_uses_the_combined_provider(self):
        self.assertTrue(mt.is_machine_translation_provider(mt.MACHINE_TRANSLATION_PROVIDER))
        self.assertTrue(mt.is_machine_translation_provider(mt.EDGE_LOCAL_PROVIDER))
        self.assertFalse(mt.is_machine_translation_provider(mt.GOOGLE_PROVIDER))
        self.assertFalse(mt.is_machine_translation_provider(mt.BING_PROVIDER))
        self.assertEqual(mt.normalize_network_provider_id(mt.BING_PROVIDER), mt.MACHINE_TRANSLATION_PROVIDER)

    def test_edge_local_provider_has_its_own_translator(self):
        translator = mt.create_translator(mt.EDGE_LOCAL_PROVIDER, "简体中文", source_language="英文")
        self.assertIsInstance(translator, mt.EdgeLocalTranslator)
        self.assertEqual(mt.provider_label(mt.EDGE_LOCAL_PROVIDER), "Edge 本地翻译")

    def test_edge_language_aliases_are_canonical_and_complete(self):
        self.assertEqual(mt.normalize_language_name("英语"), "英文")
        self.assertEqual(mt.normalize_language_name("日语"), "日文")
        self.assertEqual(mt.normalize_language_name("custom-language"), "custom-language")
        self.assertEqual(mt.language_code("英语", mt.BING_PROVIDER), "en")
        self.assertEqual(mt.language_code("德文", mt.BING_PROVIDER), "de")
        self.assertEqual(mt.language_code("法语", mt.BING_PROVIDER), "fr")

    def test_mtran_language_pairs_follow_installed_model_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            for name in ("de_en", "en_zh-Hans", "zh-Hans_en"):
                (model_dir / name).mkdir()
            self.assertEqual(
                mt.mtran_installed_model_pairs(model_dir),
                {("de", "en"), ("en", "zh-Hans"), ("zh-Hans", "en")},
            )
            available = mt.mtran_available_language_pairs(model_dir)
            self.assertIn(("de", "zh-Hans"), available, "installed de->en and en->zh packs must expose the bridge pair")
            self.assertNotIn(("en", "de"), available, "a missing en->de pack must not be offered")

    def test_layout_edge_provider_receives_the_download_consent_callback(self):
        consent = lambda _source, _target: True
        with patch("machine_translate.create_translator") as create:
            create.return_value = object()
            mt.translate_record_texts(
                [],
                "简体中文",
                provider_id=mt.EDGE_LOCAL_PROVIDER,
                source_language="英文",
                edge_download_consent=consent,
            )
        self.assertIs(create.call_args.kwargs["edge_download_consent"], consent)

    def test_edge_declined_download_is_prompted_only_once(self):
        decisions = []
        translator = mt.EdgeLocalTranslator(
            "简体中文",
            source_language="英文",
            download_consent=lambda *_args: decisions.append("asked") or False,
        )
        with patch.object(translator, "_has_cached_language_model", return_value=False), patch.object(translator, "ensure_available", side_effect=mt.MachineTranslationError("Edge 本地翻译语言模型尚未下载。")):
            with self.assertRaises(mt.MachineTranslationError):
                translator._ensure_session()
            with self.assertRaises(mt.MachineTranslationError):
                translator._ensure_session()
        self.assertEqual(decisions, ["asked"])

    def test_edge_cached_model_is_activated_without_a_second_download_prompt(self):
        decisions = []
        messages = []
        translator = mt.EdgeLocalTranslator(
            "简体中文",
            source_language="英文",
            log=messages.append,
            download_consent=lambda *_args: decisions.append("asked") or False,
        )
        unavailable = mt.MachineTranslationError("Edge 本地翻译语言模型尚未下载。")
        with (
            patch.object(translator, "_has_cached_language_model", return_value=True),
            patch.object(translator, "_start") as start,
            patch.object(translator, "ensure_available", side_effect=[unavailable, None]) as available,
            patch.object(translator, "_evaluate", return_value=True),
        ):
            translator._ensure_session()

        self.assertEqual(decisions, [])
        start.assert_called_once()
        self.assertEqual(available.call_args_list[1].kwargs, {"allow_download": True, "report_download_progress": False})
        self.assertIn("正在启用已下载的 Edge 本地翻译语言模型…", messages)

    def test_standalone_edge_provider_uses_short_batches_for_live_updates(self):
        fallback = mt.FallbackMachineTranslator("简体中文", source_language="英文")
        edge = mt.EdgeLocalTranslator("简体中文", source_language="英文")
        self.assertEqual(mt.translator_batch_limit(fallback), mt.GOOGLE_BATCH_CHARS)
        self.assertEqual(mt.translator_batch_limit(edge), mt.EDGE_LOCAL_MAX_CHARS)

    def test_edge_translates_each_block_once_without_marker_batch_retries(self):
        class EchoEdgeTranslator:
            current_provider = mt.EDGE_LOCAL_PROVIDER

            def __init__(self):
                self.calls: list[str] = []

            def translate(self, text):
                self.calls.append(text)
                return "译:" + text

        translator = EchoEdgeTranslator()
        items = [("first", "first paragraph"), ("second", "second paragraph")]
        result = mt.translate_text_items_batched(translator, items)

        self.assertEqual(translator.calls, ["first paragraph", "second paragraph"])
        self.assertEqual(result, {"first": "译:first paragraph", "second": "译:second paragraph"})

    def test_edge_replaces_ocr_replacement_character_before_request(self):
        messages: list[str] = []
        sent: list[str] = []
        translator = mt.EdgeLocalTranslator("简体中文", source_language="英文", log=messages.append)
        translator._ensure_session = lambda: None
        translator._translate_once = lambda text: sent.append(text) or "译文"

        self.assertEqual(translator.translate("via �NN search"), "译文")
        self.assertEqual(sent, ["via ?NN search"])
        self.assertTrue(any("OCR 缺失字符" in message for message in messages))

    def test_edge_placeholder_unknown_error_rebuilds_session_and_retries(self):
        messages: list[str] = []
        translator = mt.EdgeLocalTranslator("简体中文", source_language="英文", log=messages.append)
        session_starts = 0
        session_closes = 0
        expressions: list[str] = []

        def ensure_session():
            nonlocal session_starts
            session_starts += 1

        def close_session():
            nonlocal session_closes
            session_closes += 1

        def evaluate(expression: str):
            expressions.append(expression)
            if len(expressions) == 1:
                raise mt.MachineTranslationError("Edge 本地翻译错误: Uncaught (in promise)")
            return "译文保留 LTMKEEP00"

        translator._ensure_session = ensure_session
        translator.close = close_session
        translator._evaluate = evaluate
        placeholder = "ZXQH001E4E0FC897HQXZ"

        self.assertEqual(
            translator.translate(f"The density is {placeholder}."),
            f"译文保留 {placeholder}",
        )
        self.assertEqual(session_starts, 2)
        self.assertEqual(session_closes, 1)
        self.assertEqual(len(expressions), 2)
        self.assertIn(placeholder, expressions[0])
        self.assertIn("LTMKEEP00", expressions[1])
        self.assertTrue(any("重建会话" in message for message in messages))

    def test_edge_generic_promise_error_rebuilds_session_and_retries_original_text(self):
        messages: list[str] = []
        attempts: list[str] = []
        translator = mt.EdgeLocalTranslator("简体中文", source_language="英文", log=messages.append)
        session_starts = 0
        session_closes = 0

        def ensure_session():
            nonlocal session_starts
            session_starts += 1

        def close_session():
            nonlocal session_closes
            session_closes += 1

        def translate_once(text: str):
            attempts.append(text)
            if len(attempts) == 1:
                raise mt.MachineTranslationError("Edge 本地翻译错误: Uncaught (in promise)")
            return "恢复后的译文"

        translator._ensure_session = ensure_session
        translator.close = close_session
        translator._translate_once = translate_once

        self.assertEqual(translator.translate("A plain paragraph without protected content."), "恢复后的译文")
        self.assertEqual(attempts, ["A plain paragraph without protected content."] * 2)
        self.assertEqual(session_starts, 2)
        self.assertEqual(session_closes, 1)
        self.assertTrue(any("原文重试" in message for message in messages))

    def test_edge_persistent_promise_error_splits_plain_text_locally(self):
        source = "The first half contains enough words to form a useful fragment while the second half remains translatable."
        messages: list[str] = []
        attempts: list[str] = []
        translator = mt.EdgeLocalTranslator("简体中文", source_language="英文", log=messages.append)
        translator._ensure_session = lambda: None
        translator.close = lambda: None

        def translate_once(text: str):
            attempts.append(text)
            if text == source:
                raise mt.MachineTranslationError("Edge 本地翻译错误: Uncaught (in promise)")
            return f"译:{text}"

        translator._translate_once = translate_once

        self.assertEqual(translator.translate(source), "译:The first half contains enough words to form a useful 译:fragment while the second half remains translatable.")
        self.assertEqual(attempts[:2], [source, source])
        self.assertEqual(len(attempts), 4)
        self.assertTrue(any("拆分为两段" in message for message in messages))

    def test_edge_persistent_placeholder_error_translates_only_surrounding_text(self):
        marker = "ZXQH001E4E0FC897HQXZ"
        source = f"The formula {marker} appears here."
        messages: list[str] = []
        translator = mt.EdgeLocalTranslator("简体中文", source_language="英文", log=messages.append)
        translator._ensure_session = lambda: None
        translator.close = lambda: None

        def translate_once(text: str):
            if marker in text or "LTMKEEP00" in text:
                raise mt.MachineTranslationError("Edge 本地翻译错误: UnknownError: Other generic failures occurred.")
            return f"译:{text}"

        translator._translate_once = translate_once

        self.assertEqual(translator.translate(source), f"译:The formula {marker}译: appears here.")
        self.assertTrue(any("保留公式/引文标记" in message for message in messages))

    def test_google_failure_switches_directly_to_bing(self):
        class FailingGoogle:
            def translate(self, _text):
                raise mt.MachineTranslationError("Google offline")

        class EchoBing:
            def translate(self, text):
                return "Bing: " + text

        fallback = mt.FallbackMachineTranslator("简体中文", source_language="英文")
        fallback._google_probe_checked = True
        fallback._translator = FailingGoogle()
        with patch("machine_translate.WebMachineTranslator", return_value=EchoBing()) as factory:
            self.assertEqual(fallback.translate("first"), "Bing: first")
            self.assertEqual(fallback.translate("second"), "Bing: second")
        self.assertEqual(fallback.current_provider, mt.BING_PROVIDER)
        factory.assert_called_once()

    def test_google_failure_logs_a_clear_bing_wait_message(self):
        messages: list[str] = []
        fallback = mt.FallbackMachineTranslator("简体中文", source_language="英文", log=messages.append)

        with patch("machine_translate.WebMachineTranslator"):
            fallback._switch_to_bing(mt.MachineTranslationError("Google offline"))

        self.assertEqual(messages, ["正在尝试Bing翻译，该服务较慢，请稍等。"])

    def test_edge_failure_switches_once_to_bing(self):
        class FailingGoogle:
            def translate(self, _text):
                raise mt.MachineTranslationError("Google offline")

        class FailingEdge:
            provider_id = "edge_local"

            def __init__(self, *_args, **_kwargs):
                pass

            def ensure_available(self, **_kwargs):
                raise mt.MachineTranslationError("Edge unavailable")

            def close(self):
                pass

        class EchoBing:
            def translate(self, text):
                return "Bing: " + text

        fallback = mt.FallbackMachineTranslator("简体中文", source_language="英文")
        fallback._google_probe_checked = True
        fallback._translator = FailingGoogle()
        with patch("machine_translate.EdgeLocalTranslator", FailingEdge), patch("machine_translate.WebMachineTranslator", return_value=EchoBing()) as factory:
            self.assertEqual(fallback.translate("first"), "Bing: first")
            self.assertEqual(fallback.translate("second"), "Bing: second")
        self.assertEqual(fallback.current_provider, mt.BING_PROVIDER)
        factory.assert_called_once()

    def test_bing_normalizes_a_display_source_language(self):
        translator = mt.WebMachineTranslator(mt.BING_PROVIDER, "简体中文", source_language="英文")
        self.assertEqual(translator.source_code, "en")

    def test_bing_session_parameters_are_cached(self):
        translator = mt.WebMachineTranslator(mt.BING_PROVIDER, "简体中文", source_language="英文")
        session = ("https://www.bing.com/", "ig", "iid", "key", "token")
        with patch.object(translator, "_fetch_bing_sid", return_value=session) as fetch:
            self.assertEqual(translator._bing_sid(), session)
            self.assertEqual(translator._bing_sid(), session)
        fetch.assert_called_once()

    def test_bing_batches_use_two_isolated_workers(self):
        class EchoBingTranslator:
            current_provider = mt.BING_PROVIDER

            def __init__(self, counter):
                self.counter = counter

            def clone_for_worker(self):
                self.counter.append("clone")
                return EchoBingTranslator(self.counter)

            def translate(self, text):
                return text

        clones: list[str] = []
        items = [(str(index), "x" * 1200) for index in range(3)]
        result = mt.translate_text_items_batched(EchoBingTranslator(clones), items)
        self.assertEqual(result, dict(items))
        self.assertEqual(len(clones), 2)


if __name__ == "__main__":
    unittest.main()
