"""Serve the real dashboard over local HTTP on a temp accounts file, for the browser test. No logins, no network.

Usage: account_ui_serve_fixture.py ACCOUNTS_FILE PORT [ROUTER_MODELS_JSON]
"""

import io
import runpy
import sys
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

ui = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_ui.py"), run_name="account_ui")
accounts, port, models = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) > 3 else None


class Router:
    def open(self, request, timeout=None):
        if (request if isinstance(request, str) else request.full_url).endswith("/v1/models") and models:
            return io.BytesIO(models.read_bytes())
        raise urllib.error.URLError("offline fixture")


board = ui["Dashboard"](accounts, "http://127.0.0.1:9", Path("/etc/ssl/certs/ca-certificates.crt"))
board.opener, board.provider_quota = Router(), dict
server = ThreadingHTTPServer(("127.0.0.1", port), ui["handler"](board))
print(server.server_address[1], flush=True)
server.serve_forever()
