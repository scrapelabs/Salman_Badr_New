"""Tennis New Zealand individual tournaments with member enrichment.

The match crawl uses the shared TournamentSoftware engine. Player DOB and gender
come from the database registry populated by ``import_new_zealand_members``.
Only the exact TournamentSoftware third-party ID / CSV ``National ID`` is used
for the join; names are never used as identifiers.
"""

import csv
import io
import re
from datetime import date

from . import _ts_tournament

REQUIRED_COLUMNS = (
    "Surname",
    "Name",
    "National ID",
    "Birth Day",
    "Birth Month",
    "Birth Year",
    "Gender",
)


class MemberRegistryError(RuntimeError):
    """A safe, operator-facing member-registry failure."""


def _header_key(value):
    return (value or "").lstrip("\ufeff").strip().casefold()


def _cell(row, column):
    value = row.get(column, "")
    return value.strip() if isinstance(value, str) else ""


def _date_number(value, digits):
    value = (value or "").strip()
    match = re.fullmatch(rf"(\d{{1,{digits}}})(?:\.0+)?", value)
    return int(match.group(1)) if match else None


def _member_dob(row, columns):
    day = _date_number(_cell(row, columns["Birth Day"]), 2)
    month = _date_number(_cell(row, columns["Birth Month"]), 2)
    year = _date_number(_cell(row, columns["Birth Year"]), 4)
    if day is None or month is None or year is None or year < 1900:
        return ""
    try:
        birth_date = date(year, month, day)
    except ValueError:
        return ""
    return birth_date.strftime("%m/%d/%Y") if birth_date <= date.today() else ""


def _member_gender(value):
    value = (value or "").strip().upper()
    return value if value in {"M", "F", "O"} else ""


def _decode_member_csv(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _parse_member_csv(text):
    """Return exact National ID -> DOB/gender details from one member CSV."""
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames or []
    header_keys = [_header_key(name) for name in fieldnames]
    if len(header_keys) != len(set(header_keys)):
        raise MemberRegistryError("The member CSV has duplicate columns")
    available = dict(zip(header_keys, fieldnames))
    missing = [name for name in REQUIRED_COLUMNS if _header_key(name) not in available]
    if missing:
        raise MemberRegistryError(
            "The member CSV is missing required columns: " + ", ".join(missing)
        )
    columns = {name: available[_header_key(name)] for name in REQUIRED_COLUMNS}

    players = {}
    conflicts = set()
    for row in reader:
        # DictReader uses a None key/value when a malformed row has too many or
        # too few cells. Skip it rather than shifting values into the ID fields.
        if None in row or any(row.get(columns[name]) is None for name in REQUIRED_COLUMNS):
            continue
        national_id = _cell(row, columns["National ID"])
        if not national_id or len(national_id) > 255 or "\ufffd" in national_id:
            continue
        details = players.setdefault(national_id, {"dob": "", "gender": ""})
        dob = _member_dob(row, columns)
        gender = _member_gender(_cell(row, columns["Gender"]))
        # Duplicate IDs are common. Matching values are harmless; a later row
        # may fill a blank field, but conflicting values are discarded so the
        # registry never silently picks one person's details over another's.
        for field, value in (("dob", dob), ("gender", gender)):
            conflict_key = (national_id, field)
            if not value or conflict_key in conflicts:
                continue
            if details[field] and details[field] != value:
                details[field] = ""
                conflicts.add(conflict_key)
            elif not details[field]:
                details[field] = value
    return players


def load_player_enrichment(log, tele):
    del tele  # Database reads do not produce HTTP request telemetry.
    from accounts.models import NewZealandMember

    rows = list(
        NewZealandMember.objects.order_by().values_list("national_id", "dob", "gender")
    )
    if not rows:
        raise MemberRegistryError(
            "New Zealand member registry is empty; run "
            "python manage.py import_new_zealand_members <csv_path>"
        )
    players = {
        national_id: {
            "dob": dob.strftime("%m/%d/%Y") if dob else "",
            "gender": gender or "",
        }
        for national_id, dob, gender in rows
    }
    log("INFO", f"Player registry ready: {len(players)} National ID(s)")
    return players


CONFIG = _ts_tournament.TSTournamentConfig(
    label="New Zealand Tournament",
    base="https://tnz.tournamentsoftware.com",
    country="New Zealand",
    country_code="NZL",
    sanction_body="Tennis New Zealand",
    import_source="Tennis New Zealand",
)


def run(run_obj, log):
    return _ts_tournament.run(
        CONFIG,
        run_obj,
        log,
        player_enrichment_loader=load_player_enrichment,
    )
