"""
Migração aditiva: atualização periódica dos dados no Supabase (de hora em
hora, das 8h às 18h), pra permitir consulta a qualquer momento do dia pelo
Telegram via o comando /status.
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]
if "supabase_consulta_ativa" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN supabase_consulta_ativa INTEGER DEFAULT 1")
if "supabase_consulta_ultima_atualizacao" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN supabase_consulta_ultima_atualizacao TEXT")
conn.commit()
conn.close()
print("Migração da consulta periódica via Telegram concluída.")
