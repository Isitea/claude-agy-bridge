# claude-agy-bridge 코드 리뷰

- **대상**: `claude-agy-bridge` v0.2.0 (main 스냅샷)
- **일자**: 2026-08-08
- **범위**: `src/agy_bridge/**`, `tests/**`, `pyproject.toml`, `mise.toml`, `.gitignore`
- **검증**: `pytest` **98개 전부 통과** (Python 3.12.3, `PYTHONPATH=src`)

---

## 0. 총평

설계 품질이 높다. 특히 다음 세 가지는 실측 없이는 나오지 않는 판단이다.

1. **§2.3-A 침묵 실패 승격** — `status=SUCCESS`인데 `response`가 비어 있으면 오류로 올린다.
   이 시스템에서 가능한 최악의 버그("검증 통과"로 오독되는 침묵)를 정면으로 막는다.
2. **재시도 정책의 판별** — 인프라 실패(비정상 종료)만 재시도하고 §2.3-A 빈 응답은 즉시 실패로 승격한다.
   `jobs.py`의 주석 "재시도가 침묵을 두 배로 만든다"가 정확하다.
3. **`serve.py`의 HEAD 처리** — HEAD 응답에 본문을 실으면 keep-alive 연결의 다음 응답 프레임이 오염된다는 것을
   실측으로 잡아 고정했다.

`cancel`에서 프로세스를 죽이기 **전에** 상태를 `cancelled`로 표시해 감시 스레드의 경합을 막는 순서,
`scratch_dir`를 CWD로 써서 대상 저장소의 `AGENTS.md`/`GEMINI.md`를 검토자가 상속하지 않게 한 것,
`--mode plan` + `--disable-slash-commands`로 조립된 프롬프트를 "지시가 아니라 데이터"로 못 박은 것도 좋다.

아래 지적은 잘못된 설계가 아니라 **경계 조건**에 대한 것이다.

---

## 우선순위 요약

| # | 항목 | 심각도 | 성격 |
|---|---|---|---|
| 1 | 인라이닝 예산 char / argv 한계 byte 불일치 | **blocker** | 한글 자료에서 간헐적 호출 실패 |
| 2 | `.claude/logs/*.log` 커밋됨 | **blocker** | 공개 전 필수 (정보 노출) |
| 3 | `files` 경로에 project_root 봉쇄 없음 | major | 자격증명 유출 경로 |
| 4 | `_next_job_id` 프로세스 간 경합 | major | 같은 저장소 다중 세션에서 레코드 덮어씀 |
| 5 | 예산 계측이 실제 스폰 수와 불일치 | minor | 상한의 최대 2배까지 스폰 가능 |
| 6 | `mise.toml` 사내 프록시 제품명 노출 | minor | 공개 전 정리 |
| 7 | `requires-python = ">=3.14"` 과다 제약 | nit | 배포 제약 완화 여지 |
| 8 | `_finalize`의 possibly-unbound | nit | 정적 분석 경고 |

---

## 1. [blocker] 인라이닝 예산은 char, argv 한계는 byte — 한글에서 깨진다

### 문제

- `context.py::_render_spec` → `"chars": len(block)` (유니코드 **코드포인트**)
- `config.py::DEFAULT_MAX_INLINE_CHARS = 100_000` (코드포인트 기준)
- `runner.py::ARGV_PROMPT_LIMIT_BYTES = 130_000` (UTF-8 **바이트**)

한글은 UTF-8에서 3바이트다. 따라서 **인라이닝 예산을 통과한 자료가 argv 한계를 넘을 수 있다.**

추가로 `compose_playbooks_block`의 산출물(고정 블록)은 인라이닝 예산에 **전혀 계산되지 않는다**.
실측: `mode=review` 고정 블록 1,737 chars / **3,661 B**, `mode=verify` 1,729 chars / **3,608 B**.

### 재현

```python
import sys, tempfile; sys.path.insert(0, "src")
from pathlib import Path
from agy_bridge.context import prepare_context
from agy_bridge.prompts import assemble_prompt, compose_playbooks_block
from agy_bridge.runner import ensure_prompt_within_argv_limit, AgyError

root = Path(tempfile.mkdtemp())
line = "# 검토 대상: 상태방정식 선택이 임계점 근방에서 밀도 예측에 미치는 영향을 확인한다"
(root / "design.md").write_text("\n".join([line] * 1200), encoding="utf-8")

prep = prepare_context(["design.md"], project_root=root, deny_globs=(), max_chars=100_000)
print(prep.strategy)   # → "inline"  (예산 통과로 판정)

prompt = assemble_prompt(
    mode="review", question="상태방정식 선택을 검토해 달라.",
    context="초임계 CO2, 320 K, 80 bar.",
    files_block=prep.files_block,
    playbooks_block=compose_playbooks_block("review", project_root=root, overlay_dir="x"),
)
ensure_prompt_within_argv_limit(prompt)   # → AgyError
```

### 실측 결과

```
파일:            57,599 chars / 139,199 B
prepare_context: strategy=inline          ← 100,000자 예산 '통과'로 판정
조립된 프롬프트:  66,922 chars / 150,787 B
→ AgyError: "조립된 프롬프트가 150,787 B로 argv 단일 인자 한계(130,000 B)를 넘는다 (§2.3-D).
             인라이닝 상한이 지켜졌다면 발생할 수 없는 상태다 — 버그로 보고하라."
```

**57,599자**짜리 파일이다. 100,000자 예산의 절반을 조금 넘겼을 뿐인데 실패한다.

### 왜 나쁜가

1. **오류 메시지가 "발생할 수 없는 상태"라고 단언한다.** 실제로 발생하므로 사용자가 자기 설정을 의심하게 된다.
2. **auto 폴백이 발동하지 않는다.** `prepare_context`가 인라이닝으로 판정했으므로 `ContextServer`가 생성되지 않았다.
   크기 초과 시 서빙으로 전환하는 안전장치가 있는데도, 측정 단위가 달라 우회된다. 호출 전체가 실패한다.
3. **간헐적이다.** 한글 주석 40% 섞인 코드(67,899 chars / 95,899 B)는 통과한다.
   문서·주석 밀도에 따라 성공/실패가 갈려 재현이 어렵다.

### 참고 수치

| 자료 성격 | 100,000 chars의 UTF-8 크기 | argv 한계(130,000 B) 대비 |
|---|---|---|
| ASCII 코드 | 100,000 B | 여유 |
| 한글 주석 30% | 약 160,000 B | 초과 |
| 한글 100% (설계 문서) | 300,000 B | 2.3배 초과 |

### 제안

`_render_spec`에서 바이트를 함께 측정하고, `prepare_context`가 바이트 기준으로 예산을 채운다.

```python
# context.py::_render_spec 반환값에 추가
return {
    ...,
    "chars": len(block),
    "bytes": len(block.encode("utf-8")),
}
```

```python
# context.py::prepare_context
# 고정 블록(_common + 플레이북 + mode 지시문 + context + question) 여유를 뺀다.
# 실측 고정 블록 ≈ 3,700 B. 오버레이·context 변동을 감안해 8,000 B 마진.
ARGV_HEADROOM_BYTES = 8_000
budget_bytes = min(max_chars * 3, ARGV_PROMPT_LIMIT_BYTES - ARGV_HEADROOM_BYTES)

for item in rendered:
    if not overflowed and total_bytes + item["bytes"] <= budget_bytes:
        total_bytes += item["bytes"]
        ...
```

- `max_chars * 3`으로 상한을 두면 기존 `max_inline_chars` 설정의 의미가 보존된다(ASCII에서는 사실상 동작 불변).
- 하위 호환을 중시한다면 `[limits] max_inline_bytes`를 별도로 추가하고 **둘 중 먼저 걸리는 쪽**을 적용한다.
- `ARGV_PROMPT_LIMIT_BYTES` 초과 시의 오류 메시지에서 "발생할 수 없는 상태" 문구를 제거하고,
  실제 원인(멀티바이트 자료 + 고정 블록)을 안내하도록 바꾼다.

### 회귀 테스트 제안

```python
def test_multibyte_inline_stays_within_argv_limit(tmp_path):
    """한글 자료도 인라이닝 판정 후 argv 한계를 넘지 않아야 한다 (§2.3-D)."""
    line = "# 반응 엔탈피를 표준 상태 기준으로 환산한다 (단위: kJ/mol)"
    (tmp_path / "ko.md").write_text("\n".join([line] * 2000), encoding="utf-8")
    prep = prepare_context(["ko.md"], project_root=tmp_path, deny_globs=(), max_chars=100_000)
    prompt = assemble_prompt(
        mode="review", question="검토", context="",
        files_block=prep.files_block,
        playbooks_block=compose_playbooks_block("review", project_root=tmp_path, overlay_dir="x"),
    )
    ensure_prompt_within_argv_limit(prompt)   # raise 하지 않아야 한다
    assert prep.strategy in ("mixed", "serve")  # 초과분은 서빙으로 전환되어야 한다
```

---

## 2. [blocker] `.claude/logs/*.log`가 커밋되어 있다

### 문제

```
.claude/logs/resume-<세션>-<타임스탬프>.log   292,591 B
```

`.gitignore`에 `.claude/`가 없다. 내용에 다음이 포함된다.

- 작업자 홈 아래의 절대 경로
- Claude Code 세션 ID
- 활성화된 도구·MCP 커넥터 전체 목록 (외부 서비스 커넥터 포함)
- 세션 트랜스크립트 본문

README가 `raw.githubusercontent.com` 공개 설치 URL을 전제하고 있으므로(현재는 404 안내가 있으나 공개 예정으로 보임),
**공개 전에 히스토리에서 제거**해야 한다. 단순 삭제 커밋으로는 히스토리에 남는다.

### 제안

```gitignore
# Claude Code 로컬 산출물
.claude/
!.claude/settings.json      # 공유할 항목이 있으면 예외로 명시
```

이미 커밋된 이력은 `git filter-repo` 또는 `git filter-branch`로 제거한다.

```bash
git filter-repo --path .claude/logs --invert-paths
```

---

## 3. [major] `files` 인자에 project_root 봉쇄가 없다

### 문제

`context.py::_render_spec`:

```python
abs_path = (path if path.is_absolute() else project_root / path).resolve()

if _is_denied(abs_path, deny_globs):
    raise ContextError(...)
if not abs_path.is_file():
    raise ContextError(...)
```

- 절대경로를 그대로 받는다.
- `../`를 통한 상위 탈출을 막지 않는다.
- `resolve()`로 심볼릭 링크를 따라간 뒤에도 저장소 내부인지 확인하지 않는다.
- 방어선은 `deny_globs` 하나뿐이다.

기본 deny 목록 `(".env*", "*_key*", "*token*", "*.pem", "*.chk", "*.wfn")`에
다음은 **걸리지 않는다**:

```
~/.ssh/id_rsa
~/.ssh/id_ed25519
~/.aws/credentials
~/.config/gh/hosts.yml
~/.netrc
~/.claude/.credentials.json
```

### 위협 모델

호출자는 Claude Code 세션이고, 그 세션은 대상 저장소의 파일을 읽는다.
저장소에 프롬프트 인젝션이 포함된 파일이 있으면 Claude가
`agy_consult(files=["~/.ssh/id_rsa"])`를 호출하도록 유도될 수 있다.
브리지는 이를 인라이닝해 agy 서브프로세스로 넘기고, 내용은 외부 모델로 전송된다.

`--mode plan`, `--disable-slash-commands`로 **agy 쪽** 인젝션은 잘 막았으나,
**브리지로 들어오는 인자**는 신뢰하고 있다.

### 제안

deny-list는 유지하고, 봉쇄를 추가한다. 허용 경계를 좁히는 쪽이 금지 목록을 늘리는 쪽보다 강하다.

```python
# context.py::_render_spec, _is_denied 검사 직전
try:
    abs_path.relative_to(project_root)
except ValueError:
    raise ContextError(
        f"{spec}: project_root({project_root}) 밖의 파일은 전달하지 않는다 (§10). "
        "저장소 밖 자료가 필요하면 저장소 안으로 복사한 뒤 지정하라."
    )
```

`project_root` 자체도 `.resolve()`한 값과 비교해야 심볼릭 링크 우회를 막는다.

문서(README §10, docs/plan.md)에 "검토 대상은 저장소 내부로 제한된다"를 명시하면
소비 세션의 기대도 함께 맞출 수 있다.

---

## 4. [major] `_next_job_id`가 프로세스 간 경합에 취약하다

### 문제

```python
def _next_job_id(self) -> str:
    highest = 0
    for entry in self._jobs_dir.iterdir():
        match = _JOB_FILE_RE.match(entry.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"j-{highest + 1}"
```

`self._lock`은 **프로세스 내부** 락이다. 그런데 `state_dir`는 `project_root`의 해시로 결정되므로,
같은 저장소에서 Claude Code 세션을 두 개 띄우면 MCP 서버도 두 개 뜨고
**같은 `jobs/` 디렉터리를 공유**한다.

동시에 스폰하면 두 서버가 같은 `j-N`을 할당하고, `_persist`가 서로를 덮어쓴다.
한쪽 job은 추적 불가능해지고, `agy_result`가 다른 job의 결과를 반환할 수 있다.

같은 저장소에서 여러 세션을 병렬로 굴리는 것은 일반적인 사용 패턴이므로 실현 가능성이 낮지 않다.

### 제안 (택1)

**A. 원자적 생성으로 충돌 감지** — 순번 가독성 유지

```python
def _next_job_id(self) -> str:
    highest = 0
    for entry in self._jobs_dir.iterdir():
        match = _JOB_FILE_RE.match(entry.name)
        if match:
            highest = max(highest, int(match.group(1)))
    candidate = highest + 1
    while True:
        path = self._jobs_dir / f"j-{candidate}.json"
        try:
            # O_EXCL로 선점 — 다른 프로세스가 이미 잡았으면 FileExistsError
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            candidate += 1
            continue
        os.close(fd)
        return f"j-{candidate}"
```

`_persist`가 곧바로 덮어쓰므로 빈 파일이 남지 않는다.

**B. 접미사 부여** — 더 단순

```python
return f"j-{highest + 1}-{secrets.token_hex(3)}"
```

`_JOB_FILE_RE`와 `list_jobs`의 정렬 기준을 함께 수정해야 한다.

### 관련: `sessions.json`의 lost update

같은 구조다. `_save`가 `tmp.replace()`를 쓰므로 **파일 손상은 없지만**,
A가 읽고 → B가 읽고 → A가 쓰고 → B가 쓰면 A의 갱신이 사라진다.

세션 메타 대부분은 통계 성격이라 치명적이지 않으나,
`conversation_id`가 유실되면 세션 연속성이 끊어진다(캐시 미스 + 컨텍스트 손실).

`fcntl.flock`으로 read-modify-write 구간 전체를 감싸는 것이 확실하다.

```python
import fcntl

@contextlib.contextmanager
def _exclusive(self):
    lock_path = self._path.with_suffix(".lock")
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
```

`ledger.jsonl`은 `open(..., "a")` + 작은 엔트리라 Linux에서 PIPE_BUF 이하 쓰기는
사실상 원자적이므로 현 상태로도 크게 문제되지 않는다.

---

## 5. [minor] 예산 계측이 실제 스폰 수와 어긋난다

### 5-1. 확인과 기록 사이에 스폰이 있다

`server.py::_start_and_wait`:

```python
ledger.check_budget(config.daily_call_budget)   # ① 확인
...
record = registry.start(...)                     # ② 스폰 (비용 발생)
...
ledger.record_start(record.job_id, ...)          # ③ 기록
```

MCP 도구는 `anyio.to_thread.run_sync`로 실행되므로 동시 호출이 가능하다.
두 호출이 ①을 모두 통과한 뒤 각각 ②로 진행하면 상한을 넘긴다.

**제안**: `record_start`를 `registry.start` **앞**으로 옮기고,
스폰이 실패하면 보정 엔트리(`event: "spawn_failed"`)를 남긴다.
`calls_today`가 `start` 이벤트만 세므로, 보정 엔트리를 빼는 로직도 함께 넣는다.

### 5-2. 재시도가 원장에 기록되지 않는다

`jobs.py::_finalize`는 `MAX_ATTEMPTS`(2)까지 `_spawn`을 다시 호출하지만
`record_start`를 다시 부르지 않는다.

- agy 프로세스는 2회 떴는데 원장에는 1회로 남는다.
- `daily_call_budget = 60`이 실제로는 **최대 120회 스폰**을 허용한다.
- `report()`의 `total_tokens`도 그만큼 과소집계된다.

**제안**: `on_complete` 훅과 별도로, 재시도 스폰 시점에 원장에
`{"event": "start", "job_id": ..., "attempt": 2}`를 추가한다.

### 5-3. `attempts`가 소비 세션에 노출되지 않는다

`JobRecord.attempts`는 영속화되지만 `_job_payload`에 실리지 않는다.
소비 세션은 재시도가 있었는지 알 수 없다.
비용 인식과 신뢰도 판단 양쪽에 관련되므로 `completed` 페이로드에 포함할 가치가 있다.

```python
if record.attempts > 1:
    payload["attempts"] = record.attempts
    payload["attempts_note"] = "agy 비정상 종료로 재시도되었다. 토큰 비용이 중복 발생했다."
```

---

## 6. [minor] `mise.toml`에 사내 프록시 제품명이 노출되어 있다

```toml
# 사내 프록시(<제품명>)가 github.com을 MITM한다. ...
UV_SYSTEM_CERTS = "1"
```

기술적 해법은 정확하다(인증서 검증을 끄지 않고 시스템 CA를 신뢰 목록에 포함).
다만 공개 저장소에서는 고용주의 보안 스택과 MITM 정책을 드러낸다.

**제안**: 제품명만 제거하고 일반화한다.

```toml
# TLS를 가로채는 프록시 환경에서 uv의 번들 webpki 루트만으로는 CPython 다운로드가
# `invalid peer certificate: UnknownIssuer`로 실패한다. 시스템 CA 저장소를
# 신뢰 목록에 포함시켜 해결한다 (검증을 끄는 것이 아니다).
UV_SYSTEM_CERTS = "1"
```

---

## 7. [nit] `requires-python = ">=3.14"`가 과하다

`pytest` **98개 전부가 Python 3.12.3에서 통과**했다.
표준 라이브러리 의존 중 가장 높은 요구는 `tomllib`(3.11+)로 보인다.

3.14를 고정하면 배포 대상 머신마다 3.14를 갖춰야 한다.
`>=3.11`로 낮추면 배포 제약이 크게 준다.

`mise.toml`의 개발 환경은 3.14로 유지하되, `requires-python`만 완화하는 것이 합리적이다.
CI에서 3.11/3.12/3.14 매트릭스를 돌려 확인하면 안전하다.

---

## 8. [nit] `_finalize`의 possibly-unbound

`jobs.py::_finalize`에서 `server`, `event`는 `if retry_process is None:` 블록 안에서만 대입되고
블록 밖에서 읽힌다. 조기 `return` 덕분에 **논리적으로는 안전**하나,
ruff/mypy가 `possibly-unbound`로 보고한다.

```python
# _finalize 상단
server = None
event: threading.Event | None = None
```

를 추가하면 정적 분석 경고가 사라지고, 이후 리팩터링 시의 실수도 막는다.

---

## 9. 참고 사항 (수정 대상 아님)

- **서빙 콘텐츠 메모리 상주**: `ContextServer`가 파일을 생성 시점에 메모리로 스냅샷한다.
  경로 조작 여지를 없애는 의도된 설계이므로 문제는 아니나,
  2 MB급 자료 여러 건이 동시에 돌면 그만큼 RSS가 늘어난다.
  동시 job 수 상한을 도입할 때 고려 요소.

- **`--print-timeout` 포맷**: `f"{config.print_timeout}s"`로 Go duration 문자열을 만든다.
  현재 agy에서 동작하는 것으로 보이나, CLI 업데이트 시 회귀 가능성이 있는 지점이므로
  `doctor`에서 한 번 검증하는 것도 방법이다.

- **기동 지연**: README는 고정비를 "프로세스 기동 ~10초"로 기술한다.
  최초 인증 왕복인지 매 호출 비용인지에 따라 대응이 갈린다.

  ```bash
  time agy -p "hi" --output-format json    # 1회차
  time agy -p "hi" --output-format json    # 2회차
  ```

  2회차가 유의미하게 빠르면 인증 캐시가 존재하는 것이므로,
  브리지 기동 시 백그라운드 워밍 호출 1회로 첫 사용자 호출의 체감 지연을 없앨 수 있다.
  차이가 없다면 `agy -p`가 프롬프트를 argv로 받는 구조상
  "인증만 하고 대기하는" 워밍 풀은 성립하지 않으므로, 현재의 `--conversation` 재개
  (프롬프트 캐시 히트)가 사실상 최선이다.

---

## 10. 권장 처리 순서

1. **#2 `.claude/logs` 제거** — 공개 시점 이전 필수, 히스토리 재작성 포함
2. **#1 char/byte 예산** — 현재도 간헐적으로 실패 중일 가능성이 있음, 회귀 테스트 동반
3. **#3 project_root 봉쇄** — 방어선 추가, 문서 반영
4. **#4 job_id 경합 + sessions.json 락**
5. **#5 예산 계측 정합**
6. **#6~#8** — 정리 단계에서 일괄

---

*리뷰는 정적 분석과 로컬 테스트 실행에 근거한다. 실제 `agy` 호출은 수행하지 않았으므로,
agy CLI의 런타임 동작(인증 캐시, `--print-timeout` 포맷 수용 여부 등)에 관한 항목은 검증 대상이다.*
