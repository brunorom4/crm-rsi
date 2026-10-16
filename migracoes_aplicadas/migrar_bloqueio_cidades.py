"""
Migração aditiva: bloqueio de distribuição automática por cidade (pra quando
falta técnico, tem evento climático, etc -- o ADM pausa a distribuição só
naquela(s) cidade(s), sem afetar o resto).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
conn.execute("""
CREATE TABLE IF NOT EXISTS cidades_bloqueio_distribuicao(
    cidade TEXT PRIMARY KEY,
    bloqueado INTEGER NOT NULL DEFAULT 0,
    motivo TEXT,
    atualizado_em TEXT,
    atualizado_por TEXT
)
""")
conn.commit()
conn.close()
print("Migração de bloqueio de distribuição por cidade concluída.")
