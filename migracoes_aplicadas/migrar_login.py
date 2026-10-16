"""
Migração aditiva para a Fase 1 (login + trava de contrato):
- usuarios: adiciona email, e converte senha em texto puro para hash
- mailing: adiciona ultimo_operador (pra dar preferência ao mesmo operador
  quando um contrato expira por 24h de inatividade e volta pra fila)
"""
import sqlite3
from werkzeug.security import generate_password_hash

conn = sqlite3.connect("crm.db")

colunas_usuarios = [c[1] for c in conn.execute("PRAGMA table_info(usuarios)").fetchall()]
if "email" not in colunas_usuarios:
    conn.execute("ALTER TABLE usuarios ADD COLUMN email TEXT")

colunas_mailing = [c[1] for c in conn.execute("PRAGMA table_info(mailing)").fetchall()]
if "ultimo_operador" not in colunas_mailing:
    conn.execute("ALTER TABLE mailing ADD COLUMN ultimo_operador TEXT")

# Converte senhas em texto puro para hash (só re-hasheia o que ainda não é hash)
usuarios = conn.execute("SELECT id, senha FROM usuarios").fetchall()
for uid, senha in usuarios:
    if senha and not senha.startswith("pbkdf2:") and not senha.startswith("scrypt:"):
        conn.execute("UPDATE usuarios SET senha=? WHERE id=?", (generate_password_hash(senha), uid))

conn.commit()
conn.close()
print("Migração de login/trava de contrato concluída.")