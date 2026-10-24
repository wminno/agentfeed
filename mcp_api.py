#!/usr/bin/env python3
"""MCP (Model Context Protocol) endpoint for AgentFeed.

POST /mcp  - streamable-HTTP JSON-RPC: initialize / tools/list / tools/call.
Exposes the same extraction pipeline as /api/feed and /api/article as MCP tools
(fetch_feed, fetch_article) so MCP clients (Claude Desktop, IDEs, agent
frameworks) can pull clean feed/article JSON directly.

Auth: same keys as the REST API (X-Api-Key / Authorization: Bearer / ?key=).
With a valid key, calls are billed to the key; without one, the standard free
tier (3 URLs/day per IP) applies. No new auth mechanism, no new pricing.
"""
import json
import time

import feedparser
import trafilatura

import paid

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "agentfeed", "version": "1.4"}

TOOLS = [
    {
        "name": "fetch_feed",
        "description": ("Fetch an RSS or Atom feed and return clean JSON: feed title/link plus up to 50 "
                        "entries with title, link, published date and summary. Works with any feed URL."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "HTTP(S) URL of the RSS/Atom feed"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "fetch_article",
        "description": ("Extract the main article text and Markdown from a web page URL: title, author, "
                        "date, site name, plain text (up to 20k chars) and Markdown (up to 40k chars)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "HTTP(S) URL of the article page"},
            },
            "required": ["url"],
        },
    },
]


def _rpc_error(cid, code, message):
    return {"jsonrpc": "2.0", "id": cid, "error": {"code": code, "message": message}}


def _quota_ok(key, ip):
    if key and paid.key_ok(key):
        return True
    import server as _srv  # lazy: avoid circular import at module load
    return _srv.free_ok(ip)


def _fetch(url):
    import server as _srv
    import urllib.request as _u
    r = _u.Request(url, headers={**_srv.UA, "Accept-Encoding": "gzip"})
    with _u.urlopen(r, timeout=15) as resp:
        if resp.status != 200:
            return None
        data = resp.read(2_000_000)
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            data = gzip.decompress(data[:2_000_000])
        return data


def run_feed(url):
    data = _fetch(url)
    if data is None:
        return 502, {"ok": False, "error": "fetch failed (non-200 or empty response)"}
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
        return 422, {"ok": False, "error": "no RSS/Atom feed found at this URL"}
    return 200, {
        "ok": True,
        "feed_title": (fp.feed.get("title") or "").strip(),
        "feed_link": fp.feed.get("link"),
        "count": len(entries),
        "entries": entries,
        "ms": int((time.time() - t0) * 1000),
    }


def run_article(url):
    data = _fetch(url)
    if data is None:
        return 502, {"ok": False, "error": "fetch failed (non-200 or empty response)"}
    t0 = time.time()
    html = data.decode("utf-8", "replace")
    text = trafilatura.extract(html, url=url, include_comments=False, favor_precision=True) or ""
    md = trafilatura.extract(html, url=url, include_comments=False, output_format="markdown", favor_precision=True) or ""
    meta = trafilatura.extract_metadata(html)
    m = meta and meta.as_dict() or {}
    if not text and not md:
        return 422, {"ok": False, "error": "could not extract article text (page may be JS-only or paywalled)"}
    return 200, {
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
    }


def _call_tool(name, args, key, ip):
    url = (args or {}).get("url") or ""
    if not url.startswith(("http://", "https://")):
        return 400, {"ok": False, "error": "missing or invalid 'url' (http/https required)"}
    if not _quota_ok(key, ip):
        return 402, {"ok": False, "error": ("quota used up (free tier 3 URLs/day per IP). "
                                            "Get a free trial key (50 req/day, no email): "
                                            "curl https://agentfeed.bigfloppy.net/api/trial")}
    if key and paid.key_ok(key):
        paid.bump_key(key)
    if name == "fetch_feed":
        return run_feed(url)
    return run_article(url)


def handle(body, key=None, ip="-"):
    """Handle one streamable-HTTP MCP POST. Returns (http_code, json_dict)."""
    try:
        req = json.loads(body.decode("utf-8"))
    except Exception:
        return 400, _rpc_error(None, -32700, "parse error: body is not valid JSON")
    if isinstance(req, list):  # batch
        return 200, [handle(json.dumps(r).encode(), key=key, ip=ip)[1] for r in req][:10]

    cid = req.get("id")
    method = req.get("method", "")
    if method == "initialize":
        return 200, {
            "jsonrpc": "2.0", "id": cid,
            "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "notifications/initialized":
        return 202, {}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": cid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = req.get("params") or {}
        name = p.get("name")
        if name not in ("fetch_feed", "fetch_article"):
            return 200, _rpc_error(cid, -32602, "unknown tool: %s" % name)
        code, obj = _call_tool(name, p.get("args") or p.get("arguments") or {}, key, ip)
        if code == 200:
            result = {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}],
                      "isError": False}
        else:
            result = {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}],
                      "isError": True}
        return 200, {"jsonrpc": "2.0", "id": cid, "result": result}
    return 400, _rpc_error(cid, -32601, "method not found: %s" % method)
