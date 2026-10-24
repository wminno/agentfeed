# AgentFeed — tiny content-extraction API for AI agents

One GET request turns any RSS/Atom feed into clean JSON, or any web page into
clean text/markdown. No SDK, no signup, no tracking. A live instance runs at
`https://agentfeed.bigfloppy.net`.


## MCP (Model Context Protocol)

`POST /mcp` is a streamable-HTTP MCP server exposing the same pipeline as two tools: `fetch_feed` (RSS/Atom -> JSON) and `fetch_article` (page -> text/Markdown). Point any MCP client (Claude Desktop, IDEs, agent frameworks) at `https://agentfeed.bigfloppy.net/mcp`; pass your API key via the `X-Api-Key` header. Same quotas and pricing as the REST API.

## Endpoints

```
GET /api/feed?url=<rss-or-atom-url>     # feed -> {title, link, published, summary}[]
GET /api/article?url=<page-url>         # page -> {title, author, text, markdown}
GET /api/health
```

```bash
curl 'https://agentfeed.bigfloppy.net/api/feed?url=https://hnrss.org/frontpage'
curl 'https://agentfeed.bigfloppy.net/api/article?url=https://blog.python.org/'
```

## Features

- **Agent-friendly JSON** — flat, predictable shapes; summaries truncated to 500 chars, article text capped at 20k chars.
- **Fair free tier** — 30 requests/min per IP, no key needed. Paid key: $5 per 100k requests/year (card, Stripe checkout, or USDC on Base with on-chain self-verification — no email, no accounts).
- **SSRF-safe outbound fetch** — 2 MB / 15 s caps, gzip handled.
- **Single process, stdlib server** — SQLite-free, stateless; only a small JSON file for key state.

## Run your own

```bash
pip install -r requirements.txt
python3 server.py          # serves on :5000
```

Edit `paid.py` to set your own wallet address and Stripe codes, or ignore that
file's payment logic entirely — the API works without keys on the free tier.

## License

MIT — see [LICENSE](LICENSE).
