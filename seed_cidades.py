"""
Cria a tabela cidades, usada pra padronizar o campo Cidade na tela de
Nova Tarefa. Começa vazia — o ADM cadastra as cidades pela tela
/tarefas/cidades.

Rode uma vez: python seed_cidades.py
"""
import sqlite3

conn = sqlite3.connect("crm.db")
conn.execute("""
    CREATE TABLE IF NOT EXISTS cidades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL UNIQUE,
        ativo INTEGER NOT NULL DEFAULT 1
    )
""")
conn.commit()
conn.close()
print("Tabela cidades criada.")