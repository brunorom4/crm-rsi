"""
Migração aditiva: coluna pra controlar o "Análise Pendente" da fila de GPON
Indisponível (mesma ideia do followup normal, mas fora da fila do operador).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(mailing)").fetchall()]
if "data_retorno_gpon" not in colunas:
    conn.execute("ALTER TABLE mailing ADD COLUMN data_retorno_gpon TEXT")
conn.commit()
conn.close()
print("Migração da fila de GPON Indisponível concluída.")
