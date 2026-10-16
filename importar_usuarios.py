"""
Importa/atualiza usuários a partir de um arquivo em dados/Usuarios.xlsx (ou .csv),
com as colunas: Nome, Login, Email, Perfil (Operador / Supervisor / Adm),
Data Nascimento (opcional, formato DD/MM/AAAA).

- Usuário novo (login não existe): cria com senha inicial = o próprio login
  (a pessoa deve trocar no primeiro acesso — troca de senha ainda não tem tela,
  fica pra uma próxima fase; por enquanto avise a pessoa a senha inicial).
- Usuário existente: atualiza nome, email, perfil e data de nascimento, mas
  NUNCA mexe na senha.
- Não desativa ninguém automaticamente — desligar usuário é feito na tela de
  Supervisão (fase 2).
"""
import pandas as pd
import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash

ARQUIVO_XLSX = r"dados\Usuarios.xlsx"
ARQUIVO_CSV = r"dados\Usuarios.csv"

PERFIS_VALIDOS = {"OPERADOR", "SUPERVISOR", "ADM", "ANALISTA", "ANALISTA_II"}


def carregar_planilha():
    import os
    if os.path.exists(ARQUIVO_XLSX):
        return pd.read_excel(ARQUIVO_XLSX)
    if os.path.exists(ARQUIVO_CSV):
        return pd.read_csv(ARQUIVO_CSV)
    raise FileNotFoundError(f"Não achei {ARQUIVO_XLSX} nem {ARQUIVO_CSV}")


def limpo(valor):
    if valor is None:
        return ""
    texto = str(valor).strip()
    return "" if texto.lower() == "nan" else texto


def converter_data_nascimento(valor):
    """Aceita DD/MM/AAAA (texto ou já como data do Excel) e devolve AAAA-MM-DD,
    ou None se vier vazio/inválido."""
    texto = limpo(valor)
    if not texto:
        return None
    if hasattr(valor, "strftime"):  # já veio como data/timestamp do pandas
        return valor.strftime("%Y-%m-%d")
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).strftime("%Y-%m-%d")
        except ValueError:
            continue
    print(f"Aviso: data de nascimento '{texto}' não reconhecida, ignorando.")
    return None


def pegar_coluna(linha, *nomes_possiveis):
    """Tenta achar o valor testando várias variações de nome de coluna (com
    espaço, com underscore, maiúsculo/minúsculo) -- assim não trava se a
    planilha usar uma grafia um pouco diferente da esperada."""
    for nome in nomes_possiveis:
        if nome in linha and pd.notna(linha[nome]):
            return linha[nome]
    # Tenta de novo ignorando maiúsculo/minúsculo e espaço vs underscore
    chaves_normalizadas = {str(k).strip().lower().replace(" ", "_").replace("-", "_"): k for k in linha.index}
    for nome in nomes_possiveis:
        chave_normalizada = nome.strip().lower().replace(" ", "_").replace("-", "_")
        if chave_normalizada in chaves_normalizadas:
            valor = linha[chaves_normalizadas[chave_normalizada]]
            if pd.notna(valor):
                return valor
    return None


df = carregar_planilha()
conn = sqlite3.connect("crm.db")
conn.execute("PRAGMA busy_timeout=10000")  # espera até 10s se o app.py estiver gravando algo na hora

novos = 0
atualizados = 0
ignorados = 0

for _, linha in df.iterrows():
    nome = limpo(pegar_coluna(linha, "Nome"))
    login = limpo(pegar_coluna(linha, "Login")).lower()
    email = limpo(pegar_coluna(linha, "Email"))
    perfil = limpo(pegar_coluna(linha, "Perfil")).upper().replace(" ", "_")
    data_nascimento = converter_data_nascimento(pegar_coluna(linha, "Data Nascimento", "data_nascimento", "Data de Nascimento"))

    if not login or not nome:
        ignorados += 1
        continue

    if perfil not in PERFIS_VALIDOS:
        print(f"Aviso: perfil inválido '{perfil}' para login '{login}', usando OPERADOR.")
        perfil = "OPERADOR"

    existente = conn.execute("SELECT id FROM usuarios WHERE login=?", (login,)).fetchone()

    if existente:
        conn.execute(
            "UPDATE usuarios SET nome=?, email=?, perfil=?, data_nascimento=COALESCE(?, data_nascimento) WHERE login=?",
            (nome, email, perfil, data_nascimento, login)
        )
        atualizados += 1
    else:
        senha_inicial = generate_password_hash(login)
        conn.execute(
            "INSERT INTO usuarios (nome, login, senha, email, perfil, ativo, precisa_trocar_senha, data_nascimento) VALUES (?, ?, ?, ?, ?, 1, 1, ?)",
            (nome, login, senha_inicial, email, perfil, data_nascimento)
        )
        novos += 1
        print(f"Usuário novo: {login} — senha inicial é o próprio login ('{login}').")

conn.commit()
conn.close()

print(f"{novos} novos, {atualizados} atualizados, {ignorados} ignorados (sem nome/login).")