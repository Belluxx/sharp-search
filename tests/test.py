"""Offline tests, run inside the image with networking disabled (make test)."""

import http.server
import io
import json
import resource
import socket
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import fetch
import server


def call(name, arguments):
    return server.answer({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})["result"]


def text(result):
    return result["content"][0]["text"]


class Protocol(unittest.TestCase):
    def test_messages(self):
        initialized = server.answer({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        self.assertIsNone(server.answer({"jsonrpc": "2.0", "method": "notifications/initialized"}), "notifications get no response")
        self.assertEqual(server.answer({"jsonrpc": "2.0", "id": "a", "method": "server/discover"})["error"]["code"], -32601)
        self.assertEqual(server.answer({"jsonrpc": "2.0", "id": 0, "method": "ping"}), {"jsonrpc": "2.0", "id": 0, "result": {}})
        self.assertEqual(server.answer_line(b"{")["error"]["code"], -32700)
        self.assertEqual(server.answer_line(b"[]")["error"]["code"], -32600)

    def test_tools(self):
        tools = server.answer({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["search", "fetch"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["query"])
        self.assertEqual(tools[1]["inputSchema"]["properties"]["offset"]["type"], "number")
        self.assertEqual(text(call("delete", {})), "Unknown tool: delete")
        self.assertTrue(call("delete", {})["isError"])


class Search(unittest.TestCase):
    def search(self, answer, **arguments):
        asked = []

        def searxng(query):
            asked.append(query)
            return answer if isinstance(answer, bytes) else json.dumps(answer).encode()

        with mock.patch.object(server, "searxng", searxng):
            return call("search", arguments), asked

    def test_results(self):
        answer = {
            "answers": ["42", {"answer": "forty-two", "url": "https://a.example"}],
            "infoboxes": [{"infobox": "Rust", "content": "A  language\nfor systems."}],
            "results": [{"title": " First ", "url": "https://one.example", "content": "One\n  two"}, {"title": "No address"},
                        {"url": "https://three.example"}, {"title": "Fourth", "url": "https://four.example"}],
            "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
        }
        result, asked = self.search(answer, query=" rust ", limit=2)
        self.assertEqual(asked, ["rust"])
        self.assertEqual(text(result), "42\n\nforty-two\n\nRust: A language for systems.\n\n"
                                       "1. First\n   https://one.example\n   One two\n\n2. https://three.example\n   https://three.example")
        result, _ = self.search(answer, query="rust", limit=0)
        self.assertIn("1. First", text(result))
        self.assertNotIn("2. ", text(result))

    def test_snippets_are_truncated(self):
        result, _ = self.search({"results": [{"url": "https://x.example", "content": "a" * 900}]}, query="x")
        self.assertEqual(text(result), "1. https://x.example\n   https://x.example\n   " + "a" * 500 + "…")

    def test_nothing_found(self):
        result, _ = self.search({"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ["google", "timeout"]]}, query="x")
        self.assertEqual(text(result), "No results found. Engines that failed: duckduckgo: CAPTCHA; google: timeout")
        result, _ = self.search({"results": []}, query="x")
        self.assertEqual(text(result), "No results found")

    def test_errors(self):
        result, asked = self.search({"results": []}, query="  ")
        self.assertEqual((text(result), result["isError"], asked), ("The query is empty", True, []))
        result, _ = self.search(b"<html>403</html>", query="x")
        self.assertEqual((text(result), result["isError"]), ("the search engine returned an invalid response", True))

    def test_query_encoding(self):
        asked = []

        def urlopen(address, timeout):
            asked.append(address)
            return io.BytesIO(b"{}")

        with mock.patch.object(server, "start_engine"), mock.patch.object(server.searxng_opener, "open", urlopen):
            server.searxng("c++ & rust?")
        self.assertEqual(asked, ["http://127.0.0.1:8888/search?q=c%2B%2B+%26+rust%3F&format=json"])

    def test_redirects_are_not_followed(self):
        # Site stands in for SearXNG, which answers "!!" queries with a redirect.
        site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        self.addCleanup(site.server_close)
        self.addCleanup(site.shutdown)
        with mock.patch.object(server, "start_engine"), mock.patch.object(server, "SEARXNG_PORT", site.server_port):
            result = call("search", {"query": "!! rust"})
        self.assertEqual((text(result), result["isError"]), ('"!!" shortcuts are not supported. Remove "!!" from the query', True))


class Chunks(unittest.TestCase):
    def test_chunks(self):
        loads = []

        def load(address):
            loads.append(address)
            if address != "https://one.example/":
                raise fetch.Unreadable("could not load the page")
            return {"url": address, "title": "One", "text": "a" * 40010}

        server.pages.clear()
        with mock.patch.object(fetch, "fetch", load):
            first = text(call("fetch", {"url": " https://one.example/ "}))
            rest = text(call("fetch", {"url": "https://one.example/", "offset": 40000}))
            past = text(call("fetch", {"url": "https://one.example/", "offset": 50000}))
            again = text(call("fetch", {"url": "https://one.example/"}))
            refused = call("fetch", {"url": "https://other.example/", "offset": 10})
        self.assertTrue(first.startswith("# One\nhttps://one.example/\n\naaa"), first[:40])
        self.assertTrue(first.endswith("[Characters 0-40000 of 40010. Use offset=40000 to continue]"), first[-80:])
        self.assertEqual((rest, past, again), ("a" * 10, "(offset is past the end of the page)", first))
        self.assertEqual((text(refused), refused["isError"]), ("could not load the page", True))
        self.assertEqual(loads, ["https://one.example/", "https://one.example/", "https://other.example/"], "later chunks come from the cache")


class Markdown(unittest.TestCase):
    def test_content_after_html_and_in_templates_with_undeclared_utf8(self):
        body = "<html><body><main><p>Post</p></main></body></html><template><p>A reply, sent after the page ✓</p></template>".encode()
        response = type("Response", (), {"charset_encoding": None})
        self.assertIn("A reply, sent after the page ✓", fetch.page_text(body, "https://site.test/", fetch.charset_of(response, body))[1])

    def test_react_streamed_content(self):
        body = b'<html><body><main><h1>Post</h1><!--$?--><template id="B:0"></template><p>Loading</p><!--/$--></main><div hidden id="S:0"><p>A reply</p></div><script>$RC("B:0","S:0")</script></body></html>'
        self.assertEqual(fetch.page_text(body, "https://site.test/", "utf-8")[1].replace("\n\n", " / "), "# Post / A reply")

    def test_links(self):
        body = b'<html><body><p><a href="#details">Details</a> of this page, <a href="/other#top">another page</a> and <a href="/ad?t=' + b"x" * 600 + b'">an ad</a></p></body></html>'
        self.assertEqual(fetch.page_text(body, "https://site.test/page", "utf-8")[1], "Details of this page, [another page](https://site.test/other#top) and an ad")

    def test_elements(self):
        body = b"<html><title>T</title><nav>Menu</nav><h2>Steps</h2><ul><li>One</li><li>Two</li></ul><table><tr><td>a</td><td>b</td></tr></table><pre>x  = 1\n  y</pre><footer>Legal</footer></html>"
        self.assertEqual(fetch.page_text(body, "https://site.test/", "utf-8"), ("T", "## Steps\n\n- One\n- Two\n\n| a | b\n\n```\nx  = 1\n  y\n```"))


BOT_CHECK = (b'<form method="GET" action="/checked"><input type="hidden" name="solution"></form><script>document.cookie = "passed=1";'
         b'document.addEventListener("DOMContentLoaded", async () => { const form = document.forms[0];'
         b'form.elements.namedItem("solution").value = await (async (e) => e + e)("ab"); form.requestSubmit() })</script>')


class Challenge(unittest.TestCase):
    def run_scripts(self, html):
        return fetch.run_scripts("https://site.test/checked", html)

    def test_solves_bot_check(self):
        self.assertEqual(self.run_scripts(BOT_CHECK.decode()), {"method": "GET", "url": "https://site.test/checked?solution=abab", "cookie": "passed=1"})

    def test_memory_is_limited(self):
        # Arrays are on the JavaScript heap; typed arrays are outside it.
        for script in ["const kept = []; for (;;) kept.push(new Array(1e6).fill(Math.random()))",
                       "const kept = []; for (;;) kept.push(new Uint8Array(5e7).fill(1))"]:
            with self.subTest(script):
                self.assertFalse(self.run_scripts(f"<script>{script}</script>"))
        self.assertLess(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss, fetch.CHALLENGE_KB)

    def test_time_is_limited(self):
        with mock.patch.object(fetch, "CHALLENGE_SECONDS", 1):
            self.assertIsNone(self.run_scripts("<script>for (;;);</script>"))

    def test_no_file_or_network_access_after_escaping_vm(self):
        html = ('<script>const host = document.constructor.constructor("return globalThis")(); const tried = [];'
                'try { host.Deno.readTextFileSync("/etc/hostname"); tried.push("read") } catch (e) { tried.push(e.name) }'
                'host.fetch("https://example.com/").then(() => tried.push("fetched"), (e) => tried.push(e.name)).then(() => location.href = "/?" + tried.join(","))</script>')
        self.assertEqual(self.run_scripts(html)["url"], "https://site.test/?NotCapable,NotCapable")


class Addresses(unittest.TestCase):
    def test_rejects_non_public_ips(self):
        for address in ["http://127.0.0.1/", "http://localhost:8080/", "http://10.0.0.1/", "http://172.17.0.1/", "http://192.168.1.1/",
                        "http://169.254.169.254/latest/meta-data/", "http://100.64.0.1/", "http://224.0.0.1/", "http://0.0.0.0/",
                        "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "http://[64:ff9b::7f00:1]/",
                        "http://[64:ff9b::a9fe:a9fe]/"]:
            with self.subTest(address), self.assertRaisesRegex(fetch.Unreadable, "is not a public address"):
                fetch.fetch(address)

    def test_accepts_public_ips(self):
        for ip in ["93.184.215.14", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"]:
            with self.subTest(ip):
                self.assertTrue(fetch.public(ip))

    def test_rejects_non_http_urls(self):
        for address in ["file:///etc/passwd", "ftp://example.com/", "example.com", "http:///path", "http://example.com:99999/", ""]:
            with self.subTest(address), self.assertRaisesRegex(fetch.Unreadable, r"not an http\(s\) URL"):
                fetch.fetch(address)


class Site(http.server.BaseHTTPRequestHandler):
    """Local stand-in for a public website."""

    def do_GET(self):
        path, query = urlsplit(self.path).path, parse_qs(urlsplit(self.path).query)
        port = self.server.server_port
        agent = self.headers.get("user-agent") or ""
        if path == "/hop":
            self.answer(302, location="/page")
        elif path == "/loop":
            self.answer(302, location="/loop")
        elif path == "/away":
            self.answer(302, location=f"http://127.0.0.2:{port}/page")
        elif path == "/page":
            self.answer(200, b"<title>Arrived</title><p>Arrived</p>")
        elif path == "/checked":
            passed = query.get("solution") == ["abab"] and "passed=1" in (self.headers.get("cookie") or "")
            self.answer(200, b"<p>Welcome</p>" if passed else BOT_CHECK)
        elif path == "/anubis":
            self.answer(200, b'<script id="anubis_version">"v1"</script><p>Making sure you\'re not a bot!</p>' if "Mozilla" in agent else b"<p>Welcome</p>")
        elif path == "/previews":
            self.answer(200 if agent.startswith("Slackbot") else 403, b"<p>Welcome</p>")
        elif path == "/refused":
            self.answer(403 if "Safari" in agent else 200)
        elif path == "/picture":
            self.answer(200, b"\x89PNG", kind="image/png")
        elif path == "/search":
            self.answer(302, location="/page")
        elif path == "/drip":
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"<p>Drop</p>")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
        else:
            self.answer(404, b"<p>Not here</p>")

    def answer(self, status, body=b"", kind="text/html", location=None):
        self.send_response(status)
        self.send_header("content-type", kind)
        if location:
            self.send_header("location", location)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Fetching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.site.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()

    def setUp(self):
        patch = mock.patch.object(fetch, "public", lambda ip: ip == "127.0.0.1")
        patch.start()
        self.addCleanup(patch.stop)

    def test_page(self):
        self.assertEqual(fetch.fetch(self.base + "/page"), {"url": self.base + "/page", "title": "Arrived", "text": "Arrived"})

    def test_redirects(self):
        self.assertEqual(fetch.fetch(self.base + "/hop")["url"], self.base + "/page")
        with self.assertRaisesRegex(fetch.Unreadable, "127.0.0.2 is not a public address"):
            fetch.fetch(self.base + "/away")
        with self.assertRaisesRegex(fetch.Unreadable, "too many redirects"):
            fetch.fetch(self.base + "/loop")

    def test_curl_uses_the_checked_ip(self):
        # curl can't resolve site.test without network.
        def getaddrinfo(host, port, **options):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))] if host == "site.test" else []

        with mock.patch.object(fetch.socket, "getaddrinfo", getaddrinfo):
            self.assertEqual(fetch.fetch(f"http://site.test:{self.site.server_port}/page")["text"], "Arrived")

    def test_bot_check(self):
        self.assertEqual(fetch.fetch(self.base + "/checked"), {"url": self.base + "/checked", "title": "", "text": "Welcome"})

    def test_blocked_fetch_retries_as_link_previews(self):
        for path in ["/anubis", "/previews"]:
            with self.subTest(path):
                self.assertEqual(fetch.fetch(self.base + path)["text"], "Welcome")
        with self.assertRaisesRegex(fetch.Unreadable, "HTTP 403"):
            fetch.fetch(self.base + "/refused")
        with mock.patch.object(fetch, "AGENTS", ()), self.assertRaisesRegex(fetch.Unreadable, "blocked by a bot check"):
            fetch.fetch(self.base + "/anubis")

    def test_fallbacks_can_be_turned_off(self):
        with mock.patch.object(fetch, "FALLBACKS", False):
            self.assertEqual(fetch.fetch(self.base + "/anubis")["text"], "Making sure you're not a bot!")
            with self.assertRaisesRegex(fetch.Unreadable, "HTTP 403"):
                fetch.fetch(self.base + "/previews")

    def test_slow_page_times_out(self):
        with mock.patch.object(fetch, "TIMEOUT", 1), self.assertRaisesRegex(fetch.Unreadable, "timed out"):
            fetch.fetch(self.base + "/drip")

    def test_unreadable(self):
        with self.assertRaisesRegex(fetch.Unreadable, "HTTP 404"):
            fetch.fetch(self.base + "/missing")
        with self.assertRaisesRegex(fetch.Unreadable, "not a text page: image/png"):
            fetch.fetch(self.base + "/picture")


if __name__ == "__main__":
    unittest.main()
