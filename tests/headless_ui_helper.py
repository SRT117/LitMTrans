"""
Headless UI Test Helper & Verification Script.
Runs PySide6 widgets in pure offscreen mode (no physical window popup),
performs simulated interactions, and captures screenshots for visual validation.
"""
import os
import sys

# Force Qt offscreen rendering mode
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QMainWindow, QLabel, QPushButton, QVBoxLayout, QWidget
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

def capture_widget_screenshot(widget: QWidget, output_path: str) -> str:
    """Renders widget offscreen and saves a screenshot to output_path."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    widget.show()
    app = QApplication.instance() or QApplication(sys.argv)
    app.processEvents()
    
    # Grab in-memory rendered pixmap
    pixmap = widget.grab()
    pixmap.save(output_path)
    return output_path

if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    
    # Test sample UI window
    window = QMainWindow()
    central = QWidget()
    layout = QVBoxLayout(central)
    label = QLabel("Headless UI Render Test")
    btn = QPushButton("Test Button")
    layout.addWidget(label)
    layout.addWidget(btn)
    window.setCentralWidget(central)
    window.resize(400, 300)
    
    output = capture_widget_screenshot(window, "tests/artifacts/screenshots/sample_headless.png")
    print(f"SUCCESS: Headless screenshot saved to {output}")
