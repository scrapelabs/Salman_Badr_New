"""ITF Juniors circuit (itftennis.com, circuit code ``JT``).

Thin wrapper over the shared :mod:`accounts.live_scrapers._itftennis` engine.
``run(run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from . import _itftennis

CONFIG = _itftennis.ITFConfig(
    label="ITF Juniors",
    circuit_title="Juniors",
    circuit_code="JT",
    event_category="ITF Junior",
    sanction_body="ITF juniors",
)


def run(run_obj, log):
    return _itftennis.run(CONFIG, run_obj, log)
