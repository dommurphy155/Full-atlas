"""Offline harness for prettylog — exercises every representative event.

Run:  /root/atlas/.venv/bin/python -m proxy.test_prettylog
Never imports the running proxy modules; safe while the service is live.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from . import logger as _logger  # noqa: F401  (installs root handler, as in prod)
from . import prettylog as pl

pl.attach()  # swap pretty formatter onto that root handler

log = logging.getLogger("atlas-proxy.test")


def hr(title: str) -> None:
    print(f"\n{'━' * 64}  {title}", file=sys.stderr)
    sys.stderr.flush()


def main() -> None:
    # 1 ── fast successful non-stream ---------------------------------------
    hr("1. FAST NON-STREAM (842ms, usage available)")
    t = pl.trace("aa3f5bc1a19c4b5e")
    t.t_start -= 0.842  # total duration 842ms
    t.sent(key_idx=1)
    t.t_upstream = t.t_sent + 0.11   # headers after 110ms connect
    t.usage(prompt_tokens=1204, completion_tokens=1842, total_tokens=3046)
    t.finish(status=200)

    # 2 ── medium request -----------------------------------------------------
    hr("2. MEDIUM NON-STREAM (4.82s)")
    t = pl.trace("bb2f5bc1a19c4b5e")
    t.t_start -= 4.82
    t.sent(key_idx=7)
    t.t_upstream = t.t_sent + 0.23
    t.finish(status=200)

    # 3 ── slow request -------------------------------------------------------
    hr("3. SLOW REQUEST (12.47s → ✗ red)")
    t = pl.trace("cc3f5bc1a19c4b5e")
    t.t_start -= 12.47
    t.sent(key_idx=12)
    t.t_upstream = t.t_sent + 0.31
    t.finish(status=200)

    # 4 ── failed upstream ----------------------------------------------------
    hr("4. FAILED UPSTREAM (502 after 4.8s)")
    t = pl.trace("81c2a4ff00000000")
    t.t_start -= 4.82
    t.sent(key_idx=3)
    t.fail(
        status=502,
        provider="openai",
        model="stealth/ox-alpha",
        phase="upstream",
        error="Bad Gateway",
    )

    # 5 ── streaming with TTFT + usage ----------------------------------------
    hr("5. STREAMING WITH USAGE")

    async def demo_stream():
        tr = pl.trace("dd3f5bc1a19c4b5e")
        tr.start("openai", "stealth/ox-alpha", "chat/completions", stream=True)

        async def src():
            await asyncio.sleep(0.842)  # TTFT
            for i in range(96):
                yield f"data: chunk {i}\n\n".encode()
                await asyncio.sleep(0.001)

        async for _ in pl.instrument_aiter(src(), tr):
            pass
        tr.usage(prompt_tokens=1204, completion_tokens=1842, total_tokens=3046)
        tr.finish(status=200)

    asyncio.run(demo_stream())

    # 5b ── streaming without usage ------------------------------------------
    hr("5b. STREAMING, NO USAGE (7.34s, TTFT 900ms)")
    t = pl.trace("ee3f5bc1a19c4b5e")
    t.t_start -= 7.34
    t.sent(key_idx=1)
    t.t_upstream = t.t_sent + 0.12
    for i in range(96):
        t.observe_chunk(b"data: x\n\n")
    t.t_first_byte = t.t_start + 0.9
    t.finish(status=200)

    # 6 ── key health ---------------------------------------------------------
    hr("6a. KEYS HEALTHY")
    pl.keys_health({"total": 401, "healthy": 401, "cooling": 0, "suspended": 0, "in_flight": 2})
    print()

    hr("6b. KEYS DEGRADED")
    pl.keys_health({"total": 401, "healthy": 372, "cooling": 21, "suspended": 8, "in_flight": 4})
    print()

    hr("6c. KEYS CRITICAL (all down)")
    pl.keys_health({"total": 401, "healthy": 0, "cooling": 0, "suspended": 401, "in_flight": 0})

    # 7 ── status variety ------------------------------------------------------
    hr("7. STATUS VARIETY (one-line summaries)")
    for st in (200, 201, 429, 500, 502, 503):
        t = pl.trace(f"{st:04d}f5bc1a19c4b5"[:16])
        t.t_start -= 2.31
        t.sent(key_idx=2)
        t.finish(status=st)

    # 8 ── connection failure / timeout ---------------------------------------
    hr("8. CONNECT FAILURE / TIMEOUT")
    t = pl.trace("ab12cd34ef56ab00")
    t.t_start -= 0.35
    t.fail(status=502, provider="openai", model="stealth/ox-alpha", phase="connect", error="Connection refused (111)")
    t = pl.trace("cd34ef56ab12cd00")
    t.t_start -= 30.02
    t.sent(key_idx=9)
    t.fail(status=504, provider="openai", model="stealth/ox-alpha", phase="read-timeout", error="Timed out after 30s")

    # 9 ── payload_path present vs empty --------------------------------------
    hr("9. PAYLOAD PATH FIELD (empty omitted, present shown)")
    pl.trace("1234567890abcdef").start(
        "openai", "stealth/ox-alpha", "chat/completions", stream=False)
    pl.trace("fedcba0987654321").start(
        "openai", "stealth/ox-alpha", "chat/completions", stream=False,
        payload_path="/tmp/atlas_payloads/payload_fedcba0987654321.json")

    # 10 ── legacy line cleanup (root formatter on old-style messages) --------
    hr("10. LEGACY LINES THROUGH NEW FORMATTER (incl. key redaction)")
    log.info(
        "req=%s provider=anthropic endpoint=messages model=%s stream=False payload_path= upstream=https://openrouter.ai/api/v1/messages",
        "de31968977b346d1", "stealth/ox-alpha",
    )
    log.info("req=%s key_idx=0 status=200 latency=4530ms bytes=1057", "de31968977b346d1")
    log.warning("Key idx=0 permanently retired (key=%s..., retired_count=1)", "hf_kzKkxGHEhUzxauoyL9xQw")
    log.info(
        "Key health: total=%d healthy=%d cooling=%d suspended=%d in_flight=%d",
        401, 401, 0, 0, 1,
    )

    print()


if __name__ == "__main__":
    main()
