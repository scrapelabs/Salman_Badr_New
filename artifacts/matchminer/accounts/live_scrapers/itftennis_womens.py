"""ITF Women's World Tennis Tour / Pro Circuit (itftennis.com, circuit code ``WT``).

Thin wrapper over the shared :mod:`accounts.live_scrapers._itftennis` engine.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _itftennis

CONFIG = _itftennis.ITFConfig(
    label="ITF Women's Pro Circuit",
    circuit_title="Womens",
    circuit_code="WT",
    event_category="Pro Circuit",
    sanction_body="ITF Procircuit",
)


def run(run_obj, log):
    return _itftennis.run(CONFIG, run_obj, log)
