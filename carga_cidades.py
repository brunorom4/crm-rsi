"""
Carga em lote de cidades padronizadas — só mexe no banco (crm.db),
não precisa reiniciar nem alterar o app.

Como usar:
    python carga_cidades.py cidades.txt
    python carga_cidades.py cidades.xlsx
    python carga_cidades.py cidades.csv

Formatos aceitos:
    - .txt  -> uma cidade por linha
    - .csv / .xlsx -> precisa ter uma coluna chamada "cidade" (ou "Cidade")

Cidades que já existem são reativadas (ativo=1) caso estivessem
desativadas; nomes duplicados no arquivo são ignorados automaticamente.
"""
import sys
import sqlite3

try:
    import pandas as pd
except ImportError:
    pd = None


def ler_nomes(caminho):
    if caminho.lower().endswith(".txt"):
        with open(caminho, encoding="utf-8") as f:
            return [linha.strip() for linha in f if linha.strip()]

    if pd is None:
        raise SystemExit("Pra ler .csv/.xlsx é preciso ter o pandas instalado (pip install pandas).")

    if caminho.lower().endswith(".csv"):
        df = pd.read_csv(caminho)
    elif caminho.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(caminho)
    else:
        raise SystemExit("Formato não suportado. Use .txt, .csv ou .xlsx.")

    coluna = "cidade" if "cidade" in df.columns else "Cidade" if "Cidade" in df.columns else None
    if not coluna:
        raise SystemExit("O arquivo precisa ter uma coluna chamada 'cidade'.")

    return [str(v).strip() for v in df[coluna].dropna().tolist() if str(v).strip()]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Uso: python carga_cidades.py <arquivo.txt|.csv|.xlsx>")

    nomes = ler_nomes(sys.argv[1])
    nomes_unicos = sorted(set(nomes))

    conn = sqlite3.connect("crm.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cidades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE,
            ativo INTEGER NOT NULL DEFAULT 1
        )
    """)

    novas = 0
    reativadas = 0
    for nome in nomes_unicos:
        antes = conn.execute("SELECT ativo FROM cidades WHERE nome=?", (nome,)).fetchone()
        conn.execute("""
            INSERT INTO cidades (nome, ativo) VALUES (?, 1)
            ON CONFLICT(nome) DO UPDATE SET ativo=1
        """, (nome,))
        if antes is None:
            novas += 1
        elif antes[0] == 0:
            reativadas += 1

    conn.commit()
    conn.close()

    print(f"Arquivo lido: {len(nomes)} linhas ({len(nomes_unicos)} nomes únicos).")
    print(f"Cidades novas cadastradas: {novas}")
    print(f"Cidades reativadas (estavam desativadas): {reativadas}")
    print(f"Já ativas sem alteração: {len(nomes_unicos) - novas - reativadas}")


if __name__ == "__main__":
    main()