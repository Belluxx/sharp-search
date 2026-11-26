"""MCP server with two tools: search, backed by SearXNG on loopback, and fetch, backed by fetch.py.

Serves MCP over stdio (newline-delimited JSON-RPC), or with --http over Streamable HTTP at :8080/mcp.
With --no-fallbacks, a blocked page isn't fetched again with other user agents: see Fallbacks in fetch.py.
Usage: server.py [--http] [--no-fallbacks]
"""

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fetch

SEARXNG = "/usr/local/searxng"
SEARXNG_PORT = 8888
HTTP_PORT = 8080
DEFAULT_LIMIT = 8
MAX_LIMIT = 20
MAX_SNIPPET = 500
PAGE_CHARACTERS = 40000
KEPT_PAGES = 16
# Concurrent tool calls. SearXNG idles at about 140 MB, and a fetch uses up to 75 MB.
WORK = threading.BoundedSemaphore(4)
MAX_BODY = 1 << 20

TOOLS = [
    {
        "name": "search",
        "description": "Search the web. Returns the title, URL and snippet of each result. Use fetch to read a page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "number", "description": f"Maximum number of results (default {DEFAULT_LIMIT}, at most {MAX_LIMIT})"},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "fetch",
        "description": "Fetch a web page as Markdown. Long pages are split into chunks; to continue, pass the offset given at the end of the previous chunk.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http or https URL"},
                "offset": {"type": "number", "description": "Character offset to start from (default 0)"},
            },
            "required": ["url"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
]


class ToolError(Exception):
    """An error message for the agent."""


# MARK: Search

engine = None
engine_lock = threading.Lock()


def start_engine():
    global engine
    with engine_lock:
        if engine is None or engine.poll() is not None:
            env = {**os.environ, "GRANIAN_HOST": "127.0.0.1", "GRANIAN_PORT": str(SEARXNG_PORT), "SEARXNG_SECRET": secrets.token_hex(32)}
            engine = subprocess.Popen([f"{SEARXNG}/.venv/bin/granian", "searx.webapp:app"], cwd=SEARXNG, env=env, stdin=subprocess.DEVNULL)


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# SearXNG answers a "!!" query with a redirect to another website. Following it would load that
# page from here, outside the checks in fetch.py.
searxng_opener = urllib.request.build_opener(NoRedirects)


def searxng(query):
    start_engine()
    address = f"http://127.0.0.1:{SEARXNG_PORT}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    # SearXNG takes a few seconds to start listening.
    deadline = time.monotonic() + 30
    while True:
        try:
            with searxng_opener.open(address, timeout=20) as answer:
                return answer.read()
        except urllib.error.HTTPError as error:
            # The error holds the open response.
            error.close()
            if 300 <= error.code < 400:
                raise ToolError('"!!" shortcuts are not supported. Remove "!!" from the query') from None
            raise ToolError(f"the search engine failed: HTTP {error.code}") from None
        except OSError as error:
            if isinstance(getattr(error, "reason", error), ConnectionRefusedError) and time.monotonic() < deadline:
                time.sleep(0.5)
                continue
            raise ToolError("the search engine is not responding") from None


def clean(text):
    line = " ".join(text.split()) if isinstance(text, str) else ""
    return line[:MAX_SNIPPET] + "…" if len(line) > MAX_SNIPPET else line


def search_text(body, limit):
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("results"), list):
        raise ToolError("the search engine returned an invalid response")
    blocks = []
    # Answers are strings in older SearXNG versions and objects in newer ones.
    for item in answer.get("answers") or []:
        if text := clean(item.get("answer") if isinstance(item, dict) else item):
            blocks.append(text)
    for box in answer.get("infoboxes") or []:
        if isinstance(box, dict) and (content := clean(box.get("content"))):
            blocks.append(": ".join(part for part in (clean(box.get("infobox")), content) if part))
    results = [item for item in answer["results"] if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]]
    for rank, item in enumerate(results[:limit], 1):
        title, snippet = clean(item.get("title")), clean(item.get("content"))
        blocks.append("\n".join([f"{rank}. {title or item['url']}", f"   {item['url']}"] + ([f"   {snippet}"] if snippet else [])))
    if blocks:
        return "\n\n".join(blocks)
    silent = [": ".join(map(str, pair)) for pair in answer.get("unresponsive_engines") or [] if isinstance(pair, list) and pair]
    return f"No results found. Engines that failed: {'; '.join(silent)}" if silent else "No results found"


# MARK: Pages

pages = OrderedDict()
pages_lock = threading.Lock()


def page_part(address, offset):
    with pages_lock:
        page = pages.get(address) if offset > 0 else None
    if page is None:
        page = fetch.fetch(address)
        with pages_lock:
            pages[address] = page
            pages.move_to_end(address)
            while len(pages) > KEPT_PAGES:
                pages.popitem(last=False)
    text = page["text"]
    start = min(max(0, offset), len(text))
    end = min(len(text), start + PAGE_CHARACTERS)
    part = text[start:end] or ("(offset is past the end of the page)" if start > 0 else "(the page has no text)")
    if start == 0:
        part = (f"# {page['title']}\n" if page["title"] else "") + f"{page['url']}\n\n{part}"
    if end < len(text):
        part += f"\n\n[Characters {start}-{end} of {len(text)}. Use offset={end} to continue]"
    return part


# MARK: The protocol


def number(value, default):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def call_tool(name, arguments):
    if name == "search":
        query = arguments.get("query").strip() if isinstance(arguments.get("query"), str) else ""
        if not query:
            raise ToolError("The query is empty")
        return search_text(searxng(query), max(1, min(number(arguments.get("limit"), DEFAULT_LIMIT), MAX_LIMIT)))
    if name == "fetch":
        address = arguments.get("url").strip() if isinstance(arguments.get("url"), str) else ""
        return page_part(address, number(arguments.get("offset"), 0))
    raise ToolError(f"Unknown tool: {name}")


def answer(message):
    id = message.get("id")
    if id is None:
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    method = message.get("method")
    if method == "initialize":
        # Nothing this server uses differs between protocol versions.
        result = {"protocolVersion": params.get("protocolVersion") or "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "sharp-search", "version": "1.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            with WORK:
                result = {"content": [{"type": "text", "text": call_tool(params.get("name"), arguments)}]}
        except Exception as error:
            if not isinstance(error, (ToolError, fetch.Unreadable)):
                traceback.print_exc()
            result = {"content": [{"type": "text", "text": str(error) or repr(error)}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": id, "result": result}


def answer_line(data):
    try:
        message = json.loads(data)
    except ValueError:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    return answer(message)


# MARK: Transports


def protocol_output():
    """Point fd 1 at stderr, so that SearXNG's output stays out of the protocol stream."""
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    return out


def serve_stdio(out):
    lock = threading.Lock()

    def reply(line):
        message = answer_line(line)
        if message is not None:
            with lock:
                out.write(json.dumps(message) + "\n")
                out.flush()

    # At EOF, in-flight calls still finish.
    with ThreadPoolExecutor(max_workers=8) as pool:
        for line in sys.stdin.buffer:
            if line.strip():
                pool.submit(reply, line)


class Handler(BaseHTTPRequestHandler):
    """Streamable HTTP without streaming or sessions: each POST gets one JSON response."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if not self.allowed():
            return
        length = int(self.headers.get("content-length") or 0)
        if not 0 < length <= MAX_BODY:
            return self.send(400)
        message = answer_line(self.rfile.read(length))
        if message is None:
            return self.send(202)
        self.send(400 if message.get("id") is None else 200, json.dumps(message).encode())

    def do_GET(self):
        if self.allowed():
            self.send(405)

    do_DELETE = do_GET

    def allowed(self):
        """Rejects other origins, so that websites can't reach the server through a browser."""
        origin = urllib.parse.urlsplit(self.headers.get("origin") or "http://localhost").hostname
        if self.path != "/mcp":
            self.send(404)
        elif origin not in ("localhost", "127.0.0.1", "::1"):
            self.send(403)
        else:
            return True
        return False

    def send(self, status, body=b""):
        self.send_response(status)
        if body:
            self.send_header("content-type", "application/json")
        if status == 405:
            self.send_header("allow", "POST")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    options = set(sys.argv[1:])
    if not options <= {"--http", "--no-fallbacks"}:
        sys.exit(__doc__.strip())
    fetch.FALLBACKS = "--no-fallbacks" not in options
    # Docker stops containers with SIGTERM, which PID 1 ignores without a handler.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    if "--http" in options:
        start_engine()
        print(f"sharp-search-mcp: listening on http://0.0.0.0:{HTTP_PORT}/mcp", file=sys.stderr, flush=True)
        ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()
    else:
        out = protocol_output()
        start_engine()
        serve_stdio(out)


if __name__ == "__main__":
    main()
