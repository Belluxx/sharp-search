"""Fetch a web page and convert it to Markdown: {"url", "title", "text"}.

Only public IPs are reached: the host and every redirect target are resolved and checked, and curl
is pinned to the checked IP. A page with scripts but no text is treated as a JavaScript bot check:
challenge.mjs runs its scripts offline, and the fetch follows the navigation they trigger.
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
# Seconds for a whole fetch, not for each request.
TIMEOUT = 30
MAX_ADDRESS = 500
CHALLENGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "challenge")
ROUNDS = 2
# A page with scripts, less text and fewer links than this may be a bot check that answers like a
# page. Short pages and JavaScript apps look the same.
SPARSE = 1000
SPARSE_LINKS = 10
CHALLENGE_SECONDS = 10
# RLIMIT_DATA for challenge.mjs. It reserves about 370 MB at start, so this leaves it about 400 MB.
CHALLENGE_KB = 768 << 10
NAT64 = ipaddress.ip_network("64:ff9b::/96")

DROPPED = {
    "script", "style", "noscript", "svg", "canvas", "iframe", "object", "head", "nav", "footer", "aside", "form", "button", "select",
}
DROPPED_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCKS = {
    "address", "article", "blockquote", "details", "div", "dl", "dd", "dt", "fieldset", "figcaption", "figure", "header", "hr", "main",
    "ol", "p", "section", "summary", "table", "ul",
}

# React's streaming SSR moves late content into place with $RC(boundary, content) and
# $RS(segment, placeholder).
STREAMED = re.compile(r'\$(RC|RS)\("([^"]+)","([^"]+)"\)')


class Unreadable(Exception):
    """An error message for the agent, when a page can't be read."""


def inline(text):
    return re.sub(r"\s+", " ", text or "")


def render(node, base, out):
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
        if label and href.startswith(("http://", "https://")) and urldefrag(href)[0] != urldefrag(base)[0] and len(href) <= MAX_ADDRESS:
            out[start:] = [f"[{label}]({href})"]

    if tag in BLOCKS or tag in HEADINGS:
        out.append("\n\n")


def move_into_place(source, anchor):
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
            # The loading fallback runs up to the matching <!--/$--> comment.
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
    lines, code = [], False
    for line in "".join(out).split("\n"):
        if line.strip() == "```":
            line, code = "```", not code
        lines.append(line.rstrip() if code else line.strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def page_text(body, base, charset):
    # lxml doesn't know every charset name that Python does.
    if charset:
        body = body.decode(charset, errors="replace").encode()
    document = lxml.html.fromstring(body, parser=lxml.html.HTMLParser(encoding="utf-8") if charset else None)
    title = document.findtext(".//title") or ""
    # lxml parses content after </html> as extra root elements.
    roots = [document, *document.itersiblings()]
    place_streamed(roots)
    text = text_of(roots, base)
    mains = document.xpath("//main") or document.xpath("//article")
    if len(mains) == 1:
        main = text_of(mains, base)
        if len(main) * 2 > len(text):
            text = main
    return inline(title).strip(), text


def links(body):
    """How many links lead to other pages, menus included. A bot check has next to none."""
    document = lxml.html.fromstring(body)
    addresses = [(a.get("href") or "").strip().lower() for root in (document, *document.itersiblings()) for a in root.iter("a")]
    return sum(bool(address) and not address.startswith(("#", "javascript:", "mailto:", "tel:")) for address in addresses)


def charset_of(response, body):
    """None leaves the detection to lxml, which assumes Latin-1 when the page doesn't declare one."""
    named = response.charset_encoding
    if named:
        try:
            return codecs.lookup(named).name
        except LookupError:
            pass
    try:
        # Truncating at MAX_BYTES may have split the last character.
        codecs.getincrementaldecoder("utf-8")().decode(body, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        return None


def public(ip):
    ip = ipaddress.ip_address(ip.split("%")[0])
    ip = getattr(ip, "ipv4_mapped", None) or ip
    if ip in NAT64:
        ip = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def pin(session, address):
    """Pin the session's next request to a checked IP, so that curl never does its own DNS lookup."""
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
    # Containers often have no IPv6 route.
    ip = min((info[4][0] for info in found), key=lambda ip: ":" in ip)
    if not public(ip):
        raise Unreadable(f"{parts.hostname} is not a public address")
    # curl_cffi doesn't build the curl_slist that CONNECT_TO takes.
    pinned = lib.curl_slist_append(ffi.NULL, f"::{f'[{ip}]' if ':' in ip else ip}:{port}".encode())
    session.curl_options = {CurlOpt.CONNECT_TO: ffi.gc(pinned, lib.curl_slist_free_all)}


def load(session, deadline, method, address, data=None, headers=None):
    """Redirects are followed here, not by curl, so that each target is checked."""
    for _ in range(MAX_REDIRECTS + 1):
        pin(session, address)
        # The timeout argument only limits connecting and stalling, so a slow steady response
        # would never time out. 0 would mean no limit.
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
        if response.status_code == 303 or (method == "POST" and response.status_code in (301, 302)):
            method, data, headers = "GET", None, None
    else:
        raise Unreadable("could not load the page: too many redirects")
    try:
        check_blocked(response.status_code)
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
        check_blocked(response.status_code, body)
    except Unreadable:
        raise
    except Exception as error:
        raise Unreadable(f"could not load the page: {str(error).split('. See ')[0]}")
    finally:
        response.close()
    return address, kind, charset_of(response, body), body


def run_scripts(address, html):
    """Return the navigation that the page's scripts trigger, {} if none, or None if the run fails."""
    # The shell becomes challenge.mjs, so that the timeout kills the right process.
    command = ["/bin/sh", "-c", f'ulimit -d {CHALLENGE_KB} && exec "$0"', CHALLENGE]
    try:
        answer = subprocess.run(command, input=json.dumps({"url": address, "html": html}).encode(), capture_output=True, timeout=CHALLENGE_SECONDS, check=True)
        step = json.loads(answer.stdout)
    except Exception:
        return None
    return step if isinstance(step, dict) else None


def challenge(session, address, html):
    step = run_scripts(address, html)
    if not step or not isinstance(step.get("url"), str) or not isinstance(step.get("method"), str):
        return None
    site = urlsplit(address)
    target = urlsplit(step["url"])
    if target.scheme not in ("http", "https") or target.netloc != site.netloc:
        return None
    for pair in (step.get("cookie") or "").split(";"):
        name, _, value = pair.strip().partition("=")
        if name:
            session.cookies.set(name, value, domain=site.hostname)
    return step["method"], step["url"], step.get("body"), {"content-type": step["type"]} if step.get("type") else None


def fetch_in(session, address, deadline):
    """Returns the page, and whether it may be a bot check that answers like a page."""
    request = ("GET", address)
    shown = None
    for sent_on in range(ROUNDS + 1):
        url, kind, charset, body = load(session, deadline, *request)
        if kind not in ("text/html", "application/xhtml+xml") or not body.strip():
            return {"url": shown or url, "title": "", "text": body.decode(charset or "utf-8", errors="replace")}, False
        title, text = page_text(body, url, charset)
        scripted = b"<script" in body.lower()
        if text or sent_on == ROUNDS or not scripted:
            break
        request = challenge(session, url, body.decode(charset or "utf-8", errors="replace"))
        if request is None:
            break
        # Report the requested URL, not the bot check's.
        shown = shown or url
    sparse = scripted and len(text) < SPARSE and links(body) < SPARSE_LINKS
    return {"url": shown or url, "title": title, "text": text}, sparse


# MARK: Fallbacks
#
# Workarounds for sites that block a fetch, found by trial against tests/bots.py. With FALLBACKS
# off (server.py --no-fallbacks) none of this applies.

FALLBACKS = True
# 202 and 405 are AWS WAF's challenge and CAPTCHA.
REFUSALS = {202, 401, 403, 405, 429}
# Anubis and Akamai, which answer 200.
BOT_CHECKS = (b'id="anubis_version"', b'id="sec-if-cpt-container"')
# The link previews of Slack and iMessage.
AGENTS = (
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_1) AppleWebKit/601.2.4 (KHTML, like Gecko) Version/9.0.1 Safari/601.2.4 facebookexternalhit/1.1 Facebot Twitterbot/1.0",
)


class Blocked(Unreadable):
    """The site answered with a bot check. Another user agent may get through."""


def check_blocked(status, body=b""):
    if FALLBACKS and status in REFUSALS:
        raise Blocked(f"could not load the page: HTTP {status}")
    if FALLBACKS and any(mark in body for mark in BOT_CHECKS):
        raise Blocked("could not load the page: blocked by a bot check")


def fetch(address):
    """Fetch `address` as {"url", "title", "text"}. Raises Unreadable on failure."""
    deadline = time.monotonic() + TIMEOUT
    page = refusal = None
    try:
        with requests.Session(impersonate="safari") as session:
            page, sparse = fetch_in(session, address, deadline)
        if not (FALLBACKS and sparse):
            return page
    except Blocked as error:
        refusal = error
    for agent in AGENTS:
        try:
            with requests.Session(headers={"user-agent": agent}) as session:
                other, _ = fetch_in(session, address, deadline)
        except Unreadable:
            continue
        # Some sites answer the agents they don't serve with an empty page.
        if len(other["text"]) > (len(page["text"]) if page else 0):
            return other
    if page:
        return page
    raise refusal


if __name__ == "__main__":
    try:
        json.dump(fetch(sys.argv[1]), sys.stdout, ensure_ascii=False, indent=1)
    except Unreadable as error:
        sys.exit(str(error))
