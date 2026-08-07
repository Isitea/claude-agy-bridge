"""전략 A — 파일 인라이닝 (§4.3): 스펙 파싱, 행범위 슬라이싱, deny_globs, 크기 상한.

인라이닝의 강점은 검토된 바이트가 무엇인지 브리지가 정확히 아는 것이다 (재현성·감사).
그래서 조립된 텍스트와 함께 manifest(파일·행범위·문자 수)를 반환한다.

각 행 앞에 원본 기준 절대 행 번호를 붙인다 — 검토자가 `경로:행` 형식으로
정확히 인용할 수 있어야 하기 때문이다 (§4.4 location 필드).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path


class ContextError(ValueError):
    """파일 스펙이 잘못됐거나 인라이닝이 거부된 경우."""


class ContextTooLarge(ContextError):
    """인라이닝 합계가 상한을 초과. Phase 5의 전략 C가 이 경로를 이어받는다."""


# "경로" | "경로:행" | "경로:시작-끝". 콜론 뒤가 숫자가 아니면 경로의 일부로 본다.
_SPEC_RE = re.compile(r"^(?P<path>.+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")


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


def inline_files(
    specs: list[str],
    *,
    project_root: Path,
    deny_globs: tuple[str, ...],
    max_chars: int,
) -> tuple[str, list[dict]]:
    """파일 스펙 목록을 프롬프트 블록으로 조립한다. 반환: (텍스트, manifest)."""
    blocks: list[str] = []
    manifest: list[dict] = []
    total_chars = 0

    for spec in specs:
        raw_path, start, end = parse_spec(spec)
        path = Path(raw_path)
        abs_path = (path if path.is_absolute() else project_root / path).resolve()

        if _is_denied(abs_path, deny_globs):
            raise ContextError(
                f"{spec}: deny_globs에 걸려 인라이닝을 거부한다 (§10 자격증명 보호). "
                f"패턴: {list(deny_globs)}"
            )
        if not abs_path.is_file():
            raise ContextError(
                f"{spec}: 파일이 없다 (project_root={project_root} 기준)"
            )

        lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
        total_lines = len(lines)

        if start is None:
            start, end = 1, total_lines
        assert end is not None
        if start < 1 or (total_lines and start > total_lines):
            raise ContextError(
                f"{spec}: 시작 행 {start}이 파일 범위(1-{total_lines}) 밖이다"
            )
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
            f"--- FILE {display} [{start}-{end}행 / 총 {total_lines}행] ---\n"
            f"{numbered}"
        )
        total_chars += len(block)
        if total_chars > max_chars:
            raise ContextTooLarge(
                f"인라이닝 합계가 상한 {max_chars:,}자를 초과했다 ({spec} 포함 시점). "
                "행범위를 좁히거나 파일 수를 줄여라. "
                "대용량 자동 서빙(전략 C)은 Phase 5에서 제공된다."
            )

        blocks.append(block)
        manifest.append(
            {"file": display, "lines": f"{start}-{end}", "chars": len(block)}
        )

    return "\n\n".join(blocks), manifest
