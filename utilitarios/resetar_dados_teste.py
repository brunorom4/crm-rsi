"""
Limpa os dados de teste (contratos, histórico de atendimento, pausas, sessões,
transferências, ausências) SEM mexer em usuários, escalas ou configurações
(modo de trabalho). Ideal pra sair do ambiente de teste e começar a operação
de verdade com a base de contratos real.

Rode uma vez, confirme quando pedir, e depois importe o mailing real
(python importar_mailing.py).
"""
import sqlite3

TABELAS_PARA_LIMPAR = [
    "mailing",
    "atividades",
    "followup",
    "transferencias",
    "checklist_improdutivo",
    "pausas",
    "sessoes",
    "ausencias",
]

conn = sqlite3.connect("crm.db")

print("As tabelas abaixo serão ESVAZIADAS (usuários e escalas NÃO são afetados):")
for t in TABELAS_PARA_LIMPAR:
    qtd = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  - {t}: {qtd} registro(s)")

resposta = input("\nConfirma a limpeza? Essa ação não pode ser desfeita. Digite SIM para continuar: ")

if resposta.strip().upper() != "SIM":
    print("Cancelado. Nada foi apagado.")
else:
    for t in TABELAS_PARA_LIMPAR:
        conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (t,))
    conn.commit()
    print("\nPronto. Base de movimento limpa — usuários, escalas e modo de trabalho continuam intactos.")
    print("Próximo passo: rode 'python importar_mailing.py' com a base real em dados\\Front.xlsx.")

conn.close()