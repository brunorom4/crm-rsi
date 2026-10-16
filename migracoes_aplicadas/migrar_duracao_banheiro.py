"""
Migração aditiva: duração própria da Pausa Banheiro, configurável pelo ADM
independente da pausa curta (manhã/tarde) e do almoço.
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]
if "duracao_banheiro_min" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN duracao_banheiro_min INTEGER")
conn.commit()
conn.close()
print("Migração de duração da pausa banheiro concluída.")
