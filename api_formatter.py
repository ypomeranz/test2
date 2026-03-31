#!/usr/bin/env python3
"""
API Endpoint Formatter
Usage: python api_formatter.py <url> [Header-Name=value ...]
       or run with no args to be prompted for a URL
"""

import sys
import json
import time
import argparse
from urllib import request, error
from urllib.parse import urlparse

# ANSI color codes
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[90m"
CYAN   = "\033[1;36m"
PURPLE = "\033[1;35m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BLUE   = "\033[34m"
UNDERLINE = "\033[4m"


def section(title: str):
    bar = "-" * 60
    print(f"{PURPLE}{bar}{RESET}")
    print(f"{PURPLE}  {title}{RESET}")
    print(f"{PURPLE}{bar}{RESET}")


def format_value(val) -> str:
    if val is None:
        return f"{DIM}null{RESET}"
    if isinstance(val, bool):
        return f"{GREEN}true{RESET}" if val else f"{RED}false{RESET}"
    if isinstance(val, (int, float)):
        return f"{YELLOW}{val}{RESET}"
    if isinstance(val, str):
        return f'{GREEN}"{val}"{RESET}'
    return str(val)


def pretty_print(data, indent: int = 0):
    pad = "  " * indent

    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, (dict, list)) and val:
                print(f"{pad}{CYAN}{key}{RESET}:")
                pretty_print(val, indent + 1)
            else:
                print(f"{pad}{CYAN}{key}{RESET}: {format_value(val)}")

    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, (dict, list)) and item:
                print(f"{pad}{CYAN}[{i}]{RESET}:")
                pretty_print(item, indent + 1)
            else:
                print(f"{pad}{CYAN}[{i}]{RESET}: {format_value(item)}")
    else:
        print(f"{pad}{format_value(data)}")


def status_color(code: int) -> str:
    if code < 300:
        return GREEN
    if code < 400:
        return YELLOW
    return RED


HTTP_STATUS_MESSAGES = {
    200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently",
    302: "Found", 304: "Not Modified", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    429: "Too Many Requests", 500: "Internal Server Error",
    502: "Bad Gateway", 503: "Service Unavailable",
}


def main():
    parser = argparse.ArgumentParser(
        description="Fetch an API endpoint and display the response readably."
    )
    parser.add_argument("url", nargs="?", help="API endpoint URL")
    parser.add_argument(
        "headers", nargs="*", metavar="Key=Value",
        help="Optional request headers as Key=Value pairs"
    )
    args = parser.parse_args()

    url = args.url
    if not url:
        url = input("Enter API endpoint URL: ").strip()

    if not url.startswith(("http://", "https://")):
        print(f"{RED}Error: URL must start with http:// or https://{RESET}")
        sys.exit(1)

    # Parse extra headers
    req_headers = {"User-Agent": "Python/api-formatter", "Accept": "*/*"}
    for h in (args.headers or []):
        if "=" in h:
            k, _, v = h.partition("=")
            req_headers[k.strip()] = v.strip()

    print(f"\nFetching: {UNDERLINE}{url}{RESET}\n")

    req = request.Request(url, headers=req_headers)
    start = time.monotonic()
    try:
        with request.urlopen(req) as resp:
            elapsed_ms = (time.monotonic() - start) * 1000
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            final_url = resp.url
            raw_bytes = resp.read()
    except error.HTTPError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        status = e.code
        content_type = e.headers.get("Content-Type", "")
        final_url = url
        raw_bytes = e.read()
    except error.URLError as e:
        print(f"{RED}Request failed: {e.reason}{RESET}")
        sys.exit(1)

    raw_text = raw_bytes.decode("utf-8", errors="replace")
    status_msg = HTTP_STATUS_MESSAGES.get(status, "")
    col = status_color(status)

    # --- Summary ---
    section("RESPONSE SUMMARY")
    print(f"  Status   : {col}{status} {status_msg}{RESET}")
    print(f"  URL      : {final_url}")
    print(f"  Time     : {elapsed_ms:.0f} ms")
    print(f"  Content  : {content_type or '(none)'}\n")

    if not raw_text.strip():
        section("BODY")
        print("  (empty response body)")
        return

    # Try to parse as JSON
    is_json = "json" in content_type or raw_text.strip()[0] in ("{", "[")
    parsed = None
    if is_json:
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            pass

    if parsed is not None:
        section("BODY (JSON)")
        pretty_print(parsed)
        print()
        section("RAW JSON")
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    else:
        section("BODY (raw text)")
        print(raw_text)


if __name__ == "__main__":
    main()
