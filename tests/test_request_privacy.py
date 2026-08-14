"""Regression tests for public API request construction and privacy boundaries."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from AI_common import USER_AGENT
from OT_common import _settings_from_dict
from AI_request_construction import (
    REQUEST_BODY_MODE_CODEX,
    REQUEST_BODY_MODE_STANDARD,
    normalize_request_body_mode,
    request_url_for_construction,
)
from AI_services import (
    AIConfig,
    PROVIDERS,
    build_headers,
    build_multipart_headers,
    build_text_chat_payload,
    mark_thinking_capability,
    normalize_gemini_model_id,
    normalize_base_url,
    siliconflow_supports_thinking,
)
from LS_pipeline import (
    AITranslateConfig,
    MinerUError,
    STREAM_CHUNK_CONCURRENCY,
    STREAM_CONTINUATION_MAX_ROUNDS,
    ai_chat_completion,
    gemini_quota_coordinator,
    gemini_stream_delta_is_thought,
    provider_default_base_url,
    provider_model_list_url,
    split_gemini_thought_delta,
    translation_request_concurrency_limit,
    translate_markdown_by_chunks,
)
from AI_widgets import (
    ChatWorker,
    looks_like_non_multimodal_image_error,
    redact_local_paths_for_api_text,
    sanitize_content_parts_for_api,
    strip_image_url_parts_from_content,
)
from AI_chat import ChatWindow
from layout_translate_preview import extract_json_object


class RequestPrivacyTests(unittest.TestCase):
    def test_pending_document_status_explains_image_model_exclusion(self):
        chat = ChatWindow.__new__(ChatWindow)
        status_texts: list[str] = []
        clear_button_states: list[bool] = []
        chat.document_parse_worker = None
        chat.document_contexts = [{"title": "example.pdf"}]
        chat.document_status_label = SimpleNamespace(setText=status_texts.append)
        chat.clear_documents_button = SimpleNamespace(setEnabled=clear_button_states.append)
        chat.model_combo = SimpleNamespace(currentText=lambda: "gpt-image-2")

        chat.refresh_document_status()

        self.assertEqual(
            status_texts[-1],
            "已暂存 1 个文件（图片模型本轮不会发送，切回文本模型后可用）：example.pdf",
        )
        self.assertTrue(clear_button_states[-1])

    def test_image_prompt_uses_only_user_requirements_not_prior_revised_prompts(self):
        worker = ChatWorker.__new__(ChatWorker)
        revised_prompt = (
            "请阅读下面按时间顺序整理的完整对话历史，理解用户需求如何逐步变化。\n"
            "===== 最新一轮用户要求（最高优先级） =====\n把猫猫画全"
        )
        worker.messages = [
            {"role": "user", "content": "把猫猫画全"},
            {"role": "assistant", "content": revised_prompt},
            {
                "role": "assistant",
                "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
            },
            {"role": "user", "content": "换成橘猫"},
        ]

        prompt = worker.build_image_conversation_prompt(
            3,
            worker.messages[3]["content"],
            use_edit_mode=True,
        )

        self.assertIn("[1] 把猫猫画全", prompt)
        self.assertIn("===== 当前用户要求 =====\n换成橘猫", prompt)
        self.assertIn("请结合本次随请求附带的图片进行编辑", prompt)
        self.assertNotIn(revised_prompt, prompt)
        self.assertNotIn("助手生成图片", prompt)

    def test_continuous_image_edit_combines_latest_output_and_explicit_references(self):
        assistant_image = "data:image/png;base64,QQ=="
        older_assistant_image = "data:image/png;base64,WA=="
        current_image = "data:image/png;base64,Qg=="
        selected_image = "data:image/png;base64,Qw=="

        with tempfile.TemporaryDirectory() as directory:
            local_reference = Path(directory) / "local-reference.png"
            local_reference.write_bytes(b"local-image")

            worker = ChatWorker.__new__(ChatWorker)
            worker.config = SimpleNamespace(
                local_reference_image_path=str(local_reference),
                selected_reference_images=[
                    {"kind": "chat", "data_url": assistant_image},  # duplicate: must be removed
                    {"kind": "chat", "data_url": selected_image},
                ],
            )
            worker.messages = [
                {"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": older_assistant_image}}]},
                {"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": assistant_image}}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "在上一版基础上调整，并参考附件"},
                        {"type": "image_url", "image_url": {"url": current_image}},
                    ],
                },
            ]

            image_files, source_label = worker.resolve_image_edit_inputs(2, worker.messages[2]["content"])

        self.assertEqual(
            [item[0] for item in image_files],
            ["latest-assistant-image.png", "turn-image-1.png", "local-reference.png", "selected-reference-2.png"],
        )
        self.assertIn("第 1 张：最近一张模型输出图（当前编辑底图）", source_label)
        self.assertIn("第 2 张：本轮新图片（附加输入）", source_label)
        self.assertIn("第 3 张：手动选择的本地参考图", source_label)
        self.assertIn("第 4 张：手动选中的历史参考图", source_label)

        prompt = worker.build_image_conversation_prompt(
            2,
            worker.messages[2]["content"],
            use_edit_mode=True,
            edit_source_label=source_label,
        )
        self.assertIn(f"图片顺序与用途：{source_label}。", prompt)
        self.assertIn("以该图为主要修改对象", prompt)

    def test_image_model_request_excludes_document_context_from_history(self):
        chat = ChatWindow.__new__(ChatWindow)
        document_context = (
            "以下是用户添加的文档全文。请基于这些文档回答后续问题；不要裁切、摘取或忽略正文内容。\n"
            "文档发送方式: 全文无图\n\n"
            "===== 文档 1: example.pdf =====\n来源: C:/papers/example.md\n\n完整论文正文"
        )
        messages = [
            {"role": "user", "content": document_context},
            {"role": "assistant", "content": "我已阅读。"},
            {"role": "user", "content": "生成一张研究流程图。"},
        ]
        payload_messages = chat.api_messages_for_config(
            AIConfig(
                provider_id="openai",
                api_key="test-key",
                base_url="https://api.openai.com/v1",
                model="gpt-image-2",
            ),
            messages,
        )
        self.assertEqual(len(payload_messages), 2)
        self.assertNotIn("完整论文正文", str(payload_messages))
        self.assertEqual(payload_messages[-1]["content"], "生成一张研究流程图。")

    def test_gemini_429_starts_shared_cooldown_without_proactive_throttling(self):
        config = AITranslateConfig(
            provider_id="gemini",
            api_key="quota-test-key",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-test",
        )
        coordinator = gemini_quota_coordinator(config)
        now = 100.0
        waits: list[float] = []
        notices: list[str] = []
        coordinator.cooldown_until = 0.0
        coordinator.now = lambda: now

        def fake_sleep(seconds):
            nonlocal now
            waits.append(seconds)
            now += seconds

        coordinator.sleep = fake_sleep
        calls = 0

        def fake_once(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise MinerUError(
                    "HTTP 429: RESOURCE_EXHAUSTED",
                    status=429,
                    retry_after=2.5,
                )
            return "恢复成功"

        with patch("LS_pipeline._ai_chat_completion_once", side_effect=fake_once):
            result = ai_chat_completion(
                config,
                [{"role": "user", "content": "test"}],
                rate_limit_callback=notices.append,
            )

        self.assertEqual(result, "恢复成功")
        self.assertEqual(calls, 2)
        self.assertEqual(waits, [2.5])
        self.assertEqual(len(notices), 1)
        self.assertIn("自动继续", notices[0])

    def test_stream_chunks_continue_with_provider_bounded_concurrency_and_ordered_cache(self):
        chunks = [
            f"## Section {index}\n\nSource chunk {index} has complete academic prose.\n"
            for index in range(1, 8)
        ]
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        request_count = 0

        def fake_completion(_config, messages, **kwargs):
            nonlocal active, maximum_active, request_count
            serialized = json.dumps(messages, ensure_ascii=False)
            chunk_number = int(__import__("re").search(r"Source chunk (\d+)", serialized).group(1))
            marker = __import__("re").search(r"<<<\d{8}>>>", serialized).group(0)
            assistant_rounds = sum(message["role"] == "assistant" for message in messages)
            with state_lock:
                active += 1
                request_count += 1
                maximum_active = max(maximum_active, active)
            time.sleep(max(0.001, (12 - chunk_number) / 1000))
            with state_lock:
                active -= 1
            text = (
                f"译文-{chunk_number}-B\n{marker}"
                if assistant_rounds
                else f"译文-{chunk_number}-A"
            )
            if kwargs.get("stream_callback"):
                kwargs["stream_callback"](text)
            return text

        config = AITranslateConfig(
            provider_id="deepseek",
            api_key="test-key",
            base_url="https://example.invalid",
            model="test-model",
        )
        with (
            tempfile.TemporaryDirectory() as temporary_dir,
            patch("LS_pipeline.split_markdown_for_translation", return_value=chunks),
            patch("LS_pipeline.build_translation_context", return_value="统一术语指南"),
            patch("LS_pipeline.ai_chat_completion", side_effect=fake_completion),
            patch("LS_pipeline.save_translation_request_audit"),
        ):
            work_dir = Path(temporary_dir)
            first = translate_markdown_by_chunks(
                "\n".join(chunks),
                config,
                log=lambda _message: None,
                work_dir=work_dir,
            )
            first_request_count = request_count
            second = translate_markdown_by_chunks(
                "\n".join(chunks),
                config,
                log=lambda _message: None,
                work_dir=work_dir,
            )
            manifest = json.loads(
                (work_dir / "manifest.zh.json").read_text(encoding="utf-8")
            )

        expected_concurrency = translation_request_concurrency_limit(config.provider_id)
        self.assertGreater(maximum_active, 1)
        self.assertLessEqual(maximum_active, min(expected_concurrency, len(chunks)))
        self.assertEqual(first_request_count, len(chunks) * 2)
        self.assertEqual(request_count, first_request_count)
        self.assertEqual(second, first)
        for index in range(1, len(chunks) + 1):
            current = first.index(f"译文-{index}-A")
            following = (
                first.index(f"译文-{index + 1}-A")
                if index < len(chunks)
                else len(first)
            )
            self.assertLess(current, following)
            self.assertIn(f"译文-{index}-B", first)
        self.assertEqual(STREAM_CHUNK_CONCURRENCY, 3)
        self.assertEqual(STREAM_CONTINUATION_MAX_ROUNDS, 64)
        self.assertEqual(manifest["concurrency"], expected_concurrency)
        self.assertEqual(manifest["max_rounds_per_chunk"], 64)

    def test_additional_formula_does_not_resend_complete_chunk(self):
        source = "The measured value is \\(x+y\\)."
        requests = []
        logs = []

        def fake_completion(_config, messages, **kwargs):
            requests.append(messages)
            marker = re.search(r"<<<\d{8}>>>", json.dumps(messages, ensure_ascii=False)).group(0)
            text = f"测得的数值为 \\(x+y\\)，另记 \\(Z\\)。\n{marker}"
            if kwargs.get("stream_callback"):
                kwargs["stream_callback"](text)
            return text

        config = AITranslateConfig(
            provider_id="gemini",
            api_key="test-key",
            base_url="https://example.invalid",
            model="test-model",
        )
        with (
            tempfile.TemporaryDirectory() as temporary_dir,
            patch("LS_pipeline.split_markdown_for_translation", return_value=[source]),
            patch("LS_pipeline.build_translation_context", return_value="统一术语指南"),
            patch("LS_pipeline.ai_chat_completion", side_effect=fake_completion),
            patch("LS_pipeline.save_translation_request_audit"),
        ):
            translate_markdown_by_chunks(
                source,
                config,
                log=logs.append,
                work_dir=Path(temporary_dir),
            )

        self.assertEqual(len(requests), 1)
        self.assertTrue(any("不执行整块重试" in message for message in logs))

    def test_layout_chunked_groups_use_three_way_bounded_concurrency_and_resume_cache(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_concurrency_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)

        records = [
            module.LayoutTextBlock(
                block_id=f"p001_b{index + 1:04d}",
                page=1,
                block_type="text",
                text=f"Source block number {index + 1} contains enough English words for validation.",
                block={},
            )
            for index in range(12)
        ]
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        request_count = 0

        def fake_completion(_config, messages, **_kwargs):
            nonlocal active, maximum_active, request_count
            content = messages[1]["content"]
            blocks = json.loads(content.split("Input blocks JSON:\n", 1)[1])["blocks"]
            with state_lock:
                request_count += 1
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            first_pass = len(messages) == 2
            return json.dumps(
                {
                    "translations": [] if first_pass else [
                        {
                            "id": item["id"],
                            "text": f"这是 {item['id']} 的完整中文译文。",
                        }
                        for item in blocks
                    ],
                    "formula_replacements": [],
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as temporary_dir:
            cache_path = Path(temporary_dir) / "blocks.json"
            with (
                patch.object(module.mineru, "ai_chat_completion", side_effect=fake_completion),
                patch.object(module.mineru, "save_translation_request_audit"),
                patch.object(module, "build_global_guide", return_value="统一术语指南"),
            ):
                first = module.translate_records(
                    records,
                    config=SimpleNamespace(
                        provider_id="gemini",
                        base_url="https://example.invalid",
                        model="test-model",
                    ),
                    target_language="简体中文",
                    cache_path=cache_path,
                    max_chars=0,
                    max_blocks=1,
                    concurrency=3,
                    translation_mode="chunks",
                    log=lambda _message: None,
                )
                first_request_count = request_count
                second = module.translate_records(
                    records,
                    config=SimpleNamespace(
                        provider_id="gemini",
                        base_url="https://example.invalid",
                        model="test-model",
                    ),
                    target_language="简体中文",
                    cache_path=cache_path,
                    max_chars=0,
                    max_blocks=1,
                    concurrency=3,
                    translation_mode="chunks",
                    log=lambda _message: None,
                )

            payload = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertGreater(maximum_active, 1)
        self.assertLessEqual(maximum_active, 3)
        self.assertEqual(first_request_count, len(records))
        self.assertEqual(request_count, first_request_count)
        self.assertEqual(list(first), [record.block_id for record in records])
        self.assertEqual(second, first)
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["translation_mode"], "chunked")
        self.assertEqual(
            [item["id"] for item in payload["translations"]],
            [record.block_id for record in records],
        )

    def test_layout_full_context_ignores_chunk_limits(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_full_context_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)

        records = [
            module.LayoutTextBlock(
                block_id=f"p001_b{index + 1:04d}",
                page=1,
                block_type="text",
                text=f"Source block number {index + 1} contains enough English words for validation.",
                block={},
            )
            for index in range(12)
        ]
        group_sizes = []
        logs = []

        def fake_completion(_config, messages, **_kwargs):
            blocks = json.loads(messages[1]["content"].split("Input blocks JSON:\n", 1)[1])["blocks"]
            group_sizes.append(len(blocks))
            return json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "text": f"这是 {item['id']} 的完整中文译文。"}
                        for item in blocks
                    ],
                    "formula_replacements": [],
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as temporary_dir:
            cache_path = Path(temporary_dir) / "blocks.json"
            with (
                patch.object(module.mineru, "ai_chat_completion", side_effect=fake_completion),
                patch.object(module.mineru, "save_translation_request_audit"),
                patch.object(module, "build_global_guide") as build_guide,
            ):
                translated = module.translate_records(
                    records,
                    config=SimpleNamespace(
                        provider_id="gemini",
                        base_url="https://example.invalid",
                        model="test-model",
                    ),
                    target_language="简体中文",
                    cache_path=cache_path,
                    max_chars=1,
                    max_blocks=1,
                    concurrency=3,
                    translation_mode="full_context",
                    log=logs.append,
                )
            payload = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(group_sizes, [len(records)])
        build_guide.assert_not_called()
        self.assertEqual(list(translated), [record.block_id for record in records])
        self.assertEqual(payload["translation_mode"], "full_context")
        self.assertEqual(payload["group_count"], 1)
        self.assertEqual(payload["concurrency"], 1)
        self.assertTrue(any("不限制字符数和块数" in message for message in logs))

    def test_siliconflow_uses_its_openai_compatible_v1_endpoints(self):
        self.assertEqual(PROVIDERS["siliconflow"].display_name, "硅基流动 (SiliconFlow)")
        self.assertTrue(PROVIDERS["siliconflow"].supports_model_list)
        self.assertEqual(provider_default_base_url("siliconflow"), "https://api.siliconflow.cn/v1")
        self.assertEqual(
            provider_model_list_url("siliconflow", "https://api.siliconflow.cn/v1"),
            "https://api.siliconflow.cn/v1/models?sub_type=chat",
        )
        self.assertEqual(
            normalize_base_url("https://api.siliconflow.cn/v1/chat/completions", "siliconflow"),
            "https://api.siliconflow.cn/v1",
        )
        self.assertEqual(
            request_url_for_construction("https://api.siliconflow.cn/v1", "siliconflow", "standard"),
            "https://api.siliconflow.cn/v1/chat/completions",
        )

    def test_siliconflow_thinking_controls_follow_the_selected_model(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "AI_services.THINKING_CAPABILITY_PATH",
            Path(temp_dir) / "thinking_capabilities.json",
        ):
            base_url = "https://api.siliconflow.cn/v1"
            model = "deepseek-ai/DeepSeek-V4-Flash"
            self.assertFalse(siliconflow_supports_thinking(base_url, model))
            mark_thinking_capability("siliconflow", base_url, model, True)
            self.assertTrue(siliconflow_supports_thinking(base_url, model))

            payload = build_text_chat_payload(
                AIConfig(
                    provider_id="siliconflow",
                    api_key="test-key",
                    base_url=base_url,
                    model=model,
                    thinking_mode="enabled",
                    reasoning_effort="high",
                ),
                [{"role": "user", "content": "hello"}],
            )
            self.assertEqual(payload["enable_thinking"], True)
            self.assertEqual(payload["thinking_budget"], 8192)

    def test_oneapi_legacy_claude_setting_uses_chat_completions(self):
        self.assertEqual(normalize_request_body_mode("oneapi", "claude"), REQUEST_BODY_MODE_CODEX)
        self.assertEqual(normalize_request_body_mode("deepseek", "claude"), REQUEST_BODY_MODE_STANDARD)
        self.assertEqual(
            request_url_for_construction("https://gateway.example/v1/messages", "oneapi", "claude"),
            "https://gateway.example/v1/chat/completions",
        )

    def test_standard_headers_do_not_impersonate_a_browser_or_cherry(self):
        self.assertEqual(USER_AGENT, "LitMTrans/1.0.0")
        for headers in (build_headers("test-key", stream=True), build_multipart_headers("test-key")):
            joined = "\n".join(f"{key}: {value}" for key, value in headers.items()).lower()
            self.assertIn("litmtrans/1.0", joined)
            self.assertNotIn("cherry-ai", joined)
            self.assertNotIn("mozilla/5.0", joined)
            self.assertNotIn("origin:", joined)
            self.assertNotIn("referer:", joined)
            self.assertNotIn("x-requested-with", joined)

    def test_oneapi_keeps_only_its_explicit_cache_enhancement(self):
        key = "7e2b0f7e-3b2c-4c91-9be5-999999999999"
        payload = build_text_chat_payload(
            AIConfig(
                provider_id="oneapi",
                api_key="test-key",
                base_url="https://gateway.example/v1",
                model="gpt-test",
                prompt_cache_key=key,
                request_body_mode="claude",
            ),
            [{"role": "user", "content": "hello"}],
        )
        self.assertEqual(payload["prompt_cache_key"], key)
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_openrouter_uses_sticky_session_and_leaves_latest_input_dynamic(self):
        key = "session-123"
        messages = [
            {"role": "system", "content": "fixed instructions"},
            {"role": "user", "content": "fixed paper context"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "latest question"},
        ]
        payload = build_text_chat_payload(
            AIConfig(
                provider_id="openrouter",
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="deepseek/deepseek-v3.2",
                prompt_cache_key=key,
            ),
            messages,
        )
        self.assertEqual(payload["session_id"], key)
        self.assertNotIn("prompt_cache_key", payload)
        self.assertEqual(payload["messages"][-1]["content"], "latest question")
        self.assertEqual(
            payload["messages"][-2]["content"][0]["cache_control"],
            {"type": "ephemeral"},
        )
        self.assertEqual(
            build_headers("test-key", prompt_cache_key=key, provider_id="openrouter")["x-session-id"],
            key,
        )

    def test_reasoning_aliases_are_not_concatenated(self):
        worker = ChatWorker.__new__(ChatWorker)
        text = worker.extract_reasoning_text(
            {"reasoning_content": "same"},
            {"reasoning": "same"},
            {"reasoning_details": [{"text": "same"}], "reasoning_content": "same"},
        )
        self.assertEqual(text, "same")

    def test_openai_compatible_provider_is_independent_and_uses_oneapi_transport(self):
        key = "5f39f0aa-6a5e-47e2-b08a-888888888888"
        self.assertEqual(PROVIDERS["oneapi"].default_base_url, "")
        self.assertEqual(PROVIDERS["openai_compatible"].display_name, "OpenAI 兼容接口")
        self.assertEqual(provider_default_base_url("openai_compatible"), "")
        self.assertEqual(
            normalize_base_url("https://gateway.example/chat/completions", "openai_compatible"),
            "https://gateway.example/v1",
        )
        self.assertEqual(
            normalize_request_body_mode("openai_compatible", "claude"),
            REQUEST_BODY_MODE_CODEX,
        )
        payload = build_text_chat_payload(
            AIConfig(
                provider_id="openai_compatible",
                api_key="test-key",
                base_url="https://gateway.example/v1",
                model="gpt-test",
                prompt_cache_key=key,
                request_body_mode="claude",
            ),
            [{"role": "user", "content": "hello"}],
        )
        self.assertEqual(payload["prompt_cache_key"], key)
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_gemini_uses_openai_compatibility_options(self):
        config = AIConfig(
            provider_id="gemini",
            api_key="test-key",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-3.6-flash",
            thinking_mode="disabled",
        )
        payload = build_text_chat_payload(config, [{"role": "user", "content": "hello"}])
        self.assertEqual(payload["reasoning_effort"], "minimal")
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertNotIn("extra_body", payload)
        self.assertEqual(
            build_headers("test-key", provider_id="gemini", base_url=config.base_url)["x-goog-api-client"],
            "litmtrans/1.0",
        )
        self.assertEqual(normalize_gemini_model_id("models/gemini-2.5-flash"), "gemini-2.5-flash")
        parsed = extract_json_object(r'{"translations":[{"id":"x","text":"保留 \\alpha 和 \\text{test}"}]}')
        self.assertEqual(parsed["translations"][0]["text"], "保留 \\alpha 和 \\text{test}")

    def test_gemini_requests_public_thought_summary_when_display_is_enabled(self):
        config = AIConfig(
            provider_id="gemini",
            api_key="test-key",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-3.6-flash",
            show_reasoning=True,
        )
        payload = build_text_chat_payload(config, [{"role": "user", "content": "hello"}])
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(
            payload["extra_body"],
            {"google": {"thinking_config": {"thinking_level": "medium", "include_thoughts": True}}},
        )

    def test_gemini_thought_tags_are_routed_to_the_reasoning_panel(self):
        visible, thought, in_thought = split_gemini_thought_delta("<thought>先分析", False)
        self.assertEqual((visible, thought, in_thought), ("", "先分析", True))
        visible, thought, in_thought = split_gemini_thought_delta("，再验证</thought>最终回答", in_thought)
        self.assertEqual((visible, thought, in_thought), ("最终回答", "，再验证", False))

    def test_gemini_thought_flag_is_recognized_without_tags(self):
        event = {
            "choices": [{
                "delta": {
                    "content": "正在核对术语。",
                    "extra_content": {"google": {"thought": True}},
                }
            }]
        }
        self.assertTrue(gemini_stream_delta_is_thought(event))

    def test_siliconflow_vlm_rejection_is_recognized_as_non_multimodal(self):
        error = (
            '{"code":20041,"message":"The model is not a VLM '
            '(Vision Language Model). Please use text-only prompts."}'
        )
        self.assertTrue(looks_like_non_multimodal_image_error(error))
        self.assertFalse(looks_like_non_multimodal_image_error("The request timed out."))

    def test_official_deepseek_translation_defaults_to_non_thinking_mode(self):
        config = AITranslateConfig(
            provider_id="deepseek",
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
        )
        response = {"choices": [{"message": {"content": "translated"}}]}
        with patch("LS_pipeline.http_json", return_value=response) as request:
            self.assertEqual(ai_chat_completion(config, [{"role": "user", "content": "translate"}]), "translated")

        payload = request.call_args.args[2]
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertNotIn("reasoning_effort", payload)

        config.thinking_mode = "enabled"
        config.reasoning_effort = "max"
        with patch("LS_pipeline.http_json", return_value=response) as request:
            ai_chat_completion(config, [{"role": "user", "content": "translate"}])
        payload = request.call_args.args[2]
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "max")

    def test_deepseek_fast_layout_uses_full_markdown_prefix_and_waits_for_hit(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_deepseek_fast_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)

        records = [module.LayoutTextBlock(
            block_id="title", page=1, block_type="title", text="A Long Academic Paper Title", block={},
        )] + [
            module.LayoutTextBlock(
                block_id=f"p001_b{index:04d}", page=1, block_type="text",
                text=(f"Source sentence {index} has enough English words for layout validation. " * 12).strip(), block={},
            )
            for index in range(1, 21)
        ]
        calls = []
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def fake_completion(_config, messages, **kwargs):
            nonlocal active, maximum_active
            with lock:
                calls.append(messages)
                call_number = len(calls)
                active += 1
                maximum_active = max(maximum_active, active)
            if call_number == 2:
                kwargs["usage_callback"]({"prompt_cache_hit_tokens": 123, "prompt_cache_miss_tokens": 7})
            time.sleep(0.02)
            with lock:
                active -= 1
            blocks = json.loads(messages[1]["content"].split("Input blocks JSON:\n", 1)[1])["blocks"]
            return json.dumps({"translations": [
                {"id": item["id"], "text": f"这是 {item['id']} 的完整中文译文。"}
                for item in blocks
            ]}, ensure_ascii=False)

        logs = []
        config = SimpleNamespace(
            provider_id="deepseek", base_url="https://api.deepseek.com", model="deepseek-chat",
            deepseek_fast_layout_translation=True, custom_translation_instruction="",
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            with (
                patch.object(module.mineru, "ai_chat_completion", side_effect=fake_completion),
                patch.object(module.mineru, "save_translation_request_audit"),
            ):
                translated = module.translate_records(
                    records, config, "简体中文", Path(temporary_dir) / "blocks.json",
                    full_markdown_context="# Complete paper\n\nAll pages remain available as context.",
                    log=logs.append,
                )

        self.assertEqual(set(translated), {record.block_id for record in records})
        first_blocks = json.loads(calls[0][1]["content"].split("Input blocks JSON:\n", 1)[1])["blocks"]
        self.assertEqual([item["id"] for item in first_blocks], ["title"])
        self.assertGreaterEqual(len(calls), 4)
        self.assertGreater(maximum_active, 1)
        self.assertTrue(all("===== BEGIN FULL PAPER MARKDOWN =====" in call[1]["content"] for call in calls))
        self.assertTrue(any("已确认 DeepSeek" in message for message in logs))
        self.assertTrue(any("缓存归属块 p001_b0001" in message for message in logs))
        self.assertTrue(any("高速并发翻译缓存统计报告" in message for message in logs))
        self.assertTrue(any("API 仅提供请求级统计" in message for message in logs))

    def test_deepseek_fast_layout_stops_before_parallel_wave_when_probe_is_below_threshold(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_deepseek_fast_cache_protection_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)

        records = [module.LayoutTextBlock(
            block_id="title", page=1, block_type="title", text="A Long Academic Paper Title", block={},
        )] + [
            module.LayoutTextBlock(
                block_id=f"p001_b{index:04d}", page=1, block_type="text",
                text=(f"Source sentence {index} has enough English words for cache protection validation. " * 40).strip(), block={},
            )
            for index in range(1, 6)
        ]
        calls = []

        def fake_completion(_config, messages, **kwargs):
            calls.append(messages)
            if len(calls) == 2:
                kwargs["usage_callback"]({"prompt_cache_hit_tokens": 49, "prompt_cache_miss_tokens": 51})
            elif len(calls) == 3:
                kwargs["usage_callback"]({"prompt_cache_hit_tokens": 59, "prompt_cache_miss_tokens": 41})
            blocks = json.loads(messages[1]["content"].split("Input blocks JSON:\n", 1)[1])["blocks"]
            return json.dumps({"translations": [
                {"id": item["id"], "text": f"这是 {item['id']} 的完整中文译文。"}
                for item in blocks
            ]}, ensure_ascii=False)

        config = SimpleNamespace(
            provider_id="deepseek", base_url="https://api.deepseek.com", model="deepseek-chat",
            deepseek_fast_layout_translation=True, custom_translation_instruction="",
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            cache_path = Path(temporary_dir) / "blocks.json"
            with (
                patch.object(module.mineru, "ai_chat_completion", side_effect=fake_completion),
                patch.object(module.mineru, "save_translation_request_audit"),
            ):
                with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_FAST_CACHE_PROTECTION"):
                    module.translate_records(
                        records, config, "简体中文", cache_path,
                        full_markdown_context="# Complete paper\n\nAll pages remain available as context.",
                    )
            checkpoint = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(len(calls), 3)
        self.assertFalse(checkpoint["complete"])
        self.assertEqual(checkpoint["completed_groups"], 3)
        self.assertEqual(len(checkpoint["translations"]), 3)

    def test_deepseek_fast_layout_keeps_an_overlong_continuation_together(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_continuation_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        first = module.LayoutTextBlock("left", 1, "text", "A " * 1_100, {})
        second = module.LayoutTextBlock("right", 2, "text", "continued sentence ends here.", {})
        groups = module.deepseek_fast_layout_groups(
            [first, second], "A " * 1_100 + "continued sentence ends here."
        )
        self.assertEqual([[record.block_id for record in group] for group in groups], [["left", "right"]])

    def test_deepseek_fast_layout_does_not_fragment_short_body_or_auxiliary_blocks(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_short_batch_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        records = [
            module.LayoutTextBlock("title", 1, "title", "A title", {}),
            module.LayoutTextBlock("body-one", 1, "text", "First short body.", {}),
            module.LayoutTextBlock("section", 1, "title", "A section", {}),
            module.LayoutTextBlock("body-two", 1, "text", "Second short body.", {}),
            module.LayoutTextBlock("figure", 1, "chart_caption", "Short figure caption.", {}),
            module.LayoutTextBlock("table-note", 1, "table_footnote", "Short table note.", {}),
        ]
        groups = module.deepseek_fast_layout_groups(records, "")
        self.assertEqual(
            [[record.block_id for record in group] for group in groups],
            [["title", "section"], ["body-one", "body-two"], ["figure", "table-note"]],
        )

    def test_deepseek_fast_layout_merges_a_tiny_trailing_body_group(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_tiny_tail_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        records = [
            module.LayoutTextBlock("near-limit", 1, "text", "A" * 1_950 + ".", {}),
            module.LayoutTextBlock("tail", 1, "text", "A short trailing fragment.", {}),
        ]
        groups = module.deepseek_fast_layout_groups(records, "")
        self.assertEqual([[record.block_id for record in group] for group in groups], [["near-limit", "tail"]])

    def test_deepseek_fast_layout_splits_large_auxiliary_content_into_batches(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_auxiliary_batch_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        records = [
            module.LayoutTextBlock("caption-one", 1, "chart_caption", "A" * 1_300, {}),
            module.LayoutTextBlock("caption-two", 2, "image_caption", "B" * 1_000, {}),
            module.LayoutTextBlock("caption-three", 3, "table_caption", "C" * 1_300, {}),
        ]
        groups = module.deepseek_fast_layout_groups(records, "")
        self.assertEqual(
            [[record.block_id for record in group] for group in groups],
            [["caption-one", "caption-two"], ["caption-three"]],
        )

    def test_deepseek_fast_layout_does_not_treat_text_across_a_heading_as_a_continuation(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_heading_boundary_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        records = [
            module.LayoutTextBlock("before", 1, "text", "A" * 1_800, {}),
            module.LayoutTextBlock("heading", 1, "title", "New section", {}),
            module.LayoutTextBlock("after", 1, "text", "B" * 1_000, {}),
        ]
        groups = module.deepseek_fast_layout_groups(records, "A" * 1_800 + "New section" + "B" * 1_000)
        self.assertEqual(
            [[record.block_id for record in group] for group in groups],
            [["heading"], ["before"], ["after"]],
        )

    def test_deepseek_fast_layout_consolidates_format_repairs(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_fast_format_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        records = [
            module.LayoutTextBlock("a", 1, "text", r"First value is \(x+y\).", {}),
            module.LayoutTextBlock("b", 1, "text", r"Second value is \(m+n\).", {}),
        ]
        calls = []

        def fake_completion(config, messages, **_kwargs):
            calls.append((config, deepcopy(messages)))
            blocks = json.loads(messages[-1]["content"].split("\n", 1)[-1]) if "retry_reasons" in messages[-1]["content"] else None
            if blocks:
                return json.dumps({"translations": [
                    {"id": item["id"], "text": f"数值为 {item['text'].split()[-1]}"}
                    for item in blocks["blocks"]
                ]})
            return json.dumps({"translations": [
                {"id": record.block_id, "text": "数值丢失公式。"} for record in records
            ]})

        config = SimpleNamespace(
            provider_id="deepseek", base_url="https://api.deepseek.com", model="deepseek-chat",
            deepseek_fast_layout_translation=True, custom_translation_instruction="",
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            with (
                patch.object(module.mineru, "ai_chat_completion", side_effect=fake_completion),
                patch.object(module.mineru, "save_translation_request_audit"),
            ):
                module.translate_records(
                    records, config, "简体中文", Path(temporary_dir) / "blocks.json",
                    full_markdown_context="First value is x+y. Second value is m+n.", log=lambda _message: None,
                )
        self.assertEqual(len(calls), 2)
        repair_config, repair_messages = calls[1]
        self.assertEqual(repair_config.thinking_mode, "disabled")
        self.assertEqual(len(repair_messages), 2)
        self.assertIn("极窄的公式/JSON格式修复", repair_messages[-1]["content"])
        self.assertIn("不是事实结论", repair_messages[-1]["content"])
        self.assertIn("自行核对", repair_messages[-1]["content"])
        self.assertEqual(repair_messages[-1]["content"].count('"repair_mode": "symbol-format-only"'), 2)

    def test_deepseek_translation_thinking_preference_is_restored(self):
        settings = _settings_from_dict({
            "translation_deepseek_thinking_enabled": True,
            "translation_deepseek_reasoning_effort": "max",
            "translation_deepseek_fast_layout_enabled": True,
        })
        self.assertTrue(settings.translation_deepseek_thinking_enabled)
        self.assertEqual(settings.translation_deepseek_reasoning_effort, "max")
        self.assertEqual(asdict(settings)["translation_deepseek_reasoning_effort"], "max")
        self.assertTrue(settings.translation_deepseek_fast_layout_enabled)

    def test_gemini_translation_thinking_preference_is_restored(self):
        settings = _settings_from_dict({
            "translation_gemini_thinking_enabled": True,
            "translation_gemini_reasoning_effort": "high",
        })
        self.assertTrue(settings.translation_gemini_thinking_enabled)
        self.assertEqual(settings.translation_gemini_reasoning_effort, "high")
        self.assertEqual(asdict(settings)["translation_gemini_reasoning_effort"], "high")

    def test_gemini_translation_thinking_uses_saved_intensity(self):
        config = AITranslateConfig(
            provider_id="gemini",
            api_key="test-key",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-3.6-flash",
            thinking_mode="enabled",
            reasoning_effort="high",
        )
        response = {"choices": [{"message": {"content": "translated"}}]}
        with patch("LS_pipeline.http_json", return_value=response) as request:
            self.assertEqual(ai_chat_completion(config, [{"role": "user", "content": "translate"}]), "translated")
        payload = request.call_args.args[2]
        self.assertEqual(
            payload["extra_body"],
            {"google": {"thinking_config": {"thinking_level": "high", "include_thoughts": True}}},
        )

    def test_local_absolute_paths_are_redacted_only_in_api_copy(self):
        source = (
            "===== 文档 1: A =====\n"
            "来源: C:\\Users\\Alice\\Desktop\\private\\A\\full.cleaned.md\n"
            r"文件路径: \\server\private\notes.txt" "\n"
            "正文保持不变。"
        )
        expected = "来源: [本地路径已隐藏]"
        self.assertIn(expected, redact_local_paths_for_api_text(source))
        self.assertNotIn("C:\\Users\\Alice", redact_local_paths_for_api_text(source))
        content = sanitize_content_parts_for_api([{"type": "text", "text": source}])
        self.assertIn(expected, content[0]["text"])
        self.assertNotIn("\\\\server", strip_image_url_parts_from_content([{"type": "text", "text": source}]))

    def test_layout_retry_reuses_primary_prefix_and_stays_streaming(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_retry_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        record = module.LayoutTextBlock(
            block_id="p001_b0001",
            page=1,
            block_type="text",
            text="A sufficiently long English sentence that remains deliberately untranslated for retry validation.",
            block={},
        )
        calls = []
        audit_messages = []

        def fake_completion(_config, messages, **kwargs):
            calls.append((messages, kwargs))
            payload = json.dumps({"translations": [{"id": record.block_id, "text": record.text}], "formula_replacements": []})
            if kwargs.get("stream_callback"):
                kwargs["stream_callback"](payload)
            return payload

        def fake_audit(_folder, _request_kind, _config, messages, **_kwargs):
            # 审计只能观察请求，绝不能改动随后传给服务端的 messages。
            audit_messages.append(deepcopy(messages))

        with tempfile.TemporaryDirectory() as temporary_dir:
            with (
                patch.object(module.mineru, "ai_chat_completion", side_effect=fake_completion),
                patch.object(module.mineru, "save_translation_request_audit", side_effect=fake_audit),
            ):
                module.translate_records(
                    [record],
                    config=SimpleNamespace(model="test-model"),
                    target_language="简体中文",
                    cache_path=Path(temporary_dir) / "blocks.json",
                    max_chars=0,
                    log=lambda _message: None,
                )

        self.assertEqual(len(calls), 2)  # first pass plus one bounded retry
        primary_messages, primary_kwargs = calls[0]
        retry_messages, retry_kwargs = calls[1]
        self.assertIsNotNone(primary_kwargs.get("stream_callback"))
        self.assertIsNotNone(retry_kwargs.get("stream_callback"))
        # A reasoning model may be silent for several minutes before its first
        # JSON token.  Layout translation must wait for that response until
        # the user explicitly stops the task, rather than imposing a deadline.
        self.assertIsNone(primary_kwargs.get("timeout"))
        self.assertIsNone(retry_kwargs.get("timeout"))
        self.assertEqual(retry_messages[0], primary_messages[0])
        self.assertEqual(len(retry_messages), 2)
        self.assertNotIn("Input blocks JSON", retry_messages[-1]["content"])
        self.assertEqual(len(audit_messages), 2)
        self.assertEqual(calls[1][0][0], primary_messages[0])
        self.assertEqual(audit_messages[1][0], primary_messages[0])
        self.assertEqual(audit_messages[1], retry_messages)
        self.assertIn('"current_translation"', retry_messages[-1]["content"])
        self.assertIn("repair_mode", retry_messages[-1]["content"])
        self.assertIn("不是事实结论，可能误报或漏报", retry_messages[-1]["content"])

    def test_layout_format_retry_is_surgical_and_bounded(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_format_retry_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        record = module.LayoutTextBlock(
            block_id="p001_formula",
            page=1,
            block_type="text",
            text=r"The value is \(x+y\).",
            block={},
        )
        calls = []

        def fake_completion(config, messages, **kwargs):
            calls.append((config, deepcopy(messages), kwargs))
            payload = json.dumps({"translations": [{"id": record.block_id, "text": "数值为 x+y。"}]})
            if kwargs.get("stream_callback"):
                kwargs["stream_callback"](payload)
            return payload

        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch.object(module.mineru, "ai_chat_completion", side_effect=fake_completion):
                module.translate_records(
                    [record],
                    config=SimpleNamespace(model="test-model"),
                    target_language="简体中文",
                    cache_path=Path(temporary_dir) / "blocks.json",
                    max_chars=0,
                    log=lambda _message: None,
                )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0].thinking_mode, "disabled")
        self.assertEqual(calls[1][0].reasoning_effort, "minimal")
        self.assertIn("retry_details", calls[1][1][-1]["content"])

    def test_layout_english_title_is_retried_for_cjk_translation(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_title_retry_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        record = module.LayoutTextBlock(
            block_id="title_block",
            page=1,
            block_type="title",
            text="6 EXPERIMENTS",
            block={},
        )
        self.assertTrue(module.looks_untranslated(record, record.text, "简体中文"))
        self.assertEqual(module.records_needing_retry([record], {record.block_id: record.text}, "简体中文"), [record])

    def test_layout_author_byline_is_not_treated_as_untranslated(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_author_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        record = module.LayoutTextBlock(
            block_id="byline_block",
            page=1,
            block_type="text",
            text=(
                "Alice B. Carter,<sup>1*</sup> Bruno D. Evans,<sup>1,2*</sup> "
                "Clara F. Gomez,<sup>1*</sup> Daniel H. Ito,<sup>1</sup>"
            ),
            block={},
        )
        self.assertFalse(module.should_check_translation(record))
        self.assertEqual(module.untranslated_or_missing_records([record], {record.block_id: record.text}, "简体中文"), [])
        bibliography = module.LayoutTextBlock(
            block_id="reference_block",
            page=1,
            block_type="text",
            text="A. Author, J. Appl. Phys. 100 (2006), https://doi.org/10.1063/1.2345678",
            block={},
        )
        self.assertTrue(module.bibliography_like_text(bibliography.text))
        self.assertEqual(
            module.records_needing_retry(
                [bibliography],
                {bibliography.block_id: bibliography.text},
                "简体中文",
            ),
            [],
        )
        formula = module.LayoutTextBlock(
            block_id="formula_block",
            page=1,
            block_type="text",
            text=r"The measured value is \(x + y\) under compression.",
            block={},
        )
        translated_formula = r"测得的数值为 \(x+y\)。"
        self.assertEqual(
            module.repair_record_translation(
                formula,
                r"测得的数值为 \\(\\rho_{0}\\)，且 \\(\\mu > 0\\)；路径 C:\\Temp 保持不变。",
            ),
            r"测得的数值为 \(\rho_{0}\)，且 \(\mu > 0\)；路径 C:\\Temp 保持不变。",
            "only complete doubly escaped formulas should lose one escape layer",
        )
        self.assertTrue(module.inline_formula_integrity_issue(formula, translated_formula))
        self.assertEqual(module.inline_formula_retry_issue(formula, translated_formula), "")
        self.assertEqual(
            module.records_needing_retry(
                [formula],
                {formula.block_id: translated_formula},
                "简体中文",
            ),
            [],
        )
        missing_delimiter = "测得的数值为 x+y。"
        self.assertTrue(module.inline_formula_retry_issue(formula, missing_delimiter))
        classified = module.classify_retry_records(
            [formula],
            {formula.block_id: missing_delimiter},
            "简体中文",
        )
        self.assertEqual(classified[0][1], ("formula-structure",))
        variable_list = module.LayoutTextBlock(
            block_id="variable_list",
            page=1,
            block_type="text",
            text=r"where \(n_w, n_g, \rho_w\) and \(p\) denote the variables.",
            block={},
        )
        safely_split = r"式中 \(n_w\)、\(n_g\)、\(\rho_w\) 和 \(p\) 表示这些变量。"
        self.assertEqual(
            module.inline_formula_retry_issue(variable_list, safely_split),
            "",
            "safe formula-list splitting must not spend another translation request",
        )
        bare_variable = module.LayoutTextBlock(
            block_id="bare_variable",
            page=1,
            block_type="text",
            text=r"After normalizing by \(\Delta E\), the result contains parameter M.",
            block={},
        )
        self.assertEqual(
            module.inline_formula_retry_issue(
                bare_variable,
                r"用 \(\Delta E\) 归一化后，结果包含参数 \(M\)。",
            ),
            "",
            "wrapping a source-side bare variable in TeX is a safe formatting repair",
        )
        self.assertEqual(
            module.inline_formula_retry_issue(
                bare_variable,
                r"用 \(\Delta E\) 归一化后，结果包含参数 \(Z\)。",
            ),
            "",
            "additional TeX is audited but must not trigger another paid request",
        )
        redundant_braces = module.LayoutTextBlock(
            block_id="redundant_braces",
            page=1,
            block_type="text",
            text=r"速度为 \(u_{sw}\)。",
            block={},
        )
        self.assertEqual(
            module.inline_formula_retry_issue(redundant_braces, r"速度为 \(u_{{sw}}\)。"),
            "",
            "redundant nested TeX braces must not trigger a paid retry",
        )
        mathrm = module.LayoutTextBlock(
            block_id="mathrm",
            page=1,
            block_type="text",
            text=r"元素 \(\mathrm{H}\)。",
            block={},
        )
        self.assertEqual(
            module.inline_formula_retry_issue(mathrm, r"元素 \(H\)。"),
            "",
            "equivalent mathrm presentation must not trigger a paid retry",
        )
        ocr_marker = module.LayoutTextBlock(
            block_id="ocr_marker",
            page=1,
            block_type="text",
            text=r"参数 \(C_p = 1; (ii)\)。",
            block={},
        )
        self.assertEqual(
            module.inline_formula_retry_issue(ocr_marker, r"参数 \(C_p = 1\) (ii)。"),
            "",
            "an OCR list marker moved out of a formula must not trigger a paid retry",
        )
        self.assertEqual(
            module.inline_formula_retry_issue(mathrm, r"元素 \(Z\)。"),
            "",
            "same-count formula differences are review warnings, not paid retries",
        )
        two_formulas = module.LayoutTextBlock(
            block_id="two_formulas",
            page=1,
            block_type="text",
            text=r"参数为 \(x\) 和 \(y\)。",
            block={},
        )
        self.assertTrue(
            module.inline_formula_retry_issue(two_formulas, r"参数为 \(x\) 和 y。"),
            "a genuinely missing recognizable formula must still retry",
        )
        ocr_equation_reference = module.LayoutTextBlock(
            block_id="ocr_equation_reference",
            page=1,
            block_type="text",
            text=r"The quantity in \(\operatorname { E q . }\)~3! is normalized by \(x\).",
            block={},
        )
        self.assertEqual(
            module.inline_formula_retry_issue(ocr_equation_reference, r"式 (3) 中的量由 \(x\) 归一化。"),
            "",
            "an OCR equation-reference token is prose, not a missing formula",
        )
        ocr_citation = module.LayoutTextBlock(
            block_id="ocr_citation",
            page=1,
            block_type="text",
            text=r"Measured by Yadav et\(a l . ^ { 2 7 }\).",
            block={},
        )
        self.assertEqual(
            module.inline_formula_retry_issue(ocr_citation, "由 Yadav 等人<sup>27</sup>测量。"),
            "",
            "an OCR citation tail is prose, not a missing formula",
        )

    def test_formula_context_sent_to_model_excludes_layout_coordinates(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_formula_payload_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        formula = module.LayoutFormulaItem(
            formula_id="p001_f0001",
            page=1,
            formula_type="interline_equation",
            text=r"\\frac{a}{b}",
            bbox=[49.0, 381.0, 561.0, 488.0],
            spans=[],
        )
        self.assertEqual(
            module.formula_payload([formula]),
            [{"id": "p001_f0001", "page": 1, "type": "interline_equation", "tex": r"\\frac{a}{b}"}],
        )
        self.assertEqual(formula.bbox, [49.0, 381.0, 561.0, 488.0])

    def test_layout_detects_column_fragment_completion(self):
        root = Path(__file__).resolve().parents[1]
        module_spec = importlib.util.spec_from_file_location(
            "layout_translate_preview_boundary_test",
            root / "layout_translate_preview.py",
        )
        module = importlib.util.module_from_spec(module_spec)
        self.assertIsNotNone(module_spec.loader)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_spec.name, None)
        record = module.LayoutTextBlock(
            block_id="column_fragment",
            page=1,
            block_type="text",
            text="The result indicates that the proposed mechanism affects the observed pheno-",
            block={},
        )
        self.assertTrue(module.looks_overexpanded(record, "海马编码与地点和事件有关的记忆。" * 20))


if __name__ == "__main__":
    unittest.main()
