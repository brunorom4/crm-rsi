"""
Limpeza diária das evidências de GPON Indisponível -- pedido do Bruno,
2026-10-19.

Apaga da pasta uploads/ as imagens com mais de 30 dias. É a pasta onde o
app.py grava a evidência anexada na tabulação "GPON Indisponível"
(`uploads/{id_mailing}_{nome_original}`) -- e SÓ isso: a foto de perfil do
usuário mora em static/fotos_perfil/, não é tocada aqui.

TRAVA DE SEGURANÇA: um contrato que ainda está em análise de GPON
(AGUARDANDO_ANALISE_GPON ou EM_ANALISE_GPON) nunca perde a evidência, por
mais velha que ela seja. Quem analisa precisa da imagem na tela. Quando o
contrato fecha, o arquivo volta a ser elegível e cai na próxima rodada.

COMO USAR

  Simulação (não apaga nada -- rode isso primeiro):
      python3.10 /home/crmrsi/CRM_RSI/tarefa_limpar_imagens_gpon.py --simular

  Valendo (é o que a Scheduled Task roda):
      python3.10 /home/crmrsi/CRM_RSI/tarefa_limpar_imagens_gpon.py

  Mudar o prazo numa rodada avulsa:
      python3.10 .../tarefa_limpar_imagens_gpon.py --dias 60 --simular

AGENDAMENTO (aba Tasks do PythonAnywhere)
  Uma tarefa diária, de madrugada. O horário do PythonAnywhere é UTC: para
  rodar 03:00 de Brasília, agende 06:00.

O QUE FICA REGISTRADO
  O que foi apagado vai pro stdout (a aba Tasks guarda o log de cada
  execução) e também é acrescentado em uploads_limpeza.log, na pasta do
  projeto, que é o histórico que sobrevive à rotação de log do PA.

ATENÇÃO -- efeito colateral conhecido: o nome do arquivo continua escrito
na observação da atividade pra sempre (é assim que o CRM guarda a
referência). Depois que a imagem é apagada, o link "ver evidência" nas
telas de GPON/histórico aponta pra um arquivo que não existe mais e dá
erro 404. Isso é esperado -- a imagem foi apagada de propósito --, mas se
incomodar dá pra fazer a rota /uploads devolver um aviso amigável em vez
do erro.
"""
import os
import sys
import sqlite3
from datetime import datetime, timedelta

# Caminho absoluto: Scheduled Task não roda com o cwd na pasta do projeto,
# e caminho relativo já causou "no such table" aqui antes.
PASTA_BASE = os.path.dirname(os.path.abspath(__file__))
PASTA_UPLOADS = os.path.join(PASTA_BASE, "uploads")
CAMINHO_CRM_DB = os.path.join(PASTA_BASE, "crm.db")
ARQUIVO_LOG = os.path.join(PASTA_BASE, "uploads_limpeza.log")

DIAS_PADRAO = 30

# Status em que o contrato ainda depende da evidência pra ser analisado.
STATUS_EM_ANALISE = ("AGUARDANDO_ANALISE_GPON", "EM_ANALISE_GPON")


def extrair_nome_evidencia_gpon(observacao):
    """Mesma extração usada pelo app.py -- a evidência é guardada como texto
    dentro da observação ("Evidência: arquivo.png"), não numa coluna. Se o
    formato mudar lá, tem que mudar aqui junto."""
    if not observacao or "Evidência: " not in observacao:
        return None
    trecho = observacao.split("Evidência: ", 1)[1]
    nome_arquivo = trecho.split(" | ")[0].strip()
    return nome_arquivo or None


def evidencias_protegidas():
    """Nomes de arquivo citados por atividades de contrato que ainda está em
    análise de GPON. Em caso de qualquer problema de leitura do banco,
    devolve None -- e aí a limpeza inteira é abortada, porque apagar sem
    saber o que proteger é pior do que não apagar."""
    if not os.path.exists(CAMINHO_CRM_DB):
        print(f"ERRO: não achei o banco em {CAMINHO_CRM_DB}.")
        return None
    try:
        conn = sqlite3.connect(CAMINHO_CRM_DB)
        conn.row_factory = sqlite3.Row
        marcadores = ",".join("?" * len(STATUS_EM_ANALISE))
        linhas = conn.execute(f"""
            SELECT a.observacao
            FROM atividades a
            JOIN mailing m ON m.contrato = a.contrato
            WHERE a.observacao LIKE '%Evidência: %'
              AND m.status IN ({marcadores})
        """, STATUS_EM_ANALISE).fetchall()
        conn.close()
    except Exception as erro:
        print(f"ERRO ao consultar o banco: {erro}")
        return None

    protegidas = set()
    for linha in linhas:
        nome = extrair_nome_evidencia_gpon(linha["observacao"])
        if nome:
            protegidas.add(nome)
    return protegidas


def registrar(mensagem, simular):
    print(mensagem)
    if simular:
        return
    try:
        with open(ARQUIVO_LOG, "a", encoding="utf-8") as log:
            log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {mensagem}\n")
    except Exception as erro:
        print(f"  (aviso: não consegui escrever no log -- {erro})")


def main():
    simular = "--simular" in sys.argv
    dias = DIAS_PADRAO
    if "--dias" in sys.argv:
        try:
            dias = int(sys.argv[sys.argv.index("--dias") + 1])
        except (IndexError, ValueError):
            sys.exit("ERRO: --dias precisa vir seguido de um número. Ex: --dias 60")

    cabecalho = "SIMULAÇÃO (nada será apagado)" if simular else "LIMPEZA"
    print(f"=== {cabecalho} -- evidências de GPON com mais de {dias} dias ===")
    print(f"Pasta: {PASTA_UPLOADS}")

    if not os.path.isdir(PASTA_UPLOADS):
        print("Pasta uploads/ não existe -- nada a fazer.")
        return

    protegidas = evidencias_protegidas()
    if protegidas is None:
        sys.exit("Limpeza abortada: sem a lista de proteção, nada é apagado.")
    print(f"Evidências protegidas (contrato ainda em análise): {len(protegidas)}")

    limite = datetime.now() - timedelta(days=dias)
    apagados = protegidos_pulados = mantidos = 0
    bytes_liberados = 0
    erros = []

    for nome in sorted(os.listdir(PASTA_UPLOADS)):
        caminho = os.path.join(PASTA_UPLOADS, nome)
        if not os.path.isfile(caminho):
            continue
        # Arquivo oculto não é evidência -- o .gitkeep que segura a pasta é o
        # caso real (apareceu no teste de 2026-10-19). Tudo que o app.py grava
        # aqui passa por secure_filename, que nunca começa com ponto.
        if nome.startswith("."):
            continue

        modificado = datetime.fromtimestamp(os.path.getmtime(caminho))
        if modificado >= limite:
            mantidos += 1
            continue

        idade = (datetime.now() - modificado).days
        if nome in protegidas:
            print(f"  PROTEGIDO ({idade}d): {nome} -- contrato ainda em análise")
            protegidos_pulados += 1
            continue

        tamanho = os.path.getsize(caminho)
        if simular:
            print(f"  apagaria ({idade}d, {tamanho/1024:.0f} KB): {nome}")
        else:
            try:
                os.remove(caminho)
            except Exception as erro:
                erros.append(f"{nome}: {erro}")
                continue
            registrar(f"apagado ({idade}d, {tamanho/1024:.0f} KB): {nome}", simular)
        apagados += 1
        bytes_liberados += tamanho

    verbo = "seriam apagados" if simular else "apagados"
    resumo = (f"{verbo}: {apagados} | protegidos: {protegidos_pulados} | "
              f"dentro do prazo: {mantidos} | espaço: {bytes_liberados/1024/1024:.1f} MB")
    print(f"--- {resumo}")
    if not simular and apagados:
        registrar(f"RESUMO -- {resumo}", simular)

    if erros:
        print(f"--- {len(erros)} arquivo(s) não puderam ser apagados:")
        for e in erros:
            print(f"    {e}")


if __name__ == "__main__":
    main()
