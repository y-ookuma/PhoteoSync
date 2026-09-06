# acfinder_crop.py
from db import get_conn

def search_by_crop(name: str):
    conn = get_conn()
    cur = conn.cursor()

    sql = """
        SELECT *
        FROM tekiyo
        WHERE sakumotsu LIKE ?
        ORDER BY tsusho, sakumotsu
    """

    cur.execute(sql, (f"%{name}%",))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
