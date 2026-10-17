"""
Recalcula o segmento (Célula Local x Regional) de TODAS as quebras já
importadas com a regra de data -- pedido do Bruno, 2026-10-16.

Regra nova (a mesma do app.py, classificar_segmento_quebra): uma OS de
quebra só é "CELULA LOCAL" se o contrato+cidade foi agendado por nós E a
data da quebra (dat_agenda) é no dia ou depois do nosso PRIMEIRO
agendamento daquele contrato. Quebra anterior é legado de outro
atendimento/canal -> "REGIONAL".

A importação diária já aplica a regra nova daqui pra frente (e reimportar o
mês corrente corrige o mês corrente sozinho). Este script corrige também os
meses que não são mais reimportados.

Canal de venda: linha que deixa de ser Célula Local volta pro canal
original (coluna canal_venda_original). Linhas antigas, importadas antes de
essa coluna existir, não têm o canal original guardado -- ficam como
"CANAL NAO IDENTIFICADO" (a contagem aparece na simulação).

Uso (no console, dentro de ~/CRM_RSI, DEPOIS do git pull e ANTES do Reload):
    python3.10 migrar_quebra_segmento_por_data.py            # só simula
    python3.10 migrar_quebra_segmento_por_data.py --aplicar  # grava
Idempotente: rodar de novo não muda nada.
"""
import os
import sqlite3
import sys
import unicodedata

PASTA = os.path.dirname(os.path.abspath(__file__))
CANAL_SEM_ORIGEM = "CANAL NAO IDENTIFICADO"


def normalizar_cidade(cidade):
    if not cidade:
        return ""
    sem_acento = unicodedata.normalize("NFKD", str(cidade)).encode("ascii", "ignore").decode("ascii")
    return sem_acento.strip().upper()


def chave(cidade, contrato):
    return f"{normalizar_cidade(cidade)}|{(contrato or '').strip()}"


def main():
    aplicar = "--aplicar" in sys.argv
    conn = sqlite3.connect(os.path.join(PASTA, "crm.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")

    colunas = [r[1] for r in conn.execute("PRAGMA table_info(quebras_agenda)")]
    if "canal_venda_original" not in colunas:
        conn.execute("ALTER TABLE quebras_agenda ADD COLUMN canal_venda_original TEXT")

    primeiro = {}
    for l in conn.execute("""
        SELECT m.cidade, a.contrato, MIN(date(a.data_fim)) AS dia
        FROM atividades a JOIN mailing m ON m.contrato = a.contrato
        WHERE a.tabulacao = 'Agendado'
        GROUP BY m.cidade, a.contrato
    """):
        k = chave(l["cidade"], l["contrato"])
        if k not in primeiro or (l["dia"] and (primeiro[k] is None or l["dia"] < primeiro[k])):
            primeiro[k] = l["dia"]

    mudancas = []
    for q in conn.execute("SELECT id, contrato, cidade, dat_agenda, segmento, canal_venda, canal_venda_original, tipo_tratamento FROM quebras_agenda"):
        k = chave(q["cidade"], q["contrato"])
        if k in primeiro and not (q["dat_agenda"] and primeiro[k] and q["dat_agenda"] < primeiro[k]):
            novo = "CELULA LOCAL"
        else:
            novo = "REGIONAL"
        if novo == q["segmento"]:
            continue
        if novo == "CELULA LOCAL":
            canal = "CELULA LOCAL"
            original = q["canal_venda_original"] or q["canal_venda"]
        else:
            original = q["canal_venda_original"]
            canal = original or CANAL_SEM_ORIGEM
        mudancas.append((q, novo, canal, original))

    resumo = {}
    sem_origem = 0
    for q, novo, canal, _ in mudancas:
        k = (q["segmento"], novo, (q["dat_agenda"] or "")[:7], q["tipo_tratamento"])
        resumo[k] = resumo.get(k, 0) + 1
        if canal == CANAL_SEM_ORIGEM:
            sem_origem += 1

    print(f"Quebras que mudam de segmento: {len(mudancas)}")
    for (de, para, mes, tipo), n in sorted(resumo.items(), key=lambda x: (x[0][2], x[0][0], x[0][3])):
        print(f"  {mes}  {de:12} -> {para:12}  {tipo:22} {n}")
    if sem_origem:
        print(f"  ({sem_origem} sem canal original guardado -> '{CANAL_SEM_ORIGEM}')")

    if not aplicar:
        print("\nSIMULAÇÃO -- nada foi gravado. Rode com --aplicar para gravar.")
        conn.close()
        return

    for q, novo, canal, original in mudancas:
        conn.execute(
            "UPDATE quebras_agenda SET segmento=?, canal_venda=?, canal_venda_original=? WHERE id=?",
            (novo, canal, original, q["id"]),
        )
    conn.commit()
    conn.close()
    print(f"\nAPLICADO: {len(mudancas)} quebra(s) atualizada(s).")


if __name__ == "__main__":
    main()
