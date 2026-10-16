"""
Migração aditiva: página de Informações (notícias/postagens), com rastreio de
quem já leu cada uma.
"""
import sqlite3

conn = sqlite3.connect("crm.db")

conn.execute("""
CREATE TABLE IF NOT EXISTS noticias(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo TEXT NOT NULL,
    conteudo_html TEXT NOT NULL,
    autor TEXT NOT NULL,
    data_publicacao TEXT NOT NULL,
    ativo INTEGER NOT NULL DEFAULT 1
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS noticias_leituras(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    noticia_id INTEGER NOT NULL,
    usuario_id INTEGER NOT NULL,
    data_leitura TEXT NOT NULL,
    UNIQUE(noticia_id, usuario_id)
)
""")

conn.commit()
conn.close()
print("Migração de Notícias/Informações concluída.")
