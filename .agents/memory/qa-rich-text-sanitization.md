---
name: QA rich-text sanitization coupling
description: How QA ticket/comment HTML is locked down server-side, and the lockstep rules that keep images/formatting from silently vanishing.
---

QA ticket/comment rich text comes from an in-browser Quill editor (untrusted), is sanitized server-side in `accounts/sanitize.py`, then rendered with `|safe`. Sanitization is a strict **allowlist**: anything not explicitly permitted is dropped *without error*. That creates several lockstep rules.

**Image `src` is allowlisted to two shapes only:** the attachment-serve path prefix (derived at runtime via `reverse('qa_attachment', …)`) or `https://`. Plain `http://`, protocol-relative `//host`, `data:` URLs, and any other same-origin path (e.g. `/scrapers/.../errors.csv`) are stripped.
- **Why:** prevents stored XSS, plain-HTTP tracking pixels, and aiming `<img>` at arbitrary authenticated same-origin GET endpoints.
- **How to apply:** if you add a *new* local route that serves user images, add it to `_filter_img_src` or those images render blank. Inserted images must use the attachment URL, never base64.

**Quill paste/drop must upload, not embed base64.** Pasted screenshots and dropped files default to base64 `data:` URLs, which the sanitizer strips → the image silently disappears. The editor JS intercepts `paste`/`drop` in the **capture phase** (so it runs before Quill's own clipboard handler), uploads the file, and inserts the returned attachment URL.
- **How to apply:** keep the capture-phase listeners; if you swap editors or Quill versions, re-verify paste-to-upload still beats the default base64 embed.

**Formatting is also allowlisted.** Tags/attributes live in `ALLOWED_TAGS` / `ALLOWED_ATTRIBUTES`, plus `ql-*` classes and a tiny inline-CSS safelist (`text-align`, `color`, `background-color`). Quill 2 renders both list kinds as `<ol>` and marks the kind via `li[data-list=ordered|bullet]`, so that attr is allowlisted.
- **How to apply:** adding a new toolbar format (e.g. a new block) usually needs matching tag/attr/`ql-*` allowlist updates or the formatting drops on save.
