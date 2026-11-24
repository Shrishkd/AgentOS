"""Build the public demonstration inventory once, then open it read-only at runtime."""
import sqlite3
from contextlib import closing
from pathlib import Path

DATABASE = Path(__file__).with_name("sample-inventory.sqlite3")


def build(path=DATABASE):
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE inventory (name TEXT PRIMARY KEY, category TEXT NOT NULL, quantity INTEGER NOT NULL)")
        db.executemany("INSERT INTO inventory VALUES (?,?,?)", [
            ("Monitor", "hardware", 8), ("Keyboard", "hardware", 12), ("Editor license", "software", 5)])
        db.commit()


def lookup(args, path=DATABASE):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT name,quantity FROM inventory WHERE category=? ORDER BY name LIMIT ?",
                          (args.category, args.limit))
        return {"items": [dict(row) for row in rows]}


if __name__ == "__main__":
    build()
