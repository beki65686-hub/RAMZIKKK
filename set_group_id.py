import sqlite3

DB_PATH = r"C:\Users\HP\bot.py\allowed_users.db"
GROUP_ID = "-1004368733935"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute(
    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
    ("group_chat_id", GROUP_ID),
)
conn.commit()
print(cur.execute("SELECT key, value FROM settings WHERE key = 'group_chat_id'").fetchall())
conn.close()
