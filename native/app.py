#!/usr/bin/env python3
"""Native Qt app — Cain (PD2 save editor).

This is the real app shell: persistent settings, first-run setup, native file
dialogs, Hero Editor-style character workspace, and a UI surface we can grow
without the browser/webview compromises.
"""
from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QMimeData, QPoint, QRect, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDrag,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressDialog,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QCheckBox,
    QInputDialog,
    QScrollArea,
    QSizePolicy,
    QTabBar,
    QSplitter,
    QComboBox,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from gui import server as save_api
from core.mpq import MPQArchive


APP_ORG = "cain"
APP_NAME = "Cain"
CELL = 40
EQUIP_SLOT = 48
INV_W = 10
INV_H = 8
STASH_W = 10
STASH_H = 15
ASSETS = None
EQUIP_SLOT_NAMES = {
    1: "Head", 2: "Amulet", 3: "Armor", 4: "Right Hand", 5: "Left Hand",
    6: "Right Ring", 7: "Left Ring", 8: "Belt", 9: "Boots", 10: "Gloves",
    11: "Alt Right", 12: "Alt Left",
}


@dataclass
class LoadedSave:
    path: str
    data: dict


def qclass(name: str) -> str:
    return "".join(part.capitalize() for part in str(name).replace("_", "-").split("-"))


def quality_color(quality: str) -> QColor:
    return {
        "normal": QColor("#d8d3c8"),
        "superior": QColor("#e4e0d6"),
        "magic": QColor("#6f9fff"),
        "rare": QColor("#fff06a"),
        "unique": QColor("#c7b377"),
        "set": QColor("#39d639"),
        "crafted": QColor("#e0a83c"),
    }.get(str(quality).lower(), QColor("#b8b8b8"))


class SearchableComboBox(QComboBox):
    """Combo with type-to-filter: editable line + contains-matching completer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        comp = self.completer()
        comp.setCompletionMode(QCompleter.PopupCompletion)
        comp.setFilterMode(Qt.MatchContains)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        self.setMaxVisibleItems(18)


def app_icon() -> QIcon:
    pm = QPixmap(96, 96)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#141014"))
    p.drawRoundedRect(2, 2, 92, 92, 18, 18)
    p.setBrush(QColor("#6b1e18"))
    p.drawEllipse(14, 12, 68, 68)
    p.setPen(QPen(QColor("#e2ae58"), 5))
    p.drawLine(48, 16, 48, 74)
    p.drawLine(30, 38, 66, 38)
    p.setBrush(QColor("#222126"))
    p.setPen(QPen(QColor("#e2ae58"), 4))
    p.drawRoundedRect(28, 50, 40, 28, 8, 8)
    p.end()
    return QIcon(pm)


class DiabloAssetLoader:
    def __init__(self, mpq_path: str):
        self.mpq_path = mpq_path
        self.archives = []
        self.palette = None
        self.cache: dict[str, QPixmap | None] = {}
        for path in self._candidate_mpqs(mpq_path):
            try:
                self.archives.append((path, MPQArchive(path)))
            except Exception:
                continue
        self.palette = self._load_palette()

    def _candidate_mpqs(self, mpq_path: str) -> list[str]:
        here = os.path.dirname(os.path.abspath(mpq_path))
        roots = [here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))]
        names = [
            "pd2assets.mpq", "patch_d2.mpq", "pd2data.mpq",
            "d2exp.mpq", "d2data.mpq",
        ]
        out = []
        for root in roots:
            for name in names:
                path = os.path.join(root, name)
                if os.path.isfile(path) and path not in out:
                    out.append(path)
        return out

    def _read_first(self, relpath: str) -> bytes | None:
        for _path, arc in self.archives:
            try:
                blob = arc.read_file(relpath)
            except Exception:
                continue
            if blob:
                return blob
        return None

    def _load_palette(self):
        blob = self._read_first(r"data\global\palette\ACT1\pal.dat")
        if not blob or len(blob) < 768:
            return None
        # pal.dat entries are stored B,G,R — flip to R,G,B
        return [tuple(blob[i:i + 3][::-1]) for i in range(0, 768, 3)]

    def item_pixmap(self, invfile: str) -> QPixmap | None:
        key = (invfile or "").strip().lower()
        if not key:
            return None
        if key in self.cache:
            return self.cache[key]
        stem = key[:-4] if key.endswith(".dc6") else key
        blob = self._read_first(fr"data\global\items\{stem}.dc6")
        if not blob:
            self.cache[key] = None
            return None
        img = self._decode_dc6(blob)
        pm = QPixmap.fromImage(img) if img is not None else None
        self.cache[key] = pm
        return pm

    def _decode_dc6(self, blob: bytes) -> QImage | None:
        if not self.palette or len(blob) < 60:
            return None
        try:
            _version, _flags, _encoding, _term, dirs, frames = struct.unpack_from("<6I", blob, 0)
            if dirs < 1 or frames < 1:
                return None
            frame_ptr = struct.unpack_from("<I", blob, 24)[0]
            _flip, width, height, _ox, _oy, _unk, _next, length = struct.unpack_from("<8i", blob, frame_ptr)
        except struct.error:
            return None
        if width <= 0 or height <= 0 or width > 512 or height > 512:
            return None
        payload = blob[frame_ptr + 32:frame_ptr + 32 + max(0, length)]
        img = QImage(width, height, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        x = 0
        y = height - 1
        pos = 0
        while pos < len(payload) and y >= 0:
            code = payload[pos]
            pos += 1
            if code == 0x80:
                x = 0
                y -= 1
            elif code & 0x80:
                x += code & 0x7F
            else:
                run = min(code, len(payload) - pos)
                for i in range(run):
                    if x >= width:
                        break
                    idx = payload[pos + i]
                    if idx:
                        r, g, b = self.palette[idx]
                        img.setPixelColor(x, y, QColor(r, g, b, 255))
                    x += 1
                pos += run
        return img


class SetupDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cain Setup")
        self.setMinimumWidth(720)
        self.settings = settings
        self.mpq = QLineEdit(settings.value("paths/mpq", ""))
        self.save = QLineEdit(settings.value("paths/save", ""))
        self.status = QLabel("")
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Project Diablo 2 data MPQ", self._row(self.mpq, self.pick_mpq))
        form.addRow("Character save or stash", self._row(self.save, self.pick_save))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        title = QLabel("Choose your game data and save")
        title.setObjectName("setupTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(QLabel("The editor needs pd2data.mpq to translate items and stats. These paths are remembered."))
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

    def _row(self, edit: QLineEdit, picker):
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        btn = QPushButton("Browse...")
        btn.clicked.connect(picker)
        row.addWidget(btn)
        return w

    def pick_mpq(self):
        start = self.mpq.text() or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Select pd2data.mpq", start, "MPQ archives (*.mpq);;All files (*)")
        if path:
            self.mpq.setText(path)

    def pick_save(self):
        start = self.save.text() or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Diablo II save", start,
            "Diablo II saves (*.d2s *.d2x *.sss *.stash);;All files (*)")
        if path:
            self.save.setText(path)

    def accept(self):
        mpq = self.mpq.text().strip()
        save = self.save.text().strip()
        if not os.path.isfile(mpq):
            self.status.setText("Select a valid pd2data.mpq.")
            return
        if not os.path.isfile(save):
            self.status.setText("Select a valid character save or stash.")
            return
        self.settings.setValue("paths/mpq", mpq)
        self.settings.setValue("paths/save", save)
        super().accept()


class ItemIcon:
    @staticmethod
    def pixmap(item: dict, size: QSize) -> QPixmap:
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        qcol = quality_color(item.get("quality"))
        rect = pm.rect()

        # Real sprite: draw it bare, filling the cell like in-game — no box, no border.
        asset = ASSETS.item_pixmap(item.get("invfile", "")) if ASSETS else None
        if asset and not asset.isNull():
            target = asset.scaled(rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = rect.left() + (rect.width() - target.width()) // 2
            y = rect.top() + (rect.height() - target.height()) // 2
            p.drawPixmap(x, y, target)
            p.end()
            return pm

        # Fallback glyph (no MPQ assets): subtle well, no hard border.
        p.setBrush(QBrush(QColor("#14100a")))
        p.setPen(Qt.NoPen)
        rect = rect.adjusted(1, 1, -1, -1)
        p.drawRoundedRect(rect, 3, 3)

        category = item.get("category", "")
        typ = (item.get("type_label") or item.get("base_name") or "").lower()
        p.setBrush(QBrush(QColor(qcol.red(), qcol.green(), qcol.blue(), 70)))
        p.setPen(QPen(qcol, 2))
        cx, cy = rect.center().x(), rect.center().y()
        if "armor" in typ:
            p.drawPolygon([QPoint(cx, rect.top() + 8), QPoint(rect.right() - 8, cy),
                           QPoint(cx, rect.bottom() - 8), QPoint(rect.left() + 8, cy)])
        elif "helm" in typ:
            p.drawArc(rect.adjusted(7, 9, -7, 8), 0, 180 * 16)
            p.drawLine(rect.left() + 10, cy, rect.right() - 10, cy)
        elif "glove" in typ or "boot" in typ:
            p.drawRoundedRect(rect.adjusted(10, 12, -10, -12), 8, 8)
        elif "charm" in typ:
            p.drawEllipse(rect.adjusted(11, 8, -11, -8))
        elif "ring" in typ:
            p.drawEllipse(rect.adjusted(12, 12, -12, -12))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(rect.adjusted(18, 18, -18, -18))
        elif "amulet" in typ:
            p.drawEllipse(rect.adjusted(15, 18, -15, -8))
            p.drawLine(cx, rect.top() + 8, cx, cy - 8)
        elif category == "weapon":
            p.drawLine(rect.left() + 12, rect.bottom() - 10, rect.right() - 11, rect.top() + 10)
            p.drawLine(rect.right() - 20, rect.top() + 10, rect.right() - 8, rect.top() + 22)
        else:
            p.drawRoundedRect(rect.adjusted(14, 14, -14, -14), 7, 7)

        if rect.width() >= 64:  # label only when it can fit; small tiles rely on tooltips
            p.setPen(QPen(QColor("#f1e7c7")))
            f = QFont()
            f.setPointSize(8)
            f.setBold(True)
            p.setFont(f)
            text = item.get("type_label") or item.get("base_name") or item.get("type_code")
            p.drawText(rect.adjusted(3, 0, -3, -4), Qt.AlignBottom | Qt.AlignHCenter,
                       str(text).replace(" Charm", ""))
        p.end()
        return pm


class ItemTile(QLabel):
    clicked = Signal(int)
    moved = Signal(int, int, int)

    def __init__(self, index: int, item: dict, parent=None):
        super().__init__(parent)
        self.index = index
        self.item = item
        self._drag_start = None
        self.setObjectName("itemTile")
        self.setAlignment(Qt.AlignCenter)
        self.setToolTip(item.get("name", ""))
        self.setPixmap(ItemIcon.pixmap(item, QSize(
            max(1, int(item.get("width", 1))) * CELL - 2,
            max(1, int(item.get("height", 1))) * CELL - 2,
        )))
        self.resize(max(1, int(item.get("width", 1))) * CELL,
                    max(1, int(item.get("height", 1))) * CELL)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag_start = ev.position().toPoint()
            self.clicked.emit(self.index)
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if not (ev.buttons() & Qt.LeftButton) or self._drag_start is None:
            return
        if (ev.position().toPoint() - self._drag_start).manhattanLength() < 8:
            return
        mime = QMimeData()
        mime.setText(str(self.index))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.pixmap())
        drag.exec(Qt.MoveAction)


class InventoryGrid(QFrame):
    item_selected = Signal(int)
    item_moved = Signal(int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.items = []
        self.setMinimumSize(INV_W * CELL + 2, INV_H * CELL + 2)
        self.setMaximumSize(INV_W * CELL + 2, INV_H * CELL + 2)
        self.setObjectName("inventoryGrid")

    def set_items(self, indexed_items: list[tuple[int, dict]]):
        for child in self.findChildren(ItemTile):
            child.deleteLater()
        self.items = indexed_items
        for idx, it in indexed_items:
            tile = ItemTile(idx, it, self)
            tile.clicked.connect(self.item_selected)
            tile.move(int(it.get("pos_x", 0)) * CELL + 1,
                      int(it.get("pos_y", 0)) * CELL + 1)
            tile.show()
        self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#080705"))
        p.setPen(QPen(QColor("#2b2114"), 1))
        for x in range(INV_W + 1):
            p.drawLine(x * CELL, 0, x * CELL, INV_H * CELL)
        for y in range(INV_H + 1):
            p.drawLine(0, y * CELL, INV_W * CELL, y * CELL)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasText():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        try:
            idx = int(ev.mimeData().text())
        except ValueError:
            return
        pos = ev.position().toPoint()
        x = max(0, min(INV_W - 1, pos.x() // CELL))
        y = max(0, min(INV_H - 1, pos.y() // CELL))
        self.item_moved.emit(idx, x, y)
        ev.acceptProposedAction()


class StashGrid(QFrame):
    item_selected = Signal(int)
    item_moved = Signal(int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.items = []
        self.setMinimumSize(STASH_W * CELL + 2, STASH_H * CELL + 2)
        self.setMaximumSize(STASH_W * CELL + 2, STASH_H * CELL + 2)
        self.setObjectName("stashGrid")

    def set_items(self, indexed_items: list[tuple[int, dict]]):
        for child in self.findChildren(ItemTile):
            child.deleteLater()
        self.items = indexed_items
        for idx, it in indexed_items:
            tile = ItemTile(idx, it, self)
            tile.clicked.connect(self.item_selected)
            tile.move(int(it.get("pos_x", 0)) * CELL + 1,
                      int(it.get("pos_y", 0)) * CELL + 1)
            tile.show()
        self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#080705"))
        p.setPen(QPen(QColor("#2b2114"), 1))
        for x in range(STASH_W + 1):
            p.drawLine(x * CELL, 0, x * CELL, STASH_H * CELL)
        for y in range(STASH_H + 1):
            p.drawLine(0, y * CELL, STASH_W * CELL, y * CELL)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasText():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        try:
            idx = int(ev.mimeData().text())
        except ValueError:
            return
        pos = ev.position().toPoint()
        x = max(0, min(STASH_W - 1, pos.x() // CELL))
        y = max(0, min(STASH_H - 1, pos.y() // CELL))
        self.item_moved.emit(idx, x, y)
        ev.acceptProposedAction()


class EquipSlotWidget(QFrame):
    """One paperdoll well: dark inset square, tiny slot label, clipped item icon."""

    item_dropped = Signal(int, int)
    item_selected = Signal(int)

    def __init__(self, slot_id: int, name: str, tall: bool = False, parent=None):
        super().__init__(parent)
        self.slot_id = slot_id
        self.slot_name = name
        self.item_index: int | None = None
        self.item: dict | None = None
        self._hover = False
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WA_Hover, True)
        height = EQUIP_SLOT * 2 + 6 if tall else EQUIP_SLOT
        self.setFixedSize(EQUIP_SLOT, height)

    def set_item(self, idx: int | None, item: dict | None):
        self.item_index = idx
        self.item = item
        self.setToolTip(item.get("name", "") if item else self.slot_name)
        self.setCursor(Qt.PointingHandCursor if item else Qt.ArrowCursor)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect().adjusted(0, 0, -1, -1)
        p.fillRect(self.rect(), QColor("#080705"))
        p.setPen(QPen(QColor("#000000"), 1))
        p.drawRect(rect)
        p.setPen(QPen(QColor("#1c160d"), 1))
        p.drawRect(rect.adjusted(1, 1, -1, -1))
        if self.item:
            # icon, clipped hard to the well rect
            inner = rect.adjusted(2, 2, -1, -1)
            p.setClipRect(inner)
            pm = ItemIcon.pixmap(self.item, inner.size())
            x = inner.left() + (inner.width() - pm.width()) // 2
            y = inner.top() + (inner.height() - pm.height()) // 2
            p.drawPixmap(x, y, pm)
            p.setClipping(False)
        else:
            p.setPen(QPen(QColor("#3f3a2e"), 1))
            f = QFont()
            f.setPointSize(6)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, self.slot_name.upper())
        if self._hover and self.item is not None:
            p.setPen(QPen(QColor("#8a7339"), 1))
            p.drawRect(rect.adjusted(1, 1, -1, -1))
        p.end()

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.item_index is not None:
            self.item_selected.emit(self.item_index)
        super().mousePressEvent(ev)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasText():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasText():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        try:
            idx = int(ev.mimeData().text())
        except ValueError:
            return
        self.item_dropped.emit(idx, self.slot_id)
        ev.acceptProposedAction()


class EquipmentPanel(QWidget):
    """D2-accurate paperdoll: belt center-bottom flanked by rings, gloves BL, boots BR."""

    item_selected = Signal(int)
    item_dropped = Signal(int, int)
    # (name, slot_id, row, col, rowspan) on a 7-column grid
    SLOT_ORDER = [
        ("Head", 1, 0, 1, 1),
        ("Amu", 2, 0, 5, 1),
        ("R Hand", 4, 1, 0, 2),
        ("Armor", 3, 1, 3, 1),
        ("L Hand", 5, 1, 6, 2),
        ("R Ring", 6, 2, 2, 1),
        ("Belt", 8, 2, 3, 1),
        ("L Ring", 7, 2, 4, 1),
        ("Gloves", 10, 3, 1, 1),
        ("Boots", 9, 3, 5, 1),
    ]
    ALT_SLOTS = [("Alt R", 11), ("Alt L", 12)]

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        grid_host = QWidget()
        grid_host.setObjectName("hostTransparent")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self.slots: dict[int, tuple[str, EquipSlotWidget]] = {}
        for name, slot_id, row, col, rowspan in self.SLOT_ORDER:
            box = EquipSlotWidget(slot_id, name, tall=rowspan == 2)
            box.item_selected.connect(self.item_selected)
            box.item_dropped.connect(self.item_dropped)
            grid.addWidget(box, row, col, rowspan, 1, Qt.AlignTop)
            self.slots[slot_id] = (name, box)
        for c in range(7):
            grid.setColumnMinimumWidth(c, EQUIP_SLOT)
        outer.addWidget(grid_host, alignment=Qt.AlignHCenter)

        alt_host = QWidget()
        alt_host.setObjectName("hostTransparent")
        alt = QHBoxLayout(alt_host)
        alt.setContentsMargins(0, 0, 0, 0)
        alt.setSpacing(6)
        cap = QLabel("ALT")
        cap.setObjectName("slotCaption")
        alt.addWidget(cap)
        for name, slot_id in self.ALT_SLOTS:
            box = EquipSlotWidget(slot_id, name)
            box.item_selected.connect(self.item_selected)
            box.item_dropped.connect(self.item_dropped)
            alt.addWidget(box)
            self.slots[slot_id] = (name, box)
        outer.addWidget(alt_host, alignment=Qt.AlignHCenter)

    def set_items(self, indexed_items: list[tuple[int, dict]]):
        for _name, box in self.slots.values():
            box.set_item(None, None)
        for idx, it in indexed_items:
            slot_id = int(it.get("equipped_id", 0))
            slot = self.slots.get(slot_id)
            if slot:
                slot[1].set_item(idx, it)


class BeltPanel(QWidget):
    item_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(6)

    def set_items(self, indexed_items: list[tuple[int, dict]]):
        while self.layout.count():
            child = self.layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        for idx, it in sorted(indexed_items, key=lambda pair: (int(pair[1].get("pos_x", 0)), pair[0])):
            tile = QPushButton()
            tile.setIcon(QIcon(ItemIcon.pixmap(it, QSize(34, 34))))
            tile.setIconSize(QSize(30, 30))
            tile.setToolTip(it.get("name", ""))
            tile.setFixedSize(40, 40)
            tile.clicked.connect(lambda _checked=False, item_idx=idx: self.item_selected.emit(item_idx))
            self.layout.addWidget(tile)
        self.layout.addStretch(1)


class DetailPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = QLabel("Select an item")
        self.title.setObjectName("detailTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("detailSubtitle")
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignCenter)
        self.stats = QTextEdit()
        self.stats.setReadOnly(True)
        self.stats.setObjectName("statText")
        self.max_roll = QPushButton("Max Roll")
        self.max_roll.setEnabled(False)
        self.move_inventory = QPushButton("Move to Inventory")
        self.move_inventory.setEnabled(False)
        self.equip = QPushButton("Equip...")
        self.equip.setEnabled(False)
        self.edit = QPushButton("Edit Item...")
        self.edit.setEnabled(False)
        self.unsocket = QPushButton("Unsocket")
        self.unsocket.setEnabled(False)
        self.socket = QPushButton("Socket...")
        self.socket.setEnabled(False)
        self.duplicate = QPushButton("Duplicate")
        self.duplicate.setEnabled(False)
        self.copy_stash = QPushButton("Copy to Stash...")
        self.copy_stash.setEnabled(False)
        self.copy_character = QPushButton("Copy to Character...")
        self.copy_character.setEnabled(False)
        self.delete = QPushButton("Delete")
        self.delete.setEnabled(False)
        self.delete.setObjectName("dangerBtn")
        self.build = QPushButton("Open Item Builder")
        self.title.setAlignment(Qt.AlignCenter)
        self.subtitle.setAlignment(Qt.AlignCenter)
        self.setObjectName("detailPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addWidget(self.icon)
        layout.addWidget(self.stats, 1)
        self.stats.setMinimumHeight(170)
        self.stats.setMaximumHeight(340)
        actions = QGridLayout()
        actions.setHorizontalSpacing(7)
        actions.setVerticalSpacing(7)
        actions.addWidget(self.max_roll, 0, 0, 1, 2)
        actions.addWidget(self.move_inventory, 1, 0)
        actions.addWidget(self.equip, 1, 1)
        actions.addWidget(self.edit, 2, 0)
        actions.addWidget(self.socket, 2, 1)
        actions.addWidget(self.unsocket, 3, 0)
        actions.addWidget(self.duplicate, 3, 1)
        actions.addWidget(self.copy_stash, 4, 0)
        actions.addWidget(self.copy_character, 4, 1)
        actions.addWidget(self.delete, 5, 0, 1, 2)
        actions.addWidget(self.build, 6, 0, 1, 2)
        layout.addLayout(actions)
        layout.addStretch(1)

    def show_item(self, item: dict | None):
        if not item:
            self.title.setText("Select an item")
            self.subtitle.setText("")
            self.icon.clear()
            self.stats.clear()
            self.max_roll.setEnabled(False)
            self.move_inventory.setEnabled(False)
            self.equip.setEnabled(False)
            self.edit.setEnabled(False)
            self.socket.setEnabled(False)
            self.unsocket.setEnabled(False)
            self.duplicate.setEnabled(False)
            self.copy_stash.setEnabled(False)
            self.copy_character.setEnabled(False)
            self.delete.setEnabled(False)
            return
        self.title.setText(item.get("name", "Unknown item"))
        parts = [str(item.get("quality", "")).title(), item.get("base_name", "")]
        if item.get("ethereal"):
            parts.append("Ethereal")
        if item.get("personalized"):
            parts.append(f"Personalized: {item.get('personal_name', '')}")
        if item.get("runeword"):
            parts.append(item.get("runeword_name") or f"Runeword {item.get('runeword_id', -1)}")
        if item.get("num_sockets"):
            filled = int(item.get("filled_sockets", 0))
            parts.append(f"{filled}/{item.get('num_sockets')} sockets")
        self.subtitle.setText(" · ".join(p for p in parts if p))
        self.icon.setPixmap(ItemIcon.pixmap(item, QSize(100, 100)))
        lines = []
        if item.get("_section_name"):
            lines.append(f"Container: {item.get('_section_name')}")
        if item.get("defense") is not None:
            lines.append(f"Defense: {item['defense']}")
        if item.get("durability"):
            lines.append(f"Durability: {item['durability']}")
        if item.get("quantity") is not None:
            lines.append(f"Quantity: {item['quantity']}")
        if item.get("personalized"):
            lines.append(f"Personalized Name: {item.get('personal_name', '')}")
        if item.get("runeword"):
            rw_name = item.get("runeword_name") or "Unknown Runeword"
            lines.append(f"Runeword: {rw_name} ({item.get('runeword_id', -1)})")
        lines.append(f"Item Level: {item.get('ilvl', 0)}")
        aff = item.get("affixes") or {}
        aff_lines = []
        if aff.get("prefix"):
            aff_lines.append(f"Prefix: {aff.get('prefix')} ({aff.get('prefix_id')})")
        if aff.get("suffix"):
            aff_lines.append(f"Suffix: {aff.get('suffix')} ({aff.get('suffix_id')})")
        for rare in aff.get("rare_affixes", []):
            aff_lines.append(f"Affix: {rare.get('name')} ({rare.get('id')})")
        if aff_lines:
            lines.append("")
            lines.extend(aff_lines)
        sockets = item.get("sockets") or []
        if sockets:
            lines.append("")
            lines.append("Socketed")
            for child in sockets:
                lines.append(f"- {child.get('name', child.get('type_code', 'item'))}")
                for stat in child.get("stats", [])[:4]:
                    lines.append(f"  {stat.get('text', stat.get('name', ''))}")
        lines.append("")
        base_stats = item.get("stats", [])
        runeword_stats = item.get("runeword_stats", [])
        if base_stats:
            lines.append("Item Stats")
            lines.extend(stat.get("text", stat.get("name", "")) for stat in base_stats)
            lines.append("")
        if runeword_stats:
            lines.append("Runeword Stats")
            lines.extend(stat.get("text", stat.get("name", "")) for stat in runeword_stats)
        self.stats.setPlainText("\n".join(lines))
        self.max_roll.setEnabled(bool(item.get("clean")))
        self.move_inventory.setEnabled(int(item.get("location", 0)) != 0 or int(item.get("panel", 0)) != 1)
        self.equip.setEnabled(True)
        self.edit.setEnabled(True)
        self.socket.setEnabled(int(item.get("num_sockets", 0)) > int(item.get("filled_sockets", 0)))
        self.unsocket.setEnabled(bool(item.get("sockets")))
        self.duplicate.setEnabled(bool(item.get("clean")))
        self.copy_stash.setEnabled(bool(item.get("clean")))
        self.copy_character.setEnabled(False)
        self.delete.setEnabled(True)

    def set_read_only(self, copy_to_character: bool = False):
        self.max_roll.setEnabled(False)
        self.move_inventory.setEnabled(False)
        self.equip.setEnabled(False)
        self.edit.setEnabled(False)
        self.socket.setEnabled(False)
        self.unsocket.setEnabled(False)
        self.duplicate.setEnabled(False)
        self.copy_stash.setEnabled(False)
        self.copy_character.setEnabled(copy_to_character)
        self.delete.setEnabled(False)


class CharacterStatsPanel(QWidget):
    save_requested = Signal(dict)
    FIELDS = [
        ("strength", "Strength"),
        ("energy", "Energy"),
        ("dexterity", "Dexterity"),
        ("vitality", "Vitality"),
        ("stat_points", "Unspent Stat Points"),
        ("skill_points", "Unspent Skill Points"),
        ("current_life", "Current Life"),
        ("max_life", "Maximum Life"),
        ("current_mana", "Current Mana"),
        ("max_mana", "Maximum Mana"),
        ("current_stamina", "Current Stamina"),
        ("max_stamina", "Maximum Stamina"),
        ("level", "Level"),
        ("experience", "Experience"),
        ("gold", "Gold"),
        ("stash_gold", "Stash Gold"),
    ]
    SCHEMA = {
        "strength": (10, 1), "energy": (10, 1), "dexterity": (10, 1), "vitality": (10, 1),
        "stat_points": (10, 1), "skill_points": (8, 1),
        "current_life": (21, 256), "max_life": (21, 256),
        "current_mana": (21, 256), "max_mana": (21, 256),
        "current_stamina": (21, 256), "max_stamina": (21, 256),
        "level": (7, 1, 99), "experience": (32, 1),
        "gold": (25, 1), "stash_gold": (25, 1),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edits: dict[str, QSpinBox] = {}
        self.max_values: dict[str, int] = {}

        def spin(key: str, width: int = 92) -> QSpinBox:
            edit = QSpinBox()
            edit.setRange(0, 2147483647)
            edit.setFixedWidth(width)
            edit.setAlignment(Qt.AlignCenter)
            edit.setButtonSymbols(QSpinBox.NoButtons)
            self.edits[key] = edit
            return edit

        def caps(text: str) -> QLabel:
            lbl = QLabel(" ".join(text.upper()))
            lbl.setObjectName("sheetLabel")
            return lbl

        def pair(label: str, cur_key: str, max_key: str) -> QWidget:
            host = QWidget()
            host.setObjectName("hostTransparent")
            row = QHBoxLayout(host)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(caps(label))
            row.addStretch(1)
            row.addWidget(spin(cur_key, 78))
            slash = QLabel("/")
            slash.setObjectName("slotCaption")
            row.addWidget(slash)
            row.addWidget(spin(max_key, 78))
            return host

        sheet = QFrame()
        sheet.setObjectName("panelFrame")
        sheet.setMaximumWidth(680)
        body = QVBoxLayout(sheet)
        body.setContentsMargins(22, 16, 22, 16)
        body.setSpacing(10)
        title = QLabel("C H A R A C T E R")
        title.setObjectName("panelTitle")
        title.setAlignment(Qt.AlignCenter)
        body.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(36)
        grid.setVerticalSpacing(10)
        # attributes (left) — derived pools (right), like the in-game sheet
        for i, (key, label) in enumerate([
            ("strength", "Strength"), ("dexterity", "Dexterity"),
            ("vitality", "Vitality"), ("energy", "Energy"),
        ]):
            grid.addWidget(caps(label), i, 0)
            grid.addWidget(spin(key), i, 1)
        grid.addWidget(pair("Stamina", "current_stamina", "max_stamina"), 0, 2)
        grid.addWidget(pair("Life", "current_life", "max_life"), 1, 2)
        grid.addWidget(pair("Mana", "current_mana", "max_mana"), 2, 2)
        grid.setColumnStretch(2, 1)
        body.addLayout(grid)

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setObjectName("sheetRule")
        body.addWidget(rule)

        grid2 = QGridLayout()
        grid2.setHorizontalSpacing(36)
        grid2.setVerticalSpacing(10)
        grid2.addWidget(caps("Level"), 0, 0)
        grid2.addWidget(spin("level"), 0, 1)
        grid2.addWidget(caps("Experience"), 0, 2)
        grid2.addWidget(spin("experience", 130), 0, 3)
        grid2.addWidget(caps("Gold"), 1, 0)
        grid2.addWidget(spin("gold", 130), 1, 1)
        grid2.addWidget(caps("Stash Gold"), 1, 2)
        grid2.addWidget(spin("stash_gold", 130), 1, 3)
        grid2.setColumnStretch(3, 1)
        body.addLayout(grid2)

        rule2 = QFrame()
        rule2.setFrameShape(QFrame.HLine)
        rule2.setObjectName("sheetRule")
        body.addWidget(rule2)

        points = QHBoxLayout()
        sp_lbl = QLabel("S T A T   P O I N T S   R E M A I N I N G")
        sp_lbl.setObjectName("sheetRed")
        points.addWidget(sp_lbl)
        points.addWidget(spin("stat_points", 70))
        points.addSpacing(28)
        sk_lbl = QLabel("S K I L L   P O I N T S   R E M A I N I N G")
        sk_lbl.setObjectName("sheetRed")
        points.addWidget(sk_lbl)
        points.addWidget(spin("skill_points", 70))
        points.addStretch(1)
        body.addLayout(points)

        self.save = QPushButton("Save Character Stats")
        self.save.clicked.connect(self._save)
        self.max_core = QPushButton("Max Core Stats")
        self.max_core.clicked.connect(self._max_core)
        self.max_resources = QPushButton("Max Life/Mana/Stamina")
        self.max_resources.clicked.connect(self._max_resources)
        self.max_currency = QPushButton("Max Gold")
        self.max_currency.clicked.connect(self._max_currency)
        actions = QHBoxLayout()
        actions.addWidget(self.max_core)
        actions.addWidget(self.max_resources)
        actions.addWidget(self.max_currency)
        actions.addStretch(1)
        actions.addWidget(self.save)
        body.addLayout(actions)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addWidget(sheet, alignment=Qt.AlignTop | Qt.AlignHCenter)
        layout.addStretch(1)

    def set_stats(self, stats: dict | None):
        entries = (stats or {}).get("entries", [])
        by_key = {entry.get("key"): entry for entry in entries}
        values = (stats or {}).get("values", {})
        for key, edit in self.edits.items():
            entry = by_key.get(key, {})
            schema = self.SCHEMA.get(key, (31, 1))
            default_bits, default_scale = schema[0], schema[1]
            bits = int(entry.get("bits", default_bits))
            scale = int(entry.get("scale", default_scale))
            max_value = ((1 << bits) - 1) // max(1, scale)
            if len(schema) > 2:
                max_value = min(max_value, int(schema[2]))
            self.max_values[key] = max_value
            edit.setMaximum(min(max_value, 2147483647))
            val = values.get(key, 0)
            edit.setValue(max(0, min(int(val if val is not None else 0), edit.maximum())))

    def _set_keys_to_max(self, keys: list[str]):
        for key in keys:
            if key in self.edits:
                self.edits[key].setValue(min(self.max_values.get(key, self.edits[key].maximum()), self.edits[key].maximum()))

    def _max_core(self):
        self._set_keys_to_max(["strength", "energy", "dexterity", "vitality", "stat_points", "skill_points", "level"])

    def _max_resources(self):
        self._set_keys_to_max(["current_life", "max_life", "current_mana", "max_mana", "current_stamina", "max_stamina"])

    def _max_currency(self):
        self._set_keys_to_max(["gold", "stash_gold"])

    def _save(self):
        updates = {}
        for key, edit in self.edits.items():
            updates[key] = int(edit.value())
        self.save_requested.emit(updates)


class SkillsPanel(QWidget):
    save_requested = Signal(dict)

    CLASS_CODES = {
        "Amazon": "ama", "Sorceress": "sor", "Necromancer": "nec",
        "Paladin": "pal", "Barbarian": "bar", "Druid": "dru", "Assassin": "ass",
    }
    TREE_NAMES = {
        "ama": {1: "Bow & Crossbow", 2: "Passive & Magic", 3: "Javelin & Spear"},
        "sor": {1: "Fire Spells", 2: "Lightning Spells", 3: "Cold Spells"},
        "nec": {1: "Curses", 2: "Poison & Bone", 3: "Summoning"},
        "pal": {1: "Combat Skills", 2: "Offensive Auras", 3: "Defensive Auras"},
        "bar": {1: "Combat Skills", 2: "Combat Masteries", 3: "Warcries"},
        "dru": {1: "Elemental", 2: "Shape Shifting", 3: "Summoning"},
        "ass": {1: "Martial Arts", 2: "Shadow Disciplines", 3: "Traps"},
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edits: dict[int, QSpinBox] = {}
        self._layout_cache: dict[int, tuple[int, int, int, int]] | None = None
        self.trees = QTabWidget()
        self.save = QPushButton("Save Skills")
        self.save.clicked.connect(self._save)
        self.target_level = QSpinBox()
        self.target_level.setRange(0, 255)
        self.target_level.setValue(20)
        self.set_all = QPushButton("Set All")
        self.set_all.clicked.connect(self._set_all_to_target)
        self.clear_all = QPushButton("Clear All")
        self.clear_all.clicked.connect(lambda: self._set_all(0))
        layout = QVBoxLayout(self)
        layout.addWidget(self.trees, 1)
        actions = QHBoxLayout()
        actions.addWidget(QLabel("Level"))
        actions.addWidget(self.target_level)
        actions.addWidget(self.set_all)
        actions.addWidget(self.clear_all)
        actions.addStretch(1)
        actions.addWidget(self.save)
        layout.addLayout(actions)

    def _skill_layout(self) -> dict[int, tuple[int, int, int, int]]:
        """skill id -> (page, row, col, reqlevel) from skills.txt + skilldesc."""
        if self._layout_cache is not None:
            return self._layout_cache
        out: dict[int, tuple[int, int, int, int]] = {}
        try:
            t = save_api.tables()
            sd_by = {r.get("skilldesc"): r for r in t.load_table("skilldesc")}
            for r in t.load_table("skills"):
                if not r.get("charclass"):
                    continue
                d = sd_by.get(r.get("skilldesc"))
                if not d:
                    continue
                try:
                    out[int(r.get("Id", "-1"))] = (
                        int(d.get("SkillPage") or 0), int(d.get("SkillRow") or 0),
                        int(d.get("SkillColumn") or 0), int(r.get("reqlevel") or 1))
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001 — fall back to flat layout
            pass
        self._layout_cache = out
        return out

    def set_skills(self, skills_data: dict | None, class_name: str | None = None):
        self.trees.clear()
        self.edits = {}
        skills = (skills_data or {}).get("skills", [])
        layout_map = self._skill_layout()
        code = self.CLASS_CODES.get(str(class_name or ""), "")
        tree_names = self.TREE_NAMES.get(code, {})

        def make_spin(skill) -> QSpinBox:
            edit = QSpinBox()
            edit.setRange(0, 255)
            edit.setValue(max(0, min(255, int(skill.get("level", 0)))))
            edit.setFixedWidth(64)
            edit.setAlignment(Qt.AlignCenter)
            self.edits[int(skill.get("id", skill.get("index", 0)))] = edit
            return edit

        by_page: dict[int, list] = {}
        loose = []
        for skill in skills:
            sid = int(skill.get("id", skill.get("index", 0)))
            pos = layout_map.get(sid)
            if pos and pos[0] in (1, 2, 3):
                by_page.setdefault(pos[0], []).append((pos, skill))
            else:
                loose.append(skill)

        if by_page:
            for page in sorted(by_page):
                host = QWidget()
                grid = QGridLayout(host)
                grid.setContentsMargins(24, 18, 24, 18)
                grid.setSpacing(10)
                for (pg, row, col, reqlevel), skill in by_page[page]:
                    cell = QFrame()
                    cell.setObjectName("panelFrame")
                    cl = QVBoxLayout(cell)
                    cl.setContentsMargins(8, 6, 8, 8)
                    cl.setSpacing(3)
                    name = QLabel(skill.get("name", "Skill"))
                    name.setObjectName("sheetLabel")
                    name.setWordWrap(True)
                    name.setAlignment(Qt.AlignCenter)
                    req = QLabel(f"req lvl {reqlevel}")
                    req.setObjectName("slotCaption")
                    req.setAlignment(Qt.AlignCenter)
                    cl.addWidget(name)
                    cl.addWidget(req)
                    cl.addWidget(make_spin(skill), alignment=Qt.AlignCenter)
                    grid.addWidget(cell, max(0, row - 1), max(0, col - 1))
                for c in range(3):
                    grid.setColumnStretch(c, 1)
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QFrame.NoFrame)
                scroll.setWidget(host)
                self.trees.addTab(scroll, tree_names.get(page, f"Tree {page}"))
        if loose:
            host = QWidget()
            form = QFormLayout(host)
            for skill in loose:
                form.addRow(skill.get("name", "Skill"), make_spin(skill))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidget(host)
            self.trees.addTab(scroll, "Other")

        has_data = bool(self.edits)
        self.save.setEnabled(has_data)
        self.set_all.setEnabled(has_data)
        self.clear_all.setEnabled(has_data)
        self.target_level.setEnabled(has_data)

    def _set_all(self, value: int):
        for edit in self.edits.values():
            edit.setValue(max(0, min(255, int(value))))

    def _set_all_to_target(self):
        self._set_all(self.target_level.value())

    def _save(self):
        updates = {}
        for sid, edit in self.edits.items():
            updates[str(sid)] = int(edit.value())
        self.save_requested.emit(updates)


class WaypointsPanel(QWidget):
    save_requested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.checks: dict[int, dict[int, QCheckBox]] = {}
        self.tabs = QTabWidget()
        self.save = QPushButton("Save Waypoints")
        self.save.clicked.connect(self._save)
        layout = QVBoxLayout(self)
        tools = QHBoxLayout()
        all_btn = QPushButton("Unlock Visible")
        none_btn = QPushButton("Clear Visible")
        all_btn.clicked.connect(lambda: self._set_visible(True))
        none_btn.clicked.connect(lambda: self._set_visible(False))
        tools.addWidget(all_btn)
        tools.addWidget(none_btn)
        tools.addStretch(1)
        layout.addLayout(tools)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.save)

    def set_waypoints(self, data: dict | None):
        self.tabs.clear()
        self.checks = {}
        for diff in (data or {}).get("difficulties", []):
            diff_id = int(diff.get("difficulty", 0))
            self.checks[diff_id] = {}
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            body = QWidget()
            body_layout = QVBoxLayout(body)
            current_act = None
            act_layout = None
            for wp in diff.get("waypoints", []):
                act = wp.get("act", "")
                if act != current_act:
                    current_act = act
                    label = QLabel(act)
                    label.setObjectName("detailSubtitle")
                    body_layout.addWidget(label)
                    act_layout = QGridLayout()
                    body_layout.addLayout(act_layout)
                cb = QCheckBox(wp.get("name", "Waypoint"))
                cb.setChecked(bool(wp.get("unlocked")))
                wp_id = int(wp.get("id", 0))
                self.checks[diff_id][wp_id] = cb
                row = act_layout.count() // 2 if act_layout is not None else 0
                col = act_layout.count() % 2 if act_layout is not None else 0
                if act_layout is not None:
                    act_layout.addWidget(cb, row, col)
            body_layout.addStretch(1)
            scroll.setWidget(body)
            self.tabs.addTab(scroll, diff.get("name", f"Difficulty {diff_id + 1}"))

    def _set_visible(self, checked: bool):
        diff = self.tabs.currentIndex()
        for cb in self.checks.get(diff, {}).values():
            cb.setChecked(checked)

    def _save(self):
        payload = {}
        for diff, checks in self.checks.items():
            payload[str(diff)] = [wp_id for wp_id, cb in checks.items() if cb.isChecked()]
        self.save_requested.emit(payload)


class QuestsPanel(QWidget):
    save_requested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edits: dict[int, dict[int, QComboBox]] = {}
        self.tabs = QTabWidget()
        self.save = QPushButton("Save Quests")
        self.save.clicked.connect(self._save)
        self.complete_all = QPushButton("Complete Difficulty")
        self.complete_all.clicked.connect(lambda: self._set_current_complete(True))
        self.clear_all = QPushButton("Clear Difficulty")
        self.clear_all.clicked.connect(lambda: self._set_current_complete(False))
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        actions = QHBoxLayout()
        actions.addWidget(self.complete_all)
        actions.addWidget(self.clear_all)
        actions.addStretch(1)
        actions.addWidget(self.save)
        layout.addLayout(actions)

    # Quest words: bit0 = completed, bit2 = started/given, bit12 = quest log
    # viewed (set once the completion animation has played — "long completed").
    # Presets reuse the quest's existing word where possible so an untouched
    # quest round-trips byte-exact and the dropdown never needs to show hex.

    @staticmethod
    def _quest_combo(value: int) -> QComboBox:
        combo = QComboBox()
        completed = value if value & 0x0001 else 0x1001
        in_progress = value if value and not value & 0x0001 else 0x0004
        combo.addItem("Not Started", 0x0000)
        combo.addItem("In Progress", in_progress)
        combo.addItem("Completed", completed)
        combo.setCurrentIndex(2 if value & 0x0001 else (1 if value else 0))
        return combo

    def set_quests(self, data: dict | None):
        self.tabs.clear()
        self.edits = {}
        has_data = bool((data or {}).get("difficulties"))
        self.complete_all.setEnabled(has_data)
        self.clear_all.setEnabled(has_data)
        self.save.setEnabled(has_data)
        for diff in (data or {}).get("difficulties", []):
            diff_id = int(diff.get("difficulty", 0))
            self.edits[diff_id] = {}
            body = QWidget()
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(16, 10, 16, 10)
            body_layout.setSpacing(8)
            current_act = None
            act_grid = None
            for quest in diff.get("quests", []):
                qid = int(quest.get("id", 0))
                act = quest.get("act", "")
                if act != current_act:
                    current_act = act
                    act_label = QLabel(" ".join(act.upper()))
                    act_label.setObjectName("sheetLabel")
                    body_layout.addWidget(act_label)
                    act_grid = QGridLayout()
                    act_grid.setHorizontalSpacing(16)
                    act_grid.setVerticalSpacing(4)
                    act_grid.setColumnStretch(0, 1)
                    act_grid.setColumnStretch(2, 1)
                    body_layout.addLayout(act_grid)
                try:
                    value = int(str(quest.get("hex", "0000")), 16) & 0xFFFF
                except ValueError:
                    value = 0
                combo = self._quest_combo(value)
                combo.setFixedWidth(130)
                self.edits[diff_id][qid] = combo
                row, col = divmod(act_grid.count() // 2, 2)
                act_grid.addWidget(QLabel(quest.get("name", "")), row, col * 2)
                act_grid.addWidget(combo, row, col * 2 + 1)
            body_layout.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidget(body)
            self.tabs.addTab(scroll, diff.get("name", f"Difficulty {diff_id + 1}"))

    def _set_current_complete(self, checked: bool):
        diff = self.tabs.currentIndex()
        for qid, combo in self.edits.get(diff, {}).items():
            if qid < len(save_api.QUEST_WORD_LABELS) or not checked:
                combo.setCurrentIndex(2 if checked else 0)

    def _save(self):
        payload = {}
        for diff, edits in self.edits.items():
            changes = {}
            for qid, combo in edits.items():
                value = int(combo.currentData() or 0)
                changes[str(qid)] = f"{value & 0xFFFF:04x}"
            payload[str(diff)] = changes
        self.save_requested.emit(payload)


class ItemEditDialog(QDialog):
    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit {item.get('name', 'Item')}")
        self.setMinimumWidth(760)
        self.item = item
        self.field_widgets: dict[str, QSpinBox | QCheckBox] = {}
        self.text_field_widgets: dict[str, QLineEdit] = {}
        self.stat_widgets: dict[str, QSpinBox] = {}
        self.group_widgets: dict[str, list[QSpinBox]] = {}
        self.remove_widgets: dict[str, QCheckBox] = {}
        self.stats_data = save_api.browse("stats").get("stats", [])

        layout = QVBoxLayout(self)
        title = QLabel(item.get("name", "Item"))
        title.setObjectName("detailTitle")
        subtitle = QLabel("Edit item properties and simple one-value item stats")
        subtitle.setObjectName("detailSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        form = QFormLayout()
        self._add_check(form, "identified", "Identified", bool(item.get("identified", True)))
        self._add_check(form, "ethereal", "Ethereal", bool(item.get("ethereal", False)))
        self._add_check(form, "personalized", "Personalized", bool(item.get("personalized", False)))
        self._add_text(form, "personal_name", "Personalized Name", str(item.get("personal_name", "")), 15)
        self._add_spin(form, "ilvl", "Item Level", int(item.get("ilvl", 0)), 0, 127)
        if item.get("defense") is not None:
            self._add_spin(form, "defense", "Defense", int(item.get("defense", 0)), 0, 65535)
        if item.get("max_durability") is not None:
            self._add_spin(form, "current_durability", "Current Durability",
                           int(item.get("current_durability", 0)), 0, 65535)
            self._add_spin(form, "max_durability", "Maximum Durability",
                           int(item.get("max_durability", 0)), 0, 65535)
        if item.get("quantity") is not None:
            self._add_spin(form, "quantity", "Quantity", int(item.get("quantity", 0)), 0, 65535)
        self._add_spin(form, "num_sockets", "Sockets", int(item.get("num_sockets", 0)), 0, 15)
        layout.addLayout(form)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Stat", "Value", "Range", "Status", "Remove"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        for stat in item.get("stats", []):
            self._add_stat_row(stat)
        layout.addWidget(self.table, 1)

        add_box = QWidget()
        add_row = QHBoxLayout(add_box)
        add_row.setContentsMargins(0, 0, 0, 0)
        self.add_search = QLineEdit()
        self.add_search.setPlaceholderText("Search addable stats")
        self.add_stat = QComboBox()
        self.add_value_spins: list[QSpinBox] = []
        self.add_value_wrap = QWidget()
        self.add_value_row = QHBoxLayout(self.add_value_wrap)
        self.add_value_row.setContentsMargins(0, 0, 0, 0)
        self.add_search.textChanged.connect(self._refresh_add_stats)
        self.add_stat.currentIndexChanged.connect(self._sync_add_range)
        add_row.addWidget(self.add_search, 1)
        add_row.addWidget(self.add_stat, 2)
        add_row.addWidget(self.add_value_wrap, 2)
        layout.addWidget(add_box)
        self._refresh_add_stats()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_check(self, form: QFormLayout, key: str, label: str, value: bool):
        widget = QCheckBox()
        widget.setChecked(value)
        self.field_widgets[key] = widget
        form.addRow(label, widget)

    def _add_spin(self, form: QFormLayout, key: str, label: str, value: int, low: int, high: int):
        widget = QSpinBox()
        widget.setRange(low, min(high, 2147483647))
        widget.setValue(max(low, min(int(value), min(high, 2147483647))))
        self.field_widgets[key] = widget
        form.addRow(label, widget)

    def _add_text(self, form: QFormLayout, key: str, label: str, value: str, max_len: int):
        widget = QLineEdit(value)
        widget.setMaxLength(max_len)
        self.text_field_widgets[key] = widget
        form.addRow(label, widget)

    def _add_stat_row(self, stat: dict):
        row = self.table.rowCount()
        self.table.insertRow(row)
        label = stat.get("text") or stat.get("name") or f"Stat {stat.get('id')}"
        self.table.setItem(row, 0, QTableWidgetItem(str(label)))
        if stat.get("editable"):
            sid = str(stat.get("id"))
            components = stat.get("components") or []
            if len(components) > 1:
                wrap = QWidget()
                row_layout = QHBoxLayout(wrap)
                row_layout.setContentsMargins(0, 0, 0, 0)
                spins = []
                ranges = []
                for comp in components:
                    low = max(-2147483648, int(comp.get("min", 0)))
                    high = min(2147483647, int(comp.get("max", 0)))
                    row_layout.addWidget(QLabel(self._short_stat_name(comp.get("name", ""))))
                    spin = QSpinBox()
                    spin.setRange(low, high)
                    spin.setValue(max(low, min(high, int(comp.get("value", 0)))))
                    row_layout.addWidget(spin)
                    spins.append(spin)
                    ranges.append(f"{self._short_stat_name(comp.get('name', ''))}: {low} to {high}")
                row_layout.addStretch(1)
                self.group_widgets[sid] = spins
                self.table.setCellWidget(row, 1, wrap)
                self.table.setItem(row, 2, QTableWidgetItem("; ".join(ranges)))
            else:
                low = max(-2147483648, int(stat.get("min", 0)))
                high = min(2147483647, int(stat.get("max", 0)))
                spin = QSpinBox()
                spin.setRange(low, high)
                spin.setValue(max(low, min(high, int(stat.get("value", 0)))))
                self.stat_widgets[sid] = spin
                self.table.setCellWidget(row, 1, spin)
                self.table.setItem(row, 2, QTableWidgetItem(f"{low} to {high}"))
            self.table.setItem(row, 3, QTableWidgetItem("Editable"))
            remove = QCheckBox()
            self.remove_widgets[sid] = remove
            self.table.setCellWidget(row, 4, remove)
        else:
            values = stat.get("values") or [stat.get("value", 0)]
            self.table.setItem(row, 1, QTableWidgetItem(" / ".join(str(v) for v in values)))
            self.table.setItem(row, 2, QTableWidgetItem(""))
            self.table.setItem(row, 3, QTableWidgetItem("Complex stat"))
            self.table.setItem(row, 4, QTableWidgetItem(""))
        self.table.resizeColumnsToContents()

    def _short_stat_name(self, name: str) -> str:
        text = str(name or "value").replace("_", " ")
        for prefix in ("fire", "light", "magic", "cold", "poison", "item "):
            text = text.replace(prefix, "")
        text = text.replace("damage", "dmg").replace("length", "len")
        return text.strip().title() or "Value"

    def _refresh_add_stats(self):
        current = self.add_stat.currentData()
        needle = self.add_search.text().strip().lower()
        existing = {int(stat.get("id", -1)) for stat in self.item.get("stats", [])}
        rows = []
        for row in self.stats_data:
            sid = int(row.get("id", -1))
            label = f"{row.get('name', '')} [{sid}]"
            if sid in existing:
                continue
            if needle and needle not in label.lower():
                continue
            rows.append((label, row))
        self.add_stat.blockSignals(True)
        self.add_stat.clear()
        self.add_stat.addItem("No added stat", None)
        for label, row in rows[:500]:
            self.add_stat.addItem(label, row)
        if current:
            idx = self.add_stat.findData(current)
            if idx >= 0:
                self.add_stat.setCurrentIndex(idx)
        self.add_stat.blockSignals(False)
        self._sync_add_range()

    def _sync_add_range(self):
        while self.add_value_row.count():
            item = self.add_value_row.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.add_value_spins = []
        row = self.add_stat.currentData()
        if isinstance(row, dict):
            for comp in row.get("components", []) or [row]:
                low = max(-2147483648, int(comp.get("min", 0)))
                high = min(2147483647, int(comp.get("max", 0)))
                self.add_value_row.addWidget(QLabel(self._short_stat_name(comp.get("name", ""))))
                spin = QSpinBox()
                spin.setRange(low, high)
                spin.setValue(max(low, min(high, 1)))
                self.add_value_row.addWidget(spin)
                self.add_value_spins.append(spin)
            self.add_value_row.addStretch(1)
        else:
            self.add_value_row.addWidget(QLabel(""))

    def payload(self) -> dict:
        fields = {}
        for key, widget in self.field_widgets.items():
            if isinstance(widget, QCheckBox):
                fields[key] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                fields[key] = int(widget.value())
        for key, widget in self.text_field_widgets.items():
            fields[key] = widget.text()
        removed = {sid for sid, widget in self.remove_widgets.items() if widget.isChecked()}
        stats = {
            sid: int(widget.value())
            for sid, widget in self.stat_widgets.items()
            if sid not in removed
        }
        for sid, spins in self.group_widgets.items():
            if sid not in removed:
                stats[sid] = [int(spin.value()) for spin in spins]
        add_stats = []
        add_row = self.add_stat.currentData()
        if isinstance(add_row, dict):
            values = [int(spin.value()) for spin in self.add_value_spins]
            add_stats.append({
                "stat_id": int(add_row["id"]),
                "values": values,
            })
        return {
            "fields": fields,
            "stats": stats,
            "add_stats": add_stats,
            "remove_stats": sorted(removed, key=int),
        }


class ItemBuilderDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Item Builder")
        self.setMinimumWidth(1040)
        self.bases = save_api.browse("bases").get("bases", [])
        self.stats_data = save_api.browse("stats").get("stats", [])
        self.uniques = save_api.browse("uniques").get("uniques", [])
        self.sets = save_api.browse("sets").get("sets", [])
        self.magic_prefixes = save_api.browse("magic_prefixes").get("prefixes", [])
        self.magic_suffixes = save_api.browse("magic_suffixes").get("suffixes", [])
        self.rare_prefixes = save_api.browse("rare_prefixes").get("prefixes", [])
        self.rare_suffixes = save_api.browse("rare_suffixes").get("suffixes", [])
        self.runewords = save_api.browse("runewords").get("runewords", [])

        self.base = SearchableComboBox()
        for row in self.bases:
            self.base.addItem(f"{row.get('name') or row.get('code')} [{row.get('code')}]", row.get("code"))
        self.quality = SearchableComboBox()
        for label, value in [
            ("Normal", 2), ("Superior", 3), ("Magic", 4), ("Rare", 6),
            ("Set", 5), ("Unique", 7), ("Crafted", 8),
        ]:
            self.quality.addItem(label, value)

        # Uniques and sets are global + searchable; picking one snaps the base to match.
        self.unique = SearchableComboBox()
        for row in self.uniques:
            self.unique.addItem(
                f"{row.get('name')} · {row.get('code')} · lvl {row.get('level_req', 0)}",
                {"id": int(row.get("id", -1)), "code": row.get("code")})
        self.set_item = SearchableComboBox()
        for row in self.sets:
            self.set_item.addItem(
                f"{row.get('name')} · {row.get('set')} · {row.get('code')}",
                {"id": int(row.get("id", -1)), "code": row.get("code")})

        self.magic_prefix = SearchableComboBox()
        self.magic_suffix = SearchableComboBox()
        self.rare_prefix = SearchableComboBox()
        self.rare_suffix = SearchableComboBox()
        self.rare_affixes = [SearchableComboBox() for _ in range(6)]
        self._populate_affix_selectors()
        self.runeword = SearchableComboBox()
        self.runeword_info = QLabel("")
        self.runeword_info.setObjectName("slotCaption")
        self.runeword_info.setWordWrap(True)
        self.runeword_preview = QLabel("")
        self.runeword_preview.setWordWrap(True)

        # Any number of extra stats, from the full stat table.
        self.stat_rows: list[tuple[SearchableComboBox, QSpinBox, QWidget]] = []
        self.stats_host = QWidget()
        self.stats_layout = QVBoxLayout(self.stats_host)
        self.stats_layout.setContentsMargins(0, 0, 0, 0)
        self.stats_layout.setSpacing(4)
        self.add_stat = QPushButton("Add Stat")
        self.add_stat.clicked.connect(self._add_stat_row)

        self.auto_place = QCheckBox("Auto first free space")
        self.auto_place.setChecked(True)
        self.x = QSpinBox()
        self.x.setRange(0, INV_W - 1)
        self.y = QSpinBox()
        self.y.setRange(0, INV_H - 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)

        self.base.currentIndexChanged.connect(self.refresh_quality_options)
        self.base.currentIndexChanged.connect(self.refresh_runewords)
        self.quality.currentIndexChanged.connect(self.refresh_quality_options)
        self.unique.activated.connect(lambda _i: self._sync_base_from(self.unique))
        self.set_item.activated.connect(lambda _i: self._sync_base_from(self.set_item))
        self.runeword.currentIndexChanged.connect(self.refresh_quality_options)
        self.auto_place.toggled.connect(self.update_placement_mode)

        form = QFormLayout()
        self.form = form
        form.addRow("Base", self.base)
        form.addRow("Quality", self.quality)
        form.addRow("Unique", self.unique)
        form.addRow("Set Item", self.set_item)
        form.addRow("Magic Prefix", self.magic_prefix)
        form.addRow("Magic Suffix", self.magic_suffix)
        form.addRow("Rare Prefix", self.rare_prefix)
        form.addRow("Rare Suffix", self.rare_suffix)
        self.rare_wrap = QWidget()
        rare_row = QGridLayout(self.rare_wrap)
        rare_row.setContentsMargins(0, 0, 0, 0)
        for i, combo in enumerate(self.rare_affixes):
            rare_row.addWidget(combo, i // 2, i % 2)
        form.addRow("Rare Affixes", self.rare_wrap)
        form.addRow("Runeword", self.runeword)
        form.addRow("", self.runeword_info)
        stats_wrap = QWidget()
        stats_col = QVBoxLayout(stats_wrap)
        stats_col.setContentsMargins(0, 0, 0, 0)
        stats_col.addWidget(self.stats_host)
        stats_col.addWidget(self.add_stat, alignment=Qt.AlignLeft)
        form.addRow("Extra Stats", stats_wrap)

        placement = QWidget()
        place_row = QHBoxLayout(placement)
        place_row.setContentsMargins(0, 0, 0, 0)
        place_row.addWidget(self.auto_place)
        place_row.addWidget(QLabel("X"))
        place_row.addWidget(self.x)
        place_row.addWidget(QLabel("Y"))
        place_row.addWidget(self.y)
        place_row.addStretch(1)
        form.addRow("Inventory cell", placement)

        self.create = QPushButton("Create Item")
        self.create.clicked.connect(self.create_item)

        # live preview pane
        self.preview_icon = QLabel()
        self.preview_icon.setAlignment(Qt.AlignCenter)
        self.preview_name = QLabel("")
        self.preview_name.setAlignment(Qt.AlignCenter)
        self.preview_name.setObjectName("detailTitle")
        self.preview_name.setWordWrap(True)
        self.preview_body = QLabel("")
        self.preview_body.setWordWrap(True)
        self.preview_body.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        preview = QFrame()
        preview.setObjectName("panelFrame")
        preview.setMinimumWidth(280)
        pv = QVBoxLayout(preview)
        pv.setContentsMargins(14, 12, 14, 12)
        pv_title = QLabel("P R E V I E W")
        pv_title.setObjectName("panelTitle")
        pv_title.setAlignment(Qt.AlignCenter)
        pv.addWidget(pv_title)
        pv.addWidget(self.preview_icon)
        pv.addWidget(self.preview_name)
        pv.addWidget(self.runeword_preview)
        pv.addWidget(self.preview_body)
        pv.addStretch(1)

        left = QWidget()
        left_col = QVBoxLayout(left)
        left_col.setContentsMargins(0, 0, 0, 0)
        label = QLabel("Build an item from the selected Project Diablo 2 tables.")
        label.setWordWrap(True)
        left_col.addWidget(label)
        left_col.addLayout(form)
        left_col.addWidget(self.status)
        left_col.addWidget(self.create)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        left_col.addWidget(buttons)

        layout = QHBoxLayout(self)
        layout.addWidget(left, 3)
        layout.addWidget(preview, 2)

        self._add_stat_row()
        self.refresh_runewords()
        self.refresh_quality_options()
        self.update_placement_mode()

    def _populate_affix_selectors(self):
        self.magic_prefix.addItem("No prefix", 0)
        self.magic_suffix.addItem("No suffix", 0)
        for row in self.magic_prefixes:
            if int(row.get("spawnable", 0)) or int(row.get("rare", 0)):
                self.magic_prefix.addItem(self._affix_label(row), int(row.get("id", 0)))
        for row in self.magic_suffixes:
            if int(row.get("spawnable", 0)) or int(row.get("rare", 0)):
                self.magic_suffix.addItem(self._affix_label(row), int(row.get("id", 0)))
        for row in self.rare_prefixes:
            self.rare_prefix.addItem(row.get("name", ""), int(row.get("id", 0)))
        for row in self.rare_suffixes:
            self.rare_suffix.addItem(str(row.get("name", "")).title(), int(row.get("id", 0)))
        # Rare/crafted extra affix slots serialize only one 11-bit affix id. The
        # save format does not preserve whether the UI picked a prefix or suffix
        # table row, so expose prefix rows here; those round-trip by id/name.
        rare_magic = [
            ("MagicPrefix", row) for row in self.magic_prefixes
            if int(row.get("rare", 0)) or int(row.get("spawnable", 0))
        ]
        for combo in self.rare_affixes:
            combo.addItem("No extra affix", "")
            for table, row in rare_magic:
                combo.addItem(self._affix_label(row), {"table": table, "id": int(row.get("id", 0))})

    def _property_stats(self) -> dict[str, str]:
        """property code (affix mod) -> primary stat name, from properties.txt."""
        if not hasattr(self, "_prop_stats_cache"):
            cache = {}
            try:
                for row in save_api.tables().load_table("properties"):
                    code = (row.get("code") or "").strip()
                    stat = (row.get("stat1") or "").strip()
                    if code:
                        cache[code] = stat
            except Exception:  # noqa: BLE001
                pass
            self._prop_stats_cache = cache
        return self._prop_stats_cache

    def _affix_label(self, row: dict) -> str:
        mods = []
        for mod in row.get("mods", [])[:2]:
            code = mod.get("code", "")
            if not code:
                continue
            lo = int(mod.get("min", 0))
            hi = int(mod.get("max", 0))
            value = f"{lo}-{hi}" if lo != hi else str(hi)
            stat = self._property_stats().get(code)
            label = self._display_stat_name(stat) if stat else code
            mods.append(label.replace("#", value) if "#" in label else f"{label} {value}")
        suffix = f" · {', '.join(mods)}" if mods else ""
        return f"{row.get('name', '')}{suffix}"

    def _runeword_label(self, row: dict) -> str:
        runes = " ".join(r.replace(" Rune", "") for r in (row.get("rune_names") or row.get("runes") or []))
        types = ", ".join(row.get("types") or [])
        return f"{row.get('name', '')} · {runes} · {types}"

    def _selected_base_row(self) -> dict | None:
        code = self.base.currentData()
        return next((row for row in self.bases if row.get("code") == code), None)

    def _runeword_matches_base(self, row: dict, base: dict | None) -> bool:
        if not base:
            return False
        base_types = set(base.get("type_codes") or [])
        allowed = set(row.get("allowed_types") or [])
        excluded = set(row.get("excluded_types") or [])
        if allowed and not (allowed & base_types):
            return False
        if excluded & base_types:
            return False
        max_sockets = int(base.get("max_sockets") or 0)
        if max_sockets and int(row.get("sockets") or 0) > max_sockets:
            return False
        return True

    def refresh_runewords(self):
        """Runewords are filtered to what the selected base can hold."""
        current = None
        data = self.runeword.currentData()
        if isinstance(data, dict):
            current = data.get("id")
        base = self._selected_base_row()
        self.runeword.blockSignals(True)
        self.runeword.clear()
        self.runeword.addItem("No runeword", None)
        rows = [row for row in self.runewords if self._runeword_matches_base(row, base)]
        for row in rows:
            self.runeword.addItem(self._runeword_label(row), row)
        if current is not None:
            for idx in range(self.runeword.count()):
                row = self.runeword.itemData(idx)
                if isinstance(row, dict) and row.get("id") == current:
                    self.runeword.setCurrentIndex(idx)
                    break
        self.runeword.blockSignals(False)
        base_name = (base or {}).get("name") or (base or {}).get("code") or "no base"
        self.runeword_info.setText(
            f"{len(rows)} of {len(self.runewords)} runewords fit {base_name}. "
            "Sockets and runes are added automatically — no socket count needed.")

    def _sync_base_from(self, combo: QComboBox):
        """Picking a unique/set snaps the base combo to that item's base."""
        data = combo.currentData()
        if not isinstance(data, dict):
            return
        code = data.get("code")
        for idx in range(self.base.count()):
            if self.base.itemData(idx) == code:
                self.base.setCurrentIndex(idx)
                break
        self._update_preview()

    @staticmethod
    def _display_stat_name(name: str) -> str:
        """In-game display text for a stat: the game's own string table first,
        then our label templates, then a prettified code name."""
        label = save_api.stat_display_labels().get(name)
        if label:
            return label
        template = save_api.STAT_LABELS.get(name)
        if template:
            return template.replace("{v}", "#")
        return name.removeprefix("item_").replace("_", " ").title()

    def _stat_label(self, row: dict) -> str:
        lo, hi = int(row.get("min", 0)), int(row.get("max", 0))
        return f"{self._display_stat_name(row.get('name', ''))}  ({lo}–{hi})"

    def _add_stat_row(self):
        combo = SearchableComboBox()
        combo.addItem("No stat", None)
        # sort by the *displayed* (game string-table) name, which can differ from
        # the internal stat name browse() sorts by (e.g. "maxhp" -> "Life").
        for row in sorted(self.stats_data,
                          key=lambda r: self._display_stat_name(r.get("name", "")).lower()):
            combo.addItem(self._stat_label(row), row)
        value = QSpinBox()
        value.setRange(-999999, 999999)
        value.setValue(1)
        value.setFixedWidth(90)
        remove = QPushButton("✕")
        remove.setFixedWidth(30)
        remove.setStyleSheet("padding: 4px 2px;")
        host = QWidget()
        row_l = QHBoxLayout(host)
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.setSpacing(4)
        row_l.addWidget(combo, 1)
        row_l.addWidget(value)
        row_l.addWidget(remove)
        entry = (combo, value, host)
        self.stat_rows.append(entry)
        self.stats_layout.addWidget(host)
        combo.currentIndexChanged.connect(lambda _i, c=combo, v=value: self._stat_row_changed(c, v))
        value.valueChanged.connect(lambda _v: self._update_preview())
        remove.clicked.connect(lambda _c=False, e=entry: self._remove_stat_row(e))

    def _remove_stat_row(self, entry):
        if entry in self.stat_rows:
            self.stat_rows.remove(entry)
            entry[2].deleteLater()
            self._update_preview()

    def _stat_row_changed(self, combo, value):
        row = combo.currentData()
        if isinstance(row, dict):
            value.setMaximum(max(1, int(row.get("max", 999999))))
            value.setEnabled(True)
        else:
            value.setEnabled(False)
        self._update_preview()

    def _selected_stats(self) -> list[dict]:
        out = []
        for combo, value, _host in self.stat_rows:
            row = combo.currentData()
            if isinstance(row, dict):
                out.append({"stat_id": int(row["id"]), "value": int(value.value()),
                            "_name": row["name"]})
        return out

    def refresh_quality_options(self):
        quality = int(self.quality.currentData() or 2)
        runeword = self.runeword.currentData()
        has_runeword = isinstance(runeword, dict)
        # hide whole rows (label + field) that don't apply to this quality
        self.form.setRowVisible(self.unique, quality == 7 and not has_runeword)
        self.form.setRowVisible(self.set_item, quality == 5 and not has_runeword)
        self.form.setRowVisible(self.magic_prefix, quality == 4 and not has_runeword)
        self.form.setRowVisible(self.magic_suffix, quality == 4 and not has_runeword)
        self.form.setRowVisible(self.rare_prefix, quality in (6, 8) and not has_runeword)
        self.form.setRowVisible(self.rare_suffix, quality in (6, 8) and not has_runeword)
        self.form.setRowVisible(self.rare_wrap, quality in (6, 8) and not has_runeword)

        if has_runeword:
            rune_names = " + ".join(runeword.get("rune_names") or runeword.get("runes") or [])
            stat_lines = [stat.get("text", "") for stat in (runeword.get("stats") or [])[:5]]
            more = ""
            if len(runeword.get("stats") or []) > 5:
                more = f"\n+ {len(runeword.get('stats') or []) - 5} more generated simple stat(s)"
            self.runeword_preview.setText(
                f"{runeword.get('sockets', 0)} sockets: {rune_names}\n"
                + "\n".join(stat_lines)
                + more
            )
            self.status.setText("Runeword creation uses a normal/superior socketed base and writes rune children in order.")
        else:
            self.runeword_preview.setText("")
        self._update_preview()

    def _update_preview(self):
        quality = int(self.quality.currentData() or 2)
        qname = {2: "normal", 3: "superior", 4: "magic", 6: "rare",
                 5: "set", 7: "unique", 8: "crafted"}.get(quality, "normal")
        base = self._selected_base_row() or {}
        base_name = base.get("name") or base.get("code") or "—"
        runeword = self.runeword.currentData()
        if isinstance(runeword, dict):
            name, qname = runeword.get("name", base_name), "unique"
        elif quality == 7 and isinstance(self.unique.currentData(), dict):
            name = self.unique.currentText().split(" · ")[0]
        elif quality == 5 and isinstance(self.set_item.currentData(), dict):
            name = self.set_item.currentText().split(" · ")[0]
        elif quality == 4:
            pre = self.magic_prefix.currentText().split(" · ")[0] if self.magic_prefix.currentData() else ""
            suf = self.magic_suffix.currentText().split(" · ")[0] if self.magic_suffix.currentData() else ""
            name = " ".join(p for p in [pre if pre != "No prefix" else "", base_name,
                                        suf if suf != "No suffix" else ""] if p)
        else:
            name = base_name
        color = quality_color(qname).name()
        self.preview_name.setText(f"<span style='color:{color}'>{name}</span>")
        pseudo = {"quality": qname, "type_label": base.get("name", ""),
                  "category": base.get("cat", ""), "base_name": base_name}
        self.preview_icon.setPixmap(ItemIcon.pixmap(pseudo, QSize(96, 96)))
        lines = [f"{qname.title()} · {base_name}"]
        for stat in self._selected_stats():
            label = self._display_stat_name(stat["_name"])
            value = str(stat["value"])
            lines.append(label.replace("#", value) if "#" in label
                         else f"+{value} {label}")
        self.preview_body.setText("<br>".join(lines))

    def update_placement_mode(self):
        manual = not self.auto_place.isChecked()
        self.x.setEnabled(manual)
        self.y.setEnabled(manual)

    def create_item(self):
        main = self.parent()
        if not isinstance(main, MainWindow) or not main.loaded:
            return
        code = self.base.currentData()
        if not code:
            self.status.setText("Choose an item base.")
            return
        stats = [{"stat_id": s["stat_id"], "value": s["value"]} for s in self._selected_stats()]
        payload = {
            "path": main.loaded.path,
            "code": code,
            "quality": int(self.quality.currentData()),
            "stats": stats,
        }
        if not self.auto_place.isChecked():
            payload["x"] = int(self.x.value())
            payload["y"] = int(self.y.value())
        quality = int(self.quality.currentData())
        runeword = self.runeword.currentData()
        if isinstance(runeword, dict):
            payload["quality"] = quality if quality in (2, 3) else 2
            payload["runeword_id"] = int(runeword.get("id", -1))
        elif quality == 7:
            data = self.unique.currentData()
            if not isinstance(data, dict):
                self.status.setText("Choose a unique item.")
                return
            payload["unique_id"] = int(data["id"])
        elif quality == 5:
            data = self.set_item.currentData()
            if not isinstance(data, dict):
                self.status.setText("Choose a set item.")
                return
            payload["set_id"] = int(data["id"])
        elif quality == 4:
            payload["magic_prefix"] = int(self.magic_prefix.currentData() or 0)
            payload["magic_suffix"] = int(self.magic_suffix.currentData() or 0)
        elif quality in (6, 8):
            payload["rare_prefix"] = int(self.rare_prefix.currentData() or 156)
            payload["rare_suffix"] = int(self.rare_suffix.currentData() or 0)
            payload["rare_affixes"] = []
            for combo in self.rare_affixes:
                data = combo.currentData()
                if data in (None, ""):
                    continue
                if isinstance(data, dict):
                    payload["rare_affixes"].append(data)
                else:
                    payload["rare_affixes"].append({"id": int(data)})
        if main.create_item(payload):
            self.accept()


class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(780)
        self.settings = settings
        self.mpq = QLineEdit(settings.value("paths/mpq", ""))
        self.save = QLineEdit(settings.value("paths/save", ""))
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.mpq.textChanged.connect(self._refresh_status)
        self.save.textChanged.connect(self._refresh_status)

        layout = QVBoxLayout(self)
        title = QLabel("Settings")
        title.setObjectName("setupTitle")
        layout.addWidget(title)

        tabs = QTabWidget()
        paths_tab = QWidget()
        paths_layout = QVBoxLayout(paths_tab)
        form = QFormLayout()
        form.addRow("Project Diablo 2 data MPQ", self._path_row(self.mpq, self.pick_mpq))
        form.addRow("Character save or stash", self._path_row(self.save, self.pick_save))
        paths_layout.addLayout(form)
        hint = QLabel("The MPQ provides names, stat tables, item art, and Project Diablo 2 data. The save path is the character or stash opened when the editor starts.")
        hint.setWordWrap(True)
        paths_layout.addWidget(hint)
        paths_layout.addStretch(1)
        tabs.addTab(paths_tab, "Files")

        behavior_tab = QWidget()
        behavior_layout = QVBoxLayout(behavior_tab)
        write_hint = QLabel("Edits write a sibling *.edited file and then switch the editor to that new file. Source saves are left untouched.")
        write_hint.setWordWrap(True)
        behavior_layout.addWidget(write_hint)
        behavior_layout.addStretch(1)
        tabs.addTab(behavior_tab, "Behavior")
        layout.addWidget(tabs, 1)

        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_status()

    def _path_row(self, edit: QLineEdit, picker):
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        btn = QPushButton("Browse...")
        btn.clicked.connect(picker)
        row.addWidget(btn)
        return w

    def _start_dir(self, edit: QLineEdit) -> str:
        text = edit.text().strip()
        if os.path.isdir(text):
            return text
        if text and os.path.isdir(os.path.dirname(text)):
            return os.path.dirname(text)
        return os.path.expanduser("~")

    def pick_mpq(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select pd2data.mpq", self._start_dir(self.mpq),
            "MPQ archives (*.mpq);;All files (*)")
        if path:
            self.mpq.setText(path)
            self._refresh_status()

    def pick_save(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Diablo II save", self._start_dir(self.save),
            "Diablo II saves (*.d2s *.d2x *.sss *.stash);;All files (*)")
        if path:
            self.save.setText(path)
            self._refresh_status()

    def _refresh_status(self):
        mpq_ok = os.path.isfile(self.mpq.text().strip())
        save_ok = os.path.isfile(self.save.text().strip())
        mpq_state = "ready" if mpq_ok else "missing"
        save_state = "ready" if save_ok else "missing"
        self.status.setText(f"MPQ: {mpq_state}   Save: {save_state}")

    def accept(self):
        mpq = self.mpq.text().strip()
        save = self.save.text().strip()
        if not os.path.isfile(mpq):
            self.status.setText("Select a valid Project Diablo 2 data MPQ.")
            return
        if not os.path.isfile(save):
            self.status.setText("Select a valid character save or stash.")
            return
        self.settings.setValue("paths/mpq", mpq)
        self.settings.setValue("paths/save", save)
        super().accept()


class _WarmWorker(QThread):
    done = Signal(bool, str)

    def run(self):
        try:
            save_api.warm()
            self.done.emit(True, "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(False, f"{type(e).__name__}: {e}")


class _SaveWorker(QThread):
    done = Signal(dict)

    def run(self):
        try:
            self.done.emit(save_api.commit_all())
        except Exception as e:  # noqa: BLE001
            self.done.emit({"ok": False, "results": [{"error": str(e)}]})


class _ValidateWorker(QThread):
    done = Signal(int, dict)   # (revision-validated, result)

    def __init__(self, path: str, rev: int, parent=None):
        super().__init__(parent)
        self._path = path
        self._rev = rev

    def run(self):
        try:
            res = save_api.validate_buffer(self._path)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "errors": [str(e)]}
        self.done.emit(self._rev, res)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(APP_ORG, APP_NAME)
        self.loaded: LoadedSave | None = None
        self.selected_index: int | None = None
        self.selected_stash_page: int | None = None
        self.selected_stash_index: int | None = None
        self.selected_other_section: str | None = None
        self.selected_other_section_name: str | None = None
        self.selected_other_index: int | None = None
        self.selected_other_editable: bool = False
        self._warm_gen = 0
        self.setWindowTitle("Cain")
        self.setWindowIcon(app_icon())
        self.resize(1150, 810)
        self._build_ui()
        self._last_rev = 0
        self._validated_rev = 0
        self._validating = False
        self._poll = QTimer(self)
        self._poll.setInterval(250)
        self._poll.timeout.connect(self._poll_tick)
        self._poll.start()
        self._build_menu()
        if not self._ensure_setup():
            raise SystemExit(0)
        self.load_current()

    def _build_menu(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(toolbar)
        self.save_action = QAction("Save", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)  # Ctrl+S
        self.save_action.triggered.connect(self.save_now)
        self.save_action.setEnabled(False)
        toolbar.addAction(self.save_action)
        for text, fn in [
            ("Open Save", self.pick_save),
            ("Open MPQ", self.pick_mpq),
            ("Settings", self.open_settings),
            ("Validate", self.validate_save),
            ("Item Builder", self.open_builder),
        ]:
            act = QAction(text, self)
            act.triggered.connect(fn)
            toolbar.addAction(act)

    def _build_ui(self):
        self.header = QLabel("")
        self.header.setObjectName("characterHeader")
        self.tabs = QTabWidget()

        self.inventory = InventoryGrid()
        self.inventory.item_selected.connect(self.select_item)
        self.inventory.item_moved.connect(self.move_item_ui)
        self.equipment = EquipmentPanel()
        self.equipment.item_selected.connect(self.select_item)
        self.equipment.item_dropped.connect(self.equip_item_ui)
        self.belt = BeltPanel()
        self.belt.item_selected.connect(self.select_item)
        self.character_stats = CharacterStatsPanel()
        self.character_stats.save_requested.connect(self.save_character_stats)
        self.skills = SkillsPanel()
        self.skills.save_requested.connect(self.save_skills)
        self.waypoints = WaypointsPanel()
        self.waypoints.save_requested.connect(self.save_waypoints)
        self.quests = QuestsPanel()
        self.quests.save_requested.connect(self.save_quests)
        self.stash_sources = []
        self.stash_pages = []
        self.other_sections = []
        self.stash_source = QComboBox()
        self.stash_source.currentIndexChanged.connect(self._stash_source_changed)
        self.stash_tabs = QTabBar()
        self.stash_tabs.setExpanding(False)
        self.stash_tabs.currentChanged.connect(lambda _i: self.render_stash_page())
        self.stash_grid = StashGrid()
        self.stash_grid.item_selected.connect(self.select_stash_item)
        self.stash_grid.item_moved.connect(self.move_stash_item)
        self.detail = DetailPanel()
        self.detail.max_roll.clicked.connect(self.max_roll_selected)
        self.detail.move_inventory.clicked.connect(self.move_selected_to_inventory)
        self.detail.equip.clicked.connect(self.equip_selected)
        self.detail.edit.clicked.connect(self.edit_selected_item)
        self.detail.socket.clicked.connect(self.socket_selected_item)
        self.detail.unsocket.clicked.connect(self.unsocket_selected_item)
        self.detail.duplicate.clicked.connect(self.duplicate_selected_item)
        self.detail.copy_stash.clicked.connect(self.copy_selected_to_stash)
        self.detail.copy_character.clicked.connect(self.copy_selected_to_character)
        self.detail.delete.clicked.connect(self.delete_selected_item)
        self.detail.build.clicked.connect(self.open_builder)

        eq_panel = QFrame()
        eq_panel.setObjectName("panelFrame")
        eq_layout = QVBoxLayout(eq_panel)
        eq_layout.setContentsMargins(12, 8, 12, 10)
        eq_layout.setSpacing(7)
        eq_title = QLabel("E Q U I P P E D")
        eq_title.setObjectName("panelTitle")
        eq_title.setAlignment(Qt.AlignCenter)
        eq_layout.addWidget(eq_title)
        eq_layout.addWidget(self.equipment, alignment=Qt.AlignHCenter)
        eq_layout.addWidget(self.inventory, alignment=Qt.AlignHCenter)
        belt_host = QWidget()
        belt_host.setObjectName("hostTransparent")
        belt_row = QHBoxLayout(belt_host)
        belt_row.setContentsMargins(0, 0, 0, 0)
        belt_cap = QLabel("BELT")
        belt_cap.setObjectName("slotCaption")
        belt_row.addWidget(belt_cap)
        belt_row.addWidget(self.belt, 1)
        eq_layout.addWidget(belt_host, alignment=Qt.AlignHCenter)
        eq_layout.addStretch(1)

        left_scroll = QScrollArea()
        left_scroll.setWidget(eq_panel)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)

        splitter = QSplitter()
        splitter.addWidget(left_scroll)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        self.items_list = QListWidget()
        self.items_list.itemClicked.connect(lambda item: self.select_item(item.data(Qt.UserRole)))
        self.other_list = QListWidget()
        self.other_list.itemClicked.connect(lambda item: self.select_other_item(item.data(Qt.UserRole)))

        char_tab = QWidget()
        char_layout = QVBoxLayout(char_tab)
        char_layout.addWidget(self.header)
        char_layout.addWidget(splitter, 1)

        all_tab = QWidget()
        all_layout = QVBoxLayout(all_tab)
        all_layout.addWidget(self.items_list)

        other_tab = QWidget()
        other_layout = QVBoxLayout(other_tab)
        other_layout.addWidget(self.other_list)

        stash_tab = QWidget()
        stash_layout = QVBoxLayout(stash_tab)
        source_row = QHBoxLayout()
        source_lbl = QLabel("STASH")
        source_lbl.setObjectName("slotCaption")
        source_row.addWidget(source_lbl)
        source_row.addWidget(self.stash_source, 1)
        stash_layout.addLayout(source_row)
        stash_layout.addWidget(self.stash_tabs)
        stash_layout.addWidget(self.stash_grid, alignment=Qt.AlignTop | Qt.AlignLeft)
        stash_layout.addStretch(1)

        def scrolled(widget: QWidget) -> QScrollArea:
            area = QScrollArea()
            area.setWidget(widget)
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            return area

        self.tabs.addTab(char_tab, "Character")
        self.tabs.addTab(scrolled(self.character_stats), "Stats")
        self.tabs.addTab(scrolled(self.skills), "Skills")
        self.tabs.addTab(self.waypoints, "Waypoints")
        self.tabs.addTab(self.quests, "Quests")
        self.tabs.addTab(scrolled(stash_tab), "Stash")
        self.tabs.addTab(other_tab, "Mercenary")
        self.tabs.addTab(all_tab, "All Items")

        self.validation_banner = QLabel("")
        self.validation_banner.setObjectName("validationBanner")
        self.validation_banner.setStyleSheet(
            "#validationBanner { background:#5a1e1e; color:#ffd9d9; padding:6px 10px; }")
        self.validation_banner.setVisible(False)
        self.validation_banner.setCursor(Qt.PointingHandCursor)
        self.validation_banner.mousePressEvent = lambda _e: self._show_validation_details()
        self._validation_errors: list[str] = []

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.validation_banner)
        central_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

    def _ensure_setup(self) -> bool:
        mpq = self.settings.value("paths/mpq", "")
        save = self.settings.value("paths/save", "")
        if os.path.isfile(mpq) and os.path.isfile(save):
            return True
        dlg = SetupDialog(self.settings, self)
        return dlg.exec() == QDialog.Accepted

    def open_settings(self):
        old_mpq = self.settings.value("paths/mpq", "")
        old_save = self.settings.value("paths/save", "")
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.Accepted:
            new_mpq = self.settings.value("paths/mpq", "")
            new_save = self.settings.value("paths/save", "")
            if new_mpq != old_mpq or new_save != old_save:
                if not self._confirm_lose_changes():
                    # user kept unsaved edits (or save failed) — revert the path
                    # change so QSettings stays consistent with the loaded file
                    self.settings.setValue("paths/mpq", old_mpq)
                    self.settings.setValue("paths/save", old_save)
                    return
                self.load_current()

    def _confirm_lose_changes(self) -> bool:
        """Return True if it's safe to proceed (no dirt, or user saved/discarded).
        False means cancel the pending action."""
        if not save_api.dirty_paths():
            return True
        choice = QMessageBox.question(
            self, "Unsaved changes",
            "You have unsaved changes. Save them before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Discard:
            for p in list(save_api.dirty_paths()):
                save_api.discard(p)
            self._update_title()
            return True
        # Save: commit synchronously (modal dialog above keeps the UI blocked);
        # we need the result before closing/switching.
        return self._report_save_result(save_api.commit_all())

    def _report_save_result(self, res: dict) -> bool:
        """Show validator errors on failure; refresh title on success.
        Returns whether the save succeeded."""
        if not res.get("ok"):
            errs = []
            for r in res.get("results", []):
                if r.get("error"):
                    errs.append(r["error"])
                    errs.extend(r.get("details", []) or [])
            QMessageBox.warning(self, "Save failed",
                                "The edit did not validate and was not written:\n\n"
                                + "\n".join(errs[:20]))
            return False
        self._update_title()
        return True

    def closeEvent(self, event):
        if self._confirm_lose_changes():
            event.accept()
        else:
            event.ignore()

    def pick_mpq(self):
        if not self._confirm_lose_changes():
            return
        current = self.settings.value("paths/mpq", "")
        start = os.path.dirname(current) if current and os.path.isdir(os.path.dirname(current)) else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project Diablo 2 data MPQ", start,
            "MPQ archives (*.mpq);;All files (*)")
        if path:
            self.settings.setValue("paths/mpq", path)
            self.load_current()

    def pick_save(self):
        if not self._confirm_lose_changes():
            return
        start = self.settings.value("paths/save", "") or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Diablo II save", start,
            "Diablo II saves (*.d2s *.d2x *.sss *.stash);;All files (*)")
        if path:
            self.settings.setValue("paths/save", path)
            self.load_current()

    def load_current(self):
        global ASSETS
        mpq = self.settings.value("paths/mpq", "")
        try:
            save_api.set_mpq(mpq)
            ASSETS = DiabloAssetLoader(mpq)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not load MPQ", str(e))
            return
        if save_api.is_warm():
            self._load_save_now()
            return
        self._warm_gen = getattr(self, "_warm_gen", 0) + 1
        gen = self._warm_gen
        self.statusBar().showMessage("Loading game data…")
        self._warm = _WarmWorker(self)
        self._warm.done.connect(lambda ok, msg, g=gen: self._on_warm_done(ok, msg, g))
        self._warm.start()

    def _on_warm_done(self, ok: bool, msg: str, gen: int):
        if gen != getattr(self, "_warm_gen", 0):
            return  # superseded by a newer load_current
        if not ok:
            QMessageBox.critical(self, "Could not load game data",
                                 "Failed to read tables from the MPQ." + (f"\n\n{msg}" if msg else ""))
            return
        self.statusBar().clearMessage()
        self._load_save_now()

    def _load_save_now(self):
        path = self.settings.value("paths/save", "")
        try:
            data = save_api.parse_save(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not load save", str(e))
            return
        self.loaded = LoadedSave(path=path, data=data)
        self._last_rev = 0
        self._validated_rev = 0
        self._validating = False
        self._validation_errors = []
        self.validation_banner.setVisible(False)
        self.render_save()

    def _update_title(self):
        dirty = bool(save_api.dirty_paths())
        if self.loaded:
            name = os.path.basename(self.loaded.path)
            self.setWindowTitle(f"{'*' if dirty else ''}{name} — Cain")
        else:
            self.setWindowTitle("Cain")
        self.save_action.setEnabled(dirty)

    def _poll_tick(self):
        self._update_title()
        if not self.loaded or not save_api.is_warm():
            return
        path = self.loaded.path
        rev = save_api.revision(path)
        if rev == self._last_rev:
            # buffer settled; validate once per new revision
            if rev != self._validated_rev and not self._validating:
                self._validating = True
                self._validator = _ValidateWorker(path, rev, self)
                self._validator.done.connect(self._on_validated)
                self._validator.start()
        self._last_rev = rev

    def _on_validated(self, rev: int, res: dict):
        self._validating = False
        self._validated_rev = rev
        if res.get("ok"):
            self._validation_errors = []
            self.validation_banner.setVisible(False)
        else:
            self._validation_errors = list(res.get("errors", []))
            n = len(self._validation_errors)
            self.validation_banner.setText(
                f"⚠ {n} validation issue{'s' if n != 1 else ''} — click for details")
            self.validation_banner.setVisible(True)

    def _show_validation_details(self):
        if not self._validation_errors:
            return
        QMessageBox.warning(self, "Validation issues",
                            "\n".join(self._validation_errors[:40]))

    def save_now(self):
        if not save_api.dirty_paths():
            return
        self._save_progress = QProgressDialog("Saving…", "", 0, 0, self)
        self._save_progress.setWindowTitle("Cain")
        self._save_progress.setCancelButton(None)
        self._save_progress.setWindowModality(Qt.WindowModal)
        self._save_progress.setMinimumDuration(0)
        self.setEnabled(False)            # block edits during the write
        self._save_progress.show()
        self._saver = _SaveWorker(self)
        self._saver.done.connect(self._on_save_done)
        self._saver.start()

    def _on_save_done(self, res: dict):
        self.setEnabled(True)
        self._save_progress.close()
        if self._report_save_result(res):
            self.statusBar().showMessage("Saved", 5000)

    def render_save(self):
        if not self.loaded:
            return
        data = self.loaded.data
        if data.get("kind") != "character":
            self.header.setText(f"{data.get('kind')} · {self.loaded.path}")
            items = []
            for page in data.get("pages", []):
                items.extend(page.get("items", []))
            indexed = list(enumerate(items))
        else:
            self.header.setText(
                f"{data.get('name')} · {data.get('class')} level {data.get('level')} · "
                f"{data.get('clean')}/{data.get('item_count')} decoded · {self.loaded.path}"
            )
            indexed = list(enumerate(data.get("items", [])))

        inv = [(i, it) for i, it in indexed if int(it.get("location", 0)) == 0 and int(it.get("panel", 0)) == 1]
        equipped = [(i, it) for i, it in indexed if int(it.get("location", 0)) == 1]
        belt = [(i, it) for i, it in indexed if int(it.get("location", 0)) == 2]
        self.inventory.set_items(inv)
        self.equipment.set_items(equipped)
        self.belt.set_items(belt)
        self.character_stats.set_stats(data.get("character_stats") if data.get("kind") == "character" else None)
        self.skills.set_skills(
            data.get("skills") if data.get("kind") == "character" else None,
            data.get("class"))
        self.waypoints.set_waypoints(data.get("waypoints") if data.get("kind") == "character" else None)
        self.quests.set_quests(data.get("quests") if data.get("kind") == "character" else None)
        self.set_stash_sources(self._gather_stash_sources())
        self.set_other_sections(data.get("item_sections", []) if data.get("kind") == "character" else [])
        self.items_list.clear()
        for i, it in indexed:
            item = QListWidgetItem(QIcon(ItemIcon.pixmap(it, QSize(48, 48))), it.get("name", ""))
            item.setData(Qt.UserRole, i)
            item.setToolTip("\n".join(s.get("text", "") for s in it.get("stats", [])))
            self.items_list.addItem(item)
        self.selected_index = None
        self.selected_stash_page = None
        self.selected_stash_index = None
        self.selected_other_section = None
        self.selected_other_section_name = None
        self.selected_other_index = None
        self.selected_other_editable = False
        self.detail.show_item(None)
        self._update_title()

    def set_other_sections(self, sections: list[dict]):
        self.other_sections = [sec for sec in sections if sec.get("id") != "player" and sec.get("items")]
        self.other_list.clear()
        if not self.other_sections:
            item = QListWidgetItem("No mercenary, corpse, or golem items found")
            item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.other_list.addItem(item)
            return
        for sec_idx, section in enumerate(self.other_sections):
            header = QListWidgetItem(f"{section.get('name', 'Section')} ({section.get('count', len(section.get('items', [])))})")
            header.setFlags(header.flags() & ~Qt.ItemIsSelectable)
            self.other_list.addItem(header)
            for item_idx, it in enumerate(section.get("items", [])):
                label = f"  {it.get('name', it.get('type_code', 'item'))}"
                row = QListWidgetItem(QIcon(ItemIcon.pixmap(it, QSize(48, 48))), label)
                row.setData(Qt.UserRole, (sec_idx, item_idx))
                row.setToolTip("\n".join(s.get("text", "") for s in it.get("stats", [])))
                self.other_list.addItem(row)

    @staticmethod
    def _tag_stash_pages(pages: list[dict], source_path: str) -> list[dict]:
        label = os.path.basename(source_path)
        for i, page in enumerate(pages):
            page["_source"] = source_path
            page["_page_idx"] = i
            page["_source_label"] = label
        return pages

    def _gather_stash_sources(self) -> list[dict]:
        """Every stash next to the loaded file: Characters (PlugY .d2x per
        character) and Shared (PD2 .stash, LOD .sss)."""
        folder = os.path.dirname(os.path.abspath(self.loaded.path))
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            names = []
        characters, shared = [], []
        for n in names:
            path = os.path.join(folder, n)
            low = n.lower()
            if low.endswith(".d2x"):
                characters.append((os.path.splitext(n)[0], path))
            elif low.endswith(".stash"):
                shared.append(("PD2", path))
            elif low.endswith(".sss"):
                shared.append(("LOD", path))
        # a directly-opened stash that lives elsewhere still gets a source
        lp = self.loaded.path
        if str(self.loaded.data.get("kind", "")).startswith("stash:") and \
                not any(p == lp for _l, p in characters + shared):
            label = os.path.splitext(os.path.basename(lp))[0]
            (shared if lp.lower().endswith((".sss", ".stash")) else characters).insert(0, (label, lp))
        sources = []
        for group, entries in [("Characters", characters), ("Shared", shared)]:
            for label, path in entries:
                try:
                    data = save_api.parse_save(path)
                except Exception:  # noqa: BLE001
                    continue
                if not str(data.get("kind", "")).startswith("stash:"):
                    continue
                pages = self._tag_stash_pages(data.get("pages", []), path)
                sources.append({"group": group, "label": label, "path": path, "pages": pages})
        return sources

    def set_stash_sources(self, sources: list[dict]):
        self.stash_sources = sources
        self.stash_source.blockSignals(True)
        self.stash_source.clear()
        model = self.stash_source.model()
        current_group = None
        preferred = None
        loaded_stem = os.path.splitext(os.path.basename(self.loaded.path))[0] if self.loaded else ""
        for i, src in enumerate(sources):
            if src["group"] != current_group:
                current_group = src["group"]
                self.stash_source.addItem(current_group)
                model.item(self.stash_source.count() - 1).setEnabled(False)
            self.stash_source.addItem(f"   {src['label']}", i)
            if preferred is None and (src["path"] == (self.loaded.path if self.loaded else "")
                                      or src["label"] == loaded_stem):
                preferred = self.stash_source.count() - 1
        self.stash_source.blockSignals(False)
        if self.stash_source.count():
            # land on the loaded stash / current character, else first real entry
            idx = preferred if preferred is not None else next(
                (i for i in range(self.stash_source.count())
                 if self.stash_source.itemData(i) is not None), 0)
            self.stash_source.setCurrentIndex(idx)
        self._stash_source_changed()

    def _stash_source_changed(self):
        src_idx = self.stash_source.currentData()
        if src_idx is None or not (0 <= int(src_idx) < len(self.stash_sources)):
            self.stash_pages = []
        else:
            self.stash_pages = self.stash_sources[int(src_idx)]["pages"]
        self.stash_tabs.blockSignals(True)
        while self.stash_tabs.count():
            self.stash_tabs.removeTab(0)
        for i, page in enumerate(self.stash_pages):
            name = page.get("name") or f"Page {page.get('_page_idx', i) + 1}"
            self.stash_tabs.addTab(f"{name} ({page.get('count', 0)})")
        self.stash_tabs.blockSignals(False)
        if self.stash_tabs.count():
            self.stash_tabs.setCurrentIndex(0)
        self.render_stash_page()

    def render_stash_page(self):
        page_idx = self.stash_tabs.currentIndex()
        if not (0 <= page_idx < len(self.stash_pages)):
            self.stash_grid.set_items([])
            return
        page = self.stash_pages[page_idx]
        self.stash_grid.set_items(list(enumerate(page.get("items", []))))

    def select_stash_item(self, local_index: int):
        page_idx = self.stash_tabs.currentIndex()
        if not (0 <= page_idx < len(self.stash_pages)):
            return
        items = self.stash_pages[int(page_idx)].get("items", [])
        if 0 <= local_index < len(items):
            self.selected_index = None
            self.selected_stash_page = int(page_idx)
            self.selected_stash_index = local_index
            self.selected_other_section = None
            self.selected_other_section_name = None
            self.selected_other_index = None
            self.selected_other_editable = False
            self.detail.show_item(items[local_index])
            self.detail.set_read_only(copy_to_character=bool(items[local_index].get("clean")))

    def select_other_item(self, ref):
        if not isinstance(ref, tuple) or len(ref) != 2:
            return
        sec_idx, item_idx = ref
        if not (0 <= int(sec_idx) < len(self.other_sections)):
            return
        items = self.other_sections[int(sec_idx)].get("items", [])
        if not (0 <= int(item_idx) < len(items)):
            return
        self.selected_index = None
        self.selected_stash_page = None
        self.selected_stash_index = None
        self.selected_other_section = self.other_sections[int(sec_idx)].get("id")
        self.selected_other_section_name = self.other_sections[int(sec_idx)].get("name", "Other")
        self.selected_other_index = int(item_idx)
        self.selected_other_editable = bool(self.other_sections[int(sec_idx)].get("editable"))
        item = dict(items[int(item_idx)])
        item["_section_name"] = self.selected_other_section_name
        self.detail.show_item(item)
        self.detail.set_read_only(copy_to_character=bool(item.get("clean")))
        self.detail.edit.setEnabled(self.selected_other_editable and bool(item.get("clean")))

    def move_stash_item(self, local_index: int, x: int, y: int):
        if not self.loaded:
            return
        page_idx = self.stash_tabs.currentIndex()
        if not (0 <= page_idx < len(self.stash_pages)):
            return
        page = self.stash_pages[page_idx]
        source = page.get("_source", self.loaded.path)
        source_combo_idx = self.stash_source.currentIndex()
        try:
            res = save_api.do_movestashitem({
                "path": source, "page": int(page.get("_page_idx", page_idx)),
                "item": local_index, "x": x, "y": y,
            })
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Stash move failed", str(e))
            self.load_current()
            return
        if not res.get("ok"):
            QMessageBox.warning(self, "Stash move rejected", res.get("error", "The stash item could not be moved."))
            self.load_current()
            return
        if source == self.loaded.path:
            self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage("Moved stash item — unsaved (Ctrl+S to save)", 7000)
        self.load_current()
        if source_combo_idx < self.stash_source.count():
            self.stash_source.setCurrentIndex(source_combo_idx)
        if page_idx < self.stash_tabs.count():
            self.stash_tabs.setCurrentIndex(page_idx)

    def item_by_index(self, index: int) -> dict | None:
        if not self.loaded:
            return None
        if self.loaded.data.get("kind") == "character":
            items = self.loaded.data.get("items", [])
        else:
            items = []
            for page in self.loaded.data.get("pages", []):
                items.extend(page.get("items", []))
        return items[index] if 0 <= index < len(items) else None

    def select_item(self, index: int):
        self.selected_index = index
        self.selected_stash_page = None
        self.selected_stash_index = None
        self.selected_other_section = None
        self.selected_other_section_name = None
        self.selected_other_index = None
        self.selected_other_editable = False
        self.detail.show_item(self.item_by_index(index))

    def move_item_ui(self, index: int, x: int, y: int):
        item = self.item_by_index(index)
        if not item:
            return
        try:
            res = save_api.do_moveitem({"path": self.loaded.path, "item": index, "x": x, "y": y})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Move failed", str(e))
            self.load_current()
            return
        if not res.get("ok"):
            QMessageBox.warning(self, "Move rejected", res.get("error", "The item could not be moved."))
            self.load_current()
            return
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(f"Moved {item.get('name', 'item')} — unsaved (Ctrl+S to save)", 7000)
        self.load_current()
        self.select_item(index)

    def move_selected_to_inventory(self):
        if not self.loaded or self.selected_index is None:
            return
        item = self.item_by_index(self.selected_index)
        if not item:
            return
        try:
            res = save_api.do_moveitem({"path": self.loaded.path, "item": self.selected_index})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Move failed", str(e))
            self.load_current()
            return
        if not res.get("ok"):
            QMessageBox.warning(self, "Move rejected", res.get("error", "The item could not be moved."))
            self.load_current()
            return
        idx = self.selected_index
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(f"Moved {item.get('name', 'item')} to inventory — unsaved (Ctrl+S to save)", 7000)
        self.load_current()
        self.select_item(idx)

    def _compatible_equip_slots(self, item: dict) -> list[tuple[int, str]]:
        label = str(item.get("type_label", "")).lower()
        category = item.get("category", "")
        occupied = {
            int(it.get("equipped_id", 0))
            for it in (self.loaded.data.get("items", []) if self.loaded else [])
            if int(it.get("location", 0)) == 1
        }

        def ok(slot: int) -> bool:
            if slot in occupied and int(item.get("equipped_id", 0)) != slot:
                return False
            if slot == 1:
                return "helm" in label
            if slot == 2:
                return "amulet" in label
            if slot == 3:
                return "armor" in label
            if slot in (4, 11):
                return category == "weapon"
            if slot in (5, 12):
                return category == "weapon" or "shield" in label
            if slot in (6, 7):
                return "ring" in label
            if slot == 8:
                return "belt" in label
            if slot == 9:
                return "boot" in label
            if slot == 10:
                return "glove" in label
            return False

        return [(slot, name) for slot, name in EQUIP_SLOT_NAMES.items() if ok(slot)]

    def equip_selected(self):
        if not self.loaded or self.selected_index is None:
            return
        item = self.item_by_index(self.selected_index)
        if not item:
            return
        choices = self._compatible_equip_slots(item)
        if not choices:
            QMessageBox.information(self, "No open slot", "No compatible open equipment slot is available.")
            return
        labels = [f"{name} ({slot})" for slot, name in choices]
        picked, ok = QInputDialog.getItem(self, "Equip Item", "Slot", labels, 0, False)
        if not ok or not picked:
            return
        slot = choices[labels.index(picked)][0]
        try:
            res = save_api.do_equipitem({"path": self.loaded.path, "item": self.selected_index, "slot": slot})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Equip failed", str(e))
            return
        if not res.get("ok"):
            QMessageBox.warning(self, "Equip rejected", res.get("error", "The item could not be equipped."))
            self.load_current()
            return
        idx = self.selected_index
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(f"Equipped {item.get('name', 'item')} — unsaved (Ctrl+S to save)", 7000)
        self.load_current()
        self.select_item(idx)

    def equip_item_ui(self, index: int, slot: int):
        if not self.loaded or self.loaded.data.get("kind") != "character":
            return
        item = self.item_by_index(index)
        if not item:
            return
        try:
            res = save_api.do_equipitem({"path": self.loaded.path, "item": index, "slot": slot})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Equip failed", str(e))
            return
        if not res.get("ok"):
            QMessageBox.warning(self, "Equip rejected", res.get("error", "The item could not be equipped."))
            self.load_current()
            return
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(
            f"Equipped {item.get('name', 'item')} to {EQUIP_SLOT_NAMES.get(slot, slot)} — unsaved (Ctrl+S to save)",
            7000)
        self.load_current()
        self.select_item(index)

    def max_roll_selected(self):
        if not self.loaded or self.selected_index is None:
            return
        item = self.item_by_index(self.selected_index)
        name = item.get("name", "item") if item else "item"
        try:
            res = save_api.do_maxroll({"path": self.loaded.path, "item": self.selected_index})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Max roll failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Max roll rejected", res.get("error", "The item could not be updated.") + ("\n" + details if details else ""))
            return
        idx = self.selected_index
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(f"Max rolled {name} — unsaved (Ctrl+S to save)", 7000)
        self.load_current()
        self.select_item(idx)

    def edit_selected_item(self):
        if self.selected_index is None and self.selected_other_section is not None:
            self.edit_selected_section_item()
            return
        if not self.loaded or self.selected_index is None:
            return
        item = self.item_by_index(self.selected_index)
        if not item:
            return
        dlg = ItemEditDialog(item, self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            payload = dlg.payload()
            payload.update({"path": self.loaded.path, "item": self.selected_index})
            res = save_api.do_edititem(payload)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Item edit failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Item edit rejected", res.get("error", "The item could not be updated.") + ("\n" + details if details else ""))
            return
        idx = self.selected_index
        name = item.get("name", "item")
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(f"Updated {name} — unsaved (Ctrl+S to save)", 7000)
        self.load_current()
        self.select_item(idx)

    def edit_selected_section_item(self):
        if not self.loaded or self.loaded.data.get("kind") != "character":
            return
        if self.selected_other_section is None or self.selected_other_index is None:
            return
        sec = next((s for s in self.other_sections if s.get("id") == self.selected_other_section), None)
        if not sec or not sec.get("editable"):
            return
        items = sec.get("items", [])
        if not (0 <= self.selected_other_index < len(items)):
            return
        item = items[self.selected_other_index]
        dlg = ItemEditDialog(item, self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            payload = dlg.payload()
            payload.update({
                "path": self.loaded.path,
                "section": self.selected_other_section,
                "item": self.selected_other_index,
            })
            res = save_api.do_editsectionitem(payload)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Section item edit failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Section edit rejected", res.get("error", "The section item could not be updated.") + ("\n" + details if details else ""))
            return
        section_id = self.selected_other_section
        item_idx = self.selected_other_index
        name = item.get("name", "item")
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(
            f"Updated {name} in {self.selected_other_section_name or 'section'} — unsaved (Ctrl+S to save)",
            7000)
        self.load_current()
        for row in range(self.other_list.count()):
            ref = self.other_list.item(row).data(Qt.UserRole)
            if isinstance(ref, tuple):
                sec_idx, local_idx = ref
                if local_idx == item_idx and 0 <= sec_idx < len(self.other_sections):
                    if self.other_sections[sec_idx].get("id") == section_id:
                        self.other_list.setCurrentRow(row)
                        self.select_other_item(ref)
                        break

    def unsocket_selected_item(self):
        if not self.loaded or self.selected_index is None:
            return
        item = self.item_by_index(self.selected_index)
        if not item or not item.get("sockets"):
            return
        names = ", ".join(ch.get("name", "item") for ch in item.get("sockets", []))
        if QMessageBox.question(
            self, "Unsocket Item",
            f"Remove socketed items from {item.get('name', 'item')}?\n\n{names}"
        ) != QMessageBox.Yes:
            return
        try:
            res = save_api.do_unsocketitem({"path": self.loaded.path, "item": self.selected_index})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Unsocket failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Unsocket rejected", res.get("error", "The item could not be unsocketed.") + ("\n" + details if details else ""))
            return
        idx = self.selected_index
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(
            f"Removed {res.get('removed_count', 0)} socketed items — unsaved (Ctrl+S to save)",
            7000)
        self.load_current()
        self.select_item(idx)

    def socket_selected_item(self):
        if not self.loaded or self.selected_index is None:
            return
        item = self.item_by_index(self.selected_index)
        if not item:
            return
        fillers = save_api.browse("socket_fillers").get("socket_fillers", [])
        if not fillers:
            QMessageBox.information(self, "No socket fillers", "No runes, gems, or jewels were found in the selected MPQ.")
            return
        labels = [f"{row.get('name', row.get('code'))} [{row.get('code')}]" for row in fillers]
        picked, ok = QInputDialog.getItem(self, "Socket Item", "Filler", labels, 0, False)
        if not ok or not picked:
            return
        code = fillers[labels.index(picked)].get("code")
        try:
            res = save_api.do_socketitem({"path": self.loaded.path, "item": self.selected_index, "code": code})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Socket failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Socket rejected", res.get("error", "The filler could not be socketed.") + ("\n" + details if details else ""))
            return
        idx = self.selected_index
        filler = res.get("socketed", {}).get("name", code)
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(f"Socketed {filler} — unsaved (Ctrl+S to save)", 7000)
        self.load_current()
        self.select_item(idx)

    def duplicate_selected_item(self):
        if not self.loaded or self.selected_index is None:
            return
        item = self.item_by_index(self.selected_index)
        if not item:
            return
        try:
            res = save_api.do_duplicateitem({"path": self.loaded.path, "item": self.selected_index})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Duplicate failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Duplicate rejected", res.get("error", "The item could not be duplicated.") + ("\n" + details if details else ""))
            return
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(f"Duplicated {item.get('name', 'item')} — unsaved (Ctrl+S to save)", 7000)
        new_index = int(res.get("index", self.selected_index))
        self.load_current()
        self.select_item(new_index)

    def copy_selected_to_stash(self):
        if not self.loaded or self.loaded.data.get("kind") != "character" or self.selected_index is None:
            return
        item = self.item_by_index(self.selected_index)
        if not item:
            return
        start = os.path.dirname(self.loaded.path) or os.path.expanduser("~")
        stash_path, _ = QFileDialog.getOpenFileName(
            self, "Choose destination stash", start,
            "Diablo II stashes (*.d2x *.sss *.stash);;All files (*)")
        if not stash_path:
            return
        try:
            stash_data = save_api.parse_save(stash_path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not open stash", str(e))
            return
        pages = stash_data.get("pages", [])
        if not pages:
            QMessageBox.warning(self, "No stash pages", "The selected stash has no writable pages.")
            return
        labels = [f"{i + 1}: {page.get('name') or 'Shared'} ({page.get('count', 0)})"
                  for i, page in enumerate(pages)]
        picked, ok = QInputDialog.getItem(self, "Copy to Stash", "Page", labels, 0, False)
        if not ok or not picked:
            return
        page_idx = labels.index(picked)
        try:
            res = save_api.do_copyitemtostash({
                "path": self.loaded.path,
                "item": self.selected_index,
                "stash_path": stash_path,
                "page": page_idx,
            })
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Copy to stash failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Copy rejected", res.get("error", "The item could not be copied to stash.") + ("\n" + details if details else ""))
            return
        self.statusBar().showMessage(
            f"Copied {item.get('name', 'item')} to stash page {page_idx + 1} — unsaved (Ctrl+S to save)",
            9000)
        self._update_title()

    def copy_selected_to_character(self):
        if not self.loaded:
            return
        if self.selected_other_section is not None and self.selected_other_index is not None:
            self.copy_selected_section_to_character()
            return
        if self.selected_stash_page is None or self.selected_stash_index is None:
            return
        page = self.stash_pages[self.selected_stash_page] if self.selected_stash_page < len(self.stash_pages) else {}
        items = page.get("items", [])
        if not (0 <= self.selected_stash_index < len(items)):
            return
        item = items[self.selected_stash_index]
        start = os.path.dirname(self.loaded.path) or os.path.expanduser("~")
        char_path, _ = QFileDialog.getOpenFileName(
            self, "Choose destination character", start,
            "Diablo II characters (*.d2s);;All files (*)")
        if not char_path:
            return
        try:
            res = save_api.do_copystashitemtochar({
                "path": page.get("_source", self.loaded.path),
                "page": int(page.get("_page_idx", self.selected_stash_page)),
                "item": self.selected_stash_index,
                "char_path": char_path,
            })
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Copy to character failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Copy rejected", res.get("error", "The item could not be copied to character.") + ("\n" + details if details else ""))
            return
        self.statusBar().showMessage(
            f"Copied {item.get('name', 'item')} to character inventory — unsaved (Ctrl+S to save)",
            9000)
        self._update_title()

    def copy_selected_section_to_character(self):
        if not self.loaded or self.loaded.data.get("kind") != "character":
            return
        if self.selected_other_section is None or self.selected_other_index is None:
            return
        sec = next((s for s in self.other_sections if s.get("id") == self.selected_other_section), None)
        if not sec:
            return
        items = sec.get("items", [])
        if not (0 <= self.selected_other_index < len(items)):
            return
        item = items[self.selected_other_index]
        try:
            res = save_api.do_copysectionitemtochar({
                "path": self.loaded.path,
                "section": self.selected_other_section,
                "item": self.selected_other_index,
            })
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Copy to character failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Copy rejected", res.get("error", "The section item could not be copied to character.") + ("\n" + details if details else ""))
            return
        new_index = int(res.get("index", -1))
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(
            f"Copied {item.get('name', 'item')} from {self.selected_other_section_name or 'section'} to inventory — unsaved (Ctrl+S to save)",
            9000)
        self.load_current()
        if new_index >= 0:
            self.select_item(new_index)

    def delete_selected_item(self):
        if not self.loaded or self.selected_index is None:
            return
        item = self.item_by_index(self.selected_index)
        if not item:
            return
        if QMessageBox.question(
            self, "Delete Item",
            f"Delete {item.get('name', 'item')} from this character?"
        ) != QMessageBox.Yes:
            return
        try:
            res = save_api.do_deleteitem({"path": self.loaded.path, "item": self.selected_index})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Delete failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Delete rejected", res.get("error", "The item could not be deleted.") + ("\n" + details if details else ""))
            return
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage(f"Deleted {item.get('name', 'item')} — unsaved (Ctrl+S to save)", 7000)
        self.load_current()

    def save_character_stats(self, updates: dict):
        if not self.loaded or self.loaded.data.get("kind") != "character":
            return
        try:
            res = save_api.do_editchar({"path": self.loaded.path, "stats": updates})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Character stat edit failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Character stat edit rejected", res.get("error", "The character stats could not be updated.") + ("\n" + details if details else ""))
            return
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage("Updated character stats — unsaved (Ctrl+S to save)", 7000)
        self.load_current()

    def save_skills(self, updates: dict):
        if not self.loaded or self.loaded.data.get("kind") != "character":
            return
        try:
            res = save_api.do_editskills({"path": self.loaded.path, "skills": updates})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Skill edit failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Skill edit rejected", res.get("error", "The skills could not be updated.") + ("\n" + details if details else ""))
            return
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage("Updated skills — unsaved (Ctrl+S to save)", 7000)
        self.load_current()

    def save_waypoints(self, updates: dict):
        if not self.loaded or self.loaded.data.get("kind") != "character":
            return
        try:
            res = save_api.do_editwaypoints({"path": self.loaded.path, "waypoints": updates})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Waypoint edit failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Waypoint edit rejected", res.get("error", "The waypoints could not be updated.") + ("\n" + details if details else ""))
            return
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage("Updated waypoints — unsaved (Ctrl+S to save)", 7000)
        self.load_current()

    def save_quests(self, updates: dict):
        if not self.loaded or self.loaded.data.get("kind") != "character":
            return
        try:
            res = save_api.do_editquests({"path": self.loaded.path, "quests": updates})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Quest edit failed", str(e))
            return
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Quest edit rejected", res.get("error", "The quest flags could not be updated.") + ("\n" + details if details else ""))
            return
        self.settings.setValue("paths/save", res["out"])
        self.statusBar().showMessage("Updated quest flags — unsaved (Ctrl+S to save)", 7000)
        self.load_current()

    def validate_save(self):
        if not self.loaded:
            return
        try:
            res = save_api.do_validate({"path": self.loaded.path})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Validation failed", str(e))
            return
        if res.get("valid"):
            QMessageBox.information(self, "Valid", f"The game should load this save ({res.get('items')} items).")
        else:
            QMessageBox.warning(self, "Invalid", "\n".join(res.get("errors", [])))

    def open_builder(self):
        if not self.loaded or self.loaded.data.get("kind") != "character":
            QMessageBox.information(self, "Open a character", "Item Builder edits character saves.")
            return
        try:
            ItemBuilderDialog(self).exec()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Item Builder failed", str(e))

    def create_item(self, payload: dict) -> bool:
        try:
            res = save_api.do_additem(payload)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Create item failed", str(e))
            return False
        if not res.get("ok"):
            details = "\n".join(res.get("details", []))
            QMessageBox.warning(self, "Create item rejected", res.get("error", "The item could not be created.") + ("\n" + details if details else ""))
            return False
        self.settings.setValue("paths/save", res["out"])
        added = res.get("added", {}).get("name", "item")
        self.statusBar().showMessage(f"Created {added} — unsaved (Ctrl+S to save)", 7000)
        self.load_current()
        return True


def stylesheet() -> str:
    serif = "'Cinzel', 'Marcellus', 'Palatino Linotype', 'Georgia', serif"
    return f"""
    QWidget {{ background: #0c0a07; color: #cfc6b3; font-size: 13px; }}
    QMainWindow, QDialog {{ background: #0c0a07; }}
    QToolBar {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #221b12, stop:1 #14100a);
        border-bottom: 1px solid #000; spacing: 8px; padding: 6px;
    }}
    QToolButton, QPushButton {{
        font-family: {serif}; font-weight: 600; font-size: 12px;
        color: #c9a85a; border-radius: 3px; padding: 7px 14px;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2a2115, stop:1 #160f08);
        border: 1px solid #000; border-top: 1px solid #4a3a20;
    }}
    QToolButton:hover, QPushButton:hover {{
        color: #e7cd86; border-top-color: #6a5128;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3a2d18, stop:1 #1d140a);
    }}
    QPushButton:disabled {{ color: #5d564a; border-top-color: #2a2115; }}
    QPushButton#dangerBtn {{ color: #d98a6a; border-top-color: #5a2f20; }}
    QPushButton#dangerBtn:hover {{ color: #f0a585; }}
    QPushButton#dangerBtn:disabled {{ color: #5d564a; }}
    QLineEdit, QTextEdit, QListWidget, QComboBox, QSpinBox {{
        background: #080705; border: 1px solid #2b2114; color: #cfc6b3;
        selection-background-color: #5c4124; padding: 3px;
    }}
    QTabWidget::pane {{ border: 1px solid #000; background: #16120c; }}
    QTabBar::tab {{
        font-family: {serif}; font-weight: 600; font-size: 11px;
        background: #0c0906; color: #8c8474; padding: 9px 16px 8px;
        border: 1px solid transparent; border-bottom: none;
    }}
    QTabBar::tab:hover {{ color: #cfc6b3; }}
    QTabBar::tab:selected {{
        color: #e7cd86; border: 1px solid #000; border-top: 2px solid #c9a85a;
        border-top-left-radius: 3px; border-top-right-radius: 3px;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #241c11, stop:1 #16110a);
    }}
    QLabel#characterHeader {{
        font-family: {serif}; color: #e7cd86; font-size: 16px; font-weight: 700; padding: 3px 0;
    }}
    QLabel#panelTitle {{
        font-family: {serif}; color: #8a7339; font-size: 11px; font-weight: 600;
    }}
    QLabel#slotCaption {{ color: #4f483a; font-size: 9px; }}
    QLabel#sheetLabel {{
        font-family: {serif}; color: #c9a85a; font-size: 12px; font-weight: 600;
    }}
    QLabel#sheetRed {{
        font-family: {serif}; color: #b03a2e; font-size: 11px; font-weight: 600;
    }}
    QFrame#sheetRule {{ border: none; border-top: 1px solid #2e2415; }}
    QLabel {{ background: transparent; }}
    QWidget#hostTransparent {{ background: transparent; }}
    EquipmentPanel, BeltPanel {{ background: transparent; }}
    QFrame#panelFrame {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #14100a, stop:1 #0d0a07);
        border: 1px solid #000; border-radius: 5px;
    }}
    QWidget#detailPanel {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #14100a, stop:1 #0c0906);
        border: 1px solid #000; border-radius: 5px;
    }}
    QLabel#detailTitle, QLabel#setupTitle {{
        font-family: {serif}; color: #c7b377; font-size: 18px; font-weight: 700;
        background: transparent;
    }}
    QLabel#detailSubtitle {{
        color: #8a7339; font-size: 11px; background: transparent;
    }}
    QFrame#inventoryGrid, QFrame#stashGrid {{ border: 1px solid #000; background: #080705; }}
    QLabel#itemTile {{ background: transparent; }}
    QTextEdit#statText {{ color: #6f9fff; background: transparent; border: none; }}
    QScrollArea {{ background: transparent; border: none; }}
    QSplitter::handle {{ background: #0c0a07; }}
    """


def main() -> int:
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        try:
            mpq, save = sys.argv[i + 1], sys.argv[i + 2]
        except IndexError:
            print("usage: cain --selftest <pd2data.mpq> <save.d2s>", file=sys.stderr)
            return 2
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication(sys.argv[:1])
        settings = QSettings(APP_ORG, APP_NAME)
        settings.setValue("paths/mpq", mpq)
        settings.setValue("paths/save", save)
        win = MainWindow()
        print(win.header.text())
        print(f"items={win.items_list.count()}")
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setWindowIcon(app_icon())
    app.setStyleSheet(stylesheet())
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
