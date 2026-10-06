"""Run the API with an event loop compatible with psycopg on Windows."""

import asyncio
import sys
from pathlib import Path

import uvicorn
from agent_core.dotenv import find_dotenv, load_dotenv


def main() -> None:
    env_path = find_dotenv(Path.cwd())
    if env_path:
        load_dotenv(env_path)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run("erpilot_api.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
