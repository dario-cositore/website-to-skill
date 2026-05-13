/**
 * Cloudflare Pages Function — Website → Agent Skill
 * Handles: GET /api/health, POST /api/generate
 *
 * Fast path: parallel page fetching, rich heuristic analysis, inline ZIP (no deps).
 */

export async function onRequest(context) {
  const { request } = context;
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, '');

  const CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  };

  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: CORS });
  }

  // ── Health ───────────────────────────────────────────────────────────────────
  if (path.endsWith('/api/health')) {
    return Response.json({ status: 'ok' }, { headers: CORS });
  }

  // ── Generate ─────────────────────────────────────────────────────────────────
  if (path.endsWith('/api/generate')) {
    if (request.method !== 'POST') {
      return Response.json({ error: 'POST required' }, { status: 405, headers: CORS });
    }
    let body;
    try { body = await request.json(); } catch {
      return Response.json({ error: 'Invalid JSON body' }, { status: 400, headers: CORS });
    }

    const { url: targetUrl, format = 'hermes+openclaw', depth = 2, pages = 20 } = body;
    if (!targetUrl) return Response.json({ error: 'url is required' }, { status: 400, headers: CORS });

    let parsedTarget;
    try { parsedTarget = new URL(targetUrl); } catch {
      return Response.json({ error: 'Invalid URL' }, { status: 400, headers: CORS });
    }

    try {
      const maxPages = Math.min(Number(pages) || 20, 30);
      const maxDepth = Math.min(Number(depth) || 2, 3);

      const crawled = await crawlWebsite(targetUrl, parsedTarget.origin, maxDepth, maxPages);
      const analysis = analyzePages(crawled, targetUrl);
      const zipBytes = buildZip(generateFiles(analysis, format));

      const safeName = analysis.name.replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      return new Response(zipBytes, {
        headers: {
          ...CORS,
          'Content-Type': 'application/zip',
          'Content-Disposition': `attachment; filename="${safeName}-skill.zip"`,
        },
      });
    } catch (err) {
      return Response.json({ error: err.message || 'Internal error' }, { status: 500, headers: CORS });
    }
  }

  return Response.json({ error: 'Not found' }, { status: 404, headers: CORS });
}

// ═══════════════════════════════════════════════════════════════════════════════
// CRAWLER — parallel fetch per depth level
// ═══════════════════════════════════════════════════════════════════════════════

const SKIP_EXT = /\.(pdf|jpg|jpeg|png|gif|svg|css|js|woff2?|ttf|eot|ico|zip|tar|gz|mp[34]|wav|csv|xlsx?|json|xml)$/i;
const PAGE_TIMEOUT_MS = 7000;

// Score a URL so we crawl high-value pages first
function scoreUrl(url) {
  const p = url.toLowerCase();
  if (p.match(/\/(docs?|documentation|api|guide|reference|tutorial|start|overview|features?|product|pricing|about|learn)\b/)) return 10;
  if (p.match(/\/(blog|news|post|article|changelog|release|update)\b/)) return 3;
  if (p.match(/\/(login|signup|register|auth|account|dashboard)\b/)) return 1;
  return 5;
}

async function fetchPage(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PAGE_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: { 'User-Agent': 'WebsiteSkillBot/1.0', Accept: 'text/html' },
      redirect: 'follow',
    });
    clearTimeout(timer);
    if (!res.ok) return null;
    const ct = res.headers.get('content-type') || '';
    if (!ct.includes('text/html')) return null;
    const html = await res.text();
    return { url, html };
  } catch {
    clearTimeout(timer);
    return null;
  }
}

async function crawlWebsite(startUrl, origin, maxDepth, maxPages) {
  const visited = new Set();
  const results = [];

  // BFS by depth level, parallel within each level
  let queue = [startUrl];
  visited.add(startUrl);

  for (let depth = 0; depth <= maxDepth && queue.length > 0 && results.length < maxPages; depth++) {
    const batch = queue.splice(0, Math.min(queue.length, maxPages - results.length));
    const fetched = await Promise.allSettled(batch.map(fetchPage));

    const nextLinks = new Map(); // url → score

    for (const result of fetched) {
      if (result.status !== 'fulfilled' || !result.value) continue;
      const { url, html } = result.value;
      const data = extractPageData(url, html, origin);
      results.push(data);
      if (results.length >= maxPages) break;

      if (depth < maxDepth) {
        for (const link of data.links) {
          if (!visited.has(link)) {
            visited.add(link);
            nextLinks.set(link, scoreUrl(link));
          }
        }
      }
    }

    // Sort next level by score descending
    queue = [...nextLinks.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([u]) => u)
      .slice(0, 15);
  }

  return results;
}

// ═══════════════════════════════════════════════════════════════════════════════
// EXTRACTOR
// ═══════════════════════════════════════════════════════════════════════════════

function extractPageData(url, html, origin) {
  const title = (html.match(/<title[^>]*>([^<]{1,120})<\/title>/i) || [])[1]?.trim() || '';

  const descTag =
    html.match(/<meta[^>]+name=["']description["'][^>]+content=["']([^"']{1,300})["']/i) ||
    html.match(/<meta[^>]+content=["']([^"']{1,300})["'][^>]+name=["']description["']/i);
  const description = descTag ? descTag[1].trim() : '';

  const ogDesc = (html.match(/<meta[^>]+property=["']og:description["'][^>]+content=["']([^"']{1,300})["']/i) || [])[1]?.trim() || '';

  // Headings h1–h3
  const headings = [];
  for (const m of html.matchAll(/<h([1-3])[^>]*>([\s\S]{2,120}?)<\/h[1-3]>/gi)) {
    const text = m[2].replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
    if (text.length > 2 && text.length < 100) headings.push({ level: Number(m[1]), text });
  }

  // Nav links (text labels)
  const navLabels = [];
  const navBlock = html.match(/<nav[\s\S]*?<\/nav>/gi) || [];
  for (const nav of navBlock.slice(0, 2)) {
    for (const m of nav.matchAll(/>([A-Za-z][^<]{1,30})</g)) {
      const t = m[1].trim();
      if (t.length > 1) navLabels.push(t);
    }
  }

  // Text content (stripped)
  const text = html
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 4000);

  // API signals
  const hasApi = /\b(api|rest|graphql|endpoint|swagger|openapi|webhook|oauth|bearer token)\b/i.test(html);
  const hasAuth = /\b(login|sign in|signin|log in|register|signup|sign up|authenticate|password)\b/i.test(html);

  // Links
  const links = new Set();
  for (const m of html.matchAll(/href=["']([^"'#?]{4,})["']/gi)) {
    try {
      const u = new URL(m[1], url);
      if (u.origin === origin && !SKIP_EXT.test(u.pathname) && u.href !== url) {
        links.add(u.origin + u.pathname);
      }
    } catch {}
  }

  return { url, title, description: description || ogDesc, headings, navLabels, text, hasApi, hasAuth, links: [...links] };
}

// ═══════════════════════════════════════════════════════════════════════════════
// HEURISTIC ANALYZER
// ═══════════════════════════════════════════════════════════════════════════════

const ACTION_VERBS = ['search','find','get','list','fetch','create','add','update','delete','send',
  'view','manage','browse','track','monitor','analyze','export','import','sync','generate',
  'convert','summarize','upload','download','share','connect','integrate','filter','sort',
  'publish','deploy','invite','configure','schedule','notify'];

const CATEGORY_PATTERNS = [
  [/\b(saas|software|platform|tool|app|dashboard|workspace)\b/i, 'saas-tool'],
  [/\b(api|developer|sdk|documentation|docs|reference|endpoint)\b/i, 'developer-tools'],
  [/\b(shop|store|product|cart|checkout|buy|purchase|price|pricing)\b/i, 'ecommerce'],
  [/\b(blog|article|post|news|magazine|publication|newsletter)\b/i, 'blog'],
  [/\b(portfolio|work|project|case study|showcase)\b/i, 'portfolio'],
  [/\b(learn|course|tutorial|education|lesson|training|certification)\b/i, 'education'],
  [/\b(community|forum|discuss|member|group|network)\b/i, 'community'],
  [/\b(company|about us|team|contact|services|solutions|enterprise)\b/i, 'corporate'],
];

function classifyCategory(text) {
  const counts = {};
  for (const [re, cat] of CATEGORY_PATTERNS) {
    const matches = (text.match(new RegExp(re.source, 'gi')) || []).length;
    if (matches) counts[cat] = (counts[cat] || 0) + matches;
  }
  const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
  return top ? top[0] : 'website';
}

function extractWorkflows(pages) {
  const seen = new Map();
  for (const page of pages) {
    for (const h of page.headings) {
      const lower = h.text.toLowerCase();
      for (const verb of ACTION_VERBS) {
        if (lower.includes(verb)) {
          if (!seen.has(lower.slice(0, 60))) {
            seen.set(lower.slice(0, 60), {
              name: toSnakeCase(h.text),
              description: h.text.trim(),
              source: page.url,
            });
          }
          break;
        }
      }
    }
  }
  return [...seen.values()].slice(0, 12);
}

function extractFeatures(pages) {
  const features = new Set();
  // Collect h2/h3 headings across all pages as feature names
  for (const page of pages) {
    for (const h of page.headings) {
      if (h.level >= 2 && h.text.length > 3 && h.text.length < 80) {
        features.add(h.text.trim());
      }
    }
    // Nav items as features
    for (const nav of page.navLabels.slice(0, 8)) {
      if (nav.length > 2) features.add(nav);
    }
  }
  return [...features].slice(0, 10);
}

function analyzePages(pages, startUrl) {
  if (!pages.length) throw new Error('No pages could be fetched from the target URL');

  const main = pages[0];
  const allText = pages.map(p => p.text).join(' ');
  const base = new URL(startUrl);
  const domain = base.hostname.replace(/^www\./, '');
  const name = domain.split('.')[0];

  const siteName = main.title || name;
  const purpose = main.description || `Tools and capabilities for ${siteName}`;
  const category = classifyCategory(allText.slice(0, 8000));
  const features = extractFeatures(pages);
  const workflows = extractWorkflows(pages);
  const hasApi = pages.some(p => p.hasApi);
  const authRequired = pages.some(p => p.hasAuth);

  const agentUseCases = [
    `Browse and extract information from ${siteName}`,
    ...workflows.slice(0, 4).map(w => `Automate: ${w.description}`),
  ].slice(0, 6);

  return {
    url: startUrl,
    name,
    domain,
    title: siteName,
    description: purpose,
    category,
    features,
    workflows,
    pages: pages.map(p => ({ url: p.url, title: p.title, description: p.description })),
    hasApi,
    authRequired,
    agentUseCases,
    crawledAt: new Date().toISOString(),
  };
}

function toSnakeCase(str) {
  return str.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/, '').slice(0, 50) || 'action';
}

// ═══════════════════════════════════════════════════════════════════════════════
// SKILL GENERATORS
// ═══════════════════════════════════════════════════════════════════════════════

function generateFiles(a, format) {
  const files = [];
  files.push({ name: 'analysis.json', content: JSON.stringify(a, null, 2) });

  if (format.includes('hermes')) {
    const safeName = a.name.replace(/[^a-zA-Z0-9-]/g, '-');
    const featuresStr = a.features.map(f => `- ${f}`).join('\n') || '- (none extracted)';
    const workflowsStr = a.workflows.map(w => `- ${w.description}`).join('\n') || '- (none extracted)';
    const useCasesStr = a.agentUseCases.map(u => `- ${u}`).join('\n');
    const pagesTable = a.pages.slice(0, 20)
      .map(p => `| ${(p.title || p.url).slice(0, 60)} | ${p.url} |`)
      .join('\n');

    const skillMd = `# ${a.title} Agent Skill

<!-- SKILL_META_START -->
{
  "name": "${safeName}-skill",
  "description": "${a.description.replace(/"/g, "'")}",
  "category": "${a.category}",
  "version": "1.0.0",
  "author": "dario-cositore",
  "tags": ["${a.domain}", "${a.category}", "agent-skill"],
  "created": "${a.crawledAt}",
  "url": "${a.url}"
}
<!-- SKILL_META_END -->

## Overview

${a.description}

**Site:** ${a.title}
**URL:** ${a.url}
**Category:** ${a.category}
**Pages crawled:** ${a.pages.length}
**Has API:** ${a.hasApi ? 'Yes' : 'No'}
**Auth required:** ${a.authRequired ? 'Yes' : 'No'}

## Key Features

${featuresStr}

## Supported Workflows

${workflowsStr}

## Agent Use Cases

${useCasesStr}

## Interaction Guide

Use the browser tool to interact with this site:

1. \`browser_navigate(url)\` — Navigate to a specific page
2. \`browser_snapshot(full=true)\` — Read full page content
3. \`browser_click(ref)\` — Click interactive elements
4. \`browser_type(ref, text)\` — Fill forms

### Example Agent Prompt

\`\`\`
You are an expert on ${a.title}. When the user asks about ${a.domain}:
1. Navigate to the most relevant page
2. Extract the information they need
3. Summarize clearly with source URLs
\`\`\`

## Crawled Pages

| Title | URL |
|-------|-----|
${pagesTable}

---
*Generated by website-to-skill on ${a.crawledAt}*
`;

    const siteMapMd = `# Site Map: ${a.title}\n\n**Root URL:** ${a.url}\n\n## Pages\n\n${
      a.pages.map((p, i) => `${i + 1}. **[${p.title || p.url}](${p.url})**\n   ${p.description || ''}`).join('\n')
    }\n`;

    const actionsJson = JSON.stringify({
      name: safeName,
      tools: a.workflows.map(w => ({
        name: w.name,
        description: w.description,
        input_schema: { type: 'object', properties: { query: { type: 'string', description: 'Input or query' } }, required: [] },
      })),
    }, null, 2);

    files.push({ name: 'hermes-skill/SKILL.md', content: skillMd });
    files.push({ name: 'hermes-skill/references/site-map.md', content: siteMapMd });
    files.push({ name: 'hermes-skill/references/actions.json', content: actionsJson });
    files.push({ name: 'hermes-skill/references/analysis.json', content: JSON.stringify(a, null, 2) });
  }

  if (format.includes('openclaw')) {
    const safeName = a.name.replace(/[^a-zA-Z0-9-]/g, '-');

    const packageMd = `# Package: ${safeName}\n\n**Version:** 1.0.0\n**Type:** website-interaction\n**Source:** ${a.url}\n**Category:** ${a.category}\n**Generated:** ${a.crawledAt}\n\n## Description\n\n${a.description}\n\n## Files\n\n- \`SOUL.md\` — Agent personality\n- \`IDENTITY.md\` — Agent identity\n- \`AGENTS.md\` — Agent configuration\n- \`PACKAGE.md\` — This file\n- \`references/\` — Site analysis\n`;

    const soulMd = `# SOUL — ${a.title}\n\nYou are an expert AI agent specialized in **${a.title}** (${a.url}).\n\nYour purpose is to help users:\n${a.agentUseCases.map(u => `- ${u}`).join('\n')}\n\n## Personality\n\n- Professional, concise, accurate\n- Always cite source URLs\n- Suggest related actions proactively\n\n## Constraints\n\n- Never fabricate information not found on the site\n- Distinguish site content from general knowledge\n- Respect robots.txt and rate limits\n`;

    const identityMd = `# IDENTITY\n\n**Name:** ${a.title}\n**Role:** ${a.category} specialist\n**Version:** 1.0.0\n**Source:** ${a.url}\n**Created:** ${a.crawledAt}\n**Author:** dario-cositore\n`;

    const capabilitiesStr = a.workflows.length
      ? a.workflows.map(w => `- **\`${w.name}\`**: ${w.description}`).join('\n')
      : a.features.map(f => `- ${f}`).join('\n') || '- Browse and extract site information';

    const agentsMd = `# AGENTS\n\n## Primary Agent\n\n- **Name:** ${a.title}\n- **Role:** Interact with ${a.url}\n- **Capabilities:**\n${capabilitiesStr}\n- **Persona:** SOUL.md\n- **Identity:** IDENTITY.md\n\n## Required Tools\n\n- \`browser_navigate\`\n- \`browser_snapshot\`\n- \`browser_click\`\n- \`browser_type\`\n`;

    files.push({ name: 'openclaw-package/PACKAGE.md', content: packageMd });
    files.push({ name: 'openclaw-package/SOUL.md', content: soulMd });
    files.push({ name: 'openclaw-package/IDENTITY.md', content: identityMd });
    files.push({ name: 'openclaw-package/AGENTS.md', content: agentsMd });
    files.push({ name: 'openclaw-package/references/analysis.json', content: JSON.stringify(a, null, 2) });
    files.push({ name: 'openclaw-package/references/site-map.md', content: `# Site Map: ${a.title}\n\n${a.pages.map(p => `- [${p.title || p.url}](${p.url})`).join('\n')}\n` });
  }

  return files;
}

// ═══════════════════════════════════════════════════════════════════════════════
// MINIMAL ZIP BUILDER (pure JS, no deps)
// ═══════════════════════════════════════════════════════════════════════════════

function crc32(data) {
  let c = 0xffffffff;
  for (let i = 0; i < data.length; i++) {
    c ^= data[i];
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
  }
  return (c ^ 0xffffffff) >>> 0;
}

function buildZip(files) {
  const enc = new TextEncoder();

  function u16le(n) { return [(n & 0xff), (n >> 8) & 0xff]; }
  function u32le(n) { return [...u16le(n & 0xffff), ...u16le((n >>> 16) & 0xffff)]; }

  const localParts = [];
  const cdParts = [];
  let offset = 0;

  for (const { name, content } of files) {
    const nameBytes = enc.encode(name);
    const dataBytes = typeof content === 'string' ? enc.encode(content) : content;
    const crc = crc32(dataBytes);
    const sz = dataBytes.length;

    // Local file header (30 bytes + name)
    const lh = new Uint8Array([
      0x50, 0x4b, 0x03, 0x04,  // signature
      20, 0,                    // version needed
      0, 0,                     // flags
      0, 0,                     // method: stored
      0, 0, 0, 0,               // mod time/date
      ...u32le(crc),
      ...u32le(sz),             // compressed
      ...u32le(sz),             // uncompressed
      ...u16le(nameBytes.length),
      0, 0,                     // extra length
      ...nameBytes,
    ]);
    localParts.push(lh, dataBytes);

    // Central directory entry (46 bytes + name)
    const cd = new Uint8Array([
      0x50, 0x4b, 0x01, 0x02,  // signature
      20, 0,                    // version made by
      20, 0,                    // version needed
      0, 0,                     // flags
      0, 0,                     // method: stored
      0, 0, 0, 0,               // mod time/date
      ...u32le(crc),
      ...u32le(sz),             // compressed
      ...u32le(sz),             // uncompressed
      ...u16le(nameBytes.length),
      0, 0,                     // extra length
      0, 0,                     // comment length
      0, 0,                     // disk start
      0, 0,                     // internal attrs
      0, 0, 0, 0,               // external attrs
      ...u32le(offset),         // offset of local header
      ...nameBytes,
    ]);
    cdParts.push(cd);
    offset += lh.length + dataBytes.length;
  }

  const cdOffset = offset;
  const cdSize = cdParts.reduce((s, p) => s + p.length, 0);

  const eocd = new Uint8Array([
    0x50, 0x4b, 0x05, 0x06,   // signature
    0, 0,                      // disk number
    0, 0,                      // disk with CD
    ...u16le(files.length),    // entries on disk
    ...u16le(files.length),    // total entries
    ...u32le(cdSize),
    ...u32le(cdOffset),
    0, 0,                      // comment length
  ]);

  const all = [...localParts, ...cdParts, eocd];
  const total = all.reduce((s, p) => s + p.length, 0);
  const out = new Uint8Array(total);
  let pos = 0;
  for (const p of all) { out.set(p, pos); pos += p.length; }
  return out;
}
