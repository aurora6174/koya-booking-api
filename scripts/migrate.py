"""Create the tables. Run once per database:  DATABASE_URL=... python scripts/migrate.py"""
import os
from pathlib import Path

import psycopg

schema = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    conn.execute(schema)
print("schema applied")
