"""
Migração aditiva: meta mensal de agendados, definida pelo ADM, usada nos
gráficos de velocímetro do Dashboard.
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]
if "meta_agendados_mes" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN meta_agendados_mes INTEGER DEFAULT 100")
conn.commit()
conn.close()
print("Migração da meta de agendados concluída.")
