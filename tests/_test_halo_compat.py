#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三处从 hello-halo v3.0.5 借鉴的兼容性改进。

来源：openkursar/hello-halo 标签 v3.0.5。它本身没有开源 WorkBuddy/CodeBuddy
的账户实现（腾讯登录在闭源 product 变体里），可借鉴的是通用转换链路上的三处
做法。这里各钉一条：

  1. SDK 会话头识别（session_header_key_enabled，默认关）
     Claude Code / Codex 把会话 id 放在 `x-claude-code-session-id` 或
     `session_id` 头里，不放进 body。关着时 extract_session_key 与上游一字
     不差；打开后这两个头参与提取，且仍然优先信任客户端自己的会话头。

  2. 客户端断连传播（SSEHeartbeat）
     心跳线程写失败后，旧实现只把自己标记关闭，后续 write() 静默返回，请求
     线程继续把上游读到 EOF。现在记下 _client_gone 并让下一次 write() 抛
     ConnectionAbortedError——各流处理器都已把这条异常当作 client_aborted
     处理，会关闭上游、释放账号名额。

  3. usage 缺失时的输入估算（estimate_prompt_tokens）
     旧兜底用「输出 token 数 // 2」当输入，与输入毫无关系。现在按实际上行的
     messages + tools 估算；只有拿不到请求体时才退回旧算法。

运行：python tests/_test_halo_compat.py
"""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="wb-halo-compat-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy
import wb_settings


class SessionHeaderTests(unittest.TestCase):
    """会话头识别开关：默认关，关着时与上游一字不差。"""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="wb-session-header-")
        self._old = wb_proxy.ACCOUNTS_DIR
        wb_proxy.ACCOUNTS_DIR = self._dir

    def tearDown(self):
        wb_proxy.ACCOUNTS_DIR = self._old

    def test_default_is_off(self):
        self.assertFalse(wb_settings.session_header_key_enabled(self._dir))
        self.assertFalse(wb_proxy.session_header_key_enabled())

    def test_off_ignores_the_sdk_headers(self):
        headers = {"x-claude-code-session-id": "sess-abc"}
        self.assertIsNone(wb_proxy.extract_session_key(headers, {}))
        headers = {"session_id": "sess-abc"}
        self.assertIsNone(wb_proxy.extract_session_key(headers, {}))

    def test_on_reads_both_spellings(self):
        wb_settings.set_session_header_key_enabled(self._dir, True)
        self.assertEqual(
            wb_proxy.extract_session_key(
                {"x-claude-code-session-id": "sess-abc"}, {}), "sess-abc")
        self.assertEqual(
            wb_proxy.extract_session_key({"session_id": "sess-xyz"}, {}),
            "sess-xyz")

    def test_own_headers_still_win(self):
        # 网关自己的会话头优先级不变：SDK 头只是补位，不抢已有语义。
        wb_settings.set_session_header_key_enabled(self._dir, True)
        headers = {"X-Conversation-Id": "own", "session_id": "sdk"}
        self.assertEqual(wb_proxy.extract_session_key(headers, {}), "own")

    def test_body_still_wins_over_sdk_header(self):
        # SDK 头排在最末，body 里的会话 id 仍然优先。
        wb_settings.set_session_header_key_enabled(self._dir, True)
        self.assertEqual(
            wb_proxy.extract_session_key({"session_id": "sdk"},
                                         {"conversation_id": "body"}), "body")

    def test_blank_header_does_not_mask_the_body(self):
        wb_settings.set_session_header_key_enabled(self._dir, True)
        self.assertEqual(
            wb_proxy.extract_session_key({"x-claude-code-session-id": "  "},
                                         {"conversation_id": "body"}), "body")

    def test_blank_sdk_header_alone_yields_none(self):
        # 空白的 SDK 头不能变成一个空字符串会话键。
        wb_settings.set_session_header_key_enabled(self._dir, True)
        self.assertIsNone(
            wb_proxy.extract_session_key({"session_id": "   "}, {}))

    def test_setter_rejects_non_boolean(self):
        for bad in ("true", 1, None):
            with self.assertRaises(ValueError):
                wb_settings.set_session_header_key_enabled(self._dir, bad)


class HeartbeatTests(unittest.TestCase):
    """客户端断连必须传播给读循环，而不是只在心跳线程里悄悄退出。"""

    def test_hangup_is_raised_on_the_next_write(self):
        written = []
        state = {"raised": False}

        def write(data):
            written.append(data)
            if not state["raised"]:
                # 只有第一次是真实的断连异常；之后 socket 已经处于坏状态，
                # 请求线程不该再碰它，而应由 _client_gone 拦下来。
                state["raised"] = True
                raise BrokenPipeError("client gone")
            raise AssertionError("the socket must not be written again")

        beat = wb_proxy.SSEHeartbeat(write, interval=60.0)
        try:
            with self.assertRaises(BrokenPipeError):
                beat.write(b"data: x\n\n")
            # 之后的每次写都要继续抛，读循环才会停下来。
            with self.assertRaises(ConnectionAbortedError):
                beat.write(b"data: y\n\n")
            with self.assertRaises(ConnectionAbortedError):
                beat.write(b"data: z\n\n")
        finally:
            beat.close()

    def test_clean_close_still_swallows_writes(self):
        # close() 之后的写继续静默返回，不把正常收尾变成异常。
        beat = wb_proxy.SSEHeartbeat(lambda data: None, interval=60.0)
        beat.close()
        beat.write(b"data: z\n\n")   # 不应抛

    def test_other_write_failures_are_not_reported_as_hangup(self):
        # 非断连类失败不冒充客户端断开，否则会把正常请求记成 client_aborted。
        def write(data):
            raise OSError("disk full")

        beat = wb_proxy.SSEHeartbeat(write, interval=60.0)
        try:
            with self.assertRaises(OSError):
                beat.write(b"data: x\n\n")
            self.assertFalse(beat._client_gone)
        finally:
            beat.close()

    def test_keepalive_hangup_reaches_the_request_thread(self):
        # 心跳线程先发现断连时，请求线程的下一次 write 必须抛出去。
        def write(data):
            if b"keepalive" in data:
                raise ConnectionResetError("peer reset")
            return None

        beat = wb_proxy.SSEHeartbeat(write, interval=0.05)
        try:
            import time as _t
            deadline = _t.time() + 3.0
            while _t.time() < deadline and not beat._client_gone:
                _t.sleep(0.02)
            self.assertTrue(beat._client_gone, "keepalive hangup was not recorded")
            with self.assertRaises(ConnectionAbortedError):
                beat.write(b"data: x\n\n")
        finally:
            beat.close()


class PromptEstimateTests(unittest.TestCase):
    """usage 缺失时按实际请求估算输入，而不是拿输出的一半充数。"""

    def test_long_prompt_short_answer_is_not_undercounted(self):
        messages = [{"role": "user", "content": "上下文 " * 2000}]
        est = wb_proxy.estimate_prompt_tokens(messages)
        self.assertGreater(est, 500)

    def test_tools_are_counted(self):
        messages = [{"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {
            "name": "read", "description": "读文件 " * 200, "parameters": {}}}]
        without = wb_proxy.estimate_prompt_tokens(messages)
        with_tools = wb_proxy.estimate_prompt_tokens(messages, tools)
        self.assertGreater(with_tools, without)

    def test_empty_request_is_zero(self):
        self.assertEqual(wb_proxy.estimate_prompt_tokens(None), 0)
        self.assertEqual(wb_proxy.estimate_prompt_tokens([]), 0)

    def test_malformed_entries_do_not_raise(self):
        self.assertEqual(
            wb_proxy.estimate_prompt_tokens([None, 5, "text"]), 0)

    def test_aggregate_uses_the_request_when_usage_is_missing(self):
        # 上游一个 usage 都不给：兜底必须来自 prompt_source，而不是 comp // 2。
        import json as _json

        long_text = "上下文 " * 3000
        chunks = [
            'data: {"id":"c1","model":"m","choices":[{"index":0,'
            '"delta":{"content":"短答案"},"finish_reason":null}]}\n\n',
            'data: [DONE]\n\n',
        ]
        raw = [c.encode("utf-8") for c in chunks]
        obj = wb_proxy.aggregate_stream(
            iter(raw), "m", None,
            prompt_source={"messages": [{"role": "user", "content": long_text}]})
        usage = obj["usage"]
        self.assertGreater(usage["prompt_tokens"], usage["completion_tokens"])

    def test_aggregate_falls_back_without_a_request(self):
        # 拿不到请求体时保持旧行为，不能因此报错。
        chunks = [
            'data: {"id":"c1","model":"m","choices":[{"index":0,'
            '"delta":{"content":"答案"},"finish_reason":"stop"}]}\n\n',
            'data: [DONE]\n\n',
        ]
        raw = [c.encode("utf-8") for c in chunks]
        obj = wb_proxy.aggregate_stream(iter(raw), "m", None)
        self.assertGreater(obj["usage"]["prompt_tokens"], 0)

    def test_reported_usage_is_never_overwritten(self):
        # 上游给了 usage 就必须原样采用，估算只在缺失时兜底。
        chunks = [
            'data: {"id":"c1","model":"m","usage":{"prompt_tokens":7,'
            '"completion_tokens":3,"total_tokens":10},'
            '"choices":[{"index":0,"delta":{"content":"x"},'
            '"finish_reason":"stop"}]}\n\n',
            'data: [DONE]\n\n',
        ]
        raw = [c.encode("utf-8") for c in chunks]
        obj = wb_proxy.aggregate_stream(
            iter(raw), "m", None,
            prompt_source={"messages": [{"role": "user", "content": "长" * 5000}]})
        self.assertEqual(obj["usage"]["prompt_tokens"], 7)
        self.assertEqual(obj["usage"]["total_tokens"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
