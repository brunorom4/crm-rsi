"""
Migração aditiva pra Fase 5 (relatório de agendados por data):
adiciona colunas estruturadas em atividades pra guardar data/período do
agendamento separadamente do texto livre da observação (que continua existindo,
mas agora tem colunas próprias pra filtrar/relatar).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(atividades)").fetchall()]

for coluna in ["data_agendamento", "janela_agendamento", "resp_cliente"]:
    if coluna not in colunas:
        conn.execute(f"ALTER TABLE atividades ADD COLUMN {coluna} TEXT")

if "flag_mdu" not in colunas:
    conn.execute("ALTER TABLE atividades ADD COLUMN flag_mdu INTEGER")

conn.commit()
conn.close()
print("Migração da Fase 5 concluída.")