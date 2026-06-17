# YouTube Downloader GUI

Windows에서 YouTube 단일 영상 또는 재생목록을 GUI로 다운로드하는 PySide6 기반 데스크톱 도구입니다.

## 주요 기능

- 단일 영상 URL 다운로드
- 재생목록 URL 다운로드
- 영상 MP4 다운로드
- 음원 MP3 추출
- 영상 화질 선택: 최고화질, 2160p, 1440p, 1080p, 720p, 480p, 360p 이하
- 저장 폴더 선택 및 마지막 선택 기억
- 진행률, 로그, 취소 버튼
- Python, yt-dlp, ffmpeg 상태 표시

재생목록 URL 또는 `list=`가 포함된 YouTube URL을 넣으면 전체 재생목록 다운로드를 우선합니다.

## 실행 방법

1. `run_app.bat`를 더블클릭합니다.
2. 처음 실행할 때 `.venv` 가상환경을 만들고 `PySide6`, `yt-dlp`를 설치합니다.
3. 앱이 열리면 YouTube URL, 저장 위치, 다운로드 형식을 선택합니다.
4. 영상으로 받을 경우 원하는 화질을 선택합니다.
5. `다운로드 시작`을 누릅니다.

## 필요한 도구

- Python 3.10 이상
- `yt-dlp`: `run_app.bat`가 자동 설치합니다.
- `ffmpeg`: MP3 변환 또는 고화질 영상 병합에 필요합니다.
- Node.js, Deno 또는 Bun: 최신 YouTube 추출에서 JavaScript 런타임이 필요할 수 있습니다.

## ffmpeg 설치

MP3 변환과 고화질 MP4 병합에는 `ffmpeg`가 필요합니다. Windows에서는 ffmpeg를 설치한 뒤 `bin` 폴더를 Windows `PATH`에 추가하세요.

앱은 Node.js, Deno, Bun 중 설치된 JavaScript 런타임을 감지해 `yt-dlp --js-runtimes ... --remote-components ejs:github` 옵션을 자동으로 사용합니다.

## 설정 저장 위치

앱은 마지막 저장 폴더와 선택한 형식을 아래 파일에 저장합니다.

```text
%APPDATA%\YoutubeDownloaderGui\settings.json
```

## 다음 단계

- PyInstaller로 exe 포장
- Chrome 확장 프로그램에서 현재 YouTube URL을 앱으로 보내기
- 재생목록별 하위 폴더 자동 생성 옵션
