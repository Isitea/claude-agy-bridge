# claude-agy-bridge

Claude Code 세션이 Antigravity CLI(`agy`)를 **독립 과학 검증자**로 부르게 하는
MCP 브리지. 시뮬레이션·수치 코드의 열역학 처리, 단위계, 근사 타당성, 오차 전파를
다른 모델(기본 `gemini-3.7-flash`, effort `high`)의 눈으로 재검토받는다.

브리지 자체에는 LLM이 없다. 설정 해석, 파일 인라이닝, 서브프로세스 실행, 상태
관리는 전부 결정론적 코드다 — 판단은 양 끝단(Claude와 agy)에만 있다.
설계 전문은 `docs/plan.md`.

## 사전 작업 (Prerequisites)

| 항목 | 필수 | 확인 방법 |
|---|---|---|
| `agy` CLI 설치 + OAuth 인증 | 필수 | `agy models` 가 모델 목록을 출력 |
| `claude` CLI (MCP 클라이언트) | 필수 | — |
| `uv` (브리지 설치용) | 필수 | `uv --version` |
| 브리지 설치 | 필수 | 아래 "설치". `agy-bridge doctor` 전 항목 통과 |
| 대상 저장소 `.mcp.json` 등록 | 필수 | `agy-bridge init --target <repo>` 가 수행 |
| agy 권한 설정 | **불필요** | 인라이닝·루프백 서빙 모두 권한이 필요 없다 |
| agy 과학 스킬 팩 | 선택 | 아래 "외부 스킬 팩" — 저장소에 벤더링하지 않는다 |

## 설치

```bash
curl -fsSL https://raw.githubusercontent.com/Isitea/claude-agy-bridge/main/install.sh | bash
#  → ~/.local/bin/agy-bridge 를 만든다. 대상 저장소는 이 이름만 참조한다.
#    재실행해도 안전하다. uv가 없으면 아무것도 바꾸지 않고 설치 안내만
#    출력하고 중단한다 (사전 작업 표의 uv는 필수 요건).

# 이후 업데이트:
agy-bridge update
```

수동 설치를 원하면 (동일한 결과):

```bash
uv tool install --from git+https://github.com/Isitea/claude-agy-bridge agy-bridge
# 개발 중 로컬 체크아웃에서: uv tool install --force --from /path/to/checkout agy-bridge
```

## 대상 저장소 등록

```bash
cd /path/to/target-repo
agy-bridge init          # --target 생략 시 현재 디렉터리의 git 루트가 대상
```

수행 내용: `.mcp.json`에 `mcpServers.agy` 등록(기존 항목 보존), 주석 처리된
`.agy-bridge.toml` 템플릿 생성, `.agy-bridge/playbooks/` 오버레이 자리 생성,
agy 바이너리 확인, 스모크 왕복 1회(저비용 모델)로 인증 검증.
끝으로 `CLAUDE.md` 사용 지침 스니펫 반영 여부를 **물어본다** — 대화형에서 `y`로
승인하면 추가·갱신하고(기존 절은 교체해 중복 방지), `--claude-md` 플래그로
묻지 않고 반영할 수도 있다. 비대화형에서는 제안만 출력한다. 브리지 업데이트 후
`agy-bridge init`을 다시 실행하면 스니펫 최신화에도 쓸 수 있다(설정·오버레이는
보존됨).

문제가 생기면 대상 저장소에서:

```bash
agy-bridge doctor    # 바이너리·인증·상태·예산 자가 진단, 조치 문장 출력
agy-bridge budget    # 오늘의 호출 사용량·잔여 예산 리포트
```

## 제거

설치가 3단계였으므로 제거도 3단계다. 각각 독립적이라 필요한 것만 하면 된다.

```bash
cd /path/to/target-repo
agy-bridge deinit          # 1) 저장소 등록 해제 (미리보기)
agy-bridge deinit --yes    #    실제 수행

agy-bridge purge --all     # 2) 런타임 상태(job·세션·원장) — 미리보기
agy-bridge purge --all --yes

curl -fsSL https://raw.githubusercontent.com/Isitea/claude-agy-bridge/main/uninstall.sh | bash
#                          # 3) 전역 바이너리 (uv tool uninstall 위임)
```

원칙은 하나다 — **브리지가 만든 것만 지운다.**

- 저장소·소스 체크아웃·사용자 저작물은 어떤 경로로도 삭제되지 않는다. `deinit`은
  `.mcp.json`의 `agy` 항목과 `CLAUDE.md`의 해당 절, init이 만든 `_TEMPLATE.md`만
  되돌리고, `.agy-bridge.toml`과 직접 쓴 오버레이는 **보존한다**(지우려면
  `--purge-config`). 다른 MCP 서버 항목과 `CLAUDE.md`의 다른 절도 그대로 둔다.
- `uv`와 `agy`는 우리가 설치하지 않았으므로 제거하지 않는다(설치 정책의 대칭).
  `uv tool uninstall`은 uv 자신의 venv와 심링크만 지우므로 **로컬 체크아웃은
  영향받지 않는다** — 개발 중이라면 제거 후에도 `uv run pytest`가 그대로 동작한다.
- 기본은 미리보기다. `--yes`가 있어야 실제로 지운다.
- 브리지 소스 저장소에서 `deinit`을 실행하면 거부한다(개발·테스트 환경 보호).
- `purge`는 실행 중인 job이 있으면 거부한다 — 원장을 지우면 예산 계측이 깨지고
  분리 실행된 agy가 고아가 된다. 먼저 `agy_cancel`로 정리하라.

상태 디렉터리 이름은 프로젝트 경로의 해시라 눈으로는 알아볼 수 없으므로, `purge`가
각 디렉터리의 출처 경로와 크기를 함께 보여준다.

## 제공 도구 (MCP)

| 도구 | 역할 |
|---|---|
| `agy_consult` | 자문·검증 요청. 45초까지 동기 대기, 넘으면 job 핸들 반환 |
| `agy_result` | job 결과 회수 (폴링) |
| `agy_followup` | 기존 세션의 conversation을 이어서 재질문 |
| `agy_cancel` | 실행 중 job 중단 |
| `agy_sessions` | 세션·진행 중 job 목록, 세션 닫기 |

### 검증자(agy)의 능력 경계 — 실측 (2026-08-07)

| 동작 | 가능 여부 | 비고 |
|---|---|---|
| 웹 검색 | ✅ | 권한 불필요. `literature` 모드에서 최신 문헌·표준 기법 확인에 활용 |
| URL fetch (루프백·외부) | ✅ | 권한 불필요. 대용량 자료 서빙(전략 C)의 기반 |
| 셸 명령 실행 (grep, curl 등) | ❌ | 헤드리스 자동 거부 → 침묵 실패(§2.3-A). 브리지가 오류로 승격 |
| 파일시스템 읽기 | ❌ | 같은 이유. 그래서 인라이닝/서빙 구조가 존재한다 |
| 파일 수정·편집 | ❌ | `--mode plan` 고정으로 의도적 차단 (§10) |

함의: 검증자는 **저장소를 스스로 탐색하지 못한다**. 검토 대상은 반드시 `files`
인자로 전달해야 하며(실행 CWD도 빈 스크래치 디렉터리다 §8.2), 질문에
"grep해 봐" 같은 셸 유도 표현을 쓰면 검증자가 명령을 시도하다 침묵 실패한다 —
"제공된 자료에서 확인하라"가 안전하다.

### 사용 원칙 (소비 세션이 알아야 할 전부)

- **드물게, 충분한 맥락을 담아 물어라.** 호출당 고정비가 프로세스 기동 ~10초 +
  입력 17k 토큰이고, review/verify는 수 분 걸린다. 구현 중 사소한 질문에 쓰지 마라.
- **`context` 인자가 품질을 가른다.** 이론 수준, 단위계, 온도·압력, 가정,
  경계조건 등 코드만으로 알 수 없는 정보를 담아라.
- **`files`는 순서가 우선순위다.** 앞에서부터 인라이닝 예산(100,000자, 한글 등
  멀티바이트는 바이트 상한이 먼저 걸릴 수 있음)에 담고, 넘치는 파일은 루프백
  HTTP 서빙으로 자동 전환된다(결과에 전환 사유 명시). 검토 대상을 앞에, 주변
  자료를 뒤에. **경로는 프로젝트 루트 안쪽만 허용된다** — 루트 밖(절대경로·
  상위 탈출·심링크)은 거부되므로, 밖의 자료는 저장소 안으로 복사한 뒤 지정하라.
- **`{"status": "running"}`이 오면 기다리지 마라.** 다른 작업을 계속하다가
  `agy_result`로 회수하라. 비동기가 정상 경로다.
- **같은 주제는 같은 `session_id`로.** 캐시 히트로 저렴해지고 검증자가 앞선
  논의를 기억한다. 후속 질문은 `agy_followup`.
- **반환값은 자문 의견이지 사실이 아니다.** verdict의 evidence를 직접 검토한 뒤
  코드에 반영하라. `insufficient_context`가 오면 context를 보강해 재시도하라.

## 설정 레퍼런스 (`.agy-bridge.toml`, 대상 저장소)

```toml
model  = "gemini-3.7-flash"      # 검증 독립성 — Claude 계열은 피하라
effort = "high"                  # low | medium | high (패밀리별 지원 범위가 다르다)
#                                # `gemini-3.1-pro-high`처럼 수준이 박힌 ID를 쓰면
#                                # effort는 그 ID가 정한다 — 어긋나면 거부된다

[playbooks]
enabled     = ["units-and-scales", "assumption-validity", "uncertainty-propagation"]
                                 # 생략 시 mode별 기본 매핑. 내장 7종:
                                 # units-and-scales, assumption-validity,
                                 # conservation-and-balance, uncertainty-propagation,
                                 # numerics, derivation, data-provenance
overlay_dir = ".agy-bridge/playbooks"

[limits]
max_inline_chars  = 100000       # 인라이닝→서빙 자동 전환 임계값(문자).
                                 # 바이트 상한이 병행 적용되며 멀티바이트 자료는
                                 # 그쪽이 먼저 걸린다
wait_seconds      = 45           # 동기 대기 창
print_timeout     = 600          # agy 자체 타임아웃 (초)
hard_kill_seconds = 900          # 브리지의 최종 안전망. job 전체 상한이며
                                 # 재시도해도 늘어나지 않는다
daily_call_budget = 60           # 초과 시 스폰 전에 사유와 함께 거부

[context]
# 기본값은 흔한 키·자격증명(.env*, *.pem, *.key, id_rsa*, .netrc, */.ssh/*,
# */.aws/* 등)과 대형 산출물(*.chk, *.wfn)을 폭넓게 막는다. 아래처럼 지정하면
# 기본값을 완전히 대체한다(보강 아님).
deny_globs = [".env*", "*.pem", "*.key", "id_rsa*", "*.chk", "*.wfn"]
```

> `files`는 프로젝트 루트 안쪽 경로만 허용된다(절대경로·상위 탈출·심링크·
> 다중 하드링크는 거부). 루트가 홈 디렉터리·`/`가 되면 봉쇄가 약해지므로
> `.git`이 있는 실제 저장소에서 실행하라 — `agy-bridge doctor`가 경고한다.

우선순위: 도구 호출 인자 > `.agy-bridge.toml` > 환경변수(`AGY_BIN`,
`AGY_BRIDGE_PROJECT_ROOT`, `AGY_BRIDGE_MODEL`, `AGY_BRIDGE_EFFORT`) > 내장 기본값.
값이 잘못되면(정수 자리에 문자열, 목록 자리에 문자열 등) 기동 시점에 조치 문장과
함께 거부한다. 상태(job·세션·원장)는 `~/.cache/claude-agy-bridge/<프로젝트 해시>/`에
프로젝트별로 격리되며, 종결된 job의 산출물은 30일 뒤 자동 정리된다.

> **알아 둘 제약**: agy는 프롬프트를 명령행 인자로 받는다(`-p`). 따라서 인라이닝된
> 파일 내용이 실행 중 같은 사용자의 다른 프로세스에게 `/proc/<pid>/cmdline`으로
> 보인다. 다중 사용자 머신에서 민감한 소스를 다룬다면 이 점을 감안하라 — 서빙
> 전략(전략 C)은 프롬프트에 URL만 실으므로 노출 면적이 훨씬 작다.

## 오버레이 — 프로젝트 고유 검증 항목 (§8.6)

`.agy-bridge/playbooks/*.md`(`_` 접두 제외)는 자동 발견되어 내장 플레이북 뒤에
주입된다. 이 저장소에서만 참인 것(단위 규약, 자체 자료구조 불변식, 팀 검증
기준)만 적어라. 일반 과학 지식과 패키지 사용법은 검증자 모델이 이미 안다.
작성 지침은 init이 만든 `_TEMPLATE.md` 참조.

## 실패 대응표

| 증상 | 의미 | 조치 |
|---|---|---|
| `response가 비어 있다... §2.3-A` 오류 | agy가 권한 필요한 도구를 시도하다 헤드리스 자동 거부됨. 브리지가 침묵을 오류로 승격한 것 | 오류 안의 stderr를 읽어라. 재시도 전에 질문·files 구성을 바꿔라. 이것은 "검증 통과"가 아니다 |
| `일일 호출 예산 초과` | 오늘 시작한 호출이 `daily_call_budget` 도달 | 자정(로컬) 초기화 대기 또는 `.agy-bridge.toml`에서 상한 조정 |
| `agy 바이너리를 찾을 수 없다` | agy 미설치 또는 PATH 밖 | Antigravity CLI 설치, 또는 `AGY_BIN`으로 경로 지정 |
| 스모크/호출에서 인증 오류 | OAuth 만료 | `agy models`로 확인, agy를 대화형으로 실행해 재인증 |
| `job ... timeout` | agy가 하드 킬 한계(900초) 초과 | 질문·자료를 쪼개라. 서빙 자료가 크면 지연이 크기에 비례함을 감안 |
| `인라이닝 합계가 상한 초과` (전략 A 전용 경로) | 단일 정책에서는 자동 서빙 전환되므로 일반적으론 안 봄 | files 행범위를 좁혀라 |
| 실행 중 브리지가 재시작됨 | job은 분리 실행이라 살아 있다 | 같은 프로젝트에서 `agy_result(job_id)` — 출력 파일에서 자동 회수 |

## 비용 특성 (실측)

| 항목 | 값 |
|---|---|
| 호출 고정비 | 프로세스 기동 ~10초 + 입력 ~17k 토큰 |
| 소형 인라이닝 리뷰 (~100 KB) | ~15–80초 |
| 서빙 왕복 | 크기 비례 (600 KB ≈ 46초, 2 MB ≈ 40–100초) |
| 세션 재개 | 프롬프트 캐시 히트로 비용 대폭 절감 |

## 외부 스킬 팩 (선택)

`google-deepmind/science-skills`(Apache 2.0) 같은 외부 자산은 이 저장소에
벤더링하지 않는다. 원하면 각자 로컬에 받아 둔다 (브리지 동작에 불필요 —
검증 절차는 내장 플레이북이 담당):

```bash
git clone --depth 1 https://github.com/google-deepmind/science-skills \
  .agents/plugins/science-skills
```

## 개발 (본 저장소)

```bash
mise install          # Python 3.14 + uv
uv run pytest         # 테스트 (실제 agy 호출 없음 — 가짜 바이너리 사용)
uv run agy-bridge --help
```
