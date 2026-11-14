#!/usr/bin/env bash
# rea-video 안전 설치 (검토한 커밋 고정, 격리된 venv, ~/.claude 등 에이전트 폴더는 건드리지 않음)
# 사용: bash setup.sh            → 설치(이미 있으면 확인만) 후 마지막 줄에 CLI 경로 출력
#       REA_VIDEO_REF=<commit> bash setup.sh   → 다른 커밋으로 (반드시 직접 검토 후)
set -euo pipefail

DIR="${REA_VIDEO_DIR:-$HOME/.local/share/rea-video}"
REF="${REA_VIDEO_REF:-356815c}"   # 2026-11-09 커밋, 보안 검토 완료 버전
REPO="https://github.com/JuneYaooo/rea-video"
CLI="$DIR/.venv/bin/rea-video"

# ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "✗ ffmpeg/ffprobe 없음. macOS: brew install ffmpeg / Ubuntu: sudo apt install ffmpeg" >&2
  exit 1
fi

# 이미 설치돼 있고 고정 커밋이 맞으면 끝
if [ -x "$CLI" ] && [ "$(git -C "$DIR" rev-parse --short=7 HEAD 2>/dev/null)" = "${REF:0:7}" ]; then
  echo "$CLI"; exit 0
fi

# Python: 3.12 > 3.11 > 3.10 (3.13은 rapidocr-onnxruntime>=1.3 휠이 없어 OCR 설치 실패)
PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
  for c in python3.12 python3.11 python3.10; do
    command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
  done
fi
if [ -z "$PY" ]; then
  echo "✗ Python 3.10~3.12 필요 (3.13 단독이면 OCR 패키지가 설치되지 않음). 예: brew install python@3.12 / uv python install 3.12 후 PYTHON_BIN=python3.12" >&2
  exit 1
fi

# 소스 받기 + 커밋 고정
if [ ! -d "$DIR/.git" ]; then
  mkdir -p "$(dirname "$DIR")"
  git clone -q "$REPO" "$DIR"
fi
git -C "$DIR" fetch -q origin 2>/dev/null || true
git -C "$DIR" -c advice.detachedHead=false checkout -q "$REF"

# 격리 venv에 설치 (편집 모드 아님: 저장소가 바뀌어도 설치본은 그대로)
rm -rf "$DIR/.venv"
"$PY" -m venv "$DIR/.venv"
VPIP="$DIR/.venv/bin/python -m pip"
$VPIP install -q --upgrade pip >/dev/null 2>&1 || true
LOG="$(mktemp)"; trap 'rm -f "$LOG"' EXIT
if ! $VPIP install -q "$DIR[ocr]" >"$LOG" 2>&1; then
  echo "⚠ OCR 포함 설치 실패 → OCR 없이 설치 (자막/화면 글자 분석 불가)" >&2
  grep -E "ERROR" "$LOG" | head -3 >&2 || true
  $VPIP install -q "$DIR" >"$LOG" 2>&1 || { cat "$LOG" >&2; exit 1; }
fi

# rapidocr이 끌고 온 opencv-python 제거, headless만 남김 (venv 안에서만)
if $VPIP show opencv-python >/dev/null 2>&1; then
  $VPIP uninstall -y -q opencv-python
  $VPIP install -q --force-reinstall --no-deps "opencv-python-headless>=4.8,<5"
fi

"$CLI" --help >/dev/null
echo "$CLI"
