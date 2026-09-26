import sqlite3

conn = sqlite3.connect('data/scanner.db')
cursor = conn.cursor()
tables = cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table';").fetchall()
for name, sql in tables:
    print(f"Table: {name}")
    print(f"SQL: {sql}\n")
    count = cursor.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    print(f"Row count: {count}\n")
