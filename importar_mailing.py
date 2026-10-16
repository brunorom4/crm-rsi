import pandas as pd
import sqlite3
import math

ARQUIVO = r"dados\Front.xlsx"


def limpo_texto(valor):
    """Converte célula da planilha em texto seguro, tratando NaN/None como string vazia."""
    if valor is None:
        return ""
    if isinstance(valor, float) and math.isnan(valor):
        return ""
    texto = str(valor).strip()
    return "" if texto.lower() == "nan" else texto


def limpo_telefone(valor):
    """Como limpo_texto, mas remove o '.0' que aparece quando a coluna vira float por causa de células vazias."""
    if valor is None:
        return ""
    if isinstance(valor, float):
        if math.isnan(valor):
            return ""
        if valor.is_integer():
            return str(int(valor))
        return str(valor)
    texto = str(valor).strip()
    return "" if texto.lower() == "nan" else texto


def limpo_numero(valor):
    """Converte célula da planilha em número seguro, tratando NaN/None/inválido como 0.0."""
    if valor is None:
        return 0.0
    try:
        valor = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(valor) else valor


def limpa_prioridade(valor):
    """Prioridade vai de 0 (menor) a 100 (maior). Qualquer coisa fora disso ou ausente vira 0."""
    numero = limpo_numero(valor)
    numero = int(round(numero))
    return max(0, min(100, numero))


df = pd.read_excel(ARQUIVO)
conn = sqlite3.connect("crm.db")

novos = 0
atualizados = 0
ignorados = 0
erros = 0

for _, linha in df.iterrows():
    try:
        contrato = limpo_texto(linha["NUM_CONTRATO"])
        if not contrato:
            ignorados += 1
            continue  # sem número de contrato não dá pra identificar o registro

        valor_virtua = limpo_numero(linha["VALOR_VIRTUA"])
        valor_total = limpo_numero(linha["VALOR_TOTAL_COBRANCA"])
        prioridade = limpa_prioridade(linha.get("PRIORIDADE", 0))

        # TV, ponto adicional e VOIP não vêm mais da planilha: ficam em branco/zerados
        # na criação do registro e são preenchidos manualmente depois, no formulário.
        # Por isso NÃO entram no dicionário "dados" usado no UPDATE — assim uma
        # reimportação não sobrescreve o que já foi preenchido manualmente.
        dados = {
            "contrato": contrato,
            "cliente": limpo_texto(linha["NOME_ASSINANTE"]),
            "cidade": limpo_texto(linha["NOME_CIDADE"]),
            "node": limpo_texto(linha["NODE"]),
            "endereco": limpo_texto(linha.get("ENDERECO", "")),
            "complemento": limpo_texto(linha.get("COMPLEMENTO", "")),
            "telefone_celular": limpo_telefone(linha["CELULAR"]),
            "telefone_residencial": limpo_telefone(linha["TEL_RES"]),
            "telefone_comercial": limpo_telefone(linha["TEL_COM"]),
            "email": limpo_texto(linha["E_MAIL"]),
            "codigo_imovel": limpo_texto(linha["COD_IMOVEL"]),
            "produto_virtua": limpo_texto(linha["PRODUTO_VIRTUA"]),
            "valor_virtua": valor_virtua,
            "valor_total": valor_total,
            "prioridade": prioridade,
        }

        existente = conn.execute("SELECT id FROM mailing WHERE contrato=?", (contrato,)).fetchone()

        if existente:
            # Upsert: atualiza os dados "informativos" do cliente/produtos/prioridade.
            # NUNCA mexe em status/tentativa/operador/datas — pra não zerar o andamento
            # de um atendimento que já está em curso ou enfileirado.
            conn.execute("""
                UPDATE mailing SET
                    cliente=:cliente, cidade=:cidade, node=:node,
                    endereco=:endereco, complemento=:complemento,
                    telefone_celular=:telefone_celular, telefone_residencial=:telefone_residencial,
                    telefone_comercial=:telefone_comercial, email=:email, codigo_imovel=:codigo_imovel,
                    produto_virtua=:produto_virtua, valor_virtua=:valor_virtua, valor_total=:valor_total,
                    prioridade=:prioridade
                WHERE contrato=:contrato
            """, dados)
            atualizados += 1
        else:
            conn.execute("""
                INSERT INTO mailing (
                    contrato, cliente, cidade, node, endereco, complemento,
                    telefone_celular, telefone_residencial, telefone_comercial,
                    email, codigo_imovel,
                    produto_tv, valor_tv, valor_ponto_adicional,
                    produto_virtua, valor_virtua,
                    produto_voip, valor_voip, valor_total,
                    status, tentativa, prioridade
                ) VALUES (
                    :contrato, :cliente, :cidade, :node, :endereco, :complemento,
                    :telefone_celular, :telefone_residencial, :telefone_comercial,
                    :email, :codigo_imovel,
                    '', 0, 0,
                    :produto_virtua, :valor_virtua,
                    '', 0, :valor_total,
                    'PENDENTE', 1, :prioridade
                )
            """, dados)
            novos += 1

    except Exception as erro:
        erros += 1
        print(f"Erro na linha (contrato={linha.get('NUM_CONTRATO')}):", erro)

conn.commit()
conn.close()

print(f"{novos} novos, {atualizados} atualizados, {ignorados} ignorados (sem contrato), {erros} com erro.")