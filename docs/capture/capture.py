#!/usr/bin/env python3
"""Captures the README screenshot of the public web surface.

smite.diemer.codes serves data and API liveness for both games off the share
and calls nothing, so this is the same page any visitor gets.

    pip install playwright && playwright install chromium
    python3 docs/capture/capture.py [base-url]   # default https://smite.diemer.codes

Writes docs/shots/*.png.
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://smite.diemer.codes").rstrip("/")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shots")
os.makedirs(OUT, exist_ok=True)


def settle(page, ms=3200):
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(ms)
    page.mouse.move(2, 2)


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 940}, device_scale_factor=2)
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="load")
    settle(page)
    page.screenshot(path=os.path.join(OUT, "web.png"))
    print("web        /")
    page.screenshot(path=os.path.join(OUT, "web-full.png"), full_page=True)
    print("web-full   / (full page)")

    ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3,
                        is_mobile=True, has_touch=True)
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="load")
    settle(page)
    page.screenshot(path=os.path.join(OUT, "mobile.png"))
    print("mobile     390x844")
    b.close()
