# acfinder_pesticide.py
from db import get_conn

def search_by_pesticide(name: str):
    conn = get_conn()
    cur = conn.cursor()

    sql = """
        SELECT *
        FROM tekiyo
        WHERE tsusho LIKE ?
        ORDER BY sakumotsu, byochu
    """

    cur.execute(sql, (f"%{name}%",))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
