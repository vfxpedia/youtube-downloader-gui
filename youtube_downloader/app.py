from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .downloader import (
    CancelToken,
    DownloadCancelled,
    DownloadError,
    DownloadRequest,
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


@dataclass(frozen=True)
class QueueJob:
    title: str
    item_count: int
    request: DownloadRequest


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

        self.url_input = QLineEdit()
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
        self.folder_mode_combo.currentIndexChanged.connect(self.refresh_preview_filenames)

        self.duplicate_mode_combo = QComboBox()
        for mode, label in DUPLICATE_MODE_LABELS.items():
            self.duplicate_mode_combo.addItem(label, mode)
        selected_duplicate_mode = self.duplicate_mode_combo.findData(self._settings.get("last_duplicate_mode", "skip"))
        self.duplicate_mode_combo.setCurrentIndex(max(selected_duplicate_mode, 0))
        self.duplicate_mode_combo.currentIndexChanged.connect(self.refresh_preview_filenames)

        self.preview_button = QPushButton("목록 불러오기")
        self.preview_button.clicked.connect(self.load_preview)
        self.add_queue_button = QPushButton("대기열 추가")
        self.add_queue_button.setEnabled(False)
        self.add_queue_button.clicked.connect(self.add_preview_to_queue)
        self.download_button = QPushButton("대기열 다운로드 시작")
        self.download_button.clicked.connect(self.start_download)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_download)
        self.clear_queue_button = QPushButton("대기열 비우기")
        self.clear_queue_button.clicked.connect(self.clear_queue)
        self.move_up_button = QPushButton("위로")
        self.move_up_button.clicked.connect(self.move_queue_item_up)
        self.move_down_button = QPushButton("아래로")
        self.move_down_button.clicked.connect(self.move_queue_item_down)
        self.remove_queue_button = QPushButton("삭제")
        self.remove_queue_button.clicked.connect(self.remove_queue_item)

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

        self.queue_table = QTableWidget(0, 8)
        self.queue_table.setHorizontalHeaderLabels(["제목", "항목", "형식", "화질", "폴더", "파일명", "중복 처리", "URL"])
        self._setup_table(self.queue_table)

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
        self.refresh_dependencies()
        self.update_quality_enabled()

    def _setup_table(self, table: QTableWidget) -> None:
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)

    def _build_layout(self) -> None:
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 16, 16, 16)

        splitter = QSplitter()
        left = QWidget()
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
        input_layout.addWidget(QLabel("중복"), 4, 0)
        input_layout.addWidget(self.duplicate_mode_combo, 4, 1, 1, 3)

        action_layout = QHBoxLayout()
        action_layout.addWidget(self.preview_button)
        action_layout.addWidget(self.add_queue_button)
        action_layout.addWidget(self.download_button)
        action_layout.addWidget(self.cancel_button)
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

        left_layout.addWidget(input_group)
        left_layout.addLayout(action_layout)
        left_layout.addWidget(progress_group)
        left_layout.addWidget(dependency_group)
        left_layout.addWidget(log_group, 1)

        right_layout.addWidget(preview_group, 1)
        right_layout.addWidget(queue_group, 1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([640, 500])
        root_layout.addWidget(splitter)
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
        job = QueueJob(title=self._preview_info.title, item_count=self._preview_info.item_count, request=request)
        self._queue.append(job)
        save_settings(
            {
                "last_output_dir": output_text,
                "last_mode": request.mode,
                "last_quality": request.quality,
                "last_filename_mode": request.filename_mode,
                "last_folder_mode": request.folder_mode,
                "last_duplicate_mode": request.duplicate_mode,
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
            collection_title=info.title,
            name_token=name_token,
        )

    def _append_queue_row(self, job: QueueJob) -> None:
        row = self.queue_table.rowCount()
        self.queue_table.insertRow(row)
        self.queue_table.setItem(row, 0, QTableWidgetItem(job.title))
        self.queue_table.setItem(row, 1, QTableWidgetItem(str(job.item_count)))
        self.queue_table.setItem(row, 2, QTableWidgetItem(MODE_LABELS.get(job.request.mode, job.request.mode)))
        quality = QUALITY_LABELS.get(job.request.quality, job.request.quality) if job.request.mode == "video" else "최고 음질"
        self.queue_table.setItem(row, 3, QTableWidgetItem(quality))
        self.queue_table.setItem(row, 4, QTableWidgetItem(FOLDER_MODE_LABELS.get(job.request.folder_mode, job.request.folder_mode)))
        self.queue_table.setItem(row, 5, QTableWidgetItem(FILENAME_MODE_LABELS.get(job.request.filename_mode, job.request.filename_mode)))
        self.queue_table.setItem(row, 6, QTableWidgetItem(DUPLICATE_MODE_LABELS.get(job.request.duplicate_mode, job.request.duplicate_mode)))
        self.queue_table.setItem(row, 7, QTableWidgetItem(job.request.url))

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
        self._queue.clear()
        self.queue_table.setRowCount(0)
        self.overall_progress_bar.setValue(0)
        self.current_progress_bar.setValue(0)
        self.status_label.setText("다운로드 준비 중")

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
            self.status_label.setText("취소 요청 중")
            self._download_worker.cancel()
            self.cancel_button.setEnabled(False)

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
        self._set_running(False)
        if self._queue:
            self._append_log("새로 추가된 대기열을 이어서 다운로드합니다.")
            self._continue_after_cleanup = True
        else:
            self._notify("다운로드 완료", "모든 대기열 다운로드가 완료되었습니다.")

    @Slot()
    def download_cancelled(self) -> None:
        self.status_label.setText("취소됨")
        self._append_log("작업이 취소되었습니다.")
        self._set_running(False)
        self._notify("다운로드 취소", "다운로드가 취소되었습니다.")

    @Slot(str)
    def download_failed(self, message: str) -> None:
        self.status_label.setText("오류")
        self._append_log(f"오류: {message}")
        self._set_running(False)
        self._notify("다운로드 실패", message)
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

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self.log_view.append(message)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_running(self, running: bool) -> None:
        self.download_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.clear_queue_button.setEnabled(not running)
        self.move_up_button.setEnabled(not running)
        self.move_down_button.setEnabled(not running)
        self.remove_queue_button.setEnabled(not running)

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
        event.accept()


def _format_duration(duration: int | None) -> str:
    if duration is None:
        return "-"
    minutes, seconds = divmod(duration, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
