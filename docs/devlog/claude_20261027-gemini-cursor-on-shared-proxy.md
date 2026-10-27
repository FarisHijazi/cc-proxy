# Gemini and cursor-agent through the shared proxy

Both backends already existed in the router; the shared `--serve` host just
had no gemini server configured.

- **cursor-cli**: the proxy host already had `cursor-agent` signed in, and the
  router listed `cursor-cli`, `cursor-cli-ask` and `cursor-cli-plan`. Nothing
  changed.
- **gemini**: the router defaults to `http://localhost:8100/v1`, and the proxy
  host runs no gemini-web-api. The dev VM already runs it as a user service on
  `0.0.0.0:8100`, with a signed-in Chrome. Writing
  `{"gemini": {"baseUrl": "http://<dev tailnet IP>:8100/v1"}}` to
  `~/.config/claude-code-proxy/config.json` on the proxy host listed the six
  gemini ids immediately. `gemini_base_url()` reads the file per request, so no
  restart was needed.

Verified from a `--via` client with real headless sessions: `--model gemini-3-flash`
and `--model cursor-cli` each answered the requested marker.

Limits: gemini depends on the dev VM being up. cursor-agent runs on the proxy
host, so a remote client's working directory does not exist there and
`workspace::detect` falls back to the router's directory. The run cannot read
the client's files.
