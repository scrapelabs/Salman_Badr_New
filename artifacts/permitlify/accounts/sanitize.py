"""Server-side HTML sanitisation for QA ticket / comment rich text.

Rich text arrives from an in-browser Quill editor, so it is untrusted: a crafted
payload could otherwise smuggle ``<script>`` or ``onerror=`` past us (stored
XSS). nh3 (the maintained ammonia binding) strips everything not on the
allowlist below, and the attribute filter keeps only Quill's formatting classes
(``ql-*``), a small safelist of inline CSS, and image/link URLs we consider
safe. The output of :func:`clean_html` is safe to render with ``|safe``.
"""

import re
from functools import lru_cache

import nh3
from django.urls import NoReverseMatch, reverse

# Tags Quill can emit that we are happy to render.
ALLOWED_TAGS = {
    "p", "br", "span", "div",
    "strong", "b", "em", "i", "u", "s", "strike",
    "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "a", "img",
}

# Attributes allowed through to the attribute filter (which refines their values).
ALLOWED_ATTRIBUTES = {
    "*": {"class", "style"},
    "a": {"href", "title", "target", "class", "style"},
    "img": {"src", "alt", "width", "height", "class", "style"},
    # Quill 2 renders both list kinds as <ol> and distinguishes them per <li>.
    "li": {"data-list"},
}

# Quill encodes alignment / indent / font / size as ``ql-*`` classes.
_CLASS_RE = re.compile(r"^ql-[a-z0-9\-]+$")

# Inline CSS we accept (colour + alignment). Anything else is dropped.
_SAFE_STYLE_PROPS = {"text-align", "color", "background-color"}
# Value guard: hex / rgb() / named colours / keywords only — no url(), no quotes.
_STYLE_VALUE_RE = re.compile(r"^[#a-zA-Z0-9 ,.\-%()]+$")


def _filter_class(value):
    keep = [t for t in (value or "").split() if _CLASS_RE.match(t)]
    return " ".join(keep) if keep else None


def _filter_style(value):
    decls = []
    for decl in (value or "").split(";"):
        prop, sep, val = decl.partition(":")
        if not sep:
            continue
        prop = prop.strip().lower()
        val = val.strip()
        if (
            prop in _SAFE_STYLE_PROPS
            and val
            and "url" not in val.lower()
            and _STYLE_VALUE_RE.match(val)
        ):
            decls.append(f"{prop}: {val}")
    return "; ".join(decls) if decls else None


_DUMMY_UUID = "00000000-0000-0000-0000-000000000000"


@lru_cache(maxsize=1)
def _attachment_prefix():
    """Stable path prefix our uploaded images are served from (no host)."""
    try:
        sample = reverse("qa_attachment", kwargs={"uuid": _DUMMY_UUID})
    except NoReverseMatch:  # pragma: no cover - URLConf always has this route
        return "/qa/attachments/"
    return sample.rsplit(_DUMMY_UUID, 1)[0]


def _filter_url(value, *, allow_relative=True, schemes=("http://", "https://")):
    v = (value or "").strip()
    low = v.lower()
    # Reject protocol-relative URLs (//host/...) — they smuggle a foreign origin.
    if v.startswith("//"):
        return None
    if allow_relative and v.startswith("/"):
        return v
    if low.startswith(schemes):
        return v
    return None


def _filter_img_src(value):
    """Images may only point at our own uploaded attachments or HTTPS hosts.

    Local sources are restricted to the attachment-serve path so a crafted body
    can't aim ``<img>`` at arbitrary same-origin GET endpoints; external images
    must be HTTPS (no plain-HTTP tracking pixels, no protocol-relative URLs).
    """
    v = (value or "").strip()
    if v.startswith(_attachment_prefix()):
        return v
    if v.lower().startswith("https://"):
        return v
    return None


def _attribute_filter(tag, attr, value):
    if attr == "class":
        return _filter_class(value)
    if attr == "style":
        return _filter_style(value)
    if attr == "data-list" and tag == "li":
        return value if value in {"ordered", "bullet"} else None
    if attr == "src" and tag == "img":
        return _filter_img_src(value)
    if attr == "href" and tag == "a":
        return _filter_url(value, schemes=("http://", "https://", "mailto:"))
    return value


def clean_html(html):
    """Return a sanitised copy of ``html`` safe to render with ``|safe``."""
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def is_blank_html(html):
    """True when sanitised rich text carries no visible text or image."""
    cleaned = clean_html(html)
    if "<img" in cleaned:
        return False
    text = nh3.clean(cleaned, tags=set()).replace("\xa0", " ").strip()
    return not text
