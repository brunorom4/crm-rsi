"""
Migração aditiva: cria a tabela de configuração do modo de trabalho
(ATIVO = distribuição automática de contratos / RECEPTIVO = operador busca o contrato).
"""
import sqlite3

conn = sqlite3.connect("crm.db")

conn.execute("""
CREATE TABLE IF NOT EXISTS configuracoes(
    id INTEGER PRIMARY KEY CHECK (id = 1),
    modo_trabalho TEXT NOT NULL DEFAULT 'ATIVO'
)
""")

existente = conn.execute("SELECT id FROM configuracoes WHERE id=1").fetchone()
if not existente:
    conn.execute("INSERT INTO configuracoes (id, modo_trabalho) VALUES (1, 'ATIVO')")

conn.commit()
conn.close()
print("Migração de modo de trabalho concluída.")