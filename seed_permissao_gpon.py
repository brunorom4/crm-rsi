"""
Libera a permissão do módulo 'tratamento_gpon' só pro perfil ANALISTA
(ADM já tem acesso automático a tudo, e Analista II / Operador / Supervisor
ficam de fora por padrão, como pedido).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
conn.execute("""
    INSERT INTO permissoes (perfil, modulo, permitido) VALUES ('ANALISTA', 'tratamento_gpon', 1)
    ON CONFLICT(perfil, modulo) DO UPDATE SET permitido=1
""")
conn.commit()
conn.close()
print("Permissão de Tratamento GPON liberada para o perfil ANALISTA.")
