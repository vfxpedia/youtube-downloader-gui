from __future__ import annotations

import os
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[float | None, str], None]
PlaylistItemCallback = Callable[[int, int], None]
LogCallback = Callable[[str], None]

PERCENT_PATTERN = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
ETA_PATTERN = re.compile(r"\bETA\s+([^\s]+)")
SPEED_PATTERN = re.compile(r"\bat\s+([^\s]+\s*/s)")
PLAYLIST_ITEM_PATTERN = re.compile(r"\[download\]\s+Downloading item\s+(\d+)\s+of\s+(\d+)")
FAILURE_CONTEXT_LIMIT = 25
ERROR_CONTEXT_LIMIT = 12


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
    filename_mode: str = "title"
    folder_mode: str = "playlist"
    duplicate_mode: str = "skip"
    collection_title: str = ""
    name_token: str = ""
    index_override: int | None = None
    subtitle_mode: str = "none"


@dataclass(frozen=True)
class MediaEntry:
    title: str
    url: str
    duration: int | None = None
    video_id: str = ""


@dataclass(frozen=True)
class MediaInfo:
    title: str
    url: str
    entries: list[MediaEntry]

    @property
    def item_count(self) -> int:
        return max(1, len(self.entries))


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
            env=_subprocess_env(),
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
    temp_dir = _download_temp_dir()
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
        "--file-access-retries",
        "15",
        "--retry-sleep",
        "file_access:2",
        "-P",
        f"home:{request.output_dir}",
        "-P",
        f"temp:{temp_dir}",
        "-o",
        _output_template(request),
    ]
    if request.duplicate_mode == "skip":
        command.append("--no-overwrites")
    elif request.duplicate_mode == "overwrite":
        command.append("--force-overwrites")
    elif request.duplicate_mode == "unique":
        command.append("--no-overwrites")
    elif request.duplicate_mode == "archive":
        command.extend(["--no-overwrites", "--download-archive", str(request.output_dir / ".download_archive.txt")])
    else:
        raise ValueError(f"Unsupported duplicate mode: {request.duplicate_mode}")

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

    if request.subtitle_mode == "manual":
        command.extend(["--write-subs", "--sub-langs", "ko,en.*", "--convert-subs", "srt"])
    elif request.subtitle_mode == "auto":
        command.extend(["--write-auto-subs", "--sub-langs", "ko,en.*", "--convert-subs", "srt"])
    elif request.subtitle_mode == "both":
        command.extend(["--write-subs", "--write-auto-subs", "--sub-langs", "ko,en.*", "--convert-subs", "srt"])
    elif request.subtitle_mode != "none":
        raise ValueError(f"Unsupported subtitle mode: {request.subtitle_mode}")

    command.append(request.url)
    return command


def preview_filename(title: str, index: int, request: DownloadRequest) -> str:
    ext = "mp3" if request.mode == "audio" else "mp4"
    safe_title = _safe_filename(title)
    token = f"_{request.name_token}" if request.duplicate_mode == "unique" and request.name_token else ""
    display_index = request.index_override if request.index_override is not None else index
    if request.filename_mode == "title":
        filename = f"{safe_title}{token}.{ext}"
    elif request.filename_mode in {"numbered", "playlist_folder"}:
        filename = f"{display_index:03d}_{safe_title}{token}.{ext}"
    else:
        raise ValueError(f"Unsupported filename mode: {request.filename_mode}")

    folder = _folder_prefix(request)
    if folder:
        return f"{folder}/{filename}"
    return filename


def fetch_media_info(url: str) -> MediaInfo:
    errors: list[str] = []
    for candidate_url in _metadata_candidate_urls(url):
        result = _run_media_info_command(candidate_url)
        if result.returncode == 0:
            return _parse_media_info(candidate_url, result.stdout)

        message = result.stderr.strip() or result.stdout.strip() or "URL 정보를 가져오지 못했습니다."
        errors.append(f"{candidate_url}\n{message}")

    raise DownloadError(_format_metadata_error(errors))


def _run_media_info_command(url: str) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--flat-playlist",
        "--skip-download",
        "--yes-playlist",
        "--no-warnings",
    ]
    js_runtime = _detect_js_runtime()
    if js_runtime is not None:
        command.extend(["--js-runtimes", js_runtime])
        command.extend(["--remote-components", "ejs:github"])
    command.append(url)

    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_subprocess_env(),
    )


def _parse_media_info(url: str, output: str) -> MediaInfo:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DownloadError("yt-dlp 정보 조회 결과를 해석하지 못했습니다.") from exc

    if not isinstance(payload, dict):
        raise DownloadError("yt-dlp 정보 조회 결과가 비어 있습니다. URL을 다시 확인해 주세요.")

    title = str(payload.get("title") or payload.get("fulltitle") or "제목 없음")
    raw_entries = payload.get("entries")
    entries: list[MediaEntry] = []
    if isinstance(raw_entries, list) and raw_entries:
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            entry_title = str(raw_entry.get("title") or raw_entry.get("id") or "제목 없음")
            entry_id = str(raw_entry.get("id") or "")
            entry_url = _entry_url(raw_entry, entry_id)
            duration = raw_entry.get("duration")
            entries.append(
                MediaEntry(
                    title=entry_title,
                    url=entry_url,
                    duration=_duration_seconds(duration),
                    video_id=entry_id,
                )
            )
    else:
        entries.append(
            MediaEntry(
                title=title,
                url=str(payload.get("webpage_url") or url),
                duration=_duration_seconds(payload.get("duration")),
                video_id=str(payload.get("id") or ""),
            )
        )

    return MediaInfo(title=title, url=url, entries=entries)


def _duration_seconds(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(round(value))
    return None


def _entry_url(raw_entry: dict[str, object], entry_id: str) -> str:
    webpage_url = raw_entry.get("webpage_url")
    if isinstance(webpage_url, str) and webpage_url:
        return webpage_url

    raw_url = raw_entry.get("url")
    if isinstance(raw_url, str) and raw_url:
        if raw_url.startswith(("http://", "https://")):
            return raw_url
        return f"https://www.youtube.com/watch?v={raw_url}"

    if entry_id:
        return f"https://www.youtube.com/watch?v={entry_id}"
    return ""


def _metadata_candidate_urls(url: str) -> list[str]:
    candidates = [url]
    normalized = _normalized_youtube_playlist_url(url)
    if normalized is not None and normalized not in candidates:
        candidates.append(normalized)
    return candidates


def _normalized_youtube_playlist_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return None

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    playlist_id = query.get("list")
    if not playlist_id or playlist_id.endswith("_"):
        return None

    # Some copied YouTube playlist URLs silently drop a trailing underscore.
    # The API then returns HTTP 400 even though the playlist exists.
    query["list"] = f"{playlist_id}_"
    return urlunparse(
        (
            parsed.scheme or "https",
            "www.youtube.com",
            parsed.path or "/playlist",
            parsed.params,
            urlencode(query),
            parsed.fragment,
        )
    )


def _format_metadata_error(errors: list[str]) -> str:
    if not errors:
        return "URL 정보를 가져오지 못했습니다."
    if len(errors) == 1:
        return errors[0]
    return "URL 분석에 실패했습니다. 아래 후보 URL을 순서대로 시도했습니다.\n\n" + "\n\n".join(
        f"[시도 {index}]\n{message}" for index, message in enumerate(errors, start=1)
    )


def _detect_js_runtime() -> str | None:
    if shutil.which("deno"):
        return "deno"
    if shutil.which("node"):
        return "node"
    if shutil.which("bun"):
        return "bun"
    return None


def _output_template(request: DownloadRequest) -> str:
    token = f"_{request.name_token}" if request.duplicate_mode == "unique" and request.name_token else ""
    if request.filename_mode == "title":
        filename = f"%(title)s{token}.%(ext)s"
    elif request.filename_mode in {"numbered", "playlist_folder"}:
        if request.index_override is not None:
            filename = f"{request.index_override:03d}_%(title)s{token}.%(ext)s"
        else:
            filename = f"%(autonumber)03d_%(title)s{token}.%(ext)s"
    else:
        raise ValueError(f"Unsupported filename mode: {request.filename_mode}")

    folder = _folder_prefix(request)
    if folder:
        return f"{folder}/{filename}"
    return filename


def _folder_prefix(request: DownloadRequest) -> str:
    if request.filename_mode == "playlist_folder":
        return _safe_filename(request.collection_title or "playlist")
    if request.folder_mode == "root":
        return ""
    if request.folder_mode == "playlist":
        return _safe_filename(request.collection_title or "playlist")
    raise ValueError(f"Unsupported folder mode: {request.folder_mode}")


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip(". ") or "untitled"


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
    on_playlist_item: PlaylistItemCallback | None = None,
) -> None:
    request.output_dir.mkdir(parents=True, exist_ok=True)
    _download_temp_dir().mkdir(parents=True, exist_ok=True)
    command = build_command(request)

    on_log("yt-dlp 실행을 시작합니다.")
    on_log(f"저장 위치: {request.output_dir}")
    on_log(f"임시 파일 위치: {_download_temp_dir()}")
    on_log(f"실행 명령: {_format_command(command)}")

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
        env=_subprocess_env(),
    )
    cancel_token.attach_process(process)
    recent_lines: list[str] = []
    error_lines: list[str] = []

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
            _append_limited(recent_lines, line, FAILURE_CONTEXT_LIMIT)
            if _is_error_context_line(line):
                _append_limited(error_lines, line, ERROR_CONTEXT_LIMIT)
            item_progress = _parse_playlist_item(line)
            if item_progress is not None and on_playlist_item is not None:
                on_playlist_item(*item_progress)
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
        raise DownloadError(_format_download_error(return_code, error_lines, recent_lines))

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


def _parse_playlist_item(line: str) -> tuple[int, int] | None:
    match = PLAYLIST_ITEM_PATTERN.search(line)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _summarize_progress(line: str) -> str:
    parts: list[str] = []
    speed = SPEED_PATTERN.search(line)
    eta = ETA_PATTERN.search(line)
    if speed:
        parts.append(speed.group(1))
    if eta:
        parts.append(f"ETA {eta.group(1)}")
    return " / ".join(parts) if parts else "다운로드 중"


def _append_limited(lines: list[str], line: str, limit: int) -> None:
    lines.append(line)
    if len(lines) > limit:
        del lines[0]


def _is_error_context_line(line: str) -> bool:
    lowered = line.lower()
    markers = (
        "error:",
        "warning:",
        "unable to",
        "failed",
        "not available",
        "unavailable",
        "private video",
        "sign in",
        "requested format",
        "http error",
        "forbidden",
        "permission denied",
        "no space left",
    )
    return any(marker in lowered for marker in markers)


def _format_download_error(return_code: int, error_lines: list[str], recent_lines: list[str]) -> str:
    lines = [f"yt-dlp가 오류 코드 {return_code}로 종료되었습니다."]
    context = error_lines or recent_lines[-8:]
    if _has_file_lock_error(context):
        lines.extend(
            [
                "",
                "분류: 파일 잠금으로 최종 저장에 실패했습니다.",
                "다운로드 조각 파일은 받았지만 Windows에서 다른 프로세스가 파일을 잡고 있어 최종 파일명으로 바꾸지 못했습니다.",
                "가능한 원인: 파일 탐색기 미리보기, 백신/보안 프로그램, 검색 인덱서, 동기화 프로그램, 미디어 플레이어.",
                "앱은 다음 실행부터 임시 폴더를 분리하고 파일 접근 재시도를 늘려 같은 문제를 줄입니다.",
            ]
        )
    if context:
        lines.append("")
        lines.append("원인으로 보이는 로그:")
        lines.extend(f"- {line}" for line in context[-8:] if _should_show_failure_context(line))
    else:
        lines.append("")
        lines.append("원인 로그를 찾지 못했습니다. 로그 창의 마지막 줄을 확인해 주세요.")
    return "\n".join(lines)


def _has_file_lock_error(lines: list[str]) -> bool:
    text = "\n".join(lines).lower()
    return "winerror 32" in text or "file is being used by another process" in text or "다른 프로세스가 파일을 사용 중" in text


def _should_show_failure_context(line: str) -> bool:
    lowered = line.lower()
    if "redownloading playlist api json with unavailable videos" in lowered:
        return False
    return True


def _format_command(command: list[str]) -> str:
    if sys.platform.startswith("win"):
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _download_temp_dir() -> Path:
    root = os.getenv("LOCALAPPDATA")
    if root:
        return Path(root) / "YoutubeDownloaderGui" / "temp"
    return Path(tempfile.gettempdir()) / "YoutubeDownloaderGui" / "temp"
