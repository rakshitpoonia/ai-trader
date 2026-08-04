import sqlite3
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

DB = "accounts.db"

with sqlite3.connect(DB) as conn:
    cursor = conn.cursor()
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY, account TEXT)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            datetime DATETIME,
            type TEXT,
            message TEXT
        )
    ''')
    conn.commit()
