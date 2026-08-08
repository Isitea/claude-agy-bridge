#!/usr/bin/env bash
# claude-agy-bridge 제거 스크립트 — install.sh의 대칭. 재실행해도 안전하다.
#
#   curl -fsSL https://raw.githubusercontent.com/Isitea/claude-agy-bridge/main/uninstall.sh | bash
#
# 이 스크립트가 지우는 것은 **전역 바이너리 하나**뿐이다.
#   - 저장소(소스 체크아웃·대상 저장소)는 어떤 경로로도 지우지 않는다.
#   - uv·agy는 우리가 설치하지 않았으므로 제거하지 않는다 (install.sh와 같은 원칙).
#   - 대상 저장소 등록과 런타임 상태는 전용 명령이 따로 있다 (아래 안내).
set -euo pipefail

info() { printf '\033[1;34m[agy-bridge]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[agy-bridge]\033[0m %s\n' "$*" >&2; }

# 1. 남은 등록·상태를 먼저 안내한다 — 바이너리를 지우고 나면 정리 명령도 사라진다.
if command -v agy-bridge >/dev/null 2>&1; then
  info "제거 전 안내: 아래는 이 스크립트가 지우지 않는다."
  warn "  대상 저장소 등록: 각 저장소에서  cd <repo> && agy-bridge deinit --yes"
  warn "  런타임 상태(job·세션·원장):      agy-bridge purge --all --yes"
  warn "  둘 다 지금 실행해 두면 깔끔하다. 지금 넘어가면 나중에 수동 정리가 필요하다."
  echo
fi

# 2. uv가 없으면 아무것도 하지 않는다. 우리가 설치하지 않은 도구는 다루지 않는다.
if ! command -v uv >/dev/null 2>&1; then
  warn "uv가 없어 uv tool로 설치된 브리지를 제거할 수 없습니다."
  warn "수동으로 설치했다면 그 방법에 맞춰 제거하세요 (예: ~/.local/bin/agy-bridge 삭제)."
  exit 1
fi

# 3. 브리지 제거 — uv는 자기 venv와 심링크만 지운다. 소스 체크아웃은 그대로다.
if uv tool list 2>/dev/null | grep -q '^agy-bridge'; then
  info "agy-bridge 제거 (uv tool uninstall)"
  uv tool uninstall agy-bridge
else
  info "uv tool에 agy-bridge가 없습니다 — 이미 제거됐거나 다른 방법으로 설치됐습니다."
fi

# 4. 잔존 확인
if command -v agy-bridge >/dev/null 2>&1; then
  warn "PATH에 agy-bridge가 아직 있습니다: $(command -v agy-bridge)"
  warn "다른 방법으로 설치된 사본일 수 있습니다 — 직접 확인하세요."
else
  info "제거 완료. 저장소와 agy·uv는 그대로입니다."
fi

# 5. 상태 디렉터리 위치 안내 (바이너리가 없으면 purge를 못 쓰므로 경로를 알려 준다)
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/claude-agy-bridge"
if [ -d "$CACHE_ROOT" ]; then
  warn "런타임 상태가 남아 있습니다: $CACHE_ROOT"
  warn "  필요 없으면 직접 삭제하세요 (프로젝트별 job·세션·원장만 들어 있습니다)."
fi
