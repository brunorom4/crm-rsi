"""
Migração aditiva: adiciona a coluna ultimo_ip em usuarios, atualizada
automaticamente a cada login bem-sucedido.
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(usuarios)").fetchall()]
if "ultimo_ip" not in colunas:
    conn.execute("ALTER TABLE usuarios ADD COLUMN ultimo_ip TEXT")
conn.commit()
conn.close()
print("Migração de último IP concluída.")