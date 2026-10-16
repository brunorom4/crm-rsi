"""
Migração aditiva: troca a autenticação de e-mail de SMTP usuário+senha (que a
Microsoft desligou por padrão em outubro/2022) pra Microsoft Graph API com
credencial de aplicativo (o jeito atual recomendado).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]

novas_colunas = {
    "email_tenant_id": "TEXT",
    "email_client_id": "TEXT",
    "email_client_secret": "TEXT",
}
for coluna, tipo in novas_colunas.items():
    if coluna not in colunas:
        conn.execute(f"ALTER TABLE configuracoes ADD COLUMN {coluna} {tipo}")

conn.commit()
conn.close()
print("Migração pra autenticação via Microsoft Graph concluída.")
