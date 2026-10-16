"""
Migração aditiva: guarda o snapshot dos indicadores comparativos (conversão,
tempo médio, pausas excedidas, faltas, atrasos, saídas antecipadas + média
geral) junto com a monitoria, no momento em que ela é criada -- assim tanto o
operador quanto Supervisor/ADM veem exatamente os números que embasaram aquele
feedback, mesmo que os números "ao vivo" mudem depois.
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(monitorias)").fetchall()]
if "metricas_json" not in colunas:
    conn.execute("ALTER TABLE monitorias ADD COLUMN metricas_json TEXT")
if "media_geral_json" not in colunas:
    conn.execute("ALTER TABLE monitorias ADD COLUMN media_geral_json TEXT")
conn.commit()
conn.close()
print("Migração dos indicadores comparativos da monitoria concluída.")
