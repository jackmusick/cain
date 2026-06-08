#!/usr/bin/env python3
"""Offscreen visual preview of the native Character tab with mock data.

Renders MainWindow's character-tab widgets without needing an MPQ or save:
builds the same widget tree _build_ui creates, feeds it mockup-equivalent
items, and writes a PNG screenshot.

  .venv/bin/python scripts/preview_native.py /tmp/native-preview.png
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSplitter,
    QTabBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from native.app import (  # noqa: E402
    BeltPanel,
    DetailPanel,
    EquipmentPanel,
    InventoryGrid,
    stylesheet,
)


def eq(idx, slot_id, name, quality, type_label, **kw):
    d = {"equipped_id": slot_id, "name": name, "quality": quality,
         "type_label": type_label, "width": 1, "height": 1}
    d.update(kw)
    return (idx, d)


def inv(idx, x, y, w, h, name, quality, type_label, **kw):
    d = {"pos_x": x, "pos_y": y, "width": w, "height": h, "name": name,
         "quality": quality, "type_label": type_label}
    d.update(kw)
    return (idx, d)


EQUIPPED = [
    eq(0, 1, "Duskdeep", "unique", "Helm"),
    eq(1, 2, "Plague Collar Amulet", "magic", "Amulet"),
    eq(2, 4, "Unique Quhab", "unique", "Claw", category="weapon"),
    eq(3, 3, "Heavenly Garb", "set", "Armor"),
    eq(4, 5, "Pattern", "magic", "Claw", category="weapon"),
    eq(5, 6, "Plague Grasp Ring", "magic", "Ring"),
    eq(6, 8, "Corpse Fringe Belt", "magic", "Belt"),
    eq(7, 7, "Ghoul Spiral Ring", "magic", "Ring"),
    eq(8, 10, "Ghoul Grip Gauntlets", "magic", "Glove"),
    eq(9, 9, "Havoc Clasp Boots", "magic", "Boot"),
    eq(10, 11, "Spiked Club", "normal", "Club", category="weapon"),
]

INVENTORY = [
    inv(20, 0, 0, 1, 2, "Healing Potion", "magic", "Potion"),
    inv(21, 1, 0, 1, 2, "Tome of TP", "magic", "Tome"),
    inv(22, 0, 2, 2, 2, "Horadric Cube", "normal", "Cube"),
    inv(23, 2, 3, 1, 1, "Nagelring", "magic", "Ring"),
    inv(24, 3, 3, 1, 3, "War Sword", "rare", "Sword", category="weapon"),
    inv(25, 5, 3, 1, 3, "Kris", "unique", "Dagger", category="weapon"),
    inv(26, 7, 4, 1, 3, "Cleaver", "set", "Axe", category="weapon"),
    inv(27, 9, 4, 1, 3, "Pike", "magic", "Polearm", category="weapon"),
]

BELT = [(40 + i, {"pos_x": i, "name": "Healing Potion", "quality": "magic",
                  "type_label": "Potion", "width": 1, "height": 1})
        for i in range(7)]

DETAIL_ITEM = {
    "name": "Duskdeep", "quality": "unique", "base_name": "Full Helm",
    "type_label": "Helm", "defense": 27, "durability": "26/30", "ilvl": 50,
    "width": 2, "height": 2, "clean": True, "location": 1, "panel": 0,
    "stats": [
        {"text": "+43% Enhanced Defense"},
        {"text": "+14 to Maximum Damage"},
        {"text": "+18 Defense"},
        {"text": "Damage Reduced by 7"},
        {"text": "+15% Fire Resist"},
        {"text": "+15% Lightning Resist"},
    ],
}


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/native-preview.png"
    app = QApplication([])
    app.setStyleSheet(stylesheet())

    win = QWidget()
    win.resize(1300, 880)
    root = QVBoxLayout(win)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    toolbar = QToolBar()
    for text in ["Open Save", "Open MPQ", "Settings", "Validate", "Item Builder"]:
        toolbar.addAction(text)
    root.addWidget(toolbar)

    tabbar = QTabBar()
    for name in ["Character", "Stats", "Skills", "Waypoints", "Quests",
                 "Stash", "Other Items", "All Items"]:
        tabbar.addTab(name)
    root.addWidget(tabbar)

    header = QLabel("LadyKiller   ·   Assassin · Level 63 · 42/42 decoded")
    header.setObjectName("characterHeader")
    header.setContentsMargins(16, 0, 0, 0)
    root.addWidget(header)

    equipment = EquipmentPanel()
    inventory = InventoryGrid()
    belt = BeltPanel()
    detail = DetailPanel()

    eq_panel = QFrame()
    eq_panel.setObjectName("panelFrame")
    eq_layout = QVBoxLayout(eq_panel)
    eq_layout.setContentsMargins(14, 12, 14, 14)
    eq_layout.setSpacing(10)
    eq_title = QLabel("E Q U I P P E D")
    eq_title.setObjectName("panelTitle")
    eq_title.setAlignment(Qt.AlignCenter)
    eq_layout.addWidget(eq_title)
    eq_layout.addWidget(equipment, alignment=Qt.AlignHCenter)
    eq_layout.addWidget(inventory, alignment=Qt.AlignHCenter)
    belt_host = QWidget()
    belt_host.setObjectName("hostTransparent")
    belt_row = QHBoxLayout(belt_host)
    belt_row.setContentsMargins(0, 0, 0, 0)
    belt_cap = QLabel("BELT")
    belt_cap.setObjectName("slotCaption")
    belt_row.addWidget(belt_cap)
    belt_row.addWidget(belt, 1)
    eq_layout.addWidget(belt_host, alignment=Qt.AlignHCenter)
    eq_layout.addStretch(1)

    left_scroll = QScrollArea()
    left_scroll.setWidget(eq_panel)
    left_scroll.setWidgetResizable(True)
    left_scroll.setFrameShape(QFrame.NoFrame)

    splitter = QSplitter()
    splitter.addWidget(left_scroll)
    splitter.addWidget(detail)
    splitter.setStretchFactor(0, 3)
    splitter.setStretchFactor(1, 1)

    body = QWidget()
    body_layout = QHBoxLayout(body)
    body_layout.setContentsMargins(12, 4, 12, 12)
    body_layout.addWidget(splitter)
    root.addWidget(body, 1)

    equipment.set_items(EQUIPPED)
    inventory.set_items(INVENTORY)
    belt.set_items(BELT)
    detail.show_item(DETAIL_ITEM)

    win.show()
    app.processEvents()
    pm = win.grab()
    pm.save(out)
    print(f"saved {out} ({pm.width()}x{pm.height()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
