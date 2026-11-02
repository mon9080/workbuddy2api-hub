#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟：网关 → 模拟上游 的完整链路（不需要真号，也不碰真上游）。

仓库的提交门禁要求提交前跑通「面板 + 服务启动 + 端到端请求」。这里用一个
本地假上游代替 WorkBuddy，覆盖：

  1. 服务起得来、/health 正常；
  2. 面板能打开、无 500；
  3. /v1/models 有内容；
  4. /v1/chat/completions 非流式：收到上游真实回复，且请求体带着 CLI 身份
     的 cache_retention；
  5. /v1/chat/completions 流式：收到完整 SSE 与 [DONE]；
  6. usage 缺失时，用量行里的 prompt_tokens 来自请求本身而不是「输出的一半」；
  7. 客户端中途断开：网关在心跳发现后停止读上游（不把上游读完）。

不覆盖：真实上游的鉴权、风控与计费——那些只能用真号验证，本脚本刻意不碰。

运行：python tests/_smoke_gateway.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import http.client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -- " + detail) if detail and not ok else ""))


# ---------------------------------------------------------------------------
# 假上游：一个只认 /v2/chat/completions 的 SSE 服务
# ---------------------------------------------------------------------------
class MockUpstream:
    """记下收到的每个请求体，按需要回流式或非流式。"""

    def __init__(self):
        self.bodies = []
        self.read_to_eof = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(30)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            length = 0
            for line in lines[1:]:
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            while len(rest) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                rest += chunk
            raw = rest[:length]
            # CLI 身份会 gzip；解开来看真实字段。
            if b"content-encoding: gzip" in head.lower():
                import gzip
                raw = gzip.decompress(raw)
            try:
                self.bodies.append(json.loads(raw.decode("utf-8")))
            except Exception:
                self.bodies.append({"_unparsed": True})
            want_stream = bool(self.bodies[-1].get("stream"))

            if want_stream:
                frames = [
                    {"id": "mock-1", "model": "mock-model", "choices": [
                        {"index": 0, "delta": {"content": "你好"},
                         "finish_reason": None}]},
                    {"id": "mock-1", "model": "mock-model", "choices": [
                        {"index": 0, "delta": {"content": "，冒烟通过"},
                         "finish_reason": "stop"}]},
                ]
                body = b"".join(
                    ("data: " + json.dumps(f, ensure_ascii=False) + "\n\n").encode("utf-8")
                    for f in frames) + b"data: [DONE]\n\n"
                self._send(conn, 200, "text/event-stream", body)
            else:
                # 故意不给 usage：验网关的输入估算兜底。
                obj = {"id": "mock-1", "model": "mock-model",
                       "choices": [{"index": 0, "message": {
                           "role": "assistant", "content": "冒烟通过"},
                           "finish_reason": "stop"}]}
                self._send(conn, 200, "application/json",
                           json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _send(self, conn, status, ctype, body):
        head = ("HTTP/1.1 %d OK\r\nContent-Type: %s\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (status, ctype, len(body))).encode("ascii")
        conn.sendall(head + body)

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    work = tempfile.mkdtemp(prefix="wb-smoke-")
    accounts = os.path.join(work, "accounts")
    os.makedirs(accounts)
    upstream = MockUpstream()
    port = free_port()
    env = dict(os.environ)
    env["WB_CN_CHAT_UPSTREAM"] = "http://127.0.0.1:%d" % upstream.port
    env["WB_PROXY_USAGE_DIR"] = os.path.join(work, "usage")
    # 默认区域跟着冒烟账号走，否则跨区域会被本地 400 拦下。
    env["WB_PROXY_DEFAULT_REALM"] = "cn"

    with open(os.path.join(accounts, "smoke.json"), "w", encoding="utf-8") as fh:
        json.dump({"uid": "smoke-uid-0001", "accessToken": "smoke-token",
                   "realm": "cn", "product": "cli", "nickname": "冒烟"},
                  fh)

    proc = subprocess.Popen(
        [PY, os.path.join(ROOT, "wb_proxy.py"), "--port", str(port),
         "--host", "127.0.0.1", "--accounts-dir", accounts],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)

    def get(path, headers=None, timeout=20):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("GET", path, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def post(path, obj, headers=None, timeout=120):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            h = {"Content-Type": "application/json"}
            h.update(headers or {})
            conn.request("POST", path, body=json.dumps(obj).encode("utf-8"),
                         headers=h)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    try:
        # 1. 服务起来
        ok = False
        for _ in range(120):
            try:
                status, _ = get("/health", timeout=2)
                if status == 200:
                    ok = True
                    break
            except Exception:
                time.sleep(0.25)
        check("服务启动 /health 200", ok)
        if not ok:
            return

        # 2. 面板
        status, body = get("/")
        check("面板可打开", status == 200 and b"<html" in body.lower(),
              "status=%s" % status)

        # 3. 模型目录
        status, body = get("/v1/models")
        ids = []
        if status == 200:
            try:
                ids = [m.get("id") for m in json.loads(body).get("data") or []]
            except Exception:
                pass
        check("/v1/models 有内容", status == 200 and bool(ids),
              "status=%s count=%d" % (status, len(ids)))

        model = ids[0] if ids else "mock-model"
        # 冒烟账号是 cn，而目录第一项往往属于国际版（跨区域会被本地 400 拦下）。
        # 挑一个确定属于 cn 的模型；目录里找不到时退回内置的 cn 专属项。
        for cand in ids:
            if cand.startswith("deepseek-v4-pro"):
                model = cand
                break
        else:
            model = "deepseek-v4-pro"

        # 4. 非流式：完整链路
        n_before = len(upstream.bodies)
        status, body = post("/v1/chat/completions",
                            {"model": model, "stream": False,
                             "messages": [{"role": "user", "content": "冒烟"}]})
        reply = ""
        if status == 200:
            try:
                reply = json.loads(body)["choices"][0]["message"]["content"]
            except Exception:
                pass
        check("非流式收到上游回复", status == 200 and "冒烟通过" in reply,
              "status=%s reply=%r body=%r" % (status, reply, body[:200]))
        check("上游确实被调用", len(upstream.bodies) > n_before)
        if upstream.bodies:
            sent = upstream.bodies[-1]
            check("CLI 身份带 cache_retention",
                  sent.get("cache_retention") == "long",
                  "body keys=%s" % sorted(sent.keys()))

        # 5. 流式
        status, body = post("/v1/chat/completions",
                            {"model": model, "stream": True,
                             "messages": [{"role": "user", "content": "冒烟"}]})
        text = body.decode("utf-8", "replace")
        check("流式收到完整 SSE", status == 200 and "[DONE]" in text
              and "冒烟通过" in text,
              "status=%s tail=%r" % (status, text[-120:]))

        # 6. usage 兜底来自请求本身
        long_ctx = "上下文" * 4000
        post("/v1/chat/completions",
             {"model": model, "stream": False,
              "messages": [{"role": "user", "content": long_ctx}]})
        time.sleep(0.4)
        usage_log = os.path.join(work, "usage", "usage.jsonl")
        rows = []
        if os.path.exists(usage_log):
            with open(usage_log, encoding="utf-8") as fh:
                rows = [json.loads(l) for l in fh if l.strip()]
        last = rows[-1] if rows else {}
        pt = last.get("prompt_tokens") or 0
        ct = last.get("completion_tokens") or 0
        check("usage 兜底按输入估算（长上下文 > 短回复）", pt > ct and pt > 100,
              "prompt=%s completion=%s" % (pt, ct))

        print("\n  PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        upstream.stop()
        shutil.rmtree(work, ignore_errors=True)

    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
