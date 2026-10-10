import asyncio, sys
sys.path.insert(0, '/root/atlas/huggingface/scripts')
from main import launch_cdp_and_fill_cookie
from config import HF_JOIN_URL

async def test():
    pw, browser, context, page, port, cdp_proc = await launch_cdp_and_fill_cookie('test')
    console_msgs = []
    page.on('console', lambda msg: console_msgs.append(f'{msg.type}: {msg.text}') if msg.type in ('error','warning') else None)
    page.on('pageerror', lambda exc: console_msgs.append(f'PAGEERROR: {exc}'))

    async def log_request(route):
        try:
            method = route.request.method
            url = route.request.url[:80]
            print(f"  REQ: {method} {url}")
            await route.continue_()
        except Exception:
            await route.continue_()
    await page.route("**/*", log_request)

    try:
        await page.goto(HF_JOIN_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector('input[name="email"]', timeout=30000)

        await page.fill('input[name="email"]', "test123@example.com")
        print("Filled email")
        await page.fill('input[name="password"]', "TestPass123!")
        print("Filled password")

        # Click Next
        btns = await page.locator('button[type="submit"]').all()
        for btn in btns:
            text = await btn.inner_text()
            if 'next' in text.lower():
                await btn.click()
                print("Clicked Next")
                break

        await asyncio.sleep(3)
        print(f"\nURL after Next: {page.url}")
        print(f"Console errors ({len(console_msgs)}):")
        for m in console_msgs:
            print(f"  {m}")

        # Check if username field appeared (means Next worked)
        has_username = await page.evaluate('document.querySelector("input[name=\\"username\\"]") !== null')
        print(f"Has username input: {has_username}")
    finally:
        await pw.stop()

asyncio.run(test())
