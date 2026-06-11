import asyncio
from playwright.async_api import async_playwright
from hitter_core import single_hit, AutofillSelector, ProxyManager

async def test():
    card = {"card": "4242424242424242", "month": "12", "year": "25", "cvv": "123"}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        url = "https://checkout.stripe.com/pay/cs_test_dummy"
        # Dummy URL won't load anything real, but we just want to see if the initialization throws an error
        # wait, if page.goto fails, it throws an exception there. Let's not use page.goto in test, or use a real URL like example.com
        
        # Actually, let's just use the bot to hit a real target if we need to, but single_hit handles page.goto itself.
        
        url_info = {"amount": "$1.00", "merchant": "Test"}
        autofill_class = await AutofillSelector.detect(page, url)
        
        res = await single_hit(browser, url, card, 1, autofill_class, url_info)
        print("RESULT:")
        print(res)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
