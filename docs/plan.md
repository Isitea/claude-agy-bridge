# claude-agy-bridge 설계 및 개발 계획

Claude Code(구현 담당)가 자연과학 시뮬레이션 코드를 작성하는 도중,
Antigravity(`agy`, 과학 스킬 탑재)를 **과학적 자문·검증자**로 호출하기 위한 MCP 서버.

작성일: 2026-08-07 / 개정: 2026-08-07 (런타임 확정, 이식성 요구사항 반영) / 상태: 설계 초안 (Phase 0 착수 전)

> **투입 대상**: 본 저장소는 도구 자체를 개발하는 곳이며, 완성 후
> **별도 세션에서 진행 중인 "양자화학 계산 기반 공정 시뮬레이터 개발" 프로젝트에 투입**된다.
> 즉 이 도구는 *현재 세션이 쓰는 스크립트*가 아니라 *다른 저장소·다른 세션에 설치되는 제품*이다.
> 이 전제가 §7(배포), §8(과학 능력), §9(핸드오프) 설계를 지배한다.

---

## 1. 목표와 범위

### 목표
- Claude Code 세션 내부에서 **도구 호출 한 번**으로 Antigravity에게 물리/수치/유도 검증을 요청한다.
- 대화 맥락을 유지한 채 여러 차례 왕복(follow-up)할 수 있다.
- 검증 결과는 사람이 읽는 산문이 아니라 **Claude가 곧바로 행동에 옮길 수 있는 구조화된 판정**으로 받는다.

- **이식 가능**해야 한다. 임의의 대상 저장소에 설치되어, 본 저장소의 경로·환경을 전혀 참조하지 않고 동작한다.
- **자기설명적**이어야 한다. 이 설계 대화를 보지 못한 다른 세션의 Claude가 도구 설명만 읽고 올바르게 쓸 수 있어야 한다.

### 비목표 (명시적 제외)
- Antigravity가 저장소 파일을 **수정**하게 하지 않는다. 자문·검증 전용(read-only advisor)이다.
- 양방향 대칭 브리지(agy → claude 호출)는 이번 범위 밖. 필요해지면 별도 단계로 분리한다.
- Anthropic/Google API 직접 호출은 하지 않는다. 두 CLI 모두 OAuth 인증이 끝나 있으므로 **CLI 서브프로세스**를 계약면으로 삼는다. (API 키 관리·과금 경로를 새로 만들지 않는 것이 핵심 이점)
- Node.js 사용 안 함. MCP stdio 서버에 node가 필요하지 않으며, 런타임 표면을 줄이는 편이 이식에 유리하다.

---

## 2. 사전 조사 결과 (실측)

계획의 전제가 되는 사실들은 추정이 아니라 이 머신에서 직접 실행해 확인했다.

### 2.1 확인된 `agy` 능력

| 항목 | 확인 내용 |
|---|---|
| 비대화 실행 | `agy -p "<prompt>" --output-format json` 정상 동작 |
| 응답 스키마 | `{conversation_id, status, response, duration_seconds, num_turns, usage{input,output,thinking,cache_read,total}}` |
| 대화 재개 | `--conversation <id>` 로 맥락 유지 확인 (2턴째 이전 발화 정확히 회상) |
| 프롬프트 캐시 | 재개 시 `cache_read_tokens: 12206` 관측 → 세션 재사용이 비용상 유리 |
| 구조화 출력 | `--json-schema <file|string>` → 응답에 `structured_output` 필드가 **파싱된 객체**로 포함됨 |
| 모델 선택 | `--model` (아래 목록), `--effort low\|medium\|high` |
| 워크스페이스 | `--add-dir` (반복 가능), `--sandbox`, `--mode plan\|accept-edits` |
| 타임아웃 | `--print-timeout` 기본 **5분** |
| 커스터마이즈 | 스킬 = `.agents/skills/<name>/SKILL.md`(워크스페이스) 또는 `~/.gemini/config/skills/`(전역), 규칙 = `AGENTS.md`/`GEMINI.md` |
| 프롬프트 입력 | **stdin 미지원.** `-p`는 argv 값만 받는다 → 128 KiB 한계에 직결(§2.3-D) |
| 로컬 HTTP fetch | **권한 부여 없이 헤드리스에서 동작**(§2.3-E). 대용량 컨텍스트의 우회로 |

사용 가능 모델: `gemini-3.1-pro-high` / `gemini-3.1-pro-low` / `gemini-3.6-flash-{high,medium,low}` /
`gemini-3.5-flash-{high,medium,low}` / `claude-sonnet-4-6` / `claude-opus-4-6-thinking` / `gpt-oss-120b-medium`

> 검증 작업의 **독립성**을 위해 기본 모델은 `gemini-3.1-pro-high`로 고정한다.
> agy를 통해 Claude 모델을 부르면 "다른 관점의 검증"이라는 목적이 훼손된다.

### 2.2 실측 비용·지연

| 측정 | 값 |
|---|---|
| 프로세스 콜드스타트 오버헤드 | **약 10~11초** (총 13.6초 중 추론 2.9초) |
| 매 호출 기본 입력 토큰 | **약 17k** (agy 자체 시스템 프롬프트) |
| 세션 재개 2턴째 | 총 23.9초, 캐시 히트 12.2k |

**설계상 함의**: 호출당 고정비가 10초+17k 토큰이다. 따라서
**"자주 잘게 묻기"가 아니라 "충분한 맥락을 담아 드물게 묻기"** 로 도구를 설계해야 한다.
또한 `gemini-3.1-pro-high`로 실제 코드 리뷰를 시키면 수 분이 걸리므로 **동기 호출만으로는 부족**하다(§5).

### 2.3 확인된 함정 (설계에 반드시 반영)

**(A) 헤드리스 권한 자동 거부 — 조용한 실패**

`agy -p`로 파일 읽기를 요청하면 다음이 발생한다:

```
stderr: jetski: no output produced — a tool required the "command" permission that
        headless mode cannot prompt for, so it was auto-denied.
stdout: {"conversation_id":"...","status":"SUCCESS","response":"", ...}
```

`status`는 **SUCCESS인데 `response`가 빈 문자열**이다. 이것이 가장 위험한 실패 모드다.
→ 브리지는 `status == SUCCESS`를 신뢰해서는 안 되며, **빈 `response`를 오류로 승격**하고
stderr를 그대로 호출자에게 전달해야 한다. (Phase 1 필수 테스트 항목)

**(B) 런타임 — 해결됨**

초기 조사 시 WSL에 `node` 부재, `python3`은 있으나 `pip`/`ensurepip` 부재였다.
현재 `mise.toml`로 **Python 3.14.6 / uv 0.11.29 / node 26.5.1** 조달 완료.
node는 사용하지 않으므로 `mise.toml`의 `node = "latest"` 항목은 제거 가능하다(§7).

**(C) 커스터마이즈 탐색 경로가 CWD에 종속됨 — 이식성의 핵심 제약**

agy는 워크스페이스 커스터마이즈(`.agents/skills/`, `AGENTS.md`)를 **CWD에서 저장소 루트까지 거슬러 올라가며**
탐색한다. 따라서:

- 본 저장소의 `.agents/skills/`에 과학 스킬을 두면, **대상 프로젝트에서 실행할 때 로드되지 않는다.**
- 반대로 대상 저장소를 CWD로 삼으면, 그 저장소의 `AGENTS.md`(구현자용 지시일 가능성이 높음)를
  agy가 상속해 **검토자 역할과 충돌**할 수 있다.

→ §8에서 "프롬프트 내장 플레이북" 방식으로 이 제약을 우회한다.

**(D) argv 단일 인자 128 KiB 하드 리밋 — 인라이닝의 천장**

`agy -p`는 **stdin을 읽지 않는다.** 프롬프트를 argv 값으로만 받으므로 Linux의
`MAX_ARG_STRLEN`에 그대로 걸린다. 경계를 직접 이분 탐색해 확인했다:

| 프롬프트 크기 | 결과 |
|---|---|
| 131,000 B | OK |
| **131,072 B** | **E2BIG (Argument list too long)** |
| 1.25 MB | E2BIG — 프로세스가 뜨지도 못함 |

`getconf ARG_MAX`가 2 MB인 것과 무관하다. 그것은 인자 *전체* 합이고,
**단일 인자**는 128 KiB로 따로 제한된다. 흔히 혼동되는 지점이다.

→ 인라이닝 상한을 100,000자로 잡아 안전 마진을 둔다. 그 이상은 전략 C(§4.3)로 넘긴다.

**(E) 로컬 HTTP fetch는 권한 없이 통과한다 — (A)의 우회로**

파일 읽기 도구는 자동 거부되지만(함정 A), **루프백 URL fetch는 헤드리스에서 그대로 동작한다.**
이 비대칭이 대용량 컨텍스트 문제를 푸는 열쇠다.

| 실측 | 결과 |
|---|---|
| 60 KB 서빙, 85% 깊이 표식 | 회수 성공, 권한 부여 불필요 |
| 600 KB 서빙, 10·50·95% 깊이 표식 3개 | 3/3 회수 |
| 400 KB 서빙, 개수 미고지 표식 6개 | 6/6 회수 |

**커버리지 검증 (grep 지름길 차단).** 위 실측만 보고 "입력 토큰이 낮으니 전문이 읽히지 않는다"고
판단했으나, **이는 오판이었다.** 공통 부분문자열이 전혀 없는 래퍼로 감싼 인공 단어 8개를
균등 분포시켜 — 즉 리터럴 검색이 불가능하게 만들어 — 다시 측정했다. 모델은 프로덕션 설정
(`gemini-3.1-pro-high`)을 사용했다.

| 파일 크기 | 회수 | 소요 | input_tokens | cache_read_tokens |
|---|---|---|---|---|
| 600 KB | **8 / 8** (행 번호까지 정확) | 45.9 s | 41,356 | 93,543 |
| **2 MB** | **8 / 8** | 100.0 s | 75,492 | 448,253 |

낮은 `input_tokens`는 전문이 안 읽혔다는 뜻이 아니라, agy가 **행 번호가 붙은 탐색 가능한 뷰**로
파일을 여러 번 나눠 읽고 그 중간 상태가 캐시로 계상되기 때문이다(`cache_read`가 크기에 비례해 증가).

**결론: 전략 C에 커버리지 손실은 없다. 비용은 정확도가 아니라 지연으로 나타난다.**
크기가 3.3배 늘 때 소요 시간은 2.2배, 캐시 읽기는 4.8배 증가했다.
이 정정이 §4.3 전체를 다시 쓰게 했다.

---

## 3. 아키텍처

```
┌────────────────────────┐
│  Claude Code (세션)     │  시뮬레이션 코드 작성 주체
│   └ MCP client          │
└──────────┬─────────────┘
           │ stdio (JSON-RPC)
┌──────────▼─────────────┐
│  claude-agy-bridge      │  본 저장소
│  ├ prompts  역할별 템플릿 │
│  ├ context  파일 인라이닝 │
│  ├ runner   서브프로세스  │
│  ├ jobs     비동기 레지스트리
│  └ sessions conversation_id 매핑
└──────────┬─────────────┘
           │ exec: agy -p --output-format json [--conversation ID] [--json-schema ...]
┌──────────▼─────────────┐
│  Antigravity CLI        │  Gemini 3.1 Pro + 과학 스킬(.agents/skills)
└─────────────────────────┘
```

### 3.1 브리지에는 LLM이 없다

이 점을 명확히 해 둔다. **MCP 서버는 순수하게 기계적으로 동작하며 모델을 호출하지 않는다.**
지능은 양 끝단에만 있다.

| 단계 | 주체 | 성격 |
|---|---|---|
| 무엇을 언제 물을지 결정 (mode·files·question 작성) | **Claude** | 판단 |
| 설정 해석, 파일 읽기, 플레이북 선택, 프롬프트 문자열 조립 | **브리지** | 결정론적 코드 |
| 서브프로세스 실행, JSON 파싱, job·세션 상태 관리 | **브리지** | 결정론적 코드 |
| 과학적 판단 | **agy (Gemini)** | 판단 |
| 판정을 받아 코드에 반영 | **Claude** | 판단 |

브리지 안의 모든 "선택"은 **테이블 조회이거나 설정값**이다.
`mode → 플레이북` 매핑은 정적 사전이고, 어떤 파일이 중요한지 고르는 일은 브리지가 하지 않는다.
그건 이미 코드베이스를 컨텍스트에 들고 있는 Claude의 몫이다.

의도적인 설계다.

- **테스트 가능하다.** 함정 A(빈 응답 승격)에 대한 회귀 테스트가 성립하는 것은 브리지가 코드이기 때문이다.
  중간에 모델이 있으면 그 경계는 결정론적으로 검증할 수 없다.
- **세 번째 의견이 끼어들지 않는다.** 중간 모델이 요약·선별하면 검증자에게 도달하는 내용이
  왜곡되고, 정작 문제가 있는 줄이 요약 과정에서 사라질 수 있다.
- **비용이 0에 수렴한다.** 브리지는 토큰을 쓰지 않고 수 밀리초를 쓴다.

즉 브리지는 번역기이자 배관이다. 판단은 하지 않는다.

### 3.2 파일시스템 공유

두 프로세스는 동일 파일시스템 위에 있지만, 함정 A 때문에 "경로만 주고받는" 단순한 형태는
기본 경로가 될 수 없다. 실제 컨텍스트 경로는 §4.3의 세 전략으로 갈린다.

---

## 4. MCP 도구 설계

도구는 **5개로 제한**한다. 도구가 많으면 Claude가 선택에 소모하는 토큰이 늘고, 실제로 필요한
동작은 "묻는다 / 결과를 받는다 / 이어서 묻는다" 세 가지뿐이다.

### 4.1 도구 목록

| 도구 | 역할 |
|---|---|
| `agy_consult` | 자문·검증 요청. 동기 대기하다 한계를 넘으면 job 핸들 반환 |
| `agy_result` | job 결과 조회 (폴링) |
| `agy_followup` | 기존 conversation을 이어서 재질문 |
| `agy_sessions` | 활성 세션/작업 목록 및 종료 |
| `agy_cancel` | 실행 중 job 중단 |

### 4.2 `agy_consult` 인터페이스 (핵심)

```jsonc
{
  "name": "agy_consult",
  "input": {
    "mode": "review | verify | derive | literature | design",  // 역할 프리셋 → 프롬프트 템플릿 선택
    "question": "string",              // 필수. 무엇을 판단해 달라는지
    "files": ["src/solver.py:1-120"],  // 선택. 경로 또는 경로:행범위
    "context": "string",               // 선택. 물리 설정, 단위계, 가정, 경계조건 등
    "model": "gemini-3.1-pro-high",    // 기본값
    "effort": "high",
    "session_id": "string",            // 선택. 있으면 --conversation 으로 재개
    "wait_seconds": 45,                // 이 시간까지는 동기 대기 (§5)
    "structured": true                 // mode=verify 시 기본 true
  }
}
```

**반환은 두 가지 형태 중 하나** (하나의 도구로 동기/비동기를 모두 처리 — 도구 개수를 늘리지 않기 위함):

```jsonc
// (a) 시간 내 완료
{ "status": "completed", "session_id": "...", "job_id": "...",
  "verdict": { ... },            // structured=true 인 경우
  "response": "...",             // 산문 응답
  "usage": { "total_tokens": 21440 }, "elapsed_s": 47.2 }

// (b) 아직 실행 중
{ "status": "running", "job_id": "j-3", "elapsed_s": 120,
  "hint": "agy_result 로 조회하시오. 예상 소요 2~5분." }
```

### 4.3 컨텍스트 전달 — 단일 정책 `auto`

커버리지 실측(§2.3-E) 결과 전략 지형이 단순해졌다. **선택지를 셋에서 둘로 줄이고,
그 둘의 전환마저 브리지가 자동으로 처리한다.** 사용자와 소비 세션은 이 결정을 몰라도 된다.

**전략 A — 인라이닝** · 페이로드를 프롬프트에 직접 싣는다.
- 상한 **100,000자** (argv 한계 131,072 B에 안전 마진)
- 실측: 118 KB → 입력 117,765 토큰, 90% 깊이 표식 회수, 왕복 14.9 s
- 강점: **최저 지연**, 그리고 검토된 바이트가 무엇인지 브리지가 **정확히 안다**(재현성·감사 가능)

**전략 C — 루프백 HTTP 서빙** · 큐레이션된 파일 집합과 인덱스를 노출하고 URL만 준다.
- **크기 상한 사실상 없음**, 권한 부여 불필요
- 실측: 600 KB → 8/8 (45.9 s), 2 MB → 8/8 (100.0 s). **커버리지 손실 없음**
- 약점: 지연이 크기에 비례. 어떤 부분이 실제로 읽혔는지 브리지가 알 수 없다

**폐기 — 전략 B(워크스페이스 위임).**
C가 같은 목적(에이전트 주도 탐색)을 **권한 설정 없이** 달성한다. B를 유지하면
`permissions.allow` 문법 확인, README 사전 작업 절, 설정 노브, 미검증 경로가 전부 따라온다.
얻는 것이 없으므로 설계에서 **완전히 제거**한다. → §9 README 사전 작업이 그만큼 짧아진다.

**전환 정책 (`context.strategy = "auto"`, 기본값)**

```
payload ≤ 100,000자  → A (인라이닝)
payload >  100,000자  → C (서빙)로 자동 전환, 도구 결과에 전환 사실과 사유를 명시
mode ∈ {review, verify} 이고 파일이 여러 개  → 검토 대상은 A, 주변 자료는 C  (혼합)
```

혼합이 기본 형태다. 검토 대상 파일은 A로 전문을 실어 **무엇이 검토됐는지 확정**하고,
주변 모듈·문헌·로그는 C로 URL만 준다. 한 호출에서 병행한다.

| | A · 인라이닝 | C · 서빙 |
|---|---|---|
| 크기 한계 | 100 KB | 없음 (2 MB 실측) |
| 커버리지 | 보장 | **보장** (8/8 실측) |
| 지연 | 최저 (~15 s) | 크기 비례 (600 KB 46 s, 2 MB 100 s) |
| 읽힌 범위 추적 | 가능 | 불가 |
| 권한 부여 | 불필요 | 불필요 |

**지연이 곧 설계 제약이다.** 2 MB에 100초라는 사실은 §5의 기본값을 바꾼다 —
동기 대기가 예외가 되고 비동기가 정상 경로가 된다.

전략 C의 안전 요건은 §10.1에서 별도로 다룬다.

### 4.4 구조화 판정 스키마 (`mode: verify`)

`--json-schema`로 강제하며, 브리지는 `structured_output` 필드를 그대로 도구 결과에 실어 준다.

```jsonc
{
  "type": "object",
  "required": ["verdict", "summary", "issues", "confidence"],
  "properties": {
    "verdict":    { "enum": ["correct", "minor_issues", "major_issues", "incorrect", "insufficient_context"] },
    "summary":    { "type": "string" },
    "confidence": { "enum": ["low", "medium", "high"] },
    "issues": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["severity", "location", "problem", "evidence", "suggestion"],
        "properties": {
          "severity":   { "enum": ["blocker", "major", "minor", "nit"] },
          "location":   { "type": "string" },   // "src/solver.py:87" 형식 요구
          "problem":    { "type": "string" },
          "evidence":   { "type": "string" },   // 물리 법칙/수식/문헌 근거
          "suggestion": { "type": "string" }
        }
      }
    },
    "assumptions_made": { "type": "array", "items": { "type": "string" } }
  }
}
```

`insufficient_context`와 `assumptions_made`를 넣은 이유: 검증자가 맥락 부족을 **환각으로 메우는 것**이
이 구조에서 가장 흔한 실패다. 부족하면 부족하다고 말할 출구를 스키마에 명시적으로 만들어 둔다.

### 4.5 역할 프리셋 (`mode`)

| mode | 용도 | 기본 모델/effort | 구조화 |
|---|---|---|---|
| `review` | 시뮬레이션 코드의 수치적 타당성 검토 | pro-high / high | 예 |
| `verify` | 특정 주장·수식·결과의 참거짓 판정 | pro-high / high | 예 |
| `derive` | 유도 과정 점검, 대안 정식화 제시 | pro-high / high | 아니오(산문) |
| `literature` | 표준 기법·관례·선행연구 확인 | pro-high / medium | 아니오 |
| `design` | 알고리즘/이산화 선택지 비교 | pro-high / high | 아니오 |

최종 프롬프트는 **`_common.md`(역할 규약) + 내장 플레이북 + 프로젝트 오버레이 +
mode별 지시 + 인라이닝된 파일 + 호출별 `context` + 질문** 순으로 조립된다(§8).
어떤 플레이북을 실을지는 `mode`가 결정하며 설정으로 덮어쓸 수 있다.

---

## 5. 실행 모델 (동기 + 비동기 하이브리드)

이것이 이 브리지의 가장 중요한 구현 결정이다.

- MCP 도구 호출은 Claude Code를 **블로킹**한다. `gemini-3.1-pro-high` 리뷰는 수 분이 걸릴 수 있고
  `--print-timeout` 기본값도 5분이다.
- 따라서 `agy_consult`는 서브프로세스를 **분리 실행(detached)** 하고 job 레지스트리에 등록한 뒤,
  `wait_seconds`까지만 결과를 기다린다. 넘어가면 즉시 job 핸들을 반환한다.
- Claude는 그 사이 다른 작업(테스트 작성, 다른 모듈 구현)을 계속하다가 `agy_result`로 회수한다.

**`wait_seconds` 기본값을 120 → 45로 내린다.** 실측 지연 분포가 근거다.

| 작업 | 실측 지연 | 45 s 동기 창 |
|---|---|---|
| 소형 인라이닝 (~118 KB) | 14.9 s | **안에 들어옴** |
| 600 KB 서빙 | 45.9 s | 경계 |
| 2 MB 서빙 | 100.0 s | 초과 → job 핸들 |

짧은 질의는 어차피 15초 안에 끝나므로 120초 창은 아무 이득이 없고,
긴 작업에서는 Claude를 2분간 묶어 둘 뿐이다. 45초는 "빠른 건 그냥 받고,
느린 건 일찍 놓아준다"는 경계다. **비동기가 예외가 아니라 정상 경로다.**

```
job 상태 전이:  queued → running → completed | failed | timeout | cancelled
```

job 레코드는 `~/.cache/claude-agy-bridge/<프로젝트 해시>/jobs/<job_id>.json`에 영속화한다(§7.4).
MCP 서버가 재시작되어도 진행 중이던 결과를 잃지 않고, 여러 프로젝트에서 동시에 써도 섞이지 않는다.

**타임아웃 계층**
1. `wait_seconds` (기본 45s) — 동기 대기 한계
2. `--print-timeout` (기본 600s로 상향 설정) — agy 자체 한계
3. 하드 킬 (기본 900s) — 브리지가 프로세스 그룹을 종료

---

## 6. 세션 관리

- `conversation_id`를 브리지가 논리적 `session_id`(예: `sess-ns-scheme-01`)에 매핑한다.
- 동일 주제의 연속 질문은 **반드시 같은 세션으로 재개**한다 — 캐시 히트로 비용이 크게 줄고(§2.2),
  검증자가 앞선 논의를 기억한다.
- 세션 메타(주제, 생성 시각, 누적 토큰, 턴 수)를 `~/.cache/claude-agy-bridge/<프로젝트 해시>/sessions.json`에 기록.
- `agy_sessions`가 이 목록을 반환하므로, Claude가 "이전에 이 solver에 대해 물어본 세션"을 재사용할 수 있다.

---

## 7. 구현 스택과 배포 모델

### 7.1 스택 (확정)

**Python 3.14 + uv + 공식 `mcp` SDK(FastMCP)**

- `mise.toml`로 런타임이 재현 가능하게 고정되어 있다 (Python 3.14.6, uv 0.11.29).
- FastMCP를 쓰면 프로토콜 세부(핸드셰이크, 도구 스키마 광고)를 직접 다루지 않는다.
- **Node는 사용하지 않는다.** MCP stdio 서버에 불필요하고, 이식 시 런타임 표면이 하나 줄어든다.
  → `mise.toml`에서 `node = "latest"` 제거 권장 (Phase 0).
- 외부 의존성은 `mcp` 하나로 제한한다. 나머지는 표준 라이브러리(`asyncio`, `subprocess`, `json`, `tomllib`)로 해결한다.
  의존성이 적을수록 대상 프로젝트에 설치할 때 충돌 가능성이 낮다.

### 7.2 두 개의 저장소를 구분한다

| | 개발 저장소 (본 저장소) | 대상 저장소 (양자화학 공정 시뮬레이터) |
|---|---|---|
| 역할 | 브리지 소스, 테스트, 문서 | 브리지를 **소비**하는 곳 |
| 브리지 코드 | 있음 | **없음** (설치된 실행파일을 참조만) |
| 설정 | 개발용 기본값 | `.agy-bridge.toml` (도메인 프리셋, 모델, 상한) |
| 상태(job/session) | 프로젝트별 캐시 디렉터리에 분리 저장 | 동일 |

**절대 규칙: 브리지 코드는 자신의 설치 경로 밖 어떤 경로도 하드코딩하지 않는다.**

### 7.3 설치 방식

**확정: `uv tool install`로 전역 실행파일 설치** (로컬 경로 기준)

```bash
uv tool install --from /path/to/claude-agy-bridge agy-bridge
#  → ~/.local/bin/agy-bridge 생성. 대상 저장소는 이 이름만 참조한다.
```

대상 저장소의 `.mcp.json` (경로 하드코딩 없음):

```jsonc
{
  "mcpServers": {
    "agy": {
      "command": "agy-bridge",
      "args": ["serve"],
      "env": { "AGY_BRIDGE_PROFILE": "quantum-chemistry" }
    }
  }
}
```

부트스트랩 서브커맨드를 함께 제공한다:

```bash
agy-bridge init --target /path/to/target-repo --profile quantum-chemistry
#  → 대상 저장소에 .mcp.json 항목과 .agy-bridge.toml 생성, agy 바이너리 존재 여부 점검,
#    스모크 호출 1회로 인증·응답을 검증한 뒤 결과를 출력한다.
```

업데이트는 재설치(`uv tool install --force`)로 처리한다.
(원격 저장소 + `uvx --from git+<url>` 방식은 다른 머신으로 확장할 필요가 생기면 그때 도입한다. 지금은 불필요한 복잡도.)

### 7.4 경로·상태·설정 규칙

- **프로젝트 루트**: MCP 서버가 기동된 CWD를 기준으로 `.git`까지 거슬러 올라가 판정.
  `AGY_BRIDGE_PROJECT_ROOT`로 명시 오버라이드 가능.
- **상태 저장**: `~/.cache/claude-agy-bridge/<project-root의 안정 해시>/{jobs,sessions}.json`
  → 여러 프로젝트에서 동시에 써도 job/session이 섞이지 않는다.
- **agy 바이너리**: `AGY_BIN` → 없으면 `PATH`에서 탐색 → 없으면 **기동 시점에 명확히 실패**
  (도구 호출 시점에 실패하면 원인 파악이 어렵다).
- **설정 우선순위**: 도구 호출 인자 > 대상 저장소 `.agy-bridge.toml` > 환경변수 > 내장 기본값.

`.agy-bridge.toml` 예시 (대상 저장소에 위치):

```toml
model  = "gemini-3.1-pro-high"
effort = "high"

[playbooks]                        # §8. 스택이 아니라 관심사 단위로 고른다
enabled     = ["units-and-scales", "assumption-validity", "uncertainty-propagation"]
overlay_dir = ".agy-bridge/playbooks"   # 프로젝트 고유 검증 항목이 들어가는 자리

[limits]
max_inline_chars  = 100000         # 이 값이 A→C 전환 임계값 (argv 한계 131,072B 대비 마진)
wait_seconds      = 45             # 실측 지연 분포 기반 (§5)
print_timeout     = 600
daily_call_budget = 60             # 초과 시 도구가 거부하고 사유를 반환

[context]
strategy = "auto"                  # "auto"(기본) | "inline" | "serve" — 보통 건드릴 일이 없다
# 서빙 파라미터는 노출하지 않는다: 루프백 고정, 임의 고포트, 1회용 경로 토큰,
# 서버 수명 = job 수명 (§10.1). 설정으로 완화할 수 없게 코드에 고정한다.

[context]
deny_globs = [".env*", "*_key*", "*token*", "*.pem", "*.chk", "*.wfn"]
```

### 7.5 저장소 구조

```
claude-agy-bridge/
├── mise.toml                     # python + uv (node 제거)
├── pyproject.toml                # [project.scripts] agy-bridge = "agy_bridge.cli:main"
├── README.md                     # ★ 핸드오프 문서 (§9). 대상 세션이 읽는 유일한 입구
├── docs/plan.md                  # 본 문서
├── src/agy_bridge/
│   ├── cli.py                    # serve / init / doctor / budget 서브커맨드
│   ├── server.py                 # MCP 진입점, 도구 등록 + 도구 설명문(★자기설명성)
│   ├── runner.py                 # agy 서브프로세스 실행 + JSON 파싱 + 실패 승격
│   ├── jobs.py                   # 비동기 job 레지스트리 (프로젝트별 영속화)
│   ├── sessions.py               # session_id ↔ conversation_id
│   ├── context.py                # 파일 인라이닝, 행범위 슬라이싱, deny_globs, 크기 상한
│   ├── serve.py                  # 전략 C: 루프백 임시 HTTP 서버 + 1회용 토큰 경로
│   ├── config.py                 # 4단 우선순위 설정 해석, 프로젝트 루트 판정
│   ├── schemas.py                # --json-schema 페이로드
│   └── playbooks/                # ★ 검증 플레이북 (§8). 스택 무관, 관심사 단위
│       ├── _common.md            # 역할·금지사항·불확실성 표기 규약
│       ├── units-and-scales.md
│       ├── assumption-validity.md
│       ├── conservation-and-balance.md
│       ├── uncertainty-propagation.md
│       ├── numerics.md
│       ├── derivation.md
│       └── data-provenance.md
└── tests/
    ├── test_runner_failure_modes.py   # §2.3(A) 조용한 실패 회귀 테스트
    ├── test_context_inlining.py
    └── test_portability.py            # 임의 CWD/저장소에서 기동·경로 격리 검증
```

---

## 8. 과학 능력 구성 — 프롬프트 내장 플레이북

브리지만으로는 "과학적 조언"이 되지 않는다. agy 쪽에 도메인 검증 절차를 심어야 한다.
문제는 **어디에 심는가**이다.

### 8.1 배치 전략 결정

| 방식 | 문제점 |
|---|---|
| 본 저장소 `.agents/skills/` | 대상 프로젝트에서 실행하면 **로드되지 않음** (§2.3(C)) |
| 대상 저장소 `.agents/skills/` | 대상 저장소를 오염시키고, 브리지 업데이트 시 동기화가 깨진다 |
| 전역 `~/.gemini/config/skills/` | 머신 상태에 숨는다. 브리지 버전과 스킬 버전이 어긋나도 알 수 없다 |

**채택: 플레이북을 패키지 안에 두고, 브리지가 프롬프트에 직접 주입한다.**

- 플레이북은 `src/agy_bridge/playbooks/<profile>/<name>.md`로 **브리지와 함께 버전 관리**된다.
- `profile`(설정) × `mode`(호출 인자)로 어떤 플레이북을 실을지 결정론적으로 정해진다.
- agy 쪽에 **설치할 것이 아무것도 없다.** 이식성 요구사항(§1)을 그대로 만족한다.
- 비용: 호출당 수 KB 추가. 기본 17k 토큰 대비 무시 가능하며, 어차피 mode를 명시적으로 고르므로
  progressive disclosure의 이점이 크지 않다.
- 탈출구: 플레이북 총량이 커지면(경험칙 8k자 초과) `agy-bridge init --install-skills`로
  `~/.gemini/config/skills/`에 설치하고 프롬프트에서는 이름만 참조하는 모드로 전환한다.

### 8.2 실행 CWD 격리

대상 저장소를 CWD로 삼으면 agy가 그 저장소의 `AGENTS.md`/`GEMINI.md`를 상속한다.
이 파일들은 대개 **구현자에게 주는 지시**이며 검토자 역할과 충돌한다.

→ 기본값은 브리지가 관리하는 **중립 스크래치 디렉터리**를 CWD로 사용한다.
→ 대상 저장소 규칙을 일부러 상속시키려면 `inherit_workspace: true`를 명시한다(전략 B와 함께).

### 8.3 공통 규약 (`_common.md`, 모든 호출에 주입)

- 역할: 구현자가 아니라 **검토자**. 파일 수정·명령 실행을 시도하지 말 것.
- 모든 지적에 물리 법칙, 수식, 또는 표준 관행 근거를 붙일 것.
- **계산된 값 / 문헌값 / 추정값을 구분해 표기**할 것.
- 단위계와 기준 상태를 항상 명시적으로 확인할 것.
- 맥락이 부족하면 메우지 말고 `insufficient_context`로 답하고, 무엇이 더 필요한지 명시할 것.

### 8.4 무엇을 브리지에 담고 무엇을 담지 않는가

플레이북을 스택별(PySCF용, ORCA용, Cantera용…)로 짜면 안 된다. **적용 프로젝트마다 달라지므로
브리지에 구우면 이식 가능한 제품이 아니게 된다.** 경계는 다음과 같이 긋는다.

| 성격 | 예 | 어디에 두는가 |
|---|---|---|
| **도메인 불변식** | 기준 상태 일관성, 단위 정합, 오차 전파, 보존 법칙, 근사의 적용 범위 | **브리지 내장** — 프로젝트가 바뀌어도 변하지 않는다 |
| **패키지 관례** | ORCA 입력 키워드, PySCF 수렴 설정 이름, Cantera 열역학 파일 형식 | **어디에도 두지 않는다** — 검토자 모델이 이미 안다 |
| **프로젝트 규약** | 이 저장소의 단위계, 자체 자료구조, 팀 내부 검증 기준 | **대상 저장소 오버레이**(§8.5) |

핵심은 플레이북이 *"무엇을 확인하라"*를 적는 것이지 *"패키지 X에서 그것을 뭐라고 부르는가"*를
적는 게 아니라는 점이다. 후자를 적기 시작하면 유지보수 불가능해지고, 모델이 이미 아는 것을
토큰을 써 가며 다시 알려주는 낭비가 된다.

### 8.5 내장 플레이북 (스택 무관)

관심사 단위로 구성하며 어떤 계산과학 프로젝트에도 적용된다.

| 플레이북 | 검사 항목 |
|---|---|
| `units-and-scales` | 단위 정합성, 기준 상태(표준 상태·기준 온도·압력) 일관성, 분자당 ↔ 몰당, 무차원화 |
| `assumption-validity` | 적용된 근사가 해당 조건에서 유효한가 (이상성 가정, 조화 근사, 정상상태, 선형화 등) |
| `conservation-and-balance` | 물질·에너지·전하 수지 폐합, 보존량 드리프트 |
| `uncertainty-propagation` | 상류 오차가 하류 예측으로 전파·증폭되는 경로, 감도 분석 필요성 |
| `numerics` | 이산화·수렴 기준·강성·조건수, 수렴성 검증 계획의 타당성 |
| `derivation` | 차원 해석, 극한·점근 거동, 경계·초기 조건 정합, 부호 규약 |
| `data-provenance` | 계산값 / 문헌값 / 추정값의 출처와 구분, 하드코딩된 상수의 근거 |

`mode`가 이 중 무엇을 실을지 결정하며, 매핑은 설정으로 덮어쓸 수 있다.

**`uncertainty-propagation`은 별도로 강조한다.** 상류(예: 양자화학 계산)의 작은 오차가
하류(예: 공정 평형·수율)에서 지수적으로 증폭되는 구조가 흔하기 때문이다.
298 K에서 ΔG가 1 kcal·mol⁻¹ 어긋나면 평형상수는 약 **5.4배** 틀어진다
(exp(4.184 / (8.314 × 298 × 10⁻³)) ≈ 5.4). "계산은 맞는데 예측은 무의미한" 상태는
코드 리뷰로 절대 잡히지 않으므로, **오차 막대 없이 하류로 넘어가는 물리량을 지적하도록** 명시한다.

### 8.6 오버레이 — 프로젝트 고유 지식이 들어가는 자리

브리지를 고치지 않고 대상 저장소가 자신의 검증 항목을 추가하는 확장점이다.

```
<대상 저장소>/.agy-bridge/playbooks/*.md   # 있으면 자동 발견, 내장 플레이북 뒤에 덧붙여 주입
```

- `agy-bridge init`이 주석 달린 빈 템플릿과 작성 지침을 생성한다.
- 대상 저장소의 VCS에 들어가므로 팀이 공유하고, 프로젝트가 성숙하며 함께 자란다.
- 브리지 업데이트가 오버레이를 덮어쓰지 않는다 (버전 경계가 명확).

```toml
# .agy-bridge.toml (대상 저장소)
[playbooks]
enabled     = ["units-and-scales", "assumption-validity", "uncertainty-propagation"]
overlay_dir = ".agy-bridge/playbooks"
```

### 8.7 호출 단위 컨텍스트

이론 수준, 기저함수, 온도·압력 조건, 반응계 성격처럼 **호출마다 달라지는 정보**는
플레이북이 아니라 `agy_consult`의 `context` 인자로 전달한다.
이 세 층(내장 불변식 → 프로젝트 오버레이 → 호출별 컨텍스트)이 변화 빈도에 따라 분리되어 있고,
이것이 브리지를 특정 프로젝트에 묶이지 않게 하는 구조다.

---

## 9. 핸드오프 설계 (다른 세션이 쓰게 만들기)

이 도구를 실제로 사용할 Claude 세션은 **본 설계 대화를 보지 못한다.** 따라서 지식은
사람의 설명이 아니라 **산출물 자체에** 실려 있어야 한다. 세 가지 매개체를 쓴다.

**(1) MCP 도구 설명문 — 가장 중요**
도구 설명은 소비 세션이 반드시 읽는 유일한 텍스트다. 각 도구 설명에 다음을 담는다:
- 언제 쓰는가 / 언제 쓰지 말아야 하는가
- **비용 경고**: 호출당 최소 ~10초 + 17k 토큰, `review`/`verify`는 수 분 소요
- 반환값이 **자문 의견이지 사실이 아님** (판정을 그대로 코드에 반영하기 전 근거를 검토할 것)
- 같은 주제는 `session_id` 재사용이 저렴하고 정확함
- `running` 반환 시 대기하지 말고 다른 작업을 진행할 것

**(2) `README.md`** — 사전 작업, 설치, `.mcp.json` 등록, `.agy-bridge.toml` 전체 레퍼런스, 실패 대응표.

README의 **"사전 작업(Prerequisites)"** 절은 필수이며 다음을 빠짐없이 적는다.
사전 조건을 코드 기본값 뒤에 숨기지 않고 문서에 드러내는 것이 이 절의 목적이다.

| 항목 | 필수 여부 | 확인 방법 |
|---|---|---|
| `agy` CLI 설치 및 OAuth 인증 | 필수 | `agy models` 가 모델 목록을 출력 |
| `claude` CLI (MCP 클라이언트) | 필수 | — |
| `uv` (브리지 설치용) | 필수 | `uv --version` |
| 브리지 설치 | 필수 | `agy-bridge doctor` 전 항목 통과 |
| 대상 저장소에 `.mcp.json` 등록 | 필수 | `agy-bridge init --target <repo>` 가 수행 |
| agy 권한 설정 | **불필요** | 전략 B 폐기로 사라짐 |
| agy 과학 스킬 팩 (선택) | 선택 | 아래 참조 — 저장소에 벤더링하지 않는다 |

**외부 스킬 팩은 벤더링하지 않는다.**
`google-deepmind/science-skills`(Apache 2.0) 같은 외부 자산은 본 저장소에 복사해 넣지 않고,
각자 로컬에 내려받아 `.agents/plugins/` 아래에 둔다(해당 경로는 `.gitignore` 처리됨).

```bash
git clone --depth 1 https://github.com/google-deepmind/science-skills \
  .agents/plugins/science-skills
```

남의 저장소를 미러링하면 라이선스·귀속·업스트림 동기화 책임이 따라온다.
그리고 **브리지 설계상 필요하지도 않다** — 검증 절차는 §8의 내장 플레이북이 담당하고,
그것은 agy 쪽 설치물에 의존하지 않는다. 이 스킬 팩은 어디까지나 사용자의 로컬 agy 환경을
풍부하게 하는 선택 사항이다.
**선택 항목은 없다.** 전략 B를 폐기(§4.3)하면서 `permissions.allow` 등록 절차가 통째로 사라졌다.
agy 쪽에 사용자가 설정할 것이 하나도 없다는 뜻이며, 이것이 핸드오프에서 가장 큰 이득이다 —
설명해야 할 사전 조건이 적을수록 다른 세션이 실패할 여지도 줄어든다.

> 참고로 `doctor`의 스모크 검사는 `status == "SUCCESS"`가 아니라 **`response`가 비어 있지 않은지**로
> 판정한다. §2.3(A)에서 실측한 함정 때문이다.

**(3) `agy-bridge doctor`** — 자가 진단. "만든 사람에게 물어보기"를 대체한다.
`agy` 바이너리 위치 / OAuth 유효성(스모크 호출 1회) / 프로젝트 루트 판정 결과 /
상태 디렉터리 / 남은 호출 예산 / 플레이북 프로파일을 점검하고, 실패 시 **조치 가능한 문장**으로 출력한다.

**(4) 선택: 대상 저장소 `CLAUDE.md` 스니펫**
`agy-bridge init`이 대상 저장소의 `CLAUDE.md`에 덧붙일 짧은 사용 지침을 제안한다
(예: "새 수치 기법을 커밋하기 전, 열역학량이 공정 모델로 넘어가기 전에는 `agy_consult`로 검증할 것").
자동으로 쓰지 않고 **제안 후 승인**을 받는다.

**핸드오프 완료 판정 기준**:
대상 저장소에서 새 Claude 세션이 `README.md`만 읽고, 사람 개입 없이 `verify` 왕복 1회를 성공시킨다.
이것이 Phase 6의 인수 조건이다.

---

## 10. 보안·안전 정책

- agy는 **읽기 전용 자문역**. `--mode plan`을 기본으로 두어 편집 의도를 차단한다.
- `--dangerously-skip-permissions`는 **사용하지 않는다.** A와 C 모두 권한 부여가 불필요하므로
  이 플래그가 필요한 경로 자체가 설계에 없다.
- 프롬프트에 실리는 파일 내용에 자격증명이 섞이지 않도록, `context.py`에 거부 패턴
  (`.env`, `*_key*`, `*token*`, `*.pem`)을 두고 해당 경로는 인라이닝하지 않는다.
- agy가 반환한 텍스트는 **데이터이지 지시가 아니다.** 브리지는 응답을 도구 결과로 감싸 전달하고,
  Claude는 이를 검토 의견으로만 취급한다 (프롬프트 인젝션 경계).

### 10.1 전략 C(로컬 HTTP 서버)의 안전 요건

로컬 서버는 편리한 만큼 **같은 머신의 모든 프로세스에게 열린 창구**이기도 하다.
소스 코드를 노출하는 경로이므로 다음을 강제한다.

| 요건 | 이유 |
|---|---|
| `127.0.0.1` 바인딩 고정, `0.0.0.0` 금지 | 네트워크 노출 차단. 설정으로도 변경 불가하게 둔다 |
| 임의 고포트 자동 배정 | 고정 포트는 다른 프로세스가 예측·선점할 수 있다 |
| 경로에 **1회용 랜덤 토큰** 포함 | 포트 스캔만으로 목록을 얻지 못하게 한다 |
| **화이트리스트만 노출** — 디렉터리 리스팅 금지 | 브리지가 선정한 파일 외에는 존재 자체를 드러내지 않는다 |
| `deny_globs` 를 서빙 경로에도 동일 적용 | 인라이닝에만 걸고 서빙에서 빠지면 우회로가 된다 |
| GET만 허용, 서버 수명 = job 수명 | 유휴 상태로 남은 서버가 가장 흔한 사고 원인 |
| 종료 보장 (정상·예외·타임아웃 모두) | job이 죽어도 서버가 남으면 안 된다 |

`agy-bridge doctor`가 유휴 서버 잔존 여부를 점검 항목에 포함한다.

---

## 11. 개발 로드맵

| Phase | 내용 | 완료 기준 | 예상 |
|---|---|---|---|
| **0. 환경 정비** | `mise.toml`에서 node 제거, `pyproject.toml` 골격, agy 권한 정책 결정 | `uv run agy-bridge --help` 성공 | 0.5d |
| **1. 최소 브리지** | `agy_consult` 동기 전용 + 전략 A 인라이닝 + `runner.py` 실패 승격 | Claude에서 `agy_consult`로 실제 코드 리뷰 1건 왕복 | 1d |
| **2. 비동기 + 세션** | job 레지스트리, `agy_result`/`agy_cancel`/`agy_followup`, 프로젝트별 영속화 | 5분 걸리는 리뷰를 non-blocking으로 회수 | 1d |
| **3. 구조화 판정** | `--json-schema` 연동, `verify` 모드, `structured_output` 전달 | 열화학 보정을 누락시킨 샘플에서 `major_issues` 판정 획득 | 0.5d |
| **4. 과학 능력 팩** | `_common.md` + 내장 플레이북 7종 + **오버레이 발견 기구** | 자체 제작한 결함 샘플에서 플레이북 유무 간 지적 품질 차이 확인 | 1.5d |
| **5. 대용량 컨텍스트 · 운영성** | **전략 C 서빙 + `auto` 전환 + §10.1 안전 요건**, 비용 계측, 호출 예산, 재시도 | 2 MB 자료 왕복 성공 + 서버 잔존 0 + 비용 리포트 | 1.5d |
| **6. 패키징·핸드오프** | `uv tool install` 검증, `init`/`doctor` 서브커맨드, `README.md`, 이식성 테스트 | **새 세션이 README만으로 대상 저장소에서 왕복 1회 성공** | 1d |

모든 Phase가 대상 저장소와 독립적이다. 도메인 고유 지식은 오버레이(§8.6)로 나중에 주입되므로
대상 프로젝트 정보를 기다릴 필요 없이 0→6을 순서대로 진행한다.

**Phase 1의 최우선 테스트**는 기능이 아니라 §2.3(A) 실패 모드다.
`status=SUCCESS` + 빈 `response`를 반드시 오류로 잡는지 회귀 테스트로 고정한다.
이걸 놓치면 브리지가 "검증 통과"로 오독되는 침묵을 반환하게 되고, 그것이 이 시스템에서
가능한 최악의 버그다.

---

## 12. 사용 시나리오 (대상 프로젝트, Phase 4 완료 시점 기준)

```
[대상 세션의 Claude] DFT로 얻은 ΔG를 평형 전환율 계산에 넣는 모듈을 구현했다. 검증받자.

→ agy_consult(
     mode="verify",
     question="QC에서 얻은 ΔG를 평형상수로 변환해 전환율을 예측하는 이 경로가 타당한가?",
     files=["src/thermo/from_qc.py:1-140", "src/process/equilibrium.py:20-90"],
     context="기상 반응, B3LYP-D3/def2-TZVP, 298 K 1 atm 기준 열보정 적용,
              공정 모델은 이상기체 가정, 전환율은 Kp로부터 직접 계산",
     session_id="sess-qc-coupling-01")

← { status: "running", job_id: "j-7", hint: "예상 2~5분" }
   [Claude는 그동안 테스트 코드 작성 계속]

→ agy_result(job_id="j-7")
← { status: "completed",
    verdict: { verdict: "major_issues", confidence: "high",
      issues: [
        { severity: "blocker", location: "src/thermo/from_qc.py:88",
          problem: "표준 상태 변환이 누락됨",
          evidence: "QC 열보정은 1 atm 기준으로 산출되는데 공정 모델은 농도 기준을 쓴다.
                     Δn ≠ 0인 반응에서 RT·ln(RT/p°) 항이 빠지면 ΔG가 계통적으로 어긋난다",
          suggestion: "Δn을 반영한 기준 상태 변환을 적용" },
        { severity: "major", location: "src/process/equilibrium.py:41",
          problem: "ΔG 불확실성이 전파되지 않음",
          evidence: "298 K에서 ΔG 오차 1 kcal/mol은 K를 약 5.4배 바꾼다. B3LYP-D3의 통상
                     오차를 감안하면 전환율 예측의 신뢰구간을 함께 산출해야 한다",
          suggestion: "ΔG에 ±오차를 부여해 K와 전환율의 구간을 함께 반환" }],
      assumptions_made: ["기상 이상기체 거동 가정", "저진동수 모드 보정은 미적용으로 간주"] } }

[Claude] 두 지점 수정 → agy_followup(session_id="sess-qc-coupling-01") 로 재검증
```

---

## 13. 결정 사항 및 남은 입력

**확정된 결정 (2026-08-07)**

| 항목 | 결정 |
|---|---|
| 런타임 | Python 3.14.6 + uv 0.11.29 (`mise.toml`). **Node 미사용** |
| 적용 대상 | 별도 저장소 — 양자화학 계산 기반 공정 시뮬레이터 |
| 컨텍스트 전략 | **`auto` 단일 정책** — 100 KB 이하 A(인라이닝), 초과 시 C(루프백 서빙) 자동 전환. **전략 B 폐기** |
| 동기 대기 | `wait_seconds` 45초. 비동기가 정상 경로 (§5) |
| 배포 형태 | `uv tool install --from <로컬 경로> agy-bridge`. 업데이트는 `--force` 재설치 |
| 비용 상한 | `daily_call_budget = 60` (일일 호출 상한, 초과 시 도구가 사유와 함께 거부) |
| 플레이북 범위 | **스택 무관·관심사 단위**로 내장. 프로젝트 고유 지식은 오버레이(§8.6)로 분리 |

**남은 입력: 없음.** Phase 0부터 6까지 대상 프로젝트 정보 없이 진행 가능하다.

초기 설계에서는 `quantum-chemistry` 프로파일에 패키지별 점검 항목(ORCA 키워드, PySCF 수렴 설정,
Cantera 열역학 파일 형식 등)을 담으려 했으나 폐기했다. 그것은 **적용 프로젝트마다 달라지므로
브리지에 구우면 이식성이 깨지고**, 동시에 **검토자 모델이 이미 아는 지식**이라 인코딩할 가치도 없다.
브리지는 변하지 않는 것(도메인 불변식)만 담고, 변하는 것은 오버레이와 호출별 `context`로 받는다.

**대상 프로젝트 투입 시점의 작업** (브리지 개발과 무관, 투입 시 1회):
- `agy-bridge init --target <repo>` 실행 → `.mcp.json` 등록, `.agy-bridge.toml`·오버레이 템플릿 생성
- 필요하면 `.agy-bridge/playbooks/`에 그 프로젝트 고유 검증 항목을 추가 (선택, 나중에 해도 됨)
