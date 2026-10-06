import asyncio


def pytest_asyncio_loop_factories(config, item):
    # psycopg async requires selectors on Windows; a single factory also works on Linux.
    return {"selector": asyncio.SelectorEventLoop}
