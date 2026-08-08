"""전략 A — 파일 인라이닝 (§4.3): 스펙 파싱, 행범위 슬라이싱, deny_globs, 크기 상한.

인라이닝의 강점은 검토된 바이트가 무엇인지 브리지가 정확히 아는 것이다 (재현성·감사).
그래서 조립된 텍스트와 함께 manifest(파일·행범위·문자 수)를 반환한다.

각 행 앞에 원본 기준 절대 행 번호를 붙인다 — 검토자가 `경로:행` 형식으로
정확히 인용할 수 있어야 하기 때문이다 (§4.4 location 필드).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from agy_bridge.runner import ARGV_PROMPT_LIMIT_BYTES


class ContextError(ValueError):
    """파일 스펙이 잘못됐거나 인라이닝이 거부된 경우."""


class ContextTooLarge(ContextError):
    """인라이닝 합계가 상한을 초과 (inline_files 전용 — auto 정책에서는
    prepare_context가 초과분을 서빙으로 전환하므로 이 오류가 나지 않는다)."""


# "경로" | "경로:행" | "경로:시작-끝". 콜론 뒤가 숫자가 아니면 경로의 일부로 본다.
_SPEC_RE = re.compile(r"^(?P<path>.+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")

# 서빙 파일의 부분 분할 크기. 실측(Phase 5): 단일 2 MB URL을 주면 검토자
# 에이전트가 fetch 대신 셸 검색(권한 거부→침묵)을 시도하는 경향이 있다.
# 유한한 부분 목록을 주면 fetch 반복 계획이 명확해져 이 탈선이 사라진다.
SERVE_CHUNK_BYTES = 400_000

# 인라이닝 예산의 바이트 상한 (§2.3-D). 예산을 문자 수로만 세면 한글(UTF-8
# 3 B/자) 자료가 문자 예산을 통과하고도 argv 한계(바이트)를 넘어, auto 폴백이
# 발동하지 못한 채 호출 전체가 실패한다. 그래서 문자·바이트 예산을 병행하고
# 둘 중 먼저 걸리는 쪽을 적용한다 — ASCII 자료의 기존 동작은 변하지 않는다.
# 헤드룸은 고정 블록(_common + 플레이북 + mode 지시문, 실측 최대 ~4.4 KB)과
# question/context 몫이다. 사용자 입력은 무한정일 수 있으므로 조립 후의
# ensure_prompt_within_argv_limit가 최종 방어선으로 남는다.
ARGV_HEADROOM_BYTES = 8_000


def _budget_bytes(max_chars: int) -> int:
    return min(max_chars * 3, ARGV_PROMPT_LIMIT_BYTES - ARGV_HEADROOM_BYTES)


def parse_spec(spec: str) -> tuple[str, int | None, int | None]:
    match = _SPEC_RE.match(spec.strip())
    if not match or not match.group("path"):
        raise ContextError(f"파일 스펙을 해석할 수 없다: {spec!r}")
    start = match.group("start")
    end = match.group("end")
    return (
        match.group("path"),
        int(start) if start else None,
        int(end) if end else (int(start) if start else None),
    )


def _is_denied(path: Path, deny_globs: tuple[str, ...]) -> bool:
    name = path.name
    full = path.as_posix()
    return any(
        fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(full, glob)
        for glob in deny_globs
    )


def _render_spec(
    spec: str, *, project_root: Path, deny_globs: tuple[str, ...]
) -> dict:
    """스펙 하나를 행 번호 붙은 블록으로 렌더링한다. deny_globs는 인라이닝과
    서빙 양쪽에 동일하게 적용된다 (§10.1)."""
    raw_path, start, end = parse_spec(spec)
    path = Path(raw_path)
    abs_path = (path if path.is_absolute() else project_root / path).resolve()

    # project_root 봉쇄 (§10, 리뷰 #3): 호출 인자는 프롬프트 인젝션으로 유도될
    # 수 있는 입력이다. 절대경로·../ 탈출·심링크(resolve 후 비교이므로) 모두
    # 여기서 막는다. 존재 확인보다 먼저 — 루트 밖 파일의 존재 여부조차 답하지
    # 않는다. deny_globs는 루트 안쪽의 자격증명을 거르는 두 번째 방어선이다.
    root = project_root.resolve()
    try:
        display = abs_path.relative_to(root).as_posix()
    except ValueError:
        raise ContextError(
            f"{spec}: project_root({project_root}) 밖의 파일은 전달하지 않는다 "
            "(§10 유출 방지). 저장소 밖 자료가 필요하면 저장소 안으로 복사한 뒤 "
            "지정하라."
        ) from None

    if _is_denied(abs_path, deny_globs):
        raise ContextError(
            f"{spec}: deny_globs에 걸려 전달을 거부한다 (§10 자격증명 보호). "
            f"패턴: {list(deny_globs)}"
        )
    if not abs_path.is_file():
        raise ContextError(f"{spec}: 파일이 없다 (project_root={project_root} 기준)")

    # 하드링크 방어 (§10): 경로 기반 봉쇄는 resolve로 심링크만 정규화한다.
    # 루트 안쪽에 만든 하드링크는 경로가 루트 안이라 봉쇄를 통과하지만 같은
    # inode가 루트 밖 비밀 파일을 가리킬 수 있다. 소스 파일은 사실상 링크 수가
    # 1이므로, 다중 하드링크 파일은 별칭 가능성으로 보고 거부한다.
    if abs_path.stat().st_nlink > 1:
        raise ContextError(
            f"{spec}: 하드링크가 여럿인 파일은 전달하지 않는다 (§10 — 루트 밖 "
            "자료의 별칭일 수 있다). 사본을 만들어 지정하라."
        )

    lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    total_lines = len(lines)

    if start is None:
        start, end = 1, total_lines
    assert end is not None
    if start < 1 or (total_lines and start > total_lines):
        raise ContextError(f"{spec}: 시작 행 {start}이 파일 범위(1-{total_lines}) 밖이다")
    end = min(end, total_lines)
    if end < start:
        raise ContextError(f"{spec}: 행범위가 비었다 ({start}-{end})")

    selected = lines[start - 1 : end]
    width = len(str(end))
    numbered = "\n".join(
        f"{number:>{width}}| {line}"
        for number, line in enumerate(selected, start=start)
    )

    block = (
        f"--- FILE {display} [{start}-{end}행 / 총 {total_lines}행] ---\n{numbered}"
    )
    return {
        "spec": spec,
        "display": display,
        "lines": f"{start}-{end}",
        "block": block,
        "chars": len(block),
        "bytes": len(block.encode("utf-8")),
    }


def inline_files(
    specs: list[str],
    *,
    project_root: Path,
    deny_globs: tuple[str, ...],
    max_chars: int,
) -> tuple[str, list[dict]]:
    """파일 스펙 목록을 전부 인라이닝한다. 상한 초과는 오류다 (전략 A 전용 경로)."""
    blocks: list[str] = []
    manifest: list[dict] = []
    total_chars = 0
    total_bytes = 0
    budget_bytes = _budget_bytes(max_chars)

    for spec in specs:
        rendered = _render_spec(spec, project_root=project_root, deny_globs=deny_globs)
        total_chars += rendered["chars"]
        total_bytes += rendered["bytes"]
        if total_chars > max_chars or total_bytes > budget_bytes:
            raise ContextTooLarge(
                f"인라이닝 합계가 상한(문자 {max_chars:,} / 바이트 {budget_bytes:,})을 "
                f"초과했다 ({spec} 포함 시점). 행범위를 좁히거나 파일 수를 줄여라."
            )
        blocks.append(rendered["block"])
        manifest.append(
            {"file": rendered["display"], "lines": rendered["lines"],
             "chars": rendered["chars"], "bytes": rendered["bytes"]}
        )

    return "\n\n".join(blocks), manifest


@dataclass
class PreparedContext:
    """auto 정책(§4.3)의 산출물: 인라이닝 블록 + 서빙 대상 + 전환 사유."""

    files_block: str
    inline_manifest: list[dict]
    to_serve: dict[str, bytes]        # 서빙 이름 → 렌더링된 바이트
    served_manifest: list[dict]
    strategy: str                     # "inline" | "mixed" | "serve"
    reason: str | None


def _chunk_into(
    to_serve: dict[str, bytes], base_name: str, block: str
) -> list[str]:
    """렌더링된 블록을 행 경계에서 SERVE_CHUNK_BYTES 이하 부분들로 나눠 담는다.
    각 행에 절대 행 번호가 붙어 있으므로 어느 부분을 읽어도 위치가 보존된다."""
    encoded = block.encode("utf-8")
    if len(encoded) <= SERVE_CHUNK_BYTES:
        to_serve[base_name] = encoded
        return [base_name]

    parts: list[list[str]] = [[]]
    size = 0
    for line in block.split("\n"):
        line_size = len(line.encode("utf-8")) + 1
        if size + line_size > SERVE_CHUNK_BYTES and parts[-1]:
            parts.append([])
            size = 0
        parts[-1].append(line)
        size += line_size

    total = len(parts)
    names = []
    for index, chunk in enumerate(parts, start=1):
        name = f"{base_name}__part{index:02d}of{total:02d}"
        names.append(name)
        to_serve[name] = "\n".join(chunk).encode("utf-8")
    return names


def prepare_context(
    specs: list[str],
    *,
    project_root: Path,
    deny_globs: tuple[str, ...],
    max_chars: int,
) -> PreparedContext:
    """auto 전환 (§4.3): 지정 순서대로 인라이닝 예산을 채우고, 넘치는 파일부터는
    루프백 서빙 대상으로 돌린다. files 순서가 곧 우선순위다 — 검토 대상을 앞에
    두라는 규약은 도구 설명문이 소비 세션에 전달한다."""
    rendered = [
        _render_spec(spec, project_root=project_root, deny_globs=deny_globs)
        for spec in specs
    ]

    blocks: list[str] = []
    inline_manifest: list[dict] = []
    to_serve: dict[str, bytes] = {}
    served_manifest: list[dict] = []
    total_chars = 0
    total_bytes = 0
    budget_bytes = _budget_bytes(max_chars)
    overflowed = False

    for item in rendered:
        if (
            not overflowed
            and total_chars + item["chars"] <= max_chars
            and total_bytes + item["bytes"] <= budget_bytes
        ):
            total_chars += item["chars"]
            total_bytes += item["bytes"]
            blocks.append(item["block"])
            inline_manifest.append(
                {"file": item["display"], "lines": item["lines"],
                 "chars": item["chars"], "bytes": item["bytes"],
                 "delivery": "inline"}
            )
        else:
            overflowed = True
            base_name = item["display"].replace("/", "__") + f"__L{item['lines']}"
            names = _chunk_into(to_serve, base_name, item["block"])
            served_manifest.append(
                {"file": item["display"], "lines": item["lines"],
                 "chars": item["chars"], "bytes": item["bytes"],
                 "delivery": "served", "serve_names": names}
            )

    if not to_serve:
        strategy, reason = "inline", None
    elif inline_manifest:
        strategy = "mixed"
        reason = (
            f"인라이닝 예산(문자 {max_chars:,} / 바이트 {budget_bytes:,})을 초과해 "
            f"{len(served_manifest)}개 파일을 루프백 서빙으로 전환 (§4.3 auto)"
        )
    else:
        strategy = "serve"
        reason = (
            f"첫 파일부터 인라이닝 예산(문자 {max_chars:,} / 바이트 {budget_bytes:,})을 "
            "초과해 전체를 루프백 서빙으로 전환 (§4.3 auto)"
        )

    return PreparedContext(
        files_block="\n\n".join(blocks),
        inline_manifest=inline_manifest,
        to_serve=to_serve,
        served_manifest=served_manifest,
        strategy=strategy,
        reason=reason,
    )
