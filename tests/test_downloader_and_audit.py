from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

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


if __name__ == "__main__":
    unittest.main()
