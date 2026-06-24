---
name: WhiteNoise manifest storage chokes on vendored sourcemap comments
description: Why collectstatic fails after vendoring a JS/CSS lib, and the rule for vendoring static assets under CompressedManifestStaticFilesStorage.
---

`collectstatic` under WhiteNoise `CompressedManifestStaticFilesStorage` (the prod static setup) post-processes every JS/CSS file: it resolves and rewrites *every reference* inside them — `url(...)` in CSS **and** `//# sourceMappingURL=...` / `/*# sourceMappingURL=... */` comments. If a referenced file is missing, it raises `whitenoise.storage.MissingFileError` and `collectstatic` aborts.

**Rule:** when vendoring a minified library into `static/`, either also vendor its `.map` files **or** strip the trailing `sourceMappingURL` comment from the JS/CSS. Maps are dev-only, so stripping is the simpler fix.

**Why:** minified dist files (e.g. Quill's `quill.js`, `quill.snow.css`) ship with a `sourceMappingURL` pointer to a `.map` that we usually don't copy. The reference alone is enough to fail the manifest pass — even though the map is never needed at runtime.

**How to apply:** after dropping any new vendored asset into `static/vendor/...`, grep it for `sourceMappingURL` and `url(`; delete the sourcemap comment lines (they sit on their own line, safe to delete) and confirm any `url(...)` targets (fonts/images) are actually present. Then run `manage.py collectstatic --noinput` to verify before relying on it. This bites both the Replit prod build and local Windows `2_collectstatic.bat`.
