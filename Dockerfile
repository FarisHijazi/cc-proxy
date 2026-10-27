# cc-proxy --serve in a container: the Rust router plus the Python launcher and mitmproxy.
FROM rust:1-bookworm AS router
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src src
RUN cargo build --release --locked

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /usr/local/bin/
# git: claude-swap is a git dependency. uv also runs the launcher's MCP helper scripts.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app
COPY --from=router /src/target/release/claude-codex /usr/local/bin/claude-codex
COPY pyproject.toml README.md LICENSE /app/
COPY python /app/python
COPY scripts /app/scripts
RUN uv pip install --system --no-cache /app
USER app
WORKDIR /home/app
EXPOSE 18790
ENTRYPOINT ["cc-proxy"]
CMD ["--serve", "--listen-host", "0.0.0.0", "--port", "18790", "--accounts-file", "/config/accounts.json"]
