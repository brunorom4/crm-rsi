"""
Migração aditiva: configuração do bot do Telegram (relatório de fechamentos e
agendamentos, enviado automaticamente no fim do dia e disponível sob demanda
por comando no bot).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]

novas_colunas = {
    "telegram_bot_token": "TEXT",
    "telegram_chat_id": "TEXT",
    "telegram_envio_automatico_ativo": "INTEGER DEFAULT 1",
    "telegram_horario_envio": "TEXT DEFAULT '18:00'",
    "telegram_ultimo_envio_data": "TEXT",
    "telegram_ultimo_update_id": "INTEGER",
}
for coluna, tipo in novas_colunas.items():
    if coluna not in colunas:
        conn.execute(f"ALTER TABLE configuracoes ADD COLUMN {coluna} {tipo}")

conn.commit()
conn.close()
print("Migração do bot do Telegram concluída.")
