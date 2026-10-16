"""
Migração aditiva:
- mailing: adiciona prioridade (0 a 100, maior = mais prioritário na distribuição)
- usuarios: adiciona precisa_trocar_senha (1 = obrigado a trocar no próximo login)
"""
import sqlite3

conn = sqlite3.connect("crm.db")

colunas_mailing = [c[1] for c in conn.execute("PRAGMA table_info(mailing)").fetchall()]
if "prioridade" not in colunas_mailing:
    conn.execute("ALTER TABLE mailing ADD COLUMN prioridade INTEGER DEFAULT 0")
    conn.execute("UPDATE mailing SET prioridade = 0 WHERE prioridade IS NULL")

colunas_usuarios = [c[1] for c in conn.execute("PRAGMA table_info(usuarios)").fetchall()]
if "precisa_trocar_senha" not in colunas_usuarios:
    conn.execute("ALTER TABLE usuarios ADD COLUMN precisa_trocar_senha INTEGER DEFAULT 0")

conn.commit()
conn.close()
print("Migração de prioridade e troca de senha concluída.")