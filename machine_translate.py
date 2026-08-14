from __future__ import annotations

import html
import atexit
import json
import hashlib
import os
import re
import socket
import subprocess
import sys
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable

from websocket import create_connection


MACHINE_TRANSLATION_PROVIDER = "free_machine"
GOOGLE_PROVIDER = "google_free"
BING_PROVIDER = "bing_free"
EDGE_LOCAL_PROVIDER = "edge_local"
MTRAN_SERVER_PROVIDER = "mtranserver_local"
MACHINE_TRANSLATION_PROVIDERS = {
    MACHINE_TRANSLATION_PROVIDER,
    MTRAN_SERVER_PROVIDER,
    EDGE_LOCAL_PROVIDER,
}
WEB_MACHINE_TRANSLATION_PROVIDERS = {GOOGLE_PROVIDER, BING_PROVIDER}
# Web translation endpoints do not publish stable limits for these requests.
# These conservative budgets include boundary-marker overhead. Bing is kept for
# compatibility with older saved configs.
BING_INITIAL_CHARS = 3000
BING_MIN_CHARS = 900
GOOGLE_CHARS = 4500
GOOGLE_BATCH_CHARS = 4200
BING_BATCH_CHARS = 2600
MIN_BATCH_CHARS = 700
MTRAN_SERVER_DEFAULT_BASE_URL = "http://127.0.0.1:8989"
MTRAN_SERVER_DEFAULT_PORT = 8989
MTRAN_SERVER_DEFAULT_PARALLELISM = 4
MTRAN_SERVER_MAX_PARALLELISM = 28
MTRAN_SERVER_ITEM_CHARS = 4800
MTRAN_SERVER_BATCH_CHARS = 24000
MTRAN_SERVER_BATCH_ITEMS = 48
MTRAN_SERVER_REQUEST_TIMEOUT_SECONDS = 120
MTRAN_SERVER_START_TIMEOUT_SECONDS = 25
MTRAN_SERVER_ACADEMIC_MODEL_PAIRS = (
    ("en", "zh-Hans"),
    ("zh-Hans", "en"),
    ("ja", "en"),
    ("ko", "en"),
    ("de", "en"),
    ("fr", "en"),
    ("es", "en"),
)
BING_RETRY_COUNT = 3
BING_RETRY_DELAY_SECONDS = 2.0
BING_SESSION_TTL_SECONDS = 300.0
BING_REQUEST_PARALLELISM = 2
GOOGLE_PROBE_TIMEOUT_SECONDS = 2
FALLBACK_GOOGLE_TIMEOUT_SECONDS = 12
EDGE_LOCAL_MAX_CHARS = 3000
EDGE_START_TIMEOUT_SECONDS = 8
EDGE_WHOLE_DOCUMENT_TIMEOUT_SECONDS = 90
EQUATION_REFERENCE_RE = re.compile(
    r"\b(?:Eq|Eqs|Equation|Equations)\.?\s+"
    r"(?:(?![.;。；]\s).){0,120}"
    r"[（(]\s*[A-Za-z]?\d+[A-Za-z]?\s*[)）]",
    re.I,
)
EQUATION_NUMBER_RE = re.compile(r"[（(]\s*[A-Za-z]?\d+[A-Za-z]?\s*[)）]")


class MachineTranslationError(RuntimeError):
    pass


def is_machine_translation_provider(provider_id: str) -> bool:
    return (provider_id or "").strip().lower() in MACHINE_TRANSLATION_PROVIDERS


def normalize_network_provider_id(provider_id: str) -> str:
    """Migrate retired individual web-provider selections to the combined route."""
    normalized = (provider_id or "").strip().lower()
    if normalized in {GOOGLE_PROVIDER, BING_PROVIDER}:
        return MACHINE_TRANSLATION_PROVIDER
    return normalized


def provider_label(provider_id: str) -> str:
    provider_id = (provider_id or "").strip().lower()
    if provider_id == MTRAN_SERVER_PROVIDER:
        return "本地免费机翻"
    if provider_id == EDGE_LOCAL_PROVIDER:
        return "Edge 本地翻译"
    return "联网免费机翻"


def remove_control_characters(text: str) -> str:
    return "".join(ch for ch in str(text or "") if unicodedata.category(ch)[0] != "C")


LANGUAGE_NAME_ALIASES = {
    "中文": "简体中文", "汉语": "简体中文", "简体汉语": "简体中文",
    "繁体汉语": "繁体中文",
    "英语": "英文", "english": "英文",
    "日语": "日文", "japanese": "日文",
    "韩语": "韩文", "korean": "韩文",
    "德语": "德文", "german": "德文",
    "法语": "法文", "french": "法文",
    "西班牙语": "西班牙文", "spanish": "西班牙文",
    "意大利语": "意大利文", "italian": "意大利文",
    "葡萄牙语": "葡萄牙文", "portuguese": "葡萄牙文",
    "俄语": "俄文", "russian": "俄文",
}


def normalize_language_name(language: str, fallback: str = "") -> str:
    raw = str(language or "").strip()
    if not raw:
        return str(fallback or "").strip()
    return LANGUAGE_NAME_ALIASES.get(raw, LANGUAGE_NAME_ALIASES.get(raw.lower(), raw))


def language_code(language: str, provider_id: str) -> str:
    canonical = normalize_language_name(language)
    normalized = canonical.lower()
    provider_id = (provider_id or "").strip().lower()
    if provider_id == "bing_free":
        mapping = {
            "简体中文": "zh-Hans",
            "繁体中文": "zh-Hant",
            "英文": "en",
            "日文": "ja",
            "韩文": "ko",
            "德文": "de",
            "法文": "fr",
            "西班牙文": "es",
            "意大利文": "it",
            "葡萄牙文": "pt",
            "俄文": "ru",
        }
    else:
        mapping = {
            "简体中文": "zh-CN",
            "繁体中文": "zh-TW",
            "英文": "en",
            "日文": "ja",
            "韩文": "ko",
            "德文": "de",
            "法文": "fr",
            "西班牙文": "es",
            "意大利文": "it",
            "葡萄牙文": "pt",
            "俄文": "ru",
        }
    return mapping.get(canonical, mapping.get(normalized, normalized or "zh-CN"))


def normalize_parallelism(value) -> int:
    try:
        parsed = int(value or MTRAN_SERVER_DEFAULT_PARALLELISM)
    except Exception:
        parsed = MTRAN_SERVER_DEFAULT_PARALLELISM
    if parsed <= 0:
        parsed = MTRAN_SERVER_DEFAULT_PARALLELISM
    return max(1, min(MTRAN_SERVER_MAX_PARALLELISM, parsed))


def mtran_language_code(language: str) -> str:
    normalized = (language or "").strip().lower().replace("_", "-")
    mapping = {
        "auto": "auto",
        "自动": "auto",
        "自动检测": "auto",
        "简体中文": "zh-Hans",
        "中文": "zh-Hans",
        "zh": "zh-Hans",
        "zh-cn": "zh-Hans",
        "zh-hans": "zh-Hans",
        "繁体中文": "zh-Hant",
        "zh-tw": "zh-Hant",
        "zh-hant": "zh-Hant",
        "英文": "en",
        "英语": "en",
        "english": "en",
        "en": "en",
        "日文": "ja",
        "日语": "ja",
        "ja": "ja",
        "韩文": "ko",
        "韩语": "ko",
        "ko": "ko",
        "德文": "de",
        "德语": "de",
        "de": "de",
        "法文": "fr",
        "法语": "fr",
        "fr": "fr",
        "西班牙文": "es",
        "西班牙语": "es",
        "es": "es",
        "俄文": "ru",
        "俄语": "ru",
        "ru": "ru",
        "意大利文": "it",
        "意大利语": "it",
        "it": "it",
        "葡萄牙文": "pt",
        "葡萄牙语": "pt",
        "pt": "pt",
        "荷兰文": "nl",
        "荷兰语": "nl",
        "nl": "nl",
        "波兰文": "pl",
        "波兰语": "pl",
        "pl": "pl",
        "土耳其文": "tr",
        "土耳其语": "tr",
        "tr": "tr",
        "阿拉伯文": "ar",
        "阿拉伯语": "ar",
        "ar": "ar",
    }
    return mapping.get((language or "").strip(), mapping.get(normalized, normalized or "auto"))


def mtran_language_label(code: str) -> str:
    normalized = mtran_language_code(code)
    labels = {
        "auto": "自动检测",
        "zh-Hans": "简体中文",
        "zh-Hant": "繁体中文",
        "en": "英文",
        "ja": "日文",
        "ko": "韩文",
        "de": "德文",
        "fr": "法文",
        "es": "西班牙文",
        "ru": "俄文",
        "it": "意大利文",
        "pt": "葡萄牙文",
        "nl": "荷兰文",
        "pl": "波兰文",
        "tr": "土耳其文",
        "ar": "阿拉伯文",
    }
    return labels.get(normalized, normalized)


def mtran_required_model_pairs(source_language: str, target_language: str) -> list[tuple[str, str]]:
    source = mtran_language_code(source_language)
    target = mtran_language_code(target_language)
    if target == "auto":
        raise MachineTranslationError("本地免费机翻必须指定目标语言。")
    if source == target and source != "auto":
        return []
    if source == "auto":
        source = "en"
    direct_pairs = {(source, target) for source, target in MTRAN_SERVER_ACADEMIC_MODEL_PAIRS}
    direct_pairs.update({("en", "zh-Hant"), ("zh-Hant", "en"), ("en", "ja"), ("en", "ko")})
    if (source, target) in direct_pairs:
        return [(source, target)]
    if source != "en" and target != "en":
        return [(source, "en"), ("en", target)]
    return [(source, target)]


def mtran_installed_model_pairs(model_dir: Path | None = None) -> set[tuple[str, str]]:
    root = Path(model_dir) if model_dir is not None else _resource_root() / "models"
    if not root.is_dir():
        return set()
    pairs: set[tuple[str, str]] = set()
    try:
        children = list(root.iterdir())
    except OSError:
        return set()
    for child in children:
        if not child.is_dir() or "_" not in child.name:
            continue
        source, target = child.name.split("_", 1)
        source_code = mtran_language_code(source)
        target_code = mtran_language_code(target)
        if source_code != "auto" and target_code != "auto" and source_code != target_code:
            pairs.add((source_code, target_code))
    return pairs


def mtran_available_language_pairs(model_dir: Path | None = None) -> set[tuple[str, str]]:
    """Return only directions that the installed direct/English-bridge packs can serve."""
    installed = mtran_installed_model_pairs(model_dir)
    languages = sorted({code for pair in installed for code in pair})
    available: set[tuple[str, str]] = set()
    for source in languages:
        for target in languages:
            if source == target:
                continue
            required = mtran_required_model_pairs(source, target)
            if required and all(pair in installed for pair in required):
                available.add((source, target))
    return available



def split_text_for_machine_translation(text: str, limit: int) -> list[str]:
    text = str(text or "")
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for piece in re.split(r"(\n+|(?<=[.!?。！？；;])\s+)", text):
        if not piece:
            continue
        if current and len(current) + len(piece) > limit:
            parts.append(current)
            current = ""
        while len(piece) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(piece[:limit])
            piece = piece[limit:]
        current += piece
    if current:
        parts.append(current)
    return parts


class WebMachineTranslator:
    def __init__(self, provider_id: str, target_language: str, source_language: str = "auto", timeout: int = 30):
        self.provider_id = (provider_id or "").strip().lower()
        if self.provider_id not in MACHINE_TRANSLATION_PROVIDERS | WEB_MACHINE_TRANSLATION_PROVIDERS:
            raise MachineTranslationError(f"不支持的免费机翻服务: {provider_id}")
        if self.provider_id == MACHINE_TRANSLATION_PROVIDER:
            self.provider_id = GOOGLE_PROVIDER
        self.target_code = language_code(target_language, self.provider_id)
        self.source_code = (
            "auto"
            if not source_language or str(source_language).strip().lower() in {"auto", "自动", "自动检测"}
            else language_code(source_language, self.provider_id)
        )
        self.timeout = timeout
        self.target_language = target_language
        self.source_language = source_language
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self._bing_session_lock = threading.RLock()
        self._bing_session: tuple[str, str, str, str, str] | None = None
        self._bing_session_expires_at = 0.0
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        }

    @property
    def max_chars(self) -> int:
        return BING_INITIAL_CHARS if self.provider_id == "bing_free" else GOOGLE_CHARS

    def translate(self, text: str) -> str:
        text = str(text or "")
        if not text.strip():
            return text
        if self.provider_id == BING_PROVIDER:
            translated_parts = [self._translate_bing_adaptive(part, self.max_chars) for part in split_text_for_machine_translation(text, self.max_chars)]
        else:
            translated_parts = [self._translate_one(part) for part in split_text_for_machine_translation(text, self.max_chars)]
        return "".join(translated_parts)

    def _open_text(self, request: urllib.request.Request) -> str:
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MachineTranslationError(f"免费机翻 HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise MachineTranslationError(f"免费机翻网络请求失败: {exc.reason}") from exc

    def _translate_one(self, text: str) -> str:
        if self.provider_id == "bing_free":
            return self._translate_bing(text)
        return self._translate_google(text)

    def _translate_google(self, text: str) -> str:
        params = urllib.parse.urlencode({"tl": self.target_code, "sl": self.source_code, "q": text})
        request = urllib.request.Request(
            f"https://translate.google.com/m?{params}",
            headers={
                **self.headers,
                "User-Agent": "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; SV1)",
            },
            method="GET",
        )
        response_text = self._open_text(request)
        match = re.findall(r'(?s)class="(?:t0|result-container)">(.*?)<', response_text)
        if not match:
            raise MachineTranslationError("Google 免费机翻没有返回可解析的译文。")
        return remove_control_characters(html.unescape(match[0]))

    def probe_google(self) -> None:
        probe_text = "network test"
        params = urllib.parse.urlencode({"tl": self.target_code, "sl": self.source_code, "q": probe_text})
        request = urllib.request.Request(
            f"https://translate.google.com/m?{params}",
            headers={
                **self.headers,
                "User-Agent": "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; SV1)",
            },
            method="GET",
        )
        response_text = self._open_text(request)
        if not re.findall(r'(?s)class="(?:t0|result-container)">(.*?)<', response_text):
            raise MachineTranslationError("Google 免费机翻探测失败：返回页面中未找到可解析译文。")

    def clone_for_worker(self) -> "WebMachineTranslator":
        """Create an isolated cookie jar for a concurrently translated batch."""
        return WebMachineTranslator(
            self.provider_id,
            self.target_language,
            source_language=self.source_language,
            timeout=self.timeout,
        )

    def _bing_sid(self, force_refresh: bool = False) -> tuple[str, str, str, str, str]:
        with self._bing_session_lock:
            if not force_refresh and self._bing_session and time.monotonic() < self._bing_session_expires_at:
                return self._bing_session
            session = self._fetch_bing_sid()
            self._bing_session = session
            self._bing_session_expires_at = time.monotonic() + BING_SESSION_TTL_SECONDS
            return session

    def _fetch_bing_sid(self) -> tuple[str, str, str, str, str]:
        request = urllib.request.Request("https://www.bing.com/translator", headers=self.headers, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                response_text = response.read().decode("utf-8", errors="replace")
                final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MachineTranslationError(f"Bing 免费机翻初始化 HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise MachineTranslationError(f"Bing 免费机翻初始化失败: {exc.reason}") from exc

        ig_match = re.search(r'"ig":"(.*?)"', response_text)
        iid_matches = re.findall(r'data-iid="(.*?)"', response_text)
        token_match = re.search(r"params_AbusePreventionHelper\s*=\s*\[(.*?),\"(.*?)\",", response_text)
        if not ig_match or not iid_matches or not token_match:
            raise MachineTranslationError("Bing 免费机翻页面参数解析失败。")
        if "/translator" in final_url:
            base_url = final_url.split("/translator", 1)[0].rstrip("/") + "/"
        else:
            base_url = "https://www.bing.com/"
        return base_url, ig_match.group(1), iid_matches[-1], token_match.group(1), token_match.group(2)

    def _invalidate_bing_session(self) -> None:
        with self._bing_session_lock:
            self._bing_session = None
            self._bing_session_expires_at = 0.0

    def _translate_bing(self, text: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, BING_RETRY_COUNT + 1):
            try:
                return self._translate_bing_once(text, refresh_session=attempt > 1)
            except Exception as exc:
                last_error = exc
                if attempt < BING_RETRY_COUNT:
                    # A rejected page token is fixed by refreshing it, not by
                    # waiting. Other transient failures retain the backoff.
                    if "会话参数失效" not in str(exc):
                        time.sleep(BING_RETRY_DELAY_SECONDS)
        if last_error:
            raise last_error
        raise MachineTranslationError("Bing 免费机翻失败。")

    def _translate_bing_once(self, text: str, refresh_session: bool = False) -> str:
        base_url, ig, iid, key, token = self._bing_sid(force_refresh=refresh_session)
        data = urllib.parse.urlencode(
            {
                "fromLang": "auto-detect" if self.source_code == "auto" else self.source_code,
                "to": self.target_code,
                "text": text,
                "token": token,
                "key": key,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}ttranslatev3?IG={urllib.parse.quote(ig)}&IID={urllib.parse.quote(iid)}",
            data=data,
            headers={
                **self.headers,
                "Content-Type": "application/x-www-form-urlencoded",
                # Bing now validates that the translation request originated
                # from its Translator page.  The session cookies alone result
                # in a JSON-wrapped 400 response.
                "Referer": "https://www.bing.com/translator",
            },
            method="POST",
        )
        response_text = self._open_text(request)
        try:
            payload = json.loads(response_text)
            if isinstance(payload, dict) and payload.get("statusCode"):
                self._invalidate_bing_session()
                raise MachineTranslationError("Bing 网页翻译会话参数失效。")
            return remove_control_characters(str(payload[0]["translations"][0]["text"]))
        except Exception as exc:
            raise MachineTranslationError(f"Bing 免费机翻返回格式无法解析: {response_text[:300]}") from exc

    def _translate_bing_adaptive(self, text: str, limit: int) -> str:
        text = str(text or "")
        if len(text) <= BING_MIN_CHARS:
            return self._translate_bing(text)
        try:
            return self._translate_bing(text)
        except Exception:
            next_limit = max(BING_MIN_CHARS, min(limit // 2, len(text) // 2 or BING_MIN_CHARS))
            if next_limit >= len(text):
                next_limit = max(BING_MIN_CHARS, len(text) // 2)
            if next_limit <= 0 or next_limit >= len(text):
                raise
            parts = split_text_for_machine_translation(text, next_limit)
            if len(parts) <= 1:
                raise
            return "".join(self._translate_bing_adaptive(part, next_limit) for part in parts)



def _resource_root() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "resources" / "mtranserver"


def _bundled_mtran_executable() -> Path | None:
    bin_dir = _resource_root() / "bin"
    names = (
        "mtranserver.exe",
        "mtranserver-windows-amd64.exe",
        "mtranserver-4.0.33-windows-amd64.exe",
    )
    for name in names:
        path = bin_dir / name
        if path.exists():
            return path
    candidates = sorted(bin_dir.glob("mtranserver*.exe")) if bin_dir.exists() else []
    return candidates[0] if candidates else None


def _port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def _find_available_ports(count: int, start: int = MTRAN_SERVER_DEFAULT_PORT) -> list[int]:
    ports: list[int] = []
    port = int(start)
    while len(ports) < count and port < start + 500:
        if not _port_is_open(port):
            ports.append(port)
        port += 1
    if len(ports) < count:
        raise MachineTranslationError("无法为本地免费机翻找到足够的可用端口。")
    return ports


class _MTranServerProcessPool:
    _lock = threading.Lock()
    _processes: dict[int, subprocess.Popen] = {}
    _registered_cleanup = False
    _active_jobs = 0

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen) -> None:
        """Stop a server and every worker it created.

        mtranserver starts Bun worker processes.  ``Popen.terminate()`` only
        stops the server parent on Windows, leaving those workers (and their
        loaded models) behind.  taskkill's /T flag follows the child tree.
        """
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                except OSError:
                    pass

    @classmethod
    def begin_job(cls) -> None:
        with cls._lock:
            cls._active_jobs += 1

    @classmethod
    def end_job(cls) -> None:
        with cls._lock:
            if cls._active_jobs > 0:
                cls._active_jobs -= 1
            should_cleanup = cls._active_jobs == 0
        if should_cleanup:
            cls.cleanup()

    @classmethod
    def cleanup(cls):
        with cls._lock:
            processes = list(cls._processes.values())
            cls._processes.clear()
        for process in processes:
            try:
                cls._terminate_process_tree(process)
            except Exception:
                pass

    @classmethod
    def ensure(cls, count: int, log: Callable[[str], None] | None = None) -> list[str] | None:
        executable = _bundled_mtran_executable()
        if executable is None:
            return None
        count = normalize_parallelism(count)
        root = _resource_root()
        config_dir = root / "config"
        model_dir = root / "models"
        records_path = config_dir / "records.json"
        if not records_path.exists() or not model_dir.exists():
            raise MachineTranslationError(
                "本地免费机翻资源不完整：缺少 resources/mtranserver/config/records.json 或 models 目录。"
            )
        with cls._lock:
            if not cls._registered_cleanup:
                atexit.register(cls.cleanup)
                cls._registered_cleanup = True
            alive_ports = [port for port, process in cls._processes.items() if process.poll() is None and _port_is_open(port)]
            if len(alive_ports) >= count:
                return [f"http://127.0.0.1:{port}" for port in sorted(alive_ports)[:count]]
            for port, process in list(cls._processes.items()):
                if process.poll() is not None:
                    cls._processes.pop(port, None)
            needed = count - len(cls._processes)
            ports = _find_available_ports(needed)
            try:
                for port in ports:
                    env = os.environ.copy()
                    env.update(
                        {
                            "MT_HOST": "127.0.0.1",
                            "MT_PORT": str(port),
                            "MT_ENABLE_UI": "false",
                            "MT_OFFLINE": "true",
                            "MT_CONFIG_DIR": str(config_dir),
                            "MT_MODEL_DIR": str(model_dir),
                            "MT_LOG_LEVEL": "warn",
                            "MT_WORKER_IDLE_TIMEOUT": "3600",
                        }
                    )
                    startupinfo = None
                    creationflags = 0
                    if os.name == "nt":
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        creationflags = subprocess.CREATE_NO_WINDOW
                    process = subprocess.Popen(
                        [str(executable), "--host", "127.0.0.1", "--port", str(port), "--offline", "--ui=false"],
                        cwd=str(root),
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        startupinfo=startupinfo,
                        creationflags=creationflags,
                    )
                    cls._processes[port] = process
                deadline = time.time() + MTRAN_SERVER_START_TIMEOUT_SECONDS
                for port in sorted(cls._processes):
                    while time.time() < deadline and not _port_is_open(port):
                        time.sleep(0.15)
                    if not _port_is_open(port):
                        raise MachineTranslationError(f"本地免费机翻服务端口 {port} 启动超时。")
            except Exception:
                # A partial startup must not leave a half-created server pool
                # consuming several gigabytes in the background.
                processes = list(cls._processes.values())
                cls._processes.clear()
                for process in processes:
                    cls._terminate_process_tree(process)
                raise
            endpoints = [f"http://127.0.0.1:{port}" for port in sorted(cls._processes)[:count]]
        if log:
            log(f"本地机翻引擎已就绪（启动 {len(endpoints)} 个并发服务）。")
        return endpoints


class MTranServerTranslator:
    provider_id = MTRAN_SERVER_PROVIDER
    current_provider = MTRAN_SERVER_PROVIDER

    def __init__(
        self,
        target_language: str,
        source_language: str = "auto",
        base_url: str = "",
        api_key: str = "",
        parallelism: int = MTRAN_SERVER_DEFAULT_PARALLELISM,
        timeout: int = MTRAN_SERVER_REQUEST_TIMEOUT_SECONDS,
        log=None,
    ):
        self.target_language = target_language
        self.source_language = source_language or "auto"
        self.target_code = mtran_language_code(target_language)
        self.source_code = mtran_language_code(source_language or "auto")
        self.base_url = (base_url or MTRAN_SERVER_DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or ""
        self.parallelism = normalize_parallelism(parallelism)
        self.timeout = timeout
        self.log = log
        endpoints = _MTranServerProcessPool.ensure(self.parallelism, log=log)
        self.endpoints = endpoints or [self.base_url]
        if endpoints:
            self._validate_bundled_model_pairs()

    def _validate_bundled_model_pairs(self) -> None:
        model_dir = _resource_root() / "models"
        missing: list[str] = []
        for source, target in mtran_required_model_pairs(self.source_code, self.target_code):
            if source == target:
                continue
            pair_dir = model_dir / f"{source}_{target}"
            if not pair_dir.exists():
                missing.append(f"{source}->{target}")
        if missing:
            readable = "、".join(missing)
            raise MachineTranslationError(f"本地免费机翻缺少所需语言包: {readable}。请确认已内置扩展学术包。")

    def begin_job(self) -> None:
        _MTranServerProcessPool.begin_job()

    def end_job(self) -> None:
        _MTranServerProcessPool.end_job()

    @property
    def max_chars(self) -> int:
        return MTRAN_SERVER_ITEM_CHARS

    def should_translate_text(self, text: str) -> bool:
        text = str(text or "").strip()
        if not text or re.fullmatch(r"[\W\d_]+", text, flags=re.UNICODE):
            return False
        if self.source_code == "auto":
            return bool(re.search(r"[A-Za-z]{3,}|[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text))
        if self.source_code in {"ja", "zh-Hans", "zh-Hant", "ko"}:
            return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text))
        return bool(re.search(r"[A-Za-z]{2,}", text))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post_json(self, endpoint: str, path: str, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{endpoint.rstrip('/')}/{path.lstrip('/')}",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MachineTranslationError(f"本地免费机翻 HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise MachineTranslationError(f"本地免费机翻连接失败: {exc.reason}") from exc
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:
            raise MachineTranslationError(f"本地免费机翻返回无法解析: {body[:300]}") from exc
        raise MachineTranslationError(f"本地免费机翻返回格式异常: {body[:300]}")

    def translate(self, text: str) -> str:
        return self.translate_on_endpoint(text)

    def translate_on_endpoint(self, text: str, endpoint_index: int = 0) -> str:
        text = str(text or "")
        if not text.strip():
            return text
        payload = {"from": self.source_code, "to": self.target_code, "text": text, "html": False}
        endpoint = self.endpoints[endpoint_index % len(self.endpoints)]
        data = self._post_json(endpoint, "/translate", payload)
        return remove_control_characters(str(data.get("result") or ""))

    def translate_batch(self, texts: list[str], endpoint_index: int = 0) -> list[str]:
        if not texts:
            return []
        endpoint = self.endpoints[endpoint_index % len(self.endpoints)]
        payload = {"from": self.source_code, "to": self.target_code, "texts": [str(text or "") for text in texts], "html": False}
        data = self._post_json(endpoint, "/translate/batch", payload)
        results = data.get("results")
        if not isinstance(results, list) or len(results) != len(texts):
            raise MachineTranslationError("本地免费机翻 batch 返回数量与请求数量不一致。")
        return [remove_control_characters(str(item or "")) for item in results]


def create_translator(
    provider_id: str,
    target_language: str,
    source_language: str = "auto",
    log=None,
    base_url: str = "",
    api_key: str = "",
    parallelism: int = MTRAN_SERVER_DEFAULT_PARALLELISM,
    edge_download_consent=None,
):
    provider_id = (provider_id or "").strip().lower()
    if provider_id == MTRAN_SERVER_PROVIDER:
        return MTranServerTranslator(
            target_language,
            source_language=source_language,
            base_url=base_url,
            api_key=api_key,
            parallelism=parallelism,
            log=log,
        )
    if provider_id == EDGE_LOCAL_PROVIDER:
        return EdgeLocalTranslator(
            target_language,
            source_language=source_language,
            log=log,
            download_consent=edge_download_consent,
        )
    return WebMachineTranslator(provider_id, target_language, source_language=source_language)


class EdgeLocalTranslator:
    """Translate through Edge's on-device Translator API in a persistent hidden profile.

    The API deliberately requires a gesture when it needs to download a model.
    This bridge therefore uses Edge only when the language pair is already
    available; callers can safely fall back to a web provider otherwise.
    """

    provider_id = EDGE_LOCAL_PROVIDER
    current_provider = provider_id

    def __init__(self, target_language: str, source_language: str = "auto", timeout: int = 30, log=None, download_consent=None):
        self.target_language = target_language
        self.source_language = source_language or "auto"
        self.target_code = language_code(target_language, BING_PROVIDER)
        self.source_code = language_code(source_language, BING_PROVIDER)
        self.timeout = timeout
        self.log = log
        self.download_consent = download_consent
        self._download_declined = False
        self._process: subprocess.Popen | None = None
        self._edge_process_pid: int | None = None
        self._ws = None
        self._message_id = 0
        self._session_created = False
        # A normal translation finishes through end_job(); this is only a
        # process-exit safety net for an abrupt application shutdown.
        atexit.register(self.close)

    @property
    def max_chars(self) -> int:
        return EDGE_LOCAL_MAX_CHARS

    @staticmethod
    def _edge_executable() -> Path | None:
        candidates = []
        for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        candidates.append(Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"))
        candidates.append(Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"))
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _profile_dir() -> Path:
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache")
        return root / "LitMTrans" / "edge-local-translation"

    @classmethod
    def _host_page(cls) -> Path:
        directory = cls._profile_dir()
        directory.mkdir(parents=True, exist_ok=True)
        page = directory / "translator-host.html"
        if not page.exists():
            page.write_text(
                "<!doctype html><meta charset='utf-8'><title>LitMTrans Edge Local Translation</title>",
                encoding="utf-8",
            )
        return page

    def _stop(self) -> None:
        if self._ws is not None:
            try:
                # Give Edge a chance to flush its component/model registry.
                # Killing it immediately after a completed download can leave
                # the model files present but not registered for next launch.
                self._message_id += 1
                self._ws.send(json.dumps({"id": self._message_id, "method": "Browser.close"}))
                self._ws.settimeout(2)
                try:
                    self._ws.recv()
                except Exception:
                    pass
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        process, self._process = self._process, None
        edge_process_pid, self._edge_process_pid = self._edge_process_pid, None
        if os.name == "nt":
            # Edge may relaunch itself, making Popen's original PID exit while
            # the actual browser child remains alive.  Prefer the PID found
            # from this task's private DevTools port.
            target_pid = edge_process_pid or (process.pid if process is not None else None)
            if target_pid is not None:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self._pid_is_running(target_pid):
                    time.sleep(0.1)
                if not self._pid_is_running(target_pid):
                    self._session_created = False
                    return
                try:
                    subprocess.run(["taskkill", "/PID", str(target_pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass
        elif process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                except OSError:
                    pass
        self._session_created = False

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        try:
            output = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=2,
            ).stdout
            return str(int(pid)) in output
        except (OSError, subprocess.SubprocessError):
            return True

    @staticmethod
    def _edge_pid_for_debug_port(port: int) -> int | None:
        if os.name != "nt":
            return None
        try:
            output = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=3,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        suffix = f":{int(port)}"
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(suffix) and parts[-1].isdigit():
                return int(parts[-1])
        return None

    def close(self) -> None:
        self._stop()

    def end_job(self) -> None:
        self.close()

    def _start(self) -> None:
        if self._ws is not None:
            return
        if os.name != "nt":
            raise MachineTranslationError("Edge 本地翻译仅支持 Windows。")
        if self.source_code == "auto":
            raise MachineTranslationError("Edge 本地翻译需要指定源语言，自动检测时无法使用。")
        executable = self._edge_executable()
        if executable is None:
            raise MachineTranslationError("未检测到 Microsoft Edge。")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self._process = subprocess.Popen(
            [
                str(executable), "--headless=new", f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1", "--remote-allow-origins=*", f"--user-data-dir={self._profile_dir()}",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                self._host_page().as_uri(),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + EDGE_START_TIMEOUT_SECONDS
        tab = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as response:
                    tabs = json.loads(response.read().decode("utf-8"))
                tab = next((item for item in tabs if item.get("title") == "LitMTrans Edge Local Translation"), None)
                if tab:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        if not tab:
            self._stop()
            raise MachineTranslationError("Edge 本地翻译启动超时。")
        self._edge_process_pid = self._edge_pid_for_debug_port(port)
        try:
            self._ws = create_connection(tab["webSocketDebuggerUrl"], timeout=self.timeout, http_proxy_host=None, http_proxy_port=None)
        except Exception as exc:
            self._stop()
            raise MachineTranslationError(f"无法连接 Edge 本地翻译: {exc}") from exc

    def _evaluate(self, expression: str):
        self._start()
        assert self._ws is not None
        self._message_id += 1
        message_id = self._message_id
        self._ws.send(json.dumps({"id": message_id, "method": "Runtime.evaluate", "params": {"expression": expression, "awaitPromise": True, "returnByValue": True}}))
        while True:
            response = json.loads(self._ws.recv())
            if response.get("id") != message_id:
                continue
            result = response.get("result", {}).get("result", {})
            if "exceptionDetails" in response.get("result", {}):
                detail = response["result"]["exceptionDetails"].get("text", "Edge 脚本执行失败")
                raise MachineTranslationError(f"Edge 本地翻译错误: {detail}")
            if result.get("type") == "undefined":
                return None
            return result.get("value")

    def _command(self, method: str, params: dict | None = None) -> dict:
        self._start()
        assert self._ws is not None
        self._message_id += 1
        message_id = self._message_id
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            response = json.loads(self._ws.recv())
            if response.get("id") == message_id:
                return response.get("result", {})

    def _has_cached_language_model(self) -> bool:
        """Whether this private Edge profile already contains this pair's pack.

        Edge currently reports ``downloadable`` during startup even when its
        language-pack component is already present.  A pack is stored using
        base language codes (for example ``en-zh`` for ``en`` → ``zh-Hans``).
        Do not show a second consent prompt merely to activate that cache.
        """
        source = self.source_code.split("-", 1)[0].lower()
        target = self.target_code.split("-", 1)[0].lower()
        pair_dir = self._profile_dir() / "EdgeTranslateKitLanguagePack" / f"{source}-{target}"
        try:
            return any(item.is_file() and item.stat().st_size >= 1024 * 1024 for item in pair_dir.rglob("*"))
        except OSError:
            return False

    def _download_model(self, report_progress: bool = True) -> None:
        options = json.dumps({"sourceLanguage": self.source_code, "targetLanguage": self.target_code})
        button = self._evaluate(
            "(()=>{const b=document.createElement('button');b.id='litmtrans-download-model';"
            "b.textContent='Download';document.body.appendChild(b);"
            "window.__litmtransDownloadState={done:false,error:'',loaded:0,total:0};"
            f"b.onclick=async()=>{{try{{const s=await Translator.create({{...{options},monitor:m=>m.addEventListener('downloadprogress',e=>{{window.__litmtransDownloadState.loaded=e.loaded;window.__litmtransDownloadState.total=e.total}})}});s.destroy();window.__litmtransDownloadState.done=true}}catch(e){{window.__litmtransDownloadState.error=e.name+': '+e.message}}}};"
            "const r=b.getBoundingClientRect();return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2})})()"
        )
        point = json.loads(button)
        self._command("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1})
        self._command("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1})
        last_percent = -1
        while True:
            state = json.loads(self._evaluate("JSON.stringify(window.__litmtransDownloadState)"))
            total = float(state.get("total") or 0)
            percent = int(float(state.get("loaded") or 0) * 100 / total) if total else 0
            if report_progress and self.log and percent != last_percent and (percent == 100 or percent // 5 != last_percent // 5):
                self.log(f"正在下载 Edge 本地翻译模型：{percent}%…")
            last_percent = percent
            if state.get("error"):
                raise MachineTranslationError(f"Edge 本地翻译语言模型下载失败: {state['error']}")
            if state.get("done"):
                return
            time.sleep(0.25)

    def ensure_available(self, allow_download: bool = False, report_download_progress: bool = True) -> None:
        availability = self._evaluate(
            f"Translator.availability({json.dumps({'sourceLanguage': self.source_code, 'targetLanguage': self.target_code})})"
        )
        if availability != "available":
            if availability == "downloadable" and allow_download:
                self._download_model(report_progress=report_download_progress)
                return self.ensure_available(allow_download=False, report_download_progress=report_download_progress)
            self._stop()
            if availability in {"downloadable", "downloading"}:
                raise MachineTranslationError("Edge 本地翻译语言模型尚未下载。")
            raise MachineTranslationError(f"Edge 本地翻译不支持该语言对（{availability or 'unknown'}）。")

    def _ensure_session(self) -> None:
        if self._session_created:
            return
        try:
            self.ensure_available()
        except MachineTranslationError as exc:
            if "尚未下载" not in str(exc) or not callable(self.download_consent):
                raise
            if self._download_declined:
                raise MachineTranslationError("用户未同意下载 Edge 本地翻译语言模型。") from exc
            if self._has_cached_language_model():
                if self.log:
                    self.log("正在启用已下载的 Edge 本地翻译语言模型…")
                self._start()
                self.ensure_available(allow_download=True, report_download_progress=False)
            else:
                if not self.download_consent(self.source_language, self.target_language):
                    self._download_declined = True
                    raise MachineTranslationError("用户未同意下载 Edge 本地翻译语言模型。") from exc
                if self.log:
                    self.log("用户已同意，正在下载 Edge 本地翻译语言模型…")
                self._start()
                self.ensure_available(allow_download=True)
        options = json.dumps({"sourceLanguage": self.source_code, "targetLanguage": self.target_code})
        self._evaluate(f"(async()=>{{window.__litmtransEdgeTranslator=await Translator.create({options});return true}})()")
        self._session_created = True
        if self.log:
            self.log("Edge 本地翻译模型已准备就绪。")

    def translate(self, text: str) -> str:
        text = str(text or "")
        if not text.strip():
            return text
        # the source glyph is unknown while keeping the request translatable.
        if "\ufffd" in text:
            text = text.replace("\ufffd", "?")
            if self.log:
                self.log("检测到 OCR 缺失字符（\ufffd），已用 ? 安全替换后交由 Edge 本地翻译。")
        self._ensure_session()
        try:
            result = self._translate_once(text)
            if not isinstance(result, str) or not result.strip():
                raise MachineTranslationError("Edge 本地翻译没有返回译文。")
            return remove_control_characters(result)
        except Exception as exc:
            shortened, mappings = shorten_edge_protected_placeholders(text)
            if is_edge_placeholder_sensitive_error(exc):
                if self.log:
                    if mappings:
                        self.log("当前文本块触发 Edge 占位符异常，正在重建会话并使用短保护标记重试…")
                    else:
                        self.log("当前文本块触发 Edge 本地翻译异常，正在重建会话并原文重试…")
                # Edge can leave this Translator object poisoned after a
                # rejected translate() promise.  A valid fallback request in
                # the same session would fail again, so rebuild it first.
                self.close()
                try:
                    self._ensure_session()
                    retry_result = self._translate_once(shortened if mappings else text)
                    restored = restore_edge_short_placeholders(retry_result, mappings) if mappings else retry_result
                    if isinstance(restored, str) and restored.strip():
                        return remove_control_characters(restored)
                except Exception as retry_exc:
                    self.close()
                    if not mappings:
                        return self._translate_by_segments_after_error(text, retry_exc)
                    return self._translate_by_protected_segments_after_error(text, retry_exc)
                # Edge can return a non-empty response while rewriting a
                # short marker. Restoration then yields an empty string even
                # though no exception was raised; use the same local
                # marker-preserving recovery path rather than aborting.
                self.close()
                if mappings:
                    return self._translate_by_protected_segments_after_error(text, exc)
                return self._translate_by_segments_after_error(text, exc)
            self.close()
            raise

    def _translate_by_segments_after_error(self, text: str, original_error: Exception) -> str:
        split = split_edge_local_retry_text(text)
        if split is None:
            raise original_error
        left, separator, right = split
        if self.log:
            self.log("当前文本块重试仍失败，正在拆分为两段继续使用 Edge 本地翻译…")
        translated_parts: list[str] = []
        try:
            for part in (left, right):
                # Each fragment gets a fresh Translator object. The previous
                # rejected promise may leave its Edge session unusable.
                self._ensure_session()
                result = self._translate_once(part)
                if not isinstance(result, str) or not result.strip():
                    raise original_error
                translated_parts.append(remove_control_characters(result))
                self.close()
        except Exception:
            self.close()
            raise
        return separator.join(translated_parts)

    def _translate_by_protected_segments_after_error(self, text: str, original_error: Exception) -> str:
        """Keep protected formula/citation markers local when Edge rejects them."""
        parts = re.split(f"({EDGE_PROTECTED_PLACEHOLDER_RE.pattern})", str(text or ""))
        if len(parts) < 3:
            raise original_error
        if self.log:
            self.log("短保护标记重试仍失败，正在保留公式/引文标记并分段使用 Edge 本地翻译…")
        translated_parts: list[str] = []
        try:
            for index, part in enumerate(parts):
                if index % 2:
                    translated_parts.append(part)
                elif part.strip():
                    translated_parts.append(self.translate(part))
                else:
                    translated_parts.append(part)
        except Exception:
            self.close()
            raise
        return "".join(translated_parts)

    def _translate_once(self, text: str):
        previous_timeout = self._ws.gettimeout() if self._ws is not None else self.timeout
        try:
            if len(text) > EDGE_LOCAL_MAX_CHARS and self._ws is not None:
                self._ws.settimeout(max(self.timeout, EDGE_WHOLE_DOCUMENT_TIMEOUT_SECONDS))
            return self._evaluate(f"window.__litmtransEdgeTranslator.translate({json.dumps(text)})")
        finally:
            if self._ws is not None:
                self._ws.settimeout(previous_timeout)


class FallbackMachineTranslator:
    def __init__(self, target_language: str, source_language: str = "auto", log=None, edge_download_consent=None):
        self.target_language = target_language
        self.source_language = source_language
        self.log = log
        self.edge_download_consent = edge_download_consent
        self.current_provider = GOOGLE_PROVIDER
        self._translator = WebMachineTranslator(
            GOOGLE_PROVIDER,
            target_language,
            source_language=source_language,
            timeout=FALLBACK_GOOGLE_TIMEOUT_SECONDS,
        )
        self._google_probe_checked = False
        self._google_probe_error = ""
        self._google_probe_succeeded = False
        self._bing_translator: WebMachineTranslator | None = None

    def end_job(self) -> None:
        return None

    @property
    def max_chars(self) -> int:
        return min(self._translator.max_chars, EDGE_LOCAL_MAX_CHARS)

    def _probe_google_if_needed(self) -> None:
        if self.current_provider != GOOGLE_PROVIDER or self._google_probe_checked:
            return
        probe = WebMachineTranslator(
            GOOGLE_PROVIDER,
            self.target_language,
            source_language=self.source_language,
            timeout=GOOGLE_PROBE_TIMEOUT_SECONDS,
        )
        try:
            probe.probe_google()
            self._google_probe_checked = True
            self._google_probe_error = ""
            self._google_probe_succeeded = True
        except Exception as exc:
            self._google_probe_checked = True
            self._google_probe_error = str(exc)
            self._google_probe_succeeded = False
            raise MachineTranslationError(f"Google 网页翻译快速探测失败: {exc}") from exc

    def _switch_to_bing(self, _reason: Exception) -> WebMachineTranslator:
        if self.current_provider != BING_PROVIDER:
            self.current_provider = BING_PROVIDER
            self._bing_translator = WebMachineTranslator(
                BING_PROVIDER,
                self.target_language,
                source_language=self.source_language,
            )
            if self.log:
                self.log("正在尝试Bing翻译，该服务较慢，请稍等。")
        return self._bing_translator or WebMachineTranslator(
            BING_PROVIDER,
            self.target_language,
            source_language=self.source_language,
        )

    def translate(self, text: str) -> str:
        if self.current_provider == BING_PROVIDER:
            return self._switch_to_bing(MachineTranslationError("Google 已被判定不可达")).translate(text)
        try:
            self._probe_google_if_needed()
            return self._translator.translate(text)
        except Exception as exc:
            return self._switch_to_bing(exc).translate(text)


def create_fallback_translator(target_language: str, source_language: str = "auto", log=None, edge_download_consent=None) -> FallbackMachineTranslator:
    return FallbackMachineTranslator(target_language, source_language=source_language, log=log, edge_download_consent=edge_download_consent)


def should_translate_text(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return False
    if re.fullmatch(r"[\W\d_]+", text, flags=re.UNICODE):
        return False
    return bool(re.search(r"[A-Za-z]{3,}", text))


# Keep the complete Markdown image construct together.  Protecting only the
# ``IMAGE_###`` alt text leaves ``![ ]( )`` exposed to local NMT; it commonly
# drops one of those delimiters while translating a caption in the same block.
MARKDOWN_IMAGE_INLINE_RE = re.compile(r"!\[[^\]\n]*\]\([^\)\n]*\)")
MARKDOWN_IMAGE_REFERENCE_RE = re.compile(r"!\[[^\]\n]*\]\[[^\]\n]*\]")
# MinerU sometimes closes a TeX span immediately before the numerical part of
# a statistic, for example ``\\(( t_6 = 8.57, P &lt;\\) 0.001)``.  That is
# visually readable in the source preview but is not a complete unit for an
# NMT model.  Detect the entire construct before the normal inline pass.
FRAGMENTED_STATISTIC_RE = re.compile(
    r"\\\((?P<formula>[^\n]{0,360}?)\\\)"
    r"(?P<tail>\s*(?:(?:&(?:lt|gt);|[<>]=?)\s*)?\d[\d\s.,]*\)?)",
    re.I,
)
UNPROTECTED_TEX_RE = re.compile(r"\\(?:[()\[\]]|[A-Za-z]+)")
# Prefixes belong to document structure, so they must survive NMT byte-for-byte.
# The expression deliberately covers Roman main sections (``IV.``), lettered
# subsections (``A.``), and numeric list levels (``1.``, ``1.2.``, ``(1)``).
SECTION_PREFIX_RE = re.compile(
    r"^\s*(?P<marker>(?:[IVXLCDM]+|[A-Z]|\(?\d+(?:\.\d+)*\)?))(?P<punct>[.)])(?=\s+)"
)
FIGURE_OR_TABLE_CAPTION_RE = re.compile(
    r"^\s*(?P<label>fig(?:ure)?|table)\.?\s*(?P<number>\d+[A-Za-z]?)\.?\s*",
    re.I,
)

# MinerU's plain-text export intentionally drops superscript styling.  Without
# restoring the most unambiguous citation forms before NMT, a local model sees
# e.g. ``Zhang 6 demonstrated`` or ``rupture. 5 However`` and treats the
# reference number as normal prose.  That is the root cause of ``张6号`` and
# ``17次`` in academic translations.
AUTHOR_CITATION_RE = re.compile(r"\b([A-Z][a-z]{1,})\s+(\d{1,3})(?=\s+[a-z])")
TRAILING_CITATION_RE = re.compile(
    r"(?<=[A-Za-z\)])([.,;])\s+(\d{1,3}(?:\s*[–-]\s*\d{1,3})?(?:\s*,\s*\d{1,3})*)"
    r"(?=\s+(?:[A-Z][a-z]|and\b|or\b|with\b|thereby\b|while\b|including\b|contraction\b|collapse\b|"
    r"investigat(?:e|ed|es|ing)\b|investi-\s*gating\b|as\b|stud(?:y|ied|ies)\b|report(?:ed|s)?\b|demonstrat(?:e|ed|es|ing)\b|"
    r"show(?:ed|s)?\b|observ(?:e|ed|es|ing)\b|analy[sz](?:e|ed|es|ing)\b))"
)


def restore_plain_text_citation_markup(text: str) -> str:
    """Recover only high-confidence lost superscript citations from layout text."""
    raw = str(text or "")
    if "<sup" in raw.lower():
        return raw
    raw = AUTHOR_CITATION_RE.sub(lambda match: f"{match.group(1)} <sup>{match.group(2)}</sup>", raw)
    def restore_trailing(match: re.Match) -> str:
        citation = re.sub(r"\s+", "", match.group(2))
        return f"{match.group(1)} <sup>{citation}</sup>"
    return TRAILING_CITATION_RE.sub(
        restore_trailing,
        raw,
    )


def academic_heading_translation(source_text: str, target_language: str) -> str | None:
    """Return deterministic Chinese translations for universal paper headings."""
    if "zh" not in str(target_language or "").lower() and not any(
        token in str(target_language or "") for token in ("中文", "汉语", "简体", "繁体")
    ):
        return None
    source = re.sub(r"\s+", " ", str(source_text or "").strip()).upper()
    section_labels = {
        "INTRODUCTION": "引言",
        "METHODOLOGY": "方法",
        "METHOD": "方法",
        "METHODS": "方法",
        "MATERIALS AND METHODS": "材料与方法",
        "RESULTS": "结果",
        "DISCUSSION": "讨论",
        "ANALYSIS AND DISCUSSION": "分析与讨论",
        "RESULTS AND DISCUSSION": "结果与讨论",
        "CONCLUSIONS": "结论",
        "CONCLUSION": "结论",
    }
    labels = {
        "ABSTRACT": "摘要",
        "AFFILIATIONS": "作者单位",
        "ACKNOWLEDGMENTS": "致谢",
        "ACKNOWLEDGEMENTS": "致谢",
        "REFERENCES": "参考文献",
        "CONFLICT OF INTEREST": "利益冲突声明",
        "DATA AVAILABILITY": "数据可用性",
        "AUTHOR CONTRIBUTIONS": "作者贡献",
        "FUNDING": "资金支持",
        "SUPPLEMENTARY MATERIAL": "补充材料",
        **section_labels,
    }
    if source in labels:
        return labels[source]
    match = re.fullmatch(r"((?:[IVXLCDM]+|[A-Z])\.)\s+(.+)", source)
    if not match:
        return None
    translated = section_labels.get(match.group(2))
    return f"{match.group(1)} {translated}" if translated else None


def normalize_local_academic_translation(source_text: str, translated_text: str, target_language: str) -> str:
    """Repair deterministic academic-layout errors that NMT cannot infer reliably."""
    source = str(source_text or "")
    translated = str(translated_text or "").strip()
    heading = academic_heading_translation(source, target_language)
    if heading:
        return heading
    if not translated:
        return translated
    caption = FIGURE_OR_TABLE_CAPTION_RE.match(source)
    if caption and ("zh" in str(target_language or "").lower() or "中文" in str(target_language or "")):
        label = "图" if caption.group("label").lower().startswith("fig") else "表"
        number = caption.group("number")
        # Discard just the machine-generated caption prefix.  The rest remains
        # model translated, so this does not overwrite scientific content.
        translated = re.sub(
            r"^\s*(?:资料图|图表|插图|图|表格|表|fig(?:ure)?|table)\s*[.。．、:：]*\s*"
            + re.escape(number)
            + r"\s*[.。．、:：]*\s*",
            "",
            translated,
            flags=re.I,
        )
        return f"{label} {number}" + (f". {translated}" if translated else "")
    # Do not let a bare machine translation turn an English section marker
    # into Chinese full-width punctuation (``A。`` / ``I。``).
    prefix = SECTION_PREFIX_RE.match(source)
    if prefix:
        marker = prefix.group("marker")
        # The source prefix has already been restored from a protected token.
        # Some NMT engines also emit it on their own, yielding ``A. A.`` or
        # ``1. 1。``.  Remove every leading duplicate in any common full-width
        # punctuation variant, then add the original source spelling once.
        duplicate_prefix = re.compile(
            r"^\s*(?:(?:" + re.escape(marker) + r")[.。．、\)]\s*)+",
            re.I,
        )
        translated = duplicate_prefix.sub("", translated)
        # Parenthesized list markers are parsed as marker ``(1`` + punctuation
        # ``)``.  If a model then adds another full-width stop, remove that
        # orphaned duplicate too before restoring the source prefix.
        translated = re.sub(r"^\s*[。．、]+\s*", "", translated)
        canonical_prefix = f"{marker}{prefix.group('punct')}"
        return canonical_prefix + (f" {translated.strip()}" if translated.strip() else "")
    return translated


def source_equation_reference_numbers(text: str) -> list[str]:
    """Return equation identifiers mentioned by an academic prose fragment."""
    numbers: list[str] = []
    for reference in EQUATION_REFERENCE_RE.finditer(str(text or "")):
        for match in re.finditer(r"[（(]\s*([A-Za-z]?\d+[A-Za-z]?)\s*[)）]", reference.group(0)):
            number = match.group(1)
            if number not in numbers:
                numbers.append(number)
    return numbers


def repair_local_equation_references(source_text: str, translated_text: str, target_language: str) -> str:
    """Normalize equation references that sentence-level NMT commonly damages."""
    if "zh" not in str(target_language or "").lower() and not any(
        token in str(target_language or "") for token in ("中文", "汉语", "简体", "繁体")
    ):
        return str(translated_text or "")
    result = str(translated_text or "")
    numbers = source_equation_reference_numbers(source_text)
    if not numbers:
        return result
    # Restore parenthesized equation identifiers before normalizing their label.
    result = re.sub(
        r"(?<![A-Za-z0-9])(?:[~～〜]\s*)([A-Za-z]?\d+[A-Za-z]?)\s*[!！](?![A-Za-z0-9])",
        lambda match: f"({match.group(1)})",
        result,
    )
    for number in numbers:
        escaped = re.escape(number)
        result = re.sub(
            rf"(?i)(?:Eq(?:uation)?s?|方程|公式|式)\s*[.。．、:]?\s*[（(]\s*{escaped}\s*[)）]",
            f"式 ({number})",
            result,
        )
        result = re.sub(rf"(?<![A-Za-z0-9])(?:式|方程)\s*{escaped}(?![A-Za-z0-9])", f"式 ({number})", result)
    result = re.sub(r"(?:方程|公式)\s*[。.．]\s*(式\s*\()", r"\1", result)
    result = re.sub(r"\)\s*(和|与|及)\s*式", r") \1式", result)
    return result


def normalize_local_academic_result(source_text: str, translated_text: str, target_language: str) -> str:
    """Apply the shared academic post-processing used by layout and stream modes."""
    normalized = normalize_local_academic_translation(source_text, translated_text, target_language)
    return repair_local_equation_references(source_text, normalized, target_language)


def probable_author_line(text: str) -> bool:
    """Keep author identity strings out of NMT; names must never be translated."""
    raw = str(text or "")
    chinese_name_pairs = re.findall(r"\([^)]*[\u4e00-\u9fff][^)]*\)", raw)
    latin_names = re.findall(r"\b[A-Z][A-Za-z-]+\s+[A-Z][A-Za-z-]+\b", raw)
    return len(chinese_name_pairs) >= 2 and len(latin_names) >= 2


def local_translation_quality_issues(source_text: str, translated_text: str, target_language: str) -> list[str]:
    """Cheap, deterministic guardrails for errors recurring in local academic NMT."""
    if "zh" not in str(target_language or "").lower() and "中文" not in str(target_language or ""):
        return []
    source = str(source_text or "")
    translated = str(translated_text or "")
    issues: list[str] = []
    if re.search(r"^(?:我|一|二|三|四|五|六|七|八|九|十)[。．、]", translated):
        issues.append("章节编号疑似误译")
    if re.search(r"(?:资料图|图表)\s*[。．、]*\s*\d", translated):
        issues.append("图注标签疑似误译")
    if re.search(r"[A-Za-z0-9）\)]。(?=\s*[（(\dA-Za-z])", translated):
        issues.append("非中文标点疑似全角化")
    if "<sup>" in source and "<sup>" not in translated:
        issues.append("引文上标丢失")
    return issues


def _inline_protected_ranges(text: str) -> list[tuple[int, int]]:
    """Return non-overlapping spans that must not be sent to a translator.

    The range detector is shared by web and local translation.  The local path
    replaces these ranges with short, validated markers while keeping the
    surrounding sentence intact; the web path keeps its longer sentinels for
    compatibility with its existing batch parser.
    """
    raw = str(text or "")
    if not raw:
        return []

    ranges: list[tuple[int, int]] = []

    def claim(start: int, end: int) -> None:
        if start >= end:
            return
        if any(start < existing_end and end > existing_start for existing_start, existing_end in ranges):
            return
        ranges.append((start, end))

    # Numbered academic headings are identifiers, not prose.  Keeping their
    # ASCII period prevents local NMT from converting ``I.`` into ``我。``.
    section_prefix = SECTION_PREFIX_RE.match(raw)
    if section_prefix:
        claim(section_prefix.start(), section_prefix.end())

    # Claim a broken formula and its trailing probability/value as one token.
    # Requiring a comparator or an unmatched opening parenthesis prevents a
    # normal parenthesized prose fragment from being hidden from translation.
    for match in FRAGMENTED_STATISTIC_RE.finditer(raw):
        formula = match.group("formula")
        normalized_formula = html.unescape(formula).rstrip()
        is_statistic = bool(re.search(r"(?:[<>]=?|=)\s*$", normalized_formula))
        has_unmatched_open_paren = formula.count("(") > formula.count(")")
        if is_statistic or has_unmatched_open_paren:
            claim(match.start(), match.end())

    # Only the number in an equation reference is protected.  The surrounding
    # phrase (for example, ``Eqs.``) still benefits from translation.
    for reference in EQUATION_REFERENCE_RE.finditer(raw):
        reference_text = reference.group(0)
        for number in EQUATION_NUMBER_RE.finditer(reference_text):
            claim(reference.start() + number.start(), reference.start() + number.end())

    # Superscript/subscript markup is usually a citation or a chemical index.
    # Protect the complete element, not its opening and closing tags
    # separately, so the model does not have to translate between two markers.
    for match in re.finditer(
        r"<(?P<tag>sup|sub)\b[^>\n]*>[\s\S]*?</(?P=tag)\s*>",
        raw,
        flags=re.I,
    ):
        claim(match.start(), match.end())

    patterns = (
        MARKDOWN_IMAGE_INLINE_RE,
        MARKDOWN_IMAGE_REFERENCE_RE,
        r"\$[^$\n]+\$",
        # A formula may itself contain ordinary parentheses.  Stop at the TeX
        # delimiter, rather than the first literal right parenthesis.
        r"\\\([\s\S]*?\\\)",
        r"\\\[[\s\S]*?\\\]",
        r"<[^>\n]+>",
        r"`[^`\n]+`",
        r"https?://\S+",
        r"IMAGE_\d+",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, raw):
            claim(match.start(), match.end())
    return sorted(ranges)


def split_inline_tokens(text: str) -> list[tuple[bool, str]]:
    """Split text into ``(is_protected, value)`` pieces.

    This is intentionally public within the module so the local batch path
    and the single-text fallback use exactly the same protection rules.
    """
    raw = str(text or "")
    if not raw:
        return []
    ranges = _inline_protected_ranges(raw)
    if not ranges:
        return [(False, raw)]

    pieces: list[tuple[bool, str]] = []
    cursor = 0
    for start, end in ranges:
        if start > cursor:
            pieces.append((False, raw[cursor:start]))
        pieces.append((True, raw[start:end]))
        cursor = end
    if cursor < len(raw):
        pieces.append((False, raw[cursor:]))
    return pieces


def _native_marker_fragment(value: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_")
    return fragment[:24]


def _make_native_inline_placeholder(index: int, value: str) -> str:
    """Return a compact lexical marker that the bundled NMT copies reliably."""
    raw = str(value or "")
    lowered = raw.lower()
    if lowered.startswith("<sup>") and lowered.endswith("</sup>"):
        marker_type = "SUP"
        marker_hint = _native_marker_fragment(raw[5:-6])
    elif lowered.startswith("<sub>") and lowered.endswith("</sub>"):
        marker_type = "SUB"
        marker_hint = _native_marker_fragment(raw[5:-6])
    elif EQUATION_NUMBER_RE.fullmatch(raw.strip()):
        marker_type = "EQ"
        marker_hint = _native_marker_fragment(raw)
    elif raw.startswith("$") or raw.startswith("\\"):
        marker_type = "FORM"
        marker_hint = ""
    elif re.match(r"https?://", raw, flags=re.I):
        marker_type = "URL"
        marker_hint = ""
    elif raw.startswith("`") and raw.endswith("`"):
        marker_type = "CODE"
        marker_hint = ""
    elif raw.startswith("![") and ("](" in raw or "][" in raw):
        marker_type = "IMAGE"
        marker_hint = ""
    elif raw.startswith("IMAGE_"):
        marker_type = "IMAGE"
        marker_hint = ""
    elif raw.startswith("<") and raw.endswith(">"):
        marker_type = "HTML"
        marker_hint = ""
    else:
        marker_type = "KEEP"
        marker_hint = ""
    suffix = f"_{marker_hint}" if marker_hint else ""
    return f"#{marker_type}{suffix}_{index:02d}#"


def native_inline_end_anchor_needed(text: str) -> bool:
    pieces = split_inline_tokens(text)
    return bool(pieces) and next((is_protected for is_protected, value in reversed(pieces) if value.strip()), False)


def _remove_native_end_anchor(text: str, enabled: bool = False) -> str:
    cleaned = str(text or "")
    if not enabled:
        return cleaned
    # The anchor is a disposable period, not a lexical token.  The source
    # text has a protected span at its end, so one terminal punctuation mark
    # after the marker belongs to the anchor and can be removed safely.
    cleaned = re.sub(r"[ \t]*(?:\.|。)[ \t]*$", "", cleaned, count=1)
    return cleaned.rstrip()


def protect_native_inline_tokens(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Protect inline fragments while keeping the whole sentence in context.

    The local model is much more likely to copy a short lexical marker than a
    long random ASCII sentinel.  The markers are only used with MTranServer
    and are validated before restoration; a malformed response never gets
    silently mixed back into the document.
    """
    placeholders: list[tuple[str, str]] = []
    parts: list[str] = []
    pieces = split_inline_tokens(text)
    for is_protected, value in pieces:
        if not is_protected:
            parts.append(value)
            continue
        # Start at one: the bundled model occasionally drops the opening
        # delimiter of a zero-based marker at the beginning of a long item;
        # one-based lexical markers remain stable in the same context.
        token = _make_native_inline_placeholder(len(placeholders) + 1, value)
        placeholders.append((token, value))
        parts.append(token)
    if placeholders and native_inline_end_anchor_needed(text):
        # A final protected span is otherwise prone to losing its closing
        # delimiter when the service trims the end of a translation.  This
        # disposable punctuation is removed after validation/restoration.
        parts.append(" .")
    return "".join(parts), placeholders


def native_inline_tokens_are_valid(
    text: str,
    placeholders: list[tuple[str, str]],
    end_anchor: bool = False,
) -> bool:
    """Check that every local marker survived exactly once and in order."""
    if not placeholders:
        return True
    candidate = _remove_native_end_anchor(text, end_anchor)
    expected = [token for token, _value in placeholders]
    expected_set = set(expected)
    unexpected = re.findall(r"#[A-Za-z][A-Za-z0-9_]*#", candidate)
    if any(token not in expected_set for token in unexpected):
        return False
    # Every source TeX fragment is represented by a marker at this stage.  A
    # new TeX delimiter in the model response means it has rewritten or
    # invented math outside the protected region; do not let that reach
    # MathJax, even when all marker strings happened to survive.
    masked_candidate = candidate
    for token in expected:
        masked_candidate = masked_candidate.replace(token, "")
    if UNPROTECTED_TEX_RE.search(masked_candidate):
        return False
    cursor = 0
    for token in expected:
        if candidate.count(token) != 1:
            return False
        position = candidate.find(token, cursor)
        if position < 0:
            return False
        cursor = position + len(token)
    return True


def restore_native_inline_tokens(
    text: str,
    placeholders: list[tuple[str, str]],
    end_anchor: bool = False,
) -> str:
    restored = _remove_native_end_anchor(text, end_anchor)
    for token, value in placeholders:
        restored = restored.replace(token, value)
    return restored


def _make_inline_placeholder_token(index: int, value: str) -> str:
    digest = hashlib.sha1(f"{index}:{value}".encode("utf-8")).hexdigest()[:10].upper()
    return f"ZXQH{index:02X}{digest}HQXZ"


def _tolerant_token_pattern(token: str) -> re.Pattern[str]:
    separator = r"[\s_\-]*"
    # Allow a translator to insert separators *inside* a token, but do not
    # consume whitespace after its final character.  That whitespace may be
    # the Markdown line break separating an image from its caption.
    pieces: list[str] = []
    for index, char in enumerate(token):
        pieces.append(re.escape(char))
        if index < len(token) - 1:
            pieces.append(separator)
    return re.compile("".join(pieces), flags=re.I)


def _make_batch_marker(prefix: str, token_id: str) -> str:
    digest = hashlib.sha1(f"{prefix}:{token_id}".encode("utf-8")).hexdigest()[:8].upper()
    return f"{prefix}{token_id}{digest}"


EDGE_PROTECTED_PLACEHOLDER_RE = re.compile(r"ZXQH[0-9A-F]{12}HQXZ", flags=re.I)


def shorten_edge_protected_placeholders(text: str) -> tuple[str, list[tuple[str, str]]]:
    mappings: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        alias = f"LTMKEEP{len(mappings):02X}"
        mappings.append((alias, match.group(0)))
        return alias

    return EDGE_PROTECTED_PLACEHOLDER_RE.sub(replace, str(text or "")), mappings


def restore_edge_short_placeholders(text: str, mappings: list[tuple[str, str]]) -> str:
    restored = str(text or "")
    for alias, token in mappings:
        pattern = re.compile(re.escape(alias), flags=re.I)
        if len(pattern.findall(restored)) != 1:
            return ""
        restored = pattern.sub(lambda _match, replacement=token: replacement, restored)
    return restored


def is_edge_placeholder_sensitive_error(error: Exception) -> bool:
    return bool(re.search(r"UnknownError|generic failures|Uncaught \(in promise\)", str(error), flags=re.I))


def split_edge_local_retry_text(text: str) -> tuple[str, str, str] | None:
    """Split a rejected Edge request at a nearby whitespace boundary."""
    source = str(text or "")
    if len(source) < 48:
        return None
    middle = len(source) // 2
    for distance in range(0, len(source) // 4 + 1):
        for index in (middle + distance, middle - distance):
            if index < 0 or index >= len(source) or not source[index].isspace():
                continue
            separator = re.match(r"\s+", source[index:])
            if separator is None:
                continue
            left = source[:index].rstrip()
            right = source[index + len(separator.group(0)):].lstrip()
            if len(left) >= 24 and len(right) >= 24:
                return left, separator.group(0), right
    return None


def protect_inline_tokens(text: str) -> tuple[str, list[tuple[str, str]]]:
    placeholders: list[tuple[str, str]] = []
    protected_parts: list[str] = []
    for is_protected, value in split_inline_tokens(text):
        if not is_protected:
            protected_parts.append(value)
            continue
        token = _make_inline_placeholder_token(len(placeholders), value)
        placeholders.append((token, value))
        protected_parts.append(f" {token} ")
    return "".join(protected_parts), placeholders


def restore_inline_tokens(text: str, placeholders: list[tuple[str, str]]) -> str:
    restored = str(text or "")
    for token, value in placeholders:
        restored = _tolerant_token_pattern(token).sub(lambda _match, replacement=value: replacement, restored)
    return restored


def _translate_native_single(translator, text: str, endpoint_index: int = 0) -> str:
    """Send one local request to a selected server when the translator supports it."""
    translate_on_endpoint = getattr(translator, "translate_on_endpoint", None)
    if callable(translate_on_endpoint):
        return str(translate_on_endpoint(text, endpoint_index=endpoint_index) or "")
    return str(translator.translate(text) or "")


def _translate_native_by_segments(translator, text: str, endpoint_index: int = 0) -> str:
    """Last-resort lossless fallback for a response with damaged markers."""
    translated_parts: list[str] = []
    for is_protected, part in split_inline_tokens(text):
        if is_protected or not translator_should_translate_text(translator, part):
            translated_parts.append(part)
            continue
        translated = _translate_native_single(translator, part, endpoint_index=endpoint_index)
        translated_parts.append(translated if translated.strip() else part)
    return "".join(translated_parts)


def translate_native_text_with_protection(translator, text: str, endpoint_index: int = 0) -> str:
    """Translate one local item with full context and validated restoration."""
    source_text = str(text or "")
    if not translator_should_translate_text(translator, source_text):
        return source_text

    protected, placeholders = protect_native_inline_tokens(source_text)
    end_anchor = native_inline_end_anchor_needed(source_text)
    translated = _translate_native_single(translator, protected, endpoint_index=endpoint_index)
    if not placeholders:
        return translated if translated.strip() else source_text
    if native_inline_tokens_are_valid(translated, placeholders, end_anchor=end_anchor):
        return restore_native_inline_tokens(translated, placeholders, end_anchor=end_anchor)

    # Never restore a marker that was inserted, deleted, reordered, or
    # partially rewritten by the model.  The fallback is deliberately rare;
    # its purpose is correctness and losslessness when a model response is
    # malformed, not to replace the context-preserving primary path.
    return _translate_native_by_segments(translator, source_text, endpoint_index=endpoint_index)


def translate_plain_text(translator, text: str) -> str:
    if not should_translate_text(text):
        return text
    # Keep the complete sentence/paragraph in the local model's context.  If
    # its short markers are not copied exactly, use the lossless segmented
    # fallback below rather than restoring a corrupted placeholder.
    if callable(getattr(translator, "translate_batch", None)):
        return translate_native_text_with_protection(translator, text)
    protected, placeholders = protect_inline_tokens(text)
    if not should_translate_text(protected):
        return text
    translated = translator.translate(protected)
    return restore_inline_tokens(translated, placeholders)


def translator_batch_limit(translator) -> int:
    provider = getattr(translator, "current_provider", getattr(translator, "provider_id", ""))
    if provider == EDGE_LOCAL_PROVIDER:
        return EDGE_LOCAL_MAX_CHARS
    return BING_BATCH_CHARS if provider == BING_PROVIDER else GOOGLE_BATCH_CHARS


def pack_machine_translation_items(items: list[tuple[str, str]], limit: int) -> list[list[tuple[str, str]]]:
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for item_id, text in items:
        candidate = [*current, (item_id, text)]
        candidate_text, _ = encode_translation_batch(candidate)
        if current and len(candidate_text) > limit:
            batches.append(current)
            current = [(item_id, text)]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def machine_batch_start(token_id: str) -> str:
    return f"[[[{_make_batch_marker('ZXA', token_id)}]]]"


def machine_batch_end(token_id: str) -> str:
    return f"[[[{_make_batch_marker('ZXZ', token_id)}]]]"


def encode_translation_batch(items: list[tuple[str, str]]) -> tuple[str, dict[str, str]]:
    parts: list[str] = []
    token_to_item: dict[str, str] = {}
    for index, (item_id, text) in enumerate(items, 1):
        token_id = f"{index:06d}"
        token_to_item[token_id] = item_id
        parts.append(f"{machine_batch_start(token_id)}\n{text}\n{machine_batch_end(token_id)}")
    return "\n\n".join(parts), token_to_item


def parse_translation_batch(response: str, token_to_item: dict[str, str]) -> dict[str, str] | None:
    parsed: dict[str, str] = {}
    for token_id, item_id in token_to_item.items():
        start_token = _make_batch_marker("ZXA", token_id)
        end_token = _make_batch_marker("ZXZ", token_id)
        # Accept punctuation and separator changes around the token. If the
        # translator still mutates the marker beyond recognition, parsing fails
        # and the caller automatically retries with smaller batches.
        start = rf"\[\s*\[\s*\[\s*{_tolerant_token_pattern(start_token).pattern}\s*\]\s*\]\s*\]"
        end = rf"\[\s*\[\s*\[\s*{_tolerant_token_pattern(end_token).pattern}\s*\]\s*\]\s*\]"
        pattern = rf"{start}\s*(.*?)\s*{end}"
        match = re.search(pattern, response or "", flags=re.S)
        if not match:
            return None
        parsed[item_id] = match.group(1).strip()
    return parsed


def translate_packed_items(
    translator,
    items: list[tuple[str, str]],
    limit: int,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, str]:
    if not items:
        return {}
    if should_stop and should_stop():
        raise MachineTranslationError("用户已停止翻译。")
    if len(items) == 1:
        item_id, text = items[0]
        return {item_id: translate_plain_text(translator, text)}

    packed, token_to_item = encode_translation_batch(items)
    if len(packed) <= limit:
        try:
            translated = translator.translate(packed)
            parsed = parse_translation_batch(translated, token_to_item)
            if parsed is not None:
                return parsed
        except Exception:
            pass

    if len(items) == 1:
        item_id, text = items[0]
        return {item_id: translate_plain_text(translator, text)}
    if limit <= MIN_BATCH_CHARS:
        return {item_id: translate_plain_text(translator, text) for item_id, text in items}

    midpoint = max(1, len(items) // 2)
    result: dict[str, str] = {}
    result.update(translate_packed_items(translator, items[:midpoint], max(MIN_BATCH_CHARS, limit // 2), should_stop=should_stop))
    result.update(translate_packed_items(translator, items[midpoint:], max(MIN_BATCH_CHARS, limit // 2), should_stop=should_stop))
    return result


def translator_should_translate_text(translator, text: str) -> bool:
    checker = getattr(translator, "should_translate_text", None)
    if callable(checker):
        return bool(checker(text))
    return should_translate_text(text)


def pack_native_translation_items(
    items: list[tuple[str, str]],
    max_chars: int = MTRAN_SERVER_BATCH_CHARS,
    max_items: int = MTRAN_SERVER_BATCH_ITEMS,
) -> list[list[tuple[str, str]]]:
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_chars = 0
    for item_id, text in items:
        text_len = len(str(text or ""))
        if current and (len(current) >= max_items or current_chars + text_len > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append((item_id, text))
        current_chars += text_len
    if current:
        batches.append(current)
    return batches


def split_native_batches_for_parallelism(
    batches: list[list[tuple[str, str]]],
    parallelism: int,
) -> list[list[tuple[str, str]]]:
    """Split independent local batches so available local servers can work together."""
    result = [list(batch) for batch in batches if batch]
    target_batch_count = min(normalize_parallelism(parallelism), sum(len(batch) for batch in result))
    while len(result) < target_batch_count:
        candidates = [
            (index, batch)
            for index, batch in enumerate(result)
            if len(batch) > 1
        ]
        if not candidates:
            break
        index, batch = max(candidates, key=lambda item: sum(len(str(text or "")) for _id, text in item[1]))
        total_chars = sum(len(str(text or "")) for _id, text in batch)
        midpoint = total_chars / 2
        split_at = 1
        accumulated = 0
        for item_index, (_item_id, text) in enumerate(batch[:-1], 1):
            accumulated += len(str(text or ""))
            if accumulated >= midpoint:
                split_at = item_index
                break
        result[index:index + 1] = [batch[:split_at], batch[split_at:]]
    return result


def _build_native_translation_plan(
    translator,
    items: list[tuple[str, str]],
) -> tuple[
    list[tuple[str, str]],
    dict[str, list[tuple[str, str]]],
    dict[str, str],
    dict[str, str],
]:
    """Prepare complete local items with short, validated inline markers."""
    translatable: list[tuple[str, str]] = []
    item_placeholders: dict[str, list[tuple[str, str]]] = {}
    item_sources: dict[str, str] = {}
    translations: dict[str, str] = {}

    for item_id, text in items:
        source_text = str(text or "")
        natural_text = "".join(value for is_protected, value in split_inline_tokens(source_text) if not is_protected)
        if not translator_should_translate_text(translator, natural_text):
            translations[item_id] = source_text
            continue

        protected, placeholders = protect_native_inline_tokens(source_text)
        translatable.append((item_id, protected))
        item_placeholders[item_id] = placeholders
        item_sources[item_id] = source_text

    return translatable, item_placeholders, item_sources, translations


def translate_native_batch_with_retry(
    translator,
    batch: list[tuple[str, str]],
    endpoint_index: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, str]:
    if should_stop and should_stop():
        raise MachineTranslationError("用户已停止翻译。")
    if not batch:
        return {}
    try:
        translated = translator.translate_batch([text for _item_id, text in batch], endpoint_index=endpoint_index)
        if not isinstance(translated, (list, tuple)) or len(translated) != len(batch):
            raise MachineTranslationError("本地免费机翻 batch 返回数量与请求条目数量不一致。")
        return {item_id: str(text or "") for (item_id, _source), text in zip(batch, translated)}
    except Exception:
        if len(batch) <= 1:
            item_id, text = batch[0]
            return {item_id: str(translator.translate(text) or "")}
        midpoint = max(1, len(batch) // 2)
        result: dict[str, str] = {}
        result.update(translate_native_batch_with_retry(translator, batch[:midpoint], endpoint_index, should_stop=should_stop))
        result.update(translate_native_batch_with_retry(translator, batch[midpoint:], endpoint_index, should_stop=should_stop))
        return result


def translate_text_items_native_batched(
    translator,
    items: list[tuple[str, str]],
    log: Callable[[str], None] | None = None,
    live_update: Callable[[dict[str, str]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, str]:
    translatable, item_placeholders, item_sources, translations = _build_native_translation_plan(translator, items)

    configured_workers = normalize_parallelism(getattr(translator, "parallelism", MTRAN_SERVER_DEFAULT_PARALLELISM))
    batches = split_native_batches_for_parallelism(
        pack_native_translation_items(translatable),
        configured_workers,
    )
    completed = 0
    total = len(translatable)
    active_workers = min(configured_workers, len(batches))
    if log:
        log(
            f"本地机翻准备就绪：共 {len(batches)} 个批次（{total} 个文本块，并发数 {active_workers}）。"
        )

    retry_items: list[tuple[str, str, int]] = []

    def finish_batch(translated_batch: dict[str, str]) -> None:
        nonlocal completed
        for item_id, translated_text in translated_batch.items():
            source_text = item_sources.get(item_id, "")
            translated = str(translated_text or "")
            placeholders = item_placeholders.get(item_id, [])
            end_anchor = native_inline_end_anchor_needed(source_text)
            if not translated.strip():
                translations[item_id] = source_text
            elif not placeholders:
                translations[item_id] = translated
            elif native_inline_tokens_are_valid(
                translated,
                placeholders,
                end_anchor=end_anchor,
            ):
                translations[item_id] = restore_native_inline_tokens(
                    translated,
                    placeholders,
                    end_anchor=end_anchor,
                )
            else:
                # A damaged marker must never be restored heuristically.  Try
                # the same complete item once more (batch and single requests
                # can tokenize differently), then use the lossless segment
                # fallback only if the second context-preserving attempt also
                # fails validation.
                if log:
                    log(f"检测到个别标记需校正，正在自动微调重试…")
                # Do not make one damaged batch hold up reporting or handling
                # of the other completed batches.  Spread complete-item
                # retries across the same local service pool as the primary
                # batch requests.
                retry_items.append((item_id, source_text, len(retry_items) % max(1, active_workers)))
                continue
            completed += 1

    def finish_retries(executor: ThreadPoolExecutor | None = None) -> None:
        nonlocal completed
        if not retry_items:
            return

        def retry_item(item_id: str, source_text: str, endpoint_index: int) -> tuple[str, str]:
            if should_stop and should_stop():
                raise MachineTranslationError("用户已停止翻译。")
            try:
                translated = translate_native_text_with_protection(
                    translator,
                    source_text,
                    endpoint_index=endpoint_index,
                )
            except Exception:
                translated = source_text
            return item_id, translated

        retry_workers = min(max(1, active_workers), len(retry_items))
        if log:
            reuse_note = "复用首轮工作线程" if executor is not None else "当前线程执行"
            log(
                f"正在对 {len(retry_items)} 个待微调条目进行自动校对重试（并发数 {retry_workers}）…"
            )

        def record_retry(item_id: str, translated: str) -> None:
            nonlocal completed
            translations[item_id] = translated
            completed += 1
            if live_update:
                live_update(dict(translations))
            if log and total:
                log(f"本地机翻进度：已完成 {completed}/{total} 个文本块。")

        if executor is None:
            for item in retry_items:
                if should_stop and should_stop():
                    raise MachineTranslationError("用户已停止翻译。")
                record_retry(*retry_item(*item))
        else:
            # Keep the primary executor alive until retry work is complete.
            # This avoids a second set of Python threads and reuses the
            # already-warm workers while retaining the configured limit.
            futures = [executor.submit(retry_item, *item) for item in retry_items]
            for future in as_completed(futures):
                if should_stop and should_stop():
                    raise MachineTranslationError("用户已停止翻译。")
                record_retry(*future.result())

    max_workers = active_workers
    if max_workers <= 1 or len(batches) <= 1:
        for batch_index, batch in enumerate(batches, 1):
            if should_stop and should_stop():
                raise MachineTranslationError("用户已停止翻译。")
            if log:
                log(f"正在翻译第 {batch_index}/{len(batches)} 批（共 {len(batch)} 项，约 {sum(len(text) for _id, text in batch)} 字符）…")
            finish_batch(translate_native_batch_with_retry(translator, batch, batch_index - 1, should_stop=should_stop))
            if live_update:
                live_update(dict(translations))
            if log and total:
                log(f"本地机翻进度：已完成 {completed}/{total} 个文本块。")
    else:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mtranserver") as executor:
            future_to_index = {
                executor.submit(translate_native_batch_with_retry, translator, batch, index, should_stop): (index, batch)
                for index, batch in enumerate(batches)
            }
            for future in as_completed(future_to_index):
                if should_stop and should_stop():
                    raise MachineTranslationError("用户已停止翻译。")
                index, batch = future_to_index[future]
                if log:
                    log(f"第 {index + 1}/{len(batches)} 批翻译完成（已处理 {len(batch)} 项）。")
                finish_batch(future.result())
                if live_update:
                    live_update(dict(translations))
                if log and total:
                    log(f"本地机翻进度：已完成 {completed}/{total} 个文本块。")
            # Reuse the same worker threads instead of creating a second
            # retry pool.  Leaving this context joins those threads before
            # the local MTranServer processes are released by end_job().
            finish_retries(executor)
    if max_workers <= 1 or len(batches) <= 1:
        finish_retries()
    return {item_id: translations.get(item_id, text) for item_id, text in items}


def translate_text_items_batched(
    translator,
    items: list[tuple[str, str]],
    log: Callable[[str], None] | None = None,
    live_update: Callable[[dict[str, str]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, str]:
    begin_job = getattr(translator, "begin_job", None)
    end_job = getattr(translator, "end_job", None)
    if callable(begin_job):
        begin_job()
    try:
        if callable(getattr(translator, "translate_batch", None)):
            return translate_text_items_native_batched(
                translator,
                items,
                log=log,
                live_update=live_update,
                should_stop=should_stop,
            )

        translatable: list[tuple[str, str]] = []
        translations: dict[str, str] = {}
        for item_id, text in items:
            if translator_should_translate_text(translator, text):
                protected, placeholders = protect_inline_tokens(text)
                translatable.append((item_id, protected))
                translations[f"__placeholders__{item_id}"] = json.dumps(placeholders, ensure_ascii=False)
            else:
                translations[item_id] = text

        batches = pack_machine_translation_items(translatable, translator_batch_limit(translator))
        completed = 0
        total = len(translatable)
        translated_batches: dict[int, dict[str, str]] = {}
        provider = str(getattr(translator, "current_provider", getattr(translator, "provider_id", "")) or "").lower()
        translation_log_label = provider_label(provider)

        # Edge's Translator API has no multi-string batch operation.  It also
        # rewrites the sentinel markers used by ``translate_packed_items``;
        # attempting a packed request therefore succeeds only superficially,
        # then recursively retries the same text in smaller groups.  Translate
        # each protected block exactly once, while retaining outer batches for
        # live-preview cadence and concise progress reporting.
        if provider == EDGE_LOCAL_PROVIDER:
            for batch_index, batch in enumerate(batches, 1):
                if should_stop and should_stop():
                    raise MachineTranslationError("用户已停止翻译。")
                if log:
                    batch_text, _ = encode_translation_batch(batch)
                    log(
                        f"{translation_log_label}正在处理第 {batch_index}/{len(batches)} 批（共 {len(batch)} 块）…"
                    )
                for item_id, protected_text in batch:
                    if should_stop and should_stop():
                        raise MachineTranslationError("用户已停止翻译。")
                    translated_text = translator.translate(protected_text)
                    placeholders = json.loads(translations.pop(f"__placeholders__{item_id}", "[]"))
                    translations[item_id] = restore_inline_tokens(translated_text, placeholders)
                    completed += 1
                if live_update:
                    live_update(dict(translations))
                if log and total:
                    log(f"{translation_log_label}进度：已完成 {completed}/{total} 个文本块。")
            return {item_id: translations.get(item_id, text) for item_id, text in items}

        clone_for_worker = getattr(translator, "clone_for_worker", None)
        if provider == BING_PROVIDER and len(batches) > 1 and callable(clone_for_worker):
            worker_count = min(BING_REQUEST_PARALLELISM, len(batches))
            if log:
                log(f"Bing 翻译已启用（并发数: {worker_count}）。")

            def translate_bing_batch_group(indexes: list[int]) -> list[tuple[int, dict[str, str]]]:
                worker_translator = clone_for_worker()
                return [
                    (
                        index,
                        translate_packed_items(
                            worker_translator,
                            batches[index],
                            translator_batch_limit(worker_translator),
                            should_stop=should_stop,
                        ),
                    )
                    for index in indexes
                ]

            assignments = [list(range(worker_index, len(batches), worker_count)) for worker_index in range(worker_count)]
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="bing-web") as executor:
                futures = [executor.submit(translate_bing_batch_group, indexes) for indexes in assignments if indexes]
                for future in as_completed(futures):
                    for batch_index, translated_batch in future.result():
                        translated_batches[batch_index] = translated_batch

        for batch_index, batch in enumerate(batches, 1):
            if should_stop and should_stop():
                raise MachineTranslationError("用户已停止翻译。")
            if log:
                batch_text, _ = encode_translation_batch(batch)
                log(f"{translation_log_label}正在翻译第 {batch_index}/{len(batches)} 批（约 {len(batch_text)} 字符）…")
            translated_batch = translated_batches.get(batch_index - 1)
            if translated_batch is None:
                translated_batch = translate_packed_items(
                    translator,
                    batch,
                    translator_batch_limit(translator),
                    should_stop=should_stop,
                )
            for item_id, translated_text in translated_batch.items():
                placeholders = json.loads(translations.pop(f"__placeholders__{item_id}", "[]"))
                translations[item_id] = restore_inline_tokens(translated_text, placeholders)
                completed += 1
            if live_update:
                live_update(dict(translations))
            if log and total:
                log(f"{translation_log_label}进度：已完成 {completed}/{total} 个文本块。")
        return {item_id: translations.get(item_id, text) for item_id, text in items}
    finally:
        if callable(end_job):
            end_job()


def markdown_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    in_math = False

    def flush() -> None:
        nonlocal current
        if current:
            blocks.append("".join(current))
            current = []

    for line in str(text or "").splitlines(keepends=True):
        stripped = line.strip()
        starts_fence = stripped.startswith("```") or stripped.startswith("~~~")
        starts_math = stripped in {"$$", "\\[", "\\]"} or stripped.startswith("$$")
        is_table_line = stripped.startswith("|") and stripped.endswith("|")

        if starts_fence:
            if not in_fence and current:
                flush()
            current.append(line)
            in_fence = not in_fence
            if not in_fence:
                flush()
            continue
        if in_fence:
            current.append(line)
            continue
        if starts_math and stripped.startswith("$$"):
            if not in_math and current:
                flush()
            current.append(line)
            in_math = not in_math if stripped == "$$" else in_math
            if not in_math and stripped == "$$":
                flush()
            continue
        if in_math:
            current.append(line)
            continue
        if is_table_line:
            if current and not current[-1].strip().startswith("|"):
                flush()
            current.append(line)
            continue
        if stripped == "":
            current.append(line)
            flush()
            continue
        if stripped.startswith("#") and current:
            flush()
        current.append(line)
    flush()
    return blocks


def translate_markdown_table(block: str, translator) -> str:
    return block


def translate_markdown_block(block: str, translator) -> str:
    stripped = block.strip()
    if not stripped:
        return block
    if stripped.startswith(("```", "~~~", "$$", "\\[")) or stripped.endswith("\\]"):
        return block
    if re.fullmatch(r"!\[[^\]]*\]\([^)]+\)\s*", stripped):
        return block
    if all(line.strip().startswith("|") and line.strip().endswith("|") for line in block.splitlines() if line.strip()):
        return translate_markdown_table(block, translator)
    heading_match = re.match(r"^(\s{0,3}#{1,6}\s+)(.*?)(\s*#*\s*)(\n?)$", block, flags=re.S)
    if heading_match:
        return (
            heading_match.group(1)
            + translate_plain_text(translator, heading_match.group(2))
            + heading_match.group(3)
            + heading_match.group(4)
        )
    return translate_plain_text(translator, block)


def markdown_block_translation_text(block: str) -> tuple[str, str, str] | None:
    stripped = block.strip()
    if not stripped:
        return None
    if stripped.startswith(("```", "~~~", "$$", "\\[")) or stripped.endswith("\\]"):
        return None
    if re.fullmatch(r"!\[[^\]]*\]\([^)]+\)\s*", stripped):
        return None
    if all(line.strip().startswith("|") and line.strip().endswith("|") for line in block.splitlines() if line.strip()):
        return None
    heading_match = re.match(r"^(\s{0,3}#{1,6}\s+)(.*?)(\s*#*\s*)(\n?)$", block, flags=re.S)
    if heading_match:
        return heading_match.group(1), heading_match.group(2), heading_match.group(3) + heading_match.group(4)
    outer_match = re.match(r"^(\s*)(.*?)(\s*)$", block, flags=re.S)
    if outer_match:
        return outer_match.group(1), outer_match.group(2), outer_match.group(3)
    return "", block, ""


def translate_markdown_document(
    markdown: str,
    target_language: str,
    provider_id: str = MACHINE_TRANSLATION_PROVIDER,
    source_language: str = "auto",
    base_url: str = "",
    api_key: str = "",
    parallelism: int = MTRAN_SERVER_DEFAULT_PARALLELISM,
    log: Callable[[str], None] | None = None,
    live_update: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    translator=None,
    edge_download_consent=None,
) -> str:
    translator = translator or (
        create_fallback_translator(target_language, source_language=source_language, log=log, edge_download_consent=edge_download_consent)
        if (provider_id or "").strip().lower() == MACHINE_TRANSLATION_PROVIDER
        else create_translator(
            provider_id,
            target_language,
            source_language=source_language,
            log=log,
            base_url=base_url,
            api_key=api_key,
            parallelism=parallelism,
            edge_download_consent=edge_download_consent,
        )
    )
    blocks = markdown_blocks(markdown)
    translated_blocks: list[str] = list(blocks)
    items: list[tuple[str, str]] = []
    wrappers: dict[str, tuple[int, str, str]] = {}
    source_by_id: dict[str, str] = {}
    direct_count = 0
    for index, block in enumerate(blocks, 1):
        if should_stop and should_stop():
            raise MachineTranslationError("用户已停止翻译。")
        item = markdown_block_translation_text(block)
        if item is None:
            continue
        prefix, text, suffix = item
        item_id = f"md{index:04d}"
        wrappers[item_id] = (index - 1, prefix, suffix)
        source_by_id[item_id] = text
        deterministic = academic_heading_translation(text, target_language)
        if deterministic:
            translated_blocks[index - 1] = prefix + deterministic + suffix
            direct_count += 1
            continue
        if probable_author_line(text):
            translated_blocks[index - 1] = prefix + text + suffix
            direct_count += 1
            continue
        items.append((item_id, restore_plain_text_citation_markup(text)))

    def normalized_item(item_id: str, translated_text: str) -> str:
        source_text = source_by_id.get(item_id, translated_text)
        return normalize_local_academic_result(source_text, translated_text, target_language)

    def update_live(partial: dict[str, str]) -> None:
        if should_stop and should_stop():
            raise MachineTranslationError("用户已停止翻译。")
        if not live_update:
            return
        preview_blocks = list(translated_blocks)
        for item_id, translated_text in partial.items():
            wrapper = wrappers.get(item_id)
            if not wrapper:
                continue
            block_index, prefix, suffix = wrapper
            preview_blocks[block_index] = prefix + normalized_item(item_id, translated_text) + suffix
        live_update("".join(preview_blocks))

    translated_items = translate_text_items_batched(
        translator,
        items,
        log=log,
        live_update=update_live,
        should_stop=should_stop,
    )
    for item_id, translated_text in translated_items.items():
        block_index, prefix, suffix = wrappers[item_id]
        translated_blocks[block_index] = prefix + normalized_item(item_id, translated_text) + suffix
    if log:
        quality_hits = 0
        for item_id in wrappers:
            block_index, prefix, suffix = wrappers[item_id]
            translated_text = translated_blocks[block_index]
            if prefix and translated_text.startswith(prefix):
                translated_text = translated_text[len(prefix):]
            if suffix and translated_text.endswith(suffix):
                translated_text = translated_text[:-len(suffix)]
            issues = local_translation_quality_issues(source_by_id.get(item_id, ""), translated_text, target_language)
            if issues:
                quality_hits += 1
                log(f"提示：个别段落格式已自动校对（{'、'.join(issues)}）")
        log(
            f"本地学术翻译完成：已自动规范化引文、公式与图表标签（微调 {quality_hits} 处）。"
        )
    return "".join(translated_blocks)


def translate_record_texts(
    records,
    target_language: str,
    provider_id: str = MACHINE_TRANSLATION_PROVIDER,
    source_language: str = "auto",
    base_url: str = "",
    api_key: str = "",
    parallelism: int = MTRAN_SERVER_DEFAULT_PARALLELISM,
    log: Callable[[str], None] | None = None,
    live_update: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    translator=None,
    edge_download_consent=None,
) -> dict[str, str]:
    translator = translator or (
        create_fallback_translator(target_language, source_language=source_language, log=log, edge_download_consent=edge_download_consent)
        if (provider_id or "").strip().lower() == MACHINE_TRANSLATION_PROVIDER
        else create_translator(
            provider_id,
            target_language,
            source_language=source_language,
            log=log,
            base_url=base_url,
            api_key=api_key,
            parallelism=parallelism,
            edge_download_consent=edge_download_consent,
        )
    )
    translations: dict[str, str] = {}
    records = list(records or [])
    items: list[tuple[str, str]] = []
    source_by_id: dict[str, str] = {}
    for record in records:
        block_id = str(getattr(record, "block_id", ""))
        text = str(getattr(record, "text", "") or "")
        source_by_id[block_id] = text
        # Headings and author lines are structured document metadata.  A
        # sentence-level NMT model has no reliable way to infer their role.
        deterministic = academic_heading_translation(text, target_language)
        if deterministic:
            translations[block_id] = deterministic
            continue
        if probable_author_line(text):
            translations[block_id] = text
            continue
        items.append((block_id, restore_plain_text_citation_markup(text)))

    def update_live(partial: dict[str, str]) -> None:
        if should_stop and should_stop():
            raise MachineTranslationError("用户已停止翻译。")
        if live_update:
            done_count = sum(1 for item_id, _ in items if item_id in partial)
            live_label = provider_label((provider_id or "").strip().lower())
            live_update(
                f"正在{live_label}排版块...\n\n"
                f"- 已完成: {done_count}/{len(records)}\n"
                f"- 当前服务: {provider_label(getattr(translator, 'current_provider', provider_id))}"
            )

    translations.update(
        translate_text_items_batched(
            translator,
            items,
            log=log,
            live_update=update_live,
            should_stop=should_stop,
        )
    )
    quality_hits = 0
    for block_id, source_text in source_by_id.items():
        normalized = normalize_local_academic_result(
            source_text,
            translations.get(block_id, source_text),
            target_language,
        )
        translations[block_id] = normalized
        issues = local_translation_quality_issues(source_text, normalized, target_language)
        if issues:
            quality_hits += 1
            if log:
                log(f"提示：个别文本块格式已自动校对（{'、'.join(issues)}）")
    if log:
        bypassed = len(records) - len(items)
        log(
            f"排版机翻完成：已自动规范化引文、公式与图表标签（微调 {quality_hits} 处）。"
        )
    return translations
