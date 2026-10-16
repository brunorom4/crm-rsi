"""
Migração aditiva: cria índices nas colunas mais consultadas pelo sistema.
Não muda nenhum dado, só acelera as buscas (fica mais importante conforme
a base de contratos/histórico cresce). Seguro rodar quantas vezes quiser.
"""
import sqlite3

conn = sqlite3.connect("crm.db")

indices = [
    ("idx_mailing_contrato", "mailing", "contrato"),
    ("idx_mailing_status", "mailing", "status"),
    ("idx_mailing_operador", "mailing", "operador"),
    ("idx_mailing_reservado_para", "mailing", "reservado_para"),
    ("idx_mailing_ultimo_operador", "mailing", "ultimo_operador"),
    ("idx_atividades_contrato", "atividades", "contrato"),
    ("idx_atividades_operador", "atividades", "operador"),
    ("idx_followup_contrato", "followup", "contrato"),
    ("idx_followup_status_retorno", "followup", "status, data_retorno"),
    ("idx_usuarios_login", "usuarios", "login"),
    ("idx_sessoes_usuario", "sessoes", "usuario_id"),
    ("idx_pausas_usuario_data", "pausas", "usuario_id, data"),
]

for nome, tabela, colunas in indices:
    conn.execute(f"CREATE INDEX IF NOT EXISTS {nome} ON {tabela} ({colunas})")

conn.commit()
conn.close()
print(f"{len(indices)} índices criados/confirmados com sucesso.")