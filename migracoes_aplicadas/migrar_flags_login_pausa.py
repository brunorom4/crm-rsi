"""
Migração aditiva: dois flags novos que o ADM pode ligar/desligar em
Tratamento / Pausas:
- bloqueio_login_ativo: impede login fora do horário/dia da escala (tolerância 5 min antes)
- pausa_automatica_ativa: liga/desliga a inserção automática de pausa obrigatória
"""
import sqlite3

conn = sqlite3.connect("crm.db")
colunas = [c[1] for c in conn.execute("PRAGMA table_info(configuracoes)").fetchall()]
if "bloqueio_login_ativo" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN bloqueio_login_ativo INTEGER DEFAULT 1")
if "pausa_automatica_ativa" not in colunas:
    conn.execute("ALTER TABLE configuracoes ADD COLUMN pausa_automatica_ativa INTEGER DEFAULT 1")
conn.execute("UPDATE configuracoes SET bloqueio_login_ativo=1 WHERE bloqueio_login_ativo IS NULL")
conn.execute("UPDATE configuracoes SET pausa_automatica_ativa=1 WHERE pausa_automatica_ativa IS NULL")
conn.commit()
conn.close()
print("Migração dos flags de bloqueio de login e pausa automática concluída.")
