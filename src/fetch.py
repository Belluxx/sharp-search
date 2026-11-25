"""Fetch a web page and convert it to Markdown: {"url", "title", "text"}.

Only public IPs are reached: the host and every redirect target are resolved and checked, and curl
is pinned to the checked IP. A page with scripts but no text is treated as a JavaScript bot check:
challenge.mjs runs its scripts offline, and the fetch follows the resulting navigation in the same
session. Websites are untrusted, so a fetch is limited in time and size, and the scripts in time
and memory.
Usage: fetch.py <url>
"""

import codecs
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urldefrag, urljoin, urlsplit

import lxml.etree
import lxml.html
from curl_cffi import CurlOpt, requests
from curl_cffi._wrapper import ffi, lib

MAX_BYTES = 2 << 20
MAX_REDIRECTS = 5
# Seconds for all the requests of one fetch: every redirect and bot-check round, headers and body.
TIMEOUT = 30
# Links with longer URLs (usually ads and trackers) are rendered as plain text.
MAX_ADDRESS = 500
CHALLENGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "challenge")
# Max bot-check navigations to follow.
ROUNDS = 2
# Limits for one run of challenge.mjs: seconds, and KB of writable memory (RLIMIT_DATA). It starts
# at about 370 MB, most of it reserved but never used, so this leaves it about 400 MB.
CHALLENGE_SECONDS = 10
CHALLENGE_KB = 768 << 10
# NAT64 addresses (RFC 6052). The gateway connects to the IPv4 address in the last 32 bits.
NAT64 = ipaddress.ip_network("64:ff9b::/96")

# Elements that aren't content: code, media and site navigation. <template> is kept because
# streamed pages put content in it.
DROPPED = {
    "script", "style", "noscript", "svg", "canvas", "iframe", "object", "head", "nav", "footer", "aside", "form", "button", "select",
}
DROPPED_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCKS = {
    "address", "article", "blockquote", "details", "div", "dl", "dd", "dt", "fieldset", "figcaption", "figure", "header", "hr", "main",
    "ol", "p", "section", "summary", "table", "ul",
}


# React streaming SSR calls that move late content into place: $RC(boundary, content) completes a
# Suspense boundary, $RS(segment, placeholder) fills in a segment.
STREAMED = re.compile(r'\$(RC|RS)\("([^"]+)","([^"]+)"\)')


class Unreadable(Exception):
    """An error message for the agent, when a page can't be read."""


def inline(text):
    return re.sub(r"\s+", " ", text or "")


def render(node, base, out):
    """Append `node` and its descendants to `out` as Markdown."""
    tag = node.tag if isinstance(node.tag, str) else None
    if tag is None or tag in DROPPED or node.get("role") in DROPPED_ROLES or node.get("hidden") is not None or node.get("aria-hidden") == "true":
        return
    if tag == "pre":
        out.append("\n\n```\n" + node.text_content().strip("\n") + "\n```\n\n")
        return
    if tag == "br":
        out.append("\n")
    elif tag in BLOCKS:
        out.append("\n\n")
    elif tag in HEADINGS:
        out.append("\n\n" + "#" * int(tag[1]) + " ")
    elif tag == "li":
        out.append("\n- ")
    elif tag == "tr":
        out.append("\n")
    elif tag in ("td", "th"):
        out.append(" | ")

    start = len(out)
    out.append(inline(node.text))
    for child in node:
        render(child, base, out)
        out.append(inline(child.tail))
    if tag == "a":
        href = urljoin(base, node.get("href") or "")
        label = "".join(out[start:]).strip()
        # Same-page anchors and overlong URLs are rendered as plain text.
        if label and href.startswith(("http://", "https://")) and urldefrag(href)[0] != urldefrag(base)[0] and len(href) <= MAX_ADDRESS:
            out[start:] = [f"[{label}]({href})"]

    if tag in BLOCKS or tag in HEADINGS:
        out.append("\n\n")


def move_into_place(source, anchor):
    """Move the contents of `source` to just before `anchor`, then remove `source`."""
    if source.text:
        previous = anchor.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + source.text
        else:
            anchor.getparent().text = (anchor.getparent().text or "") + source.text
    for child in list(source):
        anchor.addprevious(child)
    source.drop_tree()


def place_streamed(roots):
    """Apply React's streaming SSR calls. Streamed content arrives hidden at the end of the
    document, so it would be dropped if left there."""
    nodes = [node for root in roots for node in root.iter()]
    found = {node.get("id"): node for node in nodes if isinstance(node.tag, str) and node.get("id")}
    for script in [node for node in nodes if node.tag == "script"]:
        for call, first, second in STREAMED.findall(script.text or ""):
            moved, there = (found.get(first), found.get(second)) if call == "RS" else (found.get(second), found.get(first))
            if moved is None or there is None or moved.getparent() is None or there.getparent() is None:
                continue
            if call == "RS":
                move_into_place(moved, there)
                there.drop_tree()
                continue
            # Remove the loading fallback: the boundary up to its matching <!--/$--> comment.
            loading, depth, end = [there], 0, None
            for node in there.itersiblings():
                if node.tag is lxml.etree.Comment:
                    if node.text == "/$" and depth == 0:
                        end = node
                        break
                    depth += {"$": 1, "$?": 1, "$!": 1, "/$": -1}.get(node.text, 0)
                loading.append(node)
            if end is not None:
                for node in loading:
                    node.getparent().remove(node)
                move_into_place(moved, end)


def text_of(nodes, base):
    out = []
    for node in nodes:
        render(node, base, out)
    # Trim each line, but keep indentation in code blocks.
    lines, code = [], False
    for line in "".join(out).split("\n"):
        if line.strip() == "```":
            line, code = "```", not code
        lines.append(line.rstrip() if code else line.strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def page_text(body, base, charset):
    # Decode in Python and pass UTF-8 to lxml, which doesn't know every charset name Python does.
    if charset:
        body = body.decode(charset, errors="replace").encode()
    document = lxml.html.fromstring(body, parser=lxml.html.HTMLParser(encoding="utf-8") if charset else None)
    title = document.findtext(".//title") or ""
    # Browsers render content after </html>. lxml parses it as extra root elements.
    roots = [document, *document.itersiblings()]
    place_streamed(roots)
    text = text_of(roots, base)
    # Use the page's single <main> or <article>, if it has more than half of the text.
    mains = document.xpath("//main") or document.xpath("//article")
    if len(mains) == 1:
        main = text_of(mains, base)
        if len(main) * 2 > len(text):
            text = main
    return inline(title).strip(), text


def charset_of(response, body):
    """The charset from the headers, else UTF-8 if the body decodes as UTF-8, else None (lxml
    detects it from the page). Without this, lxml decodes pages with no declared charset as Latin-1."""
    named = response.charset_encoding
    if named:
        try:
            return codecs.lookup(named).name
        except LookupError:
            pass
    try:
        # final=False: truncating at MAX_BYTES may have split the last character.
        codecs.getincrementaldecoder("utf-8")().decode(body, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        return None


def public(ip):
    """Whether `ip` is public: not loopback, private, link-local (cloud metadata), shared or
    multicast."""
    ip = ipaddress.ip_address(ip.split("%")[0])
    ip = getattr(ip, "ipv4_mapped", None) or ip
    if ip in NAT64:
        ip = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def pin(session, address):
    """Resolve the URL's host, check that the IP is public, and pin the session's next request to
    that IP. The pin applies to any host, so curl never does its own DNS lookup."""
    parts = urlsplit(address)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        port = None
    if parts.scheme not in ("http", "https") or not parts.hostname or port is None:
        raise Unreadable(f"not an http(s) URL: {address}")
    try:
        found = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise Unreadable(f"could not resolve {parts.hostname}")
    # Prefer IPv4: containers often have no IPv6 route.
    ip = min((info[4][0] for info in found), key=lambda ip: ":" in ip)
    if not public(ip):
        raise Unreadable(f"{parts.hostname} is not a public address")
    # CONNECT_TO takes a curl_slist, which curl_cffi doesn't build for this option, so build it
    # here. The session holds it until the next request replaces it or the session closes.
    pinned = lib.curl_slist_append(ffi.NULL, f"::{f'[{ip}]' if ':' in ip else ip}:{port}".encode())
    session.curl_options = {CurlOpt.CONNECT_TO: ffi.gc(pinned, lib.curl_slist_free_all)}


def load(session, deadline, method, address, data=None, headers=None):
    """Send a request, following redirects here so that each target is checked. Returns
    (final URL, content type, charset, body truncated to MAX_BYTES). Fails at `deadline`, a
    time.monotonic() value."""
    for _ in range(MAX_REDIRECTS + 1):
        pin(session, address)
        # The timeout argument below only limits connecting and stalling, because the response is
        # streamed: a server that keeps sending slowly would never time out. This limits the whole
        # transfer. 0 would mean no limit.
        session.curl_options[CurlOpt.TIMEOUT_MS] = max(1, int((deadline - time.monotonic()) * 1000))
        try:
            response = session.request(method, address, data=data, headers=headers, timeout=20, allow_redirects=False, stream=True)
        except Exception as error:
            raise Unreadable(f"could not load the page: {str(error).split('. See ')[0]}")
        location = response.headers.get("location")
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            break
        response.close()
        address = urljoin(address, location)
        # Like browsers, switch to GET after a 303, or after a 301 or 302 in response to a POST.
        if response.status_code == 303 or (method == "POST" and response.status_code in (301, 302)):
            method, data, headers = "GET", None, None
    else:
        raise Unreadable("could not load the page: too many redirects")
    try:
        if response.status_code >= 400:
            raise Unreadable(f"could not load the page: HTTP {response.status_code}")
        kind = response.headers.get("content-type", "text/html").split(";")[0].strip().lower()
        if not (kind.startswith("text/") or kind.endswith(("json", "xml", "javascript"))):
            raise Unreadable(f"not a text page: {kind}")
        body = b""
        for chunk in response.iter_content():
            body += chunk
            if len(body) >= MAX_BYTES:
                break
    except Unreadable:
        raise
    except Exception as error:
        raise Unreadable(f"could not load the page: {str(error).split('. See ')[0]}")
    finally:
        response.close()
    return address, kind, charset_of(response, body), body


def run_scripts(address, html):
    """Run the page's scripts in challenge.mjs and return the navigation they trigger ({} if none),
    or None if the run fails or exceeds its limits."""
    # The shell sets the memory limit, which the kernel enforces, then becomes challenge.mjs, so
    # the timeout kills the right process.
    command = ["/bin/sh", "-c", f'ulimit -d {CHALLENGE_KB} && exec "$0"', CHALLENGE]
    try:
        answer = subprocess.run(command, input=json.dumps({"url": address, "html": html}).encode(), capture_output=True, timeout=CHALLENGE_SECONDS, check=True)
        step = json.loads(answer.stdout)
    except Exception:
        return None
    return step if isinstance(step, dict) else None


def challenge(session, address, html):
    """Return the request that the page's scripts navigate to, as (method, url, data, headers), or
    None. Only navigations to the same host are followed."""
    step = run_scripts(address, html)
    if not step or not isinstance(step.get("url"), str) or not isinstance(step.get("method"), str):
        return None
    site = urlsplit(address)
    target = urlsplit(step["url"])
    if target.scheme not in ("http", "https") or target.netloc != site.netloc:
        return None
    # Send cookies set through document.cookie, like a browser.
    for pair in (step.get("cookie") or "").split(";"):
        name, _, value = pair.strip().partition("=")
        if name:
            session.cookies.set(name, value, domain=site.hostname)
    return step["method"], step["url"], step.get("body"), {"content-type": step["type"]} if step.get("type") else None


def fetch(address):
    """Fetch `address` as {"url", "title", "text"}. Raises Unreadable on failure."""
    with requests.Session(impersonate="chrome") as session:
        request = ("GET", address)
        shown = None
        deadline = time.monotonic() + TIMEOUT
        for sent_on in range(ROUNDS + 1):
            url, kind, charset, body = load(session, deadline, *request)
            if kind not in ("text/html", "application/xhtml+xml") or not body.strip():
                return {"url": shown or url, "title": "", "text": body.decode(charset or "utf-8", errors="replace")}
            title, text = page_text(body, url, charset)
            if text or sent_on == ROUNDS or b"<script" not in body.lower():
                break
            request = challenge(session, url, body.decode(charset or "utf-8", errors="replace"))
            if request is None:
                break
            # Report the requested URL, not the bot check's.
            shown = shown or url
    return {"url": shown or url, "title": title, "text": text}


if __name__ == "__main__":
    try:
        json.dump(fetch(sys.argv[1]), sys.stdout, ensure_ascii=False, indent=1)
    except Unreadable as error:
        sys.exit(str(error))
