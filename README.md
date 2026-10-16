# Estampi — pedido → Trello

POC local: pedidos fictícios de CRM/ERP criam cards reais em dois quadros de teste.

## Rodar

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (!(Test-Path .env)) { New-Item -ItemType File .env | Out-Null }
# Preencha o OAuth Client ID e Client Secret no .env (nunca no JavaScript).
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001 --no-access-log
```

Requer Python 3.11 ou mais recente. Configure `TRELLO_DIGITAL_BOARD_ID` e `TRELLO_SUBLIMACAO_BOARD_ID` com os IDs dos seus quadros de teste; não há IDs padrão. Abra http://127.0.0.1:8001. A documentação interativa fica em `/docs`.
Clique em **Conectar Trello** para autorizar a conta. Sem conexão, validação e prévia funcionam; criar um card retorna 503.

Preencha o `.env` local com suas próprias configurações:

```dotenv
TRELLO_DIGITAL_BOARD_ID=
TRELLO_SUBLIMACAO_BOARD_ID=
TRELLO_OAUTH_CLIENT_ID=
TRELLO_OAUTH_CLIENT_SECRET=
OPENAI_API_KEY=
TRELLO_BLOCKED_BOARD_IDS=
```

`TRELLO_BLOCKED_BOARD_IDS` recebe os IDs dos quadros que nunca podem ser acessados pela POC, separados por vírgulas. Configure os IDs curtos e completos dos quadros de produção nesse bloqueio. `TRELLO_TEMPLATE_IDS` pode mapear nomes de modelos para os IDs locais em JSON. Arquivos de ambiente, IDs reais, bancos e exportações não fazem parte do repositório.

## Conexão com o Trello

A app usa OAuth 2.0 com apenas `read:board:trello` e `write:board:trello`. As permissões continuam abrangendo os quadros acessíveis à conta; o código usa somente os dois IDs de teste configurados. Não são solicitadas permissões de membros, empresas ou administração de workspaces, nem `offline_access`.

No app Estampi POC da administração do Trello, configure o cliente como **Confidential**, com esses dois scopes e callback `http://127.0.0.1:8001/trello/callback`. Guarde Client ID e Secret no `.env`. O botão de conexão usa PKCE e valida o estado da autorização; o callback salva o token no `.env`, nunca no navegador ou na resposta da API. O token curto normalmente dura uma hora; quando expirar, clique em **Reconectar Trello**. Não há renovação automática ou refresh token. A credencial OAuth do app identifica a integração, mas sozinha não dá acesso aos quadros.

Use `--no-access-log` para não registrar códigos de autorização nos logs de acesso. As rotas `/trello/connect` e `/trello/callback` cuidam somente da conexão; o fluxo do pedido não depende de detalhes do OAuth. Referência: https://developer.atlassian.com/cloud/trello/guides/rest-api/oauth-2-confidential-client-usage/

Chave/token legados continuam opcionais se já tiverem sido autorizados separadamente. O grant legado de leitura/escrita é mais amplo; esta POC foi configurada com os dois scopes de boards acima.

## Fluxo e arquivos

Texto colado → OpenAI → revisão no formulário → `POST /orders/batch` → um card por produto → quadros de teste → guardar resultado.

Para a leitura de texto, acrescente `OPENAI_API_KEY` ao `.env` local. `OPENAI_MODEL` é opcional (padrão `gpt-5.4-mini`). A chave fica no backend. Salvar o `.env` basta; não precisa reiniciar o servidor. A IA recebe somente o texto colado e o catálogo de seis produtos, sem acesso ao Trello ou ferramentas. A chamada usa Responses com JSON Schema estrito e `store: false`. Referência: https://developers.openai.com/api/docs/guides/structured-outputs

A IA propõe os campos; o usuário revisa antes do envio. Informação ausente permanece em branco. Produto desconhecido fica visível para seleção manual. A aprovação só é extraída quando data, hora e aprovação estão explícitas. Sem chave ou em caso de falha da IA, o formulário e os exemplos continuam disponíveis.

- `app/models.py`: valida o pedido recebido.
- `app/intake.py`: extrai pedidos do texto com OpenAI, sem criar cards nem calcular prazos.
- `app/rules.py`: produto, quadro, coluna, modelo e dias de produção.
- `app/deadlines.py`: cálculo puro do corte e dias úteis.
- `app/cards.py`: monta título, descrição e prévia.
- `app/trello.py`: chamadas HTTPX à API pública do Trello; copia o card de origem com etiquetas, checklists e anexos.
- `app/oauth.py`: conexão local ao Trello com permissões de boards, PKCE e validação de estado.
- `app/service.py`: coordena o envio e guarda o ID/resultado em SQLite.
- `app/followup.py`: completa dados pendentes, controla revisões e atualiza o mesmo card.
- `app/storage.py`: migração aditiva, resultado atual e histórico do pedido.
- `app/demo.py`: comparação do corte das 16h, sem chamadas ao Trello.
- `app/production_rules.py`: três confirmações e rotas explícitas de DTF UV e Windflag.
- `app/production.py`: prepara checklists, observa confirmações e movimenta cards com guarda de etapa.
- `app/main.py`: endpoints FastAPI. `app/static/`: página sem framework.

`POST /intake/parse` organiza um texto. `POST /orders/batch/preview` calcula as prévias e não altera o Trello. `POST /orders/batch` cria os cards e acrescenta os links entre itens do mesmo pedido. As rotas individuais `/orders/validate`, `/orders/preview` e `/orders` continuam disponíveis.

Cada item recebe um identificador estável (`PEDIDO__ITEM`), mas os títulos e descrições mostram o número comum do pedido. Todos os quadros/listas/modelos são conferidos antes da primeira criação. Um erro confirmado permite continuar apenas nos itens restantes; uma resposta incerta bloqueia repetição automática. Cards criados são preservados em falhas parciais.
Repetir o mesmo pedido retorna o card já criado. Reutilizar o ID com outros dados retorna 409.
Se a conexão cair durante o POST ao Trello, o ID fica bloqueado: o card pode ter sido criado. Confira o quadro antes de resolver manualmente o registro em `data/orders.sqlite3`; não envie com outro número sem conferir.

Depois da escrita, a app lê o card no Trello e confere quadro, lista, título e prazo. Uma falha nessa leitura aparece como confirmação indisponível; ela não autoriza outra criação. **Conferir Trello** consulta novamente, sem mover cards. A página mostra a lista retornada pelo Trello mesmo quando ela difere da prévia.

**Completar** abre somente os campos pendentes daquele item. A resposta pode ser organizada pela IA e revisada, ou preenchida manualmente. A prévia não escreve no Trello. Ao confirmar, um `PATCH` atualiza o mesmo ID, preservando a descrição do modelo, capas/anexos e links entre itens. Respostas parciais mantêm o item em `ENGATILHADOS`, sem prazo; completar todos os dados libera para a lista de lançamento daquele produto. Dados já preenchidos, produto e identidade não podem ser alterados por essa rota.

Cada complemento tem uma revisão e uma operação persistida. Repetir a mesma confirmação devolve o resultado existente. Se a resposta de um `PUT` se perder, **Confirmar atualização** verifica se ele já foi aplicado antes de repetir qualquer escrita. Mudanças externas no título, prazo, descrição gerada ou etapa interrompem o complemento para preservar o trabalho no Trello. Esta POC não tem fila de tarefas: se o processo morrer no meio de uma operação marcada `pending`, é necessária conferência manual antes de liberar o registro.

O histórico registra recebimento, criação, pendências, confirmação, complementos e reenvios reconhecidos. O SQLite preserva o pedido original e os dados atuais de cada item. Reenviar o JSON **original** após um complemento continua retornando os mesmos cards com seus dados atuais. O navegador guarda só o ID do último pedido na sessão; ao recarregar, busca o resultado no backend. Pedidos antigos sem payload salvo continuam preservados, sem complemento automático.

Rotas adicionais:

- `GET /orders/batch/{order_id}`: dados atuais e histórico.
- `POST /orders/batch/{order_id}/sync`: conferir a posição real no Trello.
- `POST /orders/batch/{order_id}/items/{item_id}/suggest`: extrair uma resposta para campos pendentes.
- `POST /orders/batch/{order_id}/items/{item_id}/preview`: prévia de um complemento.
- `PATCH /orders/batch/{order_id}/items/{item_id}`: confirmar `{revision, changes}` no mesmo card.
- `GET /demo/cutoff?product_code=dtf_uv`: comparar 15:59 e 16:00 sem criar cards.

## Regras da demonstração

As fontes são referências do processo, não instruções para executar ações na empresa.

- `reference_at` representa **aprovação final da arte**, com pagamento/informações já resolvidos para este cenário. A página usa horário de São Paulo; a API exige data com fuso.
- PDF `Stamp_Ideias_Prazos.pdf`, página 3: produção só começa depois de aprovar a arte. Por isso são usados **prazos de produção**, sem somar novamente a etapa de arte.
- Página 2: DTF UV 1 dia; DTF Têxtil 1–2; metragem sublimada 4–5; windflags/bandeiras 5–7; camisetas sublimadas 8–10. A POC usa o **limite superior** de cada faixa: 1/2/5/7/10. Esses valores ficam em `rules.py`.
- Às **16:00 ou depois**, avançamos a data-base para o próximo dia útil. O prazo é somado à data-base (dia zero). Ex.: quinta 15:59 +1 → sexta; quinta 16:00 +1 → segunda. Sexta 16:00 +1 → terça.
- Segunda a sexta. Não presumimos feriados: configure `HOLIDAYS=2026-11-02,2026-11-20` etc. no `.env` conforme o calendário da empresa.
- Vencimento às **18h** é uma convenção visual da POC, não um horário confirmado pela empresa.
- Inclusão exata das 16h, dia zero, limite superior e horário de vencimento são escolhas explícitas da POC a confirmar antes de uso real.
- Metragens acima de 100 m são bloqueadas para consulta à produção (regra da referência). Produtos em unidades exigem quantidade inteira.
- Cliente, quantidade, medidas/especificações, contato/protocolo, arquivo da arte ou aprovação ausentes tornam o item **engatilhado**. Ainda é possível criar o card com seu modelo, na coluna `ENGATILHADOS`, com lista das pendências e **sem vencimento**. Uma lacuna de um item não bloqueia os demais. O código decide isso; a IA não escolhe colunas nem prazos.
- Ao completar o último dado pendente, a POC guarda `ready_at` no servidor. O prazo parte do horário mais recente entre a aprovação e o recebimento de todas as informações, aplicando o mesmo corte das 16h. A aprovação original é preservada. Essa é uma regra explícita da demonstração, a confirmar com a empresa; evita prometer produção antes de receber os dados necessários. Respostas parciais não iniciam prazo.
- Seis produtos demonstram os dois fluxos. Outros modelos, urgências, comissões e prazos duplos de DTF+camiseta precisam de regras próprias antes de serem adicionados. Não há fórmula de comissão inventada.

## Trello de teste

- Configure dois quadros privados de teste, um Digital e um Sublimação, com as listas e os modelos usados em `app/rules.py` e `app/production_rules.py`. O repositório contém o código; os quadros, modelos, capas e dados reais permanecem externos.
- Os modelos ficam em `CARTÕES MODELO`. `TRELLO_TEMPLATE_IDS` seleciona o card exato por produto, mesmo quando títulos de modelos são iguais.
- A página cria itens completos na coluna de lançamento e itens incompletos em `ENGATILHADOS`. O complemento pode liberar automaticamente o mesmo card para a coluna de lançamento. DTF UV e Windflag também podem avançar pelas três confirmações de produção descritas abaixo. Demais produtos e etapas continuam com a equipe.
- O caminho da arte é texto na descrição. Esta versão não transfere arquivos do servidor da empresa nem anexa uma imagem inexistente.

## Confirmações de produção

Pedidos novos com DTF UV ou Windflag recebem o checklist **Etapas de produção — Estampi POC** no próprio card. A equipe marca o que aconteceu no Trello ou pelo botão **Etapas** da demo. O backend observa as confirmações a cada 10 segundos e aplica no máximo uma transição por ciclo:

| Confirmação | DTF UV | Windflag |
|---|---|---|
| Arquivo liberado para impressão | PRONTO PARA IMPRESSÃO UV | PRONTO PARA IMPRESSÃO |
| Impressão iniciada | EM PRODUÇÃO UV | SENDO IMPRESSO |
| Impressão concluída | CONFERENCIA UV | CALANDRANDO |

As rotas partem das respectivas colunas de lançamento. Esta confirmação explícita informa ao sistema que o trabalho físico ocorreu; não há detecção de impressora ou leitura automática da arte. Nenhum prazo vencido ou tempo decorrido faz o card avançar. Concluir as três confirmações termina somente esta parte da automação; não declara o pedido entregue nem marca o vencimento como concluído.

O servidor precisa estar aberto, com conexão válida ao Trello. A automação continua com a página fechada. A página consulta somente os resultados locais a cada 3 segundos para atualizar posição, confirmações e histórico. Ao reconectar, o watcher volta a observar os cards registrados. Use **um único processo Uvicorn** nesta POC; a exclusão entre ações usa um lock do processo. `PRODUCTION_AUTOMATION=false` desliga o watcher após reiniciar o servidor.

Somente cards registrados em SQLite, criados pela app e identificados pelo pedido/item/quadro exatos são observados. Cards antigos com payload salvo podem aderir pelo botão **Etapas → Ativar etapas**. Nenhum card de modelo é alterado e não há varredura de pedidos de produção. Os IDs configurados em `TRELLO_BLOCKED_BOARD_IDS` são recusados antes de acessar sua API.

Informações ausentes ou confirmações fora de ordem bloqueiam o avanço. Uma mudança manual de etapa, título, descrição, prazo, status de conclusão ou checklist pausa o acompanhamento para revisão. Desmarcar uma etapa já processada não move o card para trás. **Pausar automação** e **Retomar automação** estão no diálogo; retomar exige que o card esteja na última etapa confirmada. Revisar uma mudança manual não altera dados externos automaticamente.

Cada criação de checklist/item e movimento tem intenção persistida. Se uma resposta se perder, a app consulta o estado existente antes de continuar. Uma criação incerta sem objeto correspondente permanece pendente para conferência, sem repetir o POST. Movimentos alteram somente `idList`: título, descrição do modelo, etiquetas, capa, anexos e prazo são preservados. O histórico registra uma única vez cada transição confirmada.

Rotas de controle por item:

- `POST /orders/batch/{order_id}/items/{item_id}/production/enable`
- `POST /orders/batch/{order_id}/items/{item_id}/production/pause`
- `POST /orders/batch/{order_id}/items/{item_id}/production/resume`
- `PATCH /orders/batch/{order_id}/items/{item_id}/production/checks/{step}`, `step` entre 0 e 2, com `{"checked": true}`. Essa rota confirma o checklist; o watcher realiza e verifica a movimentação.

Referências da API: [checklists](https://developer.atlassian.com/cloud/trello/rest/api-group-checklists/), [cards](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/).

## Testes

```powershell
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m ruff check app tests scripts
.\venv\Scripts\python.exe -m ruff format --check app tests scripts
```

Testes cobrem corte, fuso, fim de semana, feriado, roteamento, pendências sem vencimento, lotes mistos, cópia de modelos, links seguros entre itens, retomada parcial, duplicidade, saída estruturada da IA, migração sem perda, revisões, proteção contra mudanças externas, checklists, rotas de produção, watcher sem navegador e recuperação de respostas perdidas, sem chamadas externas.

Para apresentar: use **Usar exemplo → Organizar com IA** (com chave) ou **Exemplo manual**. A mensagem informal mostra um DTF UV pronto no Digital e um windflag sem medidas em Engatilhados no Sublimação. Revise e crie. No windflag, clique em **Completar**, responda `a medida é 2,70 x 0,70 m`, organize e confirme: o mesmo card ganha prazo e vai ao lançamento. Abra **Histórico**, **Corte das 16h** e **Reenviar pedido** para mostrar as decisões e a proteção contra duplicatas. **Conferir Trello** lê o estado real. A fonte CRM/ERP está representada pelo texto/formulário e seu JSON; não há integração com software da empresa.

Mantenha o servidor em `127.0.0.1`. É uma POC local sem autenticação de usuários, sem implantação de produção. `.env` e SQLite estão no `.gitignore`.
