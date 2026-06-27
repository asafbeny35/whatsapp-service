"""
WhatsApp Web microservice — runs on Railway with persistent Playwright session.
The main Vercel app calls POST /send with JSON body.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

SECRET_TOKEN = os.environ.get("WHATSAPP_SERVICE_SECRET", "")

app = FastAPI()

_PLAYWRIGHT = None
_CONTEXT = None
_PAGE = None  # single persistent page — keeps WhatsApp Web in memory
_CONTEXT_LOCK = asyncio.Lock()
_BIDI_CHARS = {0x200F, 0x200E, 0x202B, 0x202A, 0x202C, 0x202D, 0x202E}
_WA_READY = False  # True once WhatsApp Web is loaded


@app.on_event("startup")
async def _startup():
    """Pre-warm the browser and load WhatsApp Web on startup."""
    asyncio.create_task(_warm_whatsapp())


@app.on_event("shutdown")
async def _on_shutdown():
    """Close Chromium gracefully so profile data is flushed to Railway volume."""
    import logging
    global _PAGE, _CONTEXT, _PLAYWRIGHT
    logging.warning("Shutdown: closing Chromium context...")
    if _PAGE and not _PAGE.is_closed():
        try:
            await _PAGE.close()
        except Exception:
            pass
    if _CONTEXT:
        try:
            await _CONTEXT.close()
        except Exception:
            pass
    if _PLAYWRIGHT:
        try:
            await _PLAYWRIGHT.stop()
        except Exception:
            pass
    logging.warning("Shutdown: Chromium closed.")


async def _warm_whatsapp():
    global _WA_READY
    try:
        import logging
        logging.warning("Warming up WhatsApp Web...")
        _, page = await _get_fresh_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
            window.chrome = {runtime: {}};
        """)
        await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
        # Wait until authenticated — check for any persistent UI element that only appears after login
        for _ in range(90):
            await page.wait_for_timeout(2000)
            authenticated = page.locator(
                "[data-testid='chatlist-header'], "
                "[data-testid='drawer-left'], "
                "header[data-testid='chatlist-header'], "
                "div[aria-label='Chat list'], "
                "#side, div[data-testid='chat-list']"
            )
            if await authenticated.count() > 0:
                _WA_READY = True
                logging.warning("WhatsApp Web warmed up and authenticated.")
                return
        logging.warning("WhatsApp Web warm-up: not authenticated (QR scan needed).")
    except Exception as exc:
        import logging
        logging.error(f"WhatsApp warm-up failed: {exc}")

PROFILE_DIR = Path(os.environ.get("WHATSAPP_PROFILE_DIR", "/app/whatsapp-profile"))
PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0"):
        digits = "972" + digits[1:]
    return digits


def _file_send_wait_ms(size_bytes: int) -> int:
    size_mb = max(size_bytes, 1) / (1024 * 1024)
    return max(8000, min(int(7000 + size_mb * 4500), 30000))


def _file_post_send_wait_ms(size_bytes: int) -> int:
    size_mb = max(size_bytes, 1) / (1024 * 1024)
    return max(18000, min(int(14000 + size_mb * 7000), 60000))


async def _launch_context():
    global _PLAYWRIGHT, _CONTEXT
    if _PLAYWRIGHT:
        try:
            await _PLAYWRIGHT.stop()
        except Exception:
            pass
    _PLAYWRIGHT = await async_playwright().start()
    _CONTEXT = await _PLAYWRIGHT.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=True,
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-session-crashed-bubble",
            "--disable-blink-features=AutomationControlled",
        ],
    )


async def _get_fresh_page():
    """Return the persistent page, restarting it if crashed."""
    global _PLAYWRIGHT, _CONTEXT, _PAGE
    async with _CONTEXT_LOCK:
        # Check if context is alive
        if _CONTEXT is not None:
            try:
                _ = _CONTEXT.pages
            except Exception:
                _CONTEXT = None
                _PAGE = None

        if _CONTEXT is None:
            await _launch_context()

        # Reuse persistent page if still alive, else open a new one
        if _PAGE is not None:
            try:
                if _PAGE.is_closed():
                    _PAGE = None
                else:
                    # Quick check: try to evaluate JS
                    await _PAGE.evaluate("1+1")
            except Exception:
                _PAGE = None

        if _PAGE is None:
            try:
                _PAGE = await _CONTEXT.new_page()
            except Exception:
                _CONTEXT = None
                _PAGE = None
                await _launch_context()
                _PAGE = await _CONTEXT.new_page()

        return _CONTEXT, _PAGE


async def _send(phone: str, message: str, file_items: list[dict]) -> dict:
    """file_items: list of {name, content_b64, size_bytes}"""
    phone = _normalize_phone(phone)
    _, page = await _get_fresh_page()

    from urllib.parse import quote
    send_url = f"https://web.whatsapp.com/send?phone={phone}"
    if message:
        send_url += f"&text={quote(message)}"

    await page.goto(send_url, wait_until="domcontentloaded", timeout=60000)

    # Quick auth check — fail fast instead of hanging 330s in _chat_ready
    await page.wait_for_timeout(4000)
    _qr_count = await page.locator("canvas, div[data-ref]").count()
    _chat_count = await page.locator("footer, #side, [data-testid='chatlist-header']").count()
    if _qr_count and not _chat_count:
        raise RuntimeError(
            "WhatsApp Web is not authenticated. "
            "Visit /qr/page to scan the QR code, then retry."
        )

    attach_button = page.locator("button[aria-label='Attach']")
    message_box = page.locator(
        "footer div[contenteditable='true'], "
        "div[contenteditable='true'][data-tab='10'], "
        "div[contenteditable='true'][role='textbox'], "
        "div[contenteditable='true']"
    ).last
    send_icon = page.locator(
        "span[data-icon='send'], button[aria-label='Send'], "
        "div[role='button'][aria-label='Send']"
    )

    async def _chat_ready():
        for loc in (attach_button, message_box, send_icon):
            try:
                if await loc.count() and await loc.first.is_visible():
                    return True
            except Exception:
                pass
        return False

    for attempt in range(360):
        if await _chat_ready():
            break
        await page.wait_for_timeout(500 if attempt < 60 else 1000)
    else:
        raise RuntimeError("WhatsApp chat did not become ready in time")

    if message:
        try:
            await send_icon.first.wait_for(timeout=10000)
            await send_icon.first.click()
        except Exception:
            try:
                await message_box.wait_for(timeout=10000)
                await message_box.fill(message)
                await page.keyboard.press("Enter")
            except Exception:
                await page.keyboard.press("Enter")
        await page.wait_for_timeout(1200)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for item in file_items:
            file_path = tmp_path / item["name"]
            file_path.write_bytes(base64.b64decode(item["content_b64"]))
            size_bytes = item.get("size_bytes", file_path.stat().st_size)

            attachment_send_button = page.locator(
                "div[role='button'][aria-label='Send'], "
                "button[aria-label='Send'], "
                "div[role='button'][aria-label='שלח'], "
                "button[aria-label='שלח'], "
                "span[data-icon='send'], "
                "[data-testid='send']"
            ).last

            # Open attach menu and click Document to get a file chooser
            await attach_button.first.wait_for(timeout=30000)
            try:
                async with page.expect_file_chooser(timeout=20000) as fc_info:
                    await attach_button.first.click()
                    await page.wait_for_timeout(1000)
                    # Click Document option — selector confirmed from live WhatsApp Web DOM
                    for selector in [
                        "button[role='menuitem'][aria-label='Document']",
                        "button[role='menuitem'][aria-label='מסמך']",
                        "[role='menuitem'][aria-label='Document']",
                        "[role='menuitem'][aria-label='מסמך']",
                        "li[data-testid='mi-attach-document']",
                        "li[data-testid='attach-document']",
                    ]:
                        loc = page.locator(selector)
                        try:
                            if await loc.count() > 0:
                                await loc.first.click(timeout=5000)
                                break
                        except Exception:
                            continue
                fc = await fc_info.value
                await fc.set_files(str(file_path))
                await page.wait_for_timeout(5000)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not attach {item['name']}: {exc}. "
                    "Check /qr/page to confirm session is active."
                )

            # Wait for attachment send button — try clicking, fall back to Enter
            try:
                await attachment_send_button.wait_for(timeout=20000)
            except Exception:
                pass  # fall through to Enter fallback
            await page.wait_for_timeout(max(2000, _file_send_wait_ms(size_bytes) - 3000))

            clicked = False
            for attempt_cfg in [{"force": False}, {"force": True}]:
                try:
                    await attachment_send_button.click(timeout=4000, **attempt_cfg)
                    await page.wait_for_timeout(900)
                    clicked = True
                    break
                except Exception:
                    pass

            if not clicked:
                await page.keyboard.press("Enter")

            await page.wait_for_timeout(_file_post_send_wait_ms(size_bytes))

    await page.wait_for_timeout(5000)
    # Don't close the page — keep it alive so WhatsApp Web stays in memory
    return {"status": "ok", "phone": phone}


@app.get("/health")
async def health():
    return {"ok": True, "wa_ready": _WA_READY}


async def _get_whatsapp_screenshot() -> bytes:
    import base64 as _b64
    _, page = await _get_fresh_page()
    # Hide automation fingerprints before loading
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
        window.chrome = {runtime: {}};
    """)
    await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
    # Wait for QR canvas or chat
    for _ in range(20):
        await page.wait_for_timeout(1500)
        qr_canvas = page.locator("canvas")
        chat_ready = page.locator("div[aria-label='Chat list'], div[data-icon='chat']")
        if await qr_canvas.count() > 0 or await chat_ready.count() > 0:
            break
    await page.wait_for_timeout(1000)

    # Try to extract canvas pixel data via JS (works even when CSS rendering fails)
    try:
        canvas_data_url = await page.evaluate("""() => {
            const canvas = document.querySelector('canvas');
            if (!canvas) return null;
            try { return canvas.toDataURL('image/png'); } catch(e) { return null; }
        }""")
        if canvas_data_url and canvas_data_url.startswith("data:image/png;base64,"):
            return _b64.b64decode(canvas_data_url.split(",", 1)[1])
    except Exception:
        pass

    return await page.screenshot(full_page=False)


@app.get("/qr/page", response_class=None)
async def qr_page():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>WhatsApp QR</title>
  <style>
    body { background:#111; display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:100vh; margin:0; font-family:sans-serif; color:#fff; }
    img { width:300px; height:300px; border:4px solid #25D366; border-radius:8px; }
    p { margin-top:16px; color:#aaa; font-size:14px; }
    #timer { color:#25D366; font-size:20px; font-weight:bold; }
  </style>
</head>
<body>
  <h2 style="color:#25D366">סרוק את ה-QR עם WhatsApp</h2>
  <img id="qr" src="/qr/image" alt="QR Code">
  <p>מתרענן אוטומטית בעוד <span id="timer">20</span> שניות</p>
  <script>
    let t = 20;
    setInterval(() => {
      t--;
      document.getElementById('timer').textContent = t;
      if (t <= 0) {
        t = 20;
        document.getElementById('qr').src = '/qr/image?t=' + Date.now();
      }
    }, 1000);
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/qr")
async def qr_endpoint():
    """Open WhatsApp Web and return a screenshot so the user can scan the QR code."""
    screenshot = await _get_whatsapp_screenshot()
    import base64 as _b64
    return {"screenshot_b64": _b64.b64encode(screenshot).decode(), "url": "https://web.whatsapp.com"}


@app.get("/qr/image")
async def qr_image():
    """Return QR screenshot as PNG image."""
    from fastapi.responses import Response
    try:
        screenshot = await _get_whatsapp_screenshot()
        return Response(content=screenshot, media_type="image/png")
    except Exception as exc:
        import traceback
        return JSONResponse({"error": str(exc), "trace": traceback.format_exc()}, status_code=500)



@app.post("/send")
async def send_endpoint(body: dict):
    if SECRET_TOKEN and body.get("secret") != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        result = await _send(
            phone=body["phone"],
            message=body.get("message", ""),
            file_items=body.get("files", []),
        )
        return result
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
