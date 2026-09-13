#!/usr/bin/env python3
"""Entry point: python -m src.matrix_client"""

import asyncio
import os

from src.matrix_client.bot import CoaraMatrixBot


async def main():
    bot = CoaraMatrixBot(
        homeserver=os.getenv("MATRIX_HOMESERVER", "http://localhost:8008"),
        user_id=os.getenv("MATRIX_USER_ID", "@coara:coara.local"),
        password=os.getenv("MATRIX_PASSWORD"),
        device_id=os.getenv("MATRIX_DEVICE_ID", "COARA_AGENT"),
        device_name=os.getenv("MATRIX_DEVICE_NAME", "Coara Agent"),
        server_name=os.getenv("MATRIX_SERVER_NAME", "coara.local"),
    )
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
