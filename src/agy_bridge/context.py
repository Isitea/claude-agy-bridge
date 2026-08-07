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


class ContextError(ValueError):
    """파일 스펙이 잘못됐거나 인라이닝이 거부된 경우."""


class ContextTooLarge(ContextError):
    """인라이닝 합계가 상한을 초과. Phase 5의 전략 C가 이 경로를 이어받는다."""


# "경로" | "경로:행" | "경로:시작-끝". 콜론 뒤가 숫자가 아니면 경로의 일부로 본다.
_SPEC_RE = re.compile(r"^(?P<path>.+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")

# 서빙 파일의 부분 분할 크기. 실측(Phase 5): 단일 2 MB URL을 주면 검토자
# 에이전트가 fetch 대신 셸 검색(권한 거부→침묵)을 시도하는 경향이 있다.
# 유한한 부분 목록을 주면 fetch 반복 계획이 명확해져 이 탈선이 사라진다.
SERVE_CHUNK_BYTES = 400_000


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

    if _is_denied(abs_path, deny_globs):
        raise ContextError(
            f"{spec}: deny_globs에 걸려 전달을 거부한다 (§10 자격증명 보호). "
            f"패턴: {list(deny_globs)}"
        )
    if not abs_path.is_file():
        raise ContextError(f"{spec}: 파일이 없다 (project_root={project_root} 기준)")

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

    try:
        display = abs_path.relative_to(project_root).as_posix()
    except ValueError:
        display = abs_path.as_posix()

    block = (
        f"--- FILE {display} [{start}-{end}행 / 총 {total_lines}행] ---\n{numbered}"
    )
    return {
        "spec": spec,
        "display": display,
        "lines": f"{start}-{end}",
        "block": block,
        "chars": len(block),
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

    for spec in specs:
        rendered = _render_spec(spec, project_root=project_root, deny_globs=deny_globs)
        total_chars += rendered["chars"]
        if total_chars > max_chars:
            raise ContextTooLarge(
                f"인라이닝 합계가 상한 {max_chars:,}자를 초과했다 ({spec} 포함 시점). "
                "행범위를 좁히거나 파일 수를 줄여라."
            )
        blocks.append(rendered["block"])
        manifest.append(
            {"file": rendered["display"], "lines": rendered["lines"],
             "chars": rendered["chars"]}
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
    overflowed = False

    for item in rendered:
        if not overflowed and total_chars + item["chars"] <= max_chars:
            total_chars += item["chars"]
            blocks.append(item["block"])
            inline_manifest.append(
                {"file": item["display"], "lines": item["lines"],
                 "chars": item["chars"], "delivery": "inline"}
            )
        else:
            overflowed = True
            base_name = item["display"].replace("/", "__") + f"__L{item['lines']}"
            names = _chunk_into(to_serve, base_name, item["block"])
            served_manifest.append(
                {"file": item["display"], "lines": item["lines"],
                 "chars": item["chars"], "delivery": "served",
                 "serve_names": names}
            )

    if not to_serve:
        strategy, reason = "inline", None
    elif inline_manifest:
        strategy = "mixed"
        reason = (
            f"인라이닝 합계가 상한 {max_chars:,}자를 초과해 "
            f"{len(served_manifest)}개 파일을 루프백 서빙으로 전환 (§4.3 auto)"
        )
    else:
        strategy = "serve"
        reason = (
            f"첫 파일부터 상한 {max_chars:,}자를 초과해 전체를 루프백 서빙으로 "
            "전환 (§4.3 auto)"
        )

    return PreparedContext(
        files_block="\n\n".join(blocks),
        inline_manifest=inline_manifest,
        to_serve=to_serve,
        served_manifest=served_manifest,
        strategy=strategy,
        reason=reason,
    )
