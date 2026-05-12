#!/usr/bin/env python3
"""
website_to_skill — Convert any website into a Hermes Agent Skill and/or OpenClaw Agent Package.

Usage:
    python website_to_skill.py --url https://example.com --format hermes+openclaw --output ./my-skill
    python website_to_skill.py --url https://example.com --format hermes --output ./skills/my-skill
    python website_to_skill.py --url https://example.com --provider openrouter --model deepseek/deepseek-v4-pro
"""

import argparse
import json
import os
import re
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Optional

import httpx
from bs4 import BeautifulSoup
import trafilatura
from openai import OpenAI


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

def load_config():
    """Load config from .env file or environment."""
    dotenv_path = Path(__file__).resolve().parent.parent / ".env"
    if dotenv_path.exists():
        for line in dotenv_path.read_text().strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    return {
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.environ.get("OPENAI_MODEL", "deepseek/deepseek-v4-pro"),
        "max_depth": int(os.environ.get("MAX_DEPTH", "2")),
        "max_pages": int(os.environ.get("MAX_PAGES", "30")),
        "request_delay": float(os.environ.get("REQUEST_DELAY", "1.0")),
    }


# ─────────────────────────────────────────────
# Crawler
# ─────────────────────────────────────────────

class WebsiteCrawler:
    def __init__(self, base_url: str, max_depth: int = 2, max_pages: int = 30, delay: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.domain = urlparse(base_url).netloc
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.delay = delay
        self.visited: set[str] = set()
        self.pages: list[dict] = []
        self.client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; WebsiteToSkillBot/1.0)"},
        )

    def is_same_domain(self, url: str) -> bool:
        return urlparse(url).netloc == self.domain

    def is_crawlable(self, url: str) -> bool:
        parsed = urlparse(url)
        # Skip non-http, fragments, mailto, etc.
        if parsed.scheme not in ("http", "https"):
            return False
        if not self.is_same_domain(url):
            return False
        # Skip binary/file extensions
        skip_exts = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js",
                      ".zip", ".tar", ".gz", ".mp3", ".mp4", ".wav", ".ico", ".woff",
                      ".woff2", ".ttf", ".eot", ".json", ".xml", ".txt", ".csv", ".xlsx")
        if any(url.lower().endswith(ext) for ext in skip_exts):
            return False
        return True

    def extract_links(self, html: str, base: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            full = urljoin(base, href)
            # Remove fragment
            full = full.split("#")[0]
            if self.is_crawlable(full):
                links.append(full)
        return list(set(links))

    def fetch_page(self, url: str) -> Optional[dict]:
        try:
            resp = self.client.get(url)
            if resp.status_code != 200:
                return None

            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type:
                return None

            html = resp.text

            # Check noindex
            soup = BeautifulSoup(html, "lxml")
            robots_meta = soup.find("meta", attrs={"name": "robots"})
            if robots_meta and "noindex" in robots_meta.get("content", "").lower():
                return None

            # Extract with trafilatura
            text = trafilatura.extract(html, include_links=False, include_tables=True) or ""
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            description = ""
            desc_tag = soup.find("meta", attrs={"name": "description"})
            if desc_tag:
                description = desc_tag.get("content", "").strip()

            return {
                "url": url,
                "title": title,
                "description": description,
                "content": text.strip(),
                "content_length": len(text.strip()),
                "links": self.extract_links(html, url),
            }
        except Exception as e:
            print(f"  ⚠ Failed to fetch {url}: {e}")
            return None

    def crawl(self) -> list[dict]:
        print(f"🕷️  Crawling {self.base_url} (max_depth={self.max_depth}, max_pages={self.max_pages})")
        queue = [(self.base_url, 0)]

        while queue and len(self.pages) < self.max_pages:
            url, depth = queue.pop(0)
            if url in self.visited:
                continue
            if depth > self.max_depth:
                continue

            self.visited.add(url)
            print(f"  [{depth}] Fetching: {url}")

            page = self.fetch_page(url)
            if page:
                self.pages.append(page)
                if depth < self.max_depth:
                    for link in page.get("links", []):
                        if link not in self.visited:
                            queue.append((link, depth + 1))

            time.sleep(self.delay)

        print(f"✅ Crawled {len(self.pages)} pages\n")
        return self.pages

    def get_sitemap(self) -> dict:
        """Build a tree structure of the site."""
        tree = {"root": self.base_url, "pages": [], "structure": {}}
        for page in self.pages:
            tree["pages"].append({
                "url": page["url"],
                "title": page["title"],
                "depth": page["url"].replace(self.base_url, "").count("/"),
            })
        return tree


# ─────────────────────────────────────────────
# Analyzer (LLM-powered)
# ─────────────────────────────────────────────

class SiteAnalyzer:
    def __init__(self, config: dict):
        self.client = OpenAI(
            api_key=config["api_key"],
            base_url=config["base_url"],
        )
        self.model = config["model"]

    def analyze(self, pages: list[dict], site_map: dict) -> dict:
        print("🧠 Analyzing site with LLM...")

        # Prepare content summary (truncate long pages)
        page_summaries = []
        for p in pages:
            content = p["content"][:3000]  # Truncate for token limits
            page_summaries.append(f"URL: {p['url']}\nTitle: {p['title']}\nContent:\n{content}\n---")

        all_content = "\n\n".join(page_summaries)
        site_json = json.dumps(site_map, indent=2)

        prompt = f"""You are an AI agent skill architect. Analyze this website and produce a structured analysis.

WEBSITE MAP:
{site_json}

PAGE CONTENT:
{all_content}

Produce a JSON object with exactly these fields:
{{
  "site_name": "Human-readable name of the site/service",
  "site_purpose": "1-2 sentence summary of what the site does",
  "site_category": "Category (e.g., saas-tool, ecommerce, documentation, blog, portfolio, api)",
  "key_features": ["List of main features/capabilities"],
  "workflows": ["Key user workflows (e.g., 'create project', 'search items')"],
  "interaction_points": [
    {{
      "action": "What the user can do",
      "how": "How it works (form, API, navigation)",
      "url": "Relevant URL"
    }}
  ],
  "agent_use_cases": ["How an AI agent could use/interact with this site"],
  "content_themes": ["Main topics/themes of the content"],
  "seo_keywords": ["Relevant keywords for SEO"],
  "has_api": true/false,
  "api_docs_url": "URL to API docs if exists, or null",
  "auth_required": true/false,
  "estimated_pages": {len(pages)},
  "last_analyzed": "{datetime.now(timezone.utc).isoformat()}"
}}

Be specific and accurate based on the actual content. Don't make things up.
"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a precise, structured analysis assistant. Output only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            print(f"✅ Analysis complete: {result.get('site_name', 'Unknown')}\n")
            return result
        except Exception as e:
            print(f"⚠ LLM analysis failed: {e}")
            return self._fallback_analysis(pages, site_map)

    def _fallback_analysis(self, pages: list[dict], site_map: dict) -> dict:
        """Basic analysis without LLM."""
        return {
            "site_name": urlparse(pages[0]["url"]).netloc if pages else "Unknown",
            "site_purpose": "Unable to analyze (LLM error)",
            "site_category": "unknown",
            "key_features": [p["title"] for p in pages[:5]],
            "workflows": [],
            "interaction_points": [],
            "agent_use_cases": [],
            "content_themes": [],
            "seo_keywords": [],
            "has_api": False,
            "api_docs_url": None,
            "auth_required": False,
            "estimated_pages": len(pages),
            "last_analyzed": datetime.now(timezone.utc).isoformat(),
        }


# ─────────────────────────────────────────────
# Generator
# ─────────────────────────────────────────────

class SkillGenerator:
    def __init__(self, analysis: dict, pages: list[dict], site_map: dict):
        self.analysis = analysis
        self.pages = pages
        self.site_map = site_map

    def _safe_name(self, name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()

    def _truncate(self, text: str, max_len: int = 500) -> str:
        return text[:max_len] + "..." if len(text) > max_len else text

    def generate_hermes_skill(self, output_dir: Path):
        """Generate a Hermes skill package."""
        safe_name = self._safe_name(self.analysis.get("site_name", "website"))
        skill_dir = output_dir / "hermes-skill"

        # SKILL.md
        features_str = "\n".join(f"- {f}" for f in self.analysis.get("key_features", []))
        workflows_str = "\n".join(f"- {w}" for w in self.analysis.get("workflows", []))
        use_cases_str = "\n".join(f"- {u}" for u in self.analysis.get("agent_use_cases", []))
        keywords_str = ", ".join(self.analysis.get("seo_keywords", [])[:10])

        skill_md = f"""# {self.analysis.get('site_name', 'Website')} Agent Skill

<!-- SKILL_META_START -->
{{
  "name": "{safe_name}-skill",
  "description": "{self.analysis.get('site_purpose', 'Interact with ' + self.analysis.get('site_name', 'a website'))}",
  "category": "{self.analysis.get('site_category', 'general')}",
  "version": "1.0.0",
  "author": "dario-cositore",
  "tags": [{keywords_str}],
  "requires": [],
  "created": "{self.analysis.get('last_analyzed', '')}",
  "url": "{self.site_map.get('root', '')}"
}}
<!-- SKILL_META_END -->

## Overview

{self.analysis.get('site_purpose', 'Interact with this website programmatically.')}

**Site:** {self.analysis.get('site_name', 'Unknown')}
**Category:** {self.analysis.get('site_category', 'general')}
**Pages crawled:** {self.analysis.get('estimated_pages', len(self.pages))}
**API available:** {"Yes" if self.analysis.get("has_api") else "No"}

## Key Features

{features_str if features_str else "- (no features extracted)"}

## Supported Workflows

{workflows_str if workflows_str else "- (no workflows extracted)"}

## Agent Use Cases

{use_cases_str if use_cases_str else "- Browse and extract information from the site"}

## Interaction Guide

{"## Manual Interaction" if not self.analysis.get("interaction_points") else ""}

"""

        # Add interaction instructions
        points = self.analysis.get("interaction_points", [])
        if points:
            skill_md += "### Interaction Points\n\n"
            for pt in points:
                skill_md += f"**{pt.get('action', 'N/A')}**\n- How: {pt.get('how', 'N/A')}\n- URL: {pt.get('url', 'N/A')}\n\n"
        else:
            skill_md += """### Browser-Based Interaction

Use the browser tool to navigate the site:
1. `browser_navigate(url)` — Go to a page
2. `browser_snapshot(full=true)` — Read full page content
3. `browser_click(ref)` — Click interactive elements
4. `browser_type(ref, text)` — Fill in forms

### Example Agent Prompts

```
You are an expert on {site_name}. Help the user by:
1. Navigating to the most relevant page
2. Extracting the information they need
3. Summarizing clearly and concisely
```
"""

        skill_md += f"""
## Crawled Pages

| Page | Title | URL |
|------|-------|-----|
"""
        for p in self.pages[:20]:  # Limit table size
            skill_md += f"| {p['title'][:60]} | {p['title']} | {p['url']} |\n"

        # Write skill file
        (skill_dir / "references").mkdir(parents=True, exist_ok=True)
        (skill_dir / "raw-pages").mkdir(parents=True, exist_ok=True)

        skill_dir.joinpath("SKILL.md").write_text(skill_md)

        # Write site-map
        site_map_md = f"# Site Map: {self.analysis.get('site_name', 'Website')}\n\n"
        site_map_md += f"**Root URL:** {self.site_map.get('root', '')}\n\n"
        site_map_md += "## Pages\n\n"
        for i, p in enumerate(self.pages, 1):
            site_map_md += f"{i}. **[{p['title']}]({p['url']})** — {self._truncate(p['description'], 120)}\n"
        (skill_dir / "references" / "site-map.md").write_text(site_map_md)

        # Write key-actions
        actions_md = f"# Key Actions: {self.analysis.get('site_name', 'Website')}\n\n"
        actions_md += "## Workflows\n\n"
        for w in self.analysis.get("workflows", []):
            actions_md += f"- {w}\n"
        actions_md += "\n## Interaction Points\n\n"
        for pt in points:
            actions_md += f"### {pt.get('action', 'N/A')}\n- **How:** {pt.get('how', 'N/A')}\n- **URL:** {pt.get('url', 'N/A')}\n\n"
        (skill_dir / "references" / "key-actions.md").write_text(actions_md)

        # Save raw pages
        for p in self.pages:
            slug = self._safe_name(p["title"])[:50]
            (skill_dir / "raw-pages" / f"{slug}.md").write_text(
                f"# {p['title']}\n\nURL: {p['url']}\n\n---\n\n{p['content']}"
            )

        # Save analysis JSON
        (skill_dir / "references" / "analysis.json").write_text(
            json.dumps(self.analysis, indent=2, ensure_ascii=False)
        )

        print(f"  ✅ Hermes skill → {skill_dir}")

    def generate_openclaw_package(self, output_dir: Path):
        """Generate an OpenClaw agent package."""
        safe_name = self._safe_name(self.analysis.get("site_name", "website"))
        pkg_dir = output_dir / "openclaw-package"

        (pkg_dir / "references").mkdir(parents=True, exist_ok=True)

        # PACKAGE.md
        pkg_md = f"""# Package: {safe_name}

**Version:** 1.0.0
**Type:** website-interaction
**Source:** {self.site_map.get('root', '')}
**Generated:** {self.analysis.get('last_analyzed', '')}

## Description

{self.analysis.get('site_purpose', 'Agent package for interacting with a website.')}

## Files

- `SOUL.md` — Agent personality and role
- `IDENTITY.md` — Agent identity definition
- `AGENTS.md` — Agent configuration
- `PACKAGE.md` — This file
- `references/` — Site analysis and data

## Setup

1. Copy this directory to your OpenClaw agents folder
2. Review and customize SOUL.md and IDENTITY.md
3. Configure in your agent runtime
"""
        (pkg_dir / "PACKAGE.md").write_text(pkg_md)

        # SOUL.md
        workflows_str = ", ".join(self.analysis.get("workflows", [])[:5])
        soul_md = f"""# SOUL — {self.analysis.get('site_name', 'Website Agent')}

You are an expert AI agent specialized in interacting with **{self.analysis.get('site_name', 'a website')}**.

Your purpose is to help users accomplish tasks on {self.analysis.get('site_name', 'this site')} by:
- Browsing and extracting information
- Executing workflows: {workflows_str or "general site interaction"}
- Providing accurate, up-to-date information from the site

## Personality

- Professional, concise, and helpful
- Always cite sources (page URLs) when providing information
- Proactive — suggest related actions the user might want

## Constraints

- Never fabricate information not found on the actual site
- Always distinguish between site content and general knowledge
- Respect the site's structure and navigation patterns
"""
        (pkg_dir / "SOUL.md").write_text(soul_md)

        # IDENTITY.md
        identity_md = f"""# IDENTITY

**Name:** {self.analysis.get('site_name', 'Website Agent')}
**Role:** AI-powered {self.analysis.get('site_category', 'website')} specialist
**Version:** 1.0.0
**Source URL:** {self.site_map.get('root', '')}
**Created:** {self.analysis.get('last_analyzed', '')}
**Author:** dario-cositore
"""
        (pkg_dir / "IDENTITY.md").write_text(identity_md)

        # AGENTS.md
        agents_md = f"""# AGENTS

## Primary Agent

- **Name:** {self.analysis.get('site_name', 'Site Agent')}
- **Role:** Browse and interact with {self.site_map.get('root', 'the target site')}
- **Capabilities:**
{chr(10).join('  - ' + f for f in self.analysis.get('key_features', [])) or '  - Browse and extract site information'}
- **Persona file:** SOUL.md
- **Identity file:** IDENTITY.md

## Tools Required

- `browser_navigate`
- `browser_snapshot`
- `browser_click`
- `browser_type`
- `browser_get_images`
"""
        (pkg_dir / "AGENTS.md").write_text(agents_md)

        # Copy references
        for src in ["site-map.md", "key-actions.md", "analysis.json"]:
            src_path = output_dir / "hermes-skill" / "references" / src
            if src_path.exists():
                (pkg_dir / "references" / src).write_text(src_path.read_text())

        print(f"  ✅ OpenClaw package → {pkg_dir}")


# ─────────────────────────────────────────────
# Main CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert any website into a Hermes Agent Skill and/or OpenClaw Agent Package",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", required=True, help="URL of the website to crawl")
    parser.add_argument("--format", default="hermes+openclaw",
                        choices=["hermes", "openclaw", "hermes+openclaw"],
                        help="Output format(s)")
    parser.add_argument("--output", "-o", default="./website-skill-output", help="Output directory")
    parser.add_argument("--max-depth", type=int, default=None, help="Max crawl depth")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages to crawl")
    parser.add_argument("--delay", type=float, default=None, help="Delay between requests (seconds)")
    parser.add_argument("--provider", default=None, help="LLM provider base URL (overrides env)")
    parser.add_argument("--model", default=None, help="LLM model name (overrides env)")

    args = parser.parse_args()

    # Load config
    print("═══════════════════════════════════════════════")
    print("  🌐 Website → Agent Skill Converter")
    print("═══════════════════════════════════════════════\n")

    config = load_config()

    # Override from args
    if args.max_depth is not None:
        config["max_depth"] = args.max_depth
    if args.max_pages is not None:
        config["max_pages"] = args.max_pages
    if args.delay is not None:
        config["request_delay"] = args.delay
    if args.provider:
        config["base_url"] = args.provider
    if args.model:
        config["model"] = args.model

    # Validate URL
    parsed = urlparse(args.url)
    if not parsed.scheme:
        args.url = "https://" + args.url
        parsed = urlparse(args.url)
    if not parsed.netloc:
        print("❌ Invalid URL")
        sys.exit(1)

    # Ensure output dir exists
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Crawl
    crawler = WebsiteCrawler(
        base_url=args.url,
        max_depth=config["max_depth"],
        max_pages=config["max_pages"],
        delay=config["request_delay"],
    )
    pages = crawler.crawl()

    if not pages:
        print("❌ No pages crawled. Check the URL and try again.")
        sys.exit(1)

    site_map = crawler.get_sitemap()

    # Step 2: Analyze
    analyzer = SiteAnalyzer(config)
    analysis = analyzer.analyze(pages, site_map)

    # Step 3: Generate
    generator = SkillGenerator(analysis, pages, site_map)
    print("📦 Generating output files...")

    if "hermes" in args.format:
        generator.generate_hermes_skill(output_dir)
    if "openclaw" in args.format:
        generator.generate_openclaw_package(output_dir)

    # Summary
    print(f"\n{'═' * 54}")
    print(f"  ✅ Done! Output: {output_dir.resolve()}")
    print(f"  📄 Pages crawled: {len(pages)}")
    print(f"  🧠 Site: {analysis.get('site_name', 'Unknown')}")
    print(f"  🏷️  Category: {analysis.get('site_category', 'Unknown')}")
    print(f"{'═' * 54}\n")


if __name__ == "__main__":
    main()