import re
import sqlite3

from wechat_cli.core.context import AppContext


def main():
    ctx = AppContext()
    db_path = None
    for key in ctx.msg_db_keys:
        path = ctx.cache.get(key)
        if path:
            db_path = path
            break

    print(f"msg_db: {db_path}")
    if not db_path:
        return

    conn = sqlite3.connect(db_path)
    try:
        tables = [row[0] for row in conn.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()]
        msg_tables = [t for t in tables if t.startswith("Msg_")]
        print(f"msg_tables: {len(msg_tables)}")
        if msg_tables:
            sample = msg_tables[0]
            cols = [c[1] for c in conn.execute(
                f"pragma table_info([{sample}])"
            ).fetchall()]
            print(f"sample_msg_table: {sample}")
            print(f"columns: {cols}")
            img_cols = [c for c in cols if re.search(r"img|path|file|thumb|md5", c, re.I)]
            print(f"image_related_columns: {img_cols}")

        interesting = []
        for table in tables:
            cols = [c[1] for c in conn.execute(
                f"pragma table_info([{table}])"
            ).fetchall()]
            hits = [c for c in cols if re.search(r"img|path|file|thumb|md5", c, re.I)]
            if hits:
                interesting.append((table, hits))

        print(f"tables_with_image_like_columns: {len(interesting)}")
        for table, hits in interesting[:20]:
            print(f"{table} -> {hits}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
