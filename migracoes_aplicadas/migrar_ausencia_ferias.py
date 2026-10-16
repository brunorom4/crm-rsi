"""
Migração aditiva:
- ausencias: registra motivo/submotivo/observação quando o supervisor marca a falta de alguém
- escalas: adiciona campos de férias (data início/fim), sem mexer no cadastro semanal existente
"""
import sqlite3

conn = sqlite3.connect("crm.db")

conn.execute("""
CREATE TABLE IF NOT EXISTS ausencias(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    data TEXT,              -- data (AAAA-MM-DD) a que a falta se refere
    motivo TEXT,             -- 'Falta Justificada' ou 'Injustificada'
    submotivo TEXT,          -- só preenchido quando motivo = 'Falta Justificada'
    observacao TEXT,
    registrado_por TEXT,
    data_registro TEXT
)
""")

colunas_escalas = [c[1] for c in conn.execute("PRAGMA table_info(escalas)").fetchall()]
if "ferias_inicio" not in colunas_escalas:
    conn.execute("ALTER TABLE escalas ADD COLUMN ferias_inicio TEXT")
if "ferias_fim" not in colunas_escalas:
    conn.execute("ALTER TABLE escalas ADD COLUMN ferias_fim TEXT")

conn.commit()
conn.close()
print("Migração de ausências/férias concluída.")