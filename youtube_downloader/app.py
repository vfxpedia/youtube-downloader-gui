from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .downloader import (
    CancelToken,
    DownloadCancelled,
    DownloadError,
    DownloadRequest,
    check_dependencies,
    run_download,
)
from .settings import load_settings, save_settings


MODE_LABELS = {
    "video": "영상 MP4",
    "audio": "음원 MP3",
}


class DownloadWorker(QObject):
    progress = Signal(float, str)
    log = Signal(str)
    finished = Signal()
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, request: DownloadRequest) -> None:
        super().__init__()
        self._request = request
        self._cancel_token = CancelToken()

    @Slot()
    def run(self) -> None:
        try:
            run_download(
                self._request,
                self._cancel_token,
                lambda percent, status: self.progress.emit(percent or 0.0, status),
                self.log.emit,
            )
        except DownloadCancelled:
            self.cancelled.emit()
        except (DownloadError, OSError, ValueError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # Keeps unexpected worker errors visible in the UI.
            self.failed.emit(f"예상하지 못한 오류: {exc}")
        else:
            self.finished.emit()

    def cancel(self) -> None:
        self._cancel_token.cancel()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("YouTube Playlist Downloader")
        self.resize(860, 620)

        self._settings = load_settings()
        self._thread: QThread | None = None
        self._worker: DownloadWorker | None = None

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

        self.dependency_label = QLabel()
        self.refresh_button = QPushButton("상태 새로고침")
        self.refresh_button.clicked.connect(self.refresh_dependencies)

        self.download_button = QPushButton("다운로드 시작")
        self.download_button.clicked.connect(self.start_download)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_download)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label = QLabel("대기 중")
        self.status_label.setWordWrap(True)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)

        self._build_layout()
        self._apply_style()
        self.refresh_dependencies()

    def _build_layout(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        input_group = QGroupBox("다운로드 설정")
        input_layout = QGridLayout(input_group)
        input_layout.setColumnStretch(1, 1)
        input_layout.addWidget(QLabel("URL"), 0, 0)
        input_layout.addWidget(self.url_input, 0, 1, 1, 2)
        input_layout.addWidget(QLabel("저장 위치"), 1, 0)
        input_layout.addWidget(self.output_input, 1, 1)
        input_layout.addWidget(self.output_button, 1, 2)
        input_layout.addWidget(QLabel("형식"), 2, 0)
        input_layout.addWidget(self.mode_combo, 2, 1, 1, 2)

        dependency_group = QGroupBox("실행 환경")
        dependency_layout = QHBoxLayout(dependency_group)
        dependency_layout.addWidget(self.dependency_label, 1)
        dependency_layout.addWidget(self.refresh_button)

        action_layout = QHBoxLayout()
        action_layout.addWidget(self.download_button)
        action_layout.addWidget(self.cancel_button)
        action_layout.addStretch(1)

        progress_group = QGroupBox("진행 상태")
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.status_label)

        log_group = QGroupBox("로그")
        log_layout = QVBoxLayout(log_group)
        log_layout.addWidget(self.log_view)

        layout.addWidget(input_group)
        layout.addWidget(dependency_group)
        layout.addLayout(action_layout)
        layout.addWidget(progress_group)
        layout.addWidget(log_group, 1)

        self.setCentralWidget(central)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-size: 14px;
            }
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
            QLineEdit, QComboBox, QTextEdit {
                border: 1px solid #b8b8b8;
                border-radius: 4px;
                padding: 6px;
            }
            QPushButton {
                border: 1px solid #8d8d8d;
                border-radius: 4px;
                padding: 7px 14px;
                background: #f4f4f4;
            }
            QPushButton:hover {
                background: #e9f2ff;
            }
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
        yt_dlp_text = status.yt_dlp_version or "설치 필요"
        ffmpeg_text = status.ffmpeg_path or "설치 필요"
        self.dependency_label.setText(
            f"Python: {status.python_path}\n"
            f"yt-dlp: {yt_dlp_text}\n"
            f"ffmpeg: {ffmpeg_text}"
        )

    @Slot()
    def start_download(self) -> None:
        url = self.url_input.text().strip()
        output_text = self.output_input.text().strip()
        mode = str(self.mode_combo.currentData())

        if not url:
            QMessageBox.warning(self, "URL 필요", "다운로드할 YouTube URL을 입력하세요.")
            return

        if not output_text:
            QMessageBox.warning(self, "저장 위치 필요", "저장 위치를 선택하세요.")
            return

        output_dir = Path(output_text)
        status = check_dependencies()
        if not status.yt_dlp_ready:
            QMessageBox.warning(
                self,
                "yt-dlp 설치 필요",
                "yt-dlp가 설치되어 있지 않습니다. run_app.bat로 실행하면 필요한 패키지를 설치합니다.",
            )
            return

        if mode in {"audio", "video"} and not status.ffmpeg_ready:
            QMessageBox.warning(
                self,
                "ffmpeg 필요",
                "MP3 변환 또는 MP4 병합에는 ffmpeg가 필요합니다. ffmpeg를 PATH에 추가한 뒤 다시 시도하세요.",
            )
            return

        save_settings({"last_output_dir": str(output_dir), "last_mode": mode})
        self.progress_bar.setValue(0)
        self.status_label.setText("다운로드 준비 중")
        self.log_view.clear()
        self._append_log(f"URL: {url}")
        self._append_log(f"형식: {MODE_LABELS.get(mode, mode)}")

        request = DownloadRequest(url=url, output_dir=output_dir, mode=mode)
        self._thread = QThread(self)
        self._worker = DownloadWorker(request)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.update_progress)
        self._worker.log.connect(self._append_log)
        self._worker.finished.connect(self.download_finished)
        self._worker.cancelled.connect(self.download_cancelled)
        self._worker.failed.connect(self.download_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.cancelled.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_worker)

        self._set_running(True)
        self._thread.start()

    @Slot()
    def cancel_download(self) -> None:
        if self._worker is not None:
            self.status_label.setText("취소 요청 중")
            self._worker.cancel()
            self.cancel_button.setEnabled(False)

    @Slot(float, str)
    def update_progress(self, percent: float, detail: str) -> None:
        self.progress_bar.setValue(max(0, min(100, int(percent))))
        self.status_label.setText(detail)

    @Slot()
    def download_finished(self) -> None:
        self.progress_bar.setValue(100)
        self.status_label.setText("완료")
        self._append_log("작업이 완료되었습니다.")
        self._set_running(False)

    @Slot()
    def download_cancelled(self) -> None:
        self.status_label.setText("취소됨")
        self._append_log("작업이 취소되었습니다.")
        self._set_running(False)

    @Slot(str)
    def download_failed(self, message: str) -> None:
        self.status_label.setText("오류")
        self._append_log(f"오류: {message}")
        self._set_running(False)
        QMessageBox.critical(self, "다운로드 실패", message)

    @Slot()
    def _cleanup_worker(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self.log_view.append(message)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_running(self, running: bool) -> None:
        self.url_input.setEnabled(not running)
        self.output_input.setEnabled(not running)
        self.output_button.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        self.refresh_button.setEnabled(not running)
        self.download_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            answer = QMessageBox.question(
                self,
                "다운로드 중",
                "다운로드가 진행 중입니다. 취소하고 종료할까요?",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._worker.cancel()
            if self._thread is not None:
                self._thread.quit()
                self._thread.wait(3000)
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
