import sys
import os
import platform
import bz2
import json
import zipfile
import tarfile
import io
from pathlib import Path
from collections import defaultdict

try:
    import zstandard
except ImportError:
    zstandard = None

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QTreeWidget, QTreeWidgetItem,
    QStatusBar, QFrame, QMenu, QAction
)
from PyQt5.QtCore import Qt, QUrl, QSettings
from PyQt5.QtGui import QFont, QIcon, QColor, QDesktopServices


def _get_rack2_plugins_dir():
    """Return the VCV Rack 2 plugins directory for the current OS, or None."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        rack_dir = Path.home() / ".local" / "share" / "Rack2"
    elif system == "Darwin":
        rack_dir = Path.home() / "Documents" / "Rack2"
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            rack_dir = Path(local) / "Rack2"
        else:
            rack_dir = Path.home() / "AppData" / "Local" / "Rack2"
    else:
        return None
    if not rack_dir.is_dir():
        return None
    # Determine platform-specific plugins directory name
    if system == "Linux":
        candidates = ["plugins-lin-x64", "plugins"]
    elif system == "Darwin":
        if "arm" in machine or "aarch64" in machine:
            candidates = ["plugins-mac-arm64", "plugins-mac-x64", "plugins"]
        else:
            candidates = ["plugins-mac-x64", "plugins-mac-arm64", "plugins"]
    elif system == "Windows":
        candidates = ["plugins-win-x64", "plugins"]
    else:
        candidates = ["plugins"]
    for name in candidates:
        d = rack_dir / name
        if d.is_dir():
            return d
    return None


def _get_installed_plugins():
    """Return a set of installed plugin slugs by scanning the Rack2 plugins directory."""
    plugins_dir = _get_rack2_plugins_dir()
    if plugins_dir is None:
        return set()
    installed = set()
    for entry in plugins_dir.iterdir():
        if entry.is_dir():
            # Try to read plugin.json for the slug
            pj = entry / "plugin.json"
            if pj.is_file():
                try:
                    data = json.loads(pj.read_text(encoding="utf-8"))
                    slug = data.get("slug", entry.name)
                    installed.add(slug)
                except Exception:
                    installed.add(entry.name)
            else:
                installed.add(entry.name)
    return installed


class VCVPatchAnalyzer(QMainWindow):
    MAX_RECENT = 10

    def __init__(self):
        super().__init__()
        self.setWindowTitle("VCV Rack 2 Patch Analyzer")
        self.setMinimumSize(600, 500)
        self.installed_plugins = _get_installed_plugins()
        self._build_ui()
        self._restore_geometry()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Top bar
        top = QHBoxLayout()
        self.open_btn = QPushButton("Open Patch File…")
        self.open_btn.setFixedHeight(32)
        self.open_btn.clicked.connect(self.open_file)
        top.addWidget(self.open_btn)

        self.recent_btn = QPushButton("Recent Files ▾")
        self.recent_btn.setFixedHeight(32)
        self.recent_menu = QMenu(self)
        self.recent_btn.setMenu(self.recent_menu)
        self._rebuild_recent_menu()
        top.addWidget(self.recent_btn)

        top.addStretch()

        self.expand_btn = QPushButton("Expand All")
        self.expand_btn.setFixedHeight(32)
        self.expand_btn.clicked.connect(self.tree.expandAll if hasattr(self, 'tree') else lambda: None)
        self.expand_btn.setEnabled(False)

        self.collapse_btn = QPushButton("Collapse All")
        self.collapse_btn.setFixedHeight(32)
        self.collapse_btn.clicked.connect(self.tree.collapseAll if hasattr(self, 'tree') else lambda: None)
        self.collapse_btn.setEnabled(False)

        top.addWidget(self.expand_btn)
        top.addWidget(self.collapse_btn)
        layout.addLayout(top)

        # File label
        self.file_label = QLabel("No file loaded")
        self.file_label.setFrameShape(QFrame.StyledPanel)
        self.file_label.setContentsMargins(6, 4, 6, 4)
        layout.addWidget(self.file_label)

        # Tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Plugin / Module", "Count", "VCV Library"])
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(1, 60)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        bold = QFont()
        bold.setBold(True)
        self.tree.headerItem().setFont(0, bold)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.tree)

        # Wire up expand/collapse now that tree exists
        self.expand_btn.clicked.disconnect()
        self.expand_btn.clicked.connect(self.tree.expandAll)
        self.collapse_btn.clicked.disconnect()
        self.collapse_btn.clicked.connect(self.tree.collapseAll)

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)

    def _on_item_clicked(self, item, column):
        if column == 2:
            url = item.data(2, Qt.UserRole)
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _on_item_double_clicked(self, item, column):
        if column != 2:
            url = item.data(2, Qt.UserRole)
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _rebuild_recent_menu(self):
        self.recent_menu.clear()
        settings = QSettings("VCVPatchAnalyzer", "VCVPatchAnalyzer")
        recent = settings.value("recentFiles", [])
        if not recent:
            action = self.recent_menu.addAction("(no recent files)")
            action.setEnabled(False)
            return
        for path in recent:
            action = self.recent_menu.addAction(path)
            action.triggered.connect(lambda checked, p=path: self.load_patch(p))

    def _add_recent(self, path: str):
        settings = QSettings("VCVPatchAnalyzer", "VCVPatchAnalyzer")
        recent = settings.value("recentFiles", [])
        if not isinstance(recent, list):
            recent = [recent] if recent else []
        # Remove if already present, then prepend
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        recent = recent[:self.MAX_RECENT]
        settings.setValue("recentFiles", recent)
        self._rebuild_recent_menu()

    def open_file(self):
        settings = QSettings("VCVPatchAnalyzer", "VCVPatchAnalyzer")
        last_dir = settings.value("lastFolder", "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open VCV Rack 2 Patch", last_dir,
            "VCV Rack Patch Files (*.vcv);;All Files (*)"
        )
        if path:
            settings.setValue("lastFolder", str(Path(path).parent))
            self.load_patch(path)

    def load_patch(self, path: str):
        try:
            with open(path, "rb") as f:
                raw = f.read()
            # VCV Rack 2 patches may be zstd-tar, ZIP, bzip2-compressed, or plain JSON
            if raw[:4] == b'\x28\xb5\x2f\xfd':
                if zstandard is None:
                    raise ImportError(
                        "This patch is zstandard-compressed but the 'zstandard' "
                        "package is not installed. Install it with:  pip install zstandard"
                    )
                # Zstandard-compressed (may be tar archive or plain JSON)
                dctx = zstandard.ZstdDecompressor()
                decompressed = dctx.decompress(raw, max_output_size=50 * 1024 * 1024)
                # Try as tar first
                try:
                    with tarfile.open(fileobj=io.BytesIO(decompressed)) as tf:
                        for member in tf:
                            if member.name.lower().endswith('.json'):
                                raw = tf.extractfile(member).read()
                                break
                        else:
                            raise ValueError("No .json file found in zstd-tar archive")
                except tarfile.TarError:
                    # Not a tar – treat decompressed data as plain JSON
                    raw = decompressed
            elif raw[:2] == b'PK':
                # ZIP archive – extract patch.json
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    names = zf.namelist()
                    json_name = next(
                        (n for n in names if n.lower().endswith('.json')), names[0]
                    )
                    raw = zf.read(json_name)
            else:
                try:
                    raw = bz2.decompress(raw)
                except Exception:
                    pass  # not bzip2, try as plain JSON
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            data = json.loads(text)
        except Exception as e:
            msg = f"Error loading file: {e}"
            print(msg)
            self.status.showMessage(msg)
            return

        modules = data.get("modules", [])
        if not isinstance(modules, list):
            msg = "Invalid patch file: 'modules' key not found or not a list."
            print(msg)
            self.status.showMessage(msg)
            return

        # Group modules by plugin
        plugin_modules: dict[str, list[str]] = defaultdict(list)
        for mod in modules:
            plugin = mod.get("plugin", "(unknown plugin)")
            model = mod.get("model", "(unknown module)")
            plugin_modules[plugin].append(model)

        self.tree.clear()
        total_modules = 0

        for plugin in sorted(plugin_modules.keys(), key=str.lower):
            models = plugin_modules[plugin]
            # Count occurrences of each model
            model_counts: dict[str, int] = defaultdict(int)
            for m in models:
                model_counts[m] += 1

            plugin_installed = plugin == "Core" or plugin in self.installed_plugins
            not_installed_color = QColor("#cc0000")

            plugin_item = QTreeWidgetItem(self.tree)
            plugin_item.setText(0, plugin)
            plugin_item.setText(1, str(len(models)))
            plugin_item.setTextAlignment(1, Qt.AlignLeft | Qt.AlignVCenter)
            plugin_url = f"https://library.vcvrack.com/{plugin}"
            plugin_item.setText(2, plugin_url.replace("https://", ""))
            plugin_item.setData(2, Qt.UserRole, plugin_url)
            plugin_item.setForeground(2, QColor("#3586ff"))
            font = plugin_item.font(0)
            font.setBold(True)
            plugin_item.setFont(0, font)
            plugin_item.setFont(1, font)
            if not plugin_installed:
                plugin_item.setForeground(0, not_installed_color)
                plugin_item.setForeground(1, not_installed_color)

            for model in sorted(model_counts.keys(), key=str.lower):
                count = model_counts[model]
                mod_item = QTreeWidgetItem(plugin_item)
                mod_item.setText(0, model)
                mod_item.setText(1, str(count))
                mod_item.setTextAlignment(1, Qt.AlignLeft | Qt.AlignVCenter)
                mod_url = f"https://library.vcvrack.com/{plugin}/{model}"
                mod_item.setText(2, mod_url.replace("https://", ""))
                mod_item.setData(2, Qt.UserRole, mod_url)
                mod_item.setForeground(2, QColor("#3586ff"))
                if not plugin_installed:
                    mod_item.setForeground(0, not_installed_color)
                    mod_item.setForeground(1, not_installed_color)

            plugin_item.setExpanded(True)
            total_modules += len(models)

        self._add_recent(path)
        self.file_label.setText(path)
        patch_version = data.get("version", "unknown")
        plugin_count = len(plugin_modules)
        msg = (
            f"Patch version: {patch_version}  |  "
            f"{plugin_count} plugin(s)  |  {total_modules} module(s) total"
        )
        self.status.showMessage(msg)
        self.tree.resizeColumnToContents(0)
        self.expand_btn.setEnabled(True)
        self.collapse_btn.setEnabled(True)


    def _restore_geometry(self):
        settings = QSettings("VCVPatchAnalyzer", "VCVPatchAnalyzer")
        geo = settings.value("geometry")
        state = settings.value("windowState")
        if geo is not None:
            self.restoreGeometry(geo)
        if state is not None:
            self.restoreState(state)

    def closeEvent(self, event):
        settings = QSettings("VCVPatchAnalyzer", "VCVPatchAnalyzer")
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("windowState", self.saveState())
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("VCV Patch Analyzer")
    window = VCVPatchAnalyzer()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
