#!/usr/bin/env bash
# claude-agy-bridge 설치 스크립트 — 재실행해도 안전하다 (업데이트로도 동작).
#
#   curl -fsSL https://raw.githubusercontent.com/Isitea/claude-agy-bridge/main/install.sh | bash
#
# 환경변수로 소스를 바꿀 수 있다:
#   AGY_BRIDGE_GIT  설치 소스 저장소 (기본: https://github.com/Isitea/claude-agy-bridge)
#   AGY_BRIDGE_REF  git ref — 브랜치·태그 (기본: main)
set -euo pipefail

REPO="${AGY_BRIDGE_GIT:-https://github.com/Isitea/claude-agy-bridge}"
REF="${AGY_BRIDGE_REF:-main}"

info() { printf '\033[1;34m[agy-bridge]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[agy-bridge]\033[0m %s\n' "$*" >&2; }

# 1. uv — 없으면 공식 설치 스크립트로 조달
if ! command -v uv >/dev/null 2>&1; then
  info "uv가 없어 설치합니다 (astral.sh 공식 스크립트)"
  curl -fsSL https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2. 브리지 설치·갱신 (--force 라 기존 설치 위에 덮어쓴다)
info "agy-bridge 설치: ${REPO} @ ${REF}"
uv tool install --force --from "git+${REPO}@${REF}" agy-bridge

# 3. PATH 점검
if ! command -v agy-bridge >/dev/null 2>&1; then
  warn "~/.local/bin 이 PATH에 없습니다. 'uv tool update-shell' 실행 후 셸을 재시작하세요."
else
  info "설치 완료: $(agy-bridge --version)"
fi

# 4. agy(Antigravity CLI) 점검 — 브리지의 실행 전제. 없어도 설치는 유효하므로 경고만.
if command -v agy >/dev/null 2>&1; then
  info "agy 바이너리 확인: $(command -v agy)"
else
  warn "agy(Antigravity CLI)가 PATH에 없습니다. 사용 전에 설치·인증하세요. 확인: agy models"
fi

info "다음 단계: cd <대상 저장소> && agy-bridge init"
