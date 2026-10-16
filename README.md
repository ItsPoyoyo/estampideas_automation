# Estampi

POC de pedidos fictícios de CRM/ERP para o Trello, com FastAPI, SQLite e preenchimento por IA.

## Rodar

Python 3.11 ou mais recente.

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Preencha o `.env` com os IDs dos seus dois quadros de teste e as credenciais do Trello. `OPENAI_API_KEY` é opcional para organizar mensagens com IA.

Para OAuth, configure o callback `http://127.0.0.1:8001/trello/callback` e os scopes `read:board:trello` e `write:board:trello`. Use as listas e modelos definidos em [rules.py](app/rules.py) e [production_rules.py](app/production_rules.py).

```powershell
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --no-access-log
```

Abra http://127.0.0.1:8001 e clique em **Conectar Trello**. A API está em `/docs`. Tokens OAuth são preenchidos pela app; reconecte quando expirarem.

## Fluxo

- Cole uma mensagem ou use **Exemplo manual**, revise e crie um card por produto.
- Dados incompletos vão para **ENGATILHADOS**; **Completar** libera o mesmo card.
- Prazos usam dias úteis, aprovação da arte, informações completas e corte às 16h.
- Em DTF UV e Windflag, confirme **Etapas** na demo ou no checklist do Trello para mover o card automaticamente.
- Reenvios mantêm os mesmos cards. O histórico registra as decisões.

Mantenha o servidor aberto para a automação. Configure `TRELLO_BLOCKED_BOARD_IDS` para impedir acesso a quadros de produção. Credenciais, IDs reais e bancos ficam locais; somente `.env.example` entra no Git.

## Testes

```powershell
.\venv\Scripts\python.exe -m pytest -q
```
