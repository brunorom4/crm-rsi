"""
Diagnóstico: mostra exatamente o que a fila de distribuição está vendo,
na mesma ordem que o sistema usaria pra escolher o próximo contrato.

Uso: python diagnostico_prioridade.py NOME_DO_OPERADOR
(o nome deve ser igual ao campo "nome" da pessoa, ex: JOANA)
"""
import sqlite3
import sys
from datetime import datetime

operador = sys.argv[1] if len(sys.argv) > 1 else "TESTE"

conn = sqlite3.connect("crm.db")
conn.row_factory = sqlite3.Row
agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

print(f"=== Verificando fila para o operador: {operador} ===\n")

print("--- 1) Followups vencidos (têm prioridade sobre tudo, ignoram a coluna 'prioridade') ---")
fila = conn.execute("""
    SELECT contrato, operador, data_retorno, status FROM followup
    WHERE status = 'PENDENTE' AND data_retorno <= ? AND (operador = ? OR operador IS NULL)
    ORDER BY (CASE WHEN operador = ? THEN 0 ELSE 1 END), data_retorno ASC
""", (agora_str, operador, operador)).fetchall()
if fila:
    for f in fila:
        print(f"  contrato={f['contrato']}  dono_do_followup={f['operador']}  vencido_em={f['data_retorno']}")
    print(f"\n  >>> Se existir QUALQUER linha acima, é ELA que será entregue primeiro, não importa a prioridade.")
else:
    print("  (nenhum — não é isso)")

print("\n--- 2) Top 15 da fila normal (PENDENTE), na ordem real de distribuição ---")
pendentes = conn.execute("""
    SELECT contrato, status, prioridade, reservado_para, ultimo_operador, typeof(prioridade) as tipo
    FROM mailing WHERE status = 'PENDENTE'
      AND (reservado_para IS NULL OR reservado_para = ?)
    ORDER BY
        (CASE WHEN reservado_para = ? THEN 0 WHEN ultimo_operador = ? THEN 1 ELSE 2 END),
        prioridade DESC,
        id
    LIMIT 15
""", (operador, operador, operador)).fetchall()
for p in pendentes:
    print(f"  contrato={p['contrato']:15} prioridade={p['prioridade']} (tipo:{p['tipo']})  reservado_para={p['reservado_para']}  ultimo_operador={p['ultimo_operador']}")

print("\n--- 3) Prioridades mais altas que existem na base toda (independente de reserva) ---")
top = conn.execute("""
    SELECT contrato, status, prioridade, reservado_para, ultimo_operador FROM mailing
    ORDER BY prioridade DESC LIMIT 10
""").fetchall()
for t in top:
    print(f"  contrato={t['contrato']:15} status={t['status']:12} prioridade={t['prioridade']}  reservado_para={t['reservado_para']}  ultimo_operador={t['ultimo_operador']}")

conn.close()