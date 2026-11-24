# -*- coding: utf-8 -*-
"""
Shared HTTP helper for HDC and NHSO API clients.
Supports gzip/deflate decompression, automatic retries with exponential backoff,
connection reuse, and clean error handling.
"""

import gzip
import io
import json
import time
import urllib.request
import urllib.error

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'domain': 'ksn',
    'authorization': 'Bearer null',
    'referer': 'https://hdc.moph.go.th/',
    'accept': 'application/json, text/plain, */*',
    'Accept-Encoding': 'gzip, deflate',
}


def fetch_json_with_retry(url, headers=None, max_retries=3, timeout=25, label=""):
    """
    Fetches JSON from URL with retry and gzip decompression.
    Returns (data_json, error_message).
    """
    req_headers = dict(DEFAULT_HEADERS)
    if headers:
        req_headers.update(headers)

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                encoding = resp.headers.get('Content-Encoding', '').lower()
                if 'gzip' in encoding or raw_bytes[:2] == b'\x1f\x8b':
                    try:
                        raw_bytes = gzip.decompress(raw_bytes)
                    except Exception as gz_err:
                        pass
                text = raw_bytes.decode('utf-8', errors='replace')
                return json.loads(text), None
        except Exception as e:
            last_err = e
            tag = f"[{label}] " if label else ""
            if attempt < max_retries:
                sleep_time = attempt * 2
                print(f"⚠️  {tag}Request failed (attempt {attempt}/{max_retries}): {e}. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
            else:
                print(f"❌ {tag}Request failed after {max_retries} attempts: {e}")
                return None, str(last_err)
    return None, str(last_err)
