#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reasoning_summary injection, aligned with official CLI 2.179.1.

The rule was read out of the npm package (dist/codebuddy.js,
injectReasoningSummaryIfNeeded): inject "auto" when the request expresses that
thinking is wanted and the caller did not set the field itself. Two of those
signals are in the body; the third (a client-side "always thinking" setting)
is not knowable from the gateway, so it is deliberately not guessed.

Under-injecting only means no summary comes back; over-injecting changes what
the caller asked for. Every test below pins that asymmetry.

Run: python3 tests/_test_reasoning_summary.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_identity


class InjectionTests(unittest.TestCase):
    def test_reasoning_effort_triggers_auto(self):
        self.assertEqual(
            wb_identity.reasoning_summary_for({"reasoning_effort": "high"}),
            "auto")

    def test_nested_reasoning_effort_triggers_auto(self):
        self.assertEqual(
            wb_identity.reasoning_summary_for({"reasoning": {"effort": "low"}}),
            "auto")

    def test_plain_request_gets_nothing(self):
        self.assertIsNone(wb_identity.reasoning_summary_for({"model": "m"}))

    def test_model_that_cannot_disable_thinking_is_never_off(self):
        # map_reasoning_effort() already rewrote the level for such a model;
        # writing "none" back would undo that correction.
        self.assertFalse(wb_identity.thinking_is_off(
            {"reasoning_effort": "none"}, can_disable=False))
        self.assertFalse(wb_identity.thinking_is_off(
            {"thinking": {"type": "disabled"}}, can_disable=False))
        self.assertEqual(
            wb_identity.reasoning_summary_for({"reasoning_effort": "high"},
                                              can_disable=False),
            "auto")

    def test_camel_case_effort_is_read_like_snake_case(self):
        # wb_proxy.client_effort_of() reads both spellings; this must agree or
        # a camelCase client asking for "none" would be handed a summary.
        self.assertEqual(wb_identity._effort_text({"reasoningEffort": "high"}), "high")
        self.assertTrue(wb_identity.thinking_is_off({"reasoningEffort": "none"}))
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoningEffort": "none"}))
        self.assertEqual(wb_identity.reasoning_summary_for({"reasoningEffort": "high"}),
                         "auto")

    def test_effort_none_means_thinking_is_off(self):
        # The official adapter rewrites reasoning_effort to "none" when the
        # session has thinking disabled; that is not a request for a summary.
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoning_effort": "none"}))
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoning_effort": "NONE"}))
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoning": {"effort": "none"}}))

    def test_thinking_type_disabled_gets_nothing(self):
        self.assertIsNone(wb_identity.reasoning_summary_for(
            {"thinking": {"type": "disabled"}, "reasoning_effort": "high"}))

    def test_thinking_type_enabled_alone_does_not_trigger(self):
        # "enabled" is not one of the official triggers; only an effort string
        # is. Under-injecting is the safe direction.
        self.assertIsNone(wb_identity.reasoning_summary_for(
            {"thinking": {"type": "enabled"}}))

    def test_empty_effort_string_does_not_trigger(self):
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoning_effort": ""}))
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoning_effort": "   "}))

    def test_non_string_effort_does_not_trigger(self):
        # The official check is `typeof === "string"`, so a number or null must
        # not be treated as "thinking wanted".
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoning_effort": 5}))
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoning_effort": None}))
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoning": {"effort": 5}}))
        self.assertIsNone(wb_identity.reasoning_summary_for({"reasoning": "high"}))

    def test_caller_value_is_never_overwritten(self):
        for stated in ("auto", "concise", "detailed"):
            self.assertIsNone(wb_identity.reasoning_summary_for(
                {"reasoning_effort": "high", "reasoning_summary": stated}),
                "a caller-set reasoning_summary must survive")

    def test_malformed_body_is_ignored(self):
        for body in (None, [], "text", 5):
            self.assertIsNone(wb_identity.reasoning_summary_for(body))


class ConstantTests(unittest.TestCase):
    def test_value_matches_the_official_constant(self):
        self.assertEqual(wb_identity.CLI_REASONING_SUMMARY, "auto")

    def test_it_is_not_a_dropped_field(self):
        # cache_retention is injected after the drop pass; reasoning_summary
        # must not be dropped by the same list or the injection would vanish.
        self.assertNotIn("reasoning_summary", wb_identity.CLI_BODY_DROP_FIELDS)


class IntegrationTests(unittest.TestCase):
    """The shape the chat path actually builds for a CLI account."""

    def _body(self, payload):
        body = dict(payload)
        body = {k: v for k, v in body.items()
                if k not in wb_identity.CLI_BODY_DROP_FIELDS}
        body["cache_retention"] = wb_identity.CLI_CACHE_RETENTION
        summary = wb_identity.reasoning_summary_for(body)
        if summary:
            body["reasoning_summary"] = summary
        return body

    def test_thinking_request_carries_both_cli_fields(self):
        body = self._body({"model": "m", "reasoning_effort": "high",
                           "metadata": {"drop": "me"}})
        self.assertEqual(body["cache_retention"], "long")
        self.assertEqual(body["reasoning_summary"], "auto")
        self.assertNotIn("metadata", body)

    def test_plain_request_carries_only_cache_retention(self):
        body = self._body({"model": "m"})
        self.assertEqual(body["cache_retention"], "long")
        self.assertNotIn("reasoning_summary", body)


class ThinkingOffTests(unittest.TestCase):
    """The other official chat branch: force reasoning_effort to "none"."""

    def test_effort_none_is_off(self):
        self.assertTrue(wb_identity.thinking_is_off({"reasoning_effort": "none"}))
        self.assertTrue(wb_identity.thinking_is_off({"reasoning_effort": "NONE"}))
        self.assertTrue(wb_identity.thinking_is_off({"reasoning": {"effort": "none"}}))

    def test_thinking_type_disabled_is_off(self):
        self.assertTrue(wb_identity.thinking_is_off({"thinking": {"type": "disabled"}}))
        self.assertTrue(wb_identity.thinking_is_off(
            {"thinking": {"type": "Disabled"}, "reasoning_effort": "high"}))

    def test_other_states_are_not_off(self):
        for body in ({"reasoning_effort": "high"}, {"thinking": {"type": "enabled"}},
                     {"model": "m"}, None, "text"):
            self.assertFalse(wb_identity.thinking_is_off(body), repr(body))


class ChatBranchTests(unittest.TestCase):
    """What the CLI chat path writes, mirroring the official two branches."""

    def _body(self, payload):
        body = {k: v for k, v in payload.items()
                if k not in wb_identity.CLI_BODY_DROP_FIELDS}
        body["cache_retention"] = wb_identity.CLI_CACHE_RETENTION
        if wb_identity.thinking_is_off(body):
            if "reasoning_effort" in body or "reasoningEffort" in body:
                key = ("reasoning_effort" if "reasoning_effort" in body
                       else "reasoningEffort")
                body[key] = wb_identity.CLI_REASONING_EFFORT_OFF
            else:
                body["reasoning_effort"] = wb_identity.CLI_REASONING_EFFORT_OFF
        else:
            summary = wb_identity.reasoning_summary_for(body)
            if summary:
                body["reasoning_summary"] = summary
        return body

    def test_thinking_off_writes_effort_none_and_no_summary(self):
        body = self._body({"model": "m", "reasoning_effort": "none"})
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertNotIn("reasoning_summary", body)

    def test_thinking_off_from_thinking_type_adds_effort(self):
        body = self._body({"model": "m", "thinking": {"type": "disabled"}})
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertNotIn("reasoning_summary", body)

    def test_alternate_spelling_is_kept(self):
        body = self._body({"model": "m", "reasoningEffort": "none"})
        self.assertEqual(body["reasoningEffort"], "none")
        self.assertNotIn("reasoning_effort", body)

    def test_thinking_on_writes_summary_and_leaves_effort(self):
        body = self._body({"model": "m", "reasoning_effort": "high"})
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["reasoning_summary"], "auto")


class WireTests(unittest.TestCase):
    """The two injection branches are chat-only, mirroring official `!eG`."""

    def test_responses_call_site_declares_its_wire(self):
        # /v1/responses translates to a chat body and calls the same
        # open_upstream(); without wire="responses" the chat-only branches
        # would fire on a responses request.
        import re
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "wb_proxy.py"), encoding="utf-8").read()
        self.assertIn('wire="responses"', src)
        self.assertIn('wire="chat"', src)

    def test_default_wire_is_chat(self):
        import inspect
        import wb_proxy
        sig = inspect.signature(wb_proxy.open_upstream)
        self.assertEqual(sig.parameters["wire"].default, "chat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
