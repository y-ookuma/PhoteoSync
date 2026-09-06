# acfinder_pest.py
from db import get_conn

def search_by_pest(name: str):
    conn = get_conn()
    cur = conn.cursor()

    sql = """
        SELECT *
        FROM tekiyo
        WHERE byochu LIKE ?
        ORDER BY tsusho, byochu
    """

    cur.execute(sql, (f"%{name}%",))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
