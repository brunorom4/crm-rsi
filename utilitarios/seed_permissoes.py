"""
Cria a tabela permissoes e popula com o comportamento ATUAL do sistema
(o que cada perfil já pode acessar hoje), pra nada mudar até o ADM
começar a editar pela tela nova.

Rode uma vez: python seed_permissoes.py
"""
import sqlite3

MODULOS = [
    ("dashboard", "Dashboard"),
    ("atendimento", "Atendimento"),
    ("buscar_contrato", "Buscar Contrato"),
    ("agendados_data", "Agendados por Data"),
    ("nova_tarefa", "Nova Tarefa"),
    ("pausas", "Tratamento / Pausas"),
    ("supervisao", "Supervisão"),
    ("escalas", "Escalas"),
    ("transferencias", "Transferências"),
    ("modo_trabalho", "Modo de Trabalho"),
    ("extracao", "Extração"),
]

# Perfil ADM não entra aqui de propósito: ele sempre tem acesso a tudo,
# isso é garantido no código (tem_permissao), não no banco.
PADRAO_ATUAL = {
    "OPERADOR": {
        "dashboard", "atendimento", "buscar_contrato", "nova_tarefa", "pausas",
    },
    "SUPERVISOR": {
        "dashboard", "atendimento", "buscar_contrato", "agendados_data",
        "nova_tarefa", "supervisao", "escalas", "transferencias",
    },
    "ANALISTA": {
        "dashboard", "buscar_contrato", "agendados_data", "nova_tarefa",
    },
    "ANALISTA_II": {
        "dashboard", "buscar_contrato", "agendados_data", "nova_tarefa",
    },
}

conn = sqlite3.connect("crm.db")
conn.execute("""
    CREATE TABLE IF NOT EXISTS permissoes (
        perfil TEXT NOT NULL,
        modulo TEXT NOT NULL,
        permitido INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (perfil, modulo)
    )
""")