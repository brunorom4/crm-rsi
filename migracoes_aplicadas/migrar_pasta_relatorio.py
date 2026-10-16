"""
Migração aditiva: simplifica o relatório pra escrever um arquivo numa pasta
sincronizada do OneDrive, em vez de mandar e-mail direto (que esbarrou em
bloqueio de rede e depois em autenticação da Microsoft). O disparo do e-mail
de verdade passa a ser responsabilidade de um fluxo do Power Automate, de
fora da rede da empresa.
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]
if "email_pasta_destino" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN email_pasta_destino TEXT")
conn.commit()
conn.close()
print("Migração da pasta de destino do relatório concluída.")
