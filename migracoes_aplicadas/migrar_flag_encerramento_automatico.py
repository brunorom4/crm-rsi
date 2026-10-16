"""
Migração aditiva: flag pro ADM ligar/desligar o encerramento automático de
jornada às 18h30, quando precisar (ex: um dia de virada de mês, plantão, etc.).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]
if "encerramento_automatico_ativo" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN encerramento_automatico_ativo INTEGER DEFAULT 1")
    conn.execute("UPDATE configuracoes SET encerramento_automatico_ativo = 1 WHERE encerramento_automatico_ativo IS NULL")
conn.commit()
conn.close()
print("Migração do flag de encerramento automático concluída.")
