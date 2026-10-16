"""
Cria o banco crm.db do zero, já com todas as tabelas do projeto (mailing,
atividades, followup, checklist_improdutivo, usuarios, escalas, pausas, sessoes).

Use isso só para uma instalação NOVA. Se você já tem um crm.db com dados,
use os scripts de migração (migrar_escala_pausas.py, migrar_endereco.py)
em vez de rodar este arquivo de novo.
"""
import sqlite3
from werkzeug.security import generate_password_hash

conn = sqlite3.connect("crm.db")

conn.execute("""
CREATE TABLE usuarios(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT,
    login TEXT,
    senha TEXT,
    email TEXT,
    perfil TEXT,
    ativo INTEGER,
    precisa_trocar_senha INTEGER DEFAULT 0
)
""")

conn.execute("""
CREATE TABLE mailing(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contrato TEXT,
    cliente TEXT,
    cidade TEXT,
    node TEXT,
    endereco TEXT,
    complemento TEXT,
    telefone_celular TEXT,
    telefone_residencial TEXT,
    telefone_comercial TEXT,
    email TEXT,
    codigo_imovel TEXT,
    produto_tv TEXT,
    valor_tv REAL,
    valor_ponto_adicional REAL,
    produto_virtua TEXT,
    valor_virtua REAL,
    produto_voip TEXT,
    valor_voip REAL,
    valor_total REAL,
    status TEXT,
    tentativa INTEGER,
    operador TEXT,
    ultimo_operador TEXT,
    reservado_para TEXT,
    prioridade INTEGER DEFAULT 0,
    data_distribuicao TEXT,
    data_expiracao TEXT
)
""")

conn.execute("""
CREATE TABLE atividades(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contrato TEXT,
    operador TEXT,
    tentativa INTEGER,
    tabulacao TEXT,
    canal_contato TEXT,
    observacao TEXT,
    data_inicio TEXT,
    data_fim TEXT,
    status TEXT,
    data_agendamento TEXT,
    janela_agendamento TEXT,
    resp_cliente TEXT,
    flag_mdu INTEGER
)
""")

conn.execute("""
CREATE TABLE followup(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contrato TEXT,
    operador TEXT,
    data_retorno TEXT,
    observacao TEXT,
    status TEXT
)
""")

conn.execute("""
CREATE TABLE checklist_improdutivo(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_atividade INTEGER,
    celular INTEGER,
    residencial INTEGER,
    comercial INTEGER,
    email INTEGER
)
""")

conn.execute("""
CREATE TABLE escalas(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    dias_trabalho TEXT,
    hora_inicio TEXT,
    hora_fim TEXT
)
""")

conn.execute("""
CREATE TABLE pausas(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    tipo TEXT,
    data TEXT,
    inicio TEXT,
    fim_previsto TEXT,
    fim_real TEXT,
    automatica INTEGER
)
""")

conn.execute("""
CREATE TABLE sessoes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    login_em TEXT,
    logout_em TEXT
)
""")

conn.execute("""
CREATE TABLE transferencias(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contrato TEXT,
    solicitante TEXT,
    dono_atual TEXT,
    destino TEXT,
    justificativa TEXT,
    status TEXT,
    data_solicitacao TEXT,
    respondido_por TEXT,
    data_resposta TEXT
)
""")

conn.execute("""
INSERT INTO usuarios(nome, login, senha, email, perfil, ativo)
VALUES('BRUNO','bruno',?,'bruno@empresa.com.br','SUPERVISOR',1)
""", (generate_password_hash("123"),))

conn.execute("""
INSERT INTO escalas (usuario_id, dias_trabalho, hora_inicio, hora_fim)
VALUES (1, 'SEG,TER,QUA,QUI,SEX', '08:00', '17:48')
""")

for nome, tabela, colunas in [
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
]:
    conn.execute(f"CREATE INDEX IF NOT EXISTS {nome} ON {tabela} ({colunas})")

conn.commit()
conn.close()
print("Banco criado com sucesso.")