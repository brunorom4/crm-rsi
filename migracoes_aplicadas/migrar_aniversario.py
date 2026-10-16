"""
Migração aditiva: data de nascimento + postagem automática de aniversário em
Informações (com opção de a pessoa não querer ser divulgada).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(usuarios)").fetchall()]

novas_colunas = {
    "data_nascimento": "TEXT",
    "divulgar_aniversario": "INTEGER DEFAULT 1",
    "ultimo_aniversario_postado_ano": "TEXT",
}
for coluna, tipo in novas_colunas.items():
    if coluna not in colunas:
        conn.execute(f"ALTER TABLE usuarios ADD COLUMN {coluna} {tipo}")

conn.commit()
conn.close()
print("Migração do aniversário automático concluída.")
