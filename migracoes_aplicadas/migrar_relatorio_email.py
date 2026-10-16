"""
Migração aditiva: relatório por e-mail (substitui o bot do Telegram, que
esbarrou em bloqueio de rede da empresa).
"""
import sqlite3

conn = sqlite3.connect("crm.db")

conn.execute("""
CREATE TABLE IF NOT EXISTS relatorio_email_destinatarios(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    ativo INTEGER NOT NULL DEFAULT 1
)
""")

colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]
novas_colunas = {
    "email_smtp_host": "TEXT",
    "email_smtp_porta": "INTEGER DEFAULT 587",
    "email_smtp_usuario": "TEXT",
    "email_smtp_senha": "TEXT",
    "email_remetente": "TEXT",
    "email_envio_automatico_ativo": "INTEGER DEFAULT 1",
    "email_horario_envio": "TEXT DEFAULT '18:00'",
    "email_ultimo_envio_data": "TEXT",
}
for coluna, tipo in novas_colunas.items():
    if coluna not in colunas:
        conn.execute(f"ALTER TABLE configuracoes ADD COLUMN {coluna} {tipo}")

conn.commit()
conn.close()
print("Migração do relatório por e-mail concluída.")
