"""
resetar_dados.py -- Reset da base pra começar os testes reais (2026-08-21).

Apaga TODOS os dados operacionais/de teste (usuários exceto o ADM indicado
abaixo, mailing, atividades, follow-up, checklist de improdutivo, pausas,
sessões de login/logout, escalas, ausências, transferências, notícias/leituras
e monitorias), junto com os arquivos enviados (fotos de perfil de outros
usuários e capturas de tela de GPON em uploads/).

NÃO mexe em configuração do sistema: fica tudo como está em
- cidades (lista padronizada de cidades)
- cidades_bloqueio_distribuicao
- permissoes (matriz de acesso por perfil)
- configuracoes (modo de trabalho, config do Telegram/Supabase, duração de pausas)
- relatorio_email_destinatarios

Rode isso UMA VEZ, dentro da pasta do projeto (~/CRM_RSI), pelo console Bash
do PythonAnywhere:

    cd ~/CRM_RSI
    python3 resetar_dados.py

Ele pede confirmação digitada antes de apagar qualquer coisa -- não faz nada
sem você digitar o login exibido na tela.

IMPORTANTE: isso é IRREVERSÍVEL. Faça um backup do crm.db antes (basta copiar
o arquivo: `cp crm.db crm_backup_antes_do_reset.db`, pela aba Files ou pelo
próprio console Bash).
"""

import os
import sqlite3

LOGIN_ADM_PRESERVAR = "broma"  # login do usuário ADM que NUNCA deve ser apagado

TABELAS_PARA_LIMPAR = [
    "noticias_leituras",
    "noticias",
    "monitorias",
    "checklist_improdutivo",
    "followup",
    "atividades",
    "ausencias",
    "pausas",
    "sessoes",
    "escalas",
    "transferencias",
    "mailing",
]

TABELAS_PARA_MANTER = [
    "cidades",
    "cidades_bloqueio_distribuicao",
    "permissoes",
    "configuracoes",
    "relatorio_email_destinatarios",
]


def contar_linhas(conn, tabela):
    return conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]


def main():
    if not os.path.exists("crm.db"):
        print("ERRO: crm.db não encontrado nesta pasta. Rode isso dentro de ~/CRM_RSI.")
        return

    conn = sqlite3.connect("crm.db")
    conn.row_factory = sqlite3.Row

    adm = conn.execute("SELECT id, nome, login, perfil, foto FROM usuarios WHERE login=?", (LOGIN_ADM_PRESERVAR,)).fetchone()
    if not adm:
        print(f"ERRO: não encontrei nenhum usuário com login '{LOGIN_ADM_PRESERVAR}'. Nada foi apagado.")
        conn.close()
        return
    if adm["perfil"] != "ADM":
        print(f"ERRO: o usuário '{LOGIN_ADM_PRESERVAR}' encontrado não é ADM (perfil={adm['perfil']}). Nada foi apagado.")
        conn.close()
        return

    print("=" * 60)
    print("RESET DE DADOS -- CRM RSI")
    print("=" * 60)
    print(f"\nUsuário que será PRESERVADO: {adm['nome']} (login: {adm['login']}, id: {adm['id']})")
    print("\nSerão APAGADOS (todas as linhas):")
    for t in TABELAS_PARA_LIMPAR + ["usuarios (exceto o login acima)"]:
        print(f"  - {t}")
    print("\nSerão MANTIDOS como estão:")
    for t in TABELAS_PARA_MANTER:
        print(f"  - {t}")
    print("\nTambém serão apagados os arquivos em uploads/ (capturas de GPON)")
    print(f"e em static/fotos_perfil/ (fotos de perfil), exceto a foto de {adm['login']}.")
    print("\nISSO É IRREVERSÍVEL. Se ainda não fez backup do crm.db, cancele agora (Ctrl+C).")
    print("=" * 60)

    digitado = input(f"\nPra confirmar, digite exatamente o login '{adm['login']}': ").strip()
    if digitado != adm["login"]:
        print("Login não confere. Nada foi apagado.")
        conn.close()
        return

    print("\nContagem ANTES do reset:")
    antes = {}
    for t in TABELAS_PARA_LIMPAR + ["usuarios"]:
        antes[t] = contar_linhas(conn, t)
        print(f"  {t}: {antes[t]}")

    conn.execute("BEGIN")
    try:
        for t in TABELAS_PARA_LIMPAR:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM usuarios WHERE login != ?", (adm["login"],))

        # Reseta os contadores de autoincremento das tabelas limpas, pra próxima
        # inserção começar do id 1 de novo (não mexe no contador de 'usuarios',
        # já que o ADM continua existindo com o id dele).
        nomes_para_resetar = TABELAS_PARA_LIMPAR
        conn.executemany(
            "DELETE FROM sqlite_sequence WHERE name=?",
            [(t,) for t in nomes_para_resetar]
        )
        conn.commit()
    except Exception as erro:
        conn.rollback()
        print(f"\nERRO durante o reset, nada foi alterado (rollback): {erro}")
        conn.close()
        return

    print("\nContagem DEPOIS do reset:")
    depois = {}
    for t in TABELAS_PARA_LIMPAR + ["usuarios"]:
        depois[t] = contar_linhas(conn, t)
        print(f"  {t}: {depois[t]}")

    print("\nConfiguração preservada (não foi tocada):")
    for t in TABELAS_PARA_MANTER:
        print(f"  {t}: {contar_linhas(conn, t)} linha(s)")

    conn.execute("VACUUM")
    conn.close()

    # Arquivos: apaga tudo em uploads/ (exceto .gitkeep) e as fotos de perfil
    # de todo mundo, exceto a do ADM preservado.
    removidos_uploads = 0
    if os.path.isdir("uploads"):
        for nome in os.listdir("uploads"):
            if nome == ".gitkeep":
                continue
            caminho = os.path.join("uploads", nome)
            if os.path.isfile(caminho):
                os.remove(caminho)
                removidos_uploads += 1

    removidos_fotos = 0
    foto_preservar = os.path.basename(adm["foto"]) if adm["foto"] else None
    pasta_fotos = os.path.join("static", "fotos_perfil")
    if os.path.isdir(pasta_fotos):
        for nome in os.listdir(pasta_fotos):
            if nome == foto_preservar:
                continue
            caminho = os.path.join(pasta_fotos, nome)
            if os.path.isfile(caminho):
                os.remove(caminho)
                removidos_fotos += 1

    print(f"\nArquivos removidos: {removidos_uploads} em uploads/, {removidos_fotos} em static/fotos_perfil/")
    print("\nReset concluído. A base está pronta pra importar os dados reais.")


if __name__ == "__main__":
    main()
