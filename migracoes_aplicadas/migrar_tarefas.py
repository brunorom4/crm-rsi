"""
Migração aditiva pra Fase 3 (criação manual de tarefa + transferência com aprovação):
- mailing: adiciona reservado_para (reserva exclusiva de um contrato pendente pra um operador específico)
- transferencias: fila de solicitações de transferência de contrato já com dono, aguardando aprovação do supervisor
"""
import sqlite3

conn = sqlite3.connect("crm.db")

colunas_mailing = [c[1] for c in conn.execute("PRAGMA table_info(mailing)").fetchall()]
if "reservado_para" not in colunas_mailing:
    conn.execute("ALTER TABLE mailing ADD COLUMN reservado_para TEXT")

conn.execute("""
CREATE TABLE IF NOT EXISTS transferencias(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contrato TEXT,
    solicitante TEXT,
    dono_atual TEXT,
    destino TEXT,
    justificativa TEXT,
    status TEXT,              -- PENDENTE / APROVADO / REJEITADO
    data_solicitacao TEXT,
    respondido_por TEXT,
    data_resposta TEXT
)
""")

conn.commit()
conn.close()
print("Migração da Fase 3 concluída.")