import sqlite3
import sys

operador = sys.argv[1] if len(sys.argv) > 1 else "TESTE"

conn = sqlite3.connect("crm.db")
conn.row_factory = sqlite3.Row

print(f"=== Contrato(s) EM_ANDAMENTO com o operador: {operador} ===\n")
rows = conn.execute("""
    SELECT contrato, prioridade, status, data_distribuicao, data_expiracao
    FROM mailing WHERE status='EM_ANDAMENTO' AND operador LIKE ?
""", (f"%{operador}%",)).fetchall()

if not rows:
    print("  (nenhum contrato em andamento com esse nome)")
else:
    for r in rows:
        print(f"  contrato={r['contrato']}  prioridade={r['prioridade']}  distribuído_em={r['data_distribuicao']}  expira_em={r['data_expiracao']}")

conn.close()