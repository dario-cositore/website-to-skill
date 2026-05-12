#!/usr/bin/env python3
"""
website-to-skill web server (FastAPI)
Serves interactive tool at / and API at /generate

Pure heuristic analysis — no LLM required.
"""

import io
import json
import os
import re
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from collections import Counter

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import httpx
from bs4 import BeautifulSoup
import trafilatura

# ── Config ──────────────────────────────────────────

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./generated"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── CORS ─────────────────────────────────────────────
# Allow all origins so the tool works in any deployment scenario

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

app = FastAPI(
    title="Website → Agent Skill",
    description="Convert any website into a Hermes Agent Skill or OpenClaw Agent Package.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Heuristic Analyzer ──────────────────────────────

CATEGORY_PATTERNS = {
    "documentation": ["/docs/", "/doc/", "/documentation/", "/api/", "/reference/", "/guide/", "/tutorial/"],
    "ecommerce":     ["/shop/", "/store/", "/product/", "/cart/", "/checkout/", "/buy/", "/pricing/"],
    "blog":          ["/blog/", "/post/", "/article/", "/news/", "/journal/", "/archive/"],
    "saas":          ["/dashboard/", "/app/", "/login/", "/signup/", "/account/", "/settings/", "/workspace/", "/team/"],
    "portfolio":     ["/portfolio/", "/projects/", "/work/", "/about/", "/resume/"],
    "community":     ["/forum/", "/discuss/", "/community/", "/groups/", "/chat/"],
    "landing":       [],
}

WORKFLOW_PATTERNS = {
    "search":       ["search", "q", "query", "find", "explore", "lookup"],
    "signup":       ["signup", "register", "create account", "get started", "join", "sign up"],
    "login":        ["login", "sign in", "log in", "authenticate"],
    "contact":      ["contact", "get in touch", "reach out", "message us", "support"],
    "pricing":      ["pricing", "plans", "subscribe", "buy", "upgrade", "billing"],
    "download":     ["download", "get", "install", "export", "save"],
    "upload":       ["upload", "submit", "add file", "attach", "import"],
    "checkout":     ["checkout", "cart", "buy now", "purchase", "order"],
    "dashboard":    ["dashboard", "analytics", "overview", "stats", "reports"],
    "settings":     ["settings", "preferences", "profile", "account", "config"],
    "booking":      ["book", "schedule", "appointment", "reserve", "calendly"],
    "social":       ["follow", "share", "subscribe", "like", "comment"],
    "search_filter":["filter", "sort", "refine", "narrow", "category"],
}

FEATURE_SELECTORS = {
    "live_chat":        ["chat", "intercom", "drift", "chatbot", "live chat", "zendesk chat"],
    "email_signup":     ["subscribe", "newsletter", "email signup", "mailing list"],
    "video_content":    ["video", "watch", "youtube", "vimeo", "player"],
    "image_gallery":    ["gallery", "lightbox", "carousel", "swiper"],
    "maps_integration": ["map", "location", "directions"],
    "social_feeds":     ["twitter-timeline", "instagram-feed", "social-feed"],
    "payment_form":     ["pay", "credit card", "checkout", "stripe", "paypal"],
}

STOP_WORDS = set("the a an and or but in on at to for of is it this that was are were be been being have has had do does did will would shall should may might can could with from by as into through during before after above below between out off over under again further then once here there when where why how all each every both few more most other some such no nor not only own same so than too very s t d ll m re ve y ain t don shouldn wouldn isn aren wasn weren hasn haven hadn ma shan".split())


def analyze_heuristic(pages, site_url):
    """Pure heuristic site analysis — no LLM required."""

    url_parsed = urlparse(site_url)
    domain = url_parsed.netloc.replace("www.", "")

    # ── 1. Classify site ────────────────────────────
    all_paths = [urlparse(p["url"]).path for p in pages]
    category_scores = Counter()
    for cat, patterns in CATEGORY_PATTERNS.items():
        for pattern in patterns:
            matches = sum(1 for p in all_paths if pattern in p)
            if matches:
                category_scores[cat] += matches

    if category_scores:
        site_category = category_scores.most_common(1)[0][0]
    elif pages:
        html_sigs = {"saas": 0, "ecommerce": 0, "blog": 0, "documentation": 0}
        for p in pages:
            text = p.get("content", "").lower()
            if any(w in text for w in ["login", "signup", "dashboard", "workspace"]):
                html_sigs["saas"] += 1
            if any(w in text for w in ["add to cart", "buy now", "price", "checkout"]):
                html_sigs["ecommerce"] += 1
            if any(w in text for w in ["posted on", "published", "author", "tags", "comments"]):
                html_sigs["blog"] += 1
            if any(w in text for w in ["api", "installation", "usage", "parameters", "returns"]):
                html_sigs["documentation"] += 1
        site_category = max(html_sigs, key=html_sigs.get) if any(html_sigs.values()) else "landing"
    else:
        site_category = "landing"

    # ── 2. Detect features ──────────────────────────
    detected_features = []
    for feat, keywords in FEATURE_SELECTORS.items():
        for p in pages:
            content_lower = p.get("content", "").lower()
            if any(kw in content_lower for kw in keywords):
                detected_features.append(feat)
                break

    # ── 3. Detect workflows ─────────────────────────
    detected_workflows = []
    for wf, keywords in WORKFLOW_PATTERNS.items():
        for p in pages:
            text = p.get("content", "").lower()
            title = p.get("title", "").lower()
            url = p.get("url", "").lower()
            for kw in keywords:
                if kw in text or kw in title or kw.split()[0] in url:
                    detected_workflows.append(wf)
                    break
        if wf in detected_workflows:
            break
    seen = set()
    deduped = []
    for w in detected_workflows:
        if w not in seen:
            seen.add(w)
            deduped.append(w)
    detected_workflows = deduped

    # ── 4. Extract interaction points ───────────────
    interaction_points = []
    seen_actions = set()
    for p in pages[:10]:
        url = p["url"]
        content_lower = p.get("content", "").lower()
        if "form" in content_lower and "submit form" not in seen_actions:
            interaction_points.append({
                "action": "Submit form",
                "how": "HTML form on this page — may include contact, signup, or search forms",
                "url": url
            })
            seen_actions.add("submit form")
        if any(kw in (p.get("title","") + content_lower) for kw in ["search", "find", "lookup", "explore"]) and "search content" not in seen_actions:
            interaction_points.append({
                "action": "Search content",
                "how": "Search input or filter functionality detected",
                "url": url
            })
            seen_actions.add("search content")
        http_links = [w for w in content_lower.split() if w.startswith("http")]
        if http_links and "follow external links" not in seen_actions:
            interaction_points.append({
                "action": "Follow external links",
                "how": "Page contains links to external resources",
                "url": url
            })
            seen_actions.add("follow external links")
    interaction_points = interaction_points[:10]

    # ── 5. Detect agent use cases ───────────────────
    use_cases = [f"Browse and extract information from {domain}"]
    if len(pages) > 3:
        use_cases.append(f"Monitor content changes on {domain}")
    use_cases.append(f"Answer questions about {domain}'s content and features")
    if "blog" in site_category:
        use_cases.append("Summarize latest blog posts and topics")
    if "documentation" in site_category:
        use_cases.append("Look up API documentation and usage examples")
    if "ecommerce" in site_category:
        use_cases.append("Browse products, compare options, and find pricing")
    if "saas" in site_category:
        use_cases.append("Help users navigate the SaaS platform features")
    if "search" in detected_workflows:
        use_cases.append("Perform searches and filter results for users")

    # ── 6. SEO keywords (TF-IDF-lite) ──────────────
    all_content = " ".join(p.get("content", "") for p in pages).lower()
    words = re.findall(r"[a-z][a-z'-]{2,}", all_content)
    word_counts = Counter(w for w in words if w not in STOP_WORDS and len(w) > 2)
    total = sum(word_counts.values()) or 1
    scored = {w: (count / total) * min(len(w), 10) for w, count in word_counts.most_common(100)}
    seo_keywords = [w for w, _ in sorted(scored.items(), key=lambda x: -x[1])[:15]]

    # ── 7. Title and purpose ────────────────────────
    site_name = domain.title()
    for p in pages:
        title = p.get("title", "").strip()
        if title and len(title) < 80 and not title.lower().startswith("domain"):
            site_name = title
            break

    purpose_map = {
        "blog": f"Blog publishing content about {seo_keywords[0] if seo_keywords else 'various topics'}",
        "ecommerce": "Ecommerce store selling products online",
        "documentation": "Documentation and reference for developers",
        "saas": f"SaaS application for {seo_keywords[0] if seo_keywords else 'productivity'}",
        "portfolio": "Personal portfolio showcasing work and projects",
        "community": "Community platform for discussion",
        "landing": f"Landing page for {domain}",
    }
    site_purpose = purpose_map.get(site_category, f"Website at {site_url}")

    # ── 8. Key features ────────────────────────────
    feat_labels = {
        "live_chat": "Live chat / customer support widget",
        "email_signup": "Email subscription / signup forms",
        "video_content": "Video content embedding",
        "image_gallery": "Image gallery / carousel",
        "maps_integration": "Interactive maps / location display",
        "social_feeds": "Social media feed integration",
        "payment_form": "Online payment processing",
    }
    features = [feat_labels[f] for f in detected_features if f in feat_labels]

    wf_labels = {
        "signup": "User account creation / signup flow",
        "login": "User authentication / login",
        "dashboard": "Dashboard with analytics and metrics",
        "contact": "Contact / support form",
        "pricing": "Pricing plans and subscription tiers",
        "download": "File / content download capability",
        "upload": "Content upload / form submission",
        "checkout": "Shopping cart and checkout flow",
        "settings": "Account settings and preferences",
        "booking": "Appointment / booking system",
        "social": "Social sharing and follow buttons",
        "search_filter": "Content filtering and sorting",
    }
    for wf in detected_workflows:
        if wf in wf_labels and wf_labels[wf] not in features:
            features.append(wf_labels[wf])

    if not features:
        features.append("Content browsing and navigation")

    return {
        "site_name": site_name,
        "site_purpose": site_purpose,
        "site_category": site_category,
        "key_features": features,
        "workflows": detected_workflows,
        "interaction_points": interaction_points,
        "agent_use_cases": use_cases,
        "seo_keywords": seo_keywords,
        "has_api": False,
        "auth_required": False,
        "pages_analyzed": len(pages),
        "analysis_method": "heuristic",
    }


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
        self.errors = []
        self.client = httpx.Client(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        )

    def is_crawlable(self, url):
        p = urlparse(url)
        if p.scheme not in ("http", "https"): return False
        if p.netloc != self.domain: return False
        skip = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js",
                ".zip", ".tar", ".gz", ".mp3", ".mp4", ".ico", ".woff", ".woff2", ".ttf",
                ".json", ".xml", ".txt", ".csv", ".xlsx", ".webp")
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
            if resp.status_code >= 400:
                self.errors.append(f"HTTP {resp.status_code} on {url}")
                return None
            ct = resp.headers.get("content-type", "")
            if "text/html" not in ct:
                self.errors.append(f"Non-HTML content ({ct}) on {url}")
                return None
            html = resp.text
            if len(html) < 200:
                self.errors.append(f"Page too small ({len(html)} bytes) on {url}")
                return None

            soup = BeautifulSoup(html, "lxml")
            robots = soup.find("meta", attrs={"name": "robots"})
            if robots:
                rcontent = robots.get("content", "").lower()
                if "noindex" in rcontent:
                    self.errors.append(f"noindex on {url}")
                    return None

            text = trafilatura.extract(html, include_links=False, include_tables=True) or ""
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            desc = ""
            d = soup.find("meta", attrs={"name": "description"})
            if d: desc = d.get("content", "").strip()
            return {"url": url, "title": title, "description": desc, "content": text.strip()}

        except httpx.TimeoutException:
            self.errors.append(f"Timeout on {url}")
            return None
        except httpx.ConnectError:
            self.errors.append(f"Connection refused on {url}")
            return None
        except httpx.TooManyRedirects:
            self.errors.append(f"Too many redirects on {url}")
            return None
        except Exception as e:
            self.errors.append(f"{type(e).__name__}: {str(e)[:100]} on {url}")
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
                    try:
                        resp = self.client.get(url)
                        links = self.extract_links(resp.text, url)
                        page["_links"] = list(links)
                        for link in links:
                            if link not in self.visited:
                                queue.append((link, depth + 1))
                    except Exception:
                        pass
            time.sleep(self.delay)
        return self.pages


# ── Generator ────────────────────────────────────────

def safe_name(name):
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()

def generate_skill_md(analysis, pages, site_url):
    name = analysis.get("site_name", "Website")
    safe = safe_name(name)
    features = "\n".join(f"- {f}" for f in analysis.get("key_features", [])) or "- Browse and extract information"
    workflows = "\n".join(f"- {w}" for w in analysis.get("workflows", [])) or "- Navigate site pages\n- Extract content and data"
    use_cases = "\n".join(f"- {u}" for u in analysis.get("agent_use_cases", [])) or "- Browse the site and extract relevant information"
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
**Analysis method:** Heuristic (rule-based)

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
    depth: int = Query(2),
    pages: int = Query(20),
):
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if not parsed.netloc:
        raise HTTPException(400, "Invalid URL")

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
        error_detail = "; ".join(crawler.errors) if crawler.errors else "Unknown error"
        raise HTTPException(502, f"No pages could be crawled. Errors: {error_detail}")

    log(f"✅ Crawled {len(pages_data)} pages")

    # Step 2: Heuristic Analysis
    log("🔍 Analyzing site structure...")
    site_map = {"root": url, "pages": [{"url": p["url"], "title": p["title"]} for p in pages_data]}
    analysis = analyze_heuristic(pages_data, url)
    log(f"✅ Analysis: {analysis.get('site_name', 'Unknown')} — {analysis.get('site_category', '?')}")

    # Step 3: Generate
    log("📦 Generating skill package...")
    results = {}

    if format in ("hermes", "hermes+openclaw"):
        skill = generate_skill_md(analysis, pages_data, url)
        (output_dir / "SKILL.md").write_text(skill)

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
        "site_category": analysis.get("site_category", "Unknown"),
        "crawl_errors": crawler.errors,
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
    print(f"   Output dir: {OUTPUT_DIR.resolve()}")
    print(f"   Analysis: Pure heuristic (no LLM needed)")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8888)