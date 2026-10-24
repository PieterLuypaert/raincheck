"""Tests for raincheck. Run with: python3 -m unittest"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zlib
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("raincheck", os.path.join(HERE, "raincheck"))
raincheck = importlib.util.module_from_spec(importlib.util.spec_from_loader("raincheck", _loader))
sys.modules["raincheck"] = raincheck
_loader.exec_module(raincheck)

_module_patches = contextlib.ExitStack()


def setUpModule():
    """Keeps every test away from the real cache in ~/.cache."""
    cache = _module_patches.enter_context(tempfile.TemporaryDirectory())
    _module_patches.enter_context(mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache}))


def tearDownModule():
    _module_patches.close()


START = datetime(2026, 10, 13, 12, 30).astimezone()
GENT = raincheck.Place("Gent", 51.05, 3.72)


def slots(*mm):
    return [raincheck.Slot(START + index * raincheck.STEP, value) for index, value in enumerate(mm)]


def plain(text):
    return raincheck.ANSI.sub("", text)


class SummaryTest(unittest.TestCase):
    def test_dry_all_the_way(self):
        self.assertEqual(raincheck.summarize(slots(0, 0, 0, 0)), "dry until at least 12:50")

    def test_rain_on_the_way(self):
        forecast = slots(0, 0, 0.8, 1.2, 0.6, 0, 0, 0)
        self.assertEqual(raincheck.summarize(forecast), "dry until 12:40, then 15 min of light rain")

    def test_rain_now_that_stops(self):
        forecast = slots(3, 2.5, 0, 0, 0)
        self.assertEqual(raincheck.summarize(forecast), "moderate rain now, dry from 12:40")

    def test_rain_now_past_the_horizon(self):
        self.assertEqual(raincheck.summarize(slots(0.2, 0.3, 0.1)), "drizzle now, not dry before 12:45")

    def test_rain_that_starts_and_does_not_stop(self):
        self.assertEqual(raincheck.summarize(slots(0, 0, 6, 8)), "dry until 12:40, then heavy rain")

    def test_a_range_of_intensities(self):
        forecast = slots(0, 0.1, 1, 4, 12, 3, 0.2, 0, 0)
        self.assertEqual(raincheck.summarize(forecast), "dry until 12:35, then 30 min of light to very heavy rain")

    def test_a_second_shower(self):
        forecast = slots(0, 1, 1, 0, 0, 0, 0.2, 0.3, 0)
        self.assertEqual(
            raincheck.summarize(forecast),
            "dry until 12:35, then 10 min of light rain, more drizzle around 13:00",
        )

    def test_a_dry_gap_of_five_minutes_does_not_end_the_spell(self):
        forecast = slots(1, 1, 0, 1, 0, 0, 0)
        self.assertEqual(raincheck.summarize(forecast), "light rain now, dry from 12:50")

    def test_long_spells_in_hours(self):
        self.assertEqual(raincheck.duration(25), "25 min")
        self.assertEqual(raincheck.duration(60), "1 hour")
        self.assertEqual(raincheck.duration(90), "1h30")
        self.assertEqual(raincheck.duration(120), "2 hours")

    def test_peak(self):
        self.assertEqual(raincheck.peak(slots(0, 2.4, 1)), "heaviest at 12:35: 2.4 mm per hour")
        self.assertIsNone(raincheck.peak(slots(0, 0)))


class KindTest(unittest.TestCase):
    def test_bounds(self):
        names = [raincheck.kind_of(mm).label for mm in (0.1, 0.5, 1.9, 2, 5, 10, 40)]
        self.assertEqual(names, ["drizzle", "light", "light", "moderate", "heavy", "very heavy", "very heavy"])


class TripTest(unittest.TestCase):
    NOW = START.replace(minute=33)

    def plan(self, *mm, minutes=25, now=None):
        forecast = slots(*mm)
        return raincheck.describe_trip(raincheck.plan_trip(forecast, now or self.NOW, minutes), forecast)

    def test_dry_all_the_way(self):
        self.assertEqual(self.plan(*[0] * 24), "leave whenever you like, no rain in sight")

    def test_rain_on_the_way(self):
        self.assertEqual(self.plan(*[0] * 8, 1, *[0] * 15), "leave by 12:45 to stay dry for 25 min")

    def test_rain_now_that_stops(self):
        self.assertEqual(self.plan(1, 1, 1, *[0] * 21), "leave from 12:45 to stay dry for 25 min")

    def test_a_dry_spell_between_showers(self):
        self.assertEqual(self.plan(1, 1, *[0] * 7, *[1] * 15), "leave between 12:40 and 12:50 to stay dry for 25 min")

    def test_a_dry_spell_that_just_fits(self):
        self.assertEqual(self.plan(1, 1, *[0] * 5, *[1] * 17), "leave at 12:40 to stay dry for 25 min")

    def test_leave_right_now(self):
        self.assertEqual(self.plan(*[0] * 5, *[1] * 19, now=START), "leave now to stay dry for 25 min")

    def test_rain_in_the_slot_that_is_running(self):
        self.assertEqual(self.plan(1, *[0] * 23), "leave from 12:35 to stay dry for 25 min")

    def test_no_dry_spell_long_enough(self):
        self.assertEqual(self.plan(*[1, 0, 0] * 8), "no dry 25 min before 14:30")

    def test_longer_than_the_forecast(self):
        with self.assertRaisesRegex(raincheck.RaincheckError, "reaches only until 14:30, so a trip can take at most 117 minutes"):
            self.plan(*[0] * 24, minutes=120)

    def test_the_trip_is_drawn_on_the_axis(self):
        forecast = slots(1, 1, *[0] * 7, *[1] * 15)
        trip = raincheck.plan_trip(forecast, self.NOW, 25)
        window = raincheck.trip_window(forecast, trip)
        self.assertEqual(window, range(2, 9))
        line = raincheck.axis(forecast, 2, window)
        self.assertEqual(set(line[4:18]), {"━", "┯"})
        self.assertNotIn("━", line[:4] + line[18:])
        text = plain(raincheck.render(GENT, forecast, self.NOW, False, 100, trip))
        self.assertIn("leave between 12:40 and 12:50 to stay dry for 25 min", text)

    def test_a_trip_that_cannot_be_made_is_not_drawn_or_green(self):
        forecast = slots(*[1, 0, 0] * 8)
        trip = raincheck.plan_trip(forecast, self.NOW, 25)
        self.assertEqual(raincheck.trip_window(forecast, trip), range(0))
        text = raincheck.render(GENT, forecast, self.NOW, True, 100, trip)
        self.assertNotIn(f"38;5;{raincheck.TRIP_COLOR}m", text)
        self.assertIn("no dry 25 min before 14:30", plain(text))

    def test_minutes_are_required(self):
        for argv in (["--trip"], ["--trip", "gent"], ["--trip", "0"], ["--trip", "²"], ["--trip", "99999999999999"]):
            with self.assertRaisesRegex(raincheck.RaincheckError, "say how many minutes"):
                raincheck.parse_args(argv)


class ChartTest(unittest.TestCase):
    def render(self, forecast, width=100, color=False):
        now = START.replace(minute=33)
        return raincheck.render(GENT, forecast, now, color, width)

    def test_more_rain_draws_a_higher_bar(self):
        levels = [raincheck.bar_level(mm) for mm in (0, 0.1, 0.5, 2, 10, 50)]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(levels[0], 0)
        self.assertEqual(levels[1], 1)
        self.assertEqual(levels[-1], raincheck.ROWS * 8)

    def test_a_dry_forecast_draws_no_bars(self):
        text = self.render(slots(*[0] * 24))
        self.assertFalse(set(text) & set(raincheck.BLOCKS.strip()))
        self.assertIn("dry until at least 14:30", text)

    def test_a_dry_chart_says_so_in_its_middle_row(self):
        rows = raincheck.chart_rows(slots(*[0] * 24), 2, raincheck.Paint(False))
        self.assertEqual(rows[0], "")
        self.assertEqual(rows[1].strip(), "no rain in sight")
        self.assertEqual(len(rows[1]) - len(rows[1].lstrip()), (48 - len("no rain in sight")) // 2)

    def test_a_dry_chart_too_narrow_for_the_words_stays_empty(self):
        self.assertEqual(raincheck.chart_rows(slots(0, 0, 0), 2, raincheck.Paint(False)), ["", "", ""])

    def test_a_shower_draws_bars_in_its_own_columns(self):
        rows = raincheck.chart_rows(slots(0, 0, 10, 0), 2, raincheck.Paint(False))
        self.assertEqual([row.rstrip() for row in rows], ["    ██", "    ██", "    ██"])

    def test_axis_and_labels(self):
        forecast = slots(*[0] * 13)
        self.assertEqual(raincheck.axis(forecast, 1), "┬─────┬─────┬")
        self.assertEqual(raincheck.time_labels(forecast, 2), "now         13:00       13:30")
        self.assertEqual(raincheck.time_labels(forecast, 1), "now   13:00")

    def test_title_summary_and_legend(self):
        text = plain(self.render(slots(0, 0.1, 3, 0)))
        self.assertIn("Gent · 12:33", text)
        self.assertIn("drizzle █  moderate █", text)
        self.assertNotIn("light █", text)
        self.assertIn("dry until 12:35, then 10 min of light to moderate rain", text)
        self.assertIn("heaviest at 12:40: 3.0 mm per hour", text)

    def test_narrow_terminal_uses_one_column_per_slot(self):
        forecast = slots(*[1] * 36)
        wide = self.render(forecast, width=80).splitlines()
        narrow = self.render(forecast, width=60).splitlines()
        self.assertEqual(max(map(len, wide)), 2 + 72)
        self.assertLessEqual(max(map(len, narrow)), 60)

    def test_color_only_when_asked(self):
        forecast = slots(0, 1, 0)
        self.assertIn("\033[", self.render(forecast, color=True))
        self.assertNotIn("\033[", self.render(forecast, color=False))


def radar_answer(*mm):
    """Buienradar's answer with a slot every five minutes from 10:30 UTC."""
    return {
        "forecasts": [
            {"utcdatetime": f"2026-10-13T10:{30 + 5 * index}:00", "precipitation": value}
            for index, value in enumerate(mm)
        ]
    }


class ForecastTest(unittest.TestCase):
    def test_starts_at_the_slot_that_is_running(self):
        now = datetime(2026, 10, 13, 10, 37, tzinfo=timezone.utc)
        forecast = raincheck.parse_forecast(radar_answer(0, 0.5, 1.5, 0), now)
        self.assertEqual(forecast[0].time, datetime(2026, 10, 13, 10, 35, tzinfo=timezone.utc))
        self.assertEqual([slot.mm for slot in forecast], [0.5, 1.5, 0])

    def test_stale_forecast(self):
        now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(raincheck.RaincheckError, "out of date"):
            raincheck.parse_forecast(radar_answer(0, 0), now)

    def test_unexpected_answer(self):
        with self.assertRaisesRegex(raincheck.RaincheckError, "unexpected"):
            raincheck.parse_forecast({"forecasts": [{"precipitation": 1}]}, START)

    def test_outside_the_radar(self):
        with mock.patch.object(raincheck, "fetch_json", return_value=None):
            with self.assertRaisesRegex(raincheck.RaincheckError, "no radar for Madrid"):
                raincheck.fetch_forecast(raincheck.Place("Madrid", 40.4, -3.7), START)

    def test_a_connection_that_drops_halfway(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = raincheck.http.client.IncompleteRead(b"{")
        with mock.patch.object(raincheck.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(raincheck.RaincheckError, "no usable answer from graphdata.buienradar.nl"):
                raincheck.fetch_json("https://graphdata.buienradar.nl/x")

    def test_unreachable(self):
        error = urllib.error.URLError("no route")
        with mock.patch.object(raincheck.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(raincheck.RaincheckError, "unreachable"):
                raincheck.fetch_json("https://graphdata.buienradar.nl/x")


class CacheTest(unittest.TestCase):
    NOW = datetime(2026, 10, 13, 10, 32, tzinfo=timezone.utc)
    UNREACHABLE = raincheck.RaincheckError("graphdata.buienradar.nl is unreachable (no route)")

    def setUp(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache.name}).start()
        self.fetch_json = mock.patch.object(raincheck, "fetch_json", return_value=radar_answer(0, 0, 0, 1, 0)).start()
        self.addCleanup(mock.patch.stopall)

    def fetch(self, minutes_later=0):
        return raincheck.fetch_forecast(GENT, self.NOW + timedelta(minutes=minutes_later))

    def test_a_fresh_forecast_is_reused(self):
        self.fetch()
        self.fetch(4)
        self.assertEqual(self.fetch_json.call_count, 1)

    def test_an_older_one_is_fetched_again(self):
        self.fetch()
        self.fetch(5)
        self.assertEqual(self.fetch_json.call_count, 2)

    def test_offline_shows_the_last_forecast(self):
        self.fetch()
        self.fetch_json.side_effect = self.UNREACHABLE
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            forecast = self.fetch(10)
        self.assertEqual([slot.mm for slot in forecast], [0, 1, 0])
        self.assertIn(f"unreachable (no route), showing the forecast from {raincheck.clock(self.NOW.astimezone())}", err.getvalue())

    def test_offline_without_a_forecast(self):
        self.fetch_json.side_effect = self.UNREACHABLE
        with self.assertRaisesRegex(raincheck.RaincheckError, "unreachable"):
            self.fetch()

    def test_offline_when_the_last_forecast_is_over(self):
        self.fetch()
        self.fetch_json.side_effect = self.UNREACHABLE
        with self.assertRaisesRegex(raincheck.RaincheckError, "unreachable"):
            self.fetch(60)

    def test_an_unreadable_cache_is_fetched_again(self):
        path = raincheck.cache_file(GENT)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.assertEqual([slot.mm for slot in self.fetch()], [0, 0, 0, 1, 0])
        self.assertEqual(self.fetch_json.call_count, 1)

    def test_a_broken_answer_keeps_the_last_good_forecast(self):
        self.fetch()
        self.fetch_json.return_value = {"forecasts": [{"precipitation": 1}]}
        with self.assertRaisesRegex(raincheck.RaincheckError, "unexpected answer"):
            self.fetch(5)
        self.fetch_json.side_effect = self.UNREACHABLE
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual([slot.mm for slot in self.fetch(6)], [0, 0, 1, 0])

    def test_a_cache_without_a_time_zone_counts_as_none(self):
        path = raincheck.cache_file(GENT)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"fetched": "2026-10-13T10:31:00", "data": radar_answer(0)}))
        self.fetch()
        self.assertEqual(self.fetch_json.call_count, 1)

    def test_forecasts_past_three_hours_old_are_removed(self):
        old = raincheck.cache_file(raincheck.Place("Brussel", 50.85, 4.35))
        old.parent.mkdir(parents=True)
        old.write_text("{}")
        hours_ago = (self.NOW - timedelta(hours=4)).timestamp()
        os.utime(old, (hours_ago, hours_ago))
        self.fetch()
        self.assertFalse(old.exists())
        self.assertTrue(raincheck.cache_file(GENT).exists())

    def test_no_radar_is_not_cached(self):
        self.fetch_json.return_value = None
        with self.assertRaisesRegex(raincheck.RaincheckError, "no radar"):
            self.fetch()
        self.assertFalse(raincheck.cache_file(GENT).exists())


class LookupTest(unittest.TestCase):
    def test_coordinates(self):
        self.assertEqual(raincheck.lookup("51.05,3.72"), raincheck.Place("51.05, 3.72", 51.05, 3.72))
        self.assertEqual(raincheck.lookup("-3.7, 40.4").lat, -3.7)

    def test_prefers_belgium_and_the_netherlands(self):
        results = {"results": [
            {"name": "Bergen", "country_code": "NO", "latitude": 60.39, "longitude": 5.32},
            {"name": "Bergen", "country_code": "BE", "latitude": 50.45, "longitude": 3.95},
        ]}
        with mock.patch.object(raincheck, "fetch_json", return_value=results):
            self.assertEqual(raincheck.lookup("bergen"), raincheck.Place("Bergen", 50.45, 3.95))

    def test_retries_with_hyphens(self):
        found = {"results": [{"name": "Sint-Niklaas", "country_code": "BE", "latitude": 51.17, "longitude": 4.14}]}
        with mock.patch.object(raincheck, "fetch_json", side_effect=[{}, found]) as fetch:
            self.assertEqual(raincheck.lookup("sint niklaas").name, "Sint-Niklaas")
        self.assertIn("name=sint-niklaas", fetch.call_args[0][0])

    def test_unknown_place(self):
        with mock.patch.object(raincheck, "fetch_json", return_value={}):
            with self.assertRaisesRegex(raincheck.RaincheckError, "no place found called 'xyzzy'"):
                raincheck.lookup("xyzzy")


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = raincheck.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class PlacesTest(unittest.TestCase):
    def setUp(self):
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        self.places_file = os.path.join(config.name, "raincheck", "places.json")
        self.lookup = mock.patch.object(
            raincheck, "lookup", side_effect=lambda text, choose=False: raincheck.Place(text.title(), 51.05, 3.72)
        ).start()
        mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config.name}).start()
        mock.patch.object(raincheck, "fetch_forecast", return_value=slots(0, 0)).start()
        self.addCleanup(mock.patch.stopall)

    def test_home_is_remembered(self):
        code, _, err = run_main("--home", "gent")
        self.assertEqual(code, 0)
        self.assertIn("Gent is saved as home, from now on plain raincheck is enough", err)
        code, out, _ = run_main()
        self.assertEqual(code, 0)
        self.assertIn("Gent · ", plain(out))

    def test_another_place_leaves_home_alone(self):
        run_main("--home", "gent")
        run_main("brugge")
        self.assertEqual(raincheck.load_places()["home"].name, "Gent")

    def test_home_needs_a_place(self):
        code, _, err = run_main("--home")
        self.assertEqual(code, 1)
        self.assertIn("raincheck --home gent", err)

    def test_a_place_saved_under_its_own_name(self):
        code, out, err = run_main("--save", "work", "brussel")
        self.assertEqual(code, 0)
        self.assertIn("Brussel is saved as work, from now on raincheck work is enough", err)
        self.assertIn("Brussel · ", plain(out))
        self.lookup.reset_mock()
        code, out, _ = run_main("work")
        self.assertEqual(code, 0)
        self.assertIn("Brussel · ", plain(out))
        self.lookup.assert_not_called()

    def test_saved_names_ignore_case_and_may_be_several_words(self):
        run_main("--save", "Grandma Lisa", "sint", "niklaas")
        code, out, _ = run_main("grandma", "lisa")
        self.assertEqual(code, 0)
        self.assertIn("Sint Niklaas · ", plain(out))

    def test_save_needs_a_name_and_a_place(self):
        code, _, err = run_main("--save", "work")
        self.assertEqual(code, 1)
        self.assertIn("raincheck --save work brussel", err)
        self.assertFalse(os.path.exists(self.places_file))

    def test_every_place_is_kept_and_saving_again_replaces(self):
        run_main("--home", "gent")
        run_main("--save", "work", "brussel")
        run_main("--save", "work", "leuven")
        places = raincheck.load_places()
        self.assertEqual(sorted(places), ["home", "work"])
        self.assertEqual(places["work"].name, "Leuven")

    def test_an_unsaved_name_is_looked_up(self):
        run_main("--save", "work", "brussel")
        code, out, _ = run_main("brugge")
        self.assertEqual(code, 0)
        self.assertIn("Brugge · ", plain(out))

    def test_places_lists_home_first(self):
        run_main("--save", "work", "brussel")
        run_main("--save", "gym", "leuven")
        run_main("--home", "gent")
        code, out, _ = run_main("--places")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "home  Gent (51.05, 3.72)",
            "gym   Leuven (51.05, 3.72)",
            "work  Brussel (51.05, 3.72)",
        ])

    def test_places_when_nothing_is_saved(self):
        code, out, _ = run_main("--places")
        self.assertEqual(code, 0)
        self.assertIn("no saved places yet", out)

    def test_forget(self):
        run_main("--home", "gent")
        run_main("--save", "work", "brussel")
        code, _, err = run_main("--forget", "Work")
        self.assertEqual(code, 0)
        self.assertIn("forgot work", err)
        self.assertEqual(sorted(raincheck.load_places()), ["home"])

    def test_forget_an_unknown_place(self):
        code, _, err = run_main("--forget", "work")
        self.assertEqual(code, 1)
        self.assertIn("no saved place called 'work'", err)
        code, _, err = run_main("--forget")
        self.assertEqual(code, 1)
        self.assertIn("raincheck --forget work", err)

    def test_first_run_outside_a_terminal(self):
        with mock.patch.object(raincheck.sys.stdin, "isatty", return_value=False):
            code, _, err = run_main()
        self.assertEqual(code, 1)
        self.assertIn("no home saved yet", err)

    def test_first_run_asks_for_home(self):
        with mock.patch.object(raincheck.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="leuven") as ask:
            code, out, _ = run_main()
        self.assertEqual(code, 0)
        ask.assert_called_once()
        self.assertIn("Leuven · ", plain(out))
        self.assertEqual(raincheck.load_places()["home"], raincheck.Place("Leuven", 51.05, 3.72))

    def test_a_failed_save_keeps_the_places_file_whole(self):
        run_main("--home", "gent")
        with open(self.places_file) as places:
            before = places.read()
        with mock.patch.object(raincheck.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                run_main("--save", "work", "brussel")
        with open(self.places_file) as places:
            self.assertEqual(places.read(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.places_file)), ["places.json"])

    def test_unreadable_places(self):
        os.makedirs(os.path.dirname(self.places_file))
        with open(self.places_file, "w") as file:
            file.write("{broken")
        code, _, err = run_main()
        self.assertEqual(code, 1)
        self.assertIn("unreadable", err)


BERGEN = {"results": [
    {"name": "Bergen", "country_code": "NO", "country": "Noorwegen", "admin1": "Vestland",
     "admin2": "Bergen", "latitude": 60.39, "longitude": 5.32},
    {"name": "Bergen", "country_code": "BE", "country": "België", "admin1": "Waals Gewest",
     "admin2": "Henegouwen", "latitude": 50.45, "longitude": 3.95},
    {"name": "Bergen", "country_code": "NL", "country": "Nederland", "admin1": "Noord-Holland",
     "admin2": "Bergen (NH.)", "latitude": 52.67, "longitude": 4.70},
]}


class CompletionTest(unittest.TestCase):
    def setUp(self):
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config.name}).start()
        self.addCleanup(mock.patch.stopall)

    def test_every_flag_in_the_help_is_completed_and_understood(self):
        documented = set(re.findall(r"--[a-z-]+", raincheck.__doc__))
        completed = {flag.long for flag in raincheck.FLAGS}
        self.assertEqual(completed - {"--help"}, documented)
        for flag in raincheck.FLAGS:
            for name in filter(None, (flag.short, flag.long)):
                raincheck.parse_args([name, "25"] if flag.value else [name])

    def test_place_names_for_completion(self):
        code, out, _ = run_main("--places", "--short")
        self.assertEqual((code, out), (0, ""))
        raincheck.write_places({"work": GENT, "home": GENT, "sint niklaas": GENT})
        code, out, _ = run_main("--places", "--short")
        self.assertEqual(out, "home\nsint niklaas\nwork\n")

    def test_an_unknown_shell(self):
        code, _, err = run_main("--completions", "tcsh")
        self.assertEqual(code, 1)
        self.assertIn("say which shell to complete for: bash, fish, zsh", err)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_bash_completes_options_shells_and_saved_places(self):
        script = raincheck.completion_script("bash")
        probe = (
            'raincheck() { printf "home\\nwork\\nsint niklaas\\n"; }\n'
            f"{script}\n"
            'for words in "raincheck w" "raincheck s" "raincheck --sh" "raincheck --completions z"; do\n'
            '  read -ra COMP_WORDS <<< "$words"; COMP_CWORD=$(( ${#COMP_WORDS[@]} - 1 ))\n'
            '  _raincheck; (IFS=,; echo "${COMPREPLY[*]}")\n'
            "done\n"
        )
        out = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.splitlines(), ["work", "sint niklaas", "--short", "zsh"])

    def test_scripts_are_valid_for_their_shell(self):
        for shell in raincheck.SHELLS:
            with self.subTest(shell=shell):
                if not shutil.which(shell):
                    self.skipTest(f"needs {shell}")
                result = subprocess.run([shell, "-n"], input=raincheck.completion_script(shell), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


BRUSSEL = raincheck.Place("Brussel", 50.85, 4.35)
MONS = raincheck.Place("Bergen, Henegouwen", 50.45, 3.95)
BERGEN_NH = raincheck.Place("Bergen, Noord-Holland", 52.67, 4.7)


class LiveCase(unittest.TestCase):
    NOW = START.replace(minute=33)

    def setUp(self):
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config.name}).start()
        self.addCleanup(mock.patch.stopall)
        self.fetch = mock.Mock(return_value=slots(0, 0, 1.5, 6, *[0] * 20))
        self.live = raincheck.Live([raincheck.Tab("home", GENT), raincheck.Tab("work", BRUSSEL)], fetch=self.fetch)
        self.live.update(self.NOW)

    def screen(self, width=100, height=20):
        return [plain(line) for line in self.live.screen(self.NOW, width, height, False)]

    def press(self, *keys):
        return [self.live.key(key) for key in keys]


class LiveTest(LiveCase):
    def test_keys_from_the_terminal(self):
        keys = raincheck.parse_keys("\033[C\033[Dq\t\033[Z\r\x7f\033")
        self.assertEqual(keys, ["right", "left", "q", "tab", "shift-tab", "enter", "backspace", "esc"])

    def test_an_arrow_cut_in_two_by_a_read_is_completed(self):
        with mock.patch.object(raincheck.os, "read", side_effect=[b"\033[C\033", b"[C"]), \
                mock.patch.object(raincheck.select, "select", return_value=([0], [], [])):
            self.assertEqual(raincheck.read_keys(0), ["right", "right"])

    def test_a_character_cut_in_two_by_a_read_is_completed(self):
        with mock.patch.object(raincheck.os, "read", side_effect=[b"Li\xc3", b"\xa8ge"]), \
                mock.patch.object(raincheck.select, "select", return_value=([0], [], [])):
            self.assertEqual(raincheck.read_keys(0), ["L", "i", "è", "g", "e"])

    def test_the_mouse_wheel_moves_through_the_hours_and_clicks_do_nothing(self):
        keys = raincheck.parse_keys("\033[<65;10;5M\033[<65;10;5M\033[<64;10;5M\033[<0;3;4M\033[<0;3;4m")
        self.assertEqual(keys, ["wheel-down", "wheel-down", "wheel-up", "press 3 4", "release"])
        self.assertEqual(self.press(*keys), [True] * 5)
        self.assertEqual(self.live.cursor, 1)
        self.assertFalse(self.live.playing)

    def test_a_release_in_the_older_mouse_format(self):
        with mock.patch.object(raincheck.os, "read", return_value=b"\033[M#!!"), \
                mock.patch.object(raincheck.select, "select", return_value=([], [], [])):
            self.assertEqual(raincheck.read_keys(0), ["release"])

    def test_the_older_mouse_format_never_reads_as_keys(self):
        events = b"\033[Ma!!\033[M`\xf0!\033[M qqq"
        with mock.patch.object(raincheck.os, "read", return_value=events), \
                mock.patch.object(raincheck.select, "select", return_value=([], [], [])):
            self.assertEqual(raincheck.read_keys(0), ["wheel-down", "wheel-up", "press 81 81", "q"])

    def test_a_wheel_event_cut_in_two_by_a_read_is_completed(self):
        with mock.patch.object(raincheck.os, "read", side_effect=[b"\033[<65;1", b"0;5M"]), \
                mock.patch.object(raincheck.select, "select", return_value=([0], [], [])):
            self.assertEqual(raincheck.read_keys(0), ["wheel-down"])

    def test_alt_with_a_key_is_one_key_that_does_nothing(self):
        self.assertEqual(raincheck.parse_keys("\033b\033\033"), ["\033b", "esc", "esc"])
        self.assertEqual(self.press("\033b"), [True])

    def test_a_lone_escape_is_the_escape_key(self):
        with mock.patch.object(raincheck.os, "read", return_value=b"\033"), \
                mock.patch.object(raincheck.select, "select", return_value=([], [], [])):
            self.assertEqual(raincheck.read_keys(0), ["esc"])

    def test_the_screen_has_the_tabs_the_chart_and_the_keys(self):
        lines = self.screen()
        self.assertEqual(len(lines), 20)
        self.assertEqual(lines[0].rstrip(), " [home] work")
        self.assertIn("Gent · 12:33", "\n".join(lines))
        self.assertEqual(lines[-1], " " + raincheck.LIVE_KEYS)

    def test_the_cursor_tells_the_rain_of_its_slot(self):
        self.assertIn("▲ 12:30  dry", "\n".join(self.screen()))
        self.press("right", "right", "right")
        self.assertIn("▲ 12:45  heavy rain, 6.0 mm per hour", "\n".join(self.screen()))

    def test_the_cursor_stays_on_the_chart(self):
        self.press("left")
        self.assertEqual(self.live.cursor, 0)
        self.press(*["right"] * 50)
        self.assertEqual(self.live.cursor, 23)
        self.assertIn("14:25  dry ▲", "\n".join(self.screen(width=56)))

    def test_up_and_down_go_through_the_places(self):
        self.press("down")
        self.live.update(self.NOW)
        self.fetch.assert_called_with(BRUSSEL, self.NOW, fresh=False, warn=mock.ANY)
        self.assertEqual(self.screen()[0].rstrip(), "  home [work]")
        self.press("down")
        self.assertEqual(self.live.tab.place, GENT)
        self.press("up")
        self.assertEqual(self.live.tab.place, BRUSSEL)

    def test_the_forecast_is_loaded_once_a_minute(self):
        self.live.update(self.NOW + timedelta(seconds=20))
        self.assertEqual(self.fetch.call_count, 1)
        self.live.update(self.NOW + timedelta(minutes=1))
        self.assertEqual(self.fetch.call_count, 2)

    def test_r_fetches_again_past_the_cache(self):
        self.press("r")
        self.assertIn("fetching the forecast", "\n".join(self.screen()))
        self.assertTrue(self.live.update(self.NOW))
        self.fetch.assert_called_with(GENT, self.NOW, fresh=True, warn=mock.ANY)
        self.assertFalse(self.live.update(self.NOW))

    def test_q_and_escape_quit(self):
        self.assertEqual(self.press("q"), [False])
        self.assertEqual(self.press("esc"), [False])

    def test_a_place_without_radar_says_why(self):
        self.fetch.side_effect = raincheck.RaincheckError("Buienradar has no radar for Brussel")
        self.press("down")
        self.live.update(self.NOW)
        self.assertIn("Buienradar has no radar for Brussel", "\n".join(self.screen()))

    def test_an_offline_note_shows_above_the_keys_of_its_own_place(self):
        def offline(place, now, fresh=False, warn=None):
            warn("graphdata.buienradar.nl is unreachable, showing the forecast from 12:30")
            return slots(0, 0)

        self.live.fetch = offline
        self.press("r")
        self.live.update(self.NOW)
        self.assertEqual(self.screen()[-2], " graphdata.buienradar.nl is unreachable, showing the forecast from 12:30")
        self.live.fetch = self.fetch
        self.press("down")
        self.live.update(self.NOW)
        self.assertEqual(self.screen()[-1], " " + raincheck.LIVE_KEYS)
        self.assertNotIn("unreachable", "\n".join(self.screen()))

    def test_cutting_plain_text_adds_no_colour_codes(self):
        self.assertEqual(raincheck.fit("x" * 20, 5), "xxxxx")
        cut = raincheck.fit("\033[1mbold text\033[0m", 4)
        self.assertEqual(plain(cut), "bold")
        self.assertTrue(cut.endswith("\033[0m"))

    def test_every_line_fits_the_terminal(self):
        self.live.note = "x" * 200
        for line in self.live.screen(self.NOW, 50, 12, True):
            self.assertLessEqual(raincheck.visible_len(line), 50)
        self.assertEqual(len(self.live.screen(self.NOW, 50, 5, True)), 5)

    def test_a_trip_is_planned_for_the_place_on_screen(self):
        live = raincheck.Live([raincheck.Tab("home", GENT)], trip_minutes=25, fetch=self.fetch)
        live.update(self.NOW)
        text = "\n".join(plain(line) for line in live.screen(self.NOW, 100, 20, False))
        self.assertIn("leave from 12:50 to stay dry for 25 min", text)

    def test_a_trip_too_long_for_the_forecast_keeps_the_chart(self):
        live = raincheck.Live([raincheck.Tab("home", GENT)], trip_minutes=500, fetch=self.fetch)
        live.update(self.NOW)
        lines = [plain(line) for line in live.screen(self.NOW, 100, 20, False)]
        self.assertIn("Gent · 12:33", "\n".join(lines))
        self.assertIn("so a trip can take at most", lines[-2])

    def test_live_needs_termios(self):
        with mock.patch.object(raincheck, "termios", None):
            code, _, err = run_main("--live")
        self.assertEqual(code, 1)
        self.assertIn("--live needs a Unix terminal", err)

    def test_live_needs_a_terminal(self):
        with mock.patch.object(raincheck.sys.stdin, "isatty", return_value=False):
            code, _, err = run_main("--live")
        self.assertEqual(code, 1)
        self.assertIn("--live needs a terminal", err)

    def test_live_opens_the_place_asked_for_after_the_saved_ones(self):
        raincheck.write_places({"home": GENT})
        with mock.patch.object(raincheck, "run_live") as run_live, \
                mock.patch.object(raincheck, "lookup", return_value=BRUSSEL), \
                mock.patch.object(raincheck.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(raincheck.sys.stdout, "isatty", return_value=True):
            raincheck.live_mode(raincheck.parse_args(["--live", "brussel"]))
        live = run_live.call_args.args[0]
        self.assertEqual([tab.label for tab in live.tabs], ["home", "Brussel"])
        self.assertEqual(live.index, 1)


class LiveMapTest(LiveCase):
    TALL = os.terminal_size((100, 40))

    def setUp(self):
        super().setUp()
        self.frames = [raincheck.Frame(START + timedelta(minutes=minutes), radar_rows(8, 6)) for minutes in (0, 10, 20)]
        self.fetch_radar = mock.Mock(return_value=self.frames)
        self.live = raincheck.Live([raincheck.Tab("home", GENT), raincheck.Tab("work", BRUSSEL)], fetch=self.fetch,
                               radar=raincheck.Radar(fetch=self.fetch_radar))
        self.load()

    def text(self, height=40):
        return "\n".join(plain(line) for line in self.live.screen(self.NOW, 100, height, False))

    def test_a_tall_terminal_shows_the_map_above_the_chart(self):
        text = self.text()
        self.assertLess(text.index("Radar · 12:30"), text.index("Gent · 12:33"))
        width, height = self.fetch_radar.call_args.args
        self.assertEqual((width, height), raincheck.map_size(96, 40 - raincheck.MAP_RESERVED))

    def test_a_short_terminal_shows_the_chart_alone(self):
        self.fetch_radar.reset_mock()
        live = raincheck.Live([raincheck.Tab("home", GENT)], fetch=self.fetch, radar=raincheck.Radar(fetch=self.fetch_radar))
        live.update(self.NOW, os.terminal_size((100, 24)))
        self.fetch_radar.assert_not_called()
        self.assertNotIn("Radar", "\n".join(live.screen(self.NOW, 100, 24, False)))

    def test_playing_moves_the_map_and_the_cursor_along(self):
        self.live.tick()
        self.live.tick()
        self.assertIn("Radar · 12:50", self.text())
        self.assertEqual(self.live.cursor, 4)
        for _ in range(raincheck.MAP_HOLD):
            self.live.tick()
        self.assertIn("Radar · 12:50", self.text())
        self.live.tick()
        self.assertIn("Radar · 12:30", self.text())

    def test_arrows_pause_and_the_map_follows_the_cursor(self):
        self.press("right", "right")
        self.assertFalse(self.live.playing)
        self.assertIn("Radar · 12:40", self.text())
        self.live.tick()
        self.assertIn("Radar · 12:40", self.text())

    def test_space_plays_on_from_the_frame_on_screen(self):
        self.press("right", "right", " ")
        self.assertTrue(self.live.playing)
        self.assertIn("Radar · 12:40", self.text())
        self.live.tick()
        self.assertIn("Radar · 12:50", self.text())

    def test_the_radar_is_loaded_again_after_five_minutes(self):
        self.load(later=4 * 60)
        self.assertEqual(self.fetch_radar.call_count, 1)
        self.load(later=5 * 60)
        self.assertEqual(self.fetch_radar.call_count, 2)

    def load(self, later=0, size=None):
        """Updates until nothing is left to load, as the live loop does."""
        while self.live.update(self.NOW + timedelta(seconds=later), size or self.TALL):
            pass

    def test_r_fetches_the_radar_again_and_keeps_the_map_meanwhile(self):
        self.press("r")
        self.assertIn("Radar · 12:30", self.text())
        self.load()
        self.assertEqual(self.fetch_radar.call_count, 2)

    def test_the_forecast_is_drawn_before_the_radar_loads(self):
        live = raincheck.Live([raincheck.Tab("home", GENT)], fetch=self.fetch, radar=raincheck.Radar(fetch=self.fetch_radar))
        self.assertTrue(live.update(self.NOW, self.TALL))
        self.assertIsNone(live.radar.frames)
        text = "\n".join(plain(line) for line in live.screen(self.NOW, 100, 40, False))
        self.assertIn("fetching the radar", text)
        self.assertIn("Gent · 12:33", text)

    def test_a_radar_that_fails_says_so_above_the_chart(self):
        live = raincheck.Live([raincheck.Tab("home", GENT)], fetch=self.fetch,
                          radar=raincheck.Radar(fetch=mock.Mock(side_effect=raincheck.RaincheckError("Buienradar has no radar map right now"))))
        while live.update(self.NOW, self.TALL):
            pass
        text = "\n".join(plain(line) for line in live.screen(self.NOW, 100, 40, False))
        self.assertIn("Radar Buienradar has no radar map right now", text)
        self.assertIn("Gent · 12:33", text)

    def test_a_reload_that_fails_keeps_the_map_and_tries_again_in_a_minute(self):
        self.fetch_radar.side_effect = raincheck.RaincheckError("graphdata is unreachable")
        self.load(later=5 * 60)
        self.assertIn("Radar · 12:30", self.text())
        self.assertIn("no new radar (graphdata is unreachable), showing the one from 12:30", self.text())
        self.load(later=5 * 60 + 30)
        self.assertEqual(self.fetch_radar.call_count, 2)
        self.fetch_radar.side_effect = None
        self.load(later=6 * 60)
        self.assertEqual(self.fetch_radar.call_count, 3)
        self.assertNotIn("no new radar", self.text())

    def test_a_new_size_loads_once_the_terminal_holds_still(self):
        wide = os.terminal_size((120, 50))
        self.load(later=0, size=wide)
        self.assertEqual(self.fetch_radar.call_count, 1)
        self.load(later=0.5, size=os.terminal_size((130, 50)))
        self.assertEqual(self.fetch_radar.call_count, 1)
        self.load(later=2, size=os.terminal_size((130, 50)))
        self.assertEqual(self.fetch_radar.call_count, 2)

    def test_the_chart_is_never_cut_for_the_map(self):
        self.live.trip_minutes = 25
        self.live.loaded.clear()
        self.fetch.return_value = slots(0, 0, 1.5, 6, *[0] * 20)
        self.load()
        lines = [plain(line) for line in self.live.screen(self.NOW, 100, 40, False)]
        self.assertEqual(len(lines), 40)
        self.assertIn("heaviest at 12:45: 6.0 mm per hour", "\n".join(lines))
        self.assertIn("leave from 12:50 to stay dry for 25 min", "\n".join(lines))


BUGGENHOUT = raincheck.Place("Buggenhout", 51.0159, 4.20173)


class LiveZoomTest(LiveCase):
    TALL = os.terminal_size((100, 40))

    def setUp(self):
        super().setUp()
        self.fetch_radar = mock.Mock(side_effect=self.radar_with_rain_at_home)
        self.live = raincheck.Live([raincheck.Tab("home", BUGGENHOUT), raincheck.Tab("work", BRUSSEL)], fetch=self.fetch,
                               radar=raincheck.Radar(fetch=self.fetch_radar))
        self.load()

    @staticmethod
    def radar_with_rain_at_home(width, height):
        """The whole map at width x height, dry but for a block of heavy rain centred on Buggenhout."""
        x, y = raincheck.map_position(BUGGENHOUT, width, height)
        rain = {(x + dx, y + dy): DEEP_BLUE for dx in range(-3, 4) for dy in range(-3, 4)}
        return [raincheck.Frame(START, radar_rows(width, height, rain))]

    def load(self, later=0):
        while self.live.update(self.NOW + timedelta(seconds=later), self.TALL):
            pass

    def view(self):
        return self.live.view(self.live.radar.window, self.live.radar.zoom)

    def lines(self):
        return [plain(line) for line in self.live.screen(self.NOW, 100, 40, False)]

    def home_line(self):
        return next(line for line in self.lines() if "● home" in line)

    def test_zooming_in_loads_the_map_twice_as_big_around_the_place(self):
        width, height = self.fetch_radar.call_args.args
        self.press("+")
        self.load()
        self.assertEqual(self.fetch_radar.call_args.args, (width * 2, height * 2))
        self.assertIn("· 2x", "\n".join(self.lines()))
        view = self.view()
        x, y = raincheck.view_position(BUGGENHOUT, view)
        self.assertLessEqual(abs(x - view.width // 2), 1)
        self.assertLessEqual(abs(y - view.height // 2), 1)

    def test_rain_and_map_stay_under_the_place_at_every_zoom(self):
        for zoom in (1, 2, 4):
            with self.subTest(zoom=zoom):
                line = self.home_line()
                dot = line.index("● home")
                self.assertEqual(line[dot - 1], "▓")
                x, y = raincheck.view_position(BUGGENHOUT, self.view())
                window = raincheck.map_mask(self.view().width * zoom, self.view().height * zoom,
                                        (self.view().left, self.view().top, self.view().width, self.view().height))
                self.assertEqual(window[y][x], raincheck.LAND_LIGHT)
                self.press("+")
                self.load()

    def test_zoom_stops_at_four_times_and_goes_back_to_the_whole_map(self):
        self.press("+", "+", "+", "+")
        self.assertEqual(self.live.zoom, 4)
        self.press("l")
        self.press("-", "-", "-")
        self.assertEqual((self.live.zoom, self.live.center), (1, None))

    def test_moving_the_zoomed_map_and_back_to_the_place(self):
        self.press("+")
        self.load()
        before = self.view()
        self.press("l", "k")
        moved = self.view()
        self.assertEqual(moved.left - before.left, before.width // 4)
        self.assertEqual(before.top - moved.top, before.height // 4)
        self.press("c")
        self.assertEqual(self.view(), before)

    def test_moving_stops_at_the_edge_of_the_map(self):
        self.press("+")
        self.load()
        self.press(*["h"] * 20, *["k"] * 20)
        self.assertEqual(self.view()[1:3], (0, 0))
        self.press("l")
        self.assertGreater(self.view().left, 0)

    def test_dragging_moves_the_map_along_under_the_pointer(self):
        self.press("+")
        self.load()
        self.lines()
        before = self.view()
        self.press("press 30 10", "drag 26 9", "release", "drag 10 10")
        moved = self.view()
        self.assertEqual((moved.left - before.left, moved.top - before.top), (4, 2))

    def test_a_drag_that_starts_off_the_map_does_not_move_it(self):
        self.press("+")
        self.load()
        self.lines()
        before = self.view()
        self.press("press 30 35", "drag 20 35", "press 1 1", "drag 5 5")
        self.assertEqual(self.view(), before)

    def test_a_release_ends_a_drag_even_in_a_prompt(self):
        self.press("+")
        self.load()
        self.lines()
        self.press("press 30 10", "/", "release", "esc")
        self.assertIsNone(self.live.dragging)

    def test_a_place_opened_by_search_comes_into_view(self):
        self.live.search = mock.Mock(return_value=[raincheck.Place("Oostende", 51.23, 2.92)])
        self.press("+")
        self.load()
        self.press("l", "l", "l", "/", *"oostende", "enter")
        self.load()
        self.assertIsNone(self.live.center)
        self.assertIsNotNone(raincheck.view_position(self.live.tab.place, self.view()))

    def test_another_zoom_loads_at_once_after_a_failed_refresh(self):
        self.fetch_radar.side_effect = raincheck.RaincheckError("graphdata is unreachable")
        self.live.radar.reload()
        self.load()
        self.fetch_radar.side_effect = self.radar_with_rain_at_home
        self.press("+")
        self.load()
        self.assertEqual(self.live.radar.zoom, 2)

    def test_a_failed_load_at_a_new_size_drops_the_old_frames(self):
        self.fetch_radar.side_effect = raincheck.RaincheckError("graphdata is unreachable")
        self.live.update(self.NOW, os.terminal_size((110, 44)))
        self.live.update(self.NOW + timedelta(seconds=2), os.terminal_size((110, 44)))
        self.assertIsInstance(self.live.radar.frames, raincheck.RaincheckError)

    def test_zoomed_in_a_dry_window_says_no_rain_in_view(self):
        self.fetch_radar.side_effect = lambda width, height: [raincheck.Frame(START, radar_rows(width, height))]
        self.press("+")
        self.load()
        self.assertIn("no rain in view", "\n".join(self.lines()))

    def test_four_times_the_widest_map_is_as_far_as_raincheckradar_goes(self):
        self.assertLessEqual(raincheck.MAP_MAX_WIDTH * raincheck.MAX_ZOOM, raincheck.RADAR_SIZE[0])

    def test_the_whole_map_does_not_move(self):
        before = self.view()
        self.press("l", "press 30 10", "drag 20 10")
        self.assertEqual(self.view(), before)

    def test_another_place_brings_the_map_to_it(self):
        self.press("+", "l", "l")
        self.load()
        self.press("down")
        self.load()
        self.assertIsNone(self.live.center)
        x, y = raincheck.view_position(BRUSSEL, self.view())
        self.assertLessEqual(abs(x - self.view().width // 2), 1)

    def test_a_view_shows_only_its_window(self):
        frame = raincheck.Frame(START, radar_rows(20, 10, {(12, 6): RED}))
        window = raincheck.crop(frame, raincheck.View(2, 10, 5, 10, 5))
        self.assertEqual((len(window.rows[0]) // 4, len(window.rows)), (10, 5))
        self.assertEqual(bytes(window.rows[1][8:12]), bytes(RED))
        self.assertIsNone(raincheck.view_position(BUGGENHOUT, raincheck.View(2, 0, 0, 5, 5)))


class LiveSearchTest(LiveCase):
    def test_search_opens_a_single_match_right_away(self):
        self.live.search = mock.Mock(return_value=[MONS])
        self.press("/", *"mons", "enter")
        self.live.search.assert_called_with("mons")
        self.assertEqual(self.live.tab.place, MONS)
        self.assertEqual(self.screen()[0].rstrip(), "  home  work [Bergen]")

    def test_several_matches_ask_which_one(self):
        self.live.search = mock.Mock(return_value=[MONS, BERGEN_NH])
        self.press("/", *"bergen", "enter")
        lines = self.screen()
        self.assertIn("Which one?", "\n".join(lines))
        self.assertIn("  > Bergen, Henegouwen", lines)
        self.assertEqual(lines[-1], " " + raincheck.MENU_KEYS)
        self.press("down", "enter")
        self.assertEqual(self.live.tab.place, BERGEN_NH)

    def test_typing_in_the_prompt(self):
        self.assertEqual(self.press("/", "g", "q", "backspace", "e"), [True] * 5)
        self.assertEqual(self.screen()[-1], " search: ge█")

    def test_escape_leaves_the_prompt_and_the_menu_without_quitting(self):
        self.live.search = mock.Mock(return_value=[MONS, BERGEN_NH])
        self.assertEqual(self.press("/", "esc"), [True, True])
        self.assertIsNone(self.live.prompt)
        self.press("/", *"bergen", "enter", "esc")
        self.assertEqual(self.live.menu, [])
        self.assertEqual(self.live.tab.place, GENT)

    def test_a_search_that_finds_nothing_says_so(self):
        self.live.search = mock.Mock(side_effect=raincheck.RaincheckError("no place found called 'xyzzy'"))
        self.press("/", *"xyzzy", "enter")
        self.assertEqual(self.screen()[-2], " no place found called 'xyzzy'")

    def test_save_a_place_found_by_search(self):
        self.live.search = mock.Mock(return_value=[MONS])
        self.press("/", *"bergen", "enter", "s", *"Mons", "enter")
        self.assertEqual(raincheck.load_places()["mons"], MONS)
        self.assertEqual(self.live.tab.label, "mons")
        self.assertIn("is saved as mons", self.screen()[-2])

    def test_a_saved_place_is_not_saved_again(self):
        self.press("s")
        self.assertIsNone(self.live.prompt)
        self.assertEqual(self.screen()[-2], " Gent is saved as home")
        self.press("right")
        self.assertEqual(self.screen()[-1], " " + raincheck.LIVE_KEYS)
        self.assertNotIn("saved as", "\n".join(self.screen()))

    def test_a_save_that_fails_says_why(self):
        self.live.search = mock.Mock(return_value=[MONS])
        with mock.patch.object(raincheck, "write_places", side_effect=PermissionError("read-only file system")):
            self.press("/", *"bergen", "enter", "s", *"mons", "enter")
        self.assertEqual(self.screen()[-2], " read-only file system")


def encode_png(rows, kind=0, channels=4):
    """A PNG of rows of pixel bytes, each row put through filter kind, for the decoder to undo."""
    raw, prior = bytearray(), bytes(len(rows[0]))
    for row in rows:
        raw.append(kind)
        raw += png_filter(kind, bytes(row), prior, channels)
        prior = bytes(row)
    header = struct.pack(">IIBBBBB", len(rows[0]) // channels, len(rows), 8, 6 if channels == 4 else 2, 0, 0, 0)
    return raincheck.PNG_SIGNATURE + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(bytes(raw))) + png_chunk(b"IEND", b"")


def png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png_filter(kind, row, prior, bpp):
    filtered = bytearray()
    for i, value in enumerate(row):
        left, corner = (row[i - bpp], prior[i - bpp]) if i >= bpp else (0, 0)
        up = prior[i]
        estimate = left + up - corner
        nearest = min((abs(estimate - left), 0, left), (abs(estimate - up), 1, up), (abs(estimate - corner), 2, corner))[2]
        filtered.append((value - [0, left, up, (left + up) // 2, nearest][kind]) & 255)
    return filtered


DEEP_BLUE, PALE_BLUE, RED, DRY = (5, 27, 208, 204), (236, 237, 253, 203), (252, 22, 1, 204), (0, 0, 0, 0)


def radar_rows(width, height, rain=None):
    """Rows of RGBA bytes, dry but for rain, a dict of (x, y) to a pixel."""
    rain = rain or {}
    return [bytearray(b"".join(bytes(rain.get((x, y), DRY)) for x in range(width))) for y in range(height)]


class PngTest(unittest.TestCase):
    ROWS = [bytes(range(i, i + 12)) for i in (3, 90, 200)]

    def test_every_row_filter_is_undone(self):
        for kind in range(5):
            with self.subTest(kind=kind):
                picture = raincheck.decode_png(encode_png(self.ROWS, kind))
                self.assertEqual((picture.width, picture.height, picture.channels), (3, 3, 4))
                self.assertEqual([bytes(row) for row in picture.rows], self.ROWS)

    def test_rgb_without_alpha(self):
        picture = raincheck.decode_png(encode_png(self.ROWS, 4, channels=3))
        self.assertEqual((picture.width, picture.channels), (4, 3))
        self.assertEqual([bytes(row) for row in picture.rows], self.ROWS)

    def test_not_a_png(self):
        with self.assertRaisesRegex(raincheck.RaincheckError, "not a PNG"):
            raincheck.decode_png(b"GIF89a")

    def test_a_short_header(self):
        with self.assertRaisesRegex(raincheck.RaincheckError, "cannot read"):
            raincheck.decode_png(raincheck.PNG_SIGNATURE + png_chunk(b"IHDR", b"\x00\x01"))

    def test_a_kind_raincheck_cannot_read(self):
        palette = struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0)
        with self.assertRaisesRegex(raincheck.RaincheckError, "cannot read"):
            raincheck.decode_png(raincheck.PNG_SIGNATURE + png_chunk(b"IHDR", palette))

    def test_a_broken_image(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        with self.assertRaisesRegex(raincheck.RaincheckError, "broken radar image"):
            raincheck.decode_png(raincheck.PNG_SIGNATURE + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", b"not zlib"))


class MapTest(unittest.TestCase):
    def test_the_mask_has_its_size(self):
        mask = raincheck.map_mask(*raincheck.MAP_MASK_SIZE)
        self.assertEqual((len(mask[0]), len(mask)), raincheck.MAP_MASK_SIZE)

    def test_places_land_where_they_lie(self):
        width, height = raincheck.MAP_MASK_SIZE
        mask = raincheck.map_mask(width, height)
        places = {
            "Buggenhout": (raincheck.Place("Buggenhout", 51.016, 4.202), raincheck.LAND_LIGHT),
            "Utrecht": (raincheck.Place("Utrecht", 52.09, 5.12), raincheck.LAND_DARK),
            "Sauerland": (raincheck.Place("Sauerland", 51.2, 8.0), raincheck.LAND_LIGHT),
            "Arras": (raincheck.Place("Arras", 50.29, 2.78), raincheck.LAND_DARK),
            "North Sea": (raincheck.Place("North Sea", 53.0, 3.0), raincheck.SEA),
        }
        for name, (place, land) in places.items():
            with self.subTest(name):
                x, y = raincheck.map_position(place, width, height)
                self.assertEqual(mask[y][x], land)

    def test_off_the_map(self):
        self.assertIsNone(raincheck.map_position(raincheck.Place("Madrid", 40.4, -3.7), 100, 86))
        self.assertIsNone(raincheck.map_position(raincheck.Place("London", 51.5, -0.12), 100, 86))

    def test_north_is_up_and_stretched_as_on_a_web_map(self):
        ys = [raincheck.map_position(raincheck.Place("", lat, 5.0), 264, 229)[1] for lat in (50, 52, 54)]
        self.assertGreater(ys[0], ys[1])
        self.assertGreater(ys[1] - ys[2], 0)
        self.assertGreater(ys[0] - ys[1], 0)
        steps = [raincheck.mercator(lat + 1) - raincheck.mercator(lat) for lat in (50, 53)]
        self.assertGreater(steps[1], steps[0])

    def test_the_map_fits_the_terminal_in_its_own_shape(self):
        self.assertEqual(raincheck.map_size(500, 500), (264, 228))
        width, height = raincheck.map_size(200, 20)
        self.assertEqual(height, 40)
        self.assertAlmostEqual(width / height, 1058 / 915, delta=0.05)
        self.assertEqual(raincheck.map_size(60, 100)[1] % 2, 0)

    def test_shrinking_keeps_a_thin_border(self):
        grid = [[raincheck.SEA, raincheck.BORDER, raincheck.LAND_DARK, raincheck.LAND_DARK]] * 4
        self.assertEqual(raincheck.shrink_mask(grid, 2, 2), [[raincheck.BORDER, raincheck.LAND_DARK]] * 2)


class RadarTest(unittest.TestCase):
    NOW = datetime(2026, 10, 13, 10, 32, tzinfo=timezone.utc).astimezone()

    def test_kinds_of_rain_by_colour(self):
        self.assertIsNone(raincheck.radar_kind(*DRY))
        self.assertEqual(raincheck.radar_kind(*PALE_BLUE).name, "drizzle")
        self.assertEqual(raincheck.radar_kind(120, 128, 255, 204).name, "light rain")
        self.assertEqual(raincheck.radar_kind(*DEEP_BLUE).name, "heavy rain")
        self.assertEqual(raincheck.radar_kind(*RED).name, "very heavy rain")

    def test_a_sprite_is_cut_into_frames(self):
        meta = {"times": [{"timestamp": "2026-10-13T10:30:00"}, {"timestamp": "2026-10-13T10:40:00"}],
                "sprite": "https://image.buienradar.nl/2.0/image/sprite/X?width=1058&height=915&renderBackground=True&history=1"}
        sprite = encode_png(radar_rows(4, 2, {(2, 1): DEEP_BLUE}))
        with mock.patch.object(raincheck, "fetch_json", return_value=meta), \
                mock.patch.object(raincheck, "fetch_bytes", return_value=sprite) as fetch_bytes:
            frames = raincheck.fetch_radar(2, 2)
        url = fetch_bytes.call_args.args[0]
        self.assertIn("width=2&height=2&renderBackground=False", url)
        self.assertIn("history=1", url)
        self.assertEqual([frame.time for frame in frames], [self.NOW.replace(minute=30), self.NOW.replace(minute=40)])
        self.assertEqual(raincheck.kinds_on(frames[0]), set())
        self.assertEqual(bytes(frames[1].rows[1][:4]), bytes(DEEP_BLUE))

    def test_no_radar_map(self):
        with mock.patch.object(raincheck, "fetch_json", return_value=None):
            with self.assertRaisesRegex(raincheck.RaincheckError, "no radar map right now"):
                raincheck.fetch_radar(2, 2)

    def test_a_sprite_of_another_size(self):
        meta = {"times": [{"timestamp": "2026-10-13T10:30:00"}], "sprite": "https://image.buienradar.nl/x?width=1"}
        with mock.patch.object(raincheck, "fetch_json", return_value=meta), \
                mock.patch.object(raincheck, "fetch_bytes", return_value=encode_png(radar_rows(2, 3))):
            with self.assertRaisesRegex(raincheck.RaincheckError, "unexpected size"):
                raincheck.fetch_radar(2, 2)

    def test_half_blocks_carry_two_pixels_with_rain_over_the_map(self):
        mask = [[raincheck.SEA, raincheck.LAND_DARK], [raincheck.BORDER, raincheck.LAND_LIGHT]]
        frame = raincheck.Frame(self.NOW, radar_rows(2, 2, {(1, 0): DEEP_BLUE}))
        line, = raincheck.map_lines(frame, mask, [], raincheck.Paint(True))
        colors = raincheck.MAP_COLORS
        self.assertIn(f"\033[38;5;{colors[raincheck.SEA]};48;5;{colors[raincheck.BORDER]}m▀", line)
        self.assertIn(f"\033[38;5;{raincheck.KINDS[3].color};48;5;{colors[raincheck.LAND_LIGHT]}m▀", line)

    def test_without_colour_the_heavier_pixel_shows(self):
        mask = [[raincheck.SEA, raincheck.LAND_DARK, raincheck.SEA], [raincheck.BORDER, raincheck.LAND_LIGHT, raincheck.SEA]]
        frame = raincheck.Frame(self.NOW, radar_rows(3, 2, {(1, 1): RED}))
        self.assertEqual(raincheck.map_lines(frame, mask, [], raincheck.Paint(False)), [":█"])

    def test_places_are_marked_with_their_label(self):
        mask = [[raincheck.SEA] * 20 for _ in range(10)]
        frame = raincheck.Frame(self.NOW, radar_rows(20, 10))
        marks = [raincheck.Mark("west", raincheck.Place("", 53.0, 0.0), False), raincheck.Mark("east", raincheck.Place("", 51.0, 9.95), True)]
        lines = raincheck.map_lines(frame, mask, marks, raincheck.Paint(False))
        self.assertEqual(lines[1], "● west")
        self.assertEqual(lines[3], " " * 14 + "east ●")
        colored = "".join(raincheck.map_lines(frame, mask, marks, raincheck.Paint(True)))
        self.assertIn(f"\033[38;5;{raincheck.HERE_COLOR};48;5;{raincheck.MAP_COLORS[raincheck.SEA]}meast ●", colored)

    def test_labels_of_places_close_together_do_not_run_into_each_other(self):
        mask = [[raincheck.SEA] * 20 for _ in range(10)]
        frame = raincheck.Frame(self.NOW, radar_rows(20, 10))
        marks = [raincheck.Mark("gent", raincheck.Place("", 51.0, 3.7), False), raincheck.Mark("home", raincheck.Place("", 51.0, 4.2), True)]
        line = raincheck.map_lines(frame, mask, marks, raincheck.Paint(False))[3]
        self.assertEqual(line, "  gent ●● home")
        marks[0] = raincheck.Mark("gent", raincheck.Place("", 51.0, 3.0), False)
        line = raincheck.map_lines(frame, mask, marks, raincheck.Paint(False))[3]
        self.assertEqual(line, " gent ● ● home")
        marks[0] = raincheck.Mark("gent", raincheck.Place("", 51.0, 2.0), False)
        line = raincheck.map_lines(frame, mask, marks, raincheck.Paint(False))[3]
        self.assertEqual(line, "    ●   ● home")

    def test_a_legend_too_wide_for_the_map_gets_a_line_of_its_own(self):
        rain = {(x, 1): color for x, color in enumerate([PALE_BLUE, (120, 128, 255, 204), DEEP_BLUE, RED])}
        frame = raincheck.Frame(self.NOW, radar_rows(30, 6, rain))
        lines = [plain(line) for line in raincheck.render_map(frame, self.NOW, [], raincheck.Paint(False))]
        self.assertTrue(lines[1].strip().startswith("Radar ·"))
        self.assertTrue(lines[2].strip().startswith("drizzle █"))
        self.assertEqual(len(lines), 3 + 3)

    def test_the_title_says_when_and_what_falls(self):
        frame = raincheck.Frame(self.NOW + timedelta(minutes=20), radar_rows(8, 6, {(1, 1): PALE_BLUE}))
        title = " ".join(plain(line) for line in raincheck.render_map(frame, self.NOW, [], raincheck.Paint(False))[1:3])
        self.assertIn("Radar · " + raincheck.clock(frame.time) + " · in 20 min", title)
        self.assertIn("drizzle █", title)
        dry = raincheck.Frame(self.NOW - timedelta(minutes=10), radar_rows(8, 6))
        title = " ".join(plain(line) for line in raincheck.render_map(dry, self.NOW, [], raincheck.Paint(False))[1:3])
        self.assertIn("10 min ago", title)
        self.assertIn("no rain on the radar", title)


class SearchTest(unittest.TestCase):
    def setUp(self):
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config.name}).start()
        self.fetch_json = mock.patch.object(raincheck, "fetch_json", return_value=BERGEN).start()
        self.fetch_forecast = mock.patch.object(raincheck, "fetch_forecast", return_value=slots(0, 0)).start()
        self.addCleanup(mock.patch.stopall)

    def search(self, *argv, answer=None):
        is_terminal = answer is not None
        with mock.patch.object(raincheck.sys.stdin, "isatty", return_value=is_terminal), \
                mock.patch("builtins.input", return_value=answer or ""):
            return run_main("--search", *argv)

    def test_lists_every_match_belgian_and_dutch_first(self):
        code, out, _ = self.search("bergen")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "  1  Bergen, Henegouwen     België",
            "  2  Bergen, Noord-Holland  Nederland",
            "  3  Bergen, Vestland       Noorwegen",
        ])
        self.fetch_forecast.assert_not_called()

    def test_shows_the_one_you_pick(self):
        code, out, _ = self.search("bergen", answer="2")
        self.assertEqual(code, 0)
        self.assertIn("Bergen, Noord-Holland · ", plain(out))
        self.assertEqual(self.fetch_forecast.call_args[0][0], raincheck.Place("Bergen, Noord-Holland", 52.67, 4.70))

    def test_enter_stops_without_a_forecast(self):
        code, _, _ = self.search("bergen", answer="")
        self.assertEqual(code, 0)
        self.fetch_forecast.assert_not_called()

    def test_a_number_out_of_range(self):
        code, _, err = self.search("bergen", answer="7")
        self.assertEqual(code, 1)
        self.assertIn("pick a number from 1 to 3", err)

    def test_saves_the_one_you_pick(self):
        code, _, err = self.search("--save", "beach", "bergen", answer="2")
        self.assertEqual(code, 0)
        self.assertIn("Bergen, Noord-Holland is saved as beach", err)
        self.assertEqual(raincheck.load_places()["beach"].lat, 52.67)

    def test_saves_nothing_without_a_pick(self):
        code, _, _ = self.search("--home", "bergen", answer="")
        self.assertEqual(code, 0)
        self.assertEqual(raincheck.load_places(), {})

    def test_searches_past_a_saved_name(self):
        raincheck.write_places({"bergen": raincheck.Place("Gent", 51.05, 3.72)})
        code, out, _ = self.search("bergen")
        self.assertEqual(code, 0)
        self.assertIn("Bergen, Henegouwen", out)

    def test_search_needs_a_name(self):
        code, _, err = self.search()
        self.assertEqual(code, 1)
        self.assertIn("raincheck --search bergen", err)

    def test_plain_lookup_takes_the_first_match_with_its_province(self):
        self.assertEqual(raincheck.lookup("bergen"), raincheck.Place("Bergen, Henegouwen", 50.45, 3.95))

    def test_no_province_when_it_is_the_place_itself(self):
        result = {"name": "Antwerpen", "country_code": "BE", "admin1": "Vlaams Gewest",
                  "admin2": "Antwerpen", "latitude": 51.22, "longitude": 4.40}
        self.assertEqual(raincheck.to_place(result).name, "Antwerpen")
        self.assertEqual(raincheck.to_place({**result, "admin2": None, "admin1": None}).name, "Antwerpen")


class CommandTest(unittest.TestCase):
    def test_place_of_several_words(self):
        self.assertEqual(raincheck.parse_args(["sint", "niklaas"]).words, ["sint", "niklaas"])

    def test_negative_coordinates_are_not_an_option(self):
        self.assertEqual(raincheck.parse_args(["-3.7,40.4"]).words, ["-3.7,40.4"])

    def test_unknown_option(self):
        code, _, err = run_main("--foo")
        self.assertEqual(code, 1)
        self.assertIn("raincheck: unknown option --foo", err)

    def test_help(self):
        code, out, _ = run_main("-h")
        self.assertEqual(code, 0)
        self.assertIn("raincheck gent", out)

    def test_version(self):
        code, out, _ = run_main("--version")
        self.assertEqual(code, 0)
        self.assertEqual(out, f"raincheck {raincheck.__version__}\n")

    def test_the_package_has_the_same_version(self):
        with open(os.path.join(HERE, "pyproject.toml")) as pyproject:
            version = re.search(r'^version = "(.+)"$', pyproject.read(), re.MULTILINE)[1]
        self.assertEqual(version, raincheck.__version__)

    def test_requests_name_raincheck_and_its_version(self):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b"{}")
        with mock.patch.object(raincheck.urllib.request, "urlopen", return_value=response) as urlopen:
            raincheck.fetch_json("https://graphdata.buienradar.nl/x")
        agent = urlopen.call_args.args[0].get_header("User-agent")
        self.assertEqual(agent, f"raincheck/{raincheck.__version__} (+https://github.com/PieterLuypaert/raincheck)")

    def run_with_forecast(self, *argv):
        with mock.patch.object(raincheck, "lookup", return_value=GENT), \
                mock.patch.object(raincheck, "fetch_forecast", return_value=slots(0, 1.5, 0)):
            return run_main("gent", *argv)

    def test_short_prints_only_the_summary(self):
        code, out, _ = self.run_with_forecast("--short")
        self.assertEqual(code, 0)
        self.assertEqual(out, "dry until 12:35, then 5 min of light rain\n")

    def test_json(self):
        code, out, _ = self.run_with_forecast("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["place"], "Gent")
        self.assertEqual(data["summary"], "dry until 12:35, then 5 min of light rain")
        self.assertEqual([slot["mm_per_hour"] for slot in data["forecast"]], [0, 1.5, 0])
        self.assertEqual(data["forecast"][1]["time"], START.replace(minute=35).isoformat(timespec="minutes"))

    def run_trip(self, forecast, *argv):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return START.replace(minute=33)

        with mock.patch.object(raincheck, "lookup", return_value=GENT), \
                mock.patch.object(raincheck, "fetch_forecast", return_value=forecast), \
                mock.patch.object(raincheck, "datetime", Frozen):
            return run_main("gent", "--trip", "25", *argv)

    def test_trip_exits_0_when_you_can_leave_now(self):
        code, out, _ = self.run_trip(slots(*[0] * 24), "--short")
        self.assertEqual((code, out), (0, "leave whenever you like, no rain in sight\n"))

    def test_trip_exits_2_when_you_have_to_wait(self):
        code, out, _ = self.run_trip(slots(1, 1, 1, *[0] * 21), "--short")
        self.assertEqual((code, out), (2, "leave from 12:45 to stay dry for 25 min\n"))

    def test_trip_exits_0_without_short_so_a_prompt_stays_calm(self):
        code, _, _ = self.run_trip(slots(1, 1, 1, *[0] * 21), "--json")
        self.assertEqual(code, 0)

    def test_trip_in_json(self):
        _, out, _ = self.run_trip(slots(1, 1, *[0] * 7, *[1] * 15), "--json")
        trip = json.loads(out)["trip"]
        self.assertEqual(trip["minutes"], 25)
        self.assertFalse(trip["leave_now"])
        self.assertEqual(trip["earliest"], START.replace(minute=40).isoformat(timespec="minutes"))
        self.assertEqual(trip["latest"], START.replace(minute=50).isoformat(timespec="minutes"))
        self.assertEqual(trip["summary"], "leave between 12:40 and 12:50 to stay dry for 25 min")

    def test_plain_raincheck_in_a_terminal_is_live_and_text_otherwise(self):
        def live(*argv, terminal=True):
            with mock.patch.object(raincheck.sys.stdin, "isatty", return_value=terminal), \
                    mock.patch.object(raincheck.sys.stdout, "isatty", return_value=terminal):
                return raincheck.wants_live(raincheck.parse_args(list(argv)))

        self.assertTrue(live())
        self.assertTrue(live("gent"))
        self.assertFalse(live(terminal=False))
        for flag in ("--once", "--short", "--json"):
            self.assertFalse(live(flag))
        self.assertTrue(live("--live", terminal=False))
        with mock.patch.object(raincheck, "termios", None):
            self.assertFalse(live())

    def run_once(self, radar):
        out, err = io.StringIO(), io.StringIO()
        out.isatty = lambda: True
        with mock.patch.object(raincheck, "lookup", return_value=GENT), \
                mock.patch.object(raincheck, "fetch_forecast", return_value=slots(0, 1, 0)), \
                mock.patch.object(raincheck, "fetch_radar", **radar), \
                mock.patch.object(raincheck, "load_places", return_value={}), \
                mock.patch.object(raincheck.shutil, "get_terminal_size", return_value=os.terminal_size((100, 40))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = raincheck.main(["gent", "--once"])
        return code, plain(out.getvalue()), err.getvalue()

    def test_once_in_a_terminal_draws_the_map_above_the_chart(self):
        frame = raincheck.Frame(START, radar_rows(8, 6))
        code, out, _ = self.run_once({"return_value": [frame]})
        self.assertEqual(code, 0)
        self.assertLess(out.index("Radar · 12:30"), out.index("Gent · "))

    def test_once_without_a_radar_still_draws_the_chart(self):
        code, out, err = self.run_once({"side_effect": raincheck.RaincheckError("Buienradar has no radar map right now")})
        self.assertEqual(code, 0)
        self.assertIn("raincheck: no radar map (Buienradar has no radar map right now)", err)
        self.assertIn("dry until 12:35, then 5 min of light rain", out)

    def test_color_follows_the_flags_then_the_environment_then_the_terminal(self):
        def color(*argv, env=None, terminal=False):
            with mock.patch.dict(os.environ, env or {}), \
                    mock.patch.object(raincheck.sys.stdout, "isatty", return_value=terminal):
                return raincheck.use_color(raincheck.parse_args(list(argv)))

        with mock.patch.dict(os.environ):
            os.environ.pop("NO_COLOR", None)
            os.environ.pop("FORCE_COLOR", None)
            self.assertTrue(color(terminal=True))
            self.assertFalse(color())
            self.assertFalse(color(env={"NO_COLOR": "1"}, terminal=True))
            self.assertTrue(color(env={"FORCE_COLOR": "1"}))
            self.assertTrue(color("--color", env={"NO_COLOR": "1"}))
            self.assertFalse(color("--no-color", terminal=True))

    def test_shows_the_chart(self):
        with mock.patch.object(raincheck, "lookup", return_value=GENT), \
                mock.patch.object(raincheck, "fetch_forecast", return_value=slots(0, 1, 0)):
            code, out, _ = run_main("gent")
        self.assertEqual(code, 0)
        self.assertIn("dry until 12:35, then 5 min of light rain", out)



if __name__ == "__main__":
    unittest.main()
