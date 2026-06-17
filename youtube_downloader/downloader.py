from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[float | None, str], None]
LogCallback = Callable[[str], None]

PERCENT_PATTERN = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
ETA_PATTERN = re.compile(r"\bETA\s+([^\s]+)")
SPEED_PATTERN = re.compile(r"\bat\s+([^\s]+\s*/s)")


@dataclass(frozen=True)
class DependencyStatus:
    python_path: str
    yt_dlp_version: str | None
    ffmpeg_path: str | None

    @property
    def yt_dlp_ready(self) -> bool:
        return self.yt_dlp_version is not None

    @property
    def ffmpeg_ready(self) -> bool:
        return self.ffmpeg_path is not None


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    output_dir: Path
    mode: str
    quality: str = "best"


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                _terminate_process(self._process)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def attach_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._process = process
            if self.cancelled and process.poll() is None:
                _terminate_process(process)

    def clear_process(self) -> None:
        with self._lock:
            self._process = None


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(RuntimeError):
    pass


def check_dependencies() -> DependencyStatus:
    yt_dlp_version: str | None = None
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        yt_dlp_version = result.stdout.strip() or "installed"
    except (OSError, subprocess.CalledProcessError):
        yt_dlp_version = None

    return DependencyStatus(
        python_path=sys.executable,
        yt_dlp_version=yt_dlp_version,
        ffmpeg_path=shutil.which("ffmpeg"),
    )


def build_command(request: DownloadRequest) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--newline",
        "--no-color",
        "--progress",
        "--windows-filenames",
        "--ignore-errors",
        "--yes-playlist",
        "-P",
        str(request.output_dir),
        "-o",
        "%(title)s.%(ext)s",
    ]
    js_runtime = _detect_js_runtime()
    if js_runtime is not None:
        command.extend(["--js-runtimes", js_runtime])
        command.extend(["--remote-components", "ejs:github"])

    if request.mode == "audio":
        command.extend(["-x", "--audio-format", "mp3", "--audio-quality", "0"])
    elif request.mode == "video":
        command.extend(["-f", _video_format_selector(request.quality), "--merge-output-format", "mp4"])
    else:
        raise ValueError(f"Unsupported download mode: {request.mode}")

    command.append(request.url)
    return command


def _detect_js_runtime() -> str | None:
    if shutil.which("deno"):
        return "deno"
    if shutil.which("node"):
        return "node"
    if shutil.which("bun"):
        return "bun"
    return None


def _video_format_selector(quality: str) -> str:
    if quality == "best":
        return "bv*+ba/best"

    try:
        max_height = int(quality)
    except ValueError as exc:
        raise ValueError(f"Unsupported video quality: {quality}") from exc

    if max_height <= 0:
        raise ValueError(f"Unsupported video quality: {quality}")

    return f"bv*[height<={max_height}]+ba/best[height<={max_height}]/best"


def run_download(
    request: DownloadRequest,
    cancel_token: CancelToken,
    on_progress: ProgressCallback,
    on_log: LogCallback,
) -> None:
    request.output_dir.mkdir(parents=True, exist_ok=True)
    command = build_command(request)

    on_log("yt-dlp 실행을 시작합니다.")
    on_log(f"저장 위치: {request.output_dir}")

    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creation_flags,
    )
    cancel_token.attach_process(process)

    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            if cancel_token.cancelled:
                _terminate_process(process)
                raise DownloadCancelled("다운로드가 취소되었습니다.")

            line = raw_line.strip()
            if not line:
                continue

            on_log(line)
            percent = _parse_percent(line)
            if percent is not None:
                on_progress(percent, _summarize_progress(line))

        return_code = process.wait()
    finally:
        if cancel_token.cancelled and process.poll() is None:
            _terminate_process(process)
        cancel_token.clear_process()

    if cancel_token.cancelled:
        raise DownloadCancelled("다운로드가 취소되었습니다.")

    if return_code != 0:
        raise DownloadError(f"yt-dlp가 오류 코드 {return_code}로 종료되었습니다.")

    on_progress(100.0, "완료")
    on_log("다운로드가 완료되었습니다.")


def _parse_percent(line: str) -> float | None:
    match = PERCENT_PATTERN.search(line)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _summarize_progress(line: str) -> str:
    parts: list[str] = []
    speed = SPEED_PATTERN.search(line)
    eta = ETA_PATTERN.search(line)
    if speed:
        parts.append(speed.group(1))
    if eta:
        parts.append(f"ETA {eta.group(1)}")
    return " / ".join(parts) if parts else "다운로드 중"


def _terminate_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
