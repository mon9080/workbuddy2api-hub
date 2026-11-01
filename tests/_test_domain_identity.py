#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import-time identity binding (wb_settings.auto_bind_identity).

Three layers, because each can fail independently:

  1. The domain table itself - which host means which product, and that an
     unknown host means "no opinion" rather than a guess.
  2. normalise_import_row() with the switch off - the result must be byte-for-
     byte the old shape (no "product" key at all), which is what keeps an
     upgraded install behaving exactly as it did before.
  3. The same call with the switch on, including the cases that must still win
     over the table: an explicitly stated product, and an unknown domain.

Run: python3 tests/_test_domain_identity.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_identity

#: A structurally valid but unsigned JWT: only the three-segment shape and the
#: claims we read are exercised here. Nothing is sent anywhere.
def _token(issuer="https://www.codebuddy.cn/auth/realms/copilot", sub="u1"):
    import base64
    import json
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": sub, "iss": issuer, "exp": 2000000000}).encode()
    ).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0.%s.sig" % payload


class DomainTableTests(unittest.TestCase):
    def test_codebuddy_hosts_bind_to_the_cli_identity(self):
        for host in ("www.codebuddy.cn", "copilot.tencent.com", "www.codebuddy.ai"):
            self.assertEqual(wb_identity.product_for_domain(host),
                             wb_identity.PRODUCT_CLI, host)

    def test_workbuddy_hosts_bind_to_the_desktop_identity(self):
        for host in ("www.workbuddy.cn", "www.workbuddy.ai"):
            self.assertEqual(wb_identity.product_for_domain(host),
                             wb_identity.PRODUCT_DESKTOP, host)

    def test_case_and_surrounding_shape_do_not_matter(self):
        self.assertEqual(wb_identity.product_for_domain("WWW.CodeBuddy.CN"),
                         wb_identity.PRODUCT_CLI)
        self.assertEqual(wb_identity.product_for_domain("https://www.codebuddy.cn/"),
                         wb_identity.PRODUCT_CLI)
        self.assertEqual(wb_identity.product_for_domain("www.codebuddy.cn:443"),
                         wb_identity.PRODUCT_CLI)
        self.assertEqual(wb_identity.product_for_domain("  www.workbuddy.ai  "),
                         wb_identity.PRODUCT_DESKTOP)

    def test_unknown_or_empty_host_has_no_opinion(self):
        for value in ("", None, "example.com", "codebuddy.cn.evil.test",
                      "www.codebuddy.cn.example.com"):
            self.assertIsNone(wb_identity.product_for_domain(value), repr(value))


class SwitchOffTests(unittest.TestCase):
    """The default path must not gain a "product" key - that is the contract."""

    def setUp(self):
        self.row = {"accessToken": _token(), "domain": "www.codebuddy.cn",
                    "uid": "u1"}

    def test_import_writes_no_product_by_default(self):
        kwargs = wb_accounts.normalise_import_row(dict(self.row))
        self.assertNotIn("product", kwargs)

    def test_import_writes_no_product_when_asked_not_to_bind(self):
        kwargs = wb_accounts.normalise_import_row(dict(self.row),
                                                  bind_identity=False)
        self.assertNotIn("product", kwargs)

    def test_a_stated_product_survives_without_the_switch(self):
        row = dict(self.row, product="vscode")
        kwargs = wb_accounts.normalise_import_row(row)
        self.assertEqual(kwargs["product"], wb_identity.PRODUCT_VSCODE)


class SwitchOnTests(unittest.TestCase):
    def setUp(self):
        self.row = {"accessToken": _token(), "domain": "www.codebuddy.cn",
                    "uid": "u1"}

    def test_domestic_codebuddy_account_binds_to_cli(self):
        kwargs = wb_accounts.normalise_import_row(dict(self.row),
                                                  bind_identity=True)
        self.assertEqual(kwargs["product"], wb_identity.PRODUCT_CLI)

    def test_international_workbuddy_account_binds_to_desktop(self):
        row = {"accessToken": _token("https://www.workbuddy.ai/auth/realms/copilot"),
               "domain": "www.workbuddy.ai", "uid": "u2"}
        kwargs = wb_accounts.normalise_import_row(row, bind_identity=True)
        self.assertEqual(kwargs["product"], wb_identity.PRODUCT_DESKTOP)

    def test_stated_product_still_wins_over_the_table(self):
        row = dict(self.row, product="vscode")
        kwargs = wb_accounts.normalise_import_row(row, bind_identity=True)
        self.assertEqual(kwargs["product"], wb_identity.PRODUCT_VSCODE)

    def test_unknown_domain_writes_nothing(self):
        # A host the table does not know survives domain_for_realm() untouched
        # (it names no realm), so the import has nothing to bind from and must
        # leave the key out rather than guess a product.
        row = {"accessToken": _token(issuer="https://example.invalid/auth"),
               "domain": "example.invalid", "uid": "u3"}
        kwargs = wb_accounts.normalise_import_row(row, bind_identity=True)
        self.assertEqual(kwargs["domain"], "example.invalid")
        self.assertNotIn("product", kwargs)

    def test_missing_domain_binds_to_the_canonical_host(self):
        """No domain at all normalises to the realm's own host, so it binds.

        That is not a guess: domain_for_realm() has already replaced the absent
        value with the canonical host for the detected realm, and the same
        binding applies to an explicit import of that host.
        """
        row = {"accessToken": _token(issuer="https://example.invalid/auth"),
               "uid": "u4"}
        kwargs = wb_accounts.normalise_import_row(row, bind_identity=True)
        self.assertEqual(kwargs["domain"], "www.workbuddy.ai")
        self.assertEqual(kwargs["product"], wb_identity.PRODUCT_DESKTOP)

    def test_bound_product_is_what_the_account_actually_uses(self):
        """The kwargs must survive Account construction, not just the dict."""
        kwargs = wb_accounts.normalise_import_row(dict(self.row),
                                                  bind_identity=True)
        account = wb_accounts.Account(kwargs)
        self.assertEqual(account.product, wb_identity.PRODUCT_CLI)
        # And the endpoint follows the identity, which is the whole point.
        self.assertEqual(account.chat_base_url(),
                         wb_identity.endpoint_for("cn", wb_identity.PRODUCT_CLI)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
