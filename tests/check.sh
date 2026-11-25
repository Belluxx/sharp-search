#!/usr/bin/env bash
# End-to-end tests of both transports against the live web (make check). Needs Docker and curl.
set -u
IMAGE=${1:-sharp-search-mcp}
PASS=0
FAIL=0

# check NAME OUTPUT PATTERN...: passes if OUTPUT contains any of the patterns.
check() {
  local name=$1 out=$2 patterns=()
  shift 2
  for pattern in "$@"; do patterns+=(-e "$pattern"); done
  if grep -qF "${patterns[@]}" <<<"$out"; then
    PASS=$((PASS + 1)); echo "  ok    $name"
  else
    FAIL=$((FAIL + 1)); echo "  FAIL  $name"; cut -c1-300 <<<"$out" | tail -5 | sed 's/^/        /'
  fi
}
call() { printf '{"jsonrpc":"2.0","id":%s,"method":"tools/call","params":{"name":"%s","arguments":%s}}' "$1" "$2" "$3"; }
INIT='{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}'
# Results, or "No results found" when the engines are rate-limited. Both mean search works.
SEARCHED=('"text": "1. ' '\n1. ' 'No results found')

echo "stdio"
# stdin closes after the last call. In-flight calls must still get responses.
out=$(printf '%s\n' "$INIT" '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  "$(call 1 search '{"query":"wikipedia","limit":3}')" \
  "$(call 2 fetch '{"url":"https://example.com/"}')" \
  "$(call 3 fetch '{"url":"http://192.168.1.1/"}')" \
  "$(call 4 search '{"query":"!!w wikipedia"}')" | docker run -i --rm "$IMAGE" 2>/dev/null)
check "initialize" "$out" '"serverInfo": {"name": "sharp-search"'
check "stdout has only JSON-RPC" "lines=$(grep -cv '^{"jsonrpc"' <<<"$out")" "lines=0"
check "search" "$(grep '"id": 1' <<<"$out")" "${SEARCHED[@]}"
check "fetch" "$(grep '"id": 2' <<<"$out")" '# Example Domain'
check "fetch rejects private IPs" "$(grep '"id": 3' <<<"$out")" 'is not a public address'
# SearXNG redirects this query to Wikipedia. The server must not follow.
check "search rejects !! shortcuts" "$(grep '"id": 4' <<<"$out")" 'shortcuts are not supported'

echo "http"
id=$(docker run -d --rm -p 127.0.0.1::8080 "$IMAGE" --http)
port=$(docker port "$id" 8080 | head -1 | sed 's/.*://')
post() { curl -s -X POST -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' "$@" "http://127.0.0.1:$port/mcp"; }
for _ in $(seq 50); do post -d "$INIT" >/dev/null && break; sleep 0.2; done
check "initialize" "$(post -d "$INIT")" '"protocolVersion": "2025-11-25"'
check "notification returns 202" "status=$(post -o /dev/null -w '%{http_code}' -d '{"jsonrpc":"2.0","method":"notifications/initialized"}')" "status=202"
check "search" "$(post -d "$(call 1 search '{"query":"wikipedia","limit":3}')")" "${SEARCHED[@]}"
check "fetch" "$(post -d "$(call 2 fetch '{"url":"https://example.com/"}')")" '# Example Domain'
check "GET returns 405" "status=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/mcp")" "status=405"
check "cross-origin request returns 403" "status=$(post -o /dev/null -w '%{http_code}' -H 'origin: https://site.example' -d "$INIT")" "status=403"
# Reddit serves a JavaScript bot check. It may also block this IP.
out=$(post -d "$(call 3 fetch '{"url":"https://www.reddit.com/r/programming/"}')")
if grep -q "/r/programming/comments/" <<<"$out"; then PASS=$((PASS + 1)); echo "  ok    Reddit bot check"
elif grep -qE "HTTP (403|429)" <<<"$out"; then echo "  skip  Reddit bot check: blocked by Reddit"
else FAIL=$((FAIL + 1)); echo "  FAIL  Reddit bot check"; cut -c1-300 <<<"$out" | sed 's/^/        /'; fi
docker stop -t 2 "$id" >/dev/null

echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
