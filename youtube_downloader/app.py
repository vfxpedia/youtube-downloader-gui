from __future__ import annotations

import os
import sys
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
import subprocess

from PySide6.QtCore import QByteArray, QObject, QThread, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .downloader import (
    CancelToken,
    DownloadCancelled,
    DownloadError,
    DownloadRequest,
    MediaEntry,
    MediaInfo,
    check_dependencies,
    fetch_media_info,
    preview_filename,
    run_download,
)
from .settings import load_settings, save_settings


MODE_LABELS = {
    "video": "영상 MP4",
    "audio": "음원 MP3",
}

QUALITY_LABELS = {
    "best": "최고화질",
    "2160": "2160p 이하",
    "1440": "1440p 이하",
    "1080": "1080p 이하",
    "720": "720p 이하",
    "480": "480p 이하",
    "360": "360p 이하",
}

FILENAME_MODE_LABELS = {
    "title": "제목만",
    "numbered": "001_번호 + 제목",
}

FOLDER_MODE_LABELS = {
    "playlist": "재생목록명 폴더 자동 생성",
    "root": "선택 폴더에 바로 저장",
}

DUPLICATE_MODE_LABELS = {
    "skip": "이미 있으면 건너뛰기",
    "overwrite": "덮어쓰기",
    "unique": "새 이름으로 저장",
    "archive": "다운로드 기록 기준 건너뛰기",
}

SUBTITLE_MODE_LABELS = {
    "none": "자막 받지 않음",
    "manual": "공식 자막 SRT",
    "auto": "자동 자막 SRT",
    "both": "공식 + 자동 자막 SRT",
}

RESULT_FILTER_LABELS = {
    "all": "전체",
    "problem": "문제만",
    "ok": "정상",
    "incomplete": "미완료",
    "review": "확인 필요",
    "missing": "누락",
}


@dataclass(frozen=True)
class QueueJob:
    title: str
    item_count: int
    request: DownloadRequest
    entries: list[MediaEntry]


@dataclass(frozen=True)
class AuditResult:
    status: str
    job_title: str
    index: int
    title: str
    expected_path: Path
    actual_path: Path | None
    reason: str
    action: str
    request: DownloadRequest
    entry: MediaEntry


class MetadataWorker(QObject):
    info_loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url

    @Slot()
    def run(self) -> None:
        try:
            self.info_loaded.emit(fetch_media_info(self._url))
        except Exception as exc:
            self.failed.emit(str(exc))


class DownloadWorker(QObject):
    progress = Signal(float, float, str)
    log = Signal(str)
    finished = Signal()
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, jobs: list[QueueJob]) -> None:
        super().__init__()
        self._jobs = jobs
        self._cancel_token = CancelToken()

    @Slot()
    def run(self) -> None:
        total_items = max(1, sum(job.item_count for job in self._jobs))
        completed_items = 0

        try:
            for job_index, job in enumerate(self._jobs, start=1):
                current_item = 1
                item_total = max(1, job.item_count)
                self.log.emit(f"[{job_index}/{len(self._jobs)}] {job.title} 시작")

                def item_callback(index: int, reported_total: int) -> None:
                    nonlocal current_item, item_total
                    current_item = max(1, index)
                    item_total = max(item_total, reported_total)
                    status = f"{job.title} - 항목 {current_item}/{item_total}"
                    self.progress.emit(_overall_percent(completed_items, current_item, 0, total_items), 0.0, status)

                def progress_callback(percent: float | None, detail: str) -> None:
                    current_percent = percent or 0.0
                    overall = _overall_percent(completed_items, current_item, current_percent, total_items)
                    status = f"{job.title} - {detail}"
                    self.progress.emit(overall, current_percent, status)

                run_download(
                    job.request,
                    self._cancel_token,
                    progress_callback,
                    self.log.emit,
                    item_callback,
                )
                completed_items += item_total

        except DownloadCancelled:
            self.cancelled.emit()
        except (DownloadError, OSError, ValueError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"예상하지 못한 오류: {exc}")
        else:
            self.finished.emit()

    def cancel(self) -> None:
        self._cancel_token.cancel()


def _overall_percent(completed_items: int, current_item: int, current_percent: float, total_items: int) -> float:
    current_offset = max(0, current_item - 1)
    value = (completed_items + current_offset + (current_percent / 100.0)) / max(1, total_items)
    return max(0.0, min(100.0, value * 100.0))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("YouTube Downloader GUI")
        self.resize(1180, 720)

        self._settings = load_settings()
        self._preview_info: MediaInfo | None = None
        self._preview_name_token = ""
        self._queue: list[QueueJob] = []
        self._info_thread: QThread | None = None
        self._info_worker: MetadataWorker | None = None
        self._download_thread: QThread | None = None
        self._download_worker: DownloadWorker | None = None
        self._continue_after_cleanup = False
        self._active_jobs: list[QueueJob] = []
        self._last_audit_results: list[AuditResult] = []
        self._paused_jobs: list[QueueJob] = []
        self._pause_requested = False
        self._session_log_path: Path | None = None

        self.url_input = QLineEdit(str(self._settings.get("last_url", "")))
        self.url_input.setPlaceholderText("YouTube 영상 또는 재생목록 URL")

        self.output_input = QLineEdit(str(self._settings["last_output_dir"]))
        self.output_button = QPushButton("폴더 선택")
        self.output_button.clicked.connect(self.choose_output_dir)

        self.mode_combo = QComboBox()
        for mode, label in MODE_LABELS.items():
            self.mode_combo.addItem(label, mode)
        selected_index = self.mode_combo.findData(self._settings.get("last_mode", "video"))
        self.mode_combo.setCurrentIndex(max(selected_index, 0))
        self.mode_combo.currentIndexChanged.connect(self.update_quality_enabled)

        self.quality_combo = QComboBox()
        for quality, label in QUALITY_LABELS.items():
            self.quality_combo.addItem(label, quality)
        selected_quality = self.quality_combo.findData(self._settings.get("last_quality", "best"))
        self.quality_combo.setCurrentIndex(max(selected_quality, 0))
        self.quality_combo.currentIndexChanged.connect(self.refresh_preview_filenames)

        self.filename_mode_combo = QComboBox()
        for mode, label in FILENAME_MODE_LABELS.items():
            self.filename_mode_combo.addItem(label, mode)
        saved_filename_mode = self._settings.get("last_filename_mode", "title")
        if saved_filename_mode == "playlist_folder":
            saved_filename_mode = "numbered"
        selected_filename_mode = self.filename_mode_combo.findData(saved_filename_mode)
        self.filename_mode_combo.setCurrentIndex(max(selected_filename_mode, 0))
        self.filename_mode_combo.currentIndexChanged.connect(self.refresh_preview_filenames)

        self.folder_mode_combo = QComboBox()
        for mode, label in FOLDER_MODE_LABELS.items():
            self.folder_mode_combo.addItem(label, mode)
        selected_folder_mode = self.folder_mode_combo.findData(self._settings.get("last_folder_mode", "playlist"))
        self.folder_mode_combo.setCurrentIndex(max(selected_folder_mode, 0))
        self.folder_mode_combo.currentIndexChanged.connect(self.update_folder_controls)

        self.folder_name_input = QLineEdit()
        self.folder_name_input.setPlaceholderText("목록을 불러오면 재생목록명이 들어갑니다.")
        self.folder_name_input.textChanged.connect(self.refresh_preview_filenames)

        self.duplicate_mode_combo = QComboBox()
        for mode, label in DUPLICATE_MODE_LABELS.items():
            self.duplicate_mode_combo.addItem(label, mode)
        selected_duplicate_mode = self.duplicate_mode_combo.findData(self._settings.get("last_duplicate_mode", "skip"))
        self.duplicate_mode_combo.setCurrentIndex(max(selected_duplicate_mode, 0))
        self.duplicate_mode_combo.currentIndexChanged.connect(self.refresh_preview_filenames)

        self.subtitle_combo = QComboBox()
        for mode, label in SUBTITLE_MODE_LABELS.items():
            self.subtitle_combo.addItem(label, mode)
        selected_subtitle_mode = self.subtitle_combo.findData(self._settings.get("last_subtitle_mode", "none"))
        self.subtitle_combo.setCurrentIndex(max(selected_subtitle_mode, 0))

        self.open_folder_on_finish_checkbox = QCheckBox("완료 후 저장 폴더 열기")
        self.open_folder_on_finish_checkbox.setChecked(bool(self._settings.get("open_folder_on_finish", False)))

        self.preview_button = QPushButton("1. 목록 불러오기")
        self.preview_button.clicked.connect(self.load_preview)
        self.add_queue_button = QPushButton("2. 대기열 추가")
        self.add_queue_button.setEnabled(False)
        self.add_queue_button.clicked.connect(self.add_preview_to_queue)
        self.download_button = QPushButton("3. 다운로드 시작")
        self.download_button.clicked.connect(self.start_download)
        self.cancel_button = QPushButton("일시정지")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_download)
        self.resume_button = QPushButton("이어받기")
        self.resume_button.setEnabled(False)
        self.resume_button.clicked.connect(self.resume_download)
        self.clear_queue_button = QPushButton("대기열 비우기")
        self.clear_queue_button.clicked.connect(self.clear_queue)
        self.move_up_button = QPushButton("위로")
        self.move_up_button.clicked.connect(self.move_queue_item_up)
        self.move_down_button = QPushButton("아래로")
        self.move_down_button.clicked.connect(self.move_queue_item_down)
        self.remove_queue_button = QPushButton("삭제")
        self.remove_queue_button.clicked.connect(self.remove_queue_item)
        self.retry_problem_button = QPushButton("문제 항목만 다시 받기")
        self.retry_problem_button.setEnabled(False)
        self.retry_problem_button.clicked.connect(self.retry_problem_items)
        self.result_filter_combo = QComboBox()
        for mode, label in RESULT_FILTER_LABELS.items():
            self.result_filter_combo.addItem(label, mode)
        self.result_filter_combo.currentIndexChanged.connect(self.refresh_result_filter)
        self.open_output_button = QPushButton("저장 폴더 열기")
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.open_report_button = QPushButton("세션 리포트 열기")
        self.open_report_button.clicked.connect(self.open_session_report)

        self.dependency_label = QLabel()
        self.refresh_button = QPushButton("상태 새로고침")
        self.refresh_button.clicked.connect(self.refresh_dependencies)

        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setRange(0, 100)
        self.current_progress_bar = QProgressBar()
        self.current_progress_bar.setRange(0, 100)
        self.status_label = QLabel("대기 중")
        self.status_label.setWordWrap(True)

        self.preview_title_label = QLabel("URL을 분석하면 받을 항목이 여기에 표시됩니다.")
        self.preview_title_label.setWordWrap(True)
        self.preview_table = QTableWidget(0, 4)
        self.preview_table.setHorizontalHeaderLabels(["#", "제목", "길이", "예상 파일명"])
        self._setup_table(self.preview_table)
        self._set_column_widths(self.preview_table, [54, 320, 80, 520])

        self.queue_table = QTableWidget(0, 8)
        self.queue_table.setHorizontalHeaderLabels(["제목", "항목", "형식", "화질", "저장 폴더", "파일명", "중복/자막", "URL"])
        self._setup_table(self.queue_table)
        self._set_column_widths(self.queue_table, [240, 70, 100, 110, 240, 160, 180, 360])

        self.result_summary_label = QLabel("다운로드 후 결과 검증이 여기에 표시됩니다.")
        self.result_summary_label.setWordWrap(True)
        self.result_table = QTableWidget(0, 6)
        self.result_table.setHorizontalHeaderLabels(["번호", "제목", "상태", "실제 파일", "문제 원인", "조치"])
        self._setup_table(self.result_table)
        self._set_column_widths(self.result_table, [64, 320, 96, 520, 300, 220])
        self.result_table.setSortingEnabled(True)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)

        self.tray_icon = QSystemTrayIcon(self)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        self.tray_icon.setIcon(icon)
        self.tray_icon.setToolTip("YouTube Downloader GUI")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()

        self._build_layout()
        self._apply_style()
        self._restore_ui_state()
        self.refresh_dependencies()
        self.update_quality_enabled()
        self.update_folder_controls()

    def _setup_table(self, table: QTableWidget) -> None:
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionsMovable(True)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

    def _set_column_widths(self, table: QTableWidget, widths: list[int]) -> None:
        for column, width in enumerate(widths):
            table.setColumnWidth(column, width)

    def _build_layout(self) -> None:
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 16, 16, 16)

        self.main_splitter = QSplitter()
        left = QWidget()
        left.setMaximumWidth(650)
        left_layout = QVBoxLayout(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)

        input_group = QGroupBox("다운로드 설정")
        input_layout = QGridLayout(input_group)
        input_layout.setColumnStretch(1, 1)
        input_layout.addWidget(QLabel("URL"), 0, 0)
        input_layout.addWidget(self.url_input, 0, 1, 1, 3)
        input_layout.addWidget(QLabel("저장 위치"), 1, 0)
        input_layout.addWidget(self.output_input, 1, 1, 1, 2)
        input_layout.addWidget(self.output_button, 1, 3)
        input_layout.addWidget(QLabel("형식"), 2, 0)
        input_layout.addWidget(self.mode_combo, 2, 1)
        input_layout.addWidget(QLabel("화질"), 2, 2)
        input_layout.addWidget(self.quality_combo, 2, 3)
        input_layout.addWidget(QLabel("파일명"), 3, 0)
        input_layout.addWidget(self.filename_mode_combo, 3, 1)
        input_layout.addWidget(QLabel("폴더"), 3, 2)
        input_layout.addWidget(self.folder_mode_combo, 3, 3)
        input_layout.addWidget(QLabel("폴더명"), 4, 0)
        input_layout.addWidget(self.folder_name_input, 4, 1, 1, 3)
        input_layout.addWidget(QLabel("중복"), 5, 0)
        input_layout.addWidget(self.duplicate_mode_combo, 5, 1, 1, 3)
        input_layout.addWidget(QLabel("자막"), 6, 0)
        input_layout.addWidget(self.subtitle_combo, 6, 1, 1, 3)
        input_layout.addWidget(self.open_folder_on_finish_checkbox, 7, 1, 1, 3)

        action_layout = QHBoxLayout()
        action_layout.addWidget(self.preview_button)
        action_layout.addWidget(self.add_queue_button)
        action_layout.addWidget(self.download_button)
        action_layout.addWidget(self.cancel_button)
        action_layout.addWidget(self.resume_button)
        action_layout.addWidget(self.clear_queue_button)
        action_layout.addStretch(1)

        progress_group = QGroupBox("진행 상태")
        progress_layout = QGridLayout(progress_group)
        progress_layout.addWidget(QLabel("전체"), 0, 0)
        progress_layout.addWidget(self.overall_progress_bar, 0, 1)
        progress_layout.addWidget(QLabel("현재 항목"), 1, 0)
        progress_layout.addWidget(self.current_progress_bar, 1, 1)
        progress_layout.addWidget(self.status_label, 2, 0, 1, 2)

        dependency_group = QGroupBox("실행 환경")
        dependency_layout = QHBoxLayout(dependency_group)
        dependency_layout.addWidget(self.dependency_label, 1)
        dependency_layout.addWidget(self.refresh_button)

        log_group = QGroupBox("로그")
        log_layout = QVBoxLayout(log_group)
        log_layout.addWidget(self.log_view)

        preview_group = QGroupBox("받을 목록 미리보기")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.addWidget(self.preview_title_label)
        preview_layout.addWidget(self.preview_table)

        queue_group = QGroupBox("다운로드 대기열")
        queue_layout = QVBoxLayout(queue_group)
        queue_layout.addWidget(self.queue_table)
        queue_action_layout = QHBoxLayout()
        queue_action_layout.addWidget(self.move_up_button)
        queue_action_layout.addWidget(self.move_down_button)
        queue_action_layout.addWidget(self.remove_queue_button)
        queue_action_layout.addStretch(1)
        queue_layout.addLayout(queue_action_layout)

        result_group = QGroupBox("결과 검증")
        result_layout = QVBoxLayout(result_group)
        result_layout.addWidget(self.result_summary_label)
        result_filter_layout = QHBoxLayout()
        result_filter_layout.addWidget(QLabel("보기"))
        result_filter_layout.addWidget(self.result_filter_combo)
        result_filter_layout.addStretch(1)
        result_layout.addLayout(result_filter_layout)
        result_layout.addWidget(self.result_table)
        result_action_layout = QHBoxLayout()
        result_action_layout.addWidget(self.retry_problem_button)
        result_action_layout.addWidget(self.open_output_button)
        result_action_layout.addWidget(self.open_report_button)
        result_action_layout.addStretch(1)
        result_layout.addLayout(result_action_layout)

        self.right_tabs = QTabWidget()
        self.right_tabs.addTab(preview_group, "받을 목록")
        self.right_tabs.addTab(queue_group, "대기열")
        self.right_tabs.addTab(result_group, "결과 검증")

        left_layout.addWidget(input_group)
        left_layout.addLayout(action_layout)
        left_layout.addWidget(progress_group)
        left_layout.addWidget(dependency_group)
        left_layout.addWidget(log_group, 1)

        right_layout.addWidget(self.right_tabs, 1)

        self.main_splitter.addWidget(left)
        self.main_splitter.addWidget(right)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([560, 900])
        root_layout.addWidget(self.main_splitter)
        self.setCentralWidget(central)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget { font-size: 14px; }
            QGroupBox {
                font-weight: 600;
                border: 1px solid #c7c7c7;
                border-radius: 6px;
                margin-top: 10px;
                padding: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QLineEdit, QComboBox, QTextEdit, QTableWidget {
                border: 1px solid #b8b8b8;
                border-radius: 4px;
                padding: 5px;
            }
            QPushButton {
                border: 1px solid #8d8d8d;
                border-radius: 4px;
                padding: 7px 12px;
                background: #f4f4f4;
            }
            QPushButton:hover { background: #e9f2ff; }
            QPushButton:disabled {
                color: #8a8a8a;
                background: #eeeeee;
            }
            """
        )

    def _restore_ui_state(self) -> None:
        self._restore_byte_array(self.restoreGeometry, "window_geometry")
        self._restore_byte_array(self.restoreState, "window_state")
        self._restore_byte_array(self.main_splitter.restoreState, "splitter_state")
        self._restore_header_state(self.preview_table, "preview_header_state")
        self._restore_header_state(self.queue_table, "queue_header_state")
        self._restore_header_state(self.result_table, "result_header_state")
        tab_index = self._settings.get("right_tab_index", 0)
        if isinstance(tab_index, int) and 0 <= tab_index < self.right_tabs.count():
            self.right_tabs.setCurrentIndex(tab_index)

    def _restore_byte_array(self, restore: object, key: str) -> None:
        encoded = self._settings.get(key, "")
        if not isinstance(encoded, str) or not encoded:
            return
        try:
            restore(QByteArray.fromBase64(encoded.encode("ascii")))  # type: ignore[misc]
        except (TypeError, ValueError):
            return

    def _restore_header_state(self, table: QTableWidget, key: str) -> None:
        encoded = self._settings.get(key, "")
        if not isinstance(encoded, str) or not encoded:
            return
        table.horizontalHeader().restoreState(QByteArray.fromBase64(encoded.encode("ascii")))

    def _save_ui_state(self) -> None:
        save_settings(
            {
                "window_geometry": _encode_qbytearray(self.saveGeometry()),
                "window_state": _encode_qbytearray(self.saveState()),
                "splitter_state": _encode_qbytearray(self.main_splitter.saveState()),
                "right_tab_index": self.right_tabs.currentIndex(),
                "preview_header_state": _encode_qbytearray(self.preview_table.horizontalHeader().saveState()),
                "queue_header_state": _encode_qbytearray(self.queue_table.horizontalHeader().saveState()),
                "result_header_state": _encode_qbytearray(self.result_table.horizontalHeader().saveState()),
            }
        )

    @Slot()
    def choose_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "저장 위치 선택",
            self.output_input.text() or str(Path.home() / "Downloads"),
        )
        if selected:
            self.output_input.setText(selected)

    @Slot()
    def refresh_dependencies(self) -> None:
        status = check_dependencies()
        self.dependency_label.setText(
            f"Python: {status.python_path}\n"
            f"yt-dlp: {status.yt_dlp_version or '설치 필요'}\n"
            f"ffmpeg: {status.ffmpeg_path or '설치 필요'}"
        )

    @Slot()
    @Slot(int)
    def update_quality_enabled(self, _index: int | None = None) -> None:
        is_video = self.mode_combo.currentData() == "video"
        self.quality_combo.setEnabled(is_video)
        self.quality_combo.setToolTip("" if is_video else "음원 MP3는 최고 음질로 추출합니다.")
        self.refresh_preview_filenames()

    @Slot()
    @Slot(int)
    def update_folder_controls(self, _index: int | None = None) -> None:
        uses_playlist_folder = self.folder_mode_combo.currentData() == "playlist"
        self.folder_name_input.setEnabled(uses_playlist_folder)
        if uses_playlist_folder:
            self.folder_name_input.setPlaceholderText("재생목록명 또는 직접 입력한 폴더명")
        else:
            self.folder_name_input.setPlaceholderText("선택한 저장 위치에 바로 저장됩니다.")
        self.refresh_preview_filenames()

    @Slot()
    @Slot(int)
    def refresh_preview_filenames(self, _index: int | None = None) -> None:
        if self._preview_info is None:
            return
        request = self._current_request(self._preview_info, require_output=False)
        for row, entry in enumerate(self._preview_info.entries):
            self.preview_table.setItem(row, 3, QTableWidgetItem(preview_filename(entry.title, row + 1, request)))

    @Slot()
    def load_preview(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "URL 필요", "분석할 YouTube URL을 입력하세요.")
            return
        status = check_dependencies()
        if not status.yt_dlp_ready:
            QMessageBox.warning(self, "yt-dlp 설치 필요", "run_app.bat로 실행해 필요한 패키지를 설치하세요.")
            return

        self.preview_button.setEnabled(False)
        self.add_queue_button.setEnabled(False)
        self.preview_title_label.setText("URL 분석 중...")
        self.preview_table.setRowCount(0)
        self._preview_info = None
        self._preview_name_token = ""
        self.folder_name_input.clear()

        self._info_thread = QThread(self)
        self._info_worker = MetadataWorker(url)
        self._info_worker.moveToThread(self._info_thread)
        self._info_thread.started.connect(self._info_worker.run)
        self._info_worker.info_loaded.connect(self.preview_loaded)
        self._info_worker.failed.connect(self.preview_failed)
        self._info_worker.info_loaded.connect(self._info_thread.quit)
        self._info_worker.failed.connect(self._info_thread.quit)
        self._info_thread.finished.connect(self._cleanup_info_worker)
        self._info_thread.start()

    @Slot(object)
    def preview_loaded(self, info: MediaInfo) -> None:
        self._preview_info = info
        original_url = self.url_input.text().strip()
        if original_url and original_url != info.url:
            self.url_input.setText(info.url)
            self._append_log(f"URL 자동 보정: {info.url}")
        signals_blocked = self.folder_name_input.blockSignals(True)
        self.folder_name_input.setText(info.title)
        self.folder_name_input.blockSignals(signals_blocked)
        self.preview_title_label.setText(f"{info.title} - {info.item_count}개 항목")
        self.preview_table.setRowCount(0)
        for index, entry in enumerate(info.entries, start=1):
            row = self.preview_table.rowCount()
            self.preview_table.insertRow(row)
            self.preview_table.setItem(row, 0, QTableWidgetItem(str(index)))
            self.preview_table.setItem(row, 1, QTableWidgetItem(entry.title))
            self.preview_table.setItem(row, 2, QTableWidgetItem(_format_duration(entry.duration)))
        self.refresh_preview_filenames()
        self.add_queue_button.setEnabled(True)
        self.preview_button.setEnabled(True)
        self._append_log(f"목록 분석 완료: {info.title} ({info.item_count}개)")

    @Slot(str)
    def preview_failed(self, message: str) -> None:
        self.preview_title_label.setText("URL 분석 실패")
        self.preview_button.setEnabled(True)
        self._append_log(f"분석 오류: {message}")
        QMessageBox.critical(self, "목록 분석 실패", message)

    @Slot()
    def _cleanup_info_worker(self) -> None:
        if self._info_worker is not None:
            self._info_worker.deleteLater()
        if self._info_thread is not None:
            self._info_thread.deleteLater()
        self._info_worker = None
        self._info_thread = None

    @Slot()
    def add_preview_to_queue(self) -> None:
        if self._preview_info is None:
            QMessageBox.warning(self, "목록 필요", "먼저 URL 목록을 불러오세요.")
            return

        output_text = self.output_input.text().strip()
        if not output_text:
            QMessageBox.warning(self, "저장 위치 필요", "저장 위치를 선택하세요.")
            return

        status = check_dependencies()
        if not status.yt_dlp_ready:
            QMessageBox.warning(self, "yt-dlp 설치 필요", "run_app.bat로 실행해 필요한 패키지를 설치하세요.")
            return
        mode = str(self.mode_combo.currentData())
        if mode in {"audio", "video"} and not status.ffmpeg_ready:
            QMessageBox.warning(self, "ffmpeg 필요", "MP3 변환 또는 MP4 병합에는 ffmpeg가 필요합니다.")
            return

        request = self._current_request(self._preview_info, require_output=True)
        job = QueueJob(
            title=self._preview_info.title,
            item_count=self._preview_info.item_count,
            request=request,
            entries=list(self._preview_info.entries),
        )
        self._queue.append(job)
        save_settings(
            {
                "last_output_dir": output_text,
                "last_url": self._preview_info.url,
                "last_mode": request.mode,
                "last_quality": request.quality,
                "last_filename_mode": request.filename_mode,
                "last_folder_mode": request.folder_mode,
                "last_duplicate_mode": request.duplicate_mode,
                "last_subtitle_mode": request.subtitle_mode,
                "open_folder_on_finish": self.open_folder_on_finish_checkbox.isChecked(),
            }
        )
        self._append_queue_row(job)
        self._append_log(f"대기열 추가: {job.title} ({job.item_count}개)")
        if request.duplicate_mode == "unique":
            self._preview_name_token = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.refresh_preview_filenames()

    def _current_request(self, info: MediaInfo, require_output: bool) -> DownloadRequest:
        output_text = self.output_input.text().strip()
        output_dir = Path(output_text) if output_text else Path(self._settings["last_output_dir"])
        if require_output and not output_text:
            raise ValueError("저장 위치를 선택하세요.")

        duplicate_mode = str(self.duplicate_mode_combo.currentData())
        if duplicate_mode == "unique" and not self._preview_name_token:
            self._preview_name_token = datetime.now().strftime("%Y%m%d_%H%M%S")
        name_token = self._preview_name_token if duplicate_mode == "unique" else ""
        return DownloadRequest(
            url=info.url,
            output_dir=output_dir,
            mode=str(self.mode_combo.currentData()),
            quality=str(self.quality_combo.currentData()),
            filename_mode=str(self.filename_mode_combo.currentData()),
            folder_mode=str(self.folder_mode_combo.currentData()),
            duplicate_mode=duplicate_mode,
            collection_title=self._current_collection_title(info),
            name_token=name_token,
            subtitle_mode=str(self.subtitle_combo.currentData()),
        )

    def _current_collection_title(self, info: MediaInfo) -> str:
        custom_folder_name = self.folder_name_input.text().strip()
        return custom_folder_name or info.title

    def _append_queue_row(self, job: QueueJob) -> None:
        row = self.queue_table.rowCount()
        self.queue_table.insertRow(row)
        self.queue_table.setItem(row, 0, QTableWidgetItem(job.title))
        self.queue_table.setItem(row, 1, QTableWidgetItem(str(job.item_count)))
        self.queue_table.setItem(row, 2, QTableWidgetItem(MODE_LABELS.get(job.request.mode, job.request.mode)))
        quality = QUALITY_LABELS.get(job.request.quality, job.request.quality) if job.request.mode == "video" else "최고 음질"
        self.queue_table.setItem(row, 3, QTableWidgetItem(quality))
        self.queue_table.setItem(row, 4, QTableWidgetItem(self._queue_folder_text(job.request)))
        self.queue_table.setItem(row, 5, QTableWidgetItem(FILENAME_MODE_LABELS.get(job.request.filename_mode, job.request.filename_mode)))
        duplicate_text = DUPLICATE_MODE_LABELS.get(job.request.duplicate_mode, job.request.duplicate_mode)
        subtitle_text = SUBTITLE_MODE_LABELS.get(job.request.subtitle_mode, job.request.subtitle_mode)
        self.queue_table.setItem(row, 6, QTableWidgetItem(f"{duplicate_text} / {subtitle_text}"))
        self.queue_table.setItem(row, 7, QTableWidgetItem(job.request.url))

    def _queue_folder_text(self, request: DownloadRequest) -> str:
        if request.folder_mode == "root":
            return "선택 폴더 바로 저장"
        return request.collection_title or "재생목록명"

    def _selected_queue_row(self) -> int | None:
        selected = self.queue_table.selectionModel().selectedRows()
        if not selected:
            return None
        return selected[0].row()

    @Slot()
    def move_queue_item_up(self) -> None:
        row = self._selected_queue_row()
        if row is None or row <= 0 or self._download_worker is not None:
            return
        self._queue[row - 1], self._queue[row] = self._queue[row], self._queue[row - 1]
        self._refresh_queue_table(row - 1)

    @Slot()
    def move_queue_item_down(self) -> None:
        row = self._selected_queue_row()
        if row is None or row >= len(self._queue) - 1 or self._download_worker is not None:
            return
        self._queue[row + 1], self._queue[row] = self._queue[row], self._queue[row + 1]
        self._refresh_queue_table(row + 1)

    @Slot()
    def remove_queue_item(self) -> None:
        row = self._selected_queue_row()
        if row is None or self._download_worker is not None:
            return
        removed = self._queue.pop(row)
        self._refresh_queue_table(min(row, len(self._queue) - 1))
        self._append_log(f"대기열 삭제: {removed.title}")

    def _refresh_queue_table(self, select_row: int | None = None) -> None:
        self.queue_table.setRowCount(0)
        for job in self._queue:
            self._append_queue_row(job)
        if select_row is not None and select_row >= 0:
            self.queue_table.selectRow(select_row)

    @Slot()
    def clear_queue(self) -> None:
        if self._download_worker is not None:
            QMessageBox.information(self, "다운로드 중", "진행 중인 작업은 취소 버튼으로 중단할 수 있습니다.")
            return
        self._queue.clear()
        self.queue_table.setRowCount(0)

    @Slot()
    def start_download(self) -> None:
        if self._download_worker is not None:
            return
        if not self._queue:
            QMessageBox.warning(self, "대기열 비어 있음", "목록을 불러온 뒤 대기열에 추가하세요.")
            return

        jobs = list(self._queue)
        self._active_jobs = jobs
        self._session_log_path = self._create_session_log_path(jobs)
        self._queue.clear()
        self.queue_table.setRowCount(0)
        self.result_table.setSortingEnabled(False)
        self.result_table.setRowCount(0)
        self.result_table.setSortingEnabled(True)
        self.retry_problem_button.setEnabled(False)
        self._last_audit_results = []
        self.result_summary_label.setText("다운로드가 끝나면 예상 파일과 실제 파일을 자동 검증합니다.")
        self.overall_progress_bar.setValue(0)
        self.current_progress_bar.setValue(0)
        self.status_label.setText("다운로드 준비 중")
        self._append_log(f"세션 로그 파일: {self._session_log_path}")

        self._download_thread = QThread(self)
        self._download_worker = DownloadWorker(jobs)
        self._download_worker.moveToThread(self._download_thread)
        self._download_thread.started.connect(self._download_worker.run)
        self._download_worker.progress.connect(self.update_progress)
        self._download_worker.log.connect(self._append_log)
        self._download_worker.finished.connect(self.download_finished)
        self._download_worker.cancelled.connect(self.download_cancelled)
        self._download_worker.failed.connect(self.download_failed)
        self._download_worker.finished.connect(self._download_thread.quit)
        self._download_worker.cancelled.connect(self._download_thread.quit)
        self._download_worker.failed.connect(self._download_thread.quit)
        self._download_thread.finished.connect(self._cleanup_download_worker)
        self._set_running(True)
        self._download_thread.start()

    @Slot()
    def cancel_download(self) -> None:
        if self._download_worker is not None:
            self._pause_requested = True
            self._paused_jobs = list(self._active_jobs) + list(self._queue)
            self._queue.clear()
            self.queue_table.setRowCount(0)
            self.status_label.setText("일시정지 요청 중")
            self._download_worker.cancel()
            self.cancel_button.setEnabled(False)

    @Slot()
    def resume_download(self) -> None:
        if self._download_worker is not None or not self._paused_jobs:
            return
        self._queue = list(self._paused_jobs)
        self._paused_jobs = []
        self._refresh_queue_table()
        self._append_log("일시정지된 작업을 이어받기 대기열에 복원했습니다.")
        self.resume_button.setEnabled(False)
        self.start_download()

    @Slot(float, float, str)
    def update_progress(self, overall_percent: float, current_percent: float, detail: str) -> None:
        self.overall_progress_bar.setValue(max(0, min(100, int(overall_percent))))
        self.current_progress_bar.setValue(max(0, min(100, int(current_percent))))
        self.status_label.setText(detail)

    @Slot()
    def download_finished(self) -> None:
        self.overall_progress_bar.setValue(100)
        self.current_progress_bar.setValue(100)
        self.status_label.setText("완료")
        self._append_log("작업이 완료되었습니다.")
        self._audit_active_jobs("완료")
        self.right_tabs.setCurrentIndex(2)
        self._set_running(False)
        if self._queue:
            self._append_log("새로 추가된 대기열을 이어서 다운로드합니다.")
            self._continue_after_cleanup = True
        else:
            self._notify("다운로드 완료", "모든 대기열 다운로드가 완료되었습니다.")
            if self.open_folder_on_finish_checkbox.isChecked():
                self.open_output_folder()

    @Slot()
    def download_cancelled(self) -> None:
        if self._pause_requested:
            self.status_label.setText("일시정지됨")
            self._append_log("작업이 일시정지되었습니다. 이어받기를 누르면 기존 파일을 이어서 받습니다.")
        else:
            self.status_label.setText("취소됨")
            self._append_log("작업이 취소되었습니다.")
        self._audit_active_jobs("취소 후 검증")
        self.right_tabs.setCurrentIndex(2)
        self._set_running(False)
        self._notify("다운로드 일시정지", "이어받기를 누르면 중단된 작업을 다시 시작합니다.")
        self._pause_requested = False

    @Slot(str)
    def download_failed(self, message: str) -> None:
        self.status_label.setText("오류")
        self._append_log(f"오류: {message}")
        self._audit_active_jobs("오류 후 검증")
        self.right_tabs.setCurrentIndex(2)
        self._set_running(False)
        self._notify("다운로드 실패", "자세한 원인은 앱 로그와 실패 팝업을 확인하세요.")
        QMessageBox.critical(self, "다운로드 실패", message)

    @Slot()
    def _cleanup_download_worker(self) -> None:
        if self._download_worker is not None:
            self._download_worker.deleteLater()
        if self._download_thread is not None:
            self._download_thread.deleteLater()
        self._download_worker = None
        self._download_thread = None
        if self._continue_after_cleanup:
            self._continue_after_cleanup = False
            self.start_download()

    def _create_session_log_path(self, jobs: list[QueueJob]) -> Path:
        output_dir = jobs[0].request.output_dir if jobs else Path(self._settings["last_output_dir"])
        report_dir = output_dir / "_download_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return report_dir / f"download_{stamp}.log"

    def _audit_active_jobs(self, label: str) -> None:
        if not self._active_jobs:
            self.result_summary_label.setText("검증할 다운로드 작업이 없습니다.")
            return

        results = self._build_audit_results(self._active_jobs)
        self._display_audit_results(label, results)
        self._write_audit_log(label, results)

    def _build_audit_results(self, jobs: list[QueueJob]) -> list[AuditResult]:
        results: list[AuditResult] = []
        for job in jobs:
            for index, entry in enumerate(job.entries, start=1):
                results.append(self._audit_entry(job, entry, index))
        return results

    def _audit_entry(self, job: QueueJob, entry: MediaEntry, index: int) -> AuditResult:
        expected_path = self._expected_file_path(job.request, entry, index)
        final_ext = ".mp3" if job.request.mode == "audio" else ".mp4"

        if expected_path.exists():
            if _file_size(expected_path) > 0:
                return self._audit_result("정상", job, entry, index, expected_path, expected_path, "예상 파일명과 일치", "")
            return self._audit_result("확인 필요", job, entry, index, expected_path, expected_path, "0바이트 최종 파일", "파일 삭제 후 다시 받기")

        folder = expected_path.parent
        files = _safe_list_files(folder)
        final_candidates = [path for path in files if path.suffix.lower() == final_ext and _file_size(path) > 0]
        final_match = self._best_file_match(final_candidates, expected_path, entry, index, job.request)
        if final_match is not None:
            reason = "실제 저장 파일명 기준으로 확인"
            if _normalized_stem(final_match) != _normalized_stem(expected_path):
                reason = f"예상 파일명과 다르지만 같은 항목으로 판단: {final_match.name}"
            return self._audit_result("정상", job, entry, index, expected_path, final_match, reason, "")

        partial_candidates = [path for path in files if _is_intermediate_file(path)]
        partial_match = self._best_file_match(partial_candidates, expected_path, entry, index, job.request)
        if partial_match is not None:
            return self._audit_result(
                "미완료",
                job,
                entry,
                index,
                expected_path,
                partial_match,
                f"최종 파일 없이 중간 파일만 남음: {partial_match.name}",
                "문제 항목만 다시 받기",
            )

        weak_match = self._weak_final_candidate(final_candidates, expected_path, entry, index, job.request)
        if weak_match is not None:
            return self._audit_result(
                "확인 필요",
                job,
                entry,
                index,
                expected_path,
                weak_match,
                f"비슷한 최종 파일 발견: {weak_match.name}",
                "파일 확인 후 필요하면 다시 받기",
            )

        return self._audit_result("누락", job, entry, index, expected_path, None, "최종 파일과 중간 파일을 찾지 못함", "문제 항목만 다시 받기")

    def _audit_result(
        self,
        status: str,
        job: QueueJob,
        entry: MediaEntry,
        index: int,
        expected_path: Path,
        actual_path: Path | None,
        reason: str,
        action: str,
    ) -> AuditResult:
        return AuditResult(
            status=status,
            job_title=job.title,
            index=index,
            title=entry.title,
            expected_path=expected_path,
            actual_path=actual_path,
            reason=reason,
            action=action,
            request=job.request,
            entry=entry,
        )

    def _expected_file_path(self, request: DownloadRequest, entry: MediaEntry, index: int) -> Path:
        relative = preview_filename(entry.title, index, request)
        return request.output_dir.joinpath(*relative.split("/"))

    def _best_file_match(
        self,
        candidates: list[Path],
        expected_path: Path,
        entry: MediaEntry,
        index: int,
        request: DownloadRequest,
    ) -> Path | None:
        scored = [
            (score, path)
            for path in candidates
            if (score := _file_match_score(path, expected_path, entry, index, request)) >= 80
        ]
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], _file_size(item[1])), reverse=True)
        return scored[0][1]

    def _weak_final_candidate(
        self,
        candidates: list[Path],
        expected_path: Path,
        entry: MediaEntry,
        index: int,
        request: DownloadRequest,
    ) -> Path | None:
        scored = [
            (score, path)
            for path in candidates
            if (score := _file_match_score(path, expected_path, entry, index, request)) >= 55
        ]
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], _file_size(item[1])), reverse=True)
        return scored[0][1]

    def _display_audit_results(self, label: str, results: list[AuditResult]) -> None:
        self._last_audit_results = results
        ok_count = sum(1 for result in results if result.status == "정상")
        incomplete_count = sum(1 for result in results if result.status == "미완료")
        review_count = sum(1 for result in results if result.status == "확인 필요")
        missing_count = sum(1 for result in results if result.status == "누락")
        self.result_summary_label.setText(
            f"{label}: 총 {len(results)}개 중 정상 {ok_count}개, 미완료 {incomplete_count}개, 확인 필요 {review_count}개, 누락 {missing_count}개"
        )
        self.retry_problem_button.setEnabled(any(result.status in {"미완료", "누락", "확인 필요"} for result in results))
        if any(result.status != "정상" for result in results):
            problem_index = self.result_filter_combo.findData("problem")
            if problem_index >= 0:
                self.result_filter_combo.setCurrentIndex(problem_index)
        else:
            all_index = self.result_filter_combo.findData("all")
            if all_index >= 0:
                self.result_filter_combo.setCurrentIndex(all_index)
        self.refresh_result_filter()

    @Slot()
    @Slot(int)
    def refresh_result_filter(self, _index: int | None = None) -> None:
        results = self._filtered_audit_results()

        self.result_table.setSortingEnabled(False)
        self.result_table.setRowCount(0)
        for result in results:
            row = self.result_table.rowCount()
            self.result_table.insertRow(row)
            values = [
                str(result.index),
                result.title,
                result.status,
                str(result.actual_path or result.expected_path),
                result.reason,
                result.action,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    item.setBackground(_status_color(result.status))
                self.result_table.setItem(row, column, item)
        self.result_table.setSortingEnabled(True)

    def _filtered_audit_results(self) -> list[AuditResult]:
        mode = str(self.result_filter_combo.currentData())
        if mode == "problem":
            return [result for result in self._last_audit_results if result.status != "정상"]
        if mode == "ok":
            return [result for result in self._last_audit_results if result.status == "정상"]
        if mode == "incomplete":
            return [result for result in self._last_audit_results if result.status == "미완료"]
        if mode == "review":
            return [result for result in self._last_audit_results if result.status == "확인 필요"]
        if mode == "missing":
            return [result for result in self._last_audit_results if result.status == "누락"]
        return list(self._last_audit_results)

    def _write_audit_log(self, label: str, results: list[AuditResult]) -> None:
        self._append_log(f"결과 검증: {self.result_summary_label.text()}")
        for result in results:
            if result.status != "정상":
                actual = f" / 실제: {result.actual_path}" if result.actual_path is not None else ""
                self._append_log(f"[{label}] {result.status}: {result.index}. {result.title} -> 예상: {result.expected_path}{actual} / {result.reason}")

    @Slot()
    def retry_problem_items(self) -> None:
        if self._download_worker is not None:
            QMessageBox.information(self, "다운로드 중", "진행 중인 작업이 끝난 뒤 문제 항목을 다시 추가하세요.")
            return

        retry_results = [result for result in self._last_audit_results if result.status in {"미완료", "누락", "확인 필요"}]
        if not retry_results:
            QMessageBox.information(self, "재시도 항목 없음", "다시 받을 문제 항목이 없습니다.")
            return

        added = 0
        for result in retry_results:
            if not result.entry.url:
                self._append_log(f"재시도 제외: {result.index}. {result.title} - 개별 영상 URL 없음")
                continue
            retry_request = replace(
                result.request,
                url=result.entry.url,
                index_override=result.index if result.request.filename_mode in {"numbered", "playlist_folder"} else None,
            )
            retry_job = QueueJob(
                title=f"재시도: {result.title}",
                item_count=1,
                request=retry_request,
                entries=[result.entry],
            )
            self._queue.append(retry_job)
            self._append_queue_row(retry_job)
            added += 1

        if added:
            self.right_tabs.setCurrentIndex(1)
            self._append_log(f"문제 항목 재시도 대기열 추가: {added}개")
            QMessageBox.information(self, "대기열 추가 완료", f"문제 항목 {added}개를 대기열에 추가했습니다.")
        else:
            QMessageBox.warning(self, "재시도 실패", "개별 영상 URL이 없어 대기열에 추가하지 못했습니다.")

    @Slot()
    def open_output_folder(self) -> None:
        folder = self._current_output_folder()
        if not folder.exists():
            QMessageBox.warning(self, "폴더 없음", f"저장 폴더를 찾지 못했습니다.\n{folder}")
            return
        _open_path(folder)

    @Slot()
    def open_session_report(self) -> None:
        if self._session_log_path is None or not self._session_log_path.exists():
            QMessageBox.information(self, "리포트 없음", "아직 열 수 있는 세션 로그가 없습니다.")
            return
        _open_path(self._session_log_path)

    def _current_output_folder(self) -> Path:
        if self._active_jobs:
            return self._active_jobs[0].request.output_dir
        output_text = self.output_input.text().strip()
        return Path(output_text) if output_text else Path(self._settings["last_output_dir"])

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self.log_view.append(message)
        if self._session_log_path is not None:
            try:
                self._session_log_path.parent.mkdir(parents=True, exist_ok=True)
                with self._session_log_path.open("a", encoding="utf-8") as file:
                    file.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
            except OSError:
                pass
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_running(self, running: bool) -> None:
        self.download_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.resume_button.setEnabled(not running and bool(self._paused_jobs))
        self.clear_queue_button.setEnabled(not running)
        self.move_up_button.setEnabled(not running)
        self.move_down_button.setEnabled(not running)
        self.remove_queue_button.setEnabled(not running)
        self.retry_problem_button.setEnabled(
            not running and any(result.status in {"미완료", "누락", "확인 필요"} for result in self._last_audit_results)
        )

    def _notify(self, title: str, message: str) -> None:
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 6000)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._download_worker is not None:
            answer = QMessageBox.question(
                self,
                "다운로드 중",
                "다운로드가 진행 중입니다. 취소하고 종료할까요?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._download_worker.cancel()
            if self._download_thread is not None:
                self._download_thread.quit()
                self._download_thread.wait(3000)
        self._save_ui_state()
        event.accept()


def _encode_qbytearray(value: QByteArray) -> str:
    return bytes(value.toBase64()).decode("ascii")


def _format_duration(duration: int | None) -> str:
    if duration is None:
        return "-"
    minutes, seconds = divmod(duration, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _status_color(status: str) -> QColor:
    if status == "정상":
        return QColor("#dff5e1")
    if status == "미완료":
        return QColor("#fff0c2")
    if status == "확인 필요":
        return QColor("#e7f0ff")
    if status == "누락":
        return QColor("#ffd9d9")
    return QColor("#f1f1f1")


def _open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _safe_list_files(folder: Path) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    try:
        return [path for path in folder.iterdir() if path.is_file()]
    except OSError:
        return []


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _is_intermediate_file(path: Path) -> bool:
    lowered = path.name.lower()
    if lowered.endswith(".part"):
        return True
    if re.search(r"\.f\d+\.", lowered):
        return True
    return path.suffix.lower() in {".webm", ".m4a", ".m4v"}


def _file_match_score(path: Path, expected_path: Path, entry: MediaEntry, index: int, request: DownloadRequest) -> int:
    candidate_stem = _normalized_stem(path)
    expected_stem = _normalized_stem(expected_path)
    if candidate_stem == expected_stem:
        return 120

    if entry.video_id and entry.video_id.lower() in path.name.lower():
        return 115

    numbered_prefix = f"{index:03d}_"
    if request.filename_mode in {"numbered", "playlist_folder"} and path.name.startswith(numbered_prefix):
        return 100

    candidate_signature = _title_signature(path.stem)
    entry_signature = _title_signature(entry.title)
    expected_signature = _title_signature(expected_path.stem)
    signatures = {signature for signature in (entry_signature, expected_signature) if signature}
    if candidate_signature and candidate_signature in signatures:
        return 95

    entry_key = _match_key(entry.title)
    candidate_key = _match_key(path.stem)
    expected_key = _match_key(expected_path.stem)
    if entry_key and (entry_key in candidate_key or candidate_key in entry_key):
        return 92
    if expected_key and (expected_key in candidate_key or candidate_key in expected_key):
        return 90

    ratio = max(
        SequenceMatcher(None, entry_key, candidate_key).ratio() if entry_key and candidate_key else 0.0,
        SequenceMatcher(None, expected_key, candidate_key).ratio() if expected_key and candidate_key else 0.0,
    )
    if ratio >= 0.72:
        return 88
    if ratio >= 0.55:
        return 60
    return 0


def _normalized_stem(path: Path) -> str:
    return _match_key(path.stem)


def _match_key(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).lower()
    normalized = re.sub(r"\.\w+$", "", normalized)
    normalized = re.sub(r"\.\.\.|…", "", normalized)
    normalized = re.sub(r"[<>:\"/\\|?*\[\](){}'`~!@#$%^&+=,.;：｜|_-]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _title_signature(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).lower()
    bracketed = re.search(r"\[[^\]]+\]\s*\d+(?:-\d+)*", normalized)
    if bracketed:
        return _match_key(bracketed.group(0))

    lesson = re.search(r"\b\d+(?:-\d+){1,3}\b", normalized)
    if lesson:
        prefix = normalized[max(0, lesson.start() - 20) : lesson.end()]
        return _match_key(prefix)

    leading = re.search(r"^\s*(\d{1,4})[_\-. ]+", normalized)
    if leading:
        return leading.group(1).lstrip("0") or "0"
    return ""


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
