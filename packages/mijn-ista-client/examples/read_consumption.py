"""Read raw consumption payloads without Home Assistant."""

import asyncio
import getpass
import logging
import os
from pprint import pprint

from mijn_ista_client import open_client


async def main() -> None:
    username = os.environ.get("ISTA_USERNAME") or input("ista email: ")
    password = os.environ.get("ISTA_PASSWORD") or getpass.getpass("ista password: ")
    async with open_client(username, password) as api:
        await api.authenticate()
        user_values = await api.get_user_values()
        pprint(user_values)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
