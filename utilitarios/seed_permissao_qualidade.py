"""
Adiciona o módulo 'qualidade' na tela de Permissões, já ligado por padrão pra
todos os perfis configuráveis -- assim o comportamento de hoje (todo mundo vê)
não muda até o ADM decidir restringir alguém.
"""
import sqlite3

conn = sqlite3.connect("crm.db")
for perfil in ("OPERADOR", "SUPERVISOR", "ANALISTA", "ANALISTA_II"):
    conn.execute("""
        INSERT INTO permissoes (perfil, modulo, permitido) VALUES (?, 'qualidade', 1)
        ON CONFLICT(perfil, modulo) DO NOTHING
    """, (perfil,))
conn.commit()
conn.close()
print("Módulo Qualidade adicionado à tela de Permissões (ligado por padrão).")
