import { $, api, currentCardList, dateTime, element, isoTime, localTime, safeCardLink, text } from './ui.js?v=9';
import { createFollowup } from './followup.js?v=9';
import { showCutoff, showHistory } from './demo-ui.js?v=9';
import { createProduction } from './production-ui.js?v=9';
const form = $('#order-form');
const commonFields = ['order_id', 'customer_name', 'customer_contact', 'artwork_path', 'reference_at', 'notes'];
let products = [];
let configured = false;
let aiConfigured = false;
let parsing = false;
let sending = false;
let locked = false;
let nextItemId = 1;
let revision = 0;
let previewedOrder = null;
let retryOrder = null;
let previewTimer;
let previewController;
let submittedResult = null;
let submittedOrder = null;
let actionBusy = false;
let automationEnabled = false;
let pollingOrder = false;
const followup = createFollowup({ aiAvailable: () => aiConfigured, onSaved: applyBatchResult });
const production = createProduction({ canWrite: () => configured && automationEnabled, onUpdated: applyBatchResult });

const itemNodes = () => [...$('#items').children];
const commonInput = name => form.querySelector(`[data-common][name="${name}"]`);
const newOrderId = () => `DEMO-${Math.random().toString(36).slice(2, 8).toUpperCase()}`;

function showError(message) { text('#feedback', message); $('#feedback').hidden = false; }
function clearError() { $('#feedback').hidden = true; }
function intakeNote(message, warning = false) {
  text('#intake-feedback', message); $('#intake-feedback').hidden = !message;
  $('#intake-feedback').classList.toggle('warning', warning);
}
function fitMessage() {
  const field = $('#intake-text');
  field.style.height = 'auto';
  field.style.height = `${Math.max(88, Math.min(230, field.scrollHeight + 2))}px`;
}

function updateButtons() {
  $('#parse-button').disabled = !aiConfigured || parsing || locked || sending || !$('#intake-text').value.trim();
  $('#example').disabled = locked || sending || !products.length;
  $('#text-example').disabled = locked || sending;
  $('#intake-text').disabled = locked || sending;
  $('#add-item').disabled = locked || sending || itemNodes().length >= 20;
  $('#send-button').disabled = !configured || sending || parsing || (!previewedOrder && !retryOrder);
  const order = retryOrder || previewedOrder;
  text('#send-button', sending ? 'Criando no Trello…' : !configured ? 'Conecte o Trello para criar' : retryOrder ? 'Continuar envio' : order ? `Criar ${order.items.length === 1 ? 'card' : `${order.items.length} cards`} no Trello` : 'Criar no Trello');
  $('#sync-button').disabled = !configured || actionBusy;
  $('#replay-button').disabled = !configured || actionBusy || !submittedOrder;
  $('#new-order').disabled = actionBusy || sending;
}

function productChanged(node) {
  const product = products.find(item => item.code === node.querySelector('[name="product_code"]').value);
  node.querySelector('.unit').textContent = product ? `(${product.unit})` : '';
  const quantity = node.querySelector('[name="quantity"]');
  quantity.step = product?.unit === 'M' ? '0.01' : '1';
  quantity.min = product?.unit === 'M' ? '0.01' : '1';
  quantity.max = product?.unit === 'M' ? '100' : '100000';
}

function addItem(data = {}) {
  const node = $('#item-template').content.firstElementChild.cloneNode(true);
  node.dataset.itemId = data.item_id || String(nextItemId++);
  nextItemId = Math.max(nextItemId, Number(node.dataset.itemId) + 1 || nextItemId);
  const select = node.querySelector('select');
  const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Selecione o produto';
  select.replaceChildren(placeholder, ...products.map(product => {
    const option = document.createElement('option'); option.value = product.code; option.textContent = product.name; return option;
  }));
  for (const name of ['product_code', 'quantity', 'dimensions', 'artwork_path', 'reference_at', 'notes']) {
    node.querySelector(`[name="${name}"]`).value = name === 'reference_at' ? localTime(data[name]) : data[name] ?? '';
  }
  if (data.raw_product && !data.product_code) {
    node.querySelector('.raw-product').textContent = `No texto: ${data.raw_product}. Selecione o produto correspondente.`;
    node.querySelector('.raw-product').hidden = false;
  }
  node.querySelector('.item-extras').open = false;
  select.addEventListener('change', () => { productChanged(node); node.querySelector('.raw-product').hidden = true; });
  node.querySelector('.remove-item').addEventListener('click', () => {
    if (locked || sending || itemNodes().length <= 1) return;
    node.remove(); renumberItems(); changed();
  });
  $('#items').append(node); productChanged(node); renumberItems(); return node;
}

function renumberItems() {
  const nodes = itemNodes();
  text('#item-count', nodes.length);
  nodes.forEach((node, index) => {
    node.querySelector('.item-number').textContent = `ITEM ${String(index + 1).padStart(2, '0')}`;
    node.setAttribute('aria-label', `Produto ${index + 1}`);
    node.querySelector('.remove-item').disabled = nodes.length === 1;
    node.querySelector('.remove-item').setAttribute('aria-label', `Remover produto ${index + 1}`);
  });
  updateButtons();
}

function collectOrder() {
  const order = Object.fromEntries(commonFields.map(name => [name, commonInput(name).value.trim()]));
  order.reference_at = isoTime(order.reference_at);
  order.items = itemNodes().map(node => {
    const get = name => node.querySelector(`[name="${name}"]`).value.trim();
    return {
      item_id: node.dataset.itemId, product_code: get('product_code'),
      quantity: get('quantity') === '' ? null : Number(get('quantity')),
      dimensions: get('dimensions'), artwork_path: get('artwork_path'),
      reference_at: isoTime(get('reference_at')), notes: get('notes')
    };
  });
  return order;
}

function hidePreview() {
  previewedOrder = null;
  $('#preview-content').hidden = true; $('#empty-state').hidden = false;
  text('#preview-badge', 'Prévia');
  updateButtons();
}

function changed() {
  if (locked || sending) return;
  revision += 1; clearError(); retryOrder = null;
  const approval = commonInput('reference_at').value;
  text('#approval-summary', approval ? new Intl.DateTimeFormat('pt-BR', { timeZone: 'America/Sao_Paulo', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(isoTime(approval))) : 'Sem aprovação');
  previewedOrder = null;
  $('#send-button').disabled = true;
  clearTimeout(previewTimer); previewController?.abort();
  if (!form.checkValidity()) {
    hidePreview(); text('#preview-state', 'Confira o número e os produtos.'); return;
  }
  text('#preview-state', 'Atualizando prévia…');
  previewTimer = setTimeout(() => refreshPreview(), 220);
}

async function refreshPreview(explicit = false) {
  clearTimeout(previewTimer);
  if (locked || sending) return;
  if (!form.checkValidity()) { hidePreview(); if (explicit) form.reportValidity(); return; }
  const order = collectOrder();
  const signature = JSON.stringify(order);
  previewController?.abort();
  const controller = new AbortController(); previewController = controller;
  text('#preview-state', 'Atualizando prévia…');
  try {
    const preview = await api('/orders/batch/preview', order, { signal: controller.signal });
    if (JSON.stringify(collectOrder()) !== signature || controller.signal.aborted) return;
    previewedOrder = order; renderPreview(preview, order);
    text('#preview-state', 'Prévia atualizada');
  } catch (error) {
    if (error.name !== 'AbortError' && !controller.signal.aborted) {
      hidePreview(); text('#preview-state', 'Confira os dados do pedido.'); showError(error.message);
    }
  }
}

function renderPreview(preview, order, results = []) {
  $('#empty-state').hidden = true; $('#preview-content').hidden = false;
  $('#preview-cards').replaceChildren();
  text('#preview-summary', `${preview.total} ${preview.total === 1 ? 'card' : 'cards'} · ${preview.ready} ${preview.ready === 1 ? 'pronto' : 'prontos'} · ${preview.drafts} ${preview.drafts === 1 ? 'engatilhado' : 'engatilhados'}`);
  text('#payload', JSON.stringify(submittedOrder || order, null, 2));
  text('#payload-title', submittedOrder ? 'JSON original do pedido' : 'JSON do pedido');
  preview.cards.forEach(card => {
    const node = $('#card-template').content.firstElementChild.cloneNode(true);
    const set = (selector, value) => { node.querySelector(selector).textContent = value; };
    const draft = card.status === 'draft';
    node.classList.toggle('draft', draft);
    set('.product-label', card.product_name); set('.card-title', card.title);
    set('.card-status', draft ? 'Engatilhado' : 'Pronto'); node.querySelector('.card-status').classList.toggle('draft', draft);
    set('.board-name', card.board_key === 'digital' ? 'Digital' : 'Sublimação'); set('.list-name', card.list_name);
    node.querySelector('.board-name').title = card.board_name;
    set('.deadline-label', draft ? 'Prazo' : 'Entrega');
    set('.due-date', draft ? 'Sem prazo definido' : new Intl.DateTimeFormat('pt-BR', {
      timeZone: 'America/Sao_Paulo', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
    }).format(new Date(card.deadline.due_at)));
    set('.cutoff-tag', draft ? '' : card.deadline.cutoff_applied ? 'Corte das 16h aplicado · dias úteis' : 'Antes do corte das 16h · dias úteis');
    node.querySelector('.cutoff-tag').hidden = draft;
    node.querySelector('.missing').hidden = !draft;
    node.querySelector('.missing-list').replaceChildren(...card.missing_fields.map(field => {
      const chip = document.createElement('span'); chip.textContent = field; return chip;
    }));
    set('.template-name', `Modelo: ${card.template_name}`);
    set('.deadline-explanation', card.deadline?.explanation || 'O prazo será definido quando as informações estiverem completas.');
    set('.card-description', card.description);
    const saved = results.find(item => item.preview.item_id === card.item_id);
    const verification = saved?.verification;
    if (saved) {
      const paused = saved.production?.status === 'paused';
      const confirmation = node.querySelector('.trello-confirmation');
      confirmation.hidden = false;
      confirmation.textContent = paused ? 'Automação pausada · confira as etapas' : verification?.confirmed ? `Confirmado no Trello · ${dateTime(verification.checked_at)}`
        : verification?.status === 'different' ? `Trello agora: ${verification.list_name} · posição alterada`
          : 'Card criado. A posição ainda não foi confirmada.';
      try { confirmation.append(safeCardLink(saved.card.url)); } catch { /* Keep the confirmed details if a link is invalid. */ }
      confirmation.classList.toggle('confirmed', Boolean(verification?.confirmed) && !paused);
      set('.list-name', currentCardList(saved));
      if (saved.details && ['dtf_uv', 'windflag'].includes(card.product_code)) {
        const button = element('button', paused ? 'Etapas pausadas' : saved.production ? `Etapas ${saved.production.step}/3` : 'Etapas', 'button quiet small production-open');
        button.type = 'button'; button.setAttribute('aria-label', `Etapas de ${card.product_name}`);
        button.addEventListener('click', () => production.open(submittedResult.order_id, saved));
        node.querySelector('.card-top').insertBefore(button, node.querySelector('.card-status'));
      }
      if (draft && saved.details && saved.revision) {
        const button = element('button', saved.pending_update ? 'Confirmar' : 'Completar', 'button secondary small complete-item');
        button.type = 'button'; button.setAttribute('aria-label', `Completar ${card.product_name}`);
        button.addEventListener('click', () => followup.open(submittedResult.order_id, saved));
        node.querySelector('.missing').append(button);
      }
    }
    $('#preview-cards').append(node);
    const item = itemNodes().find(item => item.dataset.itemId === card.item_id);
    if (item) {
      item.querySelector('.item-status').textContent = draft ? 'Engatilhado' : 'Pronto';
      item.querySelector('.item-status').classList.toggle('draft', draft);
      item.querySelector('[name="dimensions"]').classList.toggle('pending', !order.items.find(i => i.item_id === card.item_id).dimensions);
      item.querySelector('[name="quantity"]').classList.toggle('pending', order.items.find(i => i.item_id === card.item_id).quantity === null);
    }
  });
  for (const name of ['customer_name', 'customer_contact', 'artwork_path', 'reference_at']) {
    const missing = !order[name] && (name === 'artwork_path' || name === 'reference_at' ? order.items.some(item => !item[name]) : true);
    commonInput(name).classList.toggle('pending', missing);
  }
  text('#send-hint', preview.drafts ? `${preview.drafts === 1 ? 'O item incompleto vai' : 'Os itens incompletos vão'} para Engatilhados.` : 'Cada produto usa seu modelo e prazo.');
  $('#send-area').hidden = false; updateButtons();
}

function fillOrder(draft) {
  const previousId = commonInput('order_id').value || newOrderId();
  commonFields.forEach(name => { commonInput(name).value = name === 'reference_at' ? localTime(draft[name]) : draft[name] || (name === 'order_id' ? previousId : ''); });
  $('#items').replaceChildren(); nextItemId = 1;
  draft.items.forEach(addItem); $('#send-result').hidden = true;
  changed();
}

function exampleText() {
  return `opa, Ana da Aurora aqui\n100 dtf uv 10x15cm e 2 windflags, a medida deles te confirmo depois\naprovei as duas artes hoje às 15:59. arquivo pedido-aurora.pdf`;
}

function manualExample() {
  intakeNote('Exemplo fictício preenchido. Você pode editar os campos e conferir a prévia.');
  fillOrder({
    order_id: newOrderId(), customer_name: 'Aurora Studio', customer_contact: 'Ana · MZ-DEMO',
    artwork_path: '\\\\servidor-demo\\artes\\pedido.pdf', reference_at: '2026-10-15T15:59:00-03:00',
    notes: 'Pedido fictício para demonstração. Não produzir.',
    items: [
      { item_id: '1', product_code: 'dtf_uv', quantity: 100, dimensions: '10 × 15 cm' },
      { item_id: '2', product_code: 'windflag', quantity: 2, dimensions: '' }
    ]
  });
}

$('#parse-button').addEventListener('click', async () => {
  if (parsing || locked || sending) return;
  const input = $('#intake-text').value.trim(); if (!input) return;
  parsing = true; updateButtons(); clearError();
  const startRevision = revision;
  text('#parse-button .ai-button-label', 'Organizando…'); intakeNote('Lendo produtos, quantidades e informações do pedido…');
  try {
    const result = await api('/intake/parse', { text: input });
    if (startRevision !== revision) { intakeNote('Você editou o pedido durante a leitura. Seus campos foram mantidos; organize novamente se quiser.', true); return; }
    fillOrder(result.draft);
    commonInput('order_id').title = result.draft.order_id ? 'Número informado na mensagem.' : 'Identificador local da demo; não veio da mensagem.';
    intakeNote(`${result.draft.items.length} ${result.draft.items.length === 1 ? 'produto organizado' : 'produtos organizados'}. Confira os dados.${result.warnings.length ? ` ${result.warnings.join(' ')}` : ''}`, Boolean(result.warnings.length));
  } catch (error) { intakeNote(error.message, true); }
  finally { parsing = false; text('#parse-button .ai-button-label', 'Organizar com IA'); updateButtons(); }
});

function renderResult(result, message, uncertain = false) {
  $('#send-result').hidden = false; text('#result-title', message);
  $('#result-links').replaceChildren();
  (result.status === 'sent' ? [] : result.cards || []).forEach(item => {
    const row = document.createElement('div'); row.className = 'result-link';
    const name = document.createElement('span');
    const board = item.preview.board_key === 'digital' ? 'Digital' : 'Sublimação';
    name.textContent = `${item.preview.product_name} · ${board} → ${item.verification?.list_name || item.preview.list_name}`;
    let link;
    try {
      link = safeCardLink(item.card.url);
    } catch { link = element('span', 'Confira o quadro'); }
    row.append(name, link); $('#result-links').append(row);
  });
  text('#result-note', uncertain ? 'A resposta ficou sem confirmação. Confira o Trello antes de qualquer novo envio.' : (result.warnings || []).join(' ') || (result.cards?.length > 1 ? 'Os cards compartilham o número do pedido e os links entre os itens.' : 'Card criado no seu quadro de teste.'));
  $('#result-note').hidden = result.status === 'sent' && !(result.warnings || []).length;
  $('#new-order').hidden = uncertain || Boolean(retryOrder);
  $('#result-tools').hidden = result.status !== 'sent';
}

function applyBatchResult(result) {
  submittedResult = result; submittedOrder = result.request || submittedOrder;
  sessionStorage.setItem('estampi_active_order', result.order_id);
  clearTimeout(previewTimer); previewController?.abort();
  locked = true; $('#order-fields').disabled = true;
  if (!$('#intake-text').value) $('#intake-text').placeholder = 'Pedido recuperado. Os dados e os cards estão abaixo.';
  for (const item of result.cards) {
    const node = itemNodes().find(node => node.dataset.itemId === item.preview.item_id);
    if (!node || !item.details) continue;
    for (const key of ['quantity', 'dimensions', 'artwork_path', 'reference_at', 'notes']) {
      node.querySelector(`[name="${key}"]`).value = key === 'reference_at' ? localTime(item.details[key]) : item.details[key] ?? '';
    }
  }
  const cards = result.cards.map(item => item.preview);
  renderPreview({ cards, total: cards.length, ready: cards.filter(card => card.status === 'ready').length,
    drafts: cards.filter(card => card.status === 'draft').length }, collectOrder(), result.cards);
  previewedOrder = null; retryOrder = null; $('#send-area').hidden = true;
  text('#preview-state', 'Pedido salvo');
  const confirmed = result.cards.every(item => item.verification?.confirmed);
  text('#preview-badge', result.cards.some(item => item.production?.status === 'paused') ? 'Revisar etapas' : confirmed ? 'Confirmado' : 'Criado');
  renderResult(result, result.duplicate ? 'Mesmos cards · sem duplicatas' : confirmed ? 'Cards confirmados no Trello' : 'Envio registrado');
  production.refresh(result);
  updateButtons();
}

$('#send-button').addEventListener('click', async () => {
  const order = retryOrder || previewedOrder;
  if (!order || sending || !configured || parsing) return;
  if (!retryOrder && JSON.stringify(collectOrder()) !== JSON.stringify(order)) { changed(); return; }
  clearError(); clearTimeout(previewTimer); previewController?.abort();
  sending = true; locked = true; $('#order-fields').disabled = true;
  updateButtons(); text('#send-button', 'Criando no Trello…');
  try {
    const result = await api('/orders/batch', order);
    applyBatchResult(result);
  } catch (error) {
    showError(error.message);
    const body = error.body || {};
    if (body.result && body.retryable) {
      retryOrder = order; text('#send-button', 'Continuar envio');
      text('#send-hint', 'Os cards confirmados serão mantidos. O envio continua nos itens restantes.');
      renderResult(body.result, body.result.cards.length ? 'Envio parcial' : 'Envio não concluído');
    } else if ([422, 503].includes(error.status)) {
      locked = false; $('#order-fields').disabled = false;
      text('#send-button', 'Tentar novamente');
      await refreshHealth();
    } else {
      retryOrder = null; $('#send-area').hidden = true;
      renderResult(body.result || { cards: [] }, 'Confira o envio no Trello', true);
      text('#preview-badge', 'Sem confirmação');
    }
  } finally { sending = false; updateButtons(); }
});

function resetOrder() {
  locked = false; retryOrder = null; $('#order-fields').disabled = false;
  submittedResult = null; submittedOrder = null; sessionStorage.removeItem('estampi_active_order');
  form.reset(); commonInput('order_id').value = newOrderId();
  text('#approval-summary', 'Sem aprovação');
  $('#items').replaceChildren(); nextItemId = 1; addItem();
  $('#send-result').hidden = true; $('#intake-text').value = ''; intakeNote(''); hidePreview(); clearError();
  $('#intake-text').placeholder = 'Cole a mensagem do cliente aqui…';
  fitMessage();
  revision += 1; text('#preview-state', 'Selecione um produto.');
  commonInput('order_id').focus();
}

form.addEventListener('input', changed);
form.addEventListener('change', changed);
form.addEventListener('submit', event => { event.preventDefault(); clearError(); refreshPreview(true); });
$('#add-item').addEventListener('click', () => { const item = addItem(); changed(); item.querySelector('select').focus(); });
$('#example').addEventListener('click', manualExample);
$('#new-order').addEventListener('click', resetOrder);
$('#sync-button').addEventListener('click', () => resultAction('sync'));
$('#replay-button').addEventListener('click', () => resultAction('replay'));
$('#history-button').addEventListener('click', () => showHistory(submittedResult.activities || []));
$('#cutoff-demo').addEventListener('click', () => showCutoff(itemNodes()[0]?.querySelector('select').value || 'dtf_uv').catch(error => showError(error.message)));
$('#text-example').addEventListener('click', () => { $('#intake-text').value = exampleText(); revision += 1; intakeNote(''); fitMessage(); updateButtons(); $('#intake-text').focus(); });
$('#intake-text').addEventListener('input', () => { revision += 1; fitMessage(); updateButtons(); });

async function refreshHealth() {
  try {
    const health = await api('/health');
    configured = health.trello_configured; aiConfigured = health.ai_configured;
    automationEnabled = health.production_automation;
    text('#connection-status', configured ? 'Trello conectado' : 'Trello desconectado');
    $('#connection-status').classList.toggle('ready', configured);
    $('#connection-status').title = configured && health.token_expires_at ? `Válido até ${new Date(health.token_expires_at * 1000).toLocaleString('pt-BR')}` : '';
    $('#connect-trello').hidden = !health.trello_authorization_available;
    text('#connect-trello', configured ? 'Reconectar' : 'Conectar');
    text('#ai-status', aiConfigured ? 'OpenAI' : 'IA indisponível');
    $('#ai-status').classList.toggle('ready', aiConfigured);
    text('#intake-hint', aiConfigured ? 'Revise os dados depois de organizar.' : 'Preenchimento manual disponível.');
    updateButtons();
    production.refresh(submittedResult);
  } catch { configured = false; text('#connection-status', 'API indisponível'); updateButtons(); }
}

async function resultAction(action) {
  if (!submittedResult || actionBusy) return;
  actionBusy = true; clearError(); updateButtons();
  try {
    const result = action === 'replay' ? await api('/orders/batch', submittedOrder)
      : await api(`/orders/batch/${encodeURIComponent(submittedResult.order_id)}/sync`, {});
    applyBatchResult(result);
  } catch (error) { showError(error.message); }
  finally { actionBusy = false; updateButtons(); }
}

async function start() {
  try {
    products = await api('/products');
    commonInput('order_id').value = newOrderId(); addItem();
    await refreshHealth();
    const activeId = sessionStorage.getItem('estampi_active_order');
    if (activeId) {
      try {
        const saved = await api(`/orders/batch/${encodeURIComponent(activeId)}`);
        if (saved.status === 'sent' && saved.request) { fillOrder(saved.request); applyBatchResult(saved); }
      } catch (error) { if (error.status === 404) sessionStorage.removeItem('estampi_active_order'); else showError(error.message); }
    }
    if (new URLSearchParams(location.search).get('trello') === 'denied') showError('Conexão não autorizada. A prévia continua disponível.');
  } catch (error) { showError(error.message); text('#connection-status', 'API indisponível'); }
}
window.addEventListener('focus', refreshHealth);
window.addEventListener('resize', fitMessage);
setInterval(async () => {
  if (document.hidden || !submittedResult || sending || actionBusy || pollingOrder) return;
  const orderId = submittedResult.order_id;
  pollingOrder = true;
  try {
    const result = await api(`/orders/batch/${encodeURIComponent(orderId)}`);
    if (submittedResult?.order_id === orderId && JSON.stringify(result.cards) !== JSON.stringify(submittedResult.cards)) applyBatchResult(result);
  } catch { /* Keep the last confirmed result; connection health is checked separately. */ }
  finally { pollingOrder = false; }
}, 3000);
setInterval(() => { if (!document.hidden) refreshHealth(); }, 30000);
start();
