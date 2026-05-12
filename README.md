# website-to-skill

Convert any website into a **Hermes Agent Skill** and/or **OpenClaw Agent Package** — ready to deploy.

> **Live tool → https://dariocositore.com/tools/website-to-skill**

## Quick Start

```bash
python src/website_to_skill.py --url https://example.com --format hermes+openclaw --output ./my-skill
```

Paste a URL. Get a structured agent skill package. That's it.

## Features

- **Crawl & map** any website (sitemap.xml + recursive link following)
- **LLM-powered analysis** — classifies the site, extracts workflows, identifies interaction points
- **Dual output** — generates both Hermes skill format and OpenClaw agent package
- **Configurable** — LLM provider, crawl depth, rate limiting
- **Respectful** — honors robots.txt, rate limits, noindex tags

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API key
```

## Usage

```bash
# Hermes skill only
python src/website_to_skill.py --url https://example.com --format hermes --output ./skills/my-skill

# OpenClaw package only
python src/website_to_skill.py --url https://example.com --format openclaw --output ./packages/my-package

# Both formats
python src/website_to_skill.py --url https://example.com --format hermes+openclaw --output ./output

# With custom LLM provider
python src/website_to_skill.py --url https://example.com --provider openrouter --model deepseek/deepseek-v4-pro

# Crawl depth control
python src/website_to_skill.py --url https://example.com --max-depth 2 --max-pages 50 --output ./output
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
│   └── analysis.json           # LLM analysis results
└── PACKAGE.md                  # OpenClaw compatibility manifest
```

## Requirements

- Python 3.11+
- See `requirements.txt`

## License

MIT — fork it, break it, build on it. → https://dariocositore.com