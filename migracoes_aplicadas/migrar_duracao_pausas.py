"""
Adiciona à tabela configuracoes as colunas de duração (em minutos) da pausa
curta (manhã/tarde) e do almoço, hoje fixas no código (10min e 60min).
Rode uma vez: python migrar_duracao_pausas.py
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [linha[1] for linha in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]

if "duracao_pausa_min" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN duracao_pausa_min INTEGER DEFAULT 10")
    print("Coluna duracao_pausa_min criada.")
else:
    print("Coluna duracao_pausa_min já existe.")

if "duracao_almoco_min" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN duracao_almoco_min INTEGER DEFAULT 60")
    print("Coluna duracao_almoco_min criada.")
else:
    print("Coluna duracao_almoco_min já existe.")

# Garante que a linha id=1 tenha valores preenchidos (não fica NULL em bancos já existentes)
conn.execute("""
    UPDATE configuracoes
    SET duracao_pausa_min = COALESCE(duracao_pausa_min, 10),
        duracao_almoco_min = COALESCE(duracao_almoco_min, 60)
    WHERE id = 1
""")

conn.commit()
conn.close()
print("Migração concluída.")
