#!/usr/bin/env python3
"""Build a same-origin Eurojackpot feed for Drawline.

Source: Veikkaus (Finnish state operator) public draw-results API.
  https://www.veikkaus.fi/api/draw-results/v1/games/EJACKPOT/draws/by-week/YYYY-Www
That API is key-free but does not send CORS headers, so the browser cannot call it
directly. We fetch it server-side and write a normalized, same-origin JSON file that
Drawline can poll: eurojackpot.json
"""
import json
import datetime as dt
import os
from pathlib import Path
import re
import tempfile
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

try:
    import openpyxl
except ImportError:  # The standard test runtime has no optional workbook reader.
    openpyxl = None

WEEKS = 30
MIN_DRAWS = 58
MAX_AGE_DAYS = 10
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "eurojackpot.json"
HTML = ROOT / "index.html"
BASELINE_DIR = ROOT.parent / "data" / "baseline"
BASELINE_FILES = (BASELINE_DIR / "Eurojackpot_2025.xlsx", BASELINE_DIR / "Eurojackpot_2026.xlsx")
API = "https://www.veikkaus.fi/api/draw-results/v1/games/EJACKPOT/draws/by-week/{}"
EMBEDDED_FEED_RE = re.compile(r'(<script type="application/json" id="ejFeed">)(.*?)(</script>)', re.S)


def fetch(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Drawline/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def cents(v):
    return round(v / 100, 2) if _is_number(v) else None


class RefreshError(RuntimeError):
    """A refresh did not meet the conditions for publishing."""


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_non_negative_number(value):
    return _is_number(value) and value >= 0


def normalize_integer(value, field):
    """Accept JSON integers and integer-like upstream strings, nothing else."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    raise ValueError("%s is not an integer" % field)


def _normalize_optional_integer(record, field):
    value = record.get(field)
    return None if value is None else normalize_integer(value, field)


def _parse_timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is not timezone-aware")
    return parsed


def _header_token(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _column_index(headers, aliases):
    for index, header in enumerate(headers):
        if header in aliases:
            return index
    return None


def _xlsx_rows(path):
    """Read rows without changing baseline workbooks; prefer openpyxl when available."""
    if openpyxl is not None:
        # Some supplied workbooks have an incorrect A1 dimension; normal mode reads all cells without writing.
        workbook = openpyxl.load_workbook(path, read_only=False, data_only=True)
        try:
            for sheet in workbook.worksheets:
                yield from sheet.iter_rows(values_only=True)
        finally:
            workbook.close()
        return

    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall(namespace + "si")]
        for name in sorted(item for item in archive.namelist() if item.startswith("xl/worksheets/") and item.endswith(".xml")):
            root = ET.fromstring(archive.read(name))
            for row in root.findall(".//" + namespace + "row"):
                values = {}
                for cell in row.findall(namespace + "c"):
                    reference = cell.get("r", "A1")
                    letters = re.match(r"[A-Z]+", reference).group(0)
                    column = 0
                    for letter in letters:
                        column = column * 26 + ord(letter) - ord("A") + 1
                    value = cell.findtext(namespace + "v")
                    if cell.get("t") == "inlineStr":
                        inline = cell.find(namespace + "is")
                        value = "".join(inline.itertext()) if inline is not None else None
                    elif cell.get("t") == "s" and value is not None:
                        value = shared[int(value)]
                    values[column - 1] = value
                if values:
                    yield tuple(values.get(index) for index in range(max(values) + 1))


def _baseline_timestamp(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=value.tzinfo or dt.timezone.utc).astimezone(dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
            try:
                return dt.datetime.strptime(value.strip(), pattern).replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
    raise ValueError("draw date is not a supported date")


def _baseline_money(value, field):
    if value in (None, ""):
        return None
    if _is_number(value) and value >= 0:
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"\d+", value):
        return float(int(value))
    raise ValueError("%s is not a reliable non-negative amount" % field)


def _baseline_row_to_draw(row, columns):
    timestamp = _baseline_timestamp(row[columns["date"]])
    main = [normalize_integer(row[columns["main%d" % index]], "baseline main number") for index in range(1, 6)]
    euro = [normalize_integer(row[columns["euro%d" % index]], "baseline Euro number") for index in range(1, 3)]
    raw_id = row[columns["id"]] if columns["id"] is not None else None
    if raw_id in (None, ""):
        raw_id = int(timestamp.strftime("%Y%m%d") + "".join("%02d" % value for value in main + euro))
    else:
        raw_id = normalize_integer(raw_id, "baseline draw id")
    jackpot = _baseline_money(row[columns["jackpot"]], "baseline jackpot") if columns["jackpot"] is not None else None
    winners = _normalize_optional_integer({"winners": row[columns["winners"]]}, "winners") if columns["winners"] is not None else None
    draw = {
        "id": "eurojackpot-%s" % raw_id,
        "game": "eurojackpot", "gameName": "Eurojackpot",
        "drawnAt": timestamp.isoformat(), "numbers": main,
        "extra": {"label": "Euro numbers", "numbers": euro},
        "jackpot": jackpot, "jackpotWinners": winners, "totalWinners": None,
        "currency": "EUR", "source": "baseline",
    }
    errors = validate_draw(draw)
    if errors:
        raise ValueError("; ".join(errors))
    return draw


def parse_baseline_workbook(path):
    """Normalize one read-only historical workbook into validated V4 draws."""
    path = Path(path)
    rows = list(_xlsx_rows(path))
    report = {"path": path, "rows": 0, "valid": 0, "dateRange": None, "skipped": []}
    if not rows:
        return [], report
    greek_layout = len(rows) >= 4 and isinstance(rows[1][0], str) and "ΚΛΗΡ" in rows[1][0]
    if greek_layout:
        columns = {"id": None, "date": 1, "jackpot": None, "winners": None}
        columns.update({"main%d" % index: index + 1 for index in range(1, 6)})
        columns.update({"euro%d" % index: index + 6 for index in range(1, 3)})
        data_rows = rows[3:]
    else:
        headers = [_header_token(value) for value in rows[0]]
        columns = {
            "id": _column_index(headers, {"drawid", "drawnumber", "drawno", "draw", "id"}),
            "date": _column_index(headers, {"drawdate", "date", "drawingdate"}),
            "jackpot": _column_index(headers, {"jackpot", "jackpotamount"}),
            "winners": _column_index(headers, {"jackpotwinners", "winners", "winnercount"}),
        }
        for index in range(1, 6):
            columns["main%d" % index] = _column_index(headers, {"main%d" % index, "number%d" % index, "n%d" % index, "mainnumber%d" % index})
        for index in range(1, 3):
            columns["euro%d" % index] = _column_index(headers, {"euro%d" % index, "e%d" % index, "starnumber%d" % index})
        data_rows = rows[1:]
    required = ["date"] + ["main%d" % index for index in range(1, 6)] + ["euro%d" % index for index in range(1, 3)]
    if any(columns[name] is None for name in required):
        if len(rows) > 1:
            report["skipped"].append("header does not expose required Eurojackpot columns")
        return [], report
    first_row_number = 4 if greek_layout else 2
    for row_number, row in enumerate(data_rows, start=first_row_number):
        report["rows"] += 1
        try:
            if len(row) <= max(index for index in columns.values() if index is not None):
                raise ValueError("row is shorter than the required columns")
            draws = _baseline_row_to_draw(row, columns)
            report["valid"] += 1
            report.setdefault("draws", []).append(draws)
        except (TypeError, ValueError) as exc:
            report["skipped"].append("row %d: %s" % (row_number, exc))
    parsed = report.pop("draws", [])
    if parsed:
        dates = [_parse_timestamp(draw["drawnAt"]).date().isoformat() for draw in parsed]
        report["dateRange"] = (min(dates), max(dates))
    return parsed, report


def load_baseline_draws(paths=BASELINE_FILES):
    draws, reports = [], []
    for path in paths:
        parsed, report = parse_baseline_workbook(path)
        draws.extend(parsed)
        reports.append(report)
    return draws, reports


def _draw_fingerprint(draw):
    return (_parse_timestamp(draw["drawnAt"]).date().isoformat(), tuple(draw["numbers"]), tuple(draw["extra"]["numbers"]))


def merge_draws(baseline_draws, existing_draws, upstream_draws):
    """Keep authoritative upstream/current draws before historical baseline rows."""
    merged, ids, fingerprints = [], set(), set()
    authoritative = list(upstream_draws) + list(existing_draws)
    latest_authoritative = max((_parse_timestamp(draw["drawnAt"]) for draw in authoritative), default=None)
    authoritative_dates = {_parse_timestamp(draw["drawnAt"]).date().isoformat() for draw in authoritative}
    for source_index, source in enumerate((upstream_draws, existing_draws, baseline_draws)):
        for draw in source:
            if validate_draw(draw):
                continue
            if source_index == 2 and latest_authoritative and _parse_timestamp(draw["drawnAt"]) > latest_authoritative:
                continue
            if source_index == 2 and _parse_timestamp(draw["drawnAt"]).date().isoformat() in authoritative_dates:
                continue
            fingerprint = _draw_fingerprint(draw)
            if draw["id"] in ids or fingerprint in fingerprints:
                continue
            merged.append(draw)
            ids.add(draw["id"])
            fingerprints.add(fingerprint)
    return sorted(merged, key=lambda draw: draw["drawnAt"], reverse=True)


def load_existing_draws(path=OUT):
    if not Path(path).exists():
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [draw for draw in payload.get("draws", []) if not validate_draw(draw)]


def validate_draw(draw):
    """Return strict V4 validation errors for one normalized draw."""
    errors = []
    if not isinstance(draw, dict):
        return ["draw is not an object"]
    if not isinstance(draw.get("id"), str) or not re.fullmatch(r"eurojackpot-\d+", draw["id"]):
        errors.append("id must be eurojackpot-<numeric id>")
    try:
        _parse_timestamp(draw.get("drawnAt"))
    except (TypeError, ValueError) as exc:
        errors.append("invalid drawnAt: %s" % exc)

    def validate_numbers(values, count, upper, label):
        if not isinstance(values, list) or len(values) != count:
            errors.append("%s must contain exactly %d values" % (label, count))
        elif any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            errors.append("%s must contain integers" % label)
        elif len(set(values)) != count or any(value < 1 or value > upper for value in values):
            errors.append("%s must be distinct values in 1..%d" % (label, upper))

    validate_numbers(draw.get("numbers"), 5, 50, "main numbers")
    extra = draw.get("extra")
    validate_numbers(extra.get("numbers") if isinstance(extra, dict) else None, 2, 12, "Euro numbers")
    for field in ("jackpot", "jackpotWinners", "totalWinners"):
        value = draw.get(field)
        if value is not None and not _is_non_negative_number(value):
            errors.append("%s must be non-negative when present" % field)
    return errors


def validate_feed(feed, now=None):
    """Return feed-level validation errors, including completeness and freshness."""
    if not isinstance(feed, dict) or not isinstance(feed.get("draws"), list):
        return ["feed must contain a draws array"]
    errors, draws = [], feed["draws"]
    if feed.get("count") != len(draws):
        errors.append("count does not match draws length")
    if len(draws) < MIN_DRAWS:
        errors.append("only %d valid draws; minimum is %d" % (len(draws), MIN_DRAWS))
    seen, times = set(), []
    for index, draw in enumerate(draws):
        errors.extend("draw %d: %s" % (index, error) for error in validate_draw(draw))
        draw_id = draw.get("id") if isinstance(draw, dict) else None
        if draw_id in seen:
            errors.append("duplicate draw id: %s" % draw_id)
        seen.add(draw_id)
        try:
            times.append(_parse_timestamp(draw["drawnAt"]))
        except (KeyError, TypeError, ValueError):
            pass
    if len(times) == len(draws) and any(left < right for left, right in zip(times, times[1:])):
        errors.append("draws are not ordered by descending draw time")
    if times:
        reference = now or dt.datetime.now(dt.timezone.utc)
        if reference.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if reference - times[0] > dt.timedelta(days=MAX_AGE_DAYS):
            errors.append("latest draw is older than %d days" % MAX_AGE_DAYS)
    return errors


def map_record(raw):
    """Preserve the Veikkaus-to-V4 mapping while rejecting malformed input."""
    if not isinstance(raw, dict):
        raise ValueError("record is not an object")
    raw_id = normalize_integer(raw.get("id"), "draw id")
    results = raw.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise ValueError("missing result")
    result, primary, secondary = results[0], results[0].get("primary"), results[0].get("secondary")
    if not isinstance(primary, list) or not isinstance(secondary, list):
        raise ValueError("missing or invalid draw data")
    primary = [normalize_integer(value, "primary number") for value in primary]
    secondary = [normalize_integer(value, "secondary number") for value in secondary]
    draw_time = normalize_integer(raw.get("drawTime"), "drawTime")
    tiers, jackpots = raw.get("prizeTiers") or [], raw.get("jackpots") or []
    if not isinstance(tiers, list) or not isinstance(jackpots, list) or any(not isinstance(item, dict) for item in tiers + jackpots):
        raise ValueError("invalid prize data")
    for tier in tiers:
        for field in ("shareCount", "shareAmount"):
            if tier.get(field) is not None:
                tier[field] = _normalize_optional_integer(tier, field)
            if tier.get(field) is not None and not _is_non_negative_number(tier[field]):
                raise ValueError("invalid prize-tier %s" % field)
    for item in jackpots:
        if item.get("amount") is not None:
            item["amount"] = _normalize_optional_integer(item, "amount")
        if item.get("amount") is not None and not _is_non_negative_number(item["amount"]):
            raise ValueError("invalid jackpot amount")
    top = {tier.get("id"): tier for tier in tiers}.get("1") or {}
    jackpot_amounts = {item.get("id"): item.get("amount") for item in jackpots}
    draw = {
        "id": "eurojackpot-%s" % raw_id,
        "game": "eurojackpot", "gameName": "Eurojackpot",
        "drawnAt": dt.datetime.fromtimestamp(draw_time / 1000, dt.timezone.utc).isoformat(),
        "numbers": primary,
        "extra": {"label": "Euro numbers", "numbers": secondary},
        "jackpot": cents(top.get("shareAmount")) or cents(jackpot_amounts.get("PRIMARY")),
        "jackpotWinners": top.get("shareCount") or 0,
        "totalWinners": sum(tier.get("shareCount") or 0 for tier in tiers),
        "currency": "EUR", "source": "veikkaus.fi",
    }
    errors = validate_draw(draw)
    if errors:
        raise ValueError("; ".join(errors))
    return draw


def build_feed(fetcher=fetch, today=None, now=None, baseline_paths=BASELINE_FILES, existing_path=OUT):
    """Fetch each week and isolate both upstream and individual record failures."""
    today, now = today or dt.date.today(), now or dt.datetime.now(dt.timezone.utc)
    draws, diagnostics, seen = [], [], set()
    for back in range(WEEKS):
        day = today - dt.timedelta(weeks=back)
        iso = day.isocalendar()
        week = "%d-W%02d" % (iso[0], iso[1])
        try:
            payload = fetcher(API.format(week))
            if not isinstance(payload, list):
                raise ValueError("payload is not an array")
        except Exception as exc:
            diagnostics.append("upstream %s: %s" % (week, exc))
            continue
        for index, raw in enumerate(payload):
            try:
                draw = map_record(raw)
                if draw["id"] in seen:
                    raise ValueError("duplicate draw id")
                seen.add(draw["id"])
                draws.append(draw)
            except Exception as exc:
                diagnostics.append("record %s[%d]: %s" % (week, index, exc))
    baseline_draws, _reports = load_baseline_draws(baseline_paths)
    draws = merge_draws(baseline_draws, load_existing_draws(existing_path), draws)
    return ({"game": "eurojackpot", "generatedAt": now.astimezone(dt.timezone.utc).isoformat(),
             "source": "https://www.veikkaus.fi/api/draw-results/v1/games/EJACKPOT",
             "count": len(draws), "draws": draws}, diagnostics)


def extract_embedded_feed(html):
    match = EMBEDDED_FEED_RE.search(html)
    if not match:
        raise ValueError("ejFeed script tag not found")
    return json.loads(match.group(2).replace("<\\/", "</"))


def render_html_with_feed(template, feed):
    inline = json.dumps(feed, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    rendered, replacements = EMBEDDED_FEED_RE.subn(lambda match: match.group(1) + inline + match.group(3), template, count=1)
    if replacements != 1:
        raise ValueError("ejFeed script tag not found")
    return rendered


def _stage(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return Path(temporary)


def publish(feed, json_path=OUT, html_path=HTML, now=None):
    """Validate staged artifacts before atomically replacing published files."""
    json_path, html_path = Path(json_path), Path(html_path)
    errors = validate_feed(feed, now=now)
    if errors:
        raise RefreshError("feed validation failed: " + "; ".join(errors))
    staged_json = staged_html = None
    try:
        staged_json = _stage(json_path, json.dumps(feed, ensure_ascii=False, indent=1))
        staged_html = _stage(html_path, render_html_with_feed(html_path.read_text(encoding="utf-8"), feed))
        reparsed_json = json.loads(staged_json.read_text(encoding="utf-8"))
        reparsed_html = extract_embedded_feed(staged_html.read_text(encoding="utf-8"))
        errors = validate_feed(reparsed_json, now=now) + validate_feed(reparsed_html, now=now)
        if errors:
            raise RefreshError("staged validation failed: " + "; ".join(errors))
        if reparsed_json != reparsed_html:
            raise RefreshError("standalone and embedded feeds are not equivalent")
        os.replace(staged_json, json_path)
        staged_json = None
        os.replace(staged_html, html_path)
        staged_html = None
    finally:
        for temporary in (staged_json, staged_html):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def refresh(fetcher=fetch, today=None, now=None, json_path=OUT, html_path=HTML):
    feed, diagnostics = build_feed(fetcher=fetcher, today=today, now=now, existing_path=json_path)
    errors = diagnostics + validate_feed(feed, now=now)
    if errors:
        raise RefreshError("refresh refused:\n" + "\n".join(errors))
    publish(feed, json_path=json_path, html_path=html_path, now=now)
    return feed


def main():
    try:
        feed = refresh()
    except RefreshError as exc:
        print(exc)
        return 1
    print("published", OUT, len(feed["draws"]), "draws")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
