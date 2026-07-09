"""ITF Men's World Tennis Tour / Pro Circuit (itftennis.com, circuit code ``MT``).

Thin wrapper over the shared :mod:`accounts.live_scrapers._itftennis` engine.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _itftennis

CONFIG = _itftennis.ITFConfig(
    label="ITF Men's Pro Circuit",
    circuit_title="Mens",
    circuit_code="MT",
    event_category="Pro Circuit",
    sanction_body="ITF Procircuit",
)


def run(run_obj, log):
    return _itftennis.run(CONFIG, run_obj, log)
