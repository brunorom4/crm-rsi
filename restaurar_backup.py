"""
Restaura um backup salvo pela pasta backups/ (gerada pelo botão "Backup
Completo" da aplicação) -- feito pra rodar direto pelo console Bash, sem
depender do site estar funcionando. Se o site quebrar, o botão dentro dele
quebra junto -- por isso esse script existe separado (pedido do Bruno,
2026-09-04).

COMO USAR:

  1) Ver os backups disponíveis (sem restaurar nada ainda):
       python3.11 restaurar_backup.py

  2) Restaurar um deles (usa o número que apareceu na lista acima):
       python3.11 restaurar_backup.py 1

Depois de restaurar, ainda precisa ir na aba Web e clicar em Reload -- isso
o script não consegue fazer sozinho.

Por segurança, antes de sobrescrever qualquer coisa, o script salva o estado
ATUAL (mesmo que esteja quebrado) numa pasta backups/antes_do_restore_*.zip
-- pra dar pra voltar atrás caso o backup escolhido não fosse o que você
queria.
"""
import sys
import os
import zipfile
from datetime import datetime

PASTA_BASE = os.path.dirname(os.path.abspath(__file__))
PASTA_BACKUPS = os.path.join(PASTA_BASE, "backups")


def listar_backups():
    if not os.path.isdir(PASTA_BACKUPS):
        print("Nenhum backup encontrado ainda (a pasta 'backups' não existe).")
        print("Isso é normal se você ainda não clicou no botão 'Backup Completo' nenhuma vez.")
        return []
    arquivos = sorted(
        [f for f in os.listdir(PASTA_BACKUPS) if f.startswith("backup_crm_rsi_") and f.endswith(".zip")],
        reverse=True
    )
    if not arquivos:
        print("Nenhum backup encontrado na pasta 'backups'.")
        return []
    print("Backups disponíveis (mais recente primeiro):\n")
    for i, nome in enumerate(arquivos, start=1):
        caminho = os.path.join(PASTA_BACKUPS, nome)
        tamanho_mb = os.path.getsize(caminho) / (1024 * 1024)
        print(f"  {i}) {nome}  ({tamanho_mb:.1f} MB)")
    print("\nPra restaurar um deles, rode:")
    print(f"    python3.11 restaurar_backup.py <número>")
    return arquivos


def restaurar(numero_escolhido, arquivos):
    try:
        indice = int(numero_escolhido) - 1
        nome_escolhido = arquivos[indice]
    except (ValueError, IndexError):
        print(f"'{numero_escolhido}' não é um número válido da lista acima.")
        return

    caminho_escolhido = os.path.join(PASTA_BACKUPS, nome_escolhido)
    print(f"\nRestaurando: {nome_escolhido}")

    # 1) Salva o estado ATUAL antes de mexer em qualquer coisa, por segurança
    nome_seguranca = f"antes_do_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    caminho_seguranca = os.path.join(PASTA_BACKUPS, nome_seguranca)
    print(f"Salvando o estado atual antes de sobrescrever (backups/{nome_seguranca})...")
    with zipfile.ZipFile(caminho_seguranca, "w", zipfile.ZIP_DEFLATED) as zip_seguranca:
        pastas_ignoradas = {"__pycache__", ".git", "flask_session", "backups"}
        for pasta_atual, subpastas, arquivos_pasta in os.walk(PASTA_BASE):
            subpastas[:] = [p for p in subpastas if p not in pastas_ignoradas]
            for nome_arquivo in arquivos_pasta:
                if nome_arquivo.endswith(".pyc"):
                    continue
                caminho_completo = os.path.join(pasta_atual, nome_arquivo)
                caminho_relativo = os.path.relpath(caminho_completo, PASTA_BASE)
                zip_seguranca.write(caminho_completo, caminho_relativo)
    print("OK.")

    # 2) Extrai o backup escolhido por cima de tudo
    print(f"Extraindo {nome_escolhido}...")
    with zipfile.ZipFile(caminho_escolhido, "r") as zip_restaurar:
        zip_restaurar.extractall(PASTA_BASE)
    print("OK.")

    print("\n=== Restauração concluída ===")
    print("Falta só um passo: vai na aba Web do PythonAnywhere e clica em Reload.")


if __name__ == "__main__":
    arquivos = listar_backups()
    if len(sys.argv) > 1 and arquivos:
        restaurar(sys.argv[1], arquivos)
