# Permitlify

Permitlify is a Permit Intelligence Platform SaaS that delivers daily, AI-scored building-permit leads so sales teams know exactly who to call before the competition.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `artifacts/permitlify/` — the web app (React + Vite, wouter routing), previewPath `/`.
  - `src/App.tsx` — routes (`/` → Login, `/dashboard` → placeholder, NotFound).
  - `src/pages/Login.tsx` + `src/pages/login.css` — login page; CSS scoped under `.login-page`.
  - `src/components/LogoMark.tsx` — reusable brand SVG logo (uses `useId()` for unique gradient/filter IDs).
  - `src/index.css` — shared brand design tokens in `:root` (`--pr`, `--ac`, `--grad`, `--tx`, `--sur2`, etc.) + fonts; reused by every page.
  - `index.html` — Google Fonts (Plus Jakarta Sans, DM Sans, JetBrains Mono).
- `attached_assets/dailypermit_*.html` — the supplied pixel-exact design mockups (source of truth for each page's look).

## Architecture decisions

- The supplied HTML/CSS mockups are an exact spec. Pages are recreated faithfully by hand (not via the design subagent, which improvises).
- Brand tokens defined once in `:root` (`src/index.css`); each page's mockup CSS is scoped under a page-root class (e.g. `.login-page`) to avoid cross-page selector/layout leakage.
- Frontend-only so far — no auth, OpenAPI/codegen, or DB yet. Sign In navigates to a `/dashboard` placeholder; real auth is deferred.

## Product

- **Login** (`/`) — two-column page: dark sales/marketing panel (headline, proof points, trust badges) left, white sign-in form (email/password, Google social login) right. Complete.
- Planned (mockups supplied): dashboard, profile, settings, permit report.

## User preferences

- Recreate the supplied designs faithfully/pixel-exact rather than improvising.

## Gotchas

- Always run `pnpm --filter @workspace/permitlify run typecheck` after edits (not `build`, which needs workflow-provided `PORT`/`BASE_PATH`).
- Brand tokens are global in `src/index.css` `:root`; keep new-page CSS scoped under that page's root class.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
