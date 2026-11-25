FROM --platform=$BUILDPLATFORM denoland/deno:2.9.8 AS challenge
ARG TARGETARCH
WORKDIR /build
COPY challenge/package.json challenge/deno.lock challenge/challenge.mjs ./
RUN case "$TARGETARCH" in \
      amd64) target=x86_64-unknown-linux-gnu ;; \
      arm64) target=aarch64-unknown-linux-gnu ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && deno install --frozen \
 && deno compile --frozen --no-prompt --target "$target" --allow-env=WS_NO_BUFFER_UTIL,WS_NO_UTF_8_VALIDATE --output challenge challenge.mjs

FROM searxng/searxng:2026.11.23-a7163d091
LABEL io.modelcontextprotocol.server.name="io.github.Belluxx/sharp-search"
COPY --chown=searxng:searxng searxng/settings.yml /etc/searxng/settings.yml
COPY src/server.py src/fetch.py /app/
COPY --from=challenge /build/challenge /app/challenge
USER searxng
ENTRYPOINT ["/usr/local/searxng/.venv/bin/python", "/app/server.py"]
