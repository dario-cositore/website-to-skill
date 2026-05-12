# Website-to-Skill

Convert any website into a **Hermes Agent Skill** and/or **OpenClaw Agent Package** — ready to deploy.

> **Tool:** https://dariocositore.com/tools/website-to-skill

## Quick Start

```bash
python src/website_to_skill.py --url https://example.com --format hermes+openclaw --output ./my-skill
```

Or run it as a web server for an interactive UI:

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:8888
```

## Features

- **Crawl & map** any website (sitemap.xml + recursive link following)
- **Heuristic analysis** — classifies the site, extracts workflows, identifies interaction points — **no LLM needed**
- **Dual output** — generates both Hermes skill format and OpenClaw agent package
- **Configurable** — crawl depth, rate limiting, page limits
- **Respectful** — honors robots.txt, rate limits, noindex tags
- **Free** — no API keys, no external calls, fully offline

## Setup

```bash
pip install -r requirements.txt
```

That's it. No API key, no config files.

## Usage

### Web UI

```bash
python app.py
# Open http://localhost:8888
```

### CLI

```bash
# Hermes skill only
python src/website_to_skill.py --url https://example.com --format hermes --output ./skills/my-skill

# OpenClaw package only
python src/website_to_skill.py --url https://example.com --format openclaw --output ./packages/my-package

# Both formats
python src/website_to_skill.py --url https://example.com --format hermes+openclaw --output ./output

# Crawl depth control
python src/website_to_skill.py --url https://example.com --max-depth 3 --max-pages 50 --output ./output
```

### Python API

```python
from src.website_to_skill import Crawler, analyze_heuristic, SkillGenerator

# Crawl
crawler = Crawler("https://example.com", max_depth=2, max_pages=20)
pages = crawler.crawl()

# Analyze
analysis = analyze_heuristic(pages, "https://example.com")

# Generate
gen = SkillGenerator(analysis, pages, "https://example.com")
gen.generate_hermes_skill("./output")
gen.generate_openclaw_package("./output")
```

## Output Structure

```
output/
├── SKILL.md                    # Hermes skill definition
├── site-map.md                 # Crawled site structure
├── key-actions.md              # What agents can do on this site
├── interaction-guide.md        # How to interact with the site
├── identity.md                 # Agent identity for this skill
├── references/
│   ├── raw-pages/              # Raw extracted content per page
│   └── analysis.json           # Analysis results
└── PACKAGE.md                  # OpenClaw compatibility manifest
```

## How Analysis Works

The tool uses **pure heuristic analysis** — no LLM calls, no API keys:

1. **URL pattern matching** — path segments classify the site (docs, blog, shop, etc.)
2. **HTML structure analysis** — meta tags, heading hierarchy, form detection
3. **Workflow detection** — identifies signup forms, search, checkout, dashboards, etc.
4. **Feature detection** — live chat, maps, video, payment forms, social feeds
5. **TF-IDF keyword extraction** — most relevant terms from page content
6. **Agent use case generation** — based on site category and detected features

## Requirements

- Python 3.11+
- See `requirements.txt`

## License

MIT — fork it, break it, build on it. → https://dariocositore.com