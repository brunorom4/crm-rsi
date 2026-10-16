"""
Migração aditiva: relatório disparado via Telegram, usando o Supabase como
ponte (o CRM fala com o Supabase, que ele mesmo fala com o Telegram -- assim
o bloqueio de rede da empresa não afeta, já que quem acessa o Telegram é o
servidor do Supabase, não a máquina do CRM).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]

novas_colunas = {
    "supabase_project_url": "TEXT",
    "supabase_anon_key": "TEXT",
    "supabase_telegram_ativo": "INTEGER DEFAULT 0",
    "supabase_telegram_horario_envio": "TEXT DEFAULT '18:00'",
    "supabase_telegram_dias_semana": "TEXT DEFAULT 'SEG,TER,QUA,QUI,SEX'",
    "supabase_telegram_ultimo_envio_data": "TEXT",
}
for coluna, tipo in novas_colunas.items():
    if coluna not in colunas:
        conn.execute(f"ALTER TABLE configuracoes ADD COLUMN {coluna} {tipo}")

conn.commit()
conn.close()
print("Migração do relatório via Telegram/Supabase concluída.")
