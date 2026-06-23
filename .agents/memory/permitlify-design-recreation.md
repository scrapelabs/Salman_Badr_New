---
name: Permitlify design recreation
description: How the Permitlify pages are recreated faithfully from supplied HTML/CSS mockups, and the CSS scoping convention to keep them isolated.
---

# Permitlify — faithful mockup recreation

The user supplied pixel-exact HTML/CSS mockups (login, dashboard, profile, settings, permit report) under `attached_assets/dailypermit_*.html`. These are an exact spec, not a starting point — recreate them faithfully by hand in the main agent. **Do NOT route this to the design subagent** (it improvises and would break pixel fidelity). This is the same exception the react-vite skill makes for "app transitioned from a user-made mockup".

## Conventions established

- **Brand tokens live in `:root`** in `artifacts/permitlify/src/index.css` (the short mockup names: `--pr`, `--pr2`, `--ac`, `--ac2`, `--grad`, `--tx`/`--tx2`/`--tx3`, `--sur2`, etc.). Every mockup page shares this same `:root` block, so define once and reuse.
- **Each page's mockup CSS is scoped under a page-root class** (e.g. `.login-page` in `src/pages/login.css`), prefixing every original selector. This prevents the mockup's generic class names (`.left`, `.field`, `.proof-item`, `.headline`) and layout rules (grid/flex/padding) from leaking across pages.
- **Fonts:** Plus Jakarta Sans (headings/`--app-font-serif`), DM Sans (body/`--app-font-sans`), JetBrains Mono (mono labels/`--app-font-mono`). Imported via Google Fonts `<link>` in `index.html`.
- **Logo:** reusable `src/components/LogoMark.tsx` — a brand SVG using `useId()` to make gradient/filter IDs unique per instance (it's rendered more than once per page). Current mark is a **tennis ball** (blue→green gradient circle with two white seam curves). Wordmark is plain text alongside it; first half color varies by background (`#f1f5f9` on dark, `#1C2F74` on light), second half always `#49C85B` green.

## Rebrand: Permitlify → MatchMiner (tennis pivot)

The app started as "Permitlify" (building-permit leads) and was rebranded to **MatchMiner**, a tennis intelligence platform. Wordmark split is "Match" + "Miner". **The artifact slug/directory stays `permitlify`** — renaming it would break the workflow, ports, and proxy paths. Only user-facing surfaces were changed: `artifact.toml` title, `index.html` meta + `<title>`, `public/favicon.svg`, the `LogoMark` SVG, the wordmark spans + eyebrow + footer domain in `Login.tsx`.

**Still permit-themed (not yet rewritten):** the login left-panel sales copy and proof cards (`Login.tsx`) still talk about building permits / leads / CRM. Rewrite for tennis when the user asks to update the body content.

**Why:** the user explicitly wants the supplied design reproduced exactly; faithfulness beats taste here. Keeping tokens global + page CSS scoped lets each new page match without cross-page regressions.

## Gotcha — long SVG line truncation

The logo SVG is a single ~2400-char line; the file `read` tool truncates lines >2000 chars. To get the full markup, extract it with node (`fs.readFileSync` + slice) rather than `read`.
