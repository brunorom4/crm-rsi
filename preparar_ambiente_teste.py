"""
Prepara o AMBIENTE DE TESTE (conta gratuita separada do PythonAnywhere) a
partir de um "Backup Completo" da produção -- pedido do Bruno, 2026-10-15,
quando trocamos o espelho-dentro-do-mesmo-app por uma conta separada + git.

A divisão de trabalho no fluxo novo é:

    CÓDIGO  ->  git pull        (produção e teste puxam do mesmo repositório)
    DADOS   ->  este script     (backup da produção restaurado só aqui)

Por isso este script NUNCA escreve código: ele extrai do zip apenas os
bancos e as pastas de dados. Assim ele não atropela a árvore do git (se
sobrescrevesse app.py/templates, o `git status` encheria de alteração
fantasma e o próximo `git pull` daria conflito).

COMO USAR (no console Bash da conta de TESTE)

  1) Primeira vez -- marca esta pasta como ambiente de teste:
         python3.11 preparar_ambiente_teste.py --confirmar-primeira-vez

  2) Daí em diante, sempre que quiser os dados atuais da produção:
       a) na produção: botão "Backup Completo" e baixe o zip
       b) na conta de teste: suba o zip pelo Files (pode ser na raiz do projeto)
       c) python3.11 preparar_ambiente_teste.py backup_crm_rsi_AAAAMMDD_HHMM.zip
          (sem argumento, ele pega o zip mais recente que achar na pasta do
           projeto ou em backups/)
       d) aba Web -> Reload

O QUE ELE FAZ

  * Restaura SÓ dados: crm.db, backlog.db, confirmacao_agenda.db e as pastas
    uploads/, static/fotos_perfil/, painel_dados/, painel_cotas_dados/, dados/.
  * DESARMA as integrações no crm.db restaurado: apaga token do Telegram,
    URL/chave do Supabase, credenciais de e-mail, e desliga todos os envios
    automáticos. Sem isso o ambiente de teste sairia mandando relatório no
    grupo real do Telegram com os dados de produção -- mesma trava que o
    montar_espelho.py já fazia no espelho.
  * Se recusa a rodar sem o AMBIENTE_TESTE.flag, pra nunca ser executado por
    engano dentro da produção.
"""
import os
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime

PASTA_BASE = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_FLAG = os.path.join(PASTA_BASE, "AMBIENTE_TESTE.flag")

# Pasta da produção -- trava extra: se o script estiver rodando aqui, para tudo.
PASTA_PRODUCAO = "/home/crmrsi/CRM_RSI"

BANCOS = ("crm.db", "backlog.db", "confirmacao_agenda.db")
PASTAS_DE_DADOS = ("uploads", "static/fotos_perfil", "painel_dados",
                   "painel_cotas_dados", "dados")

# Mesmas listas usadas pelo montar_espelho.py (mantidas iguais de propósito:
# se um dia aparecer uma integração nova, os dois precisam ganhar a coluna).
LIMPAR = (
    "telegram_bot_token", "telegram_chat_id",
    "email_smtp_host", "email_smtp_usuario", "email_smtp_senha", "email_remetente",
    "email_pasta_destino", "email_tenant_id", "email_client_id", "email_client_secret",
    "supabase_project_url", "supabase_anon_key",
)
DESLIGAR = (
    "telegram_envio_automatico_ativo", "email_envio_automatico_ativo",
    "supabase_consulta_ativa", "supabase_telegram_ativo",
)


def conferir_ambiente(primeira_vez):
    if os.path.realpath(PASTA_BASE) == os.path.realpath(PASTA_PRODUCAO):
        sys.exit("ERRO: esta é a pasta da PRODUÇÃO. Nada foi feito.")

    if os.path.exists(ARQUIVO_FLAG):
        return

    if not primeira_vez:
        sys.exit(
            f"ERRO: não achei o {os.path.basename(ARQUIVO_FLAG)} nesta pasta.\n"
            "Se esta é mesmo a pasta do ambiente de TESTE, rode uma vez:\n"
            "    python3.11 preparar_ambiente_teste.py --confirmar-primeira-vez"
        )

    with open(ARQUIVO_FLAG, "w", encoding="utf-8") as flag:
        flag.write(
            "Esta pasta é o AMBIENTE DE TESTE do CRM RSI (conta gratuita separada).\n"
            "NUNCA copie este arquivo pra produção.\n"
            f"Marcado em {datetime.now():%Y-%m-%d %H:%M}\n"
        )
    print(f"Pasta marcada como ambiente de teste ({os.path.basename(ARQUIVO_FLAG)} criado).")


def achar_zip():
    candidatos = []
    for pasta in (PASTA_BASE, os.path.join(PASTA_BASE, "backups")):
        if not os.path.isdir(pasta):
            continue
        for nome in os.listdir(pasta):
            if nome.startswith("backup_crm_rsi_") and nome.endswith(".zip"):
                candidatos.append(os.path.join(pasta, nome))
    if not candidatos:
        sys.exit(
            "Nenhum backup_crm_rsi_*.zip encontrado.\n"
            "Gere um Backup Completo na produção, baixe o zip e suba ele aqui pelo Files."
        )
    return max(candidatos, key=os.path.getmtime)


def eh_dado(nome):
    """True se o caminho dentro do zip for banco ou pasta de dados."""
    if nome.split("-")[0] in BANCOS:  # crm.db, crm.db-wal, crm.db-shm...
        return True
    return any(nome == pasta or nome.startswith(pasta + "/") for pasta in PASTAS_DE_DADOS)


def restaurar_dados(zip_path):
    destino_real = os.path.realpath(PASTA_BASE)
    with zipfile.ZipFile(zip_path) as z:
        nomes = z.namelist()
        if "app.py" not in nomes:
            sys.exit("ERRO: esse zip não tem app.py na raiz -- não parece um Backup Completo.")

        alvos = [n for n in nomes if eh_dado(n) and not n.endswith("/")]
        for nome in alvos:  # proteção contra caminhos "../" dentro do zip
            alvo = os.path.realpath(os.path.join(PASTA_BASE, nome))
            if not alvo.startswith(destino_real + os.sep):
                sys.exit(f"ERRO: caminho suspeito dentro do zip: {nome}")

        # Limpa as pastas de dados e os bancos antigos antes de extrair, senão
        # sobra arquivo velho que já não existe mais na produção.
        for pasta in PASTAS_DE_DADOS:
            caminho = os.path.join(PASTA_BASE, pasta)
            if os.path.isdir(caminho):
                shutil.rmtree(caminho)
        for banco in BANCOS:
            for sufixo in ("", "-wal", "-shm", "-journal"):
                caminho = os.path.join(PASTA_BASE, banco + sufixo)
                if os.path.exists(caminho):
                    os.remove(caminho)

        for nome in alvos:
            z.extract(nome, PASTA_BASE)
        return alvos


def desarmar_integracoes():
    caminho = os.path.join(PASTA_BASE, "crm.db")
    if not os.path.exists(caminho):
        print("  (sem crm.db no backup -- nada a desarmar)")
        return
    conn = sqlite3.connect(caminho)
    colunas = {linha[1] for linha in conn.execute("PRAGMA table_info(configuracoes)")}
    sets = [f"{c} = NULL" for c in LIMPAR if c in colunas] + \
           [f"{c} = 0" for c in DESLIGAR if c in colunas]
    if sets:
        conn.execute(f"UPDATE configuracoes SET {', '.join(sets)}")
        conn.commit()
    resultado = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    print(f"  integrações desarmadas ({len(sets)} campos); integridade do crm.db: {resultado}")


def main():
    primeira_vez = "--confirmar-primeira-vez" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    conferir_ambiente(primeira_vez)

    zip_path = os.path.abspath(args[0]) if args else achar_zip()
    if not os.path.isfile(zip_path):
        sys.exit(f"ERRO: não achei {zip_path}")

    print(f"Backup:  {zip_path}")
    print(f"Destino: {PASTA_BASE}  (só dados -- o código vem do git)")
    restaurados = restaurar_dados(zip_path)
    print(f"  {len(restaurados)} arquivo(s) de dados restaurado(s)")
    desarmar_integracoes()

    print("\nPronto. Próximos passos:")
    print("  1) aba Web -> Reload")
    print("  2) confira que o código está na versão que você quer testar: git log -1 --oneline")


if __name__ == "__main__":
    main()
