"""전략 C — 루프백 임시 HTTP 서버 (§4.3, §10.1).

안전 요건은 코드에 고정하며 설정으로 완화할 수 없다 (§7.4):
- 127.0.0.1 바인딩 고정 (0.0.0.0 금지)
- 임의 고포트 자동 배정 (고정 포트는 예측·선점 가능)
- 경로에 1회용 랜덤 토큰 — 포트 스캔만으로 목록을 얻지 못한다
- 화이트리스트만 노출, 디렉터리 리스팅 없음
- GET 전용, 서버 수명 = job 수명 (jobs.py가 종결 시 close를 보장)

파일 내용은 생성 시점에 메모리로 스냅샷한다. 요청 시점에 파일시스템을 읽지
않으므로 경로 조작 여지가 없고, 검토된 바이트가 무엇인지도 고정된다.
"""

from __future__ import annotations

import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_BIND_HOST = "127.0.0.1"  # 하드코딩 — 설정 노출 금지 (§10.1)


class ContextServer:
    """큐레이션된 파일 집합을 1회용 토큰 경로로 서빙하는 임시 서버."""

    def __init__(self, files: dict[str, bytes]):
        if not files:
            raise ValueError("서빙할 파일이 없다.")
        self._token = secrets.token_urlsafe(16)
        self._files = dict(files)
        self._files["INDEX"] = self._build_index().encode("utf-8")
        self._closed = False
        self._close_lock = threading.Lock()

        token = self._token
        snapshot = self._files

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self._serve(head=False)

            def do_HEAD(self):
                # HEAD 응답에 본문을 실으면 keep-alive 연결의 다음 응답 프레임이
                # 오염된다 (실측: agy READ_URL이 HEAD 프로브 후 GET을 보냄).
                # 헤더만 반환한다.
                self._serve(head=True)

            def _serve(self, head: bool):
                prefix = f"/{token}/"
                name = self.path[len(prefix):] if self.path.startswith(prefix) else None
                content = snapshot.get(name) if name else None
                if content is None:
                    self._reply(404, b"not found", head=head)
                    return
                self._reply(200, content, head=head)

            def _reply(self, code: int, body: bytes, *, head: bool = False):
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def reject_method(self):
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            # 읽기(GET/HEAD) 외 전부 405 (§10.1)
            do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = reject_method

            def log_message(self, *args):  # stdout은 MCP 채널 — 로그 침묵
                pass

        self._httpd = ThreadingHTTPServer((_BIND_HOST, 0), Handler)
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="ctx-serve"
        )
        self._thread.start()

    def _build_index(self) -> str:
        lines = ["제공 파일 목록 (이름 → 경로 뒤에 붙여 fetch):", ""]
        for name, content in self._files.items():
            lines.append(f"- {name} ({len(content):,} B)")
        return "\n".join(lines)

    @property
    def base_url(self) -> str:
        return f"http://{_BIND_HOST}:{self._port}/{self._token}"

    def url_for(self, name: str) -> str:
        return f"{self.base_url}/{name}"

    @property
    def port(self) -> int:
        return self._port

    def is_serving(self) -> bool:
        return not self._closed

    def close(self) -> None:
        """멱등 종료. 정상·예외·타임아웃 모든 경로에서 호출된다 (§10.1)."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._httpd.shutdown()
        self._httpd.server_close()
