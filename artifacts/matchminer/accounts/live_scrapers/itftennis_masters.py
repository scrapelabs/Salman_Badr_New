"""ITF Masters / Seniors circuit (itftennis.com, circuit code ``VT``).

Thin wrapper over the shared :mod:`accounts.live_scrapers._itftennis` engine.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _itftennis

CONFIG = _itftennis.ITFConfig(
    label="ITF Masters (Seniors)",
    circuit_title="Masters",
    circuit_code="VT",
    event_category="Seniors",
    sanction_body="ITF Seniors",
)


def run(run_obj, log):
    return _itftennis.run(CONFIG, run_obj, log)
