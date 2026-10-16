"""
Migração aditiva: tabela de releases do sistema, que passa a ser a fonte
única da versão exibida (rodapé do login + link discreto na sidebar) e do
conteúdo da página /notas-de-versao. Antes era uma lista fixa no código
(fácil de esquecer de atualizar) -- agora o ADM edita direto pela tela.

Semeia com o histórico já definido antes (v1.0 a v1.4), mais os 2 releases
que ficaram de fora por esquecimento (reestruturação da Ajuda, e o ajuste
rápido do relatório de Agendados por Data).
"""
import sqlite3

conn = sqlite3.connect("crm.db")

conn.execute("""
    CREATE TABLE IF NOT EXISTS releases_sistema (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        versao TEXT NOT NULL,
        data TEXT NOT NULL,
        descricao TEXT NOT NULL,
        criado_em TEXT NOT NULL DEFAULT (datetime('now'))
    )
""")

existentes = conn.execute("SELECT COUNT(*) FROM releases_sistema").fetchone()[0]
if existentes == 0:
    releases_iniciais = [
        ("1.0", "24/08/2026", "Preparação do sistema para hospedagem em nuvem"),
        ("1.1", "26/08/2026", "Correção na importação de usuários"),
        ("1.2", "28/08/2026", "Correção de acesso a arquivos após migração para hospedagem em nuvem"),
        ("1.3", "30/08/2026", "Correção de coluna ausente no banco (objetivo mensal de agendados)"),
        ("1.4", "02/09/2026", "Correção do bug de distribuição de contratos duplicados"),
        ("1.5", "03/09/2026", "Reestruturação da página de Ajuda, com controle de acesso por permissão"),
        ("1.6", "04/09/2026", "Contador de contratos e data padrão D+1 no relatório de Agendados por Data"),
    ]
    for versao, data, descricao in releases_iniciais:
        conn.execute(
            "INSERT INTO releases_sistema (versao, data, descricao) VALUES (?, ?, ?)",
            (versao, data, descricao)
        )

conn.commit()
conn.close()
print("Migração da tabela de releases concluída.")
