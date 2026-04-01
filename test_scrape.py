import asyncio
from scraper import ScheduleParser

async def main():
    p = ScheduleParser()
    await p.init()
    print("Fetching Эк-25109...")
    res = await p.fetch(wo=0, t_type="group", t_val="Эк-25109")
    print("Result Эк-25109:", res)
    await p.browser.close()
    await p.playwright.stop()

if __name__ == "__main__":
    asyncio.run(main())
