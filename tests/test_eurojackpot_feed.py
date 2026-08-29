import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
APP = REPOSITORY / "EuroJackpot_EV_Set"
BUILDER_PATH = APP / "build_eurojackpot_feed.py"
SPEC = importlib.util.spec_from_file_location("feed_builder", BUILDER_PATH)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
NOW = dt.datetime(2026, 8, 29, tzinfo=dt.timezone.utc)


class EurojackpotFeedTests(unittest.TestCase):
    def setUp(self):
        self.feed = json.loads((APP / "eurojackpot.json").read_text(encoding="utf-8"))
        self.html = (APP / "index.html").read_text(encoding="utf-8")

    def test_existing_baseline_is_complete_valid_and_embedded_equivalent(self):
        embedded = builder.extract_embedded_feed(self.html)
        self.assertEqual(self.feed, embedded)
        self.assertEqual(58, self.feed["count"])
        self.assertEqual(58, len(self.feed["draws"]))
        self.assertEqual([], builder.validate_feed(self.feed, now=NOW))
        standalone_fields = [
            (draw["id"], draw["drawnAt"], draw["numbers"], draw["extra"]["numbers"],
             draw["jackpot"], draw["jackpotWinners"])
            for draw in self.feed["draws"]
        ]
        embedded_fields = [
            (draw["id"], draw["drawnAt"], draw["numbers"], draw["extra"]["numbers"],
             draw["jackpot"], draw["jackpotWinners"])
            for draw in embedded["draws"]
        ]
        self.assertEqual(standalone_fields, embedded_fields)
        self.assertEqual(("eurojackpot-983", "2026-08-21T17:04:00+00:00", [25, 35, 45, 46, 50], [4, 8], 31000000.0, 0), standalone_fields[0])
        self.assertEqual(("eurojackpot-979", "2026-08-07T17:04:00+00:00", [1, 3, 6, 13, 23], [5, 7], 32658025.0, 1), standalone_fields[4])

    def test_validation_rejects_each_strict_rule(self):
        cases = [
            (lambda feed: feed["draws"][0].update(id="eurojackpot-x"), "numeric id"),
            (lambda feed: feed["draws"][0].update(drawnAt="2026-08-21T17:04:00"), "timezone-aware"),
            (lambda feed: feed["draws"][0].update(numbers=[1, 1, 2, 3, 4]), "main numbers"),
            (lambda feed: feed["draws"][0]["extra"].update(numbers=[1, 1]), "Euro numbers"),
            (lambda feed: feed["draws"][0].update(jackpot=-1), "jackpot must be non-negative"),
            (lambda feed: feed["draws"].__setitem__(1, copy.deepcopy(feed["draws"][0])), "duplicate draw id"),
            (lambda feed: feed["draws"].__setitem__(0, feed["draws"].pop()), "descending draw time"),
        ]
        for mutate, expected in cases:
            with self.subTest(expected=expected):
                feed = copy.deepcopy(self.feed)
                mutate(feed)
                self.assertTrue(any(expected in error for error in builder.validate_feed(feed, now=NOW)))
        incomplete = copy.deepcopy(self.feed)
        incomplete["draws"] = incomplete["draws"][:57]
        incomplete["count"] = 57
        self.assertTrue(any("minimum is 58" in error for error in builder.validate_feed(incomplete, now=NOW)))
        self.assertTrue(any("older than 10 days" in error for error in builder.validate_feed(self.feed, now=NOW + dt.timedelta(days=11))))

    def test_mapping_preserves_v4_jackpot_and_winner_fields(self):
        raw = {
            "id": "983", "drawTime": 1787331840000,
            "results": [{"primary": [25, 35, 45, 46, 50], "secondary": [4, 8]}],
            "prizeTiers": [{"id": "1", "shareAmount": 3100000000, "shareCount": 0}, {"id": "2", "shareCount": 9}],
            "jackpots": [{"id": "PRIMARY", "amount": 999}],
        }
        draw = builder.map_record(raw)
        self.assertEqual("eurojackpot-983", draw["id"])
        self.assertEqual([25, 35, 45, 46, 50], draw["numbers"])
        self.assertEqual([4, 8], draw["extra"]["numbers"])
        self.assertEqual(31000000.0, draw["jackpot"])
        self.assertEqual(0, draw["jackpotWinners"])
        self.assertEqual(9, draw["totalWinners"])

    def test_mapping_normalizes_integer_like_upstream_strings_before_validation(self):
        raw = {
            "id": "983", "drawTime": "1787331840000",
            "results": [{"primary": ["25", "35", "45", "46", "50"], "secondary": ["4", "8"]}],
            "prizeTiers": [{"id": "1", "shareAmount": "3100000000", "shareCount": "0"}, {"id": "2", "shareCount": "9"}],
            "jackpots": [{"id": "PRIMARY", "amount": "999"}],
        }
        draw = builder.map_record(raw)
        self.assertEqual([25, 35, 45, 46, 50], draw["numbers"])
        self.assertEqual([4, 8], draw["extra"]["numbers"])
        self.assertEqual(31000000.0, draw["jackpot"])
        self.assertEqual(0, draw["jackpotWinners"])
        self.assertEqual(9, draw["totalWinners"])
        self.assertEqual([], builder.validate_draw(draw))

    def test_mapping_rejects_non_integer_upstream_values(self):
        raw = {
            "id": "983", "drawTime": "1787331840000.5",
            "results": [{"primary": ["25", "35", "45", "46", "50"], "secondary": ["4", "8"]}],
            "prizeTiers": [], "jackpots": [],
        }
        with self.assertRaisesRegex(ValueError, "drawTime is not an integer"):
            builder.map_record(raw)
        raw["drawTime"] = "1787331840000"
        raw["results"][0]["primary"][0] = "25.0"
        with self.assertRaisesRegex(ValueError, "primary number is not an integer"):
            builder.map_record(raw)

    def test_baseline_workbooks_are_discovered_and_parse_read_only(self):
        draws, reports = builder.load_baseline_draws()
        self.assertEqual(["Eurojackpot_2025.xlsx", "Eurojackpot_2026.xlsx"], [report["path"].name for report in reports])
        self.assertTrue(draws)
        self.assertTrue(all(report["valid"] > 0 for report in reports))
        self.assertTrue(all(report["dateRange"] is not None for report in reports))
        self.assertTrue(all(not builder.validate_draw(draw) for draw in draws))

    def test_tabular_baseline_rows_normalize_and_pass_strict_validation(self):
        rows = iter([
            ("Draw number", "Draw date", "Number 1", "Number 2", "Number 3", "Number 4", "Number 5", "Euro 1", "Euro 2", "Jackpot", "Jackpot winners"),
            ("900", "2025-01-03", "1", "2", "3", "4", "5", "6", "7", "17000000", "0"),
        ])
        original_rows = builder._xlsx_rows
        builder._xlsx_rows = lambda _path: rows
        try:
            draws, report = builder.parse_baseline_workbook("unused.xlsx")
        finally:
            builder._xlsx_rows = original_rows
        self.assertEqual(1, report["valid"])
        self.assertEqual(("2025-01-03", "2025-01-03"), report["dateRange"])
        self.assertEqual([], report["skipped"])
        self.assertEqual([1, 2, 3, 4, 5], draws[0]["numbers"])
        self.assertEqual([6, 7], draws[0]["extra"]["numbers"])
        self.assertEqual([], builder.validate_draw(draws[0]))

    def test_baseline_merge_deduplicates_and_upstream_takes_precedence(self):
        upstream = copy.deepcopy(self.feed["draws"][0])
        upstream["source"] = "veikkaus.fi"
        baseline = copy.deepcopy(upstream)
        baseline["id"] = "eurojackpot-999999"
        baseline["source"] = "baseline"
        baseline["jackpot"] = 1.0
        baseline["numbers"] = [1, 2, 3, 4, 5]
        merged = builder.merge_draws([baseline], [], [upstream])
        self.assertEqual(1, len(merged))
        self.assertEqual(upstream, merged[0])

    def test_failures_are_diagnostic_and_preserve_last_good_files(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            json_path, html_path = directory / "eurojackpot.json", directory / "index.html"
            shutil.copy2(APP / "eurojackpot.json", json_path)
            shutil.copy2(APP / "index.html", html_path)
            before_json, before_html = json_path.read_bytes(), html_path.read_bytes()

            def failing_fetcher(_url):
                raise OSError("offline")

            with self.assertRaises(builder.RefreshError) as raised:
                builder.refresh(fetcher=failing_fetcher, today=NOW.date(), now=NOW, json_path=json_path, html_path=html_path)
            self.assertIn("upstream", str(raised.exception))
            self.assertEqual(before_json, json_path.read_bytes())
            self.assertEqual(before_html, html_path.read_bytes())

            feed, diagnostics = builder.build_feed(
                fetcher=lambda _url: [{"id": "bad"}], today=NOW.date(), now=NOW,
                baseline_paths=(), existing_path=directory / "missing.json",
            )
            self.assertEqual([], feed["draws"])
            self.assertEqual(30, len(diagnostics))
            self.assertTrue(all("record" in message for message in diagnostics))

    def test_staged_publish_reparses_and_replaces_only_after_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            json_path, html_path = directory / "eurojackpot.json", directory / "index.html"
            json_path.write_text('{"old":true}', encoding="utf-8")
            html_path.write_text(self.html, encoding="utf-8")
            staged = []
            original_stage = builder._stage

            def recording_stage(path, content):
                temporary = original_stage(path, content)
                staged.append(temporary)
                return temporary

            builder._stage = recording_stage
            try:
                builder.publish(self.feed, json_path=json_path, html_path=html_path, now=NOW)
            finally:
                builder._stage = original_stage
            self.assertEqual(self.feed, json.loads(json_path.read_text(encoding="utf-8")))
            self.assertEqual(self.feed, builder.extract_embedded_feed(html_path.read_text(encoding="utf-8")))
            self.assertTrue(all(not path.exists() for path in staged))

    def test_jackpot_only_ev_probability_and_formula_are_unchanged(self):
        match = re.search(r"const P = 1 / (\d+);", self.html)
        self.assertIsNotNone(match)
        self.assertEqual("139838160", match.group(1))
        self.assertIn("const evUnits = P*b - (1-P);", self.html)
        self.assertIn("const evEuro = evUnits * stake;", self.html)


if __name__ == "__main__":
    unittest.main()
