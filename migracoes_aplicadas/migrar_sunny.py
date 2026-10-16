"""
Migração aditiva: link do agente Sunny (Microsoft 365 Copilot / Agent Builder),
configurável pelo ADM sem precisar mexer em código.
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]
if "sunny_link" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN sunny_link TEXT")
    conn.execute("""
        UPDATE configuracoes SET sunny_link = ?
        WHERE id=1 AND (sunny_link IS NULL OR sunny_link = '')
    """, ("https://m365.cloud.microsoft/chat/agent/T_ac728b1f-c2d8-b6e5-cf99-bafe5e5234df.19d20543-28a5-46bc-9e84-8cf1ef841876.gpt.8d1f02c5-aa84-48c2-b2c6-f77e3cb3d705",))
conn.commit()
conn.close()
print("Migração do link do Sunny concluída.")
