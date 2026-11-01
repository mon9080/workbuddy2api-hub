#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Identity version cache (wb_identity_versions).

The cache exists so the CLI version string can follow the official package
without a network round trip per request. Everything here is about the failure
modes, because the happy path is one fetch:

  - a fresh row is used and no fetch happens
  - a stale or malformed row triggers exactly one fetch
  - a failed fetch leaves the verified default in place and does not raise
  - a pre-release string is not a version we are willing to send
  - the panel value still wins over whatever the cache holds

No network: every fetch is injected.

Run: python3 tests/_test_identity_version_cache.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_identity
import wb_identity_versions as V

NOW = 1800000000.0
DAY = 86400.0


class _DirMixin(object):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wbver-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_cache(self, data):
        with open(V.cache_path(self.dir), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def read_cache(self):
        with open(V.cache_path(self.dir), "r", encoding="utf-8") as fh:
            return json.load(fh)


class FreshnessTests(unittest.TestCase):
    def test_ttl_is_three_days(self):
        self.assertEqual(V.CACHE_TTL_SECONDS, 3 * DAY)

    def test_fresh_inside_the_ttl(self):
        self.assertTrue(V.is_fresh(NOW - DAY, now=NOW))

    def test_stale_past_the_ttl(self):
        self.assertFalse(V.is_fresh(NOW - 3 * DAY - 1, now=NOW))

    def test_zero_is_never_fresh(self):
        self.assertFalse(V.is_fresh(0, now=NOW))


class ResolveTests(_DirMixin, unittest.TestCase):
    def test_fresh_cache_is_used_without_fetching(self):
        self.write_cache({"cli": {"version": "9.9.9", "fetched_at": NOW - DAY}})
        calls = []

        def fetch():
            calls.append(1)
            return "1.1.1"

        got = V.resolve(self.dir, fetch=fetch, now=NOW)
        self.assertEqual(got, {"cli": "9.9.9"})
        self.assertEqual(calls, [], "a fresh row must not trigger a fetch")

    def test_stale_cache_fetches_once_and_rewrites(self):
        self.write_cache({"cli": {"version": "1.0.0", "fetched_at": NOW - 4 * DAY}})
        calls = []

        def fetch():
            calls.append(1)
            return "2.0.0"

        got = V.resolve(self.dir, fetch=fetch, now=NOW)
        self.assertEqual(got, {"cli": "2.0.0"})
        self.assertEqual(len(calls), 1)
        row = self.read_cache()["cli"]
        self.assertEqual(row["version"], "2.0.0")
        self.assertEqual(row["fetched_at"], NOW)
        self.assertEqual(row["source"], "npm")

    def test_missing_cache_fetches(self):
        got = V.resolve(self.dir, fetch=lambda: "3.0.0", now=NOW)
        self.assertEqual(got, {"cli": "3.0.0"})

    def test_failed_fetch_falls_back_to_nothing(self):
        """An empty result means "keep the verified defaults" - not an error."""
        got = V.resolve(self.dir, fetch=lambda: None, now=NOW)
        self.assertEqual(got, {})
        self.assertFalse(os.path.exists(V.cache_path(self.dir)),
                         "a failed fetch must not write a cache row")

    def test_stale_cache_survives_a_failed_refetch_as_stale(self):
        """The old row stays on disk; it is simply not reported as current."""
        self.write_cache({"cli": {"version": "1.0.0", "fetched_at": NOW - 4 * DAY}})
        got = V.resolve(self.dir, fetch=lambda: None, now=NOW)
        self.assertEqual(got, {})
        self.assertEqual(self.read_cache()["cli"]["version"], "1.0.0")
        cached = V.cached_versions(self.dir, now=NOW)
        self.assertFalse(cached["cli"]["fresh"])

    def test_prerelease_is_rejected(self):
        got = V.resolve(self.dir,
                        fetch=lambda: "2.178.1-dev.12384a3.202610301543",
                        now=NOW)
        self.assertEqual(got, {})

    def test_prerelease_already_in_cache_is_ignored(self):
        self.write_cache({"cli": {"version": "2.178.1-dev.abc", "fetched_at": NOW}})
        got = V.resolve(self.dir, fetch=lambda: "2.179.1", now=NOW)
        self.assertEqual(got, {"cli": "2.179.1"})

    def test_future_timestamp_is_not_trusted(self):
        self.write_cache({"cli": {"version": "9.9.9", "fetched_at": NOW + 3600}})
        got = V.resolve(self.dir, fetch=lambda: "4.0.0", now=NOW)
        self.assertEqual(got, {"cli": "4.0.0"})

    def test_in_process_copy_survives_a_failed_write(self):
        """The in-process copy is what keeps a read-only cache dir usable.

        Windows ignores directory permission bits, so instead of chmod this
        replaces _write_cache with one that always fails - the same condition
        the guard exists for, on every platform.
        """
        calls = []

        def fetch():
            calls.append(1)
            return "8.8.8"

        d = tempfile.mkdtemp(prefix="wbver-ro-")
        original = V._write_cache
        try:
            V._write_cache = lambda *a, **k: False
            first = V.resolve(d, fetch=fetch, now=NOW)
            second = V.resolve(d, fetch=fetch, now=NOW)
            self.assertEqual(first, {"cli": "8.8.8"})
            self.assertEqual(second, {"cli": "8.8.8"})
            self.assertEqual(len(calls), 1,
                             "the in-process copy must survive a failed write")
        finally:
            V._write_cache = original
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_clear_cache_drops_the_in_process_copy(self):
        d = tempfile.mkdtemp(prefix="wbver-clr-")
        try:
            V.resolve(d, fetch=lambda: "7.7.7", now=NOW)
            V.clear_cache(d)
            calls = []
            V.resolve(d, fetch=lambda: (calls.append(1), "6.6.6")[1], now=NOW)
            self.assertEqual(len(calls), 1, "clearing must force a refetch")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_corrupt_cache_file_is_treated_as_empty(self):
        with open(V.cache_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        got = V.resolve(self.dir, fetch=lambda: "5.0.0", now=NOW)
        self.assertEqual(got, {"cli": "5.0.0"})


class CachedVersionsTests(_DirMixin, unittest.TestCase):
    def test_reports_age_and_freshness(self):
        self.write_cache({"cli": {"version": "7.0.0", "fetched_at": NOW - DAY}})
        cached = V.cached_versions(self.dir, now=NOW)
        self.assertEqual(cached["cli"]["version"], "7.0.0")
        self.assertEqual(cached["cli"]["age_seconds"], int(DAY))
        self.assertTrue(cached["cli"]["fresh"])

    def test_empty_cache_reports_nothing(self):
        self.assertEqual(V.cached_versions(self.dir, now=NOW), {})


class FetchTests(unittest.TestCase):
    """The real fetcher, with the transport replaced."""

    class _Resp(object):
        def __init__(self, payload):
            self.payload = payload

        def read(self, _n=None):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_reads_the_latest_tag(self):
        def opener(request, timeout=None):
            return self._Resp(json.dumps({"version": "2.179.1"}).encode())

        self.assertEqual(V.fetch_cli_version(opener=opener), "2.179.1")

    def test_transport_error_returns_none(self):
        def opener(request, timeout=None):
            raise OSError("no route to host")

        self.assertIsNone(V.fetch_cli_version(opener=opener))

    def test_non_json_returns_none(self):
        def opener(request, timeout=None):
            return self._Resp(b"<html>502 Bad Gateway</html>")

        self.assertIsNone(V.fetch_cli_version(opener=opener))

    def test_prerelease_is_not_returned(self):
        def opener(request, timeout=None):
            return self._Resp(json.dumps({"version": "2.179.1-rc.1"}).encode())

        self.assertIsNone(V.fetch_cli_version(opener=opener))


class PanelValueWinsTests(unittest.TestCase):
    """The proxy merges panel overrides over the cache; panel must come first."""

    def test_panel_value_is_not_overwritten_by_the_cache(self):
        import wb_proxy

        d = tempfile.mkdtemp(prefix="wbver-")
        try:
            import wb_settings
            wb_settings.set_identity_versions(d, {"cli": "6.6.6"})
            with open(V.cache_path(d), "w", encoding="utf-8") as fh:
                json.dump({"cli": {"version": "9.9.9",
                                   "fetched_at": __import__("time").time()}}, fh)
            wb_settings.set_auto_refresh_versions(d, True)
            got = wb_proxy._identity_version_overrides(d)
            self.assertEqual(got["cli"], "6.6.6")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_switch_off_ignores_the_cache(self):
        import wb_proxy
        import wb_settings

        d = tempfile.mkdtemp(prefix="wbver-")
        try:
            wb_settings.set_auto_refresh_versions(d, False)
            with open(V.cache_path(d), "w", encoding="utf-8") as fh:
                json.dump({"cli": {"version": "9.9.9",
                                   "fetched_at": __import__("time").time()}}, fh)
            got = wb_proxy._identity_version_overrides(d)
            self.assertNotIn("cli", got)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class IdentityIntegrationTests(unittest.TestCase):
    """What the cache actually changes on the wire: the string, and only it."""

    def setUp(self):
        wb_identity.configure_versions({})

    def tearDown(self):
        wb_identity.configure_versions({})

    def test_override_changes_the_cli_version_string(self):
        before = wb_identity.cli_version()
        wb_identity.configure_versions({"cli": "2.179.1"})
        self.assertEqual(wb_identity.cli_version(), "2.179.1")
        self.assertNotEqual(before, "2.179.1")

    def test_override_leaves_the_header_family_alone(self):
        wb_identity.configure_versions({"cli": "2.179.1"})
        headers = wb_identity.cli_stainless_headers()
        # The stainless family is what makes a request look like the official
        # Node SDK; a version bump must not add or drop any of it.
        for key in ("x-stainless-lang", "x-stainless-package-version",
                    "x-stainless-runtime", "x-stainless-retry-count",
                    "x-stainless-timeout"):
            self.assertIn(key, headers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
