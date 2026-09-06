# db.py
import sqlite3

DB_PATH = "data/acis.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
