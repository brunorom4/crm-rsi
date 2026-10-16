"""
Migração aditiva: módulo de Monitoria de Qualidade.
"""
import sqlite3

conn = sqlite3.connect("crm.db")

conn.execute("""
CREATE TABLE IF NOT EXISTS monitorias(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operador_id INTEGER NOT NULL,
    supervisor_id INTEGER NOT NULL,
    data_monitoria TEXT NOT NULL,

    q1_abordagem INTEGER,
    q2_agilidade INTEGER,
    q3_cordialidade INTEGER,
    q4_oferta INTEGER,
    q5_fechamento INTEGER,
    q6_contato INTEGER,
    q7_tabulacao INTEGER,

    nota_final REAL NOT NULL,

    itens_pontuados TEXT,
    pontos_fortes TEXT,
    feedback_plano_acao TEXT,
    informacoes_adicionais TEXT,

    aceite_status TEXT NOT NULL DEFAULT 'PENDENTE',
    aceite_observacao TEXT,
    aceite_data TEXT,
    visualizado_pelo_operador INTEGER NOT NULL DEFAULT 0
)
""")

conn.execute("CREATE INDEX IF NOT EXISTS idx_monitorias_operador ON monitorias(operador_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_monitorias_data ON monitorias(data_monitoria)")

conn.commit()
conn.close()
print("Migração de Monitoria de Qualidade concluída.")
