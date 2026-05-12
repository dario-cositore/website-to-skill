#!/usr/bin/env python3
"""
website-to-skill web server (FastAPI)
Serves interactive tool at / and API at /generate
"""

import io
import json
import os
import re
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
from bs4 import BeautifulSoup
import trafilatura
from openai import OpenAI

# ── Config ──────────────────────────────────────────

load_dotenv = lambda: None
try:
    if Path(".env").exists():
        for line in Path(".env").read_text().strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
except Exception:
    pass

API_KEY = os.environ.get("OPENAI_API_KEY", "")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "deepseek/deepseek-v4-pro")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./generated"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DOMAIN = "dariocositore.com"

# ── App ──────────────────────────────────────────────

app = FastAPI(
    title="Website → Agent Skill",
    description="Convert any website into a Hermes Agent Skill and/or OpenClaw Agent Package.",
)

# Serve static frontend files (will be replaced by inline HTML for now)
# We serve index.html from root

# ── Crawler ──────────────────────────────────────────

class Crawler:
    def __init__(self, base_url, max_depth=2, max_pages=30, delay=0.5):
        self.base_url = base_url.rstrip("/")
        self.domain = urlparse(base_url).netloc
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.delay = delay
        self.visited = set()
        self.pages = []
        self.client = httpx.Client(
            timeout=30, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; WebsiteToSkillBot/1.0)"},
        )

    def is_crawlable(self, url):
        p = urlparse(url)
        if p.scheme not in ("http", "https"): return False
        if p.netloc != self.domain: return False
        skip = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js",
                ".zip", ".tar", ".gz", ".mp3", ".mp4", ".ico", ".woff", ".woff2", ".ttf")
        if any(url.lower().endswith(e) for e in skip): return False
        return True

    def extract_links(self, html, base):
        soup = BeautifulSoup(html, "lxml")
        links = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            full = urljoin(base, href).split("#")[0]
            if self.is_crawlable(full):
                links.add(full)
        return links

    def fetch_page(self, url):
        try:
            resp = self.client.get(url)
            if resp.status_code != 200: return None
            if "text/html" not in resp.headers.get("content-type", ""): return None
            html = resp.text
            soup = BeautifulSoup(html, "lxml")
            robots = soup.find("meta", attrs={"name": "robots"})
            if robots and "noindex" in robots.get("content", "").lower(): return None
            text = trafilatura.extract(html, include_links=False) or ""
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            desc = ""
            d = soup.find("meta", attrs={"name": "description"})
            if d: desc = d.get("content", "").strip()
            return {"url": url, "title": title, "description": desc, "content": text.strip(), "links": []}
        except Exception as e:
            return None

    def crawl(self, log_fn=None):
        queue = [(self.base_url, 0)]
        while queue and len(self.pages) < self.max_pages:
            url, depth = queue.pop(0)
            if url in self.visited: continue
            if depth > self.max_depth: continue
            self.visited.add(url)
            if log_fn: log_fn(f"Crawling [{depth}]: {url}")
            page = self.fetch_page(url)
            if page:
                self.pages.append(page)
                if depth < self.max_depth:
                    links = self.extract_links(
                        self.client.get(url).text if True else "", url
                    )
                    page["links"] = list(links)
                    for link in links:
                        if link not in self.visited:
                            queue.append((link, depth + 1))
            time.sleep(self.delay)

        # Now extract links from fetched pages for completeness
        for page in self.pages:
            try:
                resp = self.client.get(page["url"])
                page["links"] = list(self.extract_links(resp.text, page["url"]))
            except:
                pass
        return self.pages


# ── Analyzer ─────────────────────────────────────────

def analyze_site(pages, site_url, model_name):
    """LLM-powered site analysis"""
    if not API_KEY:
        return _fallback(pages, site_url)

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    page_summaries = []
    for p in pages:
        content = p["content"][:2500]
        page_summaries.append(f"URL: {p['url']}\nTitle: {p['title']}\n{content}\n---")

    prompt = f"""Analyze this website. Return JSON with:
{{
  "site_name": "...",
  "site_purpose": "...",
  "site_category": "saas-tool|ecommerce|documentation|blog|portfolio|api|other",
  "key_features": ["..."],
  "workflows": ["..."],
  "interaction_points": [{{"action":"...","how":"...","url":"..."}}],
  "agent_use_cases": ["..."],
  "seo_keywords": ["..."],
  "has_api": false,
  "auth_required": false
}}

CONTENT:{chr(10).join(page_summaries)}"""

    try:
        r = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role":"system","content":"Output only valid JSON."},
                {"role":"user","content":prompt},
            ],
            temperature=0.2,
            response_format={"type":"json_object"},
        )
        return json.loads(r.choices[0].message.content)
    except Exception as e:
        print(f"LLM analysis failed: {e}", file=sys.stderr)
        return _fallback(pages, site_url)


def _fallback(pages, site_url):
    netloc = urlparse(site_url).netloc
    return {
        "site_name": netloc,
        "site_purpose": f"Website at {site_url}",
        "site_category": "other",
        "key_features": [p["title"] for p in pages[:5]],
        "workflows": [],
        "interaction_points": [],
        "agent_use_cases": ["Browse and extract information"],
        "seo_keywords": [],
        "has_api": False,
        "auth_required": False,
    }


# ── Generator ────────────────────────────────────────

def safe_name(name):
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()

def generate_skill_md(analysis, pages, site_url):
    name = analysis.get("site_name", "Website")
    safe = safe_name(name)
    features = "\n".join(f"- {f}" for f in analysis.get("key_features", [])) or "- Browse and extract information"
    workflows = "\n".join(f"- {w}" for w in analysis.get("workflows", [])) or "- Navigate site pages\n- Extract content and data"
    use_cases = "\n".join(f"- {u}" for u in analysis.get("agent_use_cases", [])) or "- Browse the site and extract relevant information\n- Answer questions about the site's content"
    keywords = ", ".join(analysis.get("seo_keywords", [])[:10])

    page_table = ""
    for p in pages[:25]:
        page_table += f"| {p['title'][:60] or 'Untitled'} | {p['url']} |\n"

    points = analysis.get("interaction_points", [])
    interaction = ""
    if points:
        interaction = "### Interaction Points\n\n"
        for pt in points:
            interaction += f"**{pt.get('action','N/A')}**\n- How: {pt.get('how','N/A')}\n- URL: {pt.get('url','N/A')}\n\n"
    else:
        interaction = """### Browser Interaction Guide

Use Hermes browser tools to interact with the site:
- `browser_navigate(url)` — Navigate to a page
- `browser_snapshot(full=true)` — Extract full page content
- `browser_click(ref)` — Click elements
- `browser_type(ref, text)` — Fill forms

### Example Agent Prompt
```
You are an expert on {name}. Browse the site, extract information,
and help users with questions about its content and features.
```
"""

    return f"""# {name} — Hermes Agent Skill

<!-- SKILL_META_START -->
{{
  "name": "{safe}-skill",
  "description": "{analysis.get('site_purpose', f'Interact with {name}')}",
  "category": "{analysis.get('site_category', 'general')}",
  "version": "1.0.0",
  "author": "dario-cositore",
  "tags": ["{keywords}"],
  "created": "{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
  "url": "{site_url}"
}}
<!-- SKILL_META_END -->

## Overview

{analysis.get('site_purpose', f'Agent skill for interacting with {name}.')}

**Site:** {name}
**URL:** {site_url}
**Category:** {analysis.get('site_category', 'general')}
**Pages crawled:** {len(pages)}
**API available:** {"Yes" if analysis.get('has_api') else "No"}

## Key Features

{features}

## Supported Workflows

{workflows}

## Agent Use Cases

{use_cases}

## Interaction Guide

{interaction}

## Crawled Pages

| Title | URL |
|-------|-----|
{page_table}
"""


def generate_openclaw_package(analysis, pages, site_url):
    name = analysis.get("site_name", "Website Agent")
    safe = safe_name(name)
    workflows_str = ", ".join(analysis.get("workflows", [])[:5]) or "site browsing"

    pkg = f"""# Package: {safe}

**Version:** 1.0.0
**Type:** website-interaction
**Source:** {site_url}
**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}

## Description

{analysis.get('site_purpose', f'Agent package for interacting with {name}.')}

## Files

- `SOUL.md` — Agent personality
- `IDENTITY.md` — Agent identity
- `AGENTS.md` — Agent configuration
- `PACKAGE.md` — This file

## Setup

1. Copy to your OpenClaw agents folder
2. Review SOUL.md and IDENTITY.md
3. Configure in your agent runtime
"""

    soul = f"""# SOUL — {name}

You are an expert AI agent for **{name}** ({site_url}).

Your purpose: help users accomplish tasks on this site by browsing, extracting information, and executing workflows.

## Personality
- Professional, concise, helpful
- Always cite page URLs as sources
- Proactive — suggest related actions

## Constraints
- Never fabricate information not found on the actual site
- Distinguish site content from general knowledge
- Respect the site's navigation structure
"""

    identity = f"""# IDENTITY

**Name:** {name}
**Role:** AI-powered {analysis.get('site_category','website')} specialist
**Version:** 1.0.0
**Source URL:** {site_url}
**Created:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
**Author:** dario-cositore
"""

    agents = f"""# AGENTS

## {name} Agent
- **Role:** Browse and interact with {name}
- **Capabilities:**
{chr(10).join('  - ' + f for f in analysis.get('key_features',[])) or '  - Browse and extract site information'}
- **Persona:** SOUL.md
- **Identity:** IDENTITY.md

## Tools
- `browser_navigate`
- `browser_snapshot`
- `browser_click`
- `browser_type`
"""

    return {"PACKAGE.md": pkg, "SOUL.md": soul, "IDENTITY.md": identity, "AGENTS.md": agents}


# ── API Endpoints ───────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    return Path("index.html").read_text()


@app.post("/generate")
def generate_endpoint(
    url: str = Query(...),
    format: str = Query("hermes+openclaw"),
    provider: str = Query(""),
    model: str = Query(""),
    depth: int = Query(2),
    pages: int = Query(20),
):
    # Validate URL
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if not parsed.netloc:
        raise HTTPException(400, "Invalid URL")

    # Override config
    model_name = model or DEFAULT_MODEL
    if provider:
        global BASE_URL
        BASE_URL = provider

    output_dir = OUTPUT_DIR / safe_name(parsed.netloc) / datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    log_lines = []
    def log(msg):
        log_lines.append(msg)
        print(msg)

    # Step 1: Crawl
    log(f"🕷️ Crawling {url} (depth={depth}, max_pages={pages})")
    crawler = Crawler(url, max_depth=depth, max_pages=pages)
    pages_data = crawler.crawl(log_fn=log)

    if not pages_data:
        raise HTTPException(500, "No pages could be crawled. Check the URL.")

    log(f"✅ Crawled {len(pages_data)} pages")

    # Step 2: Analyze
    log("🧠 Analyzing with LLM...")
    site_map = {"root": url, "pages": [{"url": p["url"], "title": p["title"]} for p in pages_data]}
    analysis = analyze_site(pages_data, url, model_name)
    log(f"✅ Analysis: {analysis.get('site_name', 'Unknown')} — {analysis.get('site_category', '?')}")

    # Step 3: Generate
    log("📦 Generating skill package...")
    results = {}

    if format in ("hermes", "hermes+openclaw"):
        skill = generate_skill_md(analysis, pages_data, url)
        (output_dir / "SKILL.md").write_text(skill)

        # References
        (output_dir / "references").mkdir(exist_ok=True)
        (output_dir / "raw-pages").mkdir(exist_ok=True)

        site_map_md = f"# Site Map: {analysis.get('site_name', 'Site')}\n\n"
        site_map_md += f"**Root:** {url}\n\n"
        for i, p in enumerate(pages_data, 1):
            site_map_md += f"{i}. [{p['title']}]({p['url']})\n"
        (output_dir / "references" / "site-map.md").write_text(site_map_md)

        actions_md = "# Key Actions\n\n"
        for w in analysis.get("workflows", []):
            actions_md += f"- {w}\n"
        (output_dir / "references" / "key-actions.md").write_text(actions_md)

        (output_dir / "references" / "analysis.json").write_text(
            json.dumps(analysis, indent=2, ensure_ascii=False)
        )

        for p in pages_data:
            slug = safe_name(p["title"])[:50] or f"page-{pages_data.index(p)}"
            (output_dir / "raw-pages" / f"{slug}.md").write_text(
                f"# {p['title']}\n\nURL: {p['url']}\n\n---\n\n{p['content']}"
            )

        log("✅ Hermes skill generated")
        results["hermes"] = True

    if format in ("openclaw", "hermes+openclaw"):
        pkg_files = generate_openclaw_package(analysis, pages_data, url)
        for fname, content in pkg_files.items():
            (output_dir / fname).write_text(content)
        log("✅ OpenClaw package generated")
        results["openclaw"] = True

    # Create ZIP
    zip_name = f"{safe_name(analysis.get('site_name', 'site'))}_{int(time.time())}.zip"
    zip_path = OUTPUT_DIR / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(output_dir.rglob("*")):
            if f.is_file():
                arcname = f.relative_to(output_dir.parent)
                zf.write(f, arcname)

    log(f"✅ ZIP created: {zip_name}")

    return JSONResponse({
        "filename": zip_name,
        "format": format,
        "pages_crawled": len(pages_data),
        "site_name": analysis.get("site_name", "Unknown"),
        "results": results,
    })


@app.get("/download/{filename}")
def download(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=filename, media_type="application/zip")


# ── Run ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print(f"\n🚀 Website → Agent Skill server running at http://localhost:8888")
    print(f"   Output dir: {OUTPUT_DIR.resolve()}\n")
    uvicorn.run(app, host="0.0.0.0", port=8888)