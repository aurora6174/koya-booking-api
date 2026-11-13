import os

import psycopg
from psycopg.rows import dict_row


async def connect() -> psycopg.AsyncConnection:
    """Open one connection per request.

    On serverless hosting there is no long-lived process to hold a pool, so each
    request connects (through Neon's pooler) and the connection is closed afterwards.
    Used as `async with await connect() as conn:` - it commits when the block
    exits normally and rolls back if it raises.

    prepare_threshold=None turns off server-side prepared statements, which
    don't work through a transaction-mode pooler like PgBouncer.
    """
    return await psycopg.AsyncConnection.connect(
        os.environ["DATABASE_URL"],
        row_factory=dict_row,
        prepare_threshold=None,
    )
