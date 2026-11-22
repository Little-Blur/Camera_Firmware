#!/usr/bin/env python3
"""camctl: desktop control panel for the camera rig (PySide6 / Qt).

One CameraLink per camera keeps a TCP connection to its daemon (camd, port 7070):
commands go out as JSON lines, replies and pushed status events come back the same way.
Preview frames and recordings come over HTTP (port 8080).

The camera list:
  (RIG)    a virtual camera at the top: the same controls as one camera, applied to every
           configured camera, plus scene-based video management
  NEW      a camera still at the base IP (192.168.0.5, unconfigured). It is left out of every
           rig-wide action until it has been given a camera number on its Device tab
  camNNN   the cameras listed in cameras.json (NNN = last part of the static IP)

  python camctl.py            cameras are listed in cameras.json next to this file
"""
import ctypes
import json
import os
import re
import shutil
import signal
import sqlite3
import struct
import subprocess
import socket
import sys
import time
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import (QObject, QProcess, QProcessEnvironment, QSettings, Qt, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QPixmap
from PySide6.QtNetwork import (QAbstractSocket, QNetworkAccessManager, QNetworkReply,
                               QNetworkRequest, QTcpSocket)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
                               QDoubleSpinBox, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

CONTROL_PORT = 7070
CONFIG = Path(__file__).with_name("cameras.json")
BASE_IP = "192.168.0.5"           # where every unconfigured camera comes up
RIG_PREFIX = "192.168.1."         # camera number N -> static IP 192.168.1.N, ID camN
ID_RANGE = (100, 200)
STATE_COLORS = {"active": "#2e9d4f", "starting": "#d49a1a", "stopping": "#d49a1a",
                "idle": "#8a8a8a", "error": "#c8372d", "offline": "#555555"}
GREEN, RED, AMBER, GREY = "#2e9d4f", "#c8372d", "#d49a1a", "#777777"


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def mmss(seconds):
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def dot(color, text=""):
    return f"<span style='color:{color}'>●</span> {text}"


def stem(name):
    return name[:-4] if name.endswith(".mkv") else name


def confirm(parent, title, text):
    return QMessageBox.question(parent, title, text) == QMessageBox.StandardButton.Yes


def fps_box(tooltip):
    """Frame rate field: 0 shows "all" (every frame); decimals for very slow tests (0.5 fps)."""
    box = QDoubleSpinBox(minimum=0, maximum=240, decimals=1, singleStep=1, value=0)
    box.setSpecialValueText("all")
    box.setToolTip(tooltip)
    return box


def keep_or_redo(parent, title, text):
    """Keep finished work and add what is missing (False), start over (True), or cancel (None)."""
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    keep = box.addButton("Keep, add missing", QMessageBox.ButtonRole.AcceptRole)
    over = box.addButton("Start over", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return {keep: False, over: True}.get(box.clickedButton())


def port_open(host, port=CONTROL_PORT, timeout=0.7):
    """Is something already answering at this address? (used before handing out an IP)"""
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def sync_text(sync):
    """(color, text) for a camera's frame-sync status."""
    if not sync or sync.get("role") == "off":
        return GREY, "off"
    if sync.get("other_masters"):
        return RED, f"{sync['role']}: other master {', '.join(sync['other_masters'])}!"
    state = sync.get("state", "")
    if sync["role"] == "master":
        return (GREEN, "master") if state == "master" else (GREY, f"master ({state})")
    if sync.get("locked"):
        return GREEN, f"locked to {sync.get('master')}  {sync.get('error_us') or 0:+.0f} µs"
    if state in ("locking", "measuring clock offset"):
        err = f"  {sync['error_us']:+.0f} µs" if sync.get("error_us") is not None else ""
        return AMBER, state + err
    return (GREY, state) if state == "idle" else (RED, state)


# ---------------------------------------------------------------- connection to one daemon

class CameraLink(QObject):
    """Control connection to one camd. Reconnects by itself; emits what the daemon says."""
    status = Signal(dict)      # pushed status events (and offline)
    online = Signal(bool)
    hello = Signal(dict)       # identity: camera_id, configured, ip, hostname, mac, serial, ...
    config = Signal(dict)      # modes, presets, settings
    failed = Signal(str)       # a command without its own callback failed

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.ident, self.cfg, self.last = {}, {}, {"state": "offline"}
        self.pending = {}
        self.next_id = 1
        self.connect_started = 0.0
        self.sock = QTcpSocket(self)
        self.sock.connected.connect(self._on_connected)
        self.sock.disconnected.connect(self._on_disconnected)
        self.sock.readyRead.connect(self._on_read)
        self.retry = QTimer(self, interval=2000)
        self.retry.timeout.connect(self._connect)
        self.retry.start()
        self._connect()

    @property
    def name(self):
        return self.ident.get("camera_id", self.host)

    @property
    def http(self):
        return f"http://{self.host}:{self.ident.get('http_port', 8080)}"

    @property
    def is_online(self):
        return self.sock.state() == QAbstractSocket.SocketState.ConnectedState

    @property
    def configured(self):
        """False for a NEW camera. Older daemons don't report it: they count as configured."""
        if self.host == BASE_IP:
            return False
        return self.last.get("configured", self.ident.get("configured", True))

    @property
    def in_rig(self):
        return self.is_online and self.configured

    def close(self):
        self.retry.stop()
        self.sock.abort()

    def _connect(self):
        state = self.sock.state()
        if state == QAbstractSocket.SocketState.ConnectingState and time.time() - self.connect_started > 3:
            self.sock.abort()  # host unreachable: don't wait for the OS timeout
            state = self.sock.state()
        if state == QAbstractSocket.SocketState.UnconnectedState:
            self.connect_started = time.time()
            self.sock.connectToHost(self.host, CONTROL_PORT)

    def _on_connected(self):
        self.online.emit(True)
        self.send("hello", self._got_hello)
        self.send("get_config", self._got_config)
        self.send("status", lambda ok, r: ok and self._got_status(r))

    def _got_hello(self, ok, result):
        if ok:
            self.ident = result
            self.hello.emit(result)

    def _got_config(self, ok, result):
        if ok:
            self.cfg = result
            self.config.emit(result)

    def _got_status(self, data):
        self.last = data
        self.status.emit(data)

    def _on_disconnected(self):
        for cb in self.pending.values():
            if cb:
                cb(False, "disconnected")
        self.pending.clear()
        self.online.emit(False)
        self._got_status({"state": "offline"})

    def _on_read(self):
        while self.sock.canReadLine():
            try:
                msg = json.loads(bytes(self.sock.readLine()))
            except ValueError:
                continue
            if msg.get("event") == "status":
                self._got_status(msg["data"])
            elif "id" in msg:
                cb = self.pending.pop(msg["id"], None)
                result = msg.get("result") if msg.get("ok") else msg.get("error", "error")
                if cb:
                    cb(msg.get("ok", False), result)
                elif not msg.get("ok"):
                    self.failed.emit(f"{self.name}: {result}")

    def send(self, cmd, callback=None, **args):
        """Send a command. callback(ok, result_or_error) runs when the reply arrives."""
        if not self.is_online:
            if callback:
                callback(False, "camera offline")
            else:
                self.failed.emit(f"{self.name}: offline")
            return
        rid, self.next_id = self.next_id, self.next_id + 1
        self.pending[rid] = callback
        self.sock.write(json.dumps({"id": rid, "cmd": cmd, **args}).encode() + b"\n")


def gather(links, cmd, done, **args):
    """Send cmd to several cameras; done({link: (ok, result)}) once all have answered."""
    results = {}
    if not links:
        return done(results)

    def make_cb(link):
        def cb(ok, result):
            results[link] = (ok, result)
            if len(results) == len(links):
                done(results)
        return cb
    for link in links:
        link.send(cmd, make_cb(link), **args)


# ---------------------------------------------------------------- downloads (shared by all pages)

class Downloads(QObject):
    """Download queue. One transfer per camera at a time (the camera's disk and link are the
    limit), different cameras in parallel. Files arrive as <name>.part and are renamed when
    complete, so a half-finished file is never mistaken for a finished one."""
    changed = Signal()
    message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nam = QNetworkAccessManager(self)
        self.jobs = []      # waiting: dicts with link, name, dest, size
        self.active = {}    # host -> running job
        self.done_bytes = 0
        self.batch_total = 0

    def add(self, link, name, dest, size=0):
        dest = Path(dest)
        if any(j["dest"] == dest for j in self.jobs + list(self.active.values())):
            return False
        self.jobs.append({"link": link, "name": name, "dest": dest, "size": size, "got": 0})
        self.batch_total += size
        self._pump()
        self.changed.emit()
        return True

    def busy(self):
        return bool(self.jobs or self.active)

    def progress(self):
        """(fraction 0..1, text) over everything queued since the queue was last empty."""
        got = self.done_bytes + sum(j["got"] for j in self.active.values())
        n = len(self.jobs) + len(self.active)
        frac = got / self.batch_total if self.batch_total else 0
        return frac, f"{n} file(s) left · {human_bytes(got)} of {human_bytes(self.batch_total)}"

    def _pump(self):
        for job in list(self.jobs):
            host = job["link"].host
            if host in self.active:
                continue
            self.jobs.remove(job)
            self.active[host] = job
            self._start(job)

    def _start(self, job):
        job["dest"].parent.mkdir(parents=True, exist_ok=True)
        part = job["dest"].with_name(job["dest"].name + ".part")
        job["file"] = open(part, "wb")
        req = QNetworkRequest(QUrl(f"{job['link'].http}/recordings/{job['name']}"))
        reply = self.nam.get(req)
        job["reply"] = reply

        def progress(got, total):
            job["got"] = got
            self.changed.emit()
        reply.readyRead.connect(lambda: job["file"].write(bytes(reply.readAll())))
        reply.downloadProgress.connect(progress)
        reply.finished.connect(lambda: self._finished(job, part))

    def _finished(self, job, part):
        reply = job["reply"]
        job["file"].write(bytes(reply.readAll()))
        job["file"].close()
        del self.active[job["link"].host]
        if reply.error() == QNetworkReply.NetworkError.NoError:
            part.replace(job["dest"])
            self.message.emit(f"downloaded {job['dest']}")
        else:
            part.unlink(missing_ok=True)
            self.message.emit(f"download of {job['name']} from {job['link'].name} failed: {reply.errorString()}")
        self.done_bytes += job["size"] or job["got"]
        reply.deleteLater()
        if not self.busy():
            self.done_bytes = self.batch_total = 0
        self._pump()
        self.changed.emit()


# ---------------------------------------------------------------- preview

class Preview(QLabel):
    """Polls /preview.jpg while the camera's preview runs and the widget is visible. Polling (one
    request in flight at a time) adapts to the network by itself: a slow link gets fewer frames."""

    def __init__(self, link=None, min_size=(320, 180)):
        super().__init__("no camera")
        self.link = None
        self.pix = None
        self.inflight = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(*min_size)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background:#111; color:#777;")
        self.nam = QNetworkAccessManager(self)
        self.timer = QTimer(self, interval=150)
        self.timer.timeout.connect(self._poll)
        self.set_link(link)

    def set_link(self, link):
        if self.link:
            self.link.status.disconnect(self._on_status)
        self.link = link
        self.pix = None
        self.setPixmap(QPixmap())
        if link:
            link.status.connect(self._on_status)
            self._on_status(link.last)
        else:
            self.timer.stop()
            self.setText("no camera")

    def _on_status(self, s):
        running = bool(s.get("preview", {}).get("running"))
        if running and not self.timer.isActive():
            self.timer.start()
        elif not running and (self.timer.isActive() or not self.pix):
            self.timer.stop()
            self.pix = None
            self.setPixmap(QPixmap())
            self.setText(f"{self.link.name}: preview off" if self.link else "no camera")

    def _poll(self):
        if self.inflight or not self.isVisible() or not self.link:
            return
        req = QNetworkRequest(QUrl(f"{self.link.http}/preview.jpg"))
        req.setTransferTimeout(2000)
        self.inflight = self.nam.get(req)
        self.inflight.finished.connect(self._done)

    def _done(self):
        reply, self.inflight = self.inflight, None
        if reply.error() == QNetworkReply.NetworkError.NoError and self.timer.isActive():
            pix = QPixmap()
            if pix.loadFromData(reply.readAll()):
                self.pix = pix
                self._show()
        reply.deleteLater()

    def resizeEvent(self, event):
        self._show()
        super().resizeEvent(event)

    def _show(self):
        if self.pix:
            self.setPixmap(self.pix.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))


# ---------------------------------------------------------------- shared widgets

def make_table(headers, stretch=0):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    t.verticalHeader().hide()
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    return t


def cell(text, data=None, right=False):
    item = QTableWidgetItem(text)
    if data is not None:
        item.setData(Qt.ItemDataRole.UserRole, data)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


def selected_data(table):
    rows = sorted({i.row() for i in table.selectedIndexes()})
    return [table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]


def row_buttons(*buttons):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(2, 0, 2, 0)
    for text, slot in buttons:
        b = QPushButton(text)
        b.setFlat(True)
        b.clicked.connect(slot)
        lay.addWidget(b)
    return w


def ask_folder(parent, key, title):
    settings = QSettings("IterativeArts", "camctl")
    folder = QFileDialog.getExistingDirectory(parent, title, settings.value(key, ""))
    if folder:
        settings.setValue(key, folder)
    return folder


class CaptureControls(QGroupBox):
    """Mode / preset / bitrate / preview + start and record buttons. Used by a camera page and
    by the RIG page; the page decides where the commands go."""

    def __init__(self, title):
        super().__init__(title)
        self.mode = QComboBox()
        self.preset = QComboBox()
        self.preview_box = QCheckBox("Preview")
        self.bitrate = QSpinBox(suffix=" Mbit/s", minimum=5, maximum=200, singleStep=5, value=60)
        self.start_btn = QPushButton("Start camera")
        self.rec_btn = QPushButton("● Record")
        self.rec_btn.setStyleSheet("QPushButton:enabled { color:#c8372d; font-weight:600; }")
        grid = QGridLayout(self)
        grid.addWidget(QLabel("Mode"), 0, 0)
        grid.addWidget(self.mode, 0, 1)
        grid.addWidget(QLabel("Preset"), 1, 0)
        grid.addWidget(self.preset, 1, 1)
        grid.addWidget(QLabel("Bitrate"), 2, 0)
        grid.addWidget(self.bitrate, 2, 1)
        grid.addWidget(self.preview_box, 3, 1)
        grid.addWidget(self.start_btn, 4, 0, 1, 2)
        grid.addWidget(self.rec_btn, 5, 0, 1, 2)

    def fill(self, cfg):
        """Modes and presets from a camera's get_config (all cameras run the same firmware)."""
        if not cfg or self.mode.count():
            return
        for key, m in cfg["modes"].items():
            self.mode.addItem(f"{key}  ({m['label']})", key)
        self.preset.addItems(cfg["presets"])

    def select(self, mode=None, preset=None, preview=None, bitrate=None):
        for combo, value in ((self.mode, mode), (self.preset, preset)):
            if value is None:
                continue
            i = combo.findData(value) if combo.findData(value) >= 0 else combo.findText(value)
            if i >= 0 and combo.currentIndex() != i:
                combo.setCurrentIndex(i)
        if preview is not None:
            self.preview_box.setChecked(bool(preview))
        if bitrate is not None and not self.bitrate.hasFocus():
            self.bitrate.blockSignals(True)
            self.bitrate.setValue(int(bitrate))
            self.bitrate.blockSignals(False)

    def target(self):
        return {"mode": self.mode.currentData(), "preset": self.preset.currentText(),
                "preview": self.preview_box.isChecked(), "bitrate": self.bitrate.value()}


class VideoTable(QWidget):
    """Recordings table with per-row Download / Delete buttons, multi-select buttons and Refresh.
    The owner supplies the data and handles the actions."""
    refresh = Signal()
    download = Signal(list)   # row keys
    delete = Signal(list)

    def __init__(self, headers):
        super().__init__()
        self.table = make_table(headers + [""])
        self.headers = headers
        buttons = QHBoxLayout()
        for text, slot in [("Refresh", self.refresh.emit),
                           ("Download selected…", lambda: self._multi(self.download)),
                           ("Delete selected…", lambda: self._multi(self.delete))]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch()
        self.summary = QLabel()
        buttons.addWidget(self.summary)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addLayout(buttons)

    def _multi(self, signal):
        keys = selected_data(self.table)
        if keys:
            signal.emit(keys)

    def set_rows(self, rows):
        """rows: list of (key, [cell texts], right_aligned_columns)"""
        self.table.setRowCount(len(rows))
        for r, (key, texts, right) in enumerate(rows):
            for c, text in enumerate(texts):
                self.table.setItem(r, c, cell(text, key if c == 0 else None, c in right))
            self.table.setCellWidget(r, len(texts), row_buttons(
                ("Download", lambda _=False, k=key: self.download.emit([k])),
                ("Delete", lambda _=False, k=key: self.delete.emit([k]))))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)


# ---------------------------------------------------------------- one camera's page

class CameraPanel(QWidget):
    message = Signal(str)
    renumbered = Signal(object, str)   # link, new host

    def __init__(self, link, downloads):
        super().__init__()
        self.link = link
        self.downloads = downloads
        self.state = "offline"
        self.was_recording = None
        self.files = []

        self.title = QLabel(link.host)
        self.title.setStyleSheet("font-size:16px; font-weight:600;")
        self.tabs = QTabWidget()
        self.tabs.addTab(self._live_tab(), "Live")
        self.tabs.addTab(self._videos_tab(), "Videos")
        self.tabs.addTab(self._device_tab(), "Device")
        self.tabs.currentChanged.connect(lambda i: i == 1 and self.refresh_recordings())
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.tabs)

        link.status.connect(self.on_status)
        link.config.connect(self.on_config)
        link.hello.connect(self.on_hello)
        link.online.connect(lambda on: on and self.refresh_recordings())
        self.on_status(link.last)

    # -- tabs

    def _live_tab(self):
        self.preview = Preview(self.link)
        self.controls = c = CaptureControls("Camera")
        c.mode.activated.connect(lambda: self.link.send("set_mode", mode=c.mode.currentData()))
        c.preset.activated.connect(lambda: self.link.send("set_preset", preset=c.preset.currentText()))
        c.preview_box.clicked.connect(lambda on: self.link.send("set_preview", enabled=on))
        c.bitrate.editingFinished.connect(lambda: self.link.send("set_bitrate", mbps=c.bitrate.value()))
        c.start_btn.clicked.connect(self._start_stop)
        c.rec_btn.clicked.connect(self._record)

        self.fields = {}
        grid = QGridLayout()
        for i, (key, label) in enumerate([("state", "State"), ("stream", "Stream"),
                                          ("drops", "Capture drops"), ("rec", "Recording"),
                                          ("recdrops", "Rec drops"), ("sync", "Frame sync"),
                                          ("temp", "SoC temp"),
                                          ("disk", "Disk free")]):
            grid.addWidget(QLabel(label), i, 0)
            self.fields[key] = QLabel("–")
            self.fields[key].setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(self.fields[key], i, 1)
        self.alert = QLabel()
        self.alert.setWordWrap(True)
        self.alert.setStyleSheet("color:#c8372d;")
        grid.addWidget(self.alert, len(self.fields), 0, 1, 2)
        status_box = QGroupBox("Status")
        status_box.setLayout(grid)

        self.new_note = QLabel("<b>NEW camera.</b> Give it a camera number on the Device tab. "
                               "Until then it is left out of rig-wide actions.")
        self.new_note.setWordWrap(True)
        self.new_note.setStyleSheet(f"color:{AMBER};")
        side = QVBoxLayout()
        side.addWidget(self.new_note)
        side.addWidget(c)
        side.addWidget(status_box)
        side.addStretch()
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setFixedWidth(360)
        w = QWidget()
        top = QHBoxLayout(w)
        top.addWidget(self.preview, 1)
        top.addWidget(side_w)
        return w

    def _videos_tab(self):
        self.videos = VideoTable(["File", "Size", "Recorded"])
        self.videos.refresh.connect(self.refresh_recordings)
        self.videos.download.connect(self._download)
        self.videos.delete.connect(self._delete)
        return self.videos

    def _device_tab(self):
        self.ident_labels = {}
        ident = QFormLayout()
        for key, label in [("camera_id", "Camera ID"), ("ip", "IP address"), ("hostname", "Hostname"),
                           ("mac", "MAC"), ("serial", "Serial"), ("version", "camd version")]:
            self.ident_labels[key] = QLabel("–")
            self.ident_labels[key].setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            ident.addRow(label, self.ident_labels[key])
        ident_box = QGroupBox("Identity")
        ident_box.setLayout(ident)

        self.number = QSpinBox(minimum=ID_RANGE[0], maximum=ID_RANGE[1])
        self.number_hint = QLabel()
        self.number.valueChanged.connect(
            lambda n: self.number_hint.setText(f"→ {RIG_PREFIX}{n}  (cam{n})"))
        self.number.valueChanged.emit(self.number.value())
        apply_btn = QPushButton("Set camera number")
        apply_btn.clicked.connect(self._renumber)
        net = QGridLayout()
        net.addWidget(QLabel("Camera number"), 0, 0)
        net.addWidget(self.number, 0, 1)
        net.addWidget(self.number_hint, 0, 2)
        net.addWidget(apply_btn, 1, 0, 1, 3)
        note = QLabel("The number is the camera's static IP and its ID. The camera saves it, "
                      "switches to it right away and keeps it across reboots.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{GREY};")
        net.addWidget(note, 2, 0, 1, 3)
        net_box = QGroupBox("Network")
        net_box.setLayout(net)

        reboot = QPushButton("Reboot")
        reboot.clicked.connect(lambda: self._power("reboot"))
        shutdown = QPushButton("Shut down")
        shutdown.clicked.connect(lambda: self._power("shutdown"))
        power = QHBoxLayout()
        power.addWidget(reboot)
        power.addWidget(shutdown)
        power.addStretch()
        power_box = QGroupBox("Power")
        power_box.setLayout(power)

        w = QWidget()
        lay = QVBoxLayout(w)
        for box in (ident_box, net_box, power_box):
            box.setMaximumWidth(620)
            lay.addWidget(box)
        lay.addStretch()
        return w

    # -- daemon -> UI

    def on_hello(self, ident):
        new = not self.link.configured
        self.title.setText(f"{'NEW camera' if new else ident['camera_id']}  ·  {ident['ip']}")
        for key, lab in self.ident_labels.items():
            lab.setText(str(ident.get(key, "–")))
        if ident.get("configured_ip") and ident["configured_ip"] != ident["ip"]:
            self.ident_labels["ip"].setText(f"{ident['ip']}  (configured: {ident['configured_ip']})")

    def on_config(self, cfg):
        self.controls.fill(cfg)
        self.controls.select(bitrate=cfg["settings"].get("bitrate_mbps", 60))

    def on_status(self, s):
        self.state = s.get("state", "offline")
        f = self.fields
        busy = s.get("busy")
        f["state"].setText(f"<b style='color:{STATE_COLORS.get(self.state, '#888')}'>{self.state}</b>"
                           + (f" ({busy}…)" if busy else ""))
        st = s.get("stream")
        f["stream"].setText(f"{st['fps']:.2f} / {st['target_fps']} fps  ·  {st['size'][0]}×{st['size'][1]}"
                            if st else "–")
        f["drops"].setText(f"{st['dropped']} of {st['frames']}" if st else "–")
        rec = s.get("recording")
        f["rec"].setText(f"<b style='color:#c8372d'>● {mmss(rec['seconds'])}</b>  {human_bytes(rec['bytes'])}"
                         f"<br><small>{rec['file']}</small>" if rec else "–")
        f["recdrops"].setText(f"{rec['dropped']} of {rec['frames'] + rec['dropped']}" if rec else "–")
        color, text = sync_text(s.get("sync"))
        f["sync"].setText(dot(color, text) if s.get("sync") else "–")
        f["temp"].setText(f"{s['temp_c']:.0f} °C" if s.get("temp_c") is not None else "–")
        f["disk"].setText(f"{s['disk_free_gb']:.0f} GB" if "disk_free_gb" in s else "–")
        problems = ([s["error"]] if s.get("error") else []) + s.get("warnings", [])
        self.alert.setText("\n".join(problems))
        self.new_note.setVisible(self.state != "offline" and not self.link.configured)

        c = self.controls
        settings = s.get("settings", {})
        if settings:
            c.select(mode=s.get("mode") or settings.get("mode"), preset=settings.get("preset"),
                     preview=settings.get("preview", False), bitrate=settings.get("bitrate_mbps"))

        online = self.state != "offline"
        running = self.state in ("active", "starting")
        c.start_btn.setText("Stop camera" if running else "Start camera")
        c.start_btn.setEnabled(online and not busy)
        c.rec_btn.setEnabled(self.state == "active" and not busy)
        c.rec_btn.setText("■ Stop recording" if rec else "● Record")
        for w in (c.mode, c.preset, c.preview_box, c.bitrate):
            w.setEnabled(online and not busy and not rec)
        if (rec is None) != (self.was_recording is None):  # a recording started or finished
            self.refresh_recordings()
        self.was_recording = rec

    # -- UI -> daemon

    def _start_stop(self):
        if self.state in ("active", "starting"):
            self.link.send("stop")
        else:
            t = self.controls.target()
            self.link.send("start", mode=t["mode"], preset=t["preset"], preview=t["preview"])

    def _record(self):
        if self.link.last.get("recording"):
            self.link.send("record_stop", lambda ok, r: self.message.emit(
                f"{self.link.name}: saved {r['file']} ({r['frames']} frames, {r['dropped']} dropped)"
                if ok else f"{self.link.name}: {r}"))
        else:
            self.link.send("record_start", bitrate_mbps=self.controls.bitrate.value())

    def _renumber(self):
        n = self.number.value()
        ip = f"{RIG_PREFIX}{n}"
        if ip == self.link.host:
            return self.message.emit(f"{self.link.name} already is cam{n}")
        if port_open(ip):
            return QMessageBox.warning(self, "Camera number in use",
                                       f"Something already answers at {ip}. Pick another number.")
        if not confirm(self, "Set camera number",
                       f"Change {self.link.name} ({self.link.host}) to cam{n} at {ip}?\n\n"
                       "The camera switches address right away and keeps it after reboots."):
            return

        def done(ok, result):
            if ok:
                self.message.emit(f"{self.link.name} → cam{n} ({ip}); it reappears in a few seconds")
                self.renumbered.emit(self.link, ip)
            else:
                self.message.emit(f"{self.link.name}: {result}")
        self.link.send("set_network", done, ip=ip)

    def _power(self, action):
        what = "Reboot" if action == "reboot" else "Shut down"
        extra = "" if action == "reboot" else "\n\nIt can only be switched on again at the camera."
        if confirm(self, what, f"{what} {self.link.name}? A running recording is finished first.{extra}"):
            self.link.send(action, lambda ok, r: self.message.emit(
                f"{self.link.name}: {action} {'sent' if ok else 'failed: ' + str(r)}"))

    # -- recordings

    def refresh_recordings(self):
        self.link.send("list_recordings", self._fill_table)

    def _fill_table(self, ok, files):
        if not ok:
            return
        self.files = {f["name"]: f for f in files}
        self.videos.set_rows([
            (f["name"], [f["name"] + ("  (recording)" if f["recording"] else ""), human_bytes(f["bytes"]),
                         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f["mtime"]))], {1})
            for f in files])
        self.videos.summary.setText(f"{len(files)} file(s), "
                                    f"{human_bytes(sum(f['bytes'] for f in files))}")

    def _download(self, names):
        folder = ask_folder(self, "download_dir", "Download to")
        if not folder:
            return
        cam = self.link.name
        for n in names:
            # Rig takes are named after the scene, not the camera: keep the camera in the file name
            local = n if n.startswith(cam) else f"{cam}_{n}"
            self.downloads.add(self.link, n, Path(folder) / local, self.files.get(n, {}).get("bytes", 0))

    def _delete(self, names):
        if not confirm(self, "Delete recordings",
                       f"Permanently delete {len(names)} recording(s) from {self.link.name}?\n\n"
                       + "\n".join(names[:10]) + ("\n…" if len(names) > 10 else "")):
            return
        left = [len(names)]

        def one_done(ok, r):
            left[0] -= 1
            if not ok:
                self.message.emit(f"{self.link.name}: {r}")
            if left[0] == 0:
                self.refresh_recordings()
        for n in names:
            self.link.send("delete_recording", one_done, name=n)


# ---------------------------------------------------------------- the RIG page (virtual camera)

class RigPanel(QWidget):
    """The whole rig as one camera. Settings changes go to every configured, online camera;
    the table shows per camera whether its reported settings match (green) or not (red)."""
    message = Signal(str)

    def __init__(self, window, downloads):
        super().__init__()
        self.window = window
        self.downloads = downloads
        self.settings = QSettings("IterativeArts", "camctl")
        self.scenes = {}

        title = QLabel("RIG  ·  all configured cameras")
        title.setStyleSheet("font-size:16px; font-weight:600;")
        self.tabs = QTabWidget()
        self.tabs.addTab(self._live_tab(), "Live")
        self.tabs.addTab(self._videos_tab(), "Videos")
        self.tabs.addTab(self._device_tab(), "Device")
        self.tabs.currentChanged.connect(lambda i: i == 1 and self.refresh_scenes())
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.tabs)

        self.timer = QTimer(self, interval=500)
        self.timer.timeout.connect(self.update_view)
        self.timer.start()

    def cams(self):
        return self.window.rig_links()

    # -- Live tab: 2x2 previews, controls, per-camera sync table

    def _live_tab(self):
        grid = QGridLayout()
        self.tiles = []
        for i in range(4):
            combo = QComboBox()
            prev = Preview(None, (240, 135))
            combo.activated.connect(lambda _=0, i=i: self._tile_changed(i))
            box = QVBoxLayout()
            box.setSpacing(2)
            box.addWidget(combo)
            box.addWidget(prev, 1)
            grid.addLayout(box, i // 2, i % 2)
            self.tiles.append((combo, prev))
        grid_w = QWidget()
        grid_w.setLayout(grid)

        self.controls = c = CaptureControls("All cameras")
        saved = json.loads(self.settings.value("rig_target", "{}") or "{}")
        c.preview_box.setChecked(saved.get("preview", True))
        c.bitrate.setValue(saved.get("bitrate", 60))
        self._saved = saved
        c.mode.activated.connect(lambda: self._apply("set_mode", mode=c.mode.currentData()))
        c.preset.activated.connect(lambda: self._apply("set_preset", preset=c.preset.currentText()))
        c.preview_box.clicked.connect(lambda on: self._apply("set_preview", enabled=on))
        c.bitrate.editingFinished.connect(lambda: self._apply("set_bitrate", mbps=c.bitrate.value()))
        c.start_btn.clicked.connect(self._start_stop)
        c.rec_btn.clicked.connect(self._record)
        sync_btn = QPushButton("Push settings to all")
        sync_btn.setToolTip("Send mode, preset, bitrate and preview to every camera that doesn't match")
        sync_btn.clicked.connect(self._push_all)

        # Frame sync: one master, everyone else follows it (camsync on the cameras)
        self.sync_on = QCheckBox("Sync frames to a master camera")
        self.sync_on.setChecked(self.settings.value("sync_on", "false") == "true")
        self.sync_master = QComboBox()
        self.sync_master.addItem("Auto: first camera", "")
        self._saved_master = self.settings.value("sync_master", "") or ""
        self.sync_on.clicked.connect(self._sync_changed)
        self.sync_master.activated.connect(self._sync_changed)
        fs = QGridLayout()
        fs.addWidget(self.sync_on, 0, 0, 1, 2)
        fs.addWidget(QLabel("Master"), 1, 0)
        fs.addWidget(self.sync_master, 1, 1)
        self.sync_note = QLabel()
        self.sync_note.setWordWrap(True)
        self.sync_note.setStyleSheet(f"color:{GREY};")
        fs.addWidget(self.sync_note, 2, 0, 1, 2)
        sync_box = QGroupBox("Frame sync")
        sync_box.setLayout(fs)

        self.sync_label = QLabel()
        self.sync_label.setStyleSheet("font-size:14px; font-weight:600;")
        self.table = make_table(["Camera", "State", "Mode", "Preset", "Rec", "Frame sync", "Settings"])
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        side = QVBoxLayout()
        side.addWidget(c)
        side.addWidget(sync_box)
        side.addWidget(sync_btn)
        side.addWidget(self.sync_label)
        side.addWidget(self.table, 1)
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setFixedWidth(560)
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.addWidget(grid_w, 1)
        lay.addWidget(side_w)
        return w

    def _tile_changed(self, i):
        combo, prev = self.tiles[i]
        host = combo.currentData()
        prev.set_link(next((l for l in self.cams() if l.host == host), None))
        self.settings.setValue(f"rig_tile{i}", host or "")

    def target(self):
        return self.controls.target()

    def master_link(self):
        """The camera that should be sync master: the chosen one, or the first camera."""
        cams = self.cams()
        host = self.sync_master.currentData()
        return next((l for l in cams if l.host == host), None) if host else (cams[0] if cams else None)

    def expected_role(self, link):
        if not self.sync_on.isChecked():
            return "off"
        return "master" if link is self.master_link() else "follower"

    def _sync_changed(self):
        self.settings.setValue("sync_on", "true" if self.sync_on.isChecked() else "false")
        self._saved_master = self.sync_master.currentData() or ""
        self.settings.setValue("sync_master", self._saved_master)
        self._apply_sync_roles(self.cams())

    def _apply_sync_roles(self, links):
        # the master first, so the followers find it as soon as they listen
        for link in sorted(links, key=lambda l: self.expected_role(l) != "master"):
            role = self.expected_role(link)
            if (link.last.get("settings") or {}).get("sync", "off") != role:
                link.send("set_sync", lambda ok, r, l=link: ok or self.message.emit(f"{l.name}: {r}"),
                          role=role)

    def matches(self, link):
        """(color, text) for one camera: does it run with the rig's settings?"""
        s = link.last
        if s.get("state") in ("offline", None):
            return RED, "offline"
        if s.get("state") == "error":
            return RED, "error"
        if s.get("busy"):
            return AMBER, "applying…"
        st, t = s.get("settings", {}), {**self.target(), "sync": self.expected_role(link)}
        diffs = [k for k, have in (("mode", s.get("mode") or st.get("mode")), ("preset", st.get("preset")),
                                   ("preview", st.get("preview")), ("bitrate", st.get("bitrate_mbps")),
                                   ("sync", st.get("sync", "off")))
                 if have != t[k]]
        return (GREEN, "in sync") if not diffs else (RED, "differs: " + ", ".join(diffs))

    def update_view(self):
        cams = self.cams()
        c = self.controls
        for link in cams:
            if link.cfg and not c.mode.count():
                c.fill(link.cfg)
                c.select(mode=self._saved.get("mode") or link.cfg["settings"].get("mode"),
                         preset=self._saved.get("preset") or link.cfg["settings"].get("preset"))
        target = json.dumps(self.target())
        if target != self.settings.value("rig_target"):
            self.settings.setValue("rig_target", target)

        # tiles: keep choices current, default to the first four cameras
        hosts = [l.host for l in cams]
        for i, (combo, prev) in enumerate(self.tiles):
            items = [combo.itemData(j) for j in range(combo.count())]
            if items != [None] + hosts:
                current = combo.currentData() or self.settings.value(f"rig_tile{i}", "") \
                    or (hosts[i] if i < len(hosts) else None)
                combo.clear()
                combo.addItem("— none —", None)
                for l in cams:
                    combo.addItem(l.name, l.host)
                j = combo.findData(current)
                combo.setCurrentIndex(max(j, 0))
                self._tile_changed(i)

        items = [self.sync_master.itemData(j) for j in range(1, self.sync_master.count())]
        if items != hosts:
            current = self.sync_master.currentData() or self._saved_master
            self.sync_master.clear()
            self.sync_master.addItem("Auto: first camera", "")
            for l in cams:
                self.sync_master.addItem(l.name, l.host)
            self.sync_master.setCurrentIndex(max(self.sync_master.findData(current), 0))
        m = self.master_link()
        self.sync_note.setText(f"Master: {m.name if m else '–'}. The others lengthen or shorten single "
                               "frames so theirs start together with the master's. Rig recordings start "
                               "and stop on the same frame on every camera."
                               if self.sync_on.isChecked() else "Off: every camera runs on its own clock.")

        if not self.isVisible():
            return
        self.table.setRowCount(len(cams))
        greens = 0
        for r, link in enumerate(cams):
            s = link.last
            color, text = self.matches(link)
            greens += color == GREEN
            state = s.get("state", "offline")
            st = s.get("settings", {})
            rec = s.get("recording")
            scolor, stext = sync_text(s.get("sync"))
            items = [cell(link.name), cell(state), cell(s.get("mode") or st.get("mode") or "–"),
                     cell(st.get("preset", "–")), cell(f"● {mmss(rec['seconds'])}" if rec else ""),
                     cell("●  " + stext), cell("●  " + text)]
            items[1].setForeground(QColor(STATE_COLORS.get(state, GREY)))
            items[4].setForeground(QColor(RED))
            items[5].setForeground(QColor(scolor))
            items[6].setForeground(QColor(color))
            for col, item in enumerate(items):
                self.table.setItem(r, col, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        if not cams:
            self.sync_label.setText(dot(GREY, "no configured cameras online"))
        elif greens == len(cams):
            self.sync_label.setText(dot(GREEN, f"all {len(cams)} cameras in sync"))
        else:
            self.sync_label.setText(dot(RED, f"{len(cams) - greens} of {len(cams)} cameras not in sync"))

        states = [l.last.get("state") for l in cams]
        recording = any(l.last.get("recording") for l in cams)
        busy = any(l.last.get("busy") for l in cams)
        running = any(st in ("active", "starting") for st in states)
        c.start_btn.setText("Stop all cameras" if running else "Start all cameras")
        c.start_btn.setEnabled(bool(cams) and not busy)
        c.rec_btn.setText("■ Stop recording (all)" if recording else "● Record scene (all)")
        c.rec_btn.setEnabled(bool(cams) and (recording or "active" in states) and not busy)
        for w in (c.mode, c.preset, c.preview_box, c.bitrate, self.sync_on, self.sync_master):
            w.setEnabled(bool(cams) and not recording)

    # -- commands to every camera

    def _apply(self, cmd, **args):
        cams = self.cams()
        gather(cams, cmd, lambda res: self._report(cmd, res), **args)

    def _report(self, what, results):
        bad = [f"{l.name}: {r}" for l, (ok, r) in results.items() if not ok]
        self.message.emit(f"{what}: {len(results) - len(bad)} ok" + (f"; failed {'; '.join(bad)}" if bad else ""))

    def _push_all(self):
        t = self.target()
        for link in self.cams():
            if self.matches(link)[0] == GREEN:
                continue
            st = link.last.get("settings", {})
            if (link.last.get("mode") or st.get("mode")) != t["mode"]:
                link.send("set_mode", mode=t["mode"])
            if st.get("preset") != t["preset"]:
                link.send("set_preset", preset=t["preset"])
            if st.get("preview") != t["preview"]:
                link.send("set_preview", enabled=t["preview"])
            if st.get("bitrate_mbps") != t["bitrate"]:
                link.send("set_bitrate", mbps=t["bitrate"])
        self._apply_sync_roles(self.cams())

    def _start_stop(self):
        cams = self.cams()
        if any(l.last.get("state") in ("active", "starting") for l in cams):
            gather([l for l in cams if l.last.get("state") in ("active", "error")], "stop",
                   lambda r: self._report("stop", r))
        else:
            t = self.target()
            for l in cams:
                l.send("set_bitrate", mbps=t["bitrate"])
            gather(cams, "start", lambda r: self._report("start", r),
                   mode=t["mode"], preset=t["preset"], preview=t["preview"])

    def _synced_master(self):
        """The sync master, if frame sync is on and the master is running as master."""
        m = self.master_link()
        if not self.sync_on.isChecked() or not m or m.last.get("state") != "active":
            return None
        return m if (m.last.get("sync") or {}).get("state") == "master" else None

    def _at_master_frame(self, lead_ms, then):
        """Ask the master for the time of a frame lead_ms ahead, then call then(at_ts).
        at_ts is None (= unsynced, right away) when sync is off or the master can't say."""
        m = self._synced_master()
        if not m:
            return then(None)

        def got(ok, r):
            if not ok:
                self.message.emit(f"sync master {m.name}: {r} (unsynced)")
            then(r["ts"] if ok else None)
        m.send("sync_time", got, lead_ms=lead_ms)

    def _record(self):
        cams = self.cams()
        if any(l.last.get("recording") for l in cams):
            recording = [l for l in cams if l.last.get("recording")]
            self._at_master_frame(300, lambda ts: gather(
                recording, "record_stop", lambda r: self._report("record stop", r),
                **({"at_ts": ts} if ts else {})))
            return
        active = [l for l in cams if l.last.get("state") == "active"]
        idle = [l.name for l in cams if l not in active]
        if idle and not confirm(self, "Record scene", f"Not active, won't record: {', '.join(idle)}\n\n"
                                "Record on the active cameras anyway?"):
            return
        if self.sync_on.isChecked():
            if not self._synced_master():
                if not confirm(self, "Record scene", "Frame sync is on, but the master isn't running "
                               "as master.\n\nRecord without a synced start?"):
                    return
            else:
                unlocked = [l.name for l in active if self.expected_role(l) == "follower"
                            and not (l.last.get("sync") or {}).get("locked")]
                if unlocked and not confirm(self, "Record scene", "Not locked to the master: "
                                            f"{', '.join(unlocked)}\n\nRecord anyway?"):
                    return
        # One take name for every camera: the files of this scene match by name later
        take = f"scene_{time.strftime('%Y%m%d_%H%M%S')}_{self.target()['mode']}"
        self._at_master_frame(600, lambda ts: gather(
            active, "record_start", lambda r: self._report(f"record {take}", r),
            take=take, bitrate_mbps=self.target()["bitrate"], **({"at_ts": ts} if ts else {})))

    # -- Videos tab: scenes (one name, one file per camera)

    def _videos_tab(self):
        self.videos = VideoTable(["Scene", "Cameras", "Total size", "Recorded"])
        self.videos.refresh.connect(self.refresh_scenes)
        self.videos.download.connect(self._download_scenes)
        self.videos.delete.connect(self._delete_scenes)
        note = QLabel("Every recording name found on the rig's cameras. A scene downloads into "
                      "<i>&lt;folder&gt;/&lt;scene&gt;/&lt;camera&gt;.mkv</i>; files already there are skipped, "
                      "so downloading again fills in cameras that were missing.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{GREY};")
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(note)
        lay.addWidget(self.videos)
        return w

    def refresh_scenes(self):
        cams = self.cams()
        self.videos.summary.setText("scanning…")
        gather(cams, "list_recordings", lambda res: self._fill_scenes(cams, res))

    def _fill_scenes(self, cams, results):
        scenes = {}
        for link, (ok, files) in results.items():
            if not ok:
                continue
            for f in files:
                if f["recording"]:
                    continue
                scenes.setdefault(f["name"], {})[link] = f
        self.scenes = scenes
        order = sorted(scenes, key=lambda n: -max(f["mtime"] for f in scenes[n].values()))
        rows = []
        for name in order:
            per = scenes[name]
            rows.append((name, [stem(name), f"{len(per)} / {len(cams)}",
                                human_bytes(sum(f["bytes"] for f in per.values())),
                                time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(max(f["mtime"] for f in per.values())))],
                         {1, 2}))
        self.videos.set_rows(rows)
        self.videos.summary.setText(f"{len(order)} scene(s) on {len(cams)} camera(s)")

    def _download_scenes(self, names):
        root = ask_folder(self, "scene_dir", "Download scenes to")
        if not root:
            return
        queued = skipped = 0
        missing = []
        cams = self.cams()
        for name in names:
            folder = Path(root) / stem(name)
            per = self.scenes.get(name, {})
            for link, f in per.items():
                dest = folder / f"{link.name}.mkv"
                if dest.exists() and dest.stat().st_size == f["bytes"]:
                    skipped += 1   # already complete: only fill the holes
                    continue
                queued += self.downloads.add(link, name, dest, f["bytes"])
            missing += [f"{stem(name)}: {l.name}" for l in cams if l not in per]
        self.message.emit(f"scene download: {queued} file(s) queued, {skipped} already there"
                          + (f"; not on: {', '.join(missing)}" if missing else ""))

    def _delete_scenes(self, names):
        count = sum(len(self.scenes.get(n, {})) for n in names)
        if not confirm(self, "Delete scenes",
                       f"Permanently delete {len(names)} scene(s) ({count} file(s)) from every camera?\n\n"
                       + "\n".join(stem(n) for n in names[:10]) + ("\n…" if len(names) > 10 else "")):
            return
        pending = [(link, n) for n in names for link in self.scenes.get(n, {})]
        left = [len(pending)]

        def one_done(ok, r):
            left[0] -= 1
            if not ok:
                self.message.emit(f"delete failed: {r}")
            if left[0] == 0:
                self.refresh_scenes()
        for link, n in pending:
            link.send("delete_recording", one_done, name=n)

    # -- Device tab

    def _device_tab(self):
        reboot = QPushButton("Reboot all cameras")
        reboot.clicked.connect(lambda: self._power("reboot"))
        shutdown = QPushButton("Shut down all cameras")
        shutdown.clicked.connect(lambda: self._power("shutdown"))
        power = QHBoxLayout()
        power.addWidget(reboot)
        power.addWidget(shutdown)
        power.addStretch()
        box = QGroupBox("Power (configured cameras only; NEW cameras are not affected)")
        box.setLayout(power)
        box.setMaximumWidth(620)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(box)
        lay.addStretch()
        return w

    def _power(self, action):
        cams = self.cams()
        if not cams:
            return
        what = "Reboot" if action == "reboot" else "Shut down"
        extra = "" if action == "reboot" else "\n\nThey can only be switched on again at the cameras."
        if confirm(self, f"{what} all", f"{what} {len(cams)} camera(s): {', '.join(l.name for l in cams)}?\n"
                   f"Running recordings are finished first.{extra}"):
            gather(cams, action, lambda r: self._report(action, r))


# ---------------------------------------------------------------- Prepare: frames for reconstruction

class FrameExtractor(QObject):
    """Turns a scene folder (<scene>/<camera>.mkv) into one folder per frame:
    <scene>/frames/frame_000001/<camera>.jpg, i.e. every camera's view of one moment together,
    ready for COLMAP. Frames are numbered by timestamp, not counted: a frame a camera dropped
    leaves a gap in that one folder instead of shifting the rest of its frames. -copyts keeps the
    file's own timestamps, so in a synced take frame 1 (at 1 ms) is frame 1 on every camera.
    Runs ffmpeg (on PATH) for PARALLEL videos at a time."""
    changed = Signal()
    message = Signal(str)
    PARALLEL = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.queue = []      # (scene dir, video, fmt)
        self.running = {}    # QProcess -> job
        self.scenes = {}     # scene dir -> {"videos", "done", "frames": {camera: n}, "failed": [...]}

    def busy(self):
        return bool(self.queue or self.running)

    def add_scene(self, scene, fmt, fps=0):
        """fps: target rate (0 = every frame). Keeps every Nth source frame by timestamp,
        N = round(source fps / fps), so the numbers stay frame numbers of the source:
        45 fps at 15 -> frame_000001, frame_000004, … on every camera alike."""
        videos = sorted(p for p in scene.glob("*.mkv"))
        if not videos:
            return 0
        out = scene / "frames"
        if out.exists():
            shutil.rmtree(out)
        out.mkdir()
        self.scenes[scene] = {"videos": len(videos), "done": 0, "frames": {}, "failed": [], "rates": {}}
        for v in videos:
            self.queue.append({"scene": scene, "video": v, "fmt": fmt, "fps": fps, "frame": 0,
                               "total": self._estimate(v)})
        self._pump()
        return len(videos)

    @staticmethod
    def _estimate(video):
        """Frame count estimate for the progress bar (duration x frame rate)."""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=avg_frame_rate:format=duration", "-of", "json", str(video)],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            info = json.loads(out.stdout)
            num, den = info["streams"][0]["avg_frame_rate"].split("/")
            return max(1, int(float(info["format"]["duration"]) * int(num) / int(den)))
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, subprocess.SubprocessError):
            return 1

    @staticmethod
    def _fps(video):
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=avg_frame_rate", "-of", "csv=p=0", str(video)],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        num, den = out.stdout.strip().split("/")
        return f"{num}/{den}"

    def progress(self):
        jobs = list(self.running.values()) + self.queue
        total = sum(j["total"] for j in jobs) or 1
        done = sum(min(j["frame"], j["total"]) for j in jobs)
        names = ", ".join(f"{j['scene'].name}/{j['video'].stem}" for j in self.running.values())
        return done / total, f"{len(self.queue) + len(self.running)} video(s) left · {names}"

    def cancel(self):
        self.queue.clear()
        for proc, job in list(self.running.items()):
            proc.kill()
        self.message.emit("frame extraction cancelled")

    def _pump(self):
        while self.queue and len(self.running) < self.PARALLEL:
            job = self.queue.pop(0)
            tmp = job["scene"] / "frames" / f".tmp_{job['video'].stem}"
            tmp.mkdir(parents=True, exist_ok=True)
            job["tmp"] = tmp
            ext = "jpg" if job["fmt"] == "jpg" else "png"
            try:
                fps = self._fps(job["video"])
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                self._failed(job, f"ffprobe: {e}")
                continue
            quality = ["-q:v", "2"] if ext == "jpg" else ["-pix_fmt", "rgb24"]
            num, den = (int(x) for x in fps.split("/"))
            step = max(1, round(num / den / job["fps"])) if job["fps"] else 1
            self.scenes[job["scene"]]["rates"][job["video"].stem] = (fps, step)
            # Keep the frames whose number (= round(t x fps), as -frame_pts writes it) is a
            # multiple of step: chosen by timestamp, so all cameras keep the same moments
            select = ["-vf", f"select='not(mod(round(t*{num}/{den}),{step}))'"] if step > 1 else []
            job["total"] = max(1, job["total"] // step)
            proc = QProcess(self)
            proc.setProgram("ffmpeg")
            proc.setArguments(["-v", "error", "-nostdin", "-copyts", "-i", str(job["video"]),
                               "-map", "0:v:0", *select, "-fps_mode", "passthrough",
                               "-enc_time_base", f"{den}/{num}",
                               "-frame_pts", "1", *quality, "-progress", "pipe:1",
                               str(tmp / f"%06d.{ext}")])
            proc.readyReadStandardOutput.connect(lambda p=proc, j=job: self._on_progress(p, j))
            proc.finished.connect(lambda code, _s, p=proc: self._finished(p, code))
            proc.errorOccurred.connect(lambda err, p=proc: err == QProcess.ProcessError.FailedToStart
                                       and self._finished(p, -1))
            self.running[proc] = job
            proc.start()
        self.changed.emit()

    def _on_progress(self, proc, job):
        for line in bytes(proc.readAllStandardOutput()).decode(errors="replace").splitlines():
            if line.startswith("frame="):
                job["frame"] = int(line[6:] or 0)
        self.changed.emit()

    def _finished(self, proc, code):
        job = self.running.pop(proc, None)
        if job is None:
            return
        err = bytes(proc.readAllStandardError()).decode(errors="replace").strip()
        proc.deleteLater()
        if code != 0:
            self._failed(job, err.splitlines()[-1] if err else
                         "ffmpeg not found (install it and put it on PATH)" if code == -1 else f"exit {code}")
        else:
            # <tmp>/000000.jpg (0-based frame number by time) -> frames/frame_000001/<camera>.jpg
            frames = job["scene"] / "frames"
            n = 0
            for f in sorted(job["tmp"].iterdir()):
                dest = frames / f"frame_{int(f.stem) + 1:06d}"
                dest.mkdir(exist_ok=True)
                os.replace(f, dest / f"{job['video'].stem}{f.suffix}")
                n += 1
            job["tmp"].rmdir()
            self._scene_done(job, n)
        self._pump()

    def _failed(self, job, why):
        shutil.rmtree(job.get("tmp", job["scene"] / "frames" / ".none"), ignore_errors=True)
        self.scenes[job["scene"]]["failed"].append(f"{job['video'].stem}: {why}")
        self._scene_done(job, None)

    def _scene_done(self, job, n):
        s = self.scenes[job["scene"]]
        s["done"] += 1
        if n is not None:
            s["frames"][job["video"].stem] = n
        if s["done"] < s["videos"]:
            return
        folders = [d for d in (job["scene"] / "frames").iterdir() if d.name.startswith("frame_")]
        cams = len(s["frames"])
        partial = sum(1 for d in folders if sum(1 for _ in d.iterdir()) < cams)
        counts = ", ".join(f"{c} {n}" for c, n in sorted(s["frames"].items()))
        # The rate, for SfM's frame selection and for anyone reading the folder later
        rates = sorted(set(s["rates"].values()))
        rate = ""
        if rates:
            fps, step = rates[0]
            num, den = (int(x) for x in fps.split("/"))
            (job["scene"] / "frames" / "extract.json").write_text(json.dumps(
                {"source_fps": fps, "step": step, "fps": num / den / step,
                 "per_camera": {c: {"source_fps": f, "step": st} for c, (f, st) in s["rates"].items()}},
                indent=1) + "\n")
            rate = f" at {num / den / step:g} fps" + (f" (1 in {step} of {num / den:g} fps)" if step > 1 else "")
            if len(rates) > 1:
                rate += f"; cameras differ in frame rate: {rates}"
        self.message.emit(f"{job['scene'].name}: {len(folders)} frame folders{rate} ({counts})"
                          + (f"; {partial} missing a camera (dropped frames)" if partial else "")
                          + (f"; FAILED {'; '.join(s['failed'])}" if s["failed"] else ""))
        self.changed.emit()


# ---------------------------------------------------------------- Prepare: COLMAP structure from motion

IMAGE_SUFFIXES = (".jpg", ".png")
LENS_MODELS = [("SIMPLE_RADIAL (f, k1: robust with one image per camera)", "SIMPLE_RADIAL"),
               ("RADIAL (f, k1, k2)", "RADIAL"),
               ("OPENCV (fx, fy, k1, k2, p1, p2: needs strong overlap)", "OPENCV")]
# multinerf / 3DGS / SpacetimeGaussians: a tighter final bundle adjustment
TOLERANCE = ["--Mapper.ba_global_function_tolerance=0.000001"]


def colmap_count(path):
    """First field of a COLMAP binary model file: number of cameras / registered images / points."""
    try:
        with open(path, "rb") as f:
            return struct.unpack("<Q", f.read(8))[0]
    except (OSError, struct.error):
        return 0


def colmap_longest_side(cameras_bin):
    """Longest image side over the cameras in a COLMAP cameras.bin."""
    params = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12}   # per model id
    side = 0
    with open(cameras_bin, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        for _ in range(n):
            _cam, model, w, h = struct.unpack("<iiQQ", f.read(24))
            f.seek(8 * params[model], os.SEEK_CUR)
            side = max(side, w, h)
    return side


def colmap_command(exe):
    """(program, environment) to run COLMAP. The Windows release's COLMAP.bat only starts
    bin/colmap.exe (its DLLs sit next to it); running that directly avoids cmd.exe and its
    quoting of paths with spaces."""
    exe = Path(exe)
    if exe.suffix.lower() in (".bat", ".cmd") and (exe.parent / "bin" / "colmap.exe").exists():
        exe = exe.parent / "bin" / "colmap.exe"
    return str(exe), QProcessEnvironment.systemEnvironment()


def frame_images(folder):
    return sorted(p.name for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def scene_source_fps(scene):
    """Frame rate the frame numbers count in: the source video's (frames/extract.json, else ffprobe)."""
    try:
        num, den = json.loads((scene / "frames" / "extract.json").read_text())["source_fps"].split("/")
    except (OSError, ValueError, KeyError):
        num, den = FrameExtractor._fps(sorted(scene.glob("*.mkv"))[0]).split("/")
    return int(num) / int(den)


def thin_frames(frames, step):
    """Every step-th frame by frame number (= by time), from the first. Works whatever was
    extracted: 45 fps source, 15 fps extracted (1, 4, 7, …), 5 fps asked -> 1, 10, 19, …"""
    kept, due = [], None
    for d in frames:
        n = int(d.name[6:])
        if due is None or n >= due - 0.5:
            kept.append(d)
            due = n + step
    return kept


def colmap_folder(frame):
    """frames/frame_000001 -> colmap_0: SpacetimeGaussians' numbering (0-based, no padding)."""
    return f"colmap_{int(frame.name[6:]) - 1}"


def sfm_frames(scene):
    point = scene / "point"
    return sum(1 for _ in point.glob("colmap_*/sparse/0/points3D.bin")) if point.is_dir() else 0


class SfmRunner(QObject):
    """Structure from motion with COLMAP, every frame in one coordinate system (the method of
    SpacetimeGaussians' pre_no_prior.py):
      1. reference frame: full SfM (SIFT, exhaustive matching, mapper). This fixes every camera's
         pose and lens; the rig doesn't move, so they hold for the whole take.
      2. every other frame: SIFT and matching again (the scene moved), then point_triangulator with
         the reference cameras held fixed: new points, same coordinates, same cameras.
    Each frame is then undistorted (PINHOLE) into the layout Gaussian splatting trainers read:
      <scene>/point/colmap_<n>/images/<camera>.jpg + sparse/0/{cameras,images,points3D}.bin
      <scene>/point/colmap_<n>/distorted/sparse/0   the model with lens distortion
      <scene>/point/manual/                          the reference cameras as a text model
      <scene>/point/reference.json                   reference frame, lens model, placed cameras
    Without undistortion, images/ holds the original images and sparse/0 the lens model with
    distortion (same layout). frame_000001 is colmap_0 (SpacetimeGaussians' numbering). Every image is its own camera:
    each rig camera has its own lens. One COLMAP run at a time: SIFT and matching use the GPU.
    The cameras must have unchanged lens and position for the whole take."""
    changed = Signal()
    message = Signal(str)
    REF_STEPS = ("SIFT features", "matching", "mapping", "exporting cameras", "undistorting")
    FRAME_STEPS = ("SIFT features", "matching", "triangulating", "undistorting")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.program, self.env = "colmap", QProcessEnvironment.systemEnvironment()
        self.queue = []      # jobs: {"kind": "ref" | "frame", "scene", "frame", "out", "model"}
        self.job = None
        self.proc = None
        self.step = 0
        self.scenes = {}     # scene -> {"total", "done", "ok", "points", "failed", "note"}

    def busy(self):
        return bool(self.queue or self.job)

    def busy_scenes(self):
        return {j["scene"] for j in self.queue} | ({self.job["scene"]} if self.job else set())

    def add_scene(self, scene, first, count, ref_number, model, undistort, redo, fps=0, shared=True):
        """fps: reconstruct at this rate (0 = every extracted frame); count = frames after that.
        shared: one lens for every camera (same lens model on the whole rig)."""
        frames = sorted(d for d in (scene / "frames").iterdir()
                        if d.name.startswith("frame_") and int(d.name[6:]) >= first)
        if fps:
            try:
                frames = thin_frames(frames, scene_source_fps(scene) / fps)
            except (OSError, ValueError, IndexError, ZeroDivisionError, subprocess.SubprocessError) as e:
                self.message.emit(f"{scene.name}: frame rate unknown ({e}): use fps 'all'")
                return 0
        frames = frames[:count] if count else frames
        if not frames:
            return 0
        point = scene / "point"
        if redo:
            shutil.rmtree(point, ignore_errors=True)
        try:
            ref = json.loads((point / "reference.json").read_text())
            # Keep the scene's own settings: every frame of one take in the same form
            undistort = ref.get("undistort", True)
            shared = ref.get("shared_lens", False)
            note = (f"reference {ref['frame']} kept ({ref['model']}{', shared lens' if shared else ''}, "
                    f"{'undistorted' if undistort else 'distorted'})")
        except (OSError, ValueError):
            ref = None
            shutil.rmtree(point / "manual", ignore_errors=True)
        jobs = []
        if ref is None:
            if ref_number:
                ref_frame = scene / "frames" / f"frame_{ref_number:06d}"
                if not ref_frame.is_dir():
                    self.message.emit(f"{scene.name}: there is no frame {ref_number}")
                    return 0
            else:   # the first frame with every camera in it
                most = max(len(frame_images(d)) for d in frames)
                ref_frame = next(d for d in frames if len(frame_images(d)) == most)
            jobs.append({"kind": "ref", "frame": ref_frame})
            note = f"reference {ref_frame.name}"
        ref_name = ref["frame"] if ref else ref_frame.name
        for d in frames:
            if d.name != ref_name and not (point / colmap_folder(d) / "sparse" / "0" / "points3D.bin").exists():
                jobs.append({"kind": "frame", "frame": d})
        for j in jobs:
            j.update(scene=scene, out=point / colmap_folder(j["frame"]), model=model, undistort=undistort,
                     shared=shared)
        if jobs:
            self.scenes[scene] = {"total": len(jobs), "done": 0, "ok": 0, "points": 0, "failed": [],
                                  "note": note}
            self.queue += jobs
            QTimer.singleShot(0, self._next)   # after the caller's own "started" message
        return len(jobs)

    def progress(self):
        active = [s for s in self.scenes.values() if s["done"] < s["total"]]
        total = sum(s["total"] for s in active) or 1
        done = sum(s["done"] for s in active)
        now = ""
        if self.job:
            steps = self._steps(self.job)
            done += self.step / len(steps)
            now = f" · {self.job['scene'].name}/{self.job['out'].name}: {steps[self.step]}"
        return done / total, f"{len(self.queue) + bool(self.job)} frame(s) left{now}"

    def cancel(self):
        self.queue.clear()
        job, self.job = self.job, None
        if self.proc:
            self.proc.kill()
            self.proc.waitForFinished(5000)
        if job:
            shutil.rmtree(job["out"], ignore_errors=True)
            if job["kind"] == "ref":
                shutil.rmtree(job["scene"] / "point" / "manual", ignore_errors=True)
        self.message.emit("structure from motion cancelled")
        self.changed.emit()

    def _steps(self, job):
        steps = self.REF_STEPS if job["kind"] == "ref" else self.FRAME_STEPS
        return steps if job["undistort"] else steps[:-1]

    def _next(self):
        if self.job or not self.queue:
            return self.changed.emit()
        job = self.job = self.queue.pop(0)
        self.step = 0
        try:
            self._prepare(job)
        except (OSError, ValueError, KeyError, sqlite3.Error) as e:
            return self._failed(str(e))
        self._run()

    def _prepare(self, job):
        out = job["out"]
        shutil.rmtree(out, ignore_errors=True)
        (out / "distorted" / "sparse").mkdir(parents=True)
        job["images"] = frame_images(job["frame"])
        if job["kind"] == "ref":
            job["seed"], job["best"] = 1, 0
            return
        point = job["scene"] / "point"
        ref = json.loads((point / "reference.json").read_text())
        missing = sorted(set(ref["cameras"]) - set(job["images"]))
        if missing:
            raise ValueError(f"no image from {', '.join(Path(m).stem for m in missing)} (dropped frame)")
        # The reference database already holds every camera and image (same names, same ids as
        # the reference model): keep those rows, drop the reference frame's features and matches
        db = out / "distorted" / "database.db"
        shutil.copyfile(point / ref["folder"] / "distorted" / "database.db", db)
        with closing(sqlite3.connect(db)) as con:
            for table in ("keypoints", "descriptors", "matches", "two_view_geometries"):
                con.execute(f"DELETE FROM {table}")
            con.commit()
        (out / "distorted" / "sparse" / "0").mkdir()

    def _args(self, job):
        db = str(job["out"] / "distorted" / "database.db")
        frame = str(job["frame"])
        sparse = job["out"] / "distorted" / "sparse"
        manual = job["scene"] / "point" / "manual"
        step = self._steps(job)[self.step]
        if step == "SIFT features":
            return ["feature_extractor", "--database_path", db, "--image_path", frame,
                    "--ImageReader.camera_model", job["model"],
                    "--ImageReader.single_camera", "1" if job["shared"] else "0"]
        if step == "matching":
            return ["exhaustive_matcher", "--database_path", db]
        if step == "mapping":
            # A fixed seed per attempt: reproducible, and a retry really starts differently
            return ["mapper", "--database_path", db, "--image_path", frame, "--output_path", str(sparse),
                    "--Mapper.random_seed", str(job["seed"]), *TOLERANCE]
        if step == "exporting cameras":
            return ["model_converter", "--input_path", str(sparse / "0"), "--output_path", str(manual),
                    "--output_type", "TXT"]
        if step == "triangulating":
            return ["point_triangulator", "--database_path", db, "--image_path", frame,
                    "--input_path", str(manual), "--output_path", str(sparse / "0"), *TOLERANCE]
        return ["image_undistorter", "--image_path", frame, "--input_path", str(sparse / "0"),
                "--output_path", str(job["out"]), "--output_type", "COLMAP"]

    def _run(self):
        proc = self.proc = QProcess(self)
        proc.setProgram(self.program)
        proc.setArguments(self._args(self.job))
        proc.setProcessEnvironment(self.env)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.finished.connect(lambda code, _s, p=proc: self._finished(p, code))
        proc.errorOccurred.connect(lambda err, p=proc: err == QProcess.ProcessError.FailedToStart
                                   and self._finished(p, -1))
        proc.start()
        self.changed.emit()

    def _finished(self, proc, code):
        if proc is not self.proc:
            return
        self.proc = None
        log = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        args = proc.arguments()
        proc.deleteLater()
        job = self.job
        if job is None:
            return  # cancelled
        steps = self._steps(job)
        try:
            with open(job["out"] / "colmap.log", "a", encoding="utf-8") as f:
                f.write(f"==== {steps[self.step]}: colmap {' '.join(args)}\n{log}\n")
            if code != 0:
                lines = [line for line in log.splitlines() if line.strip()]
                raise ValueError("COLMAP not found or won't start (choose it with COLMAP…)" if code == -1
                                 else f"{steps[self.step]}: {lines[-1][-200:] if lines else f'exit {code}'}")
            if steps[self.step] == "mapping":
                if not self._adopt_reference(job):
                    return self._run()   # same step, next seed
            elif steps[self.step] == "exporting cameras":
                self._strip_manual(job)
            self.step += 1
            if self.step < len(steps):
                return self._run()
            self._finish(job)
        except (OSError, ValueError) as e:
            self._failed(str(e))

    REF_ATTEMPTS = 6

    def _adopt_reference(self, job):
        """The mapper writes one model per connected group of cameras; the largest counts. Its
        start is random, and on a borderline rig a run can end with a 2-camera fragment and
        absurd lenses where the next run places every camera (frame 800 of a 7-camera take:
        2 of 6 seeds). So: retry with the next seed until every camera is placed, keeping the
        best attempt. Returns False to run the mapper again."""
        sparse = job["out"] / "distorted" / "sparse"
        models = [d for d in sparse.iterdir() if d.name.isdigit() and (d / "images.bin").exists()]
        if models:
            top = max(models, key=lambda d: colmap_count(d / "images.bin"))
            if colmap_count(top / "images.bin") > job["best"]:
                job["best"] = colmap_count(top / "images.bin")
                shutil.rmtree(sparse / "best", ignore_errors=True)
                top.rename(sparse / "best")
        for d in sparse.iterdir():
            if d.name != "best":
                shutil.rmtree(d)
        job["attempts"] = job["seed"]
        if job["best"] < len(job["images"]) and job["seed"] < self.REF_ATTEMPTS:
            job["seed"] += 1
            return False
        if not job["best"]:
            raise ValueError("no reconstruction: too few matches between the cameras")
        (sparse / "best").rename(sparse / "0")
        (job["scene"] / "point" / "manual").mkdir(exist_ok=True)
        return True

    @staticmethod
    def _strip_manual(job):
        """Reference model -> fixed cameras for point_triangulator: keep the poses and lenses, drop
        the reference frame's 2D/3D points (each frame triangulates its own)."""
        manual = job["scene"] / "point" / "manual"
        lines = [line for line in (manual / "images.txt").read_text().splitlines()
                 if not line.startswith("#")]
        poses = lines[0::2]   # two lines per image: pose, then its 2D points
        (manual / "images.txt").write_text("".join(f"{p}\n\n" for p in poses))
        (manual / "points3D.txt").write_text("")

    def _finish(self, job):
        out = job["out"]
        final = out / "sparse" / "0"
        final.mkdir(parents=True, exist_ok=True)
        manual = job["scene"] / "point" / "manual"
        placed = [line.split()[-1] for line in (manual / "images.txt").read_text().splitlines() if line]
        if job["undistort"]:
            # image_undistorter writes sparse/*.bin; trainers look in sparse/0
            for f in (out / "sparse").glob("*.bin"):
                os.replace(f, final / f.name)
            shutil.rmtree(out / "stereo", ignore_errors=True)
            for f in out.glob("run-colmap-*.sh"):
                f.unlink()
        else:
            # Same layout with the lens model kept (SIMPLE_RADIAL / RADIAL / OPENCV): for trainers
            # that model distortion themselves. images/ = the frame's own images (hard links when
            # the drive allows, so they take no extra space)
            for f in (out / "distorted" / "sparse" / "0").glob("*.bin"):
                shutil.copyfile(f, final / f.name)
            (out / "images").mkdir(exist_ok=True)
            for name in placed:
                try:
                    os.link(job["frame"] / name, out / "images" / name)
                except OSError:
                    shutil.copyfile(job["frame"] / name, out / "images" / name)
        s = self.scenes[job["scene"]]
        if job["kind"] == "ref":
            (job["scene"] / "point" / "reference.json").write_text(json.dumps(
                {"frame": job["frame"].name, "folder": out.name, "model": job["model"],
                 "shared_lens": job["shared"], "undistort": job["undistort"], "cameras": sorted(placed)},
                indent=1) + "\n")
            lost = sorted(set(job["images"]) - set(placed))
            tries = f" (mapper attempt {job['attempts']})" if job["attempts"] > 1 else ""
            s["note"] = (f"reference {job['frame'].name}: {len(placed)}/{len(job['images'])} cameras placed{tries}"
                         + (f", NOT placed (left out of every frame): {', '.join(Path(n).stem for n in lost)}"
                            if lost else ""))
        else:
            # ~1 MB of SIFT descriptors per image; the reference keeps its database
            (out / "distorted" / "database.db").unlink(missing_ok=True)
        s["ok"] += 1
        s["points"] += colmap_count(final / "points3D.bin")
        self._done(job)

    def _failed(self, why):
        job = self.job
        s = self.scenes[job["scene"]]
        s["failed"].append(f"{job['out'].name}: {why}")
        if job["kind"] == "ref":   # nothing to triangulate against: drop the scene's other frames
            dropped = [j for j in self.queue if j["scene"] == job["scene"]]
            self.queue = [j for j in self.queue if j["scene"] != job["scene"]]
            s["total"] -= len(dropped)
            shutil.rmtree(job["scene"] / "point" / "manual", ignore_errors=True)
        self._done(job)

    def _done(self, job):
        s = self.scenes[job["scene"]]
        s["done"] += 1
        if s["done"] >= s["total"]:
            failed = s["failed"]
            self.message.emit(
                f"{job['scene'].name}: SfM {s['ok']}/{s['total']} frame(s) in point/ · {s['note']}"
                + (f" · ~{s['points'] // s['ok']} points per frame" if s["ok"] else "")
                + (f" · FAILED {'; '.join(failed[:3])}" + (f" (+{len(failed) - 3} more)" if len(failed) > 3
                                                          else "") if failed else ""))
        self.job = None
        QTimer.singleShot(0, self._next)   # not recursive: thousands of failing frames in a row


# ---------------------------------------------------------------- Reconstruct: Brush, one splat per frame

BRUSH_DEFAULT = Path(__file__).resolve().parents[1] / "External" / "brush" / "target" / "release" / "brush-cli.exe"
REFINE_LINE = re.compile(r"Refine iter (\d+), (\d+) splats")


def link_or_copy(src, dst):
    """Hard link (no extra space) when the drive allows it, else a copy."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def frame_number(colmap_dir):
    """point/colmap_0 -> 1 (frame_000001)"""
    return int(colmap_dir.name[7:]) + 1


def brush_flavor(exe):
    """What this Brush build understands, from its --help. Brush's options changed after the v0.3.0
    release: --total-steps became --total-train-iters, and distorted COLMAP cameras (SIMPLE_RADIAL,
    OPENCV, …) are only rendered as such since then; v0.3.0 reads them as pinhole. brush-cli and the
    brush app (without --with-viewer) take the same options. Raises ValueError for anything else,
    e.g. the `brush` shell that cargo installs."""
    out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    if "universal splats" not in out and "--export-path" not in out:
        raise ValueError(f"{Path(exe).name} is not the Brush splat trainer")
    if "--total-train-iters" in out:
        return {"steps": "--total-train-iters", "distortion": True}
    if "--total-steps" in out:
        return {"steps": "--total-steps", "distortion": False}
    raise ValueError(f"{Path(exe).name}: unknown Brush version (no step-count option in --help)")


def suspend_process(pid, suspend=True):
    """Freeze or thaw a running process (all its threads) without ending it. Windows has no
    SIGSTOP: ntdll's NtSuspendProcess / NtResumeProcess do the same."""
    if os.name != "nt":
        return os.kill(pid, signal.SIGSTOP if suspend else signal.SIGCONT)
    kernel32, ntdll = ctypes.windll.kernel32, ctypes.windll.ntdll
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x0800, False, pid)   # PROCESS_SUSPEND_RESUME
    if not handle:
        raise OSError(f"can't open process {pid}")
    try:
        status = (ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess)(ctypes.c_void_p(handle))
        if status:
            raise OSError(f"{'suspend' if suspend else 'resume'} failed (NTSTATUS {status & 0xffffffff:#x})")
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def ply_splats(path):
    """Splat count from a PLY header ("element vertex N")."""
    with open(path, "rb") as f:
        head = f.read(4096)
    m = re.search(rb"element vertex (\d+)", head)
    return int(m[1]) if m else 0


def scene_undistorted(scene):
    """Were the scene's SfM frames undistorted (PINHOLE)? From point/reference.json."""
    try:
        return json.loads((scene / "point" / "reference.json").read_text()).get("undistort", True)
    except (OSError, ValueError):
        return True


def sfm_frame_dirs(scene, first=1, last=0):
    """The scene's finished SfM frames (point/colmap_<n>) from frame `first` to `last` (0 = the
    last one), in frame order. Each has its own sparse/0 with the take's cameras (intrinsics and
    poses), so training can start at any of them."""
    point = scene / "point"
    frames = (d for d in point.glob("colmap_*") if (d / "sparse" / "0" / "points3D.bin").exists()) \
        if point.is_dir() else ()
    return sorted((d for d in frames if first <= frame_number(d) and (not last or frame_number(d) <= last)),
                  key=frame_number)


def brush_ply(scene, colmap_dir):
    return scene / "splats" / "brush" / f"frame_{frame_number(colmap_dir):06d}.ply"


def brush_frames(scene):
    out = scene / "splats" / "brush"
    return sum(1 for _ in out.glob("frame_*.ply")) if out.is_dir() else 0


class BrushRunner(QObject):
    """Gaussian splatting with Brush (brush-cli), one splat per frame: a PLY sequence
    <scene>/splats/brush/frame_000001.ply … from the SfM frames <scene>/point/colmap_<n>. All SfM
    frames share one coordinate system, so the PLYs line up as volumetric video.
    Warm start: a frame begins from the previous frame's trained splats (Brush loads an init.ply in
    the dataset folder) and needs fewer steps than a cold start from the COLMAP points. The first
    frame of the chosen range is always cold, and with a keyframe interval every Nth frame of the
    range is cold again (like a video I-frame), so warm-start drift can't build up.
    Each frame trains in a clean work folder (images/ links, sparse/0, init.ply): pointed at
    colmap_<n> itself, Brush would pick distorted/sparse/0 (on a tie it takes the first path) and
    would take any .ply it finds as the starting splats. One frame at a time: it uses the GPU.
    Pause freezes brush-cli where it is (its GPU memory stays allocated) and holds the queue;
    resume carries on with the same frame from the same step."""
    changed = Signal()
    message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.program = str(BRUSH_DEFAULT)
        self.flavor = {"steps": "--total-train-iters", "distortion": True}   # brush_flavor(program)
        self.queue = []      # jobs: {"scene", "data" (colmap_<n>), "ply", "prev", "opts"}
        self.job = None
        self.proc = None
        self.scenes = {}     # scene -> {"total", "done", "ok", "splats", "bytes", "failed"}
        self.paused = False

    def busy(self):
        return bool(self.queue or self.job)

    def pause(self, on):
        """Freeze (on) or thaw the running brush-cli; while paused no new frame starts."""
        if on == self.paused:
            return
        if self.proc and self.proc.processId():
            try:
                suspend_process(self.proc.processId(), on)
            except OSError as e:
                return self.message.emit(f"Brush: can't {'pause' if on else 'resume'}: {e}")
        self.paused = on
        self.message.emit("Brush training paused" if on else "Brush training resumed")
        if not on and not self.job:
            QTimer.singleShot(0, self._next)
        self.changed.emit()

    def _started(self, proc):
        """Paused while brush-cli was still starting: freeze it as soon as it runs."""
        if proc is self.proc and self.paused:
            try:
                suspend_process(proc.processId(), True)
            except OSError as e:
                self.message.emit(f"Brush: can't pause: {e}")

    def busy_scenes(self):
        return {j["scene"] for j in self.queue} | ({self.job["scene"]} if self.job else set())

    def add_scene(self, scene, opts, redo):
        frames = sfm_frame_dirs(scene, opts["first"], opts["last"])
        if not frames:
            return 0
        out = scene / "splats" / "brush"
        plys = [brush_ply(scene, d) for d in frames]
        if redo:   # only the chosen range: other ranges' PLYs stay
            shutil.rmtree(out / ".work", ignore_errors=True)
            for ply in plys:
                ply.unlink(missing_ok=True)
                (out / "logs" / f"{ply.stem}.log").unlink(missing_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        (out / "brush.json").write_text(json.dumps(opts, indent=1) + "\n")
        # Warm start from the previous SfM frame, whatever the gap (lower frame rates skip numbers),
        # except the range's first frame and every keyframe-th one after it: those start cold
        key = opts["keyframe"]
        jobs = [{"scene": scene, "data": d, "ply": ply, "opts": opts,
                 "prev": plys[i - 1] if i and not (key and i % key == 0) else None}
                for i, (d, ply) in enumerate(zip(frames, plys))]
        jobs = [j for j in jobs if not j["ply"].exists()]
        if jobs:
            self.scenes[scene] = {"total": len(jobs), "done": 0, "ok": 0, "splats": 0, "bytes": 0,
                                  "failed": []}
            self.queue += jobs
            QTimer.singleShot(0, self._next)
        return len(jobs)

    def progress(self):
        active = [s for s in self.scenes.values() if s["done"] < s["total"]]
        total = sum(s["total"] for s in active) or 1
        done = sum(s["done"] for s in active)
        now = ""
        if self.job:
            j = self.job
            done += min(j["iter"] / j["steps"], 1)
            now = (f" · {j['scene'].name}/{j['ply'].stem}: {'warm' if j['warm'] else 'cold'} "
                   f"{j['iter']}/{j['steps']} steps, {j['splats']:,} splats")
        return done / total, f"{'PAUSED · ' if self.paused else ''}{len(self.queue) + bool(self.job)} frame(s) left{now}"

    def cancel(self):
        self.queue.clear()
        self.paused = False   # a suspended process can still be killed
        job, self.job = self.job, None
        if self.proc:
            self.proc.kill()
            self.proc.waitForFinished(5000)
        if job:
            shutil.rmtree(job["work"], ignore_errors=True)
        self.message.emit("Brush training cancelled")
        self.changed.emit()

    def _next(self):
        if self.job or not self.queue or self.paused:
            return self.changed.emit()
        job = self.job = self.queue.pop(0)
        opts = job["opts"]
        out = job["ply"].parent
        work = job["work"] = out / ".work"
        prev = job["prev"]
        job["warm"] = bool(opts["warm"] and prev and prev.exists())
        job["steps"] = opts["steps_next"] if job["warm"] else opts["steps_first"]
        job["iter"], job["splats"] = 0, 0
        # A warm frame keeps the previous frame's splat budget: Brush relocates splats at the cap
        # instead of adding them, so every PLY stays the size of the first (a steady data rate).
        # Uncapped, each warm frame grew ~1.5x on the last until max-splats.
        max_splats = opts["max_splats"]
        if job["warm"] and opts.get("hold_count", True):
            try:
                max_splats = max(1000, min(max_splats, ply_splats(prev)))
            except OSError:
                pass
        try:
            shutil.rmtree(work, ignore_errors=True)
            (work / "sparse" / "0").mkdir(parents=True)
            (work / "images").mkdir()
            for f in (job["data"] / "sparse" / "0").glob("*.bin"):
                shutil.copyfile(f, work / "sparse" / "0" / f.name)
            for f in (job["data"] / "images").iterdir():
                link_or_copy(f, work / "images" / f.name)
            if job["warm"]:
                link_or_copy(prev, work / "init.ply")
            # Brush takes a longest side in pixels: the ratio of the largest camera's image
            max_res = max(64, round(colmap_longest_side(work / "sparse" / "0" / "cameras.bin")
                                    / opts["downscale"]))
        except (OSError, KeyError, struct.error) as e:
            return self._failed(f"work folder: {e}")
        steps = job["steps"]
        proc = self.proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("RUST_LOG", "info")   # "Refine iter N, X splats." every refine: the progress
        proc.setProcessEnvironment(env)
        proc.setProgram(self.program)
        proc.setArguments([
            str(work), self.flavor["steps"], str(steps),
            # growth stops halfway, as Brush's default does for its 30000 steps
            "--growth-stop-iter", str(steps // 2),
            "--max-splats", str(max_splats), "--sh-degree", str(opts["sh"]),
            "--max-resolution", str(max_res),
            "--export-every", str(steps), "--export-path", str(work / "export"),
            "--export-name", "result.ply"])
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        job["log"] = []
        proc.readyReadStandardOutput.connect(lambda p=proc: self._read(p))
        proc.finished.connect(lambda code, _s, p=proc: self._finished(p, code))
        proc.started.connect(lambda p=proc: self._started(p))
        proc.errorOccurred.connect(lambda err, p=proc: err == QProcess.ProcessError.FailedToStart
                                   and self._finished(p, -1))
        proc.start()
        self.changed.emit()

    def _read(self, proc):
        if proc is not self.proc or not self.job:
            return
        text = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        self.job["log"].append(text)
        for m in REFINE_LINE.finditer(text):
            self.job["iter"], self.job["splats"] = int(m[1]), int(m[2])
        self.changed.emit()

    def _finished(self, proc, code):
        if proc is not self.proc:
            return
        self._read(proc)
        self.proc = None
        proc.deleteLater()
        job = self.job
        if job is None:
            return  # cancelled
        log = "".join(job["log"])
        logs = job["ply"].parent / "logs"
        logs.mkdir(exist_ok=True)
        (logs / f"{job['ply'].stem}.log").write_text(
            f"brush-cli {' '.join(proc.arguments())}\n\n{log}", encoding="utf-8")
        result = job["work"] / "export" / "result.ply"
        if code != 0 or not result.exists():
            lines = [line for line in log.splitlines() if line.strip()]
            return self._failed("brush-cli not found or won't start (choose it with Brush…)" if code == -1
                                else lines[-1][-200:] if lines else f"exit {code}, no PLY written")
        os.replace(result, job["ply"])
        shutil.rmtree(job["work"], ignore_errors=True)
        s = self.scenes[job["scene"]]
        s["ok"] += 1
        s["splats"] += ply_splats(job["ply"])
        s["bytes"] += job["ply"].stat().st_size
        self._done(job)

    def _failed(self, why):
        job = self.job
        self.scenes[job["scene"]]["failed"].append(f"{job['ply'].stem}: {why}")
        self._done(job)

    def _done(self, job):
        s = self.scenes[job["scene"]]
        s["done"] += 1
        if s["done"] >= s["total"]:
            failed = s["failed"]
            per = (f" · ~{s['splats'] // s['ok']:,} splats, {human_bytes(s['bytes'] / s['ok'])} per frame"
                   if s["ok"] else "")
            self.message.emit(
                f"{job['scene'].name}: Brush {s['ok']}/{s['total']} frame(s) in splats/brush/{per}"
                + (f" · FAILED {'; '.join(failed[:3])}" + (f" (+{len(failed) - 3} more)" if len(failed) > 3
                                                          else "") if failed else ""))
        self.job = None
        QTimer.singleShot(0, self._next)


class PreparePanel(QWidget):
    """Scene folders on this PC (where RIG → Videos downloads to), frame extraction and
    structure from motion (COLMAP)."""
    message = Signal(str)

    def __init__(self):
        super().__init__()
        self.settings = QSettings("IterativeArts", "camctl")
        self.extractor = FrameExtractor(self)
        self.extractor.message.connect(self.message)
        self.extractor.message.connect(lambda _: self.refresh())
        self.extractor.changed.connect(self._progress)
        self.sfm = SfmRunner(self)
        self.sfm.message.connect(self.message)
        self.sfm.message.connect(lambda _: self.refresh())
        self.sfm.changed.connect(self._progress)
        self.brush = BrushRunner(self)
        self.brush.message.connect(self.message)
        self.brush.message.connect(lambda _: self.refresh())
        self.brush.changed.connect(self._progress)

        # Left: the scenes, like the camera list on Capture (select several for batch work)
        self.folder = QLabel()
        self.folder.setWordWrap(True)
        self.folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.folder.setStyleSheet(f"color:{GREY};")
        choose = QPushButton("Choose folder…")
        choose.clicked.connect(self._choose)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        self.table = make_table(["Scene", "Videos", "Frames", "SfM", "PLY"])
        self.table.setToolTip("Select one or more scenes: the buttons on the right work on every selected scene")
        self.table.doubleClicked.connect(lambda _: self._open())
        self.table.itemSelectionChanged.connect(self._show_scene)
        folder_row = QHBoxLayout()
        folder_row.addWidget(choose)
        folder_row.addWidget(refresh)
        folder_row.addStretch()
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.addWidget(QLabel("Scenes folder"))
        left_lay.addWidget(self.folder)
        left_lay.addLayout(folder_row)
        left_lay.addWidget(self.table, 1)
        left.setMinimumWidth(380)

        # Middle: the selected scene, its videos and how far it has come
        self.scene_title = QLabel()
        self.scene_title.setStyleSheet("font-size:16px; font-weight:600;")
        self.video_table = make_table(["Camera", "File", "Size", "Recorded"], stretch=1)
        self.video_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        videos_box = QGroupBox("Videos")
        QVBoxLayout(videos_box).addWidget(self.video_table)
        self.stages = {}
        grid = QGridLayout()
        for i, (key, label) in enumerate([("frames", "Frames"), ("sfm", "SfM"), ("ref", "Reference"),
                                          ("ply", "Splats"), ("brush", "Last training")]):
            grid.addWidget(QLabel(label), i, 0, Qt.AlignmentFlag.AlignTop)
            self.stages[key] = QLabel("–")
            self.stages[key].setWordWrap(True)
            self.stages[key].setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(self.stages[key], i, 1)
        grid.setColumnStretch(1, 1)
        status_box = QGroupBox("Status")
        status_box.setLayout(grid)
        note = QLabel("Extract frames turns each scene's videos into <i>&lt;scene&gt;/frames/frame_000001/"
                      "&lt;camera&gt;.jpg</i> …: one folder per moment with every camera's view of it, for "
                      "COLMAP. Frames are numbered by their timestamp, so a frame one camera dropped "
                      "leaves that camera out of one folder instead of shifting all its later frames. "
                      "Use frame-synced takes: only then does one folder hold one moment.<br>"
                      "SfM runs COLMAP on the reference frame to find every camera's pose and lens, then "
                      "triangulates each frame's points with those same cameras, so all frames share one "
                      "coordinate system: <i>&lt;scene&gt;/point/colmap_0/</i> … (images/ + sparse/0, "
                      "undistorted) for Gaussian splatting trainers such as SpacetimeGaussians.<br>"
                      "Train (Brush) trains one splat per SfM frame into <i>&lt;scene&gt;/splats/brush/"
                      "frame_000001.ply</i> … for the chosen frame range: the range's first frame starts "
                      "cold from its COLMAP points, the rest from the previous frame's splats (cold again "
                      "every N frames with <i>cold every</i>).")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{GREY};")
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self._open)
        open_row = QHBoxLayout()
        open_row.addWidget(open_btn)
        open_row.addStretch()
        center = QWidget()
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.addWidget(self.scene_title)
        center_lay.addWidget(videos_box, 1)
        center_lay.addWidget(status_box)
        center_lay.addLayout(open_row)
        center_lay.addWidget(note)

        # Right: the workflow top to bottom, like the camera controls: 1 extract, 2 SfM, 3 train
        self.fmt = QComboBox()
        self.fmt.addItem("JPEG (high quality, ~0.5-2 MB per image at 4K)", "jpg")
        self.fmt.addItem("PNG (lossless, ~10-15 MB per image at 4K)", "png")
        self.extract_fps = fps_box("Extract at this frame rate: every Nth source frame, N = source fps / this,\n"
                                   "chosen by timestamp (frame numbers keep counting source frames).\n"
                                   "all = every frame")
        self.extract_btn = QPushButton("Extract frames")
        self.extract_btn.setStyleSheet("QPushButton:enabled { font-weight:600; }")
        self.extract_btn.clicked.connect(self._extract)

        # Structure from motion: which frames, which one fixes the cameras, lens model
        self.first = QSpinBox(minimum=1, maximum=999999, value=1)
        self.first.setToolTip("First frame (frame_000001 = 1)")
        self.count = QSpinBox(minimum=0, maximum=999999, value=50)
        self.count.setSpecialValueText("all")
        self.count.setToolTip("How many frames from the first one (SpacetimeGaussians trains 50 at a time)")
        self.sfm_fps = fps_box("Reconstruct at this frame rate, picked from the extracted frames by time\n"
                               "(count = frames after that). all = every extracted frame.\n"
                               "Brush trains every SfM frame, so this sets its frame rate too")
        self.ref = QSpinBox(minimum=0, maximum=999999, value=0)
        self.ref.setSpecialValueText("auto")
        self.ref.setToolTip("Reference frame: full SfM on this one fixes every camera for the take.\n"
                            "auto = the first frame in the range that has every camera.\n"
                            "Pick a moment with lots of texture in view. Only used when starting over.")
        self.lens = QComboBox()
        for text, model in LENS_MODELS:
            self.lens.addItem(text, model)
        self.shared = QCheckBox("shared lens")
        self.shared.setChecked(True)
        self.shared.setToolTip("One lens (focal length, distortion) for every camera: right when the rig uses\n"
                               "one lens model, and far more reliable with one image per camera (on a\n"
                               "7-camera test all cameras were placed on 6 of 6 tries, 2 of 6 without).\n"
                               "Off: a lens per camera (mixed lenses). Kept per scene: changes on Start over.")
        self.undistort = QCheckBox("undistort")
        self.undistort.setChecked(True)
        self.undistort.setToolTip("On: undistorted images + PINHOLE cameras (3DGS, SpacetimeGaussians, most trainers).\n"
                                  "Off: the original images + the lens model with distortion, for trainers\n"
                                  "that handle distortion themselves (e.g. gsplat, nerfstudio with OPENCV).\n"
                                  "Kept per scene: only changes on Start over.")
        self.sfm_btn = QPushButton("SfM")
        self.sfm_btn.setStyleSheet("QPushButton:enabled { font-weight:600; }")
        self.sfm_btn.setToolTip("COLMAP: SIFT, exhaustive matching and mapper on the reference frame, then\n"
                                "the same cameras for every frame (point_triangulator) and undistortion")
        self.sfm_btn.clicked.connect(self._sfm)
        self.colmap_btn = QPushButton("COLMAP…")
        self.colmap_btn.clicked.connect(lambda: self._colmap(choose=True))
        self.colmap_btn.setToolTip(self.settings.value("colmap", "") or "choose colmap.exe")

        # Brush: one splat per SfM frame -> PLY sequence. Splats, SH degree and resolution set
        # the size of every PLY (the data rate of the volumetric video)
        self.train_first = QSpinBox(minimum=1, maximum=999999, value=1)
        self.train_first.setToolTip("First frame to train (frame_000001 = 1): a cold start from that frame's\n"
                                    "COLMAP points and cameras, wherever it sits in the take")
        self.train_last = QSpinBox(minimum=0, maximum=999999, value=0)
        self.train_last.setSpecialValueText("end")
        self.train_last.setToolTip("Last frame to train (inclusive). end = the last SfM frame")
        self.keyframe = QSpinBox(minimum=0, maximum=100000, value=0, suffix=" frames")
        self.keyframe.setSpecialValueText("never")
        self.keyframe.setToolTip("Cold start again every N trained frames of the range (like a video I-frame),\n"
                                 "so warm-start drift can't build up. never = only the range's first frame")
        self.steps_first = QSpinBox(minimum=1000, maximum=200000, singleStep=1000, value=30000)
        self.steps_first.setToolTip("Training steps for a cold start (first frame of the range and keyframes,\n"
                                    "from the COLMAP points)")
        self.steps_next = QSpinBox(minimum=500, maximum=200000, singleStep=500, value=5000)
        self.steps_next.setToolTip("Training steps for a warm start (from the previous frame's splats)")
        self.warm = QCheckBox("warm start")
        self.warm.setChecked(True)
        self.warm.setToolTip("Start each frame from the previous frame's trained splats: faster and\n"
                             "steadier between frames. Off: every frame from its own COLMAP points.")
        self.hold = QCheckBox("hold count")
        self.hold.setChecked(True)
        self.hold.setToolTip("Warm frames keep the previous frame's splat count (Brush relocates splats\n"
                             "instead of adding them): every PLY the size of the first, a steady data rate.\n"
                             "Off: each frame can grow up to max, ~1.5x per frame.")
        self.sh = QComboBox()
        for degree, floats in ((3, 59), (2, 41), (1, 26), (0, 14)):
            self.sh.addItem(f"SH {degree}  ({floats} floats/splat)", degree)
        self.sh.setToolTip("View-dependent colour detail. Lower = smaller PLYs (SH 0 is ~1/4 of SH 3)")
        self.max_splats = QSpinBox(minimum=50, maximum=10000, singleStep=250, value=2000, suffix=" k")
        self.max_splats.setToolTip("Upper limit of splats per frame (thousands). PLY size grows with it:\n"
                                   "1000 k splats at SH 3 is about 240 MB")
        self.scale = QComboBox()
        for n in (1, 2, 4, 8, 16):
            self.scale.addItem(f"1/{n}", n)
        self.scale.setCurrentIndex(1)
        self.scale.setToolTip("Training resolution: 1/1 = the cameras' full images, 1/2 = half width\n"
                              "and height, … Lower trains faster and needs less GPU memory")
        self.train_btn = QPushButton("Train (Brush)")
        self.train_btn.setStyleSheet("QPushButton:enabled { font-weight:600; }")
        self.train_btn.setToolTip("Brush on every SfM frame in the range: <scene>/splats/brush/frame_000001.ply …")
        self.train_btn.clicked.connect(self._train)
        self.brush_btn = QPushButton("Brush…")
        self.brush_btn.clicked.connect(lambda: self._brush(choose=True))
        self.brush_btn.setToolTip(self.settings.value("brush", "") or str(BRUSH_DEFAULT))
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setToolTip("Freeze Brush where it is and resume later from the same step.\n"
                                  "It keeps its GPU memory while paused; Cancel still ends it")
        self.pause_btn.clicked.connect(lambda: self.brush.pause(not self.brush.paused))
        self.train_cancel_btn = QPushButton("Cancel")
        self.train_cancel_btn.setStyleSheet(f"QPushButton:enabled {{ color:{RED}; }}")
        self.train_cancel_btn.setToolTip("Stop training: finished PLYs stay, the current frame is dropped\n"
                                         "(Train again with Keep, add missing to carry on)")
        self.train_cancel_btn.clicked.connect(self.brush.cancel)

        for combo in (self.fmt, self.lens):   # long item texts: don't let them widen the column
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)

        def pair(*widgets):
            row = QHBoxLayout()
            for w in widgets:
                row.addWidget(QLabel(w) if isinstance(w, str) else w, 0 if isinstance(w, str) else 1)
            return row

        def group(title, rows, buttons):
            g = QGridLayout()
            for i, (label, w) in enumerate(rows):
                if label:
                    g.addWidget(QLabel(label), i, 0)
                if isinstance(w, QHBoxLayout):
                    g.addLayout(w, i, 1)
                else:
                    g.addWidget(w, i, 1)
            g.addLayout(pair(*buttons), len(rows), 0, 1, 2)
            g.setColumnStretch(1, 1)
            box = QGroupBox(title)
            box.setLayout(g)
            return box

        extract_box = group("1 · Extract frames", [("Format", self.fmt), ("FPS", self.extract_fps)],
                            [self.extract_btn])
        sfm_box = group("2 · Structure from motion (COLMAP)",
                        [("Frames from", pair(self.first, "count", self.count)), ("FPS", self.sfm_fps),
                         ("Reference", self.ref), ("Lens", self.lens),
                         ("", pair(self.shared, self.undistort))],
                        [self.sfm_btn, self.colmap_btn])
        train_box = group("3 · Train splats (Brush)",
                          [("Frames from", pair(self.train_first, "to", self.train_last)),
                           ("Cold every", self.keyframe), ("Steps cold", self.steps_first),
                           ("Steps warm", self.steps_next), ("", pair(self.warm, self.hold)),
                           ("Detail", self.sh), ("Max splats", self.max_splats), ("Resolution", self.scale)],
                          # while Brush runs, Pause and Cancel take the place of Train and Brush…
                          [self.train_btn, self.brush_btn, self.pause_btn, self.train_cancel_btn])
        self.bar = QProgressBar(maximum=1000)
        self.bar.setTextVisible(False)
        self.bar_text = QLabel()
        self.bar_text.setWordWrap(True)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Stop frame extraction and SfM (Brush has its own Cancel in Train splats)")
        self.cancel_btn.clicked.connect(self._cancel)
        side = QVBoxLayout()
        for box in (extract_box, sfm_box, train_box):
            side.addWidget(box)
        side.addWidget(self.bar)
        side.addWidget(self.bar_text)
        side.addWidget(self.cancel_btn)
        side.addStretch()
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setFixedWidth(400)

        page = QWidget()
        page_lay = QHBoxLayout(page)
        page_lay.addWidget(center, 1)
        page_lay.addWidget(side_w)
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(page)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([460, 1000])
        lay = QVBoxLayout(self)
        lay.addWidget(splitter)
        self.refresh()
        self._progress()

    def root(self):
        return self.settings.value("scene_dir", "") or ""

    def _choose(self):
        if ask_folder(self, "scene_dir", "Scenes folder"):
            self.refresh()

    def refresh(self):
        root = Path(self.root()) if self.root() else None
        self.folder.setText(str(root) if root else "– (choose the folder RIG → Videos downloads to)")
        scenes = sorted((d for d in root.iterdir() if d.is_dir() and any(d.glob("*.mkv"))),
                        key=lambda d: d.stat().st_mtime, reverse=True) if root and root.is_dir() else []
        self.table.setRowCount(len(scenes))
        for r, d in enumerate(scenes):
            videos = sorted(d.glob("*.mkv"))
            frames = d / "frames"
            n = sum(1 for f in frames.iterdir() if f.name.startswith("frame_")) if frames.is_dir() else 0
            items = [cell(d.name, str(d)), cell(str(len(videos)), right=True),
                     cell(str(n) if n else "–", right=True),
                     cell(str(sfm_frames(d)) if sfm_frames(d) else "–", right=True),
                     cell(str(brush_frames(d)) if brush_frames(d) else "–", right=True)]
            for c, item in enumerate(items):
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._show_scene()

    def _show_scene(self):
        """The middle column: the first selected scene's videos and stages."""
        rows = selected_data(self.table)
        scene = Path(rows[0]) if rows else None
        if not scene or not scene.is_dir():
            self.scene_title.setText("select a scene on the left")
            self.video_table.setRowCount(0)
            for lab in self.stages.values():
                lab.setText("–")
            return
        self.scene_title.setText(scene.name + (f"   (+{len(rows) - 1} more selected)" if len(rows) > 1 else ""))
        videos = sorted(scene.glob("*.mkv"))
        self.video_table.setRowCount(len(videos))
        for r, v in enumerate(videos):
            st = v.stat()
            for c, item in enumerate([cell(v.stem.split("_")[0]), cell(v.name), cell(human_bytes(st.st_size), right=True),
                                      cell(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)))]):
                self.video_table.setItem(r, c, item)
        self.video_table.resizeColumnsToContents()
        self.video_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        def span(numbers):
            return f"frame {numbers[0]}" if len(numbers) == 1 else f"frames {numbers[0]}–{numbers[-1]}"
        f = self.stages
        frames = scene / "frames"
        numbers = sorted(int(d.name[6:]) for d in frames.glob("frame_*")) if frames.is_dir() else []
        f["frames"].setText(f"{len(numbers)} folder(s), {span(numbers)}" if numbers else "–  (extract frames)")
        sfm = [frame_number(d) for d in sfm_frame_dirs(scene)]
        f["sfm"].setText(f"{len(sfm)} frame(s), {span(sfm)}" if sfm else "–  (run SfM)")
        try:
            ref = json.loads((scene / "point" / "reference.json").read_text())
            f["ref"].setText(f"{ref['frame']} · {ref['model']}{', shared lens' if ref.get('shared_lens') else ''}"
                             f"{', undistorted' if ref.get('undistort', True) else ''} · "
                             f"{len(ref.get('cameras', []))} camera(s) placed")
        except (OSError, ValueError, KeyError):
            f["ref"].setText("–")
        out = scene / "splats" / "brush"
        plys = sorted(out.glob("frame_*.ply")) if out.is_dir() else []
        f["ply"].setText(f"{len(plys)} PLY(s), {span([int(p.stem[6:]) for p in plys])} · "
                         f"{human_bytes(sum(p.stat().st_size for p in plys))}" if plys else "–  (train)")
        try:
            o = json.loads((out / "brush.json").read_text())
            f["brush"].setText(f"frames {o.get('first', 1)}–{o.get('last') or 'end'} · cold every "
                               f"{o.get('keyframe') or 'never'} · {o['steps_first']}/{o['steps_next']} steps · "
                               f"SH {o['sh']} · max {o['max_splats'] // 1000} k · 1/{o['downscale']}")
        except (OSError, ValueError, KeyError):
            f["brush"].setText("–")

    def _extract(self):
        scenes = [Path(p) for p in selected_data(self.table)]
        if not scenes:
            return self.message.emit("select one or more scenes first")
        if not shutil.which("ffmpeg"):
            return self.message.emit("ffmpeg not found: install it and put it on PATH")
        redo = [s.name for s in scenes if (s / "frames").exists()]
        stale = [s.name for s in scenes if (s / "point").exists()]
        if redo and not confirm(self, "Extract frames", "Already extracted (the frames will be replaced):\n"
                                + "\n".join(redo) + ("\n\nTheir SfM results (point/) will no longer match the "
                                                    "frames: run SfM again and choose Start over." if stale else "")):
            return
        busy = {j["scene"] for j in list(self.extractor.running.values()) + self.extractor.queue}
        busy |= self.sfm.busy_scenes() | self.brush.busy_scenes()
        n = sum(self.extractor.add_scene(s, self.fmt.currentData(), self.extract_fps.value())
                for s in scenes if s not in busy)
        self.message.emit(f"extracting frames from {n} video(s)")
        self.refresh()

    def _colmap(self, choose=False):
        """Path of colmap.exe (or COLMAP.bat), saved in the settings. Use a stable release."""
        exe = self.settings.value("colmap", "") or ""
        if choose or not (exe and Path(exe).exists()):
            start = exe or shutil.which("colmap") or ""
            exe, _ = QFileDialog.getOpenFileName(self, "COLMAP (colmap.exe or COLMAP.bat)", start,
                                                 "COLMAP (colmap.exe COLMAP.bat colmap);;All files (*)")
            if not exe:
                return None
            self.settings.setValue("colmap", exe)
            self.colmap_btn.setToolTip(exe)
        return exe

    def _sfm(self):
        scenes = [Path(p) for p in selected_data(self.table)]
        if not scenes:
            return self.message.emit("select one or more scenes first")
        extracting = {j["scene"] for j in list(self.extractor.running.values()) + self.extractor.queue}
        ready = [s for s in scenes if (s / "frames").is_dir() and s not in extracting]
        if not ready:
            return self.message.emit("extract frames first (and let the extraction finish)")
        if (self.sfm.busy_scenes() | self.brush.busy_scenes()) & set(ready):
            return self.message.emit("SfM or Brush is already running on that scene")
        exe = self._colmap()
        if not exe:
            return
        self.sfm.program, self.sfm.env = colmap_command(exe)
        redo = False
        done = [s.name for s in ready if (s / "point" / "reference.json").exists()]
        if done:
            redo = keep_or_redo(self, "Structure from motion",
                                "These scenes already have a reference (camera poses and lenses):\n"
                                + "\n".join(done) + "\n\nKeep it and add the frames that are missing, "
                                "or start over (new reference, every frame again)?")
            if redo is None:
                return
        n = sum(self.sfm.add_scene(s, self.first.value(), self.count.value(), self.ref.value(),
                                   self.lens.currentData(), self.undistort.isChecked(), redo,
                                   self.sfm_fps.value(), self.shared.isChecked()) for s in ready)
        self.message.emit(f"structure from motion on {n} frame(s)" if n else
                          "SfM: nothing to do, those frames are done")
        self.refresh()

    def _brush(self, choose=False):
        """Path of brush-cli.exe (the Gaussian splatting trainer), saved in the settings. Not the
        `brush` on PATH from cargo: that one is a shell with the same name."""
        exe = self.settings.value("brush", "") or (str(BRUSH_DEFAULT) if BRUSH_DEFAULT.exists() else "")
        if choose or not (exe and Path(exe).exists()):
            exe, _ = QFileDialog.getOpenFileName(self, "Brush trainer (brush-cli.exe)", exe or str(BRUSH_DEFAULT.parent),
                                                 "Brush (brush-cli.exe brush-cli);;All files (*)")
            if not exe:
                return None
            self.settings.setValue("brush", exe)
            self.brush_btn.setToolTip(exe)
        return exe

    def _train(self):
        scenes = [Path(p) for p in selected_data(self.table)]
        if not scenes:
            return self.message.emit("select one or more scenes first")
        ready = [s for s in scenes if sfm_frames(s) and s not in self.sfm.busy_scenes()]
        if not ready:
            return self.message.emit("run SfM first (and let it finish)")
        if self.brush.busy_scenes() & set(ready):
            return self.message.emit("Brush is already training that scene")
        exe = self._brush()
        if not exe:
            return
        try:
            flavor = brush_flavor(exe)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return self.message.emit(f"Brush: {e} (choose brush-cli.exe or brush.exe with Brush…)")
        if self.brush.busy() and exe != self.brush.program:
            return self.message.emit("Brush is training with another binary: wait or cancel first")
        self.brush.program, self.brush.flavor = exe, flavor
        if not flavor["distortion"]:   # e.g. the v0.3.0 release: undistorted SfM frames only
            distorted = [s for s in ready if not scene_undistorted(s)]
            if distorted:
                self.message.emit(f"Brush: this Brush version can't use lens distortion; skipped "
                                  f"{', '.join(s.name for s in distorted)} (SfM with undistort, or a newer Brush)")
            ready = [s for s in ready if s not in distorted]
            if not ready:
                return
        first, last = self.train_first.value(), self.train_last.value()
        span = f"frames {first}-{last or 'end'}"
        if last and last < first:
            return self.message.emit(f"Brush: {span} is empty (to is before from)")
        missing = [s.name for s in ready if not sfm_frame_dirs(s, first, last)]
        if missing:
            self.message.emit(f"Brush: no SfM frames in {span} for {', '.join(missing)}")
        ready = [s for s in ready if s.name not in missing]
        if not ready:
            return
        redo = False
        done = [s.name for s in ready if any(brush_ply(s, d).exists() for d in sfm_frame_dirs(s, first, last))]
        if done:
            redo = keep_or_redo(self, "Train (Brush)",
                                f"These scenes already have trained frames in {span} (splats/brush/):\n"
                                + "\n".join(done) + "\n\nKeep them and train only the missing frames, or start "
                                "over (every frame in the range again with the current settings; frames "
                                "outside the range are kept)?")
            if redo is None:
                return
        opts = {"first": first, "last": last, "keyframe": self.keyframe.value(),
                "steps_first": self.steps_first.value(), "steps_next": self.steps_next.value(),
                "warm": self.warm.isChecked(), "hold_count": self.hold.isChecked(), "sh": self.sh.currentData(),
                "max_splats": self.max_splats.value() * 1000, "downscale": self.scale.currentData()}
        n = sum(self.brush.add_scene(s, opts, redo) for s in ready)
        self.message.emit(f"Brush: training {n} frame(s), {span}" if n else
                          f"Brush: nothing to do, every SfM frame in {span} has a PLY")
        self.refresh()

    def _cancel(self):
        for runner in (self.extractor, self.sfm):
            if runner.busy():
                runner.cancel()

    def _open(self):
        rows = selected_data(self.table)
        target = Path(rows[0]) if rows else Path(self.root()) if self.root() else None
        if target and target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target / "frames" if (target / "frames").is_dir()
                                                            else target)))

    def _progress(self):
        busy = [(name, r) for name, r in (("Extracting", self.extractor), ("SfM", self.sfm),
                                          ("Brush", self.brush)) if r.busy()]
        self.bar.setVisible(bool(busy))
        self.bar_text.setVisible(bool(busy))
        self.cancel_btn.setVisible(self.extractor.busy() or self.sfm.busy())
        training = self.brush.busy()
        for w in (self.train_btn, self.brush_btn):
            w.setVisible(not training)
        for w in (self.pause_btn, self.train_cancel_btn):
            w.setVisible(training)
        self.pause_btn.setText("Resume" if self.brush.paused else "Pause")
        self.pause_btn.setStyleSheet(f"QPushButton:enabled {{ color:{AMBER}; font-weight:600; }}"
                                     if self.brush.paused else "")
        if busy:
            name, runner = busy[0]
            frac, text = runner.progress()
            self.bar.setValue(int(frac * 1000))
            self.bar_text.setText(f"<b>{name}: {frac:.0%}</b> · {text.lstrip(' ·')}")


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Camera Rig")
        self.resize(1400, 860)
        self.links = []           # cameras from cameras.json (in list order)
        self.items = {}           # link -> list item
        self.downloads = Downloads(self)
        self.list = QListWidget()
        self.list.setMaximumWidth(240)
        self.pages = QStackedWidget()
        self.list.currentItemChanged.connect(
            lambda item, _: item and self.pages.setCurrentWidget(item.data(Qt.ItemDataRole.UserRole)))
        splitter = QSplitter()
        splitter.addWidget(self.list)
        splitter.addWidget(self.pages)
        splitter.setStretchFactor(1, 1)
        # Top level: Capture (cameras, rig) | Prepare (frames for reconstruction)
        self.prepare = PreparePanel()
        self.prepare.message.connect(self.show_message)
        self.top = QTabWidget()
        self.top.setDocumentMode(True)
        self.top.setStyleSheet("QTabBar::tab { padding: 6px 28px; font-size: 13px; font-weight: 600; }")
        self.top.addTab(splitter, "Capture")
        self.top.addTab(self.prepare, "Prepare")
        self.setCentralWidget(self.top)

        bar = self.addToolBar("Cameras")
        bar.setMovable(False)
        self.top.currentChanged.connect(lambda i: (bar.setVisible(i == 0), i == 1 and self.prepare.refresh()))
        for text, slot in [("Add camera…", self.add_camera), ("Remove camera", self.remove_camera)]:
            act = QAction(text, self)
            act.triggered.connect(slot)
            bar.addAction(act)

        self.dl_bar = QProgressBar(maximum=1000)
        self.dl_bar.setMaximumWidth(420)
        self.dl_bar.hide()
        self.statusBar().addPermanentWidget(self.dl_bar)
        self.downloads.changed.connect(self._downloads_changed)
        self.downloads.message.connect(self.show_message)

        # (RIG) first, then the NEW-camera watcher (hidden unless a new camera answers), then cameras
        self.rig = RigPanel(self, self.downloads)
        self.rig.message.connect(self.show_message)
        rig_item = QListWidgetItem("◆  RIG")
        rig_item.setData(Qt.ItemDataRole.UserRole, self.rig)
        f = rig_item.font()
        f.setBold(True)
        rig_item.setFont(f)
        self.list.addItem(rig_item)
        self.pages.addWidget(self.rig)
        self.rig_item = rig_item

        self.new_link = self._add(BASE_IP, save=False)

        for host in self.load_hosts():
            if host != BASE_IP:
                self._add(host)
        self.list.setCurrentRow(0)
        self.rig_timer = QTimer(self, interval=1000)
        self.rig_timer.timeout.connect(self._update_rig_item)
        self.rig_timer.start()
        self.statusBar().showMessage(f"{len(self.links)} camera(s) from {CONFIG.name}")

    def show_message(self, text):
        self.statusBar().showMessage(text, 15000)

    def rig_links(self):
        """Cameras that rig-wide actions go to: online and configured (never NEW)."""
        return [l for l in self.links if l.in_rig]

    @staticmethod
    def load_hosts():
        try:
            return [c["host"] for c in json.loads(CONFIG.read_text())]
        except (OSError, ValueError, KeyError):
            return []

    def save_hosts(self):
        CONFIG.write_text(json.dumps([{"host": l.host} for l in self.links], indent=1) + "\n")

    def _add(self, host, save=True):
        link = CameraLink(host, self)
        panel = CameraPanel(link, self.downloads)
        item = QListWidgetItem(host)
        item.setData(Qt.ItemDataRole.UserRole, panel)
        self.items[link] = item
        self.list.addItem(item)
        self.pages.addWidget(panel)
        if host != BASE_IP:
            self.links.append(link)
        panel.message.connect(self.show_message)
        panel.renumbered.connect(self._renumbered)
        link.failed.connect(self.show_message)

        def update_item(_=None):
            s = link.last
            state = s.get("state", "offline")
            if not link.configured:
                item.setText(f"★  NEW   {link.ident.get('ip', host)}")
                item.setForeground(QColor(AMBER))
                item.setHidden(host == BASE_IP and state == "offline")
            else:
                rec = "  ● REC" if s.get("recording") else ""
                item.setText(f"●  {link.name}   {state}{rec}")
                item.setForeground(QColor(STATE_COLORS.get(state, "#888")))
            item.setToolTip(f"{link.host}\n{link.ident.get('hostname', '')}  {link.ident.get('mac', '')}")
        link.status.connect(update_item)
        link.hello.connect(update_item)
        update_item()
        if save:
            self.save_hosts()
        return link

    def _update_rig_item(self):
        cams = self.rig_links()
        rec = "  ● REC" if any(l.last.get("recording") for l in cams) else ""
        self.rig_item.setText(f"◆  RIG   {len(cams)} camera(s){rec}")

    def _remove(self, link):
        item = self.items.pop(link)
        page = item.data(Qt.ItemDataRole.UserRole)
        self.list.takeItem(self.list.row(item))
        self.pages.removeWidget(page)
        page.deleteLater()
        link.close()
        if link in self.links:
            self.links.remove(link)

    def _renumbered(self, link, new_host):
        """A camera got a new IP: watch the new address (the NEW watcher itself stays)."""
        if link is not self.new_link:
            self._remove(link)
        existing = next((l for l in self.links if l.host == new_host), None)
        if not existing:
            existing = self._add(new_host)
        self.save_hosts()
        self.list.setCurrentItem(self.items[existing])

    def add_camera(self):
        host, ok = QInputDialog.getText(self, "Add camera", "Camera IP address:")
        host = host.strip()
        if ok and host and host != BASE_IP and host not in [l.host for l in self.links]:
            link = self._add(host)
            self.list.setCurrentItem(self.items[link])

    def remove_camera(self):
        item = self.list.currentItem()
        link = next((l for l, i in self.items.items() if i is item), None)
        if not link or link is self.new_link:
            return
        if confirm(self, "Remove camera", f"Remove {link.name} ({link.host}) from this list?\n"
                   "(The camera itself is not changed.)"):
            self._remove(link)
            self.save_hosts()

    def _downloads_changed(self):
        if not self.downloads.busy():
            self.dl_bar.hide()
            return
        frac, text = self.downloads.progress()
        self.dl_bar.setValue(int(frac * 1000))
        self.dl_bar.setFormat(f"Downloading: {text}")
        self.dl_bar.show()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("camctl")
    if not CONFIG.exists():
        CONFIG.write_text("[]\n")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
