from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import youtube_downloader.app as app_module
from youtube_downloader.app import MainWindow, QueueJob
from youtube_downloader.downloader import DownloadRequest, MediaEntry, build_command


class DownloaderCommandTests(unittest.TestCase):
    def test_build_command_uses_separate_temp_path_and_file_access_retries(self) -> None:
        request = DownloadRequest(
            url="https://www.youtube.com/watch?v=abc123",
            output_dir=Path("C:/Downloads"),
            mode="video",
        )

        command = build_command(request)

        self.assertIn("home:C:\\Downloads", command)
        self.assertTrue(any(value.startswith("temp:") and "YoutubeDownloaderGui" in value for value in command))
        self.assertIn("--file-access-retries", command)
        self.assertIn("15", command)
        self.assertIn("--retry-sleep", command)
        self.assertIn("file_access:2", command)

    def test_build_command_can_request_srt_subtitles(self) -> None:
        request = DownloadRequest(
            url="https://www.youtube.com/watch?v=abc123",
            output_dir=Path("C:/Downloads"),
            mode="video",
            subtitle_mode="both",
        )

        command = build_command(request)

        self.assertIn("--write-subs", command)
        self.assertIn("--write-auto-subs", command)
        self.assertIn("--convert-subs", command)
        self.assertIn("srt", command)

    def test_build_command_can_download_selected_playlist_items_only(self) -> None:
        request = DownloadRequest(
            url="https://www.youtube.com/playlist?list=PL_TEST",
            output_dir=Path("C:/Downloads"),
            mode="video",
            playlist_items=(1, 23, 31),
        )

        command = build_command(request)

        self.assertIn("--playlist-items", command)
        self.assertIn("1,23,31", command)


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_audit_matches_real_files_and_marks_only_partial_item_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist = root / "[R2R]03_무작정 따라하는 ROS2 - 실전과정"
            playlist.mkdir()

            (playlist / "[ROS2] 1-1. Activating a USB Camera with the usb_cam Package 1｜R2R Practice.mp4").write_bytes(b"ok")
            (playlist / "[ROS2] 4-4. Let's Monitor the Status of a Robot Drive Controller Implemented with PID.mp4").write_bytes(b"ok")
            (playlist / "[ROS2] 4-2. PID 제어기로 turtlesim 각도 제어하기｜R2R 실전｜민형기 강사님 무료 강의.f299.mp4.part").write_bytes(b"partial")
            (playlist / "[ROS2] 4-2. PID 제어기로 turtlesim 각도 제어하기｜R2R 실전｜민형기 강사님 무료 강의.f251.webm").write_bytes(b"partial")

            request = DownloadRequest(
                url="https://www.youtube.com/playlist?list=PL_TEST",
                output_dir=root,
                mode="video",
                filename_mode="title",
                folder_mode="playlist",
                collection_title="[R2R]03_무작정 따라하는 ROS2 - 실전과정",
            )
            job = QueueJob(
                title="[R2R]03_무작정 따라하는 ROS2 - 실전과정",
                item_count=3,
                request=request,
                entries=[
                    MediaEntry(
                        title="[ROS2] 1-1. Activating a USB Camera with the usb_cam Package 1",
                        url="https://www.youtube.com/watch?v=one",
                        video_id="one",
                    ),
                    MediaEntry(
                        title="[ROS2] 4-4. Let's Monitor the Status of a Robot Drive Controller Implemented with PID in a Cool W...",
                        url="https://www.youtube.com/watch?v=two",
                        video_id="two",
                    ),
                    MediaEntry(
                        title="[ROS2] 4-2. PID 제어기로 turtlesim 각도 제어하기｜R2R 실전｜민형기 강사님 무료 강의",
                        url="https://www.youtube.com/watch?v=three",
                        video_id="three",
                    ),
                ],
            )

            window = MainWindow()
            try:
                results = window._build_audit_results([job])
            finally:
                window.close()

            self.assertEqual([result.status for result in results], ["정상", "정상", "미완료"])
            self.assertTrue(results[2].actual_path)
            self.assertTrue(str(results[2].actual_path).endswith((".part", ".webm")))

    def test_result_filter_shows_only_problem_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist = root / "playlist"
            playlist.mkdir()
            (playlist / "done.mp4").write_bytes(b"ok")
            request = DownloadRequest(
                url="https://www.youtube.com/playlist?list=PL_TEST",
                output_dir=root,
                mode="video",
                folder_mode="playlist",
                collection_title="playlist",
            )
            job = QueueJob(
                title="playlist",
                item_count=2,
                request=request,
                entries=[
                    MediaEntry(title="done", url="https://www.youtube.com/watch?v=one"),
                    MediaEntry(title="missing", url="https://www.youtube.com/watch?v=two"),
                ],
            )

            window = MainWindow()
            try:
                results = window._build_audit_results([job])
                window._display_audit_results("테스트", results)
                problem_index = window.result_filter_combo.findData("problem")
                window.result_filter_combo.setCurrentIndex(problem_index)
                window.refresh_result_filter()
                self.assertEqual(window.result_table.rowCount(), 1)
                self.assertEqual(window.result_table.item(0, 2).text(), "누락")
            finally:
                window.close()

    def test_audit_marks_duration_mismatch_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist = root / "playlist"
            playlist.mkdir()
            (playlist / "lesson.mp4").write_bytes(b"ok")
            request = DownloadRequest(
                url="https://www.youtube.com/playlist?list=PL_TEST",
                output_dir=root,
                mode="video",
                folder_mode="playlist",
                collection_title="playlist",
            )
            job = QueueJob(
                title="playlist",
                item_count=1,
                request=request,
                entries=[MediaEntry(title="lesson", url="https://www.youtube.com/watch?v=one", duration=600)],
            )

            window = MainWindow()
            try:
                with patch.object(app_module, "_media_duration_seconds", return_value=120.0):
                    results = window._build_audit_results([job])
            finally:
                window.close()

            self.assertEqual(results[0].status, "확인 필요")
            self.assertIn("길이 차이", results[0].reason)

    def test_audit_does_not_match_neighbor_lesson_number_as_normal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist = root / "playlist"
            playlist.mkdir()
            (playlist / "[ROS2] 4-1. Turtlesim move sample.mp4").write_bytes(b"ok")
            (playlist / "[ROS2] 4-2. PID controller.f299.mp4.part").write_bytes(b"partial")
            request = DownloadRequest(
                url="https://www.youtube.com/playlist?list=PL_TEST",
                output_dir=root,
                mode="video",
                folder_mode="playlist",
                collection_title="playlist",
            )
            job = QueueJob(
                title="playlist",
                item_count=1,
                request=request,
                entries=[
                    MediaEntry(
                        title="[ROS2] 4-2. PID controller",
                        url="https://www.youtube.com/watch?v=pid",
                        duration=1200,
                    )
                ],
            )

            window = MainWindow()
            try:
                results = window._build_audit_results([job])
            finally:
                window.close()

            self.assertEqual(results[0].status, "미완료")
            self.assertIn("4-2", str(results[0].actual_path))

    def test_preview_exclusion_queues_only_visible_playlist_items(self) -> None:
        window = MainWindow()
        try:
            info = app_module.MediaInfo(
                title="playlist",
                url="https://www.youtube.com/playlist?list=PL_TEST",
                entries=[
                    MediaEntry(title="one", url="https://www.youtube.com/watch?v=one", duration=10),
                    MediaEntry(title="two", url="https://www.youtube.com/watch?v=two", duration=20),
                    MediaEntry(title="three", url="https://www.youtube.com/watch?v=three", duration=30),
                ],
            )
            window.output_input.setText(str(Path(tempfile.gettempdir())))
            window.preview_loaded(info)
            window.preview_table.selectRow(1)
            window.exclude_preview_selection()
            with patch.object(app_module, "check_dependencies") as check:
                check.return_value.yt_dlp_ready = True
                check.return_value.ffmpeg_ready = True
                window.add_preview_to_queue()

            self.assertEqual(len(window._queue), 1)
            self.assertEqual(window._queue[0].request.playlist_items, (1, 3))
            self.assertEqual([entry.title for entry in window._queue[0].entries], ["one", "three"])
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
