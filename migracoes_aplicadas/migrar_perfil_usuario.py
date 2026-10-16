"""
Migração aditiva: adiciona ramal, celular e foto de perfil ao cadastro do usuário.
"""
import sqlite3
import os

conn = sqlite3.connect("crm.db")

colunas = [c[1] for c in conn.execute("PRAGMA table_info(usuarios)").fetchall()]
if "ramal" not in colunas:
    conn.execute("ALTER TABLE usuarios ADD COLUMN ramal TEXT")
if "celular" not in colunas:
    conn.execute("ALTER TABLE usuarios ADD COLUMN celular TEXT")
if "foto" not in colunas:
    conn.execute("ALTER TABLE usuarios ADD COLUMN foto TEXT")

conn.commit()
conn.close()

os.makedirs("static/fotos_perfil", exist_ok=True)
print("Migração de ramal/celular/foto de perfil concluída.")