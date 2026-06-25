---
name: Django on a Replit artifact
description: How to run a Python/Django app inside Replit's Node-only artifacts framework (proxy, artifact.toml, iframe cookies).
---

# Running Django inside a Replit "web" artifact

Replit's artifacts framework has **no Python/Django kind**. To run Django, repurpose an
existing `web` artifact slot by hand-editing its `artifact.toml` and applying it with
`verifyAndReplaceArtifactToml`. `createArtifact` does not support Python.

## artifact.toml gotchas (learned the hard way)

- **Working directory = the artifact directory**, NOT the workspace root. The workflow runs
  the service `run` command from inside `artifacts/<slug>/`. So use plain relative commands
  (`python3 manage.py runserver 0.0.0.0:$PORT`). A path prefix (`artifacts/x/manage.py`) or a
  `cd artifacts/x &&` both FAIL — the latter errors `bash: cd: artifacts/x: No such file or directory`.
  **Why:** that error message is the tell that cwd is already the artifact dir.
- `verifyAndReplaceArtifactToml` **cannot change `integratedSkills`** — it returns
  `ARTIFACT_EDITING_ERROR ... cannot change integratedSkills`. Keep the original block
  (e.g. `react-vite`) byte-for-byte even if irrelevant; it's harmless metadata.
- Dev run: `python3 manage.py runserver 0.0.0.0:$PORT --noreload` (the StatReloader is heavy).
- Prod build args: `collectstatic --noinput && migrate --noinput`; prod run: `gunicorn <proj>.wsgi:application --bind 0.0.0.0:$PORT`.
- The temp edit file is consumed/removed on a successful `verifyAndReplaceArtifactToml`; recreate it for each subsequent edit.

## settings.py for the cross-site preview iframe

The preview embeds the app in a cross-origin iframe behind a TLS-terminating path-routing proxy:
- Omit `XFrameOptionsMiddleware` (its `X-Frame-Options` would block the iframe).
- `SESSION_COOKIE_SAMESITE = "None"` + `SESSION_COOKIE_SECURE = True`; same for CSRF cookies — otherwise cookies aren't sent in the iframe.
- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` so `request.is_secure()` is correct.
- `CSRF_TRUSTED_ORIGINS` must include the Replit wildcard domains (`https://*.replit.dev`, `.replit.app`, `.riker.replit.dev`, …) plus `REPLIT_DOMAINS`.
- `ALLOWED_HOSTS = ["*"]` (proxy forwards arbitrary Host).

## Removing an artifact + its workflow

`removeWorkflow` **fails on an artifact-managed workflow** (`PROHIBITED_ACTION ... managed by an
artifact and cannot be deleted`). To delete an artifact and its workflow, just `rm -rf` the
artifact directory — the framework auto-deregisters the artifact and removes its workflow.
After deleting workspace packages, run `pnpm install` to prune the lockfile and clear any
`tsconfig.json` `references` that point at the removed `lib/*` packages, then `pnpm run typecheck`.

## Testing auth

Because the cookies are `Secure`, `curl` will **not** send them over plain `http://localhost:80`.
Test the full login/logout flow over the HTTPS dev domain (`https://$REPLIT_DEV_DOMAIN/...`),
extracting `csrfmiddlewaretoken` from the form and sending a `Referer` header.
