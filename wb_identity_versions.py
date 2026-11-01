# -*- coding: utf-8 -*-
"""Refresh outbound identity version numbers from the official source.

Only the CodeBuddy CLI version has a public source: the npm package
`@tencent-ai/codebuddy-code` publishes it under the `latest` dist-tag. The
desktop client and the VSCode extension expose theirs through an upgrade
endpoint that requires an account token (`/console/upgrade/check` answers 401
to anonymous callers), so they are deliberately not refreshed here - inventing
a source would be worse than keeping the verified constant.

Cached values are handed to wb_identity.configure_versions(), which changes
only the string on the wire, never the header family or the request body. Those
were verified per version, so a newer number sent with an older shape is a
worse disguise than an older number; that is why the caller has to opt in.
"""
import json
import os
import re
import threading
import time
import urllib.request

#: npm package that carries the official CodeBuddy CLI.
CLI_PACKAGE = "@tencent-ai/codebuddy-code"
CLI_LATEST_URL = ("https://registry.npmjs.org/%s/latest"
                  % CLI_PACKAGE.replace("/", "%2F"))

#: Three days. Version numbers move on a weekly scale, not a minute one, and a
#: refresh costs a network round trip on a host that may not have one.
CACHE_TTL_SECONDS = 3 * 24 * 3600

#: Cache file name inside the accounts directory.
CACHE_FILENAME = "identity_version_cache.json"

#: Only plain releases. npm also publishes 2.178.1-dev.<sha>.<stamp>, which the
#: `latest` tag never points at, but a mirror or a future tag could - and a
#: pre-release string on the wire is not something the verified shapes cover.
_PLAIN_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

#: The cache only ever carries keys wb_identity knows about.
_REFRESHABLE = ("cli",)

_lock = threading.Lock()
#: 进程内最近一次成功获取的结果，按缓存路径分开存。
#: 磁盘缓存是跨进程的，但它可能在只读挂载 / 磁盘满时写不进去；此时若只靠
#: 重读磁盘去重，每个并发调用都会各自再拉一次。这里留一份内存结果兜底，
#: 让写盘失败不影响"同一轮只拉一次"。
_memory = {"loaded": False, "data": {}}


def _recall(accounts_dir, key, now, ttl):
    """In-process fallback for a version whose disk write failed.

    Caller must already hold _lock: resolve() reads this inside the same
    critical section that decides whether to fetch, so taking the lock again
    here would deadlock (threading.Lock is not re-entrant).
    """
    row = (_memory["data"].get(accounts_dir) or {}).get(key)
    if not row:
        return None
    version, fetched_at = row
    return version if is_fresh(fetched_at, now=now, ttl=ttl) else None


def _forget(accounts_dir):
    with _lock:
        _memory["data"].pop(accounts_dir, None)


def cache_path(accounts_dir):
    return os.path.join(accounts_dir, CACHE_FILENAME)


def _read_cache(accounts_dir):
    """Read the cache file; anything unreadable reads as empty."""
    try:
        with open(cache_path(accounts_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(accounts_dir, data):
    """Atomically replace the cache file. A failure here is not a fetch failure."""
    path = cache_path(accounts_dir)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        os.makedirs(accounts_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _clean_entry(entry, now):
    """Return (version, fetched_at) for a usable cache row, or (None, 0)."""
    if not isinstance(entry, dict):
        return None, 0
    version = str(entry.get("version") or "").strip()
    if not _PLAIN_VERSION.match(version):
        return None, 0
    try:
        fetched_at = float(entry.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return None, 0
    if fetched_at <= 0 or fetched_at > now + 300:
        # A timestamp in the future is not evidence of freshness; treat the row
        # as unusable rather than trusting a clock that already looks wrong.
        return None, 0
    return version, fetched_at


def is_fresh(fetched_at, now=None, ttl=CACHE_TTL_SECONDS):
    now = time.time() if now is None else now
    return fetched_at > 0 and (now - fetched_at) < ttl


def fetch_cli_version(timeout=10, opener=None):
    """Ask npm for the current CLI release. Returns the version or None.

    Any failure (no network, DNS, non-200, unparsable body, pre-release string)
    returns None: the caller keeps the verified default. This runs on a
    gateway host that may be firewalled, so failing quietly is the contract.
    """
    open_url = opener or urllib.request.urlopen
    try:
        request = urllib.request.Request(CLI_LATEST_URL, method="GET")
        request.add_header("Accept", "application/json")
        with open_url(request, timeout=timeout) as resp:
            raw = resp.read(65536)
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    version = str(payload.get("version") or "").strip()
    return version if _PLAIN_VERSION.match(version) else None


def resolve(accounts_dir, *, fetch=None, now=None, ttl=CACHE_TTL_SECONDS):
    """Return the version overrides to apply, refreshing the cache if stale.

    Returns a dict of wb_identity override keys (possibly empty). An empty
    result means "use the verified defaults" and is the answer whenever the
    cache is missing, stale and unfetchable, or the fetched value is unusable.
    """
    now = time.time() if now is None else now
    data = _read_cache(accounts_dir)
    overrides = {}
    stale = []
    for key in _REFRESHABLE:
        version, fetched_at = _clean_entry(data.get(key), now)
        if version is None:
            stale.append(key)
        elif is_fresh(fetched_at, now=now, ttl=ttl):
            overrides[key] = version
        else:
            stale.append(key)
    if not stale:
        return overrides

    fetcher = fetch or (lambda: fetch_cli_version())
    with _lock:
        # Re-read inside the lock: two concurrent callers must not both fetch.
        data = _read_cache(accounts_dir)
        changed = False
        for key in stale:
            version, fetched_at = _clean_entry(data.get(key), now)
            if version is not None and is_fresh(fetched_at, now=now, ttl=ttl):
                overrides[key] = version
                continue
            # 写盘失败时磁盘里永远是旧值；先看进程内有没有刚拉到的新值。
            remembered = _recall(accounts_dir, key, now, ttl)
            if remembered:
                overrides[key] = remembered
                continue
            if key != "cli":
                continue
            fetched = fetcher()
            # Validate whatever the fetcher returned: the built-in one already
            # filters, but an injected or future source must not be able to put
            # a pre-release string on the wire just by returning it.
            fetched = str(fetched or "").strip()
            if not _PLAIN_VERSION.match(fetched):
                continue
            data[key] = {"version": fetched, "fetched_at": now, "source": "npm"}
            overrides[key] = fetched
            changed = True
            # 内存兜底在锁外也能用：写盘失败不影响这一轮的结果复用。
            _memory["data"].setdefault(accounts_dir, {})[key] = (fetched, now)
        if changed:
            _write_cache(accounts_dir, data)
    return overrides


def cached_versions(accounts_dir, now=None, ttl=CACHE_TTL_SECONDS):
    """Read the cache without fetching. Used by the panel to show what it holds."""
    now = time.time() if now is None else now
    data = _read_cache(accounts_dir)
    out = {}
    for key in _REFRESHABLE:
        version, fetched_at = _clean_entry(data.get(key), now)
        if version is None:
            continue
        out[key] = {"version": version,
                    "fetched_at": fetched_at,
                    "age_seconds": int(max(0, now - fetched_at)),
                    "fresh": is_fresh(fetched_at, now=now, ttl=ttl)}
    return out


def clear_cache(accounts_dir):
    """Drop the cache file and the in-process copy. True when it is gone."""
    _forget(accounts_dir)
    try:
        os.unlink(cache_path(accounts_dir))
        return True
    except OSError:
        return True
