"""
Monta (ou remonta) o AMBIENTE ESPELHO a partir de um backup completo --
pedido do Bruno, 2026-09-25. Fica em /home/crmrsi/montar_espelho.py e roda
pelo console Bash do PythonAnywhere.

COMO USAR

  1) Espelho completo a partir do backup MAIS RECENTE da pasta
     /home/crmrsi/CRM_RSI/backups/ (gerado pelo botão "Backup Completo"):
         python3.11 /home/crmrsi/montar_espelho.py

  2) A partir de um zip específico (ex: um que você subiu pelo Files):
         python3.11 /home/crmrsi/montar_espelho.py /home/crmrsi/backup_crm_rsi_20260925_1903.zip

  3) Só atualizar os DADOS do espelho (bancos .db), mantendo o código novo
     que você já subiu lá:
         python3.11 /home/crmrsi/montar_espelho.py --so-dados

  Depois: aba Web -> Reload.

O QUE ELE FAZ

  * Apaga a pasta do espelho e extrai o backup nela (no modo --so-dados, só
    troca os arquivos .db).
  * Cria o ESPELHO.flag (é assim que o app.py sabe que é o espelho).
  * "Desarma" as integrações no banco do espelho: apaga token do Telegram,
    URL/chave do Supabase, senhas/segredos de e-mail e pasta de destino, e
    desliga os envios automáticos. É uma segunda trava -- o código do
    espelho já bloqueia essas chamadas de qualquer jeito.
  * NUNCA toca na pasta de produção (só lê o zip da pasta backups/).
"""
import os
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime

PASTA_PRODUCAO = "/home/crmrsi/CRM_RSI"
PASTA_ESPELHO = "/home/crmrsi/CRM_RSI_ESPELHO"
PASTA_BACKUPS = os.path.join(PASTA_PRODUCAO, "backups")
BANCOS = ("crm.db", "backlog.db", "confirmacao_agenda.db")

# Colunas de configuracoes que ficam vazias / desligadas no espelho
# (só mexe nas que existirem -- versões diferentes do banco têm colunas
# diferentes).
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


def backup_mais_recente():
    if not os.path.isdir(PASTA_BACKUPS):
        sys.exit(f"Não achei a pasta {PASTA_BACKUPS}. Gere um Backup Completo antes, "
                 "ou passe o caminho do zip.")
    zips = sorted(f for f in os.listdir(PASTA_BACKUPS)
                  if f.startswith("backup_crm_rsi_") and f.endswith(".zip"))
    if not zips:
        sys.exit("Nenhum backup_crm_rsi_*.zip na pasta backups/.")
    return os.path.join(PASTA_BACKUPS, zips[-1])


def conferir_destino():
    real_espelho = os.path.realpath(PASTA_ESPELHO)
    if real_espelho == os.path.realpath(PASTA_PRODUCAO):
        sys.exit("ERRO: a pasta do espelho é a mesma da produção. Nada foi feito.")
    if os.path.isdir(PASTA_ESPELHO) and os.listdir(PASTA_ESPELHO) \
            and not os.path.exists(os.path.join(PASTA_ESPELHO, "ESPELHO.flag")):
        sys.exit(f"ERRO: {PASTA_ESPELHO} já existe, tem arquivos e NÃO tem ESPELHO.flag -- "
                 "não parece ser um espelho. Por segurança, nada foi apagado.")


def extrair(zip_path, so_dados):
    with zipfile.ZipFile(zip_path) as z:
        nomes = z.namelist()
        destino_real = os.path.realpath(PASTA_ESPELHO)
        for nome in nomes:  # proteção contra caminhos "../" dentro do zip
            alvo = os.path.realpath(os.path.join(PASTA_ESPELHO, nome))
            if not (alvo == destino_real or alvo.startswith(destino_real + os.sep)):
                sys.exit(f"ERRO: caminho suspeito dentro do zip: {nome}")
        if "app.py" not in nomes:
            sys.exit("ERRO: esse zip não tem app.py na raiz -- não parece um Backup Completo.")

        if so_dados:
            for banco in BANCOS:
                for sufixo in ("", "-wal", "-shm", "-journal"):
                    caminho = os.path.join(PASTA_ESPELHO, banco + sufixo)
                    if os.path.exists(caminho):
                        os.remove(caminho)
            extraidos = [n for n in nomes if n.split("-")[0] in BANCOS]
            for nome in extraidos:
                z.extract(nome, PASTA_ESPELHO)
            return extraidos

        if os.path.isdir(PASTA_ESPELHO):
            shutil.rmtree(PASTA_ESPELHO)
        os.makedirs(PASTA_ESPELHO)
        z.extractall(PASTA_ESPELHO)
        return nomes


def desarmar_integracoes():
    caminho = os.path.join(PASTA_ESPELHO, "crm.db")
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    so_dados = "--so-dados" in sys.argv
    zip_path = args[0] if args else backup_mais_recente()
    if not os.path.isfile(zip_path):
        sys.exit(f"ERRO: não achei {zip_path}")

    conferir_destino()
    if so_dados and not os.path.exists(os.path.join(PASTA_ESPELHO, "app.py")):
        sys.exit("ERRO: --so-dados precisa de um espelho já montado. Rode sem --so-dados primeiro.")

    print(f"Backup: {zip_path}")
    print(f"Espelho: {PASTA_ESPELHO}  ({'só dados' if so_dados else 'completo'})")
    extraidos = extrair(zip_path, so_dados)
    print(f"  {len(extraidos)} arquivo(s) extraído(s)")

    with open(os.path.join(PASTA_ESPELHO, "ESPELHO.flag"), "w", encoding="utf-8") as flag:
        flag.write(
            "Esta pasta é o AMBIENTE ESPELHO do CRM RSI. NÃO copie este arquivo pra produção.\n"
            f"Montado em {datetime.now():%Y-%m-%d %H:%M} a partir de {os.path.basename(zip_path)}"
            f"{' (só dados)' if so_dados else ''}\n"
        )
    for raiz, pastas, _ in os.walk(PASTA_ESPELHO):
        for p in list(pastas):
            if p == "__pycache__":
                shutil.rmtree(os.path.join(raiz, p))
                pastas.remove(p)

    desarmar_integracoes()
    print("\nPronto. Próximos passos:")
    print(f"  1) (opcional) Suba os arquivos da versão nova em {PASTA_ESPELHO}")
    print("  2) Aba Web -> Reload")
    print("  3) Portal (logado como ADM) -> botão 'Ambiente Espelho'")


if __name__ == "__main__":
    main()
