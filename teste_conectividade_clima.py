"""
Teste isolado de conectividade -- so confirma se o PythonAnywhere consegue
falar com a API de previsao do tempo (Open-Meteo, gratuita, sem chave).
Nao mexe em nada do sistema, so imprime o resultado.

COMO USAR:
  python3.11 teste_conectividade_clima.py
"""
import urllib.request
import json

# Coordenadas de Campinas, so pra esse teste
LAT, LON = -22.9099, -47.0626
URL = (
    f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
    f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode"
    f"&timezone=America/Sao_Paulo&forecast_days=7"
)

print(f"Testando conexao com: {URL}\n")

try:
    with urllib.request.urlopen(URL, timeout=15) as resposta:
        dados = json.loads(resposta.read().decode("utf-8"))
        print("SUCESSO -- a API respondeu certinho.\n")
        diario = dados.get("daily", {})
        datas = diario.get("time", [])
        temp_max = diario.get("temperature_2m_max", [])
        temp_min = diario.get("temperature_2m_min", [])
        chuva = diario.get("precipitation_probability_max", [])
        print("Previsao pra Campinas, proximos dias:")
        for i in range(len(datas)):
            print(f"  {datas[i]}: {temp_min[i]:.0f} a {temp_max[i]:.0f} C, {chuva[i]}% chance de chuva")
except urllib.error.HTTPError as erro:
    print(f"FALHOU -- HTTP {erro.code}: {erro.reason}")
    print("(a API respondeu, mas recusou -- pouco provavel de ser bloqueio de rede)")
except urllib.error.URLError as erro:
    print(f"FALHOU -- erro de conexao: {erro.reason}")
    print("(isso sim tem cara de rede/proxy bloqueando o acesso)")
except Exception as erro:
    print(f"FALHOU -- {type(erro).__name__}: {erro}")
