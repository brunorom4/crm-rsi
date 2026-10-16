resetar_para_producao.py

Apaga TODOS os dados operacionais do CRM (usuários, contratos, atividades,
histórico de pausas/sessões, notícias, monitorias, etc.) -- pensado pra rodar
UMA VEZ SÓ, ao voltar das férias, antes de subir a versão definitiva e
cadastrar os usuários reais e a base real de contratos.

O QUE É APAGADO (dado operacional/de teste):
    - usuarios          (todo mundo -- vai recadastrar só quem existe de verdade)
    - mailing           (todos os contratos -- vai importar a base real)
    - atividades        (todo o histórico de tabulação)
    - followup          (retornos agendados)
    - checklist_improdutivo
    - pausas, sessoes   (histórico de ponto/pausas)
    - transferencias
    - ausencias
    - escalas           (presas aos usuários de teste -- recria pros usuários reais)
    - noticias, noticias_leituras
    - monitorias
    - cidades_bloqueio_distribuicao
    - relatorio_email_destinatarios (tabela antiga, não usada mais)

O QUE É MANTIDO (configuração do sistema -- não é "dado", é ajuste que você já fez):
    - configuracoes     (link do Sunny, durações de pausa, pasta do relatório, toggles)
    - permissoes        (a matriz de quem pode ver cada módulo)
    - cidades           (lista padronizada de cidades usada na tela de Nova Tarefa)

Se quiser que alguma dessas três também seja apagada, é só mover o nome dela
de TABELAS_MANTIDAS pra TABELAS_PARA_APAGAR antes de rodar.

Faz backup automático do banco ANTES de apagar qualquer coisa, e só prossegue
depois de você digitar a frase de confirmação exata.
"""
import sqlite3
import shutil
from datetime import datetime

CAMINHO_BANCO = "crm.db"