"""LitMTrans widgets for document chat, images, model output, and providers."""

from __future__ import annotations

import hashlib
import html
import json
import base64
from datetime import datetime

from AI_services import *
from PySide6.QtGui import QTextDocument
from PySide6.QtCore import Signal, Slot
from PySide6.QtWebChannel import QWebChannel

CHAT_BOLD_ACCENT = "#4f4032"


def pasted_image_name(captured_at: datetime | None = None, sequence: int = 1) -> str:
    """生成可在连续对话中稳定区分的剪贴板图片名称。"""
    timestamp = captured_at if isinstance(captured_at, datetime) else datetime.now()
    ordinal = max(1, int(sequence or 1))
    millisecond = timestamp.microsecond // 1000
    return (
        f"粘贴图片-{timestamp:%Y%m%d-%H%M%S}-{millisecond:03d}-"
        f"{ordinal:02d}.png"
    )

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    CHAT_WEBENGINE_AVAILABLE = True
except Exception:
    QWebEngineSettings = None
    QWebEngineView = None
    CHAT_WEBENGINE_AVAILABLE = False

if CHAT_WEBENGINE_AVAILABLE:
    class _ChatWebView(QWebEngineView):
        """Forward bubble gestures before Chromium consumes its child events."""

        def __init__(self, bubble, parent=None):
            super().__init__(parent)
            self.bubble = bubble

        def mouseReleaseEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton and self.bubble.expand_from_web_click():
                event.accept()
                return
            super().mouseReleaseEvent(event)

        def mouseDoubleClickEvent(self, event):
            self.bubble.mouseDoubleClickEvent(event)

        def wheelEvent(self, event):
            event.ignore()
else:
    _ChatWebView = None


class _ChatWebBridge(QObject):
    """Receive DOM gestures from the Chromium renderer through WebChannel."""

    def __init__(self, bubble, parent=None):
        super().__init__(parent)
        self.bubble = bubble

    @Slot(str)
    def gesture(self, kind: str):
        if kind == "expand":
            self.bubble.expand_from_web_click()
        elif kind == "copy":
            self.bubble.copy_raw_text()
        elif kind == "diagram":
            self.bubble.open_diagram_viewer()


class _DiagramViewerBridge(QObject):
    """Connect local SVG node actions to the owning document chat."""

    def __init__(
        self,
        locate_callback=None,
        ask_callback=None,
        image_export_callback=None,
        export_request_callback=None,
        document_path: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.locate_callback = locate_callback
        self.ask_callback = ask_callback
        self.image_export_callback = image_export_callback
        self.export_request_callback = export_request_callback
        self.document_path = str(document_path or "")

    @Slot(str)
    def locateEvidence(self, payload: str):
        try:
            evidence = json.loads(payload) if payload else {}
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        text = str(evidence.get("quote") or evidence.get("text") or "").strip()
        if not text or not callable(self.locate_callback):
            return
        quote = {
            "type": "quote", "text": text, "pane": "source",
            "document_path": self.document_path,
            "source_markdown_path": self.document_path,
            "diagram_evidence": True,
        }
        try:
            self.locate_callback(quote)
        except Exception:
            pass

    @Slot(str)
    def askNode(self, payload: str):
        if not callable(self.ask_callback):
            return
        try:
            node = json.loads(payload) if payload else {}
            self.ask_callback(node if isinstance(node, dict) else {})
        except Exception:
            pass

    @Slot(str)
    def exportImage(self, data_url: str):
        if callable(self.image_export_callback):
            self.image_export_callback(data_url)

    @Slot()
    def requestImageExport(self):
        if callable(self.export_request_callback):
            self.export_request_callback()


class DiagramViewerDialog(QDialog):
    """Large, local-only canvas for generated mind maps and flowcharts."""

    def __init__(self, raw_text: str, parent=None, locate_callback=None, ask_callback=None, document_path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("图形阅读")
        self.resize(1120, 760)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.view.page().setBackgroundColor(QColor("#F5F7F8"))
        self.channel = QWebChannel(self.view.page())
        self.bridge = _DiagramViewerBridge(
            locate_callback=locate_callback,
            ask_callback=ask_callback,
            image_export_callback=self._save_exported_image,
            export_request_callback=self.export_image,
            document_path=document_path,
            parent=self,
        )
        self.channel.registerObject("diagramBridge", self.bridge)
        self.view.page().setWebChannel(self.channel)
        layout.addWidget(self.view)
        self.view.setHtml(
            self.viewer_html(raw_text),
            QUrl.fromLocalFile(str(Path(__file__).resolve().parent)),
        )

    @staticmethod
    def image_export_script() -> str:
        """Rasterize the complete SVG, independently of the current canvas zoom."""
        return """(() => {
          const bridge = window.__litmtransDiagramBridge;
          const svg = document.querySelector('#world svg.litmtrans-diagram');
          if (!bridge || !svg) return false;
          const width = Math.ceil(Number(svg.getAttribute('width')) || svg.viewBox.baseVal.width || 800);
          const height = Math.ceil(Number(svg.getAttribute('height')) || svg.viewBox.baseVal.height || 600);
          const clone = svg.cloneNode(true);
          clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
          clone.setAttribute('width', width);
          clone.setAttribute('height', height);
          const markup = new XMLSerializer().serializeToString(clone);
          const blob = new Blob([markup], {type: 'image/svg+xml;charset=utf-8'});
          const url = URL.createObjectURL(blob);
          const image = new Image();
          image.onload = () => {
            const canvas = document.createElement('canvas');
            canvas.width = width * 2; canvas.height = height * 2;
            const context = canvas.getContext('2d');
            context.fillStyle = '#ffffff'; context.fillRect(0, 0, canvas.width, canvas.height);
            context.scale(2, 2); context.drawImage(image, 0, 0, width, height);
            URL.revokeObjectURL(url); bridge.exportImage(canvas.toDataURL('image/png'));
          };
          image.onerror = () => { URL.revokeObjectURL(url); bridge.exportImage(''); };
          image.src = url;
          return true;
        })()"""

    def export_image(self):
        """Offer a lossless PNG of the full generated diagram."""
        self.view.page().runJavaScript(self.image_export_script(), self._image_export_started)

    def _set_export_button_enabled(self, enabled: bool):
        """Keep the in-page export control from starting duplicate renders."""
        state = "false" if enabled else "true"
        self.view.page().runJavaScript(
            f"document.getElementById('export-image').disabled = {state};"
        )

    def _image_export_started(self, started):
        if not started:
            self._set_export_button_enabled(True)
            QMessageBox.warning(self, "导出图片", "图形尚未准备好，请稍后重试。")

    def _save_exported_image(self, data_url: str):
        self._set_export_button_enabled(True)
        prefix = "data:image/png;base64,"
        if not isinstance(data_url, str) or not data_url.startswith(prefix):
            QMessageBox.warning(self, "导出图片", "无法生成图形图片，请稍后重试。")
            return
        try:
            image = QImage.fromData(base64.b64decode(data_url[len(prefix):]))
        except Exception:
            image = QImage()
        if image.isNull():
            QMessageBox.warning(self, "导出图片", "生成的图形图片无效，请稍后重试。")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出图形图片", "导图.png", "PNG 图片 (*.png)"
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".png"):
            file_path += ".png"
        if not image.save(file_path, "PNG"):
            QMessageBox.critical(self, "导出图片", "图片保存失败，请检查目标位置是否可写。")

    @staticmethod
    def viewer_html(raw_text: str) -> str:
        from AI_diagrams import render_diagram_html

        diagram = render_diagram_html(raw_text)
        if not diagram:
            diagram = '<p class="viewer-error">无法绘制该图，请检查模型返回的图形数据。</p>'
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; background: #f5f7f8;
    color: #17212b; font: 14px/1.5 "Microsoft YaHei UI", "Segoe UI", sans-serif; }}
  body {{ display: grid; grid-template-rows: 54px minmax(0, 1fr) 40px; }}
  .viewer-toolbar {{ display: flex; align-items: center; gap: 7px; padding: 8px 14px;
    border-bottom: 1px solid #d9e0e5; background: rgba(255,255,255,.96); }}
  .viewer-toolbar strong {{ margin-right: auto; font-size: 15px; }}
  .viewer-toolbar button {{ min-width: 36px; height: 34px; padding: 0 11px; border: 1px solid #b7c5ce;
    border-radius: 7px; background: #fff; color: #21313d; cursor: pointer; }}
  .viewer-toolbar button:hover {{ border-color: #557e95; background: #edf4f7; }}
  #scale {{ min-width: 52px; text-align: center; color: #51636f; font-variant-numeric: tabular-nums; }}
  #viewport {{ min-width: 0; min-height: 0; overflow: auto; padding: 28px; cursor: grab; }}
  #viewport.dragging {{ cursor: grabbing; user-select: none; }}
  #world {{ position: relative; transform-origin: 0 0; margin: 0 auto; width: max-content; }}
  .litmtrans-diagram-wrap {{ padding: 0; }}
  .litmtrans-diagram-wrap > h3 {{ display: none; }}
  .litmtrans-diagram-preview {{ width: max-content; padding: 22px; border: 1px solid #d8e0e5;
    border-radius: 12px; background: #fff; box-shadow: 0 14px 38px rgba(31,47,58,.10); }}
  .litmtrans-diagram {{ display: block; max-width: none; }}
  .litmtrans-diagram .edges path {{ fill: none; stroke: #738795; stroke-width: 1.55; }}
  .litmtrans-diagram text {{ font-family: "Microsoft YaHei UI", sans-serif; dominant-baseline: middle; }}
  .litmtrans-diagram .node-title {{ fill: #102a3a; font-weight: 700; }}
  .litmtrans-diagram .node-detail {{ fill: #304b5b; font-weight: 500; }}
  .litmtrans-diagram .edge-label {{ fill: #52616a; font-size: 10.5px; font-weight: 500; dominant-baseline: auto;
    paint-order: stroke; stroke: #f7f9fa; stroke-width: 4px; stroke-linejoin: round; }}
  .mindmap-node rect, .flow-node > rect, .flow-node > path:not(.subprocess-lines) {{ fill: #fff; stroke: #7890a2; stroke-width: 1.3; }}
  .mindmap-node.root rect, .flow-node.type-terminator > rect {{ fill: #1e3347; stroke: #1e3347; }}
  .mindmap-node.root text, .flow-node.type-terminator text {{ fill: #fff; }}
  .mindmap-node.root .node-title, .mindmap-node.root .node-detail,
  .flow-node.type-terminator .node-title, .flow-node.type-terminator .node-detail {{ fill: #fff; }}
  .mindmap-node.branch rect {{ fill: #e9f1f5; stroke: #5c8399; }}
  .mindmap-node[data-kind="result"] rect, .mindmap-node[data-kind="validation"] rect {{ fill: #eaf4ef; stroke: #659078; }}
  .mindmap-node[data-kind="gap"] rect, .mindmap-node[data-kind="limitation"] rect {{ fill: #fff5e6; stroke: #ad8247; }}
  .flow-node.type-decision > path {{ fill: #fff6e4; stroke: #a77d42; }}
  .flow-node.type-database > path {{ fill: #edf5ef; stroke: #668873; }}
  .flow-node.type-io > path, .flow-node.type-document > path {{ fill: #edf4f7; stroke: #66869a; }}
  .flow-node.role-conclusion > rect, .flow-node.role-conclusion > path:not(.subprocess-lines) {{ fill: #1e3347; stroke: #1e3347; stroke-width: 1.45; }}
  .flow-node.role-conclusion .node-title {{ fill: #fff; }}
  .flow-node.role-conclusion .node-detail {{ fill: #e8f0f3; }}
  .subprocess-lines {{ fill: none; stroke: #7890a2; stroke-width: 1; }}
  .diagram-node-popover {{ position:absolute; z-index:20; width:280px; padding:11px 12px; border:1px solid #879aa5;
    border-radius:8px; background:rgba(255,255,255,.98); box-shadow:0 14px 32px rgba(27,45,56,.18); color:#1b2931; }}
  .diagram-node-popover strong {{ display:block; margin-bottom:5px; font-size:12px; }}
  .diagram-node-popover small {{ display:block; color:#65747d; font:10px/1.5 Consolas,monospace; overflow-wrap:anywhere; }}
  .diagram-node-actions {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }}
  .diagram-node-actions button {{ min-height:26px; padding:3px 7px; border:1px solid #869aa6; border-radius:5px; background:#fff; color:#315a70; cursor:pointer; }}
  .viewer-hint {{ display:flex; align-items:center; padding:0 14px; border-top:1px solid #d9e0e5; background:rgba(255,255,255,.96); color:#687780; font-size:11px; }}
  .mindmap-node.selected > rect, .flow-node.selected > rect, .flow-node.selected > path:not(.subprocess-lines) {{ stroke-width:2px!important; filter:drop-shadow(0 6px 9px rgba(35,91,124,.22)); }}
  .viewer-error {{ padding: 24px; color: #8a3c32; }}
</style></head><body>
  <nav class="viewer-toolbar"><strong>图形阅读</strong><button id="minus" title="缩小">−</button><span id="scale">100%</span><button id="plus" title="放大">＋</button><button id="fit">适合窗口</button><button id="actual">实际大小</button><button id="export-image" title="将完整导图或流程图导出为 PNG 图片">导出图片</button></nav>
  <main id="viewport"><div id="world">{diagram}</div></main>
  <footer class="viewer-hint">Ctrl + 鼠标滚轮缩放 · 拖动画布平移</footer>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<script>
(() => {{
  const viewport = document.getElementById('viewport');
  const world = document.getElementById('world');
  const output = document.getElementById('scale');
  const svg = world.querySelector('svg');
  let zoom = 1, drag = null, bridge = null;
  if (window.qt && window.QWebChannel) new QWebChannel(qt.webChannelTransport, channel => {{
    bridge = channel.objects.diagramBridge; window.__litmtransDiagramBridge = bridge;
  }});
  const naturalWidth = Number(svg?.getAttribute('width')) || 800;
  const naturalHeight = Number(svg?.getAttribute('height')) || 600;
  function apply(value) {{
    if (world.querySelector('.diagram-node-popover')) dismissPopover();
    zoom = Math.max(.2, Math.min(3, value));
    world.style.width = `${{naturalWidth * zoom + 44}}px`;
    world.style.height = `${{naturalHeight * zoom + 44}}px`;
    const preview = world.querySelector('.litmtrans-diagram-preview');
    if (preview) {{ preview.style.transformOrigin = '0 0'; preview.style.transform = `scale(${{zoom}})`; }}
    output.textContent = `${{Math.round(zoom * 100)}}%`;
  }}
  function fit() {{ apply(Math.min(1, (viewport.clientWidth - 72) / (naturalWidth + 44), (viewport.clientHeight - 72) / (naturalHeight + 44))); viewport.scrollTo(0, 0); }}
  document.getElementById('minus').onclick = () => apply(zoom / 1.2);
  document.getElementById('plus').onclick = () => apply(zoom * 1.2);
  document.getElementById('fit').onclick = fit;
  document.getElementById('actual').onclick = () => apply(1);
  document.getElementById('export-image').onclick = event => {{
    const button = event.currentTarget;
    if (!bridge || button.disabled) return;
    button.disabled = true; bridge.requestImageExport();
  }};
  viewport.addEventListener('wheel', event => {{ if (!event.ctrlKey) return; event.preventDefault(); apply(zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12)); }}, {{passive:false}});
  viewport.addEventListener('pointerdown', event => {{ if (event.button !== 0) return; drag = {{x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop,id:event.pointerId}}; viewport.setPointerCapture(event.pointerId); viewport.classList.add('dragging'); }});
  viewport.addEventListener('pointermove', event => {{ if (!drag || drag.id !== event.pointerId) return; viewport.scrollLeft = drag.left - event.clientX + drag.x; viewport.scrollTop = drag.top - event.clientY + drag.y; }});
  const stop = event => {{ if (!drag || drag.id !== event.pointerId) return; drag = null; viewport.classList.remove('dragging'); }};
  viewport.addEventListener('pointerup', stop); viewport.addEventListener('pointercancel', stop);
  function dismissPopover() {{
    world.querySelector('.diagram-node-popover')?.remove();
    world.querySelector('.mindmap-node.selected,.flow-node.selected')?.classList.remove('selected');
  }}
  function centerPopover(node, popover) {{
    requestAnimationFrame(() => {{
      const view = viewport.getBoundingClientRect(), a = node.getBoundingClientRect(), b = popover.getBoundingClientRect();
      const cx = (Math.min(a.left,b.left)+Math.max(a.right,b.right))/2;
      const cy = (Math.min(a.top,b.top)+Math.max(a.bottom,b.bottom))/2;
      viewport.scrollBy({{left:(cx-(view.left+view.right)/2),top:(cy-(view.top+view.bottom)/2),behavior:'smooth'}});
    }});
  }}
  function openNode(node) {{
    if (node.classList.contains('selected')) {{ dismissPopover(); return; }}
    dismissPopover(); node.classList.add('selected');
    let evidence = []; try {{ evidence = JSON.parse(node.dataset.nodeEvidence || '[]'); }} catch (_) {{}}
    const popover = document.createElement('section'); popover.className = 'diagram-node-popover';
    const heading = document.createElement('strong'); heading.textContent = node.dataset.nodeLabel || '节点证据'; popover.appendChild(heading);
    const quotes = evidence.map(item => String(item.quote || item.text || '')).filter(Boolean);
    const note = document.createElement('small'); note.textContent = quotes.length ? `证据：${{quotes.join('；')}}` : '该节点没有可定位的原文证据。'; popover.appendChild(note);
    const actions = document.createElement('div'); actions.className = 'diagram-node-actions';
    evidence.forEach((item,index) => {{
      const button = document.createElement('button'); button.type='button'; button.textContent = index ? `定位证据 ${{index+1}}（自动匹配）` : '定位原文（自动匹配）';
      button.onclick = event => {{ event.stopPropagation(); bridge?.locateEvidence(JSON.stringify(item)); }}; actions.appendChild(button);
    }});
    const ask = document.createElement('button'); ask.type='button'; ask.textContent='就此提问';
    ask.onclick = event => {{ event.stopPropagation(); bridge?.askNode(JSON.stringify({{label:node.dataset.nodeLabel||'',detail:node.dataset.nodeDetail||''}})); }}; actions.appendChild(ask);
    popover.appendChild(actions); world.appendChild(popover);
    const worldRect = world.getBoundingClientRect(), nodeRect = node.getBoundingClientRect();
    const overlayLeft = nodeRect.left + nodeRect.width/2 - worldRect.left - 140;
    const overlayTop = nodeRect.bottom - worldRect.top + 12;
    popover.style.left = `${{Math.max(12,Math.min(world.clientWidth-292,overlayLeft))}}px`; popover.style.top = `${{Math.max(12,overlayTop)}}px`;
    centerPopover(node,popover);
  }}
  world.querySelectorAll('.mindmap-node,.flow-node').forEach(node => {{
    node.addEventListener('pointerdown', event => event.stopPropagation());
    node.addEventListener('click', event => {{ event.stopPropagation(); openNode(node); }});
    node.addEventListener('keydown', event => {{ if(event.key==='Enter'||event.key===' '){{event.preventDefault();openNode(node);}} }});
  }});
  world.addEventListener('pointerdown', event => {{ if (event.target.closest('.diagram-node-popover')) event.stopPropagation(); }});
  viewport.addEventListener('click', event => {{ if (!event.target.closest('.mindmap-node,.flow-node,.diagram-node-popover')) dismissPopover(); }});
  window.addEventListener('resize', fit); requestAnimationFrame(fit);
}})();
</script></body></html>"""


class MultimodalInputEdit(QPlainTextEdit):
    """
    支持多模态输入的文本框。

    设计目标：
    1. 用户可以像普通聊天软件一样直接 Ctrl+V 粘贴截图或图片文件。
    2. 图片不插入为一大串 base64 文本，而是发出信号交给主窗口管理预览和发送。
    3. 普通文本粘贴仍保持 QPlainTextEdit 原有行为。
    """

    image_pasted = Signal(object, str)

    @staticmethod
    def is_supported_image_file(file_path: str) -> bool:
        """判断本地文件是否是 Qt 能读取的图片。"""
        if not file_path or not os.path.isfile(file_path):
            return False

        reader = QImageReader(file_path)
        return reader.canRead()

    def insertFromMimeData(self, source):
        pasted_any_image = False
        captured_at = datetime.now()

        # 处理剪贴板中的位图，例如截图工具复制的图片。
        if source.hasImage():
            image_data = source.imageData()

            if isinstance(image_data, QImage):
                image = image_data
            elif isinstance(image_data, QPixmap):
                image = image_data.toImage()
            else:
                image = QImage(image_data)

            if not image.isNull():
                pasted_any_image = True
                self._pasted_image_sequence = getattr(self, "_pasted_image_sequence", 0) + 1
                self.image_pasted.emit(
                    image,
                    pasted_image_name(captured_at, self._pasted_image_sequence),
                )

        # 处理从文件管理器复制的图片文件。
        if source.hasUrls():
            for url in source.urls():
                file_path = url.toLocalFile()

                if not self.is_supported_image_file(file_path):
                    continue

                reader = QImageReader(file_path)
                image = reader.read()

                if image.isNull():
                    continue

                pasted_any_image = True
                self.image_pasted.emit(image, os.path.basename(file_path))

        # If the clipboard contains both an image URL and file text, keep the path out of the input.
        if pasted_any_image and source.hasUrls():
            return

        # 图片已经进入附件预览区，输入框只保留真正的文本。
        if pasted_any_image and not source.hasText():
            return

        super().insertFromMimeData(source)


class ModelFetchWorker(QThread):
    models_received = Signal(list)
    error_occurred = Signal(str)

    def __init__(self, api_key: str, base_url: str, provider_id: str):
        super().__init__()
        self.api_key = api_key
        self.provider_id = provider_id
        self.base_url = normalize_base_url(base_url, provider_id)

    def run(self):
        try:
            import requests

            url = self.base_url.rstrip("/") + "/models"
            # SiliconFlow 的通用列表含 embedding、reranker 等模型；对话只请求 chat 子类型。
            if self.provider_id == "siliconflow":
                url += "?sub_type=chat"
            headers = build_headers(self.api_key, stream=False, provider_id=self.provider_id, base_url=self.base_url)

            response = requests.get(
                url,
                headers=headers,
                timeout=30,
            )
            response.encoding = "utf-8"

            if response.status_code != 200:
                self.error_occurred.emit(
                    f"获取模型列表失败（HTTP {response.status_code}）。请检查服务地址、访问权限或网络连接。"
                )
                return

            data = response.json()
            model_ids = []

            if isinstance(data, dict) and isinstance(data.get("data"), list):
                for item in data["data"]:
                    if isinstance(item, dict) and item.get("id"):
                        model_id = str(item["id"])
                        if self.provider_id == "gemini":
                            model_id = normalize_gemini_model_id(model_id)
                        model_ids.append(model_id)

            elif isinstance(data, dict) and isinstance(data.get("models"), list):
                for item in data["models"]:
                    if isinstance(item, str):
                        model_id = item
                        model_ids.append(normalize_gemini_model_id(model_id) if self.provider_id == "gemini" else model_id)
                    elif isinstance(item, dict) and item.get("id"):
                        model_id = str(item["id"])
                        if self.provider_id == "gemini":
                            model_id = normalize_gemini_model_id(model_id)
                        model_ids.append(model_id)

            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        model_id = item
                        model_ids.append(normalize_gemini_model_id(model_id) if self.provider_id == "gemini" else model_id)
                    elif isinstance(item, dict) and item.get("id"):
                        model_id = str(item["id"])
                        if self.provider_id == "gemini":
                            model_id = normalize_gemini_model_id(model_id)
                        model_ids.append(model_id)

            model_ids = sorted(set(model_ids))

            if not model_ids:
                self.error_occurred.emit(
                    "服务已返回内容，但未找到可用的模型列表。请检查服务地址和访问权限后重试。"
                )
                return

            self.models_received.emit(model_ids)

        except Exception as e:
            self.error_occurred.emit(str(e))


class ThinkingCapabilityProbeWorker(QThread):
    """Probe whether a SiliconFlow chat model accepts thinking controls."""

    capability_checked = Signal(str, str, str, object, str)

    def __init__(self, api_key: str, base_url: str, provider_id: str, model: str):
        super().__init__()
        self.api_key = api_key
        self.base_url = normalize_base_url(base_url, provider_id)
        self.provider_id = provider_id
        self.model = model

    def run(self):
        if self.provider_id != "siliconflow":
            self.capability_checked.emit(self.provider_id, self.base_url, self.model, None, "")
            return
        try:
            import requests

            headers = build_headers(self.api_key, stream=False, provider_id=self.provider_id, base_url=self.base_url)
            url = self.base_url.rstrip("/") + "/chat/completions"
            # Test both switch values. The minimum documented thinking budget and
            # one output token keep this capability check small and bounded.
            for enabled in (False, True):
                response = requests.post(
                    url,
                    headers=headers,
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "OK"}],
                        "stream": False,
                        "max_tokens": 1,
                        "enable_thinking": enabled,
                        "thinking_budget": 128,
                    },
                    timeout=60,
                )
                if response.status_code != 200:
                    self.capability_checked.emit(
                        self.provider_id,
                        self.base_url,
                        self.model,
                        False,
                        f"无法检查模型思考设置（HTTP {response.status_code}）。",
                    )
                    return
            self.capability_checked.emit(self.provider_id, self.base_url, self.model, True, "")
        except Exception as exc:
            self.capability_checked.emit(self.provider_id, self.base_url, self.model, None, str(exc))


def content_contains_image_url_parts(content) -> bool:
    """判断 OpenAI 兼容 content 中是否包含图片输入。"""
    if not isinstance(content, list):
        return False

    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            return True

    return False


def strip_image_url_parts_from_content(content):
    """移除 content 中的图片输入，只保留文本部分。"""
    if not isinstance(content, list):
        return redact_local_paths_for_api_text(str(content or "")) if isinstance(content, str) else content

    text_parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
            text = str(part.get("text") or "")
            if text:
                text_parts.append(text)
        elif isinstance(part, str):
            text_parts.append(part)

    return redact_local_paths_for_api_text("\n".join(text_parts))


_LOCAL_PATH_METADATA_LINE = re.compile(
    r"(?im)^(?P<label>来源|文件路径)\s*:\s*(?:[a-z]:[\\/]|\\\\)[^\r\n]*$"
)


def redact_local_paths_for_api_text(text: str) -> str:
    """Hide local absolute paths from the API-only copy of a chat message.

    The UI and local conversation archive keep their paths so document history
    remains navigable.  Only the serialized provider payload is redacted.
    """
    return _LOCAL_PATH_METADATA_LINE.sub(
        lambda match: f"{match.group('label')}: [本地路径已隐藏]",
        str(text or ""),
    )


def sanitize_content_parts_for_api(content):
    """移除本地 UI 元数据，只保留 OpenAI 兼容 content part 字段。"""
    if not isinstance(content, list):
        return redact_local_paths_for_api_text(content) if isinstance(content, str) else content

    sanitized_parts = []
    for part in content:
        if isinstance(part, str):
            sanitized_parts.append(redact_local_paths_for_api_text(part))
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in ("text", "input_text"):
            sanitized_parts.append({
                "type": part_type,
                "text": redact_local_paths_for_api_text(str(part.get("text") or "")),
            })
        elif part_type == "image_url":
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                url = image_url.get("url")
                if url:
                    sanitized_parts.append({
                        "type": "image_url",
                        "image_url": {"url": url},
                    })
            elif isinstance(image_url, str) and image_url:
                sanitized_parts.append({
                    "type": "image_url",
                    "image_url": {"url": image_url},
                })
    return sanitized_parts


def strip_image_url_parts_from_messages(messages: list[dict]) -> list[dict]:
    """把整组消息降级为纯文本消息，用于非多模态模型自动重试。"""
    stripped_messages: list[dict] = []

    for message in messages or []:
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue

        stripped_messages.append({
            "role": role,
            "content": strip_image_url_parts_from_content(message.get("content")),
        })

    return stripped_messages


def image_input_origin_summary(messages: list[dict]) -> str:
    """粗略区分本轮图片更像来自文档上下文还是用户手动粘贴。"""
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not content_contains_image_url_parts(content):
            continue
        text = strip_image_url_parts_from_content(content)
        text = str(text or "")
        if "===== 文档" in text or "IMAGE_" in text or "来源:" in text:
            return "document"
        return "manual"
    return "unknown"


def messages_contain_image_url_parts(messages: list[dict]) -> bool:
    """判断消息列表中是否包含图片输入。"""
    for message in messages or []:
        if isinstance(message, dict) and content_contains_image_url_parts(message.get("content")):
            return True
    return False


def looks_like_non_multimodal_image_error(error_text: str) -> bool:
    """识别文本模型不支持图片输入时常见的服务端错误。"""
    text = str(error_text or "").lower()
    openai_compatible_image_error = (
        "image_url" in text
        and (
            "unknown variant" in text
            or "expected" in text
            or "deserialize" in text
            or "invalid_request_error" in text
            or "not support" in text
            or "unsupported" in text
        )
    )
    # SiliconFlow may reject the same OpenAI-compatible image_url payload with
    # a model-capability message instead of echoing the field name.
    siliconflow_vlm_error = (
        "not a vlm" in text
        and "text-only prompt" in text
    )
    return openai_compatible_image_error or siliconflow_vlm_error


class ChatWorker(QThread):
    chunk_received = Signal(str)

    # reasoning_content 只用于界面展示，不默认写入 messages，不默认回传给模型。
    reasoning_chunk_received = Signal(str)

    # 用于显示缓存命中等非正文信息。
    system_info_received = Signal(str)

    # 图片不在后台线程里创建 QPixmap。
    # 后台线程只返回图片字节，主线程收到后再转 QPixmap，避免跨线程使用 GUI 资源。
    image_received = Signal(bytes)

    finished_reply = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, config: AIConfig, messages: list[dict]):
        super().__init__()
        self.config = config
        # messages 可能包含本地 UI 元数据，例如 assistant 消息中的 reasoning_content。
        # 发给模型接口时必须只保留 OpenAI 兼容字段，避免服务商因额外字段报错。
        self.messages = self.sanitize_messages_for_api(messages)
        self._full_reply_parts: list[str] = []
        self._reasoning_reply_parts: list[str] = []
        self.last_usage = None

        # 响应观测信息：
        # 1. actual_model 记录服务端实际返回的 model，便于发现 OneAPI / NewAPI 网关模型映射。
        # 2. saw_reasoning_text 只表示服务端返回了可展示思考文本。
        # 3. saw_content 只表示服务端返回了正式回答文本。
        self.actual_model = ""
        self.saw_reasoning_text = False
        self.saw_content = False

        # 取消控制：
        # 1. 不使用 terminate() 强杀线程，避免破坏 requests / Qt 资源状态。
        # 2. 主线程调用 request_stop() 后，后台循环会尽快退出。
        # 3. 关闭当前 response / session 可尽量打断阻塞中的网络读取。
        self.cancelled = False
        self.current_session = None
        self.current_response = None

        # 兼容部分模型把思考过程包在 <think>...</think> 中随 content 流式返回的情况。
        self.in_think_tag = False

    @property
    def full_reply(self) -> str:
        return "".join(self._full_reply_parts)

    @property
    def reasoning_reply(self) -> str:
        return "".join(self._reasoning_reply_parts)

    def append_full_reply(self, text: str) -> None:
        if text:
            self._full_reply_parts.append(text)

    def append_reasoning_reply(self, text: str) -> None:
        if text:
            self._reasoning_reply_parts.append(text)

    @staticmethod
    def sanitize_messages_for_api(messages: list[dict]) -> list[dict]:
        """
        清理发送给模型 API 的消息。

        本程序会在历史记录中保存 reasoning_content 等界面元数据；
        这些字段不能直接发送给 OpenAI 兼容接口，否则部分服务商会返回参数错误。
        """
        sanitized: list[dict] = []

        for message in messages or []:
            if not isinstance(message, dict):
                continue

            role = message.get("role")
            content = sanitize_content_parts_for_api(message.get("content"))

            if role not in ("system", "user", "assistant", "tool"):
                continue

            sanitized.append({
                "role": role,
                "content": content,
            })

        return sanitized

    def request_stop(self):
        """请求停止当前生成；由主线程调用，后台线程会安全退出。"""
        self.cancelled = True

        response = self.current_response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

        session = self.current_session
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def is_cancelled(self) -> bool:
        """统一判断是否已经请求停止，便于后续扩展更复杂的取消逻辑。"""
        return bool(self.cancelled)

    def run(self):
        if is_probably_image_model(self.config.model):
            if uses_claude_construction(self.config.provider_id, self.config.request_body_mode):
                self.error_occurred.emit(
                    "当前服务配置不支持图片生成或编辑，请切换到支持图片的服务或模型。"
                )
                return
            self.run_image_chat_completion()
        else:
            self.run_text_chat_completion()

    @staticmethod
    def message_content_to_text(content) -> str:
        """
        从 OpenAI 兼容多模态 content 中提取文本部分。

        content 可能是：
        1. 普通字符串。
        2. [{"type": "text", "text": "..."}, {"type": "image_url", ...}]。
        """
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            text_parts = []

            for part in content:
                if isinstance(part, str):
                    text_parts.append(part)
                    continue

                if not isinstance(part, dict):
                    continue

                if part.get("type") in ("text", "input_text") and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])

            return "\n".join(text_parts)

        return str(content or "")

    @staticmethod
    def stringify_reasoning_value(value) -> str:
        """
        将不同服务商返回的思考字段统一转为字符串。

        常见返回形式：
        1. reasoning_content: "..."
        2. reasoning: {"content": "..."} 或 {"text": "..."}
        3. reasoning_details: [{"text": "..."}]
        4. thinking: "..."
        """
        if value is None:
            return ""

        if isinstance(value, str):
            return value

        if isinstance(value, dict):
            parts = []

            for key in (
                "content",
                "text",
                "summary",
                "reasoning_content",
                "reasoning",
                "thinking",
                "thought",
            ):
                if key not in value:
                    continue

                part_text = ChatWorker.stringify_reasoning_value(value.get(key))
                if part_text:
                    parts.append(part_text)

            return "".join(parts)

        if isinstance(value, list):
            return "".join(
                ChatWorker.stringify_reasoning_value(item)
                for item in value
            )

        return str(value)

    def extract_reasoning_text(self, chunk: dict, choice: dict, delta: dict) -> str:
        """
        从流式 chunk 中尽量提取服务商公开返回的思考内容。

        注意：
        这里不能“生成”或“还原”模型隐藏的内部思考链；
        只能展示 API 响应中实际返回的 reasoning/thinking 字段。
        """
        # Some OpenRouter upstreams expose the same delta under several
        # aliases. Select one canonical value rather than concatenating every
        # alias and showing duplicated characters in the thinking panel.
        containers = (
            delta,
            choice.get("message", {}) if isinstance(choice, dict) else {},
            choice,
            chunk,
        )
        for key in (
            "reasoning_details",
            "reasoning",
            "reasoning_content",
            "reasoning_text",
            "reasoning_summary",
            "thinking",
            "thought",
            "analysis",
        ):
            for container in containers:
                if not isinstance(container, dict) or key not in container:
                    continue
                text = self.stringify_reasoning_value(container.get(key))
                if text:
                    return text
        return ""

    @staticmethod
    def safe_int(value, default: int = 0) -> int:
        """把 usage 中可能为 None / 字符串 / 数字的字段安全转为 int。"""
        try:
            if value is None:
                return default
            return int(value)
        except Exception:
            return default

    def build_usage_summary_lines(self) -> list[str]:
        """
        构造单行 usage 汇总信息。

        说明：
        1. 这里把输入、输出、推理、缓存命中、响应模型尽量合并成一行，避免界面中出现多条重复气泡。
        2. 返回 list[str]，便于调用方统一展示或合并。
        """
        if not isinstance(self.last_usage, dict):
            return []

        usage = self.last_usage
        parts: list[str] = []

        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")

        if prompt_tokens is not None:
            parts.append(f"输入 {prompt_tokens} tokens")
        if completion_tokens is not None:
            parts.append(f"输出 {completion_tokens} tokens")
        if total_tokens is not None:
            parts.append(f"总计 {total_tokens} tokens")

        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            reasoning_tokens = completion_details.get("reasoning_tokens")
            if reasoning_tokens is not None:
                parts.append(f"推理 {reasoning_tokens} tokens")

        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_tokens = prompt_details.get("cached_tokens")
            if cached_tokens is not None:
                parts.append(f"命中 {cached_tokens} tokens")
            cache_write_tokens = prompt_details.get("cache_write_tokens")
            if cache_write_tokens is not None:
                parts.append(f"写入 {cache_write_tokens} tokens")

        cache_hit = usage.get("prompt_cache_hit_tokens")
        if cache_hit is not None:
            parts.append(f"命中 {cache_hit} tokens")

        # 这里保留“缓存未命中”信息，但不单独拆成新行，避免消息气泡过多。
        cache_miss = usage.get("prompt_cache_miss_tokens")
        if cache_miss is not None:
            parts.append(f"未命中 {cache_miss} tokens")

        cache_discount = usage.get("cache_discount")
        if cache_discount is not None:
            parts.append(f"缓存折扣 {cache_discount}")

        if not parts:
            return []

        return ["用量统计：" + "，".join(parts) + "。"]

    @staticmethod
    def summarize_usage_tokens(usage: dict | None) -> dict[str, int]:
        """
        从服务端 usage 中提取缓存统计。

        这个纯函数便于做单元测试，确认不同服务商返回的字段都能读到。
        """
        if not isinstance(usage, dict):
            return {}

        summary: dict[str, int] = {}

        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_tokens = prompt_details.get("cached_tokens")
            if cached_tokens is not None:
                summary["cached_tokens"] = cached_tokens
            cache_write_tokens = prompt_details.get("cache_write_tokens")
            if cache_write_tokens is not None:
                summary["cache_write_tokens"] = cache_write_tokens

        cache_hit = usage.get("prompt_cache_hit_tokens")
        if cache_hit is not None:
            summary["prompt_cache_hit_tokens"] = cache_hit

        cache_miss = usage.get("prompt_cache_miss_tokens")
        if cache_miss is not None:
            summary["prompt_cache_miss_tokens"] = cache_miss

        return summary

    def get_reasoning_token_count(self) -> int:
        """读取 OpenAI 风格 usage 中的内部推理 token 数。"""
        if not isinstance(self.last_usage, dict):
            return 0

        completion_details = self.last_usage.get("completion_tokens_details")
        if not isinstance(completion_details, dict):
            return 0

        return self.safe_int(completion_details.get("reasoning_tokens"), 0)

    def emit_text_response_summary(self):
        """
        输出本轮文本响应摘要。

        界面消息尽量合并为一行，避免系统提示气泡过多。
        """
        lines = []

        if self.actual_model:
            lines.append(f"响应模型：{self.actual_model}")

        lines.append(f"思考：{'有' if self.saw_reasoning_text else '无'}")
        lines.append(f"回答：{'有' if self.saw_content else '无'}")
        lines.append(f"思考文本长度：{len(self.reasoning_reply)} 字")
        lines.append(f"回答文本长度：{len(self.full_reply)} 字")

        usage_lines = self.build_usage_summary_lines()
        lines.extend(usage_lines)

        # 将“响应模型 + 用量统计”尽量合并成一条系统消息，减少气泡数量。
        summary_parts = []
        if self.actual_model:
            summary_parts.append(f"响应模型：{self.actual_model}")

        if usage_lines:
            usage_text = usage_lines[0]
            if usage_text.startswith("用量统计："):
                usage_text = usage_text.removeprefix("用量统计：").strip()
            summary_parts.append(usage_text)

        if summary_parts:
            self.system_info_received.emit("；".join(summary_parts))

        # 如果本次没有可合并的用量信息，不额外拆分提示。

    def split_visible_and_tagged_reasoning(self, text: str) -> tuple[str, str]:
        """
        兼容 <think>...</think> 与 Gemini <thought>...</thought> 输出。

        有些模型或网关不会返回 reasoning_content 字段，
        而是把思考过程直接放进 content 的 <think> 标签里。
        这里把 <think> 内文本移到思考过程控件中，避免混入最终回答气泡。
        """
        if not text:
            return "", ""

        visible_parts = []
        reasoning_parts = []
        index = 0

        while index < len(text):
            if self.in_think_tag:
                end_candidates = [
                    (text.find("</think>", index), "</think>"),
                    (text.find("</thought>", index), "</thought>"),
                ]
                end_index, end_tag = min(
                    ((position, tag) for position, tag in end_candidates if position != -1),
                    default=(-1, ""),
                    key=lambda item: item[0],
                )

                if end_index == -1:
                    reasoning_parts.append(text[index:])
                    index = len(text)
                else:
                    reasoning_parts.append(text[index:end_index])
                    self.in_think_tag = False
                    index = end_index + len(end_tag)
            else:
                start_candidates = [
                    (text.find("<think>", index), "<think>"),
                    (text.find("<thought>", index), "<thought>"),
                ]
                start_index, start_tag = min(
                    ((position, tag) for position, tag in start_candidates if position != -1),
                    default=(-1, ""),
                    key=lambda item: item[0],
                )

                if start_index == -1:
                    visible_parts.append(text[index:])
                    index = len(text)
                else:
                    visible_parts.append(text[index:start_index])
                    self.in_think_tag = True
                    index = start_index + len(start_tag)

        return "".join(visible_parts), "".join(reasoning_parts)

    def run_text_chat_completion(self):
        request_slot = None
        try:
            import requests

            base_url = normalize_base_url(
                self.config.base_url,
                self.config.provider_id,
            )
            construction_mode = normalize_request_body_mode(
                self.config.provider_id,
                self.config.request_body_mode,
            )
            use_claude_messages = construction_mode == REQUEST_BODY_MODE_CLAUDE
            if use_claude_messages:
                url = request_url_for_construction(base_url, self.config.provider_id, construction_mode)
                headers = claude_headers(self.config.api_key, stream=True)
                payload = build_claude_messages_payload(
                    self.config.model,
                    self.messages,
                    stream=True,
                    temperature=0.7 if should_send_temperature(self.config) else None,
                )
            else:
                url = request_url_for_construction(base_url, self.config.provider_id, construction_mode)
                use_codex_session_headers = (
                    uses_codex_construction(self.config.provider_id, construction_mode)
                    and bool(self.config.prompt_cache_key)
                )
                headers = build_headers(
                    self.config.api_key,
                    stream=True,
                    prompt_cache_key=self.config.prompt_cache_key,
                    use_codex_session_headers=use_codex_session_headers,
                    provider_id=self.config.provider_id,
                    base_url=self.config.base_url,
                )
                payload = build_text_chat_payload(self.config, self.messages)

            # Document chat and translation share the same DeepSeek request slots.
            # 翻译已占满 100 路时，对话会等待空槽，而不是突破应用侧上限。
            request_slot = acquire_provider_request_slot(
                self.config.provider_id,
                self.is_cancelled,
            )
            self.current_session = requests.Session()
            response = self.current_session.post(
                url,
                headers=headers,
                json=payload,
                stream=True,
                timeout=120,
            )
            self.current_response = response
            response.encoding = "utf-8"

            if self.is_cancelled():
                self.system_info_received.emit("已停止生成。")
                self.finished_reply.emit(self.full_reply)
                return

            if response.status_code != 200:
                error_text = response.text[:4000]

                if (
                    response.status_code == 400
                    and messages_contain_image_url_parts(self.messages)
                    and looks_like_non_multimodal_image_error(error_text)
                ):
                    self.error_occurred.emit(
                        "NON_MULTIMODAL_IMAGE_INPUT_UNSUPPORTED\n"
                        + error_text[:2000]
                    )
                    return

                self.error_occurred.emit(
                    f"请求失败（HTTP {response.status_code}）。请检查服务配置或稍后重试。"
                )
                return

            for raw_line in response.iter_lines(decode_unicode=False):
                if self.is_cancelled():
                    break

                if not raw_line:
                    continue

                line = raw_line.decode("utf-8", errors="ignore").strip()

                if not line.startswith("data:"):
                    continue

                data = line[5:].strip()

                if data == "[DONE]":
                    break

                try:
                    chunk = json.loads(data)
                except Exception:
                    continue

                if use_claude_messages:
                    event_type = str(chunk.get("type") or "")
                    if event_type == "message_start":
                        message = chunk.get("message") if isinstance(chunk.get("message"), dict) else {}
                        model = message.get("model")
                        if isinstance(model, str) and model.strip():
                            self.actual_model = model.strip()
                        self.last_usage = normalize_claude_usage(message.get("usage"), self.last_usage)
                    elif event_type == "message_delta":
                        usage = chunk.get("usage")
                        self.last_usage = normalize_claude_usage(usage, self.last_usage)
                    elif event_type == "content_block_delta":
                        delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
                        delta_type = str(delta.get("type") or "")
                        if delta_type == "thinking_delta":
                            reasoning_text = str(delta.get("thinking") or "")
                            if reasoning_text:
                                self.saw_reasoning_text = True
                                self.append_reasoning_reply(reasoning_text)
                                if self.config.show_reasoning:
                                    self.reasoning_chunk_received.emit(reasoning_text)
                        elif delta_type == "text_delta":
                            text = str(delta.get("text") or "")
                            if text:
                                self.saw_content = True
                                self.append_full_reply(text)
                                self.chunk_received.emit(text)
                    continue

                chunk_model = chunk.get("model")
                if isinstance(chunk_model, str) and chunk_model.strip():
                    self.actual_model = chunk_model.strip()

                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    self.last_usage = usage

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})

                # DeepSeek、OneAPI / NewAPI 以及部分代理网关可能使用不同字段返回思考过程。
                # 普通聊天中它只用于界面展示，不写入 self.messages，也不回传给模型。
                reasoning_text = self.extract_reasoning_text(chunk, choices[0], delta)

                if reasoning_text:
                    self.saw_reasoning_text = True
                    self.append_reasoning_reply(reasoning_text)
                    if self.config.show_reasoning:
                        self.reasoning_chunk_received.emit(reasoning_text)

                text = delta.get("content")
                if text:
                    text = str(text)

                    # 兼容把思考链放在 <think>...</think> 中的模型。
                    visible_text, tagged_reasoning_text = self.split_visible_and_tagged_reasoning(text)

                    if tagged_reasoning_text:
                        self.saw_reasoning_text = True
                        self.append_reasoning_reply(tagged_reasoning_text)
                        if self.config.show_reasoning:
                            self.reasoning_chunk_received.emit(tagged_reasoning_text)

                    if visible_text:
                        self.saw_content = True
                        self.append_full_reply(visible_text)
                        self.chunk_received.emit(visible_text)

            if self.is_cancelled():
                self.system_info_received.emit("已停止生成。")
                self.finished_reply.emit(self.full_reply)
                return

            self.emit_text_response_summary()

            if self.config.show_reasoning and not self.reasoning_reply.strip():
                reasoning_tokens = self.get_reasoning_token_count()

                if reasoning_tokens > 0:
                    self.system_info_received.emit(
                        "本次回复包含内部推理，但服务没有提供可显示的思考摘要。"
                    )
                else:
                    self.system_info_received.emit(
                        "本次回复没有提供可显示的思考过程。"
                    )

            self.finished_reply.emit(self.full_reply)

        except Exception as e:
            if self.is_cancelled():
                self.system_info_received.emit("已停止生成。")
                self.finished_reply.emit(self.full_reply)
            else:
                self.error_occurred.emit(str(e))
        finally:
            release_provider_request_slot(request_slot)

    def run_image_chat_completion(self):
        """
        图片模型走 /v1/images/generations 或 /v1/images/edits。

        说明：
        1. 文本模型继续走 /v1/chat/completions
        2. 图片模型（如 gpt-image-2）走 /v1/images/generations 或 /v1/images/edits
        3. 是否走 edits 由当前轮是否提供参考图自动决定
        4. prompt 使用当前会话里“最后一条 user 消息”的原文
        """
        try:
            import requests

            last_user_content = None
            last_user_index = -1
            for index in range(len(self.messages) - 1, -1, -1):
                item = self.messages[index]
                if item.get("role") == "user":
                    last_user_content = item.get("content", "")
                    last_user_index = index
                    break

            base_url = normalize_base_url(
                self.config.base_url,
                self.config.provider_id,
            )

            self.current_session = requests.Session()

            if self.is_cancelled():
                self.system_info_received.emit("已停止生成。")
                self.finished_reply.emit(self.full_reply)
                return

            edit_image_files, edit_source_label = self.resolve_image_edit_inputs(
                last_user_index,
                last_user_content,
            )
            use_edit_mode = bool(edit_image_files)
            prompt = self.build_image_conversation_prompt(
                last_user_index,
                last_user_content,
                use_edit_mode=use_edit_mode,
                edit_source_label=edit_source_label,
            )

            if use_edit_mode:
                url = base_url.rstrip("/") + "/images/edits"
                headers = build_multipart_headers(self.config.api_key)

                data = {
                    "model": self.config.model,
                    "prompt": prompt,
                    "size": self.config.image_size,
                    "quality": self.config.image_quality,
                    "output_format": self.config.image_output_format,
                }

                self.system_info_received.emit(
                    f"图片编辑已启用：{edit_source_label}。图片服务不会保留上一轮图片，本应用会将这些图片作为本次输入重新发送。"
                )
                response = self.post_image_edit_request(
                    url=url,
                    headers=headers,
                    data=data,
                    image_files=edit_image_files,
                )

            else:
                url = base_url.rstrip("/") + "/images/generations"
                headers = build_headers(self.config.api_key, stream=False, provider_id=self.config.provider_id, base_url=self.config.base_url)

                payload = {
                    "model": self.config.model,
                    "prompt": prompt,
                    "size": self.config.image_size,
                    "quality": self.config.image_quality,
                    "output_format": self.config.image_output_format,
                }

                response = self.current_session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=300,
                )

            self.current_response = response
            response.encoding = "utf-8"

            if self.is_cancelled():
                self.system_info_received.emit("已停止生成。")
                self.finished_reply.emit(self.full_reply)
                return

            if response.status_code != 200:
                self.error_occurred.emit(
                    f"图片请求失败（HTTP {response.status_code}）。请检查服务配置或稍后重试。"
                )
                return

            try:
                data = response.json()
            except Exception:
                self.error_occurred.emit(
                    "图片服务返回了无法读取的内容，请检查服务配置或稍后重试。"
                )
                return

            text_parts = []
            image_items = []

            self.collect_text_and_images(data, text_parts, image_items)

            # 如果服务端返回 revised_prompt，也展示出来
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                for item in data["data"]:
                    if isinstance(item, dict):
                        revised_prompt = item.get("revised_prompt")
                        if isinstance(revised_prompt, str) and revised_prompt.strip():
                            text_parts.append(revised_prompt)

            text = "\n".join(part for part in text_parts if part)

            if text:
                self.append_full_reply(text)
                self.chunk_received.emit(text)

            rendered_any_image = False

            for item in image_items:
                if self.is_cancelled():
                    break

                image_bytes = self.image_item_to_bytes(item)
                if image_bytes:
                    rendered_any_image = True
                    self.image_received.emit(image_bytes)

            if self.is_cancelled():
                self.system_info_received.emit("已停止生成。")
                self.finished_reply.emit(self.full_reply)
                return

            if not rendered_any_image and not text:
                self.error_occurred.emit("图片服务已返回内容，但未找到可显示的图片或文字。")
                return

            self.finished_reply.emit(self.full_reply)

        except Exception as e:
            if self.is_cancelled():
                self.system_info_received.emit("已停止生成。")
                self.finished_reply.emit(self.full_reply)
            else:
                self.error_occurred.emit(str(e))

    @staticmethod
    def message_content_to_image_data_urls(content) -> list[str]:
        image_data_urls: list[str] = []

        if not isinstance(content, list):
            return image_data_urls

        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue

            image_url = part.get("image_url")
            data_url = image_url.get("url") if isinstance(image_url, dict) else image_url

            if isinstance(data_url, str) and data_url.startswith("data:image/"):
                image_data_urls.append(data_url)

        return image_data_urls

    def build_image_conversation_prompt(
        self,
        last_user_index: int,
        last_user_content,
        *,
        use_edit_mode: bool,
        edit_source_label: str = "",
    ) -> str:
        latest_user_text = self.message_content_to_text(last_user_content).strip()
        lines = [
            "请完成当前用户的图片生成或编辑要求。",
            "当前要求优先级最高；若与此前要求冲突，以当前要求为准。",
        ]

        # Images API 并不维护会话状态，因此保留先前用户要求可以让连续编辑
        # 保持意图连贯。但绝不能把 assistant 回复（尤其是服务返回的
        # revised_prompt）再次拼入 prompt：那会使应用自己的提示模板逐轮递归。
        # 图片本身由 resolve_image_edit_inputs() 单独作为 edits 请求的输入上传，
        # 这里不重复描述或伪造历史图片。
        earlier_user_texts = []
        history_messages = self.messages[:last_user_index] if last_user_index >= 0 else []
        for message in history_messages:
            if str(message.get("role") or "") != "user":
                continue
            text = self.message_content_to_text(message.get("content")).strip()
            if text:
                earlier_user_texts.append(text)

        if earlier_user_texts:
            lines.append("")
            lines.append("===== 此前用户要求（仅用于理解上下文） =====")
            for index, text in enumerate(earlier_user_texts, 1):
                lines.append(f"[{index}] {text}")

        lines.append("")
        lines.append("===== 当前用户要求 =====")
        lines.append(latest_user_text or ("请根据参考图继续修改。" if use_edit_mode else "请生成一张图片。"))

        if use_edit_mode:
            lines.append("")
            lines.append("请结合本次随请求附带的图片进行编辑。")
            if edit_source_label:
                lines.append(f"图片顺序与用途：{edit_source_label}。")
            lines.append(
                "若其中标明了当前编辑底图，以该图为主要修改对象；其余图片作为附加参考，除非当前要求另有说明。"
            )

        return "\n".join(lines).strip()

    def resolve_image_edit_inputs(self, last_user_index: int, last_user_content) -> tuple[list[tuple[str, bytes, str]], str]:
        image_files: list[tuple[str, bytes, str]] = []
        seen_sources: set[str] = set()
        source_groups: list[tuple[str, list[int]]] = []

        def add_payload(payload: tuple[str, bytes, str] | None, source_key: str) -> int:
            if not payload or source_key in seen_sources:
                return 0
            image_files.append(payload)
            seen_sources.add(source_key)
            return len(image_files)

        def add_group(label: str, indexes: list[int]):
            valid_indexes = [index for index in indexes if index > 0]
            if valid_indexes:
                source_groups.append((label, valid_indexes))

        # 连续编辑时，最近一张模型输出始终排在最前，作为当前编辑底图。
        # Images API 不保存会话图片，所以每轮都必须由客户端重新上传它。
        if last_user_index >= 0:
            latest_assistant_images = self.find_latest_assistant_image_data_urls(before_index=last_user_index)
            if latest_assistant_images:
                data_url = latest_assistant_images[-1]
                index = add_payload(
                    self.data_url_to_upload_file(data_url, "latest-assistant-image.png"),
                    f"data:{data_url}",
                )
                add_group("最近一张模型输出图（当前编辑底图）", [index])

        # 本轮新粘贴的图片作为附加输入；首次编辑且没有历史输出图时，
        # 它们自然成为本次 edits 请求的主要输入。
        current_turn_image_urls = self.message_content_to_image_data_urls(last_user_content)
        current_turn_indexes: list[int] = []
        for index, data_url in enumerate(current_turn_image_urls, 1):
            current_turn_indexes.append(add_payload(
                self.data_url_to_upload_file(data_url, f"turn-image-{index}.png"),
                f"data:{data_url}",
            ))
        add_group("本轮新图片（附加输入）", current_turn_indexes)

        local_path_text = str(getattr(self.config, "local_reference_image_path", "") or "").strip()
        if local_path_text:
            local_path = Path(local_path_text)
            local_index = add_payload(
                self.local_image_to_upload_file(local_path),
                f"local:{local_path.resolve()}",
            )
            add_group("手动选择的本地参考图", [local_index])

        selected_reference_indexes: list[int] = []
        for reference_index, item in enumerate(getattr(self.config, "selected_reference_images", []) or [], 1):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "chat")
            if kind == "local":
                path_text = str(item.get("path") or "").strip()
                if not path_text:
                    continue
                path = Path(path_text)
                selected_reference_indexes.append(add_payload(
                    self.local_image_to_upload_file(path),
                    f"local:{path.resolve()}",
                ))
                continue
            data_url = str(item.get("data_url") or "")
            selected_reference_indexes.append(add_payload(
                self.data_url_to_upload_file(data_url, f"selected-reference-{reference_index}.png"),
                f"data:{data_url}",
            ))
        add_group("手动选中的历史参考图", selected_reference_indexes)

        # 没有任何显式输入或模型输出时，才回退到最近一张历史用户图片。
        if not image_files and last_user_index >= 0:
            latest_user_images = self.find_latest_user_image_data_urls(before_index=last_user_index)
            if latest_user_images:
                data_url = latest_user_images[-1]
                history_index = add_payload(
                    self.data_url_to_upload_file(data_url, "latest-user-image.png"),
                    f"data:{data_url}",
                )
                add_group("最近一张历史用户图片（回退编辑底图）", [history_index])

        return image_files, self.format_image_source_groups(source_groups)

    @staticmethod
    def format_image_source_groups(source_groups: list[tuple[str, list[int]]]) -> str:
        parts: list[str] = []
        for label, indexes in source_groups:
            if not indexes:
                continue
            if len(indexes) == 1:
                position = f"第 {indexes[0]} 张"
            elif indexes == list(range(indexes[0], indexes[-1] + 1)):
                position = f"第 {indexes[0]}–{indexes[-1]} 张"
            else:
                position = "第 " + "、".join(str(index) for index in indexes) + " 张"
            parts.append(f"{position}：{label}")
        return "；".join(parts)

    def find_latest_assistant_image_data_urls(self, before_index: int) -> list[str]:
        for index in range(before_index - 1, -1, -1):
            message = self.messages[index]
            if str(message.get("role") or "") != "assistant":
                continue
            image_urls = self.message_content_to_image_data_urls(message.get("content"))
            if image_urls:
                return image_urls
        return []

    def find_latest_user_image_data_urls(self, before_index: int) -> list[str]:
        for index in range(before_index - 1, -1, -1):
            message = self.messages[index]
            if str(message.get("role") or "") != "user":
                continue
            image_urls = self.message_content_to_image_data_urls(message.get("content"))
            if image_urls:
                return image_urls
        return []

    @staticmethod
    def local_image_to_upload_file(path: Path) -> tuple[str, bytes, str] | None:
        try:
            image_bytes = path.read_bytes()
        except Exception:
            return None

        mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
        return path.name, image_bytes, mime_type

    def post_image_edit_request(self, url: str, headers: dict, data: dict, image_files: list[tuple[str, bytes, str]]):
        """
        兼容不同 OpenAI 兼容网关对多图字段名的差异。

        1. 官方常见示例使用 image 数组语义；
        2. 一些兼容网关接受重复的 image 字段；
        3. 先尝试 image[]，失败后再回退到 image。
        """
        if not image_files:
            raise ValueError("图片编辑请求缺少可用输入图片。")

        if len(image_files) <= 1:
            single_name, single_bytes, single_mime = image_files[0]
            return self.current_session.post(
                url,
                headers=headers,
                data=data,
                files={"image": (single_name, single_bytes, single_mime)},
                timeout=300,
            )

        attempts = [
            [("image[]", payload) for payload in image_files],
            [("image", payload) for payload in image_files],
        ]

        last_response = None

        for files in attempts:
            response = self.current_session.post(
                url,
                headers=headers,
                data=data,
                files=files,
                timeout=300,
            )
            last_response = response

            if response.status_code == 200:
                return response

        return last_response

    def collect_text_and_images(self, obj, text_parts: list, image_items: list):
        """
        递归解析返回体。
        不修改模型返回，只提取可显示的文本和图片。
        """
        if obj is None:
            return

        if isinstance(obj, str):
            # 普通字符串可能是文本，也可能是 URL/base64。
            if self.looks_like_image_url(obj):
                image_items.append({"type": "url", "value": obj})
            elif self.looks_like_base64_image(obj):
                image_items.append({"type": "base64", "value": obj})
            else:
                # 提取 Markdown 图片，但仍保留原文本，保证记录完整回复。
                markdown_images = self.extract_markdown_image_urls(obj)
                for url in markdown_images:
                    image_items.append({"type": "url", "value": url})
                text_parts.append(obj)
            return

        if isinstance(obj, list):
            for item in obj:
                self.collect_text_and_images(item, text_parts, image_items)
            return

        if isinstance(obj, dict):
            # OpenAI Chat Completions 标准路径
            if "choices" in obj and isinstance(obj["choices"], list):
                for choice in obj["choices"]:
                    self.collect_text_and_images(choice, text_parts, image_items)

            if "message" in obj:
                self.collect_text_and_images(obj["message"], text_parts, image_items)

            # content 可能是 str，也可能是多模态 list
            if "content" in obj:
                content = obj["content"]

                if isinstance(content, str):
                    markdown_images = self.extract_markdown_image_urls(content)
                    for url in markdown_images:
                        image_items.append({"type": "url", "value": url})
                    text_parts.append(content)

                elif isinstance(content, list):
                    for part in content:
                        self.collect_text_and_images(part, text_parts, image_items)

            # 多模态 content 常见字段
            if obj.get("type") in ("text", "output_text") and isinstance(obj.get("text"), str):
                text_parts.append(obj["text"])

            if obj.get("type") in ("image_url", "output_image"):
                image_url_obj = obj.get("image_url")
                if isinstance(image_url_obj, dict):
                    url = image_url_obj.get("url")
                    if url:
                        image_items.append({"type": "url", "value": url})
                elif isinstance(image_url_obj, str):
                    image_items.append({"type": "url", "value": image_url_obj})

            # Images API 风格
            if "data" in obj and isinstance(obj["data"], list):
                for item in obj["data"]:
                    self.collect_text_and_images(item, text_parts, image_items)

            if "url" in obj and isinstance(obj["url"], str) and self.looks_like_image_url(obj["url"]):
                image_items.append({"type": "url", "value": obj["url"]})

            if "image_url" in obj:
                image_url_obj = obj["image_url"]
                if isinstance(image_url_obj, dict):
                    url = image_url_obj.get("url")
                    if url:
                        image_items.append({"type": "url", "value": url})
                elif isinstance(image_url_obj, str):
                    image_items.append({"type": "url", "value": image_url_obj})

            if "b64_json" in obj and isinstance(obj["b64_json"], str):
                image_items.append({"type": "base64", "value": obj["b64_json"]})

            if "base64" in obj and isinstance(obj["base64"], str):
                image_items.append({"type": "base64", "value": obj["base64"]})

            if "result" in obj and isinstance(obj["result"], str):
                if self.looks_like_base64_image(obj["result"]):
                    image_items.append({"type": "base64", "value": obj["result"]})
                elif self.looks_like_image_url(obj["result"]):
                    image_items.append({"type": "url", "value": obj["result"]})

            if "images" in obj:
                self.collect_text_and_images(obj["images"], text_parts, image_items)

    @staticmethod
    def extract_markdown_image_urls(text: str) -> list[str]:
        return re.findall(r"!\[[^\]]*\]\((.*?)\)", text)

    @staticmethod
    def looks_like_image_url(value: str) -> bool:
        value = value.strip()

        if value.startswith("data:image/"):
            return True

        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            return False

        lower_path = parsed.path.lower()
        image_exts = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg")
        return lower_path.endswith(image_exts) or "image" in lower_path

    @staticmethod
    def looks_like_base64_image(value: str) -> bool:
        value = value.strip()

        if value.startswith("data:image/"):
            return True

        if len(value) < 200:
            return False

        # 宽松判断，避免把普通长文本误判为图片
        return bool(re.fullmatch(r"[A-Za-z0-9+/=\s]+", value[:500]))

    def image_item_to_bytes(self, item: dict) -> bytes:
        """
        将模型返回的图片转为原始图片字节。

        重要：
        1. 后台线程只处理网络下载、base64 解码等非 GUI 工作。
        2. 不在后台线程创建 QPixmap，避免跨线程使用 GUI 资源。
        3. 不写入 tempfile，不创建本地缓存目录。
        4. 主线程收到 bytes 后再创建 QPixmap 并显示。
        """
        image_type = item.get("type")
        value = item.get("value")

        if not value:
            return b""

        try:
            if image_type == "url":
                value = value.strip()

                if value.startswith("data:image/"):
                    return self.data_url_to_bytes(value)

                # URL 图片只下载到内存，不保存到本地磁盘。
                # 如果当前请求已经创建 Session，则复用 Session，方便停止生成时统一关闭。
                import requests

                session = self.current_session or requests.Session()
                response = session.get(value, timeout=300)
                response.raise_for_status()
                return response.content

            if image_type == "base64":
                return self.base64_image_to_bytes(value)

        except Exception as e:
            if not self.is_cancelled():
                self.error_occurred.emit(f"图片解析失败：{e}")

        return b""

    @staticmethod
    def data_url_to_bytes(data_url: str) -> bytes:
        """将 data:image/...;base64,... 转为图片字节，不落盘。"""
        _, encoded = data_url.split(",", 1)
        return base64.b64decode(encoded, validate=False)

    @staticmethod
    def data_url_to_upload_file(data_url: str, fallback_name: str = "reference.png") -> tuple[str, bytes, str] | None:
        if not isinstance(data_url, str) or not data_url.startswith("data:") or "," not in data_url:
            return None

        header, encoded = data_url.split(",", 1)
        mime_type = header[5:].split(";", 1)[0] or "image/png"

        try:
            image_bytes = base64.b64decode(encoded, validate=False)
        except Exception:
            return None

        ext = mimetypes.guess_extension(mime_type) or ".png"
        file_name = fallback_name
        if not file_name.lower().endswith(ext):
            file_name = f"{Path(file_name).stem}{ext}"

        return file_name, image_bytes, mime_type

    @staticmethod
    def base64_image_to_bytes(encoded: str) -> bytes:
        """将 base64 图片字符串转为图片字节，不落盘。"""
        encoded = encoded.strip()

        if encoded.startswith("data:image/"):
            return ChatWorker.data_url_to_bytes(encoded)

        return base64.b64decode(encoded, validate=False)


class CollapsibleReasoningWidget(QWidget):
    """单轮回复的可折叠思考过程，放在对应 assistant 气泡上方。"""

    MAX_BUBBLE_WIDTH = 680
    _MATH_CACHE_DIR = APP_DIR / "chat_math_cache"

    def __init__(self, parent=None):
        super().__init__(parent)

        self._reasoning_parts: list[str] = []
        self._reasoning_char_count = 0
        self._reasoning_trailing_text = ""

        self.setVisible(False)

        # 思考框宽度尽量与回答框保持一致，避免出现“思考框比回答框更窄”的视觉问题。
        # 不能强制 680px 最小宽度：嵌入式文献对话面板常小于该宽度，强制宽度会让
        # QLabel 按 680px 排版、再被父容器裁切，表现为“思考过程显示不全”。
        self.setMinimumWidth(0)
        self.setMaximumWidth(self.MAX_BUBBLE_WIDTH)

        # 使用无边框、无内部滚动条的 QTextBrowser，而不是 QLabel。QLabel 的
        # heightForWidth 会把样式表行高、旧最小高度和布局时序混在一起，难以
        # 精确收紧到底部。QTextDocument 可以直接给出稳定的排版高度。
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 6)
        layout.setSpacing(4)

        self.toggle_button = QToolButton(self)
        self.toggle_button.setText("思考过程")
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)
        self.toggle_button.toggled.connect(self.on_toggled)
        self.toggle_button.setStyleSheet("""
            QToolButton {
                background-color: transparent;
                color: #111111;
                font-size: 13px;
                padding: 2px 4px;
                border: none;
            }
            QToolButton:hover {
                background-color: #F0F0EE;
                border-radius: 6px;
            }
        """)

        self.content_box = QTextBrowser()
        self.content_box.setReadOnly(True)
        self.content_box.setFrameShape(QFrame.Shape.NoFrame)
        self.content_box.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.content_box.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.content_box.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_box.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.content_box.setAlignment(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignTop
        )

        # 思考内容区域与回答气泡宽度统一，避免折叠区被内容宽度“挤窄”。
        self.content_box.setMinimumWidth(0)
        self.content_box.setMaximumWidth(self.MAX_BUBBLE_WIDTH)

        # 不设置固定高度、不设置最大高度、不使用内部滚动条。
        # QLabel 会根据文本完整撑开高度，用户只需要滚动外层聊天区域。
        self.content_box.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.content_box.setStyleSheet("""
            QTextBrowser {
                /* 去掉思考过程文本的灰色色块，改为透明背景，与聊天区更统一。 */
                background-color: transparent;
                color: #4A4A46;
                font-size: 13px;
                line-height: 1.45;
                padding: 6px 10px;
                border: none;
                border-left: 3px solid #111111;
                border-radius: 0px;
            }
        """)

        layout.addWidget(self.toggle_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.content_box)

        # 组件首次插入 QSplitter 时会经历一次临时窄宽度布局；等父布局稳定后
        # 再计算一遍高度，避免留下按旧宽度换行形成的大块空白。
        self._layout_update_timer = QTimer(self)
        self._layout_update_timer.setSingleShot(True)
        self._layout_update_timer.setInterval(120)
        self._layout_update_timer.timeout.connect(self.update_full_height)

    @property
    def reasoning_text(self) -> str:
        return "".join(self._reasoning_parts)

    def on_toggled(self, checked: bool):
        self.content_box.setVisible(checked)
        self.toggle_button.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self.update_title()
        self.update_full_height()

    def update_title(self):
        count = self._reasoning_char_count
        state = "" if self.toggle_button.isChecked() else "（已折叠）"
        self.toggle_button.setText(f"思考过程{state} · {count} 字")

    def set_expanded(self, expanded: bool):
        self.toggle_button.setChecked(expanded)
        self.on_toggled(expanded)

    def update_full_height(self):
        """让思考过程按完整内容参与布局，不在内部产生滚动区域。"""
        # 以父布局已分配的真实宽度计算换行高度，而不是沿用最大宽度。
        # 这对左侧窄栏中的文献对话尤其重要。
        available_width = self.contentsRect().width()
        if available_width > 0:
            content_width = min(self.MAX_BUBBLE_WIDTH, available_width)
            self.content_box.setMaximumWidth(content_width)
            # 用 QTextDocument 的实际排版结果计算高度。左右 10px、上下 6px
            # 与下面的样式表保持一致，因此底部只保留明确的 6px 内边距。
            if self.content_box.isVisible():
                document = self.content_box.document()
                document.setDocumentMargin(0)
                document.setTextWidth(max(1, content_width - 23))
                text_height = int(document.size().height() + 0.999) + 12
                self.content_box.setFixedHeight(max(12, text_height))
        layout = self.layout()
        if layout is not None:
            # 同样先释放容器自身旧的固定高度，再采用当前文本尺寸。
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            layout.activate()
            # QWidget 的旧高度不会因子 QLabel 变矮而自动回收；明确采用布局的
            # 最新 sizeHint，确保思考文字下面不保留首次窄栏换行时的空白。
            self.setFixedHeight(layout.sizeHint().height())
        self.content_box.updateGeometry()
        self.updateGeometry()

        parent = self.parentWidget()
        if parent is not None:
            parent.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 面板缩放或首次插入聊天布局后，按新宽度重新计算 QLabel 的换行高度。
        # 延后到当前布局周期结束，确保读取到的是实际分配宽度。
        self._layout_update_timer.start()

    def append_text(self, text: str):
        if not text:
            return

        was_empty = not self._reasoning_parts
        self._reasoning_parts.append(text)
        self._reasoning_char_count += len(text)
        # 服务端常在 reasoning_summary 末尾附带 \n\n。原样保存以便审计，
        # 但显示这些尾随空行只会制造无意义的“思考区底部空白”。
        candidate = self._reasoning_trailing_text + text
        visible_delta = candidate.rstrip()
        self._reasoning_trailing_text = candidate[len(visible_delta):]
        if visible_delta:
            cursor = self.content_box.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(visible_delta)
            self.content_box.setTextCursor(cursor)
        self.update_title()

        if was_empty:
            self.setVisible(True)
            self.set_expanded(True)

        self._layout_update_timer.start()


class ChatImageLabel(QLabel):
    """聊天图片控件：图片只在内存显示，右键可复制或另存为。"""

    THUMBNAIL_SIZE = 180

    def __init__(
        self,
        pixmap: QPixmap,
        parent=None,
        thumbnail_size: int | None = None,
        image_data_url: str = "",
        on_set_as_reference: Callable[[str, QPixmap], None] | None = None,
    ):
        super().__init__(parent)

        self.original_pixmap = pixmap
        self.image_data_url = image_data_url or ""
        self.on_set_as_reference = on_set_as_reference

        # 用户随消息发送的图片适合小型附件缩略图；
        # Generated images use the larger default thumbnail size of 180px.
        self.thumbnail_size = int(thumbnail_size or self.THUMBNAIL_SIZE)

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

        preview_pixmap = pixmap.scaled(
            self.thumbnail_size,
            self.thumbnail_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(preview_pixmap)
        self.setFixedSize(self.thumbnail_size + 14, self.thumbnail_size + 14)
        self.setToolTip("点击预览；右键可复制或另存图片")

        self.setStyleSheet("""
            QLabel {
                background-color: #FFFFFF;
                border: 1px solid #D7D7D2;
                border-radius: 10px;
                padding: 6px;
            }
            QLabel:hover {
                background-color: #F0F0EE;
            }
        """)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            dialog = ImagePreviewDialog(self.original_pixmap, self)
            dialog.exec()
            event.accept()
            return

        super().mousePressEvent(event)

    def show_context_menu(self, pos):
        """图片右键菜单。"""
        menu = QMenu(self)

        # 图片右键菜单也使用程序统一的白底 + 金色边框风格，避免系统暗色菜单突兀。
        menu.setStyleSheet("""
            QMenu {
                background-color: #FFFFFF;
                color: #111111;
                border: 1px solid #D7D7D2;
                border-radius: 10px;
                padding: 6px;
                font-size: 13px;
            }
            QMenu::item {
                background-color: transparent;
                color: #111111;
                padding: 7px 22px 7px 12px;
                border-radius: 6px;
                min-width: 120px;
            }
            QMenu::item:selected {
                background-color: #111111;
                color: #FFFFFF;
            }
            QMenu::item:disabled {
                color: #BDBDBD;
                background-color: transparent;
            }
        """)

        copy_action = QAction("复制图片", self)
        save_action = QAction("图片另存为...", self)

        menu.addAction(copy_action)
        menu.addAction(save_action)

        copy_action.triggered.connect(self.copy_image)
        save_action.triggered.connect(self.save_image_as)

        menu.exec(self.mapToGlobal(pos))

    def copy_image(self):
        """复制图片到剪贴板，不产生本地文件。"""
        QApplication.clipboard().setPixmap(self.original_pixmap)

    def save_image_as(self):
        """用户主动选择保存位置；只有此操作才会写入本地磁盘。"""
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "图片另存为",
            "generated_image.png",
            "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg);;WebP 图片 (*.webp);;BMP 图片 (*.bmp)"
        )

        if not file_path:
            return

        self.original_pixmap.save(file_path)

    def set_as_reference_image(self):
        if not self.image_data_url or not callable(self.on_set_as_reference):
            return

        self.on_set_as_reference(self.image_data_url, self.original_pixmap)


class ImagePreviewDialog(QDialog):
    """
    聊天图片无边界预览。

    交互：
    1. 单击图片预览区域退出。
    2. 按住鼠标左键拖拽可平移图片。
    3. 鼠标滚轮可缩放图片，并以鼠标所在位置为缩放中心。
    4. Esc 退出。
    """

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )

        self.original_pixmap = pixmap
        self.setModal(False)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # 缩放状态。
        # scale_factor 表示相对原图尺寸的缩放倍数。
        # Drag the borderless image window itself so the image can move freely
        # across the screen.
        self.scale_factor = 1.0
        self.min_scale_factor = 0.05
        self.max_scale_factor = 12.0
        self._scaled_pixmap_cache: dict[tuple[int, int, bool], QPixmap] = {}
        self._smooth_render_pending = False

        self._smooth_render_timer = QTimer(self)
        self._smooth_render_timer.setSingleShot(True)
        self._smooth_render_timer.timeout.connect(self.render_smooth_scaled_pixmap)

        # 拖拽状态。press 后如果移动距离很小，则 release 时视为“单击退出”。
        self.dragging = False
        self.drag_moved = False
        self.drag_start_pos = QPoint(0, 0)
        self.drag_start_global_pos = QPoint(0, 0)
        self.drag_start_window_pos = QPoint(0, 0)

        # 不使用布局管理 image_label，避免拖拽移动时被布局重新拉回原位。
        self.image_label = QLabel(self)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("""
            QLabel {
                background-color: transparent;
                border: none;
                padding: 0px;
            }
        """)

        # 透明外层可接收滚轮和拖拽；图片控件只负责显示像素。
        # 这里必须保持完全透明，不能设置黑色/半透明背景；
        # 否则无边框窗口边缘在部分系统主题下会出现竖线阴影或色块。
        self.setStyleSheet("QDialog { background: transparent; border: none; }")
        self.setStyleSheet("QDialog { background: transparent; border: none; }")

        self.update_image_for_screen()

    def update_image_for_screen(self):
        """
        初始化图片预览窗口。

        关键调整：
        1. 预览窗口尺寸等于当前图片显示尺寸，不再创建占屏幕 92% 的大窗口。
        2. 拖拽时移动整个无边框窗口，因此图片可以在屏幕上任意移动。
        3. 窗口背景保持透明，消除大窗口边缘可能出现的竖线阴影。
        """
        if self.original_pixmap.isNull():
            self.close()
            return

        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QApplication.primaryScreen().availableGeometry()

        max_width = max(120, int(available.width() * 0.92))
        max_height = max(120, int(available.height() * 0.92))

        fit_scale = min(
            max_width / max(1, self.original_pixmap.width()),
            max_height / max(1, self.original_pixmap.height()),
            1.0,
        )

        self.scale_factor = max(self.min_scale_factor, fit_scale)
        self.render_scaled_pixmap()

        # 窗口大小严格等于图片显示大小，避免透明窗口的边界覆盖主界面形成阴影线。
        self.setFixedSize(self.image_label.size())
        self.image_label.move(0, 0)

        # 图片预览默认显示在当前屏幕正中心，而不是鼠标附近。
        # 这样无论从聊天区哪个位置点击图片，弹出的预览图都能稳定居中显示。
        center_pos = available.center() - QPoint(self.width() // 2, self.height() // 2)
        self.move(center_pos)

    def current_scaled_size(self) -> QSize:
        """根据当前缩放倍数计算图片显示尺寸。"""
        return QSize(
            max(1, int(self.original_pixmap.width() * self.scale_factor)),
            max(1, int(self.original_pixmap.height() * self.scale_factor)),
        )

    def render_scaled_pixmap(self, smooth: bool = True):
        """按当前缩放倍数重绘图片。"""
        target_size = self.current_scaled_size()
        cache_key = (target_size.width(), target_size.height(), bool(smooth))

        scaled_pixmap = self._scaled_pixmap_cache.get(cache_key)
        if scaled_pixmap is None:
            transform_mode = (
                Qt.TransformationMode.SmoothTransformation
                if smooth
                else Qt.TransformationMode.FastTransformation
            )
            scaled_pixmap = self.original_pixmap.scaled(
                target_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                transform_mode,
            )
            self._scaled_pixmap_cache[cache_key] = scaled_pixmap

            # 缩放时尺寸很多，限制缓存体积，避免大图预览长期占用过多内存。
            if len(self._scaled_pixmap_cache) > 10:
                for old_key in list(self._scaled_pixmap_cache.keys())[:4]:
                    self._scaled_pixmap_cache.pop(old_key, None)

        self.image_label.setPixmap(scaled_pixmap)
        self.image_label.setFixedSize(scaled_pixmap.size())

    def render_smooth_scaled_pixmap(self):
        """滚轮停顿后补一次高质量重绘，滚动过程中先用快速缩放保持跟手。"""
        self._smooth_render_pending = False
        self.render_scaled_pixmap(smooth=True)

    def clamp_image_position(self):
        """Retained as a no-op for callers that still invoke this hook."""
        return

    def mousePressEvent(self, event):
        """
        鼠标左键按下时进入可能拖拽状态。

        注意：
        这里不能立即关闭窗口，因为还需要支持“左键拖拽平移”。
        是否关闭要等 mouseReleaseEvent 判断是否发生明显移动。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.drag_moved = False
            self.drag_start_pos = event.position().toPoint()
            self.drag_start_global_pos = event.globalPosition().toPoint()
            self.drag_start_window_pos = self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """左键拖拽整个图片窗口，使图片可在屏幕上任意移动。"""
        if self.dragging:
            current_global_pos = event.globalPosition().toPoint()
            delta = current_global_pos - self.drag_start_global_pos

            if abs(delta.x()) > 3 or abs(delta.y()) > 3:
                self.drag_moved = True

            # 直接移动无边框预览窗口本身，而不是在窗口内部移动图片控件。
            # Moving the window keeps dragging independent of the preview bounds.
            self.move(self.drag_start_window_pos + delta)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """
        鼠标左键释放。

        1. 如果没有发生明显拖拽，则视为单击，关闭预览。
        2. 如果已经拖拽过，则只结束拖拽，不关闭窗口。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            was_dragging = self.dragging
            moved = self.drag_moved
            self.dragging = False
            self.drag_moved = False

            if was_dragging and not moved:
                self.close()

            event.accept()
            return

        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        """
        鼠标滚轮缩放图片，并以鼠标位置为缩放中心。

        新实现直接缩放整个图片窗口：
        1. 缩放前记录鼠标在图片窗口中的相对坐标比例。
        2. 缩放后调整窗口左上角，使鼠标仍指向图片中的同一位置。
        3. 窗口背景透明且尺寸等于图片尺寸，不会产生多余竖线阴影。
        """
        if self.original_pixmap.isNull():
            event.accept()
            return

        angle_delta = event.angleDelta().y()

        if angle_delta == 0:
            event.accept()
            return

        old_scale = self.scale_factor
        old_width = max(1, self.image_label.width())
        old_height = max(1, self.image_label.height())

        mouse_local_pos = event.position().toPoint()
        mouse_global_pos = event.globalPosition().toPoint()

        # 鼠标在当前图片中的相对比例，用于保持“以鼠标为中心”缩放。
        relative_x = mouse_local_pos.x() / old_width
        relative_y = mouse_local_pos.y() / old_height

        zoom_step = 1.15 if angle_delta > 0 else 1 / 1.15
        self.scale_factor = max(
            self.min_scale_factor,
            min(self.max_scale_factor, self.scale_factor * zoom_step),
        )

        if abs(self.scale_factor - old_scale) < 1e-6:
            event.accept()
            return

        target_size = self.current_scaled_size()

        new_width = max(1, target_size.width())
        new_height = max(1, target_size.height())

        # 窗口大小始终等于图片大小，避免透明大窗口遮挡主界面或形成边缘阴影。
        self.setUpdatesEnabled(False)
        self.image_label.setFixedSize(target_size)
        self.setFixedSize(target_size)
        self.image_label.move(0, 0)

        new_x = int(mouse_global_pos.x() - relative_x * new_width)
        new_y = int(mouse_global_pos.y() - relative_y * new_height)
        self.move(QPoint(new_x, new_y))
        self.setUpdatesEnabled(True)

        self.render_scaled_pixmap(smooth=False)
        self._smooth_render_pending = True
        self._smooth_render_timer.start(70)

        # 如果缩放发生在拖拽过程中，同步拖拽基准，避免下一次 mouseMove 出现跳动。
        if self.dragging:
            self.drag_start_global_pos = mouse_global_pos
            self.drag_start_window_pos = self.pos()

        event.accept()

    def keyPressEvent(self, event):
        """Esc 退出；其他按键交给 Qt 默认处理。"""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return

        super().keyPressEvent(event)


class _CopyHintPopup(QWidget):
    """双击复制后的提示气泡：使用当前界面金色边框风格，避免系统 ToolTip 黑边。"""

    def __init__(self, text: str, parent=None):
        # Use a borderless tool window so platform themes do not add an unwanted
        # tooltip frame or shadow.
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Keep the outer widget transparent; the label supplies the white surface
        # and accent border.
        self.setStyleSheet("QWidget { background: transparent; border: none; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.label = QLabel(text, self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setStyleSheet("""
            QLabel {
                color: #212121;
                background-color: #FFFFFF;
                font-size: 14px;
                font-weight: bold;
                padding: 8px 14px;
                border-radius: 8px;
                border: 1px solid #D4AF37;
            }
        """)

        layout.addWidget(self.label)
        self.adjustSize()

        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self.close)

    def show_at(self, global_pos: QPoint):
        """在鼠标附近显示提示气泡，并自动消失。"""
        self.adjustSize()
        self.move(global_pos + QPoint(18, 20))
        self.show()
        self.raise_()
        self._auto_close_timer.start(1400)


class ChatTextBubble(QTextBrowser):
    """
    聊天气泡控件。

    设计目标：
    1. 保存原始文本 raw_text，显示时可在 Markdown 渲染和原文之间切换。
    2. 双击气泡时复制原始文本，而不是复制渲染后的富文本。
    3. 禁用内部滚动条，让气泡按内容自动撑开，由外层聊天滚动区统一滚动。
    """

    MAX_BUBBLE_WIDTH = 680
    # 完整 Markdown / MathJax 渲染仍使用 WebEngine。流式期间临时采用纯文本，
    # 仅用于避免逐 token 重载页面；回复结束后必须恢复这一原始路径。
    USE_WEBENGINE_RENDER = True
    # 右键菜单信号：
    # 1. object 参数传出当前气泡对象，主窗口据此读取绑定的 message_index。
    # 2. Qt 信号必须定义为类属性。
    edit_requested = Signal(object)
    apply_requested = Signal(object)
    delete_turn_requested = Signal(object)
    resend_requested = Signal(object)
    rich_render_requested = Signal(object)

    @staticmethod
    def apply_bold_accent(html_text: str) -> str:
        """Give Markdown strong text a consistent accent in the Qt fallback."""
        return re.sub(
            r'(<span\s+style="[^"]*font-weight:(?:[6-9]00|bold);)([^"]*">)',
            rf'\1 color:{CHAT_BOLD_ACCENT};\2',
            html_text,
            flags=re.I,
        )

    def __init__(self, text: str, role: str, render_markdown: bool = False, parent=None):
        super().__init__(parent)

        self._raw_text_parts = [text] if text else []
        self.role = role
        self.render_markdown = bool(render_markdown and role == "assistant")

        # Long messages are folded without changing the stored text.
        # 2. 每个文本气泡超过 15 行时，默认只显示前 15 行。
        # 3. 用户点击折叠气泡后完整展开，raw_text 始终保留全文。
        self.max_collapsed_lines = 15
        self.is_expanded = False
        self.is_expandable = False

        # 当前气泡对应 self.messages 中的下标。
        # 系统提示、临时等待提示、尚未完成的流式气泡可能没有有效下标。
        self.message_index = None
        self.web_view = None
        self._diagram_viewers = []
        # Chromium pages are expensive. Historical replies use the existing
        # lightweight Markdown/formula renderer until selected by the user.
        self.web_render_active = False
        self._web_content_height = 0
        # 流式阶段先使用轻量的 QTextBrowser 文本预览。若每个 SSE 片段都调用
        # QWebEngineView.setHtml()，WebEngine 会反复加载页面、计算 MathJax 高度，
        # 外层 QScrollArea 也会随之连续重排，表现为聊天窗口剧烈抖动。
        self.is_streaming = False
        self._stream_geometry_timer = QTimer(self)
        self._stream_geometry_timer.setSingleShot(True)
        self._stream_geometry_timer.setInterval(120)
        self._stream_geometry_timer.timeout.connect(self.adjust_to_content)

        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)

        # Wrap to the widget width and let the outer chat area handle scrolling.
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Expanding 能让 QTextBrowser 在气泡布局中真正吃到可用宽度，
        # 避免 Preferred 策略导致气泡被压成很窄的一列。
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.document().setDocumentMargin(0)
        self.document().setDefaultStyleSheet("body { background: transparent; }")

        # 开启气泡右键菜单。只对真实 user / assistant 消息提供编辑和删除；
        # system 气泡只是运行提示，不参与对话轮次编辑。
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

        self.refresh_content()

    @property
    def raw_text(self) -> str:
        return "".join(self._raw_text_parts)

    @raw_text.setter
    def raw_text(self, text: str) -> None:
        self._raw_text_parts = [text] if text else []

    def has_exact_raw_text(self, text: str) -> bool:
        return len(self._raw_text_parts) == 1 and self._raw_text_parts[0] == text

    def activate_web_render(self, refresh: bool = True):
        """Enable reader-grade rendering for this bubble on demand."""
        if not (self.render_markdown and self.USE_WEBENGINE_RENDER and CHAT_WEBENGINE_AVAILABLE):
            return
        self.web_render_active = True
        if refresh:
            self.refresh_content()

    def release_web_render(self):
        """Release Chromium resources while retaining the exact raw reply."""
        was_active = self.web_render_active or self.web_view is not None
        self.web_render_active = False
        web_view = self.web_view
        self.web_view = None
        self.web_channel = None
        self.web_bridge = None
        if web_view is not None:
            try:
                web_view.hide()
                web_view.setParent(None)
                web_view.deleteLater()
            except RuntimeError:
                pass
        if was_active and not self.is_streaming:
            self.refresh_content()

    def ensure_web_view(self):
        """Create the embedded reader-grade HTML surface when it is needed."""
        if self.web_view is not None or not CHAT_WEBENGINE_AVAILABLE:
            return
        self.web_view = _ChatWebView(self, self)
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.web_view.page().setBackgroundColor(QColor(COLOR_BG_SURFACE_2))
        self.web_channel = QWebChannel(self.web_view.page())
        self.web_bridge = _ChatWebBridge(self, self.web_view)
        self.web_channel.registerObject("chatBubbleBridge", self.web_bridge)
        self.web_view.page().setWebChannel(self.web_channel)
        self.web_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.web_view.customContextMenuRequested.connect(
            lambda pos: self.show_context_menu(self.web_view.mapTo(self, pos))
        )
        self.web_view.loadFinished.connect(self.on_web_content_loaded)
        self.web_view.show()

    def open_diagram_viewer(self):
        """Open this response's validated diagram on an independent canvas."""
        try:
            from AI_diagrams import is_valid_diagram_response
            if not is_valid_diagram_response(self.raw_text) or not CHAT_WEBENGINE_AVAILABLE:
                return
            viewer = DiagramViewerDialog(
                self.raw_text,
                self.window(),
                locate_callback=getattr(self, "diagram_evidence_callback", None),
                ask_callback=getattr(self, "diagram_ask_callback", None),
                document_path=str(getattr(self, "diagram_document_path", "") or ""),
            )
            self._diagram_viewers.append(viewer)

            def forget(_=None):
                if viewer in self._diagram_viewers:
                    self._diagram_viewers.remove(viewer)

            viewer.destroyed.connect(forget)
            viewer.show()
            viewer.raise_()
            viewer.activateWindow()
        except Exception:
            return

    def expand_from_web_click(self) -> bool:
        """Expand a folded reply when its WebEngine surface is clicked."""
        if not self.is_expandable or self.is_expanded:
            return False
        scroll_bar = self.outer_scroll_bar()
        scroll_value = scroll_bar.value() if scroll_bar is not None else None
        self.is_expanded = True
        self.refresh_content()
        self.restore_outer_scroll_position(scroll_bar, scroll_value)
        return True

    def outer_scroll_bar(self):
        """Find the chat's owning scroll area without coupling this widget to ChatWindow."""
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, QScrollArea):
                return widget.verticalScrollBar()
            widget = widget.parentWidget()
        return None

    @staticmethod
    def restore_outer_scroll_position(scroll_bar, value):
        """Undo WebEngine's occasional reset of the surrounding chat viewport."""
        if scroll_bar is None or value is None:
            return

        def restore_if_reset():
            try:
                # Do not override deliberate scrolling by the user; only repair
                # the erroneous jump to the beginning caused by page relayout.
                if value > 2 and scroll_bar.value() <= 2:
                    scroll_bar.setValue(min(value, scroll_bar.maximum()))
            except RuntimeError:
                pass

        restore_if_reset()
        QTimer.singleShot(0, restore_if_reset)
        QTimer.singleShot(400, restore_if_reset)
        QTimer.singleShot(1100, restore_if_reset)

    def current_display_text(self) -> str:
        """返回当前应该显示在气泡中的文本；长文本默认折叠为前 15 行。"""
        # 正在生成时必须持续展示完整增量内容；折叠和 Markdown/公式渲染留到
        # 完成后一次性处理，避免一边输出一边反复切换气泡高度。
        if self.is_streaming:
            self.is_expandable = False
            return self.raw_text

        # A pretty-printed V2 diagram is normally more than fifteen lines.
        # Folding its JSON before WebEngine sees it invalidates the protocol and
        # leaves the user with a partial response instead of a map.
        try:
            from AI_diagrams import is_valid_diagram_response
            if is_valid_diagram_response(self.raw_text):
                self.is_expandable = False
                return self.raw_text
        except Exception:
            pass

        lines = self.raw_text.splitlines()

        # 空文本或 15 行以内直接完整显示。
        if len(lines) <= self.max_collapsed_lines:
            self.is_expandable = False
            return self.raw_text

        self.is_expandable = True

        if self.is_expanded:
            return self.raw_text

        # 只折叠界面显示，不修改 raw_text，也不影响右键编辑、双击复制和发送上下文。
        visible_text = "\n".join(lines[:self.max_collapsed_lines])
        return visible_text + "\n\n……（内容已折叠，点击气泡展开全文）"

    def show_context_menu(self, pos):
        """
        聊天气泡右键菜单。

        菜单项：
        1. 修改该对话：修改当前气泡绑定的 self.messages 消息内容。
        2. 删除该轮对话：删除当前气泡所在的一轮 user + assistant 回复。
        """
        if self.role not in ("user", "assistant"):
            return

        menu = QMenu(self)

        # 右键菜单使用白底 + 金色边框，避免跟随系统暗色主题显示成黑块。
        menu.setStyleSheet("""
            QMenu {
                background-color: #FFFFFF;
                color: #212121;
                border: 1px solid #D4AF37;
                border-radius: 6px;
                padding: 4px;
                font-size: 13px;
            }
            QMenu::item {
                background-color: transparent;
                color: #212121;
                padding: 7px 22px 7px 12px;
                border-radius: 4px;
                min-width: 120px;
            }
            QMenu::item:selected {
                background-color: #FFF8E1;
                color: #212121;
            }
            QMenu::item:disabled {
                color: #BDBDBD;
                background-color: transparent;
            }
            QMenu::separator {
                height: 1px;
                background-color: #E0E0E0;
                margin: 4px 6px;
            }
        """)

        apply_action = None
        if self.role == "assistant":
            # Put “copy full reply” first in the context menu for quick access.
            apply_action = menu.addAction("复制全文")

        edit_action = menu.addAction("修改该对话")

        resend_action = None
        if self.role == "user":
            resend_action = menu.addAction("重新发送该消息")

        fold_action = menu.addAction("折叠气泡" if self.is_expanded else "展开气泡")
        delete_turn_action = menu.addAction("删除该轮对话")

        if not self.is_expandable:
            fold_action.setEnabled(False)

        # 尚未绑定到 self.messages 的临时气泡不允许编辑/删除/重发，避免误操作。
        if self.message_index is None:
            edit_action.setEnabled(False)
            delete_turn_action.setEnabled(False)
            if resend_action is not None:
                resend_action.setEnabled(False)

        # 没有原文内容时不允许应用，避免误触发外部剪贴板按钮。
        if apply_action is not None and not self.raw_text.strip():
            apply_action.setEnabled(False)

        action = menu.exec(self.mapToGlobal(pos))

        if action == edit_action:
            self.edit_requested.emit(self)
        elif apply_action is not None and action == apply_action:
            self.apply_requested.emit(self)
        elif resend_action is not None and action == resend_action:
            self.resend_requested.emit(self)
        elif action == fold_action:
            self.is_expanded = not self.is_expanded
            self.refresh_content()
        elif action == delete_turn_action:
            self.delete_turn_requested.emit(self)

    def mouseDoubleClickEvent(self, event):
        """
        双击复制原始内容，避免复制到 Markdown 渲染后的富文本。

        这里改为自定义提示气泡：
        1. 避免系统默认 QToolTip 在部分主题下显示成黑块。
        2. 提示文字更大、更醒目，用户更容易注意到复制成功。
        """
        self.copy_raw_text()
        event.accept()

    def copy_raw_text(self):
        """Copy the original response for both Qt and WebEngine double-clicks."""
        QApplication.clipboard().setText(self.raw_text)

        # 每次双击都创建一个新的提示气泡，显示在鼠标附近。
        # Use the custom popup so platform tooltip themes do not change its appearance.
        try:
            if hasattr(self, "_copy_hint_popup") and self._copy_hint_popup is not None:
                self._copy_hint_popup.close()
        except Exception:
            pass

        self._copy_hint_popup = _CopyHintPopup("✓ 已复制原文到剪贴板", self)
        self._copy_hint_popup.destroyed.connect(
            lambda _=None: setattr(self, "_copy_hint_popup", None)
        )
        self._copy_hint_popup.show_at(QCursor.pos())

    def mouseReleaseEvent(self, event):
        """点击折叠的长气泡时展开全文；不改变原始消息内容。"""
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.is_expandable
            and not self.is_expanded
        ):
            self.is_expanded = True
            self.refresh_content()
            if self.role == "assistant" and self.render_markdown:
                self.rich_render_requested.emit(self)
            event.accept()
            return

        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.role == "assistant"
            and self.render_markdown
            and not self.web_render_active
        ):
            self.rich_render_requested.emit(self)

        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.adjust_to_content()

    def wheelEvent(self, event):
        """
        禁止气泡内部滚动。

        原因：
        QTextBrowser 即使隐藏滚动条，只要内容高度与控件高度存在 1~2 像素误差，
        鼠标滚轮仍可能被 QTextBrowser 自己消费，造成用户气泡内容“轻微滚动”的观感。
        这里直接忽略滚轮事件，让外层聊天滚动区统一接管滚动。
        """
        event.ignore()

    def set_raw_text(self, text: str):
        self.raw_text = text or ""
        self.refresh_content()

    def append_raw_text(self, text: str):
        if not text:
            return
        self._raw_text_parts.append(text)
        if self.is_streaming:
            # Streaming content is plain text until completion.  Appending via
            # QTextCursor avoids rebuilding and laying out the complete
            # QTextDocument for every 80 ms batch.  The final set_streaming(False)
            # still performs the original one-shot Markdown/MathJax render.
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text)
            self.setTextCursor(cursor)
            self._stream_geometry_timer.start()
            return
        self.refresh_content()

    def set_streaming(self, streaming: bool):
        """切换流式预览模式；结束时才进行一次完整 Markdown/WebEngine 渲染。"""
        streaming = bool(streaming)
        if self.is_streaming == streaming:
            return
        self.is_streaming = streaming
        self.refresh_content()

    def set_render_markdown(self, render_markdown: bool):
        """切换 Markdown 渲染状态；不改变 raw_text。"""
        self.render_markdown = bool(render_markdown and self.role == "assistant")
        self.refresh_content()

    def reader_grade_html(self, markdown: str) -> str:
        """Build a model-reply page with the same MathJax runtime as the reader."""
        diagram_html = None
        try:
            from AI_diagrams import render_diagram_html
            diagram_html = render_diagram_html(markdown)
        except Exception:
            # Invalid model output must remain readable as its original text.
            diagram_html = None
        protected_math: dict[str, str] = {}

        def protect_math(match) -> str:
            token = f"MATHJAXPROTECTED{len(protected_math)}TOKEN"
            protected_math[token] = match.group(0)
            return token

        # Qt's Markdown parser treats TeX delimiters such as \\( and \\[
        # as escapes. Hide math while it builds the surrounding Markdown HTML,
        # then restore it for MathJax unchanged.
        if diagram_html:
            html_text = f"<html><head></head><body>{diagram_html}</body></html>"
        else:
            prepared = re.sub(r"\\\[(.+?)\\\]", protect_math, markdown, flags=re.S)
            prepared = re.sub(r"\\\((.+?)\\\)", protect_math, prepared, flags=re.S)
            prepared = re.sub(r"\$\$(.+?)\$\$", protect_math, prepared, flags=re.S)
            prepared = re.sub(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$", protect_math, prepared, flags=re.S)

            scratch = QTextDocument(self)
            scratch.setMarkdown(prepared)
            html_text = scratch.toHtml()
            for token, tex_source in protected_math.items():
                html_text = html_text.replace(token, html.escape(tex_source))
        try:
            from PB_layout import mathjax_script_html

            mathjax = mathjax_script_html()
        except Exception:
            mathjax = ""
        chat_css = f"""
        <style>
          html, body {{ margin: 0; padding: 0; height: auto; min-height: 0; overflow: hidden;
            background: {COLOR_BG_SURFACE_2}; color: {COLOR_TEXT_PRIMARY}; }}
          body {{ font: 14px/1.65 {APP_SERIF_FONT_FAMILY_STACK}; overflow-wrap: anywhere; }}
          strong, b, span[style*="font-weight:700"], span[style*="font-weight: 700"] {{
            color: {CHAT_BOLD_ACCENT};
          }}
          p {{ margin: 0 0 0.7em; }}
          h1, h2, h3, h4 {{ margin: 0.45em 0; line-height: 1.35; }}
          ul, ol {{ margin: 0.35em 0; padding-left: 1.5em; }}
          pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
          img {{ max-width: 100%; height: auto; }}
          .litmtrans-diagram-wrap {{ padding: 3px 0 8px; }}
          .litmtrans-diagram-wrap h3 {{ color: {CHAT_BOLD_ACCENT}; margin:0 0 10px; }}
          .litmtrans-diagram-preview {{ overflow:hidden; padding:12px; border:1px solid #d8e0e5;
            border-radius:9px; background:#fff; cursor:pointer; outline:none; }}
          .litmtrans-diagram-preview:hover {{ border-color:#7890a2; background:#fbfcfd; }}
          .litmtrans-diagram-preview:focus-visible {{ box-shadow:0 0 0 2px #91afc0; }}
          .litmtrans-diagram {{ display:block; width:100%; height:auto; max-width:100%; }}
          .litmtrans-diagram .edges path {{ fill: none; stroke: #738795; stroke-width: 1.55; }}
          .litmtrans-diagram text {{ font-family:"Microsoft YaHei UI", sans-serif; dominant-baseline:middle; }}
          .litmtrans-diagram .node-title {{ fill:#102a3a; font-weight:700; }}
          .litmtrans-diagram .node-detail {{ fill:#304b5b; font-weight:500; }}
          .litmtrans-diagram .edge-label {{ fill:#52616a; font-size:10.5px; font-weight:500; dominant-baseline:auto;
            paint-order:stroke; stroke:#f7f9fa; stroke-width:4px; stroke-linejoin:round; }}
          .mindmap-node rect, .flow-node > rect, .flow-node > path:not(.subprocess-lines) {{ fill:#fff; stroke:#7890a2; stroke-width:1.3; }}
          .mindmap-node.root rect, .flow-node.type-terminator > rect {{ fill:#1e3347; stroke:#1e3347; }}
          .mindmap-node.root text, .flow-node.type-terminator text {{ fill:#fff; }}
          .mindmap-node.root .node-title, .mindmap-node.root .node-detail,
          .flow-node.type-terminator .node-title, .flow-node.type-terminator .node-detail {{ fill:#fff; }}
          .mindmap-node.branch rect {{ fill:#e9f1f5; stroke:#5c8399; }}
          .mindmap-node[data-kind="result"] rect, .mindmap-node[data-kind="validation"] rect {{ fill:#eaf4ef; stroke:#659078; }}
          .mindmap-node[data-kind="gap"] rect, .mindmap-node[data-kind="limitation"] rect {{ fill:#fff5e6; stroke:#ad8247; }}
          .flow-node.type-decision > path {{ fill:#fff6e4; stroke:#a77d42; }}
          .flow-node.type-database > path {{ fill:#edf5ef; stroke:#668873; }}
          .flow-node.type-io > path, .flow-node.type-document > path {{ fill:#edf4f7; stroke:#66869a; }}
          .flow-node.role-conclusion > rect, .flow-node.role-conclusion > path:not(.subprocess-lines) {{ fill:#1e3347; stroke:#1e3347; stroke-width:1.45; }}
          .flow-node.role-conclusion .node-title {{ fill:#fff; }}
          .flow-node.role-conclusion .node-detail {{ fill:#e8f0f3; }}
          .subprocess-lines {{ fill:none; stroke:#7890a2; stroke-width:1; }}
          #chat-content {{ display: flow-root; width: 100%; height: auto; min-height: 0; }}
          mjx-container[display="true"] {{ overflow-x: auto; overflow-y: hidden; max-width: 100%; }}
        </style>
        <script>
          window.addEventListener('DOMContentLoaded', () => {{
            new QWebChannel(qt.webChannelTransport, (channel) => {{
              const bridge = channel.objects.chatBubbleBridge;
               document.addEventListener('click', event => {{
                 if (event.target.closest('.litmtrans-diagram-preview')) {{
                   event.preventDefault(); event.stopPropagation(); bridge.gesture('diagram'); return;
                 }}
                 bridge.gesture('expand');
               }});
               document.addEventListener('keydown', event => {{
                 if ((event.key === 'Enter' || event.key === ' ') && event.target.closest('.litmtrans-diagram-preview')) {{
                   event.preventDefault(); bridge.gesture('diagram');
                 }}
               }});
              document.addEventListener('dblclick', (event) => {{
                event.preventDefault();
                bridge.gesture('copy');
              }});
            }});
          }});
          window.addEventListener('load', () => {{
            if (window.MathJax && window.MathJax.typesetPromise) window.MathJax.typesetPromise();
          }});
        </script>
        <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
        """
        # Measure the message container rather than document/body scrollHeight;
        # the latter can include the old viewport and overstate folded replies.
        html_text = re.sub(
            r"(<body\b[^>]*>)",
            r'\1<div id="chat-content">',
            html_text,
            count=1,
            flags=re.I,
        )
        html_text = re.sub(r"</body>", "</div></body>", html_text, count=1, flags=re.I)
        return html_text.replace("</head>", f"{chat_css}{mathjax}</head>", 1)

    def on_web_content_loaded(self, ok: bool):
        if not ok or self.web_view is None:
            return
        self.update_web_content_height()
        QTimer.singleShot(350, self.update_web_content_height)
        QTimer.singleShot(1000, self.update_web_content_height)

    def update_web_content_height(self):
        if self.web_view is None or not self.web_view.isVisible():
            return

        def apply_height(value):
            try:
                height = int(float(value or 0))
            except (TypeError, ValueError):
                return
            if height > 0 and height != self._web_content_height:
                self._web_content_height = height
                self.adjust_to_content()

        try:
            self.web_view.page().runJavaScript(
                "(() => { const node = document.getElementById('chat-content'); "
                "if (!node) return 1; return Math.max(1, Math.ceil(node.getBoundingClientRect().height)); })()",
                apply_height,
            )
        except RuntimeError:
            pass

    @classmethod
    def render_math_image(cls, tex: str, display: bool) -> tuple[str, int, int] | None:
        """Render one TeX fragment to a cached transparent PNG.

        Qt's native Markdown renderer deliberately does not implement TeX math.
        Keeping this conversion local makes chat formulas available without a
        network connection or a WebEngine view for every individual message.
        """
        tex = str(tex or "").strip()
        if not tex:
            return None

        key = hashlib.sha256(f"{display}\0{tex}".encode("utf-8")).hexdigest()
        cache_path = cls._MATH_CACHE_DIR / f"{key}.png"
        try:
            cls._MATH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            if not cache_path.exists():
                # matplotlib.mathtext covers the standard scientific TeX used
                # in AI answers (fractions, Greek letters, subscripts, etc.).
                # Import lazily so non-rendered/system bubbles start instantly.
                from matplotlib import mathtext

                mathtext.math_to_image(
                    f"${tex}$",
                    str(cache_path),
                    dpi=180 if display else 150,
                    format="png",
                    color="#171717",
                )

            image = QImage(str(cache_path))
            if image.isNull():
                return None

            # mathtext writes an opaque white canvas by default. Convert that
            # canvas to transparent so formulas blend into every chat theme.
            if image.pixelColor(0, 0).rgb() == QColor("#FFFFFF").rgb():
                image = image.convertToFormat(QImage.Format.Format_ARGB32)
                for y in range(image.height()):
                    for x in range(image.width()):
                        color = image.pixelColor(x, y)
                        if color.red() == 255 and color.green() == 255 and color.blue() == 255:
                            color.setAlpha(0)
                            image.setPixelColor(x, y, color)
                image.save(str(cache_path), "PNG")

            max_width = 560 if display else 380
            width, height = image.width(), image.height()
            if width > max_width:
                height = max(1, round(height * max_width / width))
                width = max_width
            return QUrl.fromLocalFile(str(cache_path.resolve())).toString(), width, height
        except Exception:
            # A malformed formula must not hide the whole model reply.
            # It remains visible as its original Markdown/TeX source instead.
            return None

    def markdown_html_with_math(self, markdown: str) -> str:
        """Convert Markdown to Qt HTML and replace supported TeX with images."""
        formulas: dict[str, tuple[str, int, int, bool]] = {}

        def replace_math(match, display: bool) -> str:
            original = match.group(0)
            tex = match.group(1).strip()
            rendered = self.render_math_image(tex, display)
            if not rendered:
                return original
            token = f"MATHFORMULA{len(formulas)}TOKEN"
            formulas[token] = (*rendered, display)
            return token

        # Support the three TeX delimiter styles commonly emitted by models.
        prepared = re.sub(r"\\\[(.+?)\\\]", lambda m: replace_math(m, True), markdown, flags=re.S)
        prepared = re.sub(r"\\\((.+?)\\\)", lambda m: replace_math(m, False), prepared, flags=re.S)
        prepared = re.sub(r"\$\$(.+?)\$\$", lambda m: replace_math(m, True), prepared, flags=re.S)

        # Single dollar delimiters are intentionally conservative so currency
        # and ordinary prose are never converted into an image accidentally.
        def replace_inline_dollar(match) -> str:
            tex = match.group(1).strip()
            if not ("\\" in tex or re.search(r"[=_^{}]", tex)):
                return match.group(0)
            return replace_math(match, False)

        prepared = re.sub(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$", replace_inline_dollar, prepared, flags=re.S)

        scratch = QTextDocument(self)
        scratch.setMarkdown(prepared)
        rendered_html = scratch.toHtml()
        rendered_html = self.apply_bold_accent(rendered_html)
        for token, (url, width, height, display) in formulas.items():
            image_html = (
                f'<img src="{html.escape(url, quote=True)}" width="{width}" height="{height}" '
                'style="vertical-align: middle;" />'
            )
            if display:
                image_html = f'<div align="center">{image_html}</div>'
            rendered_html = rendered_html.replace(token, image_html)
        return rendered_html

    def refresh_content(self):
        """
        根据当前渲染开关刷新显示内容。

        Qt 的 Markdown 支持常见文本格式，但不支持 TeX 数学公式；
        模型回复中的 LaTeX 会先在本地转为图片，再交给 QTextBrowser 显示。
        """
        display_text = self.current_display_text()

        # 图表协议回复必须通过 WebEngine 渲染 SVG；
        # 如果回复已结束且内容包含图表协议标记，自动激活 WebEngine 渲染。
        if not self.is_streaming and self.render_markdown and not self.web_render_active:
            try:
                from AI_diagrams import is_valid_diagram_response
                if is_valid_diagram_response(self.raw_text):
                    self.web_render_active = True
            except Exception:
                pass
        if self.is_streaming:
            # 纯文本增量渲染成本远低于反复 setHtml；原始文本始终保存在 raw_text，
            # 所以结束后仍可恢复完整 Markdown、MathJax 和折叠状态。
            if self.web_view is not None:
                self.web_view.hide()
            self.setPlainText(display_text)
        elif (
            self.render_markdown
            and self.USE_WEBENGINE_RENDER
            and CHAT_WEBENGINE_AVAILABLE
            and self.web_render_active
        ):
            try:
                self.ensure_web_view()
                self._web_content_height = 0
                self.setPlainText("")
                self.web_view.show()
                self.web_view.setHtml(
                    self.reader_grade_html(display_text),
                    QUrl.fromLocalFile(str(Path(__file__).resolve().parent)),
                )
            except Exception:
                if self.web_view is not None:
                    self.web_view.hide()
                self.setPlainText(display_text)
        elif self.render_markdown:
            try:
                self.setHtml(self.markdown_html_with_math(display_text))
            except Exception:
                self.setPlainText(display_text)
        else:
            if self.web_view is not None:
                self.web_view.hide()
            self.setPlainText(display_text)

        if self.role == "system":
            # Apply alignment to the document and its blocks so system messages stay centered after redraws.
            text_option = self.document().defaultTextOption()
            text_option.setAlignment(Qt.AlignmentFlag.AlignCenter)
            text_option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            self.document().setDefaultTextOption(text_option)

            cursor = self.textCursor()
            # Qt 6 中应使用 QTextCursor.SelectionType.Document，
            # 不依赖 QTextCursor 实例是否暴露 SelectionType，避免部分环境 AttributeError。
            cursor.select(QTextCursor.SelectionType.Document)
            block_format = cursor.blockFormat()
            block_format.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cursor.mergeBlockFormat(block_format)
            cursor.clearSelection()
            self.setTextCursor(cursor)

        self.adjust_to_content()

    def adjust_to_content(self):
        """根据文档高度调整气泡高度，避免 QTextBrowser 内部出现滚动条。"""
        if self.web_view is not None and self.web_view.isVisible():
            target_width = self.width() or self.maximumWidth() or self.MAX_BUBBLE_WIDTH
            target_width = min(self.MAX_BUBBLE_WIDTH, max(120, target_width))
            content_height = max(44, self._web_content_height or 72)
            self.web_view.setGeometry(13, 10, max(1, target_width - 26), content_height)
            self.setFixedHeight(content_height + 20)
            self.updateGeometry()
            return

        # Use the current viewport width so narrow embedded panels do not clip Markdown.
        target_width = self.viewport().width() or self.width() or self.minimumWidth()

        if target_width <= 0 or target_width > self.MAX_BUBBLE_WIDTH:
            target_width = self.MAX_BUBBLE_WIDTH

        # 预留气泡左右 padding 和边框空间。
        document_width = max(120, target_width - 32)
        self.document().setTextWidth(document_width)

        document_height = int(self.document().size().height())

        if self.role == "system":
            # 系统提示气泡通常只有一行文字，使用更紧凑的高度，避免文字下方出现大块空白。
            self.setFixedHeight(max(38, document_height + 22))
        else:
            # QTextBrowser 的文档高度已包含主要行高；此前额外 +46px 会在短用户
            # 气泡下方留下明显空白。保留适度边距即可，同时避免裁剪高 DPI 文本。
            self.setFixedHeight(max(48, document_height + 28))

        self.updateGeometry()


class ReferenceQuoteLabel(QFrame):
    """用户右键询问文档时显示的引用气泡。"""

    def __init__(self, quote: dict, open_callback, clear_callback=None, parent=None):
        super().__init__(parent)
        text = str(quote.get("text") or "").strip()
        preview = re.sub(r"\s+", " ", text)
        if len(preview) > 88:
            preview = preview[:88].rstrip() + "..."

        self.quote = quote
        self.open_callback = open_callback
        self.clear_callback = clear_callback
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击打开阅读窗口并定位到引用内容")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 8, 7)
        layout.setSpacing(6)

        self.text_label = QLabel(f"“{preview}”")
        self.text_label.setWordWrap(True)
        self.text_label.setTextFormat(Qt.TextFormat.PlainText)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.text_label.setStyleSheet("border: none; background: transparent; padding: 0px;")
        layout.addWidget(self.text_label, 1)

        self.clear_button = QToolButton()
        self.clear_button.setText("X")
        self.clear_button.setToolTip("移除这条引用")
        self.clear_button.setCursor(Qt.CursorShape.ArrowCursor)
        self.clear_button.setVisible(callable(clear_callback))
        self.clear_button.clicked.connect(self.clear_quote)
        layout.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignTop)

        self.setStyleSheet("""
            QFrame {
                background-color: #F4F1EA;
                color: #3D3328;
                border: 1px solid #D8D0C3;
                border-radius: 10px;
                font-size: 13px;
            }
            QFrame:hover {
                background-color: #EEE7DC;
                border-color: #C7B9A6;
            }
            QToolButton {
                border: none;
                background: transparent;
                color: #6B5D4F;
                font-size: 12px;
                font-weight: 700;
                padding: 0px 2px;
            }
            QToolButton:hover {
                color: #1F1A16;
                background: rgba(0, 0, 0, 0.06);
                border-radius: 6px;
            }
        """)

    def clear_quote(self):
        if callable(self.clear_callback):
            self.clear_callback()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            child = self.childAt(pos)
            if child is self.clear_button:
                super().mousePressEvent(event)
                return
            self.open_callback(self.quote)
            event.accept()
            return

        super().mousePressEvent(event)


class DocumentBubbleLabel(QLabel):
    """
    已发送文档气泡。

    设计目标：
    1. 聊天窗口只显示“文档已发送”的摘要，不把 PDF 内部图片逐张刷屏。
    2. 点击气泡时打开文档阅读界面。
    3. 气泡只负责界面交互；真正发给模型的全文和图片仍由消息构造逻辑处理。
    """

    def __init__(self, text: str, document_records: list[dict], open_callback, parent=None):
        super().__init__(text, parent)
        self.document_records = document_records
        self.open_callback = open_callback

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setToolTip("点击打开文档阅读界面")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_callback(self.document_records)
            event.accept()
            return

        super().mousePressEvent(event)


class StandaloneDocumentReaderWindow(QWidget):
    def __init__(
        self,
        source_path: Path | None,
        translation_path: Path | None,
        live_translation_markdown: str = "",
        original_path: Path | None = None,
        parent=None,
    ):
        super().__init__(parent, Qt.WindowType.Window)
        self.source_path = source_path
        self.translation_path = translation_path
        self.live_translation_markdown = live_translation_markdown
        self.original_path = original_path
        self.setWindowTitle("文档阅读")
        self.resize(1200, 800)
        if parent:
            self.setStyleSheet(parent.styleSheet())
        self.setup_ui()
        self.refresh_content()

    def setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        title = QLabel("文档阅读")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        if self.original_path:
            original_label = QLabel(f"原始文件: {self.original_path}")
            original_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            original_label.setStyleSheet("color: #666666;")
            root.addWidget(original_label)

        splitter = QSplitter()
        self.source_view = QPlainTextEdit()
        self.source_view.setReadOnly(True)
        self.translation_view = QPlainTextEdit()
        self.translation_view.setReadOnly(True)
        splitter.addWidget(self.source_view)
        splitter.addWidget(self.translation_view)
        splitter.setSizes([600, 600])
        root.addWidget(splitter, 1)

    def refresh_content(self):
        self.source_view.setPlainText(self.read_text(self.source_path, "暂无原文。"))
        translation_text = self.live_translation_markdown.strip()
        if not translation_text:
            translation_text = self.read_text(self.translation_path, "暂无译文。")
        self.translation_view.setPlainText(translation_text)

    def reveal_text(self, text: str):
        """在纯文本阅读兜底窗口中定位引用内容。"""
        text = str(text or "").strip()
        if not text:
            return

        for view in (self.source_view, self.translation_view):
            cursor = view.document().find(text)
            if cursor and not cursor.isNull():
                view.setTextCursor(cursor)
                view.ensureCursorVisible()
                view.setFocus()
                return

    @staticmethod
    def read_text(path: Path | None, fallback: str) -> str:
        if not path or not path.exists():
            return fallback
        return path.read_text(encoding="utf-8", errors="replace")


def provider_card_secret_name(card_id: str) -> str:
    safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", card_id or "").strip("_")
    return safe_id or uuid.uuid4().hex


def load_provider_card_key(card_id: str) -> str:
    return app_config.load_secret(PROVIDER_CARD_SECRET_PROVIDER_ID, provider_card_secret_name(card_id))


def save_provider_card_key(card_id: str, api_key: str) -> None:
    app_config.save_secret(PROVIDER_CARD_SECRET_PROVIDER_ID, provider_card_secret_name(card_id), api_key)


def delete_provider_card_key(card_id: str) -> None:
    app_config.delete_secret(PROVIDER_CARD_SECRET_PROVIDER_ID, provider_card_secret_name(card_id))


class ProviderCardsDialog(QDialog):
    """Manage reusable provider/API cards for fast switching."""

    def __init__(self, chat_window: "ChatWindow"):
        super().__init__(chat_window)
        self.chat_window = chat_window
        self.settings = chat_window.settings
        self.editing_card_id = ""

        self.setWindowTitle("服务配置")
        self.resize(
            AGENT_UI_METRICS["provider_card_dialog_width"],
            AGENT_UI_METRICS["provider_card_dialog_height"],
        )

        root_layout = QHBoxLayout(self)
        dialog_margin = AGENT_UI_METRICS["provider_card_dialog_margin"]
        root_layout.setContentsMargins(dialog_margin, dialog_margin, dialog_margin, dialog_margin)
        root_layout.setSpacing(AGENT_UI_METRICS["provider_card_dialog_spacing"])

        left_layout = QVBoxLayout()
        left_layout.setSpacing(AGENT_UI_METRICS["left_panel_spacing"])
        self.card_list = QListWidget()
        self.card_list.setMinimumWidth(AGENT_UI_METRICS["provider_card_list_min_width"])
        self.card_list.itemClicked.connect(self.load_card_from_item)
        left_layout.addWidget(self.card_list, 1)

        self.new_card_button = QPushButton("添加配置")
        self.delete_card_button = QPushButton("删除配置")
        self.new_card_button.clicked.connect(self.create_new_card)
        self.delete_card_button.clicked.connect(self.delete_selected_card)
        left_layout.addWidget(self.new_card_button)
        left_layout.addWidget(self.delete_card_button)

        editor_layout = QVBoxLayout()
        editor_layout.setSpacing(AGENT_UI_METRICS["left_panel_spacing"])

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("配置名称，例如：主线路")
        editor_layout.addWidget(QLabel("配置名称"))
        editor_layout.addWidget(self.name_input)

        provider_layout = QHBoxLayout()
        provider_label = QLabel("服务商:")
        provider_label.setFixedWidth(AGENT_UI_METRICS["form_label_width"])
        self.provider_combo = QComboBox()
        make_combo_popup_on_click(self.provider_combo)
        for provider_id, spec in PROVIDERS.items():
            self.provider_combo.addItem(spec.display_name, provider_id)
        provider_layout.addWidget(provider_label)
        provider_layout.addWidget(self.provider_combo, 1)
        editor_layout.addLayout(provider_layout)

        key_layout = QHBoxLayout()
        key_label = QLabel("API 密钥：")
        key_label.setFixedWidth(AGENT_UI_METRICS["form_label_width"])
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("输入该配置使用的 API 密钥")
        key_layout.addWidget(key_label)
        key_layout.addWidget(self.key_input, 1)
        editor_layout.addLayout(key_layout)

        url_layout = QHBoxLayout()
        url_label = QLabel("服务地址：")
        url_label.setFixedWidth(AGENT_UI_METRICS["form_label_width"])
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("例如：https://api.example.com/v1")
        url_layout.addWidget(url_label)
        url_layout.addWidget(self.url_input, 1)
        editor_layout.addLayout(url_layout)

        editor_layout.addStretch(1)

        action_layout = QHBoxLayout()
        self.apply_card_button = QPushButton("应用配置")
        self.save_card_button = QPushButton("保存配置")
        self.close_button = QPushButton("关闭")
        self.apply_card_button.clicked.connect(self.apply_current_card)
        self.save_card_button.clicked.connect(self.save_current_card)
        self.close_button.clicked.connect(self.accept)
        action_layout.addWidget(self.apply_card_button)
        action_layout.addWidget(self.save_card_button)
        action_layout.addWidget(self.close_button)
        editor_layout.addLayout(action_layout)

        root_layout.addLayout(left_layout, 0)
        root_layout.addLayout(editor_layout, 1)

        self.apply_button_metrics()
        self.refresh_card_list()

        if self.card_list.count():
            self.card_list.setCurrentRow(0)
            self.load_card_from_item(self.card_list.currentItem())
        else:
            self.create_new_card()

    def apply_button_metrics(self):
        height = AGENT_UI_METRICS["action_button_height"]
        for button in (
            self.new_card_button,
            self.delete_card_button,
            self.apply_card_button,
            self.save_card_button,
            self.close_button,
        ):
            button.setFixedHeight(height)

    def card_by_id(self, card_id: str) -> ProviderCard | None:
        for card in self.settings.provider_cards:
            if card.card_id == card_id:
                return card
        return None

    def card_by_name(self, name: str) -> ProviderCard | None:
        """按用户可见名称查找配置，忽略首尾空格和大小写。"""
        normalized_name = str(name or "").strip().casefold()
        if not normalized_name:
            return None
        for card in self.settings.provider_cards:
            if str(card.name or "").strip().casefold() == normalized_name:
                return card
        return None

    def refresh_card_list(self, select_card_id: str = ""):
        self.card_list.blockSignals(True)
        self.card_list.clear()
        for card in self.settings.provider_cards:
            item = QListWidgetItem(card.name or "未命名配置")
            item.setData(Qt.ItemDataRole.UserRole, card.card_id)
            self.card_list.addItem(item)
            if select_card_id and card.card_id == select_card_id:
                self.card_list.setCurrentItem(item)
        self.card_list.blockSignals(False)

    def load_card_from_item(self, item: QListWidgetItem | None):
        if item is None:
            return
        card = self.card_by_id(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        if card is None:
            return
        self.editing_card_id = card.card_id
        self.name_input.setText(card.name)
        index = self.provider_combo.findData(card.provider_id)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.key_input.setText(load_provider_card_key(card.card_id))
        self.url_input.setText(card.base_url)

    def create_new_card(self):
        provider_id = self.chat_window.get_current_provider()
        spec = get_provider_spec(provider_id)
        existing_count = len(self.settings.provider_cards) + 1
        self.editing_card_id = ""
        self.name_input.setText(f"{spec.display_name} 配置 {existing_count}")
        index = self.provider_combo.findData(provider_id)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.key_input.setText(self.chat_window.key_input.text().strip())
        self.url_input.setText(self.chat_window.url_input.text().strip())
        self.card_list.clearSelection()
        self.name_input.setFocus()
        self.name_input.selectAll()

    def form_values(self) -> tuple[str, str, str, str] | None:
        name = self.name_input.text().strip()
        provider_id = self.provider_combo.currentData() or "oneapi"
        api_key = self.key_input.text().strip()
        base_url = self.url_input.text().strip()

        if not name:
            QMessageBox.warning(self, "缺少配置名称", "请先填写配置名称。")
            return None

        return name, provider_id, api_key, base_url

    def save_current_card(self, silent: bool = False) -> str:
        values = self.form_values()
        if values is None:
            return ""

        name, provider_id, api_key, base_url = values
        # Use the configuration name as the update key so repeated saves do not duplicate entries.
        card = self.card_by_name(name)
        card_id = card.card_id if card is not None else uuid.uuid4().hex
        if card is None:
            card = app_config.ProviderCard(card_id=card_id)
            self.settings.provider_cards.append(card)

        card.name = name
        card.provider_id = provider_id
        card.base_url = base_url
        save_provider_card_key(card_id, api_key)
        app_config.save_settings(self.settings)

        self.editing_card_id = card_id
        self.refresh_card_list(select_card_id=card_id)
        if not silent:
            QMessageBox.information(self, "已保存", f"配置“{name}”已保存。")
        return card_id

    def apply_current_card(self):
        card_id = self.save_current_card(silent=True)
        if not card_id:
            return

        card = self.card_by_id(card_id)
        if card is None:
            return

        index = self.chat_window.provider_combo.findData(card.provider_id)
        if index >= 0:
            self.chat_window.provider_combo.setCurrentIndex(index)

        self.chat_window.key_input.setText(load_provider_card_key(card.card_id))
        self.chat_window.url_input.setText(card.base_url)
        self.chat_window.save_current_api_settings()
        self.chat_window.append_system_message(f"已应用服务配置：{card.name}")
        self.accept()

    def delete_selected_card(self):
        item = self.card_list.currentItem()
        if item is None:
            QMessageBox.information(self, "未选择配置", "请先选择要删除的配置。")
            return

        card_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        card = self.card_by_id(card_id)
        if card is None:
            return

        confirm = QMessageBox.question(
            self,
            "删除配置",
            f"确定删除配置“{card.name or '未命名配置'}”吗？\n\n其中保存的 API 密钥也会一并删除。",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.settings.provider_cards = [
            item for item in self.settings.provider_cards
            if item.card_id != card_id
        ]
        delete_provider_card_key(card_id)
        app_config.save_settings(self.settings)

        self.editing_card_id = ""
        self.refresh_card_list()
        if self.card_list.count():
            self.card_list.setCurrentRow(0)
            self.load_card_from_item(self.card_list.currentItem())
        else:
            self.create_new_card()


__all__ = [name for name in globals() if not name.startswith("__")]
