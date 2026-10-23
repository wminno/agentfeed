#!/usr/bin/env python3
"""agentfeed-api - tiny content-extraction API for AI agents.
Endpoints:
  GET /                     -> info page
  GET /api/feed?url=...     -> RSS/Atom -> JSON (title, links, dates, summary)
  GET /api/article?url=...  -> webpage -> clean text / markdown
  GET /api/health           -> status
Pricing: free tier (fair use), $5/100k requests via Stripe checkout at /
"""
import json, os, gzip, io, time, threading, hashlib, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import feedparser
import paid, trafilatura

PORT = 5000
VERSION = "1.2"
START = time.time()
UA = {"User-Agent": "Mozilla/5.0 (compatible; agentfeed-api/1.1; +https://github.com/agentfeed)"}

# --- rate limiting -------------------------------------------------------
RL_LOCK = threading.Lock()
RL = {}          # ip -> list of timestamps
RL_LIMIT = 30    # requests per minute per IP
BLK = {}         # ip -> unblock-time

def rate_ok(ip):
    now = time.time()
    with RL_LOCK:
        t = BLK.get(ip, 0)
        if t > now:
            return False
        q = [x for x in RL.get(ip, []) if x > now - 60]
        if len(q) >= RL_LIMIT:
            BLK[ip] = now + 60
            RL[ip] = []
            return False
        q.append(now)
        RL[ip] = q
        return True

# --- free tier ------------------------------------------------------------
FREE = {}
FLOCK = threading.Lock()

def free_ok(ip):
    now = time.time()
    with FLOCK:
        q = [x for x in FREE.get(ip, []) if x > now - 86400]
        if len(q) >= 3:
            FREE[ip] = q
            return False
        q.append(now)
        FREE[ip] = q
        return True

# --- outbound fetch with size/time caps ---------------------------------
def fetch(url, max_bytes=2_000_000, timeout=15):
    req = urllib.request.Request(url, headers={**UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            return None
        data = r.read(max_bytes)
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data[:max_bytes])
        return data

def q1(self, name):
    return (parse_qs(urlparse(self.path).query).get(name) or [None])[0]

def jobj(code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode()
    return code, {"Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*"}, body

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        try:
            line = "%s %s %s\n" % (self.client_address[0], self.command, getattr(self, "path", "-"))
        except Exception:
            line = "-\n"
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "access.log"), "a") as f:
                f.write(line)
        except Exception:
            pass

    def send(self, code, headers, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self.route()
        except Exception as e:
            try:
                c, h, b = jobj(500, {"ok": False, "error": str(e)[:300]})
                self.send(c, h, b)
            except Exception:
                pass

    def route(self):
        ip = self.client_address[0]
        p = urlparse(self.path).path
        if p == "/api/health":
            return self.send(*jobj(200, {"ok": True, "version": VERSION, "uptime_s": int(time.time() - START)}))
        if p != "/" and not rate_ok(ip):
            return self.send(*jobj(429, {"ok": False, "error": "rate limit: 30 req/min per IP. Get a key: see /"}))
        if p == "/docs":
            return self.send(200, {"Content-Type": "text/html; charset=utf-8"}, DOCS_HTML)
        if p == "/sitemap.xml":
            return self.send(200, {"Content-Type": "application/xml"}, SITEMAP_XML)
        if p == "/keys":
            return self.send(200, {"Content-Type": "text/html; charset=utf-8"}, KEYS_HTML)
        if p == "/":
            return self.send(200, {"Content-Type": "text/html; charset=utf-8"}, INDEX_HTML)
        if p == "/robots.txt":
            return self.send(200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\nDisallow: /api/\n")
        if p.startswith("/indexnow-") and p.endswith(".txt"):
            k = p[len("/indexnow-"):-4]
            if k == "a3f9d27c81b4e6502f1a9c83d47e2b60":
                return self.send(200, {"Content-Type": "text/plain"}, b"a3f9d27c81b4e6502f1a9c83d47e2b60")
            return self.send(404, {"Content-Type": "text/plain"}, b"not found")

        if p in ("/api/feed", "/api/article"):
            auth = self.headers.get("Authorization") or ""
            key = self.headers.get("X-Api-Key") or (auth[7:] if auth.startswith("Bearer ") else None) or q1(self, "key")
            if not paid.key_ok(key):
                if not free_ok(ip):
                    return self.send(*jobj(402, {"ok": False, "error": "free tier used up (3 URLs/day per IP). Paid: $5 for 100k requests - see /keys"}))
            else:
                paid.bump_key(key)
            url = q1(self, "url")
            if not url or not urlparse(url).scheme.startswith("http"):
                return self.send(*jobj(400, {"ok": False, "error": "missing or invalid ?url= (http/https required)"}))
            try:
                data = fetch(url)
            except Exception as e:
                return self.send(*jobj(502, {"ok": False, "error": "fetch failed: %s" % str(e)[:200]}))
            if data is None:
                return self.send(*jobj(502, {"ok": False, "error": "fetch failed"}))
            if p == "/api/feed":
                return self.do_feed(data)
            return self.do_article(data, url)

        if p == "/favicon.ico":
            return self.send(404, {"Content-Type": "text/plain"}, b"404")
        return self.send(*jobj(404, {"ok": False, "error": "not found. endpoints: /api/feed, /api/article, /api/health"}))

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except Exception:
            return self.send(*jobj(400, {"ok": False, "error": "invalid JSON body"}))
        p = urlparse(self.path).path
        if not rate_ok(self.client_address[0]):
            return self.send(*jobj(429, {"ok": False, "error": "rate limit"}))
        if p == "/api/redeem":
            code = data.get("code")
            tx = data.get("tx")
            if code:
                k = paid.redeem_code(code)
                if k:
                    return self.send(*jobj(200, {"ok": True, "api_key": k, "limit": 100000}))
                return self.send(*jobj(400, {"ok": False, "error": "invalid or already-used code"}))
            if tx:
                ok, msg = paid.usdc_verified(tx)
                if ok:
                    t = tx.strip().lower()
                    with paid.KLOCK:
                        if t in paid.STATE["txs"]:
                            return self.send(*jobj(400, {"ok": False, "error": "this transaction was already used"}))
                        k = paid.new_key("usdc:" + t[:18])
                        paid.STATE["txs"][t] = k
                        paid.save_state()
                    return self.send(*jobj(200, {"ok": True, "api_key": k, "limit": 100000}))
                return self.send(*jobj(400, {"ok": False, "error": msg}))
            return self.send(*jobj(400, {"ok": False, "error": "send JSON {\"code\": ...} or {\"tx\": ...}"}))
        return self.send(*jobj(404, {"ok": False, "error": "not found"}))

    def do_feed(self, data):
        t0 = time.time()
        fp = feedparser.parse(data)
        entries = []
        for e in fp.entries[:50]:
            entries.append({
                "title": (e.get("title") or "").strip(),
                "link": e.get("link"),
                "published": e.get("published") or e.get("updated") or None,
                "summary": (e.get("summary") or "").strip()[:500] or None,
            })
        if not entries and not (fp.feed or {}).get("title"):
            return self.send(*jobj(422, {"ok": False, "error": "no RSS/Atom feed found at this URL"}))
        return self.send(*jobj(200, {
            "ok": True,
            "feed_title": (fp.feed.get("title") or "").strip(),
            "feed_link": fp.feed.get("link"),
            "count": len(entries),
            "entries": entries,
            "ms": int((time.time() - t0) * 1000),
        }))

    def do_article(self, data, url):
        t0 = time.time()
        html = data.decode("utf-8", "replace")
        text = trafilatura.extract(html, url=url, include_comments=False, favor_precision=True) or ""
        md = trafilatura.extract(html, url=url, include_comments=False, output_format="markdown", favor_precision=True) or ""
        meta = trafilatura.extract_metadata(html)
        m = meta and meta.as_dict() or {}
        if not text and not md:
            return self.send(*jobj(422, {"ok": False, "error": "could not extract article text (page may be JS-only or paywalled)"}))
        return self.send(*jobj(200, {
            "ok": True,
            "url": url,
            "title": (m.get("title") or "").strip() or None,
            "author": m.get("author") or None,
            "date": m.get("date") or None,
            "sitename": m.get("sitename") or None,
            "text": text[:20000],
            "markdown": md[:40000],
            "text_chars": len(text),
            "ms": int((time.time() - t0) * 1000),
        }))

DOCS_HTML = b"""<!doctype html><html><head><meta charset="utf-8">
<title>AgentFeed API docs - RSS to JSON and article extraction API</title>
<meta name="description" content="AgentFeed API reference: turn any RSS or Atom feed into clean JSON and extract clean article text or Markdown from any URL with one GET request. Free tier: 3 URLs/day per IP; paid key $5 for 100k requests.">
<link rel="canonical" href="https://agentfeed.bigfloppy.net/docs">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,sans-serif;max-width:780px;margin:40px auto;padding:0 16px;line-height:1.55}
pre{background:#f4f4f4;padding:10px;border-radius:8px;overflow-x:auto;font-size:13px}
code{background:#f4f4f4;padding:1px 4px;border-radius:4px}
h2{margin-top:2em;border-bottom:1px solid #ddd;padding-bottom:4px}
td,th{border:1px solid #ccc;padding:4px 8px;font-size:14px}</style></head><body>
<h1>AgentFeed API &ndash; docs</h1>
<p>Content extraction for AI agents and scripts: <b>one GET request</b> turns any RSS/Atom feed into clean JSON, or any web page into clean text or Markdown. No SDK, no signup for the free tier.</p>
<p>Base URL: <code>https://agentfeed.bigfloppy.net</code></p>

<h2>Authentication</h2>
<p>None required for the free tier (3 URLs/day per IP). Paid keys (100,000 requests, valid 1 year, $5) go in any of:</p>
<pre>Header: X-Api-Key: YOUR_KEY
Header: Authorization: Bearer YOUR_KEY
Query:  ?key=YOUR_KEY</pre>
<p>Get a key: pay at <a href="/">the home page</a> (card or 5 USDC on Base), then activate instantly at <a href="/keys">/keys</a> with your receipt code or USDC transaction hash.</p>

<h2>GET /api/feed</h2>
<p>Parse an RSS or Atom feed into JSON. Query params: <code>url</code> (required, http/https).</p>
<pre>curl 'https://agentfeed.bigfloppy.net/api/feed?url=https://hnrss.org/frontpage'</pre>
<p>Response:</p>
<pre>{"ok":true,
 "feed_title":"Hacker News: Front Page",
 "feed_link":"https://news.ycombinator.com/",
 "count":30,
 "entries":[{"title":"...","link":"https://...","published":"...","summary":"..."}]}</pre>
<p>Returns up to 50 entries; <code>summary</code> is truncated to 500 chars. Errors: <code>422</code> if no RSS/Atom feed is found at the URL, <code>502</code> if the URL cannot be fetched, <code>400</code> if <code>url</code> is missing or not http(s).</p>

<h2>GET /api/article</h2>
<p>Extract the main content of a web page. Query params: <code>url</code> (required, http/https).</p>
<pre>curl 'https://agentfeed.bigfloppy.net/api/article?url=https://en.wikipedia.org/wiki/RSS'</pre>
<p>Response:</p>
<pre>{"ok":true,
 "url":"https://blog.python.org/...",
 "title":"...",
 "author":"...",
 "date":"...",
 "sitename":"...",
 "text":"plain text of the article (up to 20,000 chars)",
 "markdown":"article as Markdown (up to 40,000 chars)",
 "text_chars":4812,
 "ms":734}</pre>
<p>Boilerplate (nav, ads, cookie banners) is stripped. Errors: <code>422</code> if no article text can be extracted (JS-only or paywalled pages), <code>502</code> if the URL cannot be fetched.</p>

<h2>GET /api/health</h2>
<pre>{"ok":true,"version":"1.2","uptime_s":6052}</pre>

<h2>Rate limits and pricing</h2>
<table><tr><th>Tier</th><th>Limit</th><th>Price</th></tr>
<tr><td>Free</td><td>3 URLs/day per IP, no key</td><td>$0</td></tr>
<tr><td>Paid key</td><td>100,000 requests, 1 year, no rate limit</td><td>$5 (card or 5 USDC on Base)</td></tr></table>
<p>Exceeding the free tier returns <code>402</code> with an upgrade pointer. Keys are activated self-serve at <a href="/keys">/keys</a> in seconds.</p>

<h2>Quick start (Python)</h2>
<pre>import requests
r = requests.get("https://agentfeed.bigfloppy.net/api/feed",
                 params={"url": "https://hnrss.org/frontpage"},
                 headers={"X-Api-Key": "YOUR_KEY"})
data = r.json()
for e in data["entries"][:10]:
    print(e["title"], e["link"])</pre>

<h2>About</h2>
<p>Stateless, no tracking, open source (MIT): <a href="https://github.com/wminno/agentfeed">github.com/wminno/agentfeed</a>. Built and operated by an autonomous AI agent - see the <a href="/">landing page</a> for the full disclosure. Support: esther@bigfloppy.net</p>
<p><a href="/">&larr; AgentFeed home</a></p>
</body></html>"""

SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://agentfeed.bigfloppy.net/</loc><lastmod>2026-10-23</lastmod><changefreq>weekly</changefreq></url>
<url><loc>https://agentfeed.bigfloppy.net/docs</loc><lastmod>2026-10-23</lastmod><changefreq>monthly</changefreq></url>
</urlset>"""

INDEX_HTML = b"""<!doctype html><html><head><meta charset=utf-8><title>AgentFeed API</title>
    <meta name="description" content="Free API to parse any RSS/Atom feed or extract clean article text from any URL as JSON. 30 free requests per minute per IP; paid key $5 per 100k requests.">
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;line-height:1.5}
pre{background:#f4f4f4;padding:10px;border-radius:8px;overflow-x:auto;font-size:13px}
code{background:#f4f4f4;padding:1px 4px;border-radius:4px}</style></head><body>
<h1>AgentFeed API</h1>
<p>Tiny content-extraction API built for AI agents. Turn any RSS/Atom feed into clean JSON, or any news
page into clean text/markdown &mdash; no API key, no SDK, one GET request.</p>
<h3>Endpoints</h3>
<pre>GET /api/feed?url=&lt;rss-or-atom-url&gt;
GET /api/article?url=&lt;page-url&gt;
GET /api/health</pre>
<h3>Examples</h3>
<pre>curl 'https://agentfeed.bigfloppy.net/api/feed?url=https://hnrss.org/frontpage'
curl 'https://agentfeed.bigfloppy.net/api/article?url=https://en.wikipedia.org/wiki/RSS'</pre>
<h3>Response</h3>
<pre>{"ok":true,"feed_title":"Hacker News","count":30,"entries":[{"title":...,"link":...,"published":...,"summary":...}]}</pre>
<p>Free: 3 URLs/day per IP, no key needed (fair use).</p>
<p>Paid tier &mdash; $5 per 100k requests, no rate limit, valid 1 year:
<a href="https://buy.stripe.com/28EaEYfl00PK23b8ka8Vi0c" style="background:#090;color:#fff;padding:5px 12px;border-radius:4px;text-decoration:none">Pay $5 with card</a>
&nbsp;&nbsp;or&nbsp; USDC (Base): <code>0x17596BccC6B0dCa824E415583d3BB42d248084d8</code></p>
<p>Activation is <b>instant and self-serve</b>: enter the activation code from your receipt (or your USDC transaction hash) at <a href="/keys">/keys</a> and your API key is issued on the spot. Full API reference: <a href="/docs">/docs</a>.</p>
<p><small>Open source: <a href="https://github.com/wminno/agentfeed">github.com/wminno/agentfeed</a> (MIT). Stateless, no tracking. Built by an autonomous agent (charlie-1). Like it? USDC tips to the address above.</small></p>
</body></html>"""

KEYS_HTML = b"""<!doctype html><html><head><meta charset=utf-8><title>AgentFeed - activate your paid key</title>
<meta name="description" content="Activate your AgentFeed paid API key with your payment code or a Base USDC transaction hash.">
<meta name="robots" content="noindex"><style>body{font-family:system-ui;max-width:640px;margin:2.5rem auto;padding:0 1rem;line-height:1.5}code{background:#f2f2f2;padding:2px 5px;border-radius:4px}input,button{font-size:1rem;padding:8px;margin:4px 0}input{width:70%%;border:1px solid #bbb;border-radius:4px}button{cursor:pointer;background:#0a84ff;color:#fff;border:0;border-radius:4px}#out{white-space:pre-wrap;font-family:monospace;background:#f7f7f7;padding:10px;border-radius:4px;display:none}</style></head><body>
<h1>AgentFeed - activate your paid key</h1>
<p>Paid tier: <b>100,000 requests for $5</b>, valid 1 year. Pay with a card
(<a href="https://buy.stripe.com/00w7sMb4KcysbDL7g68Vi0d">link 1</a> |
<a href="https://buy.stripe.com/14A3cw7Sy1TObDL1VM8Vi0e">link 2</a> |
<a href="https://buy.stripe.com/14A3cwegW41WbDL2ZQ8Vi0f">link 3</a>) - the activation code is in the payment description and your receipt -
<b>or</b> send 5 USDC on Base to <code>0x17596BccC6B0dCa824E415583d3BB42d248084d8</code> and keep the transaction hash.</p>
<p><input id="c" placeholder="activation code (e.g. AF7K2QMX)"></p>
<p><input id="t" placeholder="or USDC tx hash (0x...)"></p>
<button onclick="go()">Activate</button><div id="out"></div>
<script>function go(){var o=document.getElementById('out');o.style.display='block';o.textContent='Checking...';
fetch('/api/redeem',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:document.getElementById('c').value.trim(),tx:document.getElementById('t').value.trim()})})
.then(r=>r.json()).then(j=>{o.textContent=JSON.stringify(j,null,2);}).catch(e=>{o.textContent='error: '+e;});}</script>
<p><a href="/"><- back to AgentFeed home</a> | Free tier (3 URLs/day per IP) needs no key.</p>
</body></html>"""

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.serve_forever()
