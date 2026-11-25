// Solves JavaScript bot checks. Reads {"url", "html"} on stdin, runs the page's scripts with no
// network access, and prints the first navigation they trigger as
// {"method", "url", "body"?, "type"?, "cookie"?}, or {} if there is none within WAIT_MS.
// fetch.py calls this for pages that have scripts but no text.
//
// The scripts are untrusted. Deno runs this binary with no permissions except two env vars (see
// the Dockerfile), and that holds even if a script escapes happy-dom's VM. fetch.py makes the
// actual request.
import fs from "node:fs";
import process from "node:process";
import { Browser, HTMLFormElement } from "happy-dom";

const WAIT_MS = 5000;

// happy-dom returns the method attribute as written; the spec normalizes it to get, post or dialog.
Object.defineProperty(HTMLFormElement.prototype, "method", {
  get() {
    const method = (this.getAttribute("method") || "").toLowerCase();
    return ["post", "dialog"].includes(method) ? method : "get";
  },
});

const page = JSON.parse(fs.readFileSync(0, "utf8"));
let pageWindow = null;
let next = null;
let sent;
const answered = new Promise((resolve) => (sent = resolve));
const timeUp = new Promise((resolve) => setTimeout(resolve, WAIT_MS));

const browser = new Browser({
  settings: {
    enableJavaScriptEvaluation: true,
    // The sandbox is Deno's permissions, not happy-dom's VM.
    suppressInsecureJavaScriptEnvironmentWarning: true,
    disableJavaScriptFileLoading: true,
    disableCSSFileLoading: true,
    disableIframePageLoading: true,
    disableComputedStyleRendering: true,
    errorCapture: "processLevel",
    fetch: {
      interceptor: {
        // The first request loads the page: serve its HTML. Block every other request. happy-dom
        // opens a new window for a navigation, so the first request from any other window is the
        // result. Requests from the page's own window (fetch, beacons) are ignored.
        async beforeAsyncRequest({ request, window }) {
          if (!pageWindow) {
            pageWindow = window;
            return new window.Response(page.html, { headers: { "content-type": "text/html; charset=utf-8" } });
          }
          if (!next && window !== pageWindow) {
            next = { method: request.method, url: request.url, cookie: window.document.cookie || undefined };
            if (!["GET", "HEAD"].includes(request.method)) {
              next.type = request.headers.get("content-type") ?? undefined;
              // happy-dom always sends forms as multipart. Browsers use urlencoded when all
              // fields are text, and that's what sites expect.
              const fields = next.type?.startsWith("multipart/form-data") ? [...(await request.clone().formData())] : [];
              if (fields.length && fields.every(([, value]) => typeof value === "string")) {
                next.body = new URLSearchParams(fields).toString();
                next.type = "application/x-www-form-urlencoded";
              } else {
                next.body = await request.text();
              }
            }
            sent();
          }
          return new window.Response(null, { status: 204 });
        },
        beforeSyncRequest({ window }) {
          return new window.Response(null, { status: 204 });
        },
      },
    },
  },
});

try {
  const tab = browser.newPage();
  await tab.goto(page.url);
  // happy-dom doesn't fire DOMContentLoaded.
  tab.mainFrame.document.dispatchEvent(new tab.mainFrame.window.Event("DOMContentLoaded", { bubbles: true }));
  // Wait until the page is idle. If it has started navigating, wait for that request.
  await Promise.race([answered, tab.waitUntilComplete(), timeUp]);
  if (!next && tab.mainFrame.window !== pageWindow) await Promise.race([answered, timeUp]);
} catch {
  // If happy-dom crashes, report no navigation.
}
process.stdout.write(JSON.stringify(next ?? {}) + "\n");
process.exit(0);
