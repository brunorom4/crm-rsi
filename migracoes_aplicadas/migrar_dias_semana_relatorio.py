"""
Migração aditiva: dias da semana em que o relatório automático deve disparar
(por padrão, Seg-Sex -- sábado e domingo desligados).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]
if "email_dias_semana" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN email_dias_semana TEXT DEFAULT 'SEG,TER,QUA,QUI,SEX'")
    conn.execute("""
        UPDATE configuracoes SET email_dias_semana = 'SEG,TER,QUA,QUI,SEX'
        WHERE id=1 AND (email_dias_semana IS NULL OR email_dias_semana = '')
    """)
conn.commit()
conn.close()
print("Migração dos dias da semana do relatório concluída.")
