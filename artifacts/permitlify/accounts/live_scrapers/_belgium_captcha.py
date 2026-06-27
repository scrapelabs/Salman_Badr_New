"""Zenedge "ben jij een robot?" captcha solver for the Belgium scraper.

``www.tennisenpadelvlaanderen.be`` sits behind a Zenedge anti-bot interstitial
that, when triggered, serves an inline HTML page (HTTP 200) titled
*"ben jij een robot?"* with a captcha image embedded as a ``data:`` URI. The
real page is only returned after POSTing the correct 6-character code to
``/__zenedge/c``; the clearance cookie then rides on the same session.

The original spider decodes that captcha with a small Keras CNN (one softmax
head per character). That model is ~43 MB and TensorFlow is a heavyweight
dependency, so **neither is bundled here**. This module loads them *lazily*:

- the model is read once from ``belgium_assets/captcha_model.keras`` (sibling
  ``char_map.json`` describes the alphabet + image size), shared process-wide
  and guarded by locks (Keras inference is not thread-safe);
- if TensorFlow is not installed *or* the model file is absent, constructing a
  :class:`CaptchaSolver` raises :class:`CaptchaSolverUnavailable`, so the runner
  can fail the run **honestly** (empty 5-tuple + a diagnostic error) exactly like
  the Stadion scrapers fail without a residential proxy.

Challenge detection (the page ``<title>``) needs no model, so an unsolved
challenge is still recognised — and reported — even when the solver is absent.
"""

import io
import os
import re
import threading

from parsel import Selector

BASE_URL = "https://www.tennisenpadelvlaanderen.be/"
CAPTCHA_ENDPOINT = "https://www.tennisenpadelvlaanderen.be/__zenedge/c"
ROBOT_TITLE = "ben jij een robot?"

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "belgium_assets")
MODEL_PATH = os.path.join(_ASSETS_DIR, "captcha_model.keras")
MAP_PATH = os.path.join(_ASSETS_DIR, "char_map.json")

# Headers mirror the production spider's; the UA is left to curl_cffi's Chrome
# impersonation so the TLS fingerprint and UA stay consistent.
_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language": "en-US,en;q=0.9,fr;q=0.8,en-GB;q=0.7,nl;q=0.6",
    "upgrade-insecure-requests": "1",
}


class CaptchaSolverUnavailable(RuntimeError):
    """Raised when TensorFlow or the captcha model is not present."""


def _file_sha256(path):
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def materialize_uploaded_model(scraper, log=None):
    """Write a Settings-tab–uploaded captcha model to ``MODEL_PATH``.

    Hosted runs can't rely on a committed binary (we don't ship the ~43 MB model),
    so an admin uploads it and it lives in the DB (:class:`accounts.models.ScraperModelFile`).
    Before building the solver the worker calls this to drop those bytes onto disk
    where :meth:`CaptchaSolver._ensure_model` already looks. No-op when no upload
    exists or the on-disk file already matches the uploaded bytes (so a locally
    committed/dropped model is left untouched).
    """
    try:
        from accounts.models import ScraperModelFile

        rec = (
            ScraperModelFile.objects.filter(scraper=scraper)
            .defer("data")
            .first()
        )
    except Exception:  # noqa: BLE001 - DB unavailable / unmigrated: fall back to disk
        return
    if rec is None:
        return
    if (
        os.path.exists(MODEL_PATH)
        and os.path.getsize(MODEL_PATH) == rec.size
        and _file_sha256(MODEL_PATH) == rec.sha256
    ):
        return  # disk already has exactly these bytes
    os.makedirs(_ASSETS_DIR, exist_ok=True)
    tmp = MODEL_PATH + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(bytes(rec.data))  # touches the blob column only now
    os.replace(tmp, MODEL_PATH)
    if log:
        log("INFO", f"\U0001f4e6 captcha model loaded from upload ({rec.size} bytes)")


def page_title(html_text):
    """Lower-cased ``<title>`` of an HTML string (``""`` if none)."""
    if not html_text:
        return ""
    return (Selector(text=html_text).xpath("//title/text()").get() or "").strip().lower()


def is_challenge(html_text):
    """True when an HTML page is the Zenedge "are you a robot?" interstitial."""
    return ROBOT_TITLE in page_title(html_text)


class CaptchaSolver:
    """Decode Zenedge captchas with the vendored Keras model and clear the gate.

    The model + char map load once and are shared across every instance/thread;
    inference is serialised because Keras model calls are not thread-safe.
    """

    MAX_TRIES = 10
    MIN_CONFIDENCE = 0.50  # reject low-confidence guesses rather than wasting a POST

    _model = None
    _idx_to_char = None
    _img_w = None
    _img_h = None
    _load_lock = threading.Lock()
    _predict_lock = threading.Lock()

    def __init__(self, log):
        self.log = log
        # Eagerly load so an unavailable solver fails fast at run start, not
        # mid-scrape on the first challenged page.
        self._ensure_model()

    # -- model loading --------------------------------------------------
    @classmethod
    def _ensure_model(cls):
        if cls._model is not None:
            return
        with cls._load_lock:
            if cls._model is not None:
                return
            if not os.path.exists(MODEL_PATH):
                raise CaptchaSolverUnavailable(
                    f"captcha model not found at {MODEL_PATH}"
                )
            try:
                import json

                from tensorflow import keras
            except Exception as exc:  # noqa: BLE001 - TF optional/heavy
                raise CaptchaSolverUnavailable(
                    f"tensorflow/keras not importable: {exc}"
                ) from exc
            # A present-but-broken char map or model (corrupt file, version
            # mismatch) must honest-fail too — not crash the run mid-scrape.
            try:
                with open(MAP_PATH) as fh:
                    meta = json.load(fh)
                idx_to_char = {i: c for i, c in enumerate(meta["chars"])}
                img_w = int(meta["img_w"])
                img_h = int(meta["img_h"])
                _patch_batchnorm(keras)  # must run before load_model
                model = keras.models.load_model(MODEL_PATH)
            except Exception as exc:  # noqa: BLE001 - bad/incompatible captcha infra
                raise CaptchaSolverUnavailable(
                    f"captcha model/char-map could not be loaded: {exc}"
                ) from exc
            cls._idx_to_char = idx_to_char
            cls._img_w = img_w
            cls._img_h = img_h
            cls._model = model

    # -- inference ------------------------------------------------------
    def _preprocess(self, image_bytes):
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            img = im.convert("L").resize((self._img_w, self._img_h))
        arr = np.asarray(img, dtype=np.float32) / 255.0
        return arr.reshape(1, self._img_h, self._img_w, 1)

    def _predict(self, image_bytes):
        """Decode one image into ``(text, per_char_confidence)``."""
        import numpy as np

        x = self._preprocess(image_bytes)
        with self._predict_lock:  # Keras inference isn't thread-safe
            outputs = self._model(x, training=False)
        if not isinstance(outputs, (list, tuple)):
            outputs = [outputs]
        chars, confs = [], []
        for o in outputs:  # one softmax head per character position
            p = np.asarray(o)[0]
            i = int(np.argmax(p))
            chars.append(self._idx_to_char[i])
            confs.append(float(p[i]))
        return "".join(chars), confs

    def _solve_image(self, image_bytes):
        """Return the predicted code, or ``""`` if confidence is too low."""
        try:
            text, confs = self._predict(image_bytes)
        except Exception as exc:  # noqa: BLE001 - bad image / model mismatch
            self.log("WARN", f"captcha inference failed: {exc}")
            return ""
        avg = sum(confs) / len(confs) if confs else 0.0
        if avg < self.MIN_CONFIDENCE:
            self.log("WARN", f"captcha confidence {avg:.2f} < {self.MIN_CONFIDENCE}, rejecting")
            return ""
        return text  # case-sensitive, returned as predicted

    # -- challenge loop -------------------------------------------------
    def _reload(self, client, url):
        """Re-GET ``url`` for a fresh captcha; ``""`` on failure (loop exits)."""
        resp = client.get(url, headers=_HEADERS, tries=3)
        if resp is not None and 200 <= resp.status_code < 300:
            return resp.text
        return ""

    def solve_challenge(self, client, index_url, html_text):
        """Clear the Zenedge gate for ``index_url`` and return the real page HTML.

        ``html_text`` is the (already-fetched) interstitial. Returns the cleared
        page's HTML on success, or ``""`` if the captcha couldn't be solved
        within :attr:`MAX_TRIES`. Submits via the *same* ``client`` so the
        clearance cookie persists for subsequent requests.
        """
        if ROBOT_TITLE not in page_title(html_text):
            return html_text  # not challenged

        tries = 0
        while ROBOT_TITLE in page_title(html_text) and tries < self.MAX_TRIES:
            tries += 1
            try:
                sel = Selector(text=html_text)
                src = sel.xpath('//img[@alt="Captcha"]/@src').get()
                if not src:
                    html_text = self._reload(client, index_url)
                    continue

                try:
                    image_bytes = _decode_data_uri(src)
                except Exception as exc:  # noqa: BLE001 - malformed data URI
                    self.log("WARN", f"captcha decode failed: {exc}")
                    html_text = self._reload(client, index_url)
                    continue

                solution = self._solve_image(image_bytes)
                if not solution:
                    html_text = self._reload(client, index_url)
                    continue

                resp = client.post(
                    CAPTCHA_ENDPOINT,
                    params={"src": index_url.replace(BASE_URL, "/")},
                    data={"code": solution},
                    headers=_HEADERS,
                    tries=3,
                )
                if resp is not None and 200 <= resp.status_code < 300:
                    html_text = resp.text
                    if ROBOT_TITLE not in page_title(html_text):
                        self.log("INFO", f"captcha solved after {tries} try(s)")
                        return html_text
                    # Still challenged: response carries a NEW captcha — loop
                    # with the fresh page (do not re-solve the stale image).
                else:
                    html_text = self._reload(client, index_url)
            except Exception as exc:  # noqa: BLE001 - never spin on a bad state
                self.log("WARN", f"captcha attempt {tries} errored: {exc}")
                html_text = self._reload(client, index_url)

        self.log("WARN", f"captcha not solved after {tries} try(s)")
        return ""


def _decode_data_uri(data_uri):
    """Decode a ``data:image/png;base64,...`` URI to raw PNG bytes."""
    import html as html_mod
    from base64 import b64decode

    data_uri = html_mod.unescape(data_uri)  # &#x2F; -> /
    match = re.match(r"data:[^;,]*(?:;[^,]*)?,(.*)$", data_uri, re.DOTALL)
    if not match:
        raise ValueError("not a valid data URI")
    b64 = re.sub(r"\s+", "", match.group(1).strip())
    image_bytes = b64decode(b64, validate=True)
    if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"not a PNG (got {image_bytes[:8]!r})")
    return image_bytes


def _patch_batchnorm(keras):
    """Drop no-op ``renorm*`` kwargs some Keras builds serialise but newer ones
    reject, so a model saved by a different Keras version still loads."""
    BN = keras.layers.BatchNormalization
    if getattr(BN, "_renorm_compat_patched", False):
        return
    drop = ("renorm", "renorm_clipping", "renorm_momentum")
    orig_init = BN.__init__

    def patched_init(self, *args, **kwargs):
        if kwargs.get("renorm") is not True:
            for k in drop:
                kwargs.pop(k, None)
        orig_init(self, *args, **kwargs)

    BN.__init__ = patched_init
    BN._renorm_compat_patched = True
