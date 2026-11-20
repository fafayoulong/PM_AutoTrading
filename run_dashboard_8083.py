from src.config import load_config
from src.dashboard import Dashboard
from src.paper_trader import PaperTrader


async def main():
    config = load_config()
    trader = PaperTrader(config)
    dashboard = Dashboard(config, trader)
    await dashboard.start(port=8084)
    await asyncio.Event().wait()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
