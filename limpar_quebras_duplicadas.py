"""
Limpeza única: remove duplicatas já existentes em quebras_agenda (mesmo
contrato + mesma data, código de OS diferente). Daqui pra frente isso é
feito automaticamente a cada importação -- esse script é só pra arrumar o
que já estava no banco antes dessa correção (pedido do Bruno, 2026-09-08).
"""
import sqlite3

conn = sqlite3.connect("crm.db")
conn.row_factory = sqlite3.Row

duplicados = conn.execute("""
    SELECT contrato, dat_agenda FROM quebras_agenda
    WHERE contrato IS NOT NULL AND contrato != '' AND dat_agenda IS NOT NULL
    GROUP BY contrato, dat_agenda HAVING COUNT(*) > 1
""").fetchall()

total_removidas = 0
for grupo in duplicados:
    linhas = conn.execute(
        "SELECT id, cod_os FROM quebras_agenda WHERE contrato=? AND dat_agenda=? ORDER BY cod_os DESC",
        (grupo["contrato"], grupo["dat_agenda"])
    ).fetchall()
    id_manter = linhas[0]["id"]
    print(f"Contrato {grupo['contrato']} ({grupo['dat_agenda']}): mantendo OS {linhas[0]['cod_os']}, "
          f"removendo {', '.join(str(l['cod_os']) for l in linhas[1:])}")
    for linha_remover in linhas[1:]:
        id_remover = linha_remover["id"]
        conn.execute("UPDATE quebras_tratamento SET quebra_id=? WHERE quebra_id=?", (id_manter, id_remover))
        conn.execute("DELETE FROM quebras_agenda WHERE id=?", (id_remover,))
        total_removidas += 1

conn.commit()
conn.close()
print(f"\n{total_removidas} linha(s) duplicada(s) removida(s).")
