"""Migração aditiva: adiciona endereco/complemento na mailing, sem apagar dados existentes."""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(mailing)").fetchall()]

if "endereco" not in colunas:
    conn.execute("ALTER TABLE mailing ADD COLUMN endereco TEXT")
if "complemento" not in colunas:
    conn.execute("ALTER TABLE mailing ADD COLUMN complemento TEXT")

conn.commit()
conn.close()
print("Colunas de endereço adicionadas com sucesso.")