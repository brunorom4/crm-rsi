"""
Promove o CÓDIGO testado no espelho para a produção -- pedido do Bruno,
2026-09-25. Fica em /home/crmrsi/promover_espelho.py e roda pelo console
Bash do PythonAnywhere.

COMO USAR

  1) Ver o que mudou (não copia nada):
         python3.11 /home/crmrsi/promover_espelho.py

  2) Copiar de verdade:
         python3.11 /home/crmrsi/promover_espelho.py --confirmar
     Depois: aba Web -> Reload.

  3) Desfazer a última promoção (volta os arquivos da produção como estavam):
         python3.11 /home/crmrsi/promover_espelho.py --desfazer
     Depois: aba Web -> Reload.

O QUE É COPIADO

  Só código: .py, templates/, static/ (menos static/fotos_perfil/),
  requirements.txt etc. NUNCA copia bancos (.db), uploads/, backups/,
  dados/, painel_dados/, painel_cotas_dados/ nem o ESPELHO.flag -- os dados
  da produção continuam sendo os da produção.

  Arquivos que existem na produção mas não no espelho NÃO são apagados (só
  aparecem listados, pra você decidir).

  Antes de sobrescrever, guarda as versões atuais da produção em
  CRM_RSI/backups/antes_da_promocao_AAAAMMDD_HHMMSS.zip (é isso que o
  --desfazer usa). Mesmo assim, clique em "Backup Completo" antes -- é o
  que garante os bancos.
"""
import filecmp
import os
import shutil
import sys
import zipfile
from datetime import datetime

PASTA_PRODUCAO = "/home/crmrsi/CRM_RSI"
PASTA_ESPELHO = "/home/crmrsi/CRM_RSI_ESPELHO"
PASTA_BACKUPS = os.path.join(PASTA_PRODUCAO, "backups")
PREFIXO_ZIP = "antes_da_promocao_"

PASTAS_IGNORADAS = {
    "__pycache__", ".git", "backups", "uploads", "dados", "painel_dados",
    "painel_cotas_dados", "flask_session", os.path.join("static", "fotos_perfil"),
}
ARQUIVOS_IGNORADOS = {"ESPELHO.flag"}
SUFIXOS_IGNORADOS = (".db", ".db-wal", ".db-shm", ".db-journal", ".pyc", ".log")


def arquivos_de_codigo(pasta):
    encontrados = set()
    for raiz, pastas, arquivos in os.walk(pasta):
        rel_raiz = os.path.relpath(raiz, pasta)
        pastas[:] = [p for p in pastas
                     if p not in PASTAS_IGNORADAS
                     and os.path.normpath(os.path.join(rel_raiz, p)) not in PASTAS_IGNORADAS]
        for nome in arquivos:
            if nome in ARQUIVOS_IGNORADOS or nome.endswith(SUFIXOS_IGNORADOS):
                continue
            encontrados.add(os.path.normpath(os.path.join(rel_raiz, nome)))
    return encontrados


def comparar():
    no_espelho = arquivos_de_codigo(PASTA_ESPELHO)
    na_producao = arquivos_de_codigo(PASTA_PRODUCAO)
    novos = sorted(no_espelho - na_producao)
    alterados = sorted(
        f for f in no_espelho & na_producao
        if not filecmp.cmp(os.path.join(PASTA_ESPELHO, f), os.path.join(PASTA_PRODUCAO, f), shallow=False)
    )
    so_na_producao = sorted(na_producao - no_espelho)
    return novos, alterados, so_na_producao


def validar_sintaxe(arquivos):
    import ast
    erros = []
    for f in arquivos:
        if f.endswith(".py"):
            try:
                with open(os.path.join(PASTA_ESPELHO, f), encoding="utf-8") as arq:
                    ast.parse(arq.read(), filename=f)
            except SyntaxError as erro:
                erros.append(f"{f}: linha {erro.lineno}: {erro.msg}")
    return erros


def promover(confirmar):
    if not os.path.exists(os.path.join(PASTA_ESPELHO, "ESPELHO.flag")):
        sys.exit(f"ERRO: {PASTA_ESPELHO} não tem ESPELHO.flag -- não parece ser o espelho.")
    novos, alterados, so_na_producao = comparar()

    print(f"Espelho  -> {PASTA_ESPELHO}\nProdução -> {PASTA_PRODUCAO}\n")
    print(f"Alterados ({len(alterados)}):")
    for f in alterados:
        print(f"   ~ {f}")
    print(f"Novos ({len(novos)}):")
    for f in novos:
        print(f"   + {f}")
    if so_na_producao:
        print(f"Só existem na produção -- NÃO serão apagados ({len(so_na_producao)}):")
        for f in so_na_producao:
            print(f"   ? {f}")

    if not novos and not alterados:
        print("\nNada para promover: o código do espelho é igual ao da produção.")
        return
    erros = validar_sintaxe(novos + alterados)
    if erros:
        print("\nERRO DE SINTAXE -- nada foi copiado:")
        for e in erros:
            print("   " + e)
        sys.exit(1)
    if not confirmar:
        print("\nNada foi copiado. Para copiar: python3.11 /home/crmrsi/promover_espelho.py --confirmar")
        return

    os.makedirs(PASTA_BACKUPS, exist_ok=True)
    zip_path = os.path.join(PASTA_BACKUPS, f"{PREFIXO_ZIP}{datetime.now():%Y%m%d_%H%M%S}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in alterados:
            z.write(os.path.join(PASTA_PRODUCAO, f), f)
        # lista dos arquivos novos, pro --desfazer saber o que remover
        z.writestr("_arquivos_novos.txt", "\n".join(novos))

    for f in alterados + novos:
        destino = os.path.join(PASTA_PRODUCAO, f)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        shutil.copy2(os.path.join(PASTA_ESPELHO, f), destino)

    print(f"\n{len(alterados) + len(novos)} arquivo(s) copiado(s) para a produção.")
    print(f"Versões anteriores guardadas em: {zip_path}")
    print("Agora: aba Web -> Reload.")


def desfazer():
    zips = sorted(f for f in os.listdir(PASTA_BACKUPS)
                  if f.startswith(PREFIXO_ZIP) and f.endswith(".zip")) if os.path.isdir(PASTA_BACKUPS) else []
    if not zips:
        sys.exit("Nenhuma promoção anterior encontrada para desfazer.")
    zip_path = os.path.join(PASTA_BACKUPS, zips[-1])
    with zipfile.ZipFile(zip_path) as z:
        novos = [n for n in z.read("_arquivos_novos.txt").decode("utf-8").splitlines() if n]
        restaurados = [n for n in z.namelist() if n != "_arquivos_novos.txt"]
        for nome in restaurados:
            z.extract(nome, PASTA_PRODUCAO)
    for f in novos:
        caminho = os.path.join(PASTA_PRODUCAO, f)
        if os.path.exists(caminho):
            os.remove(caminho)
    os.rename(zip_path, zip_path.replace(PREFIXO_ZIP, "desfeito_" + PREFIXO_ZIP))
    print(f"Desfeito a partir de {os.path.basename(zip_path)}: {len(restaurados)} restaurado(s), "
          f"{len(novos)} arquivo(s) novo(s) removido(s).\nAgora: aba Web -> Reload.")


if __name__ == "__main__":
    if "--desfazer" in sys.argv:
        desfazer()
    else:
        promover("--confirmar" in sys.argv)
