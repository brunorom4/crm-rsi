"""
Migração aditiva: cria as tabelas de escala/pausas/sessões no crm.db existente,
sem apagar nada do que já foi criado pelo criar_banco.py.
Rode uma única vez: python migrar_escala_pausas.py
"""
import sqlite3

conn = sqlite3.connect("crm.db")

conn.execute("""
CREATE TABLE IF NOT EXISTS escalas(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    dias_trabalho TEXT,   -- ex: "SEG,TER,QUA,QUI,SEX"
    hora_inicio TEXT,     -- "08:00"
    hora_fim TEXT         -- "17:48"
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS pausas(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    tipo TEXT,            -- PAUSA_MANHA / ALMOCO / PAUSA_TARDE
    data TEXT,            -- "AAAA-MM-DD", pra filtrar o dia
    inicio TEXT,
    fim_previsto TEXT,
    fim_real TEXT,
    automatica INTEGER    -- 1 = inserida automaticamente pelo sistema, 0 = manual
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS sessoes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    login_em TEXT,
    logout_em TEXT
)
""")

# Escala padrão de exemplo para o usuário de teste (BRUNO, id 1): Seg-Sex 08:00-17:48
existe = conn.execute("SELECT COUNT(*) FROM escalas WHERE usuario_id=1").fetchone()[0]
if not existe:
    conn.execute("""
        INSERT INTO escalas (usuario_id, dias_trabalho, hora_inicio, hora_fim)
        VALUES (1, 'SEG,TER,QUA,QUI,SEX', '08:00', '17:48')
    """)

conn.commit()
conn.close()
print("Tabelas de escala/pausas/sessões criadas com sucesso.")