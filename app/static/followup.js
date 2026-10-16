import { $, api, dateTime, element, isoTime, localTime, text } from './ui.js?v=9';

const fields = {
  dimensions: { label: 'Medidas / especificações', type: 'text', max: 200, question: 'Qual a medida deste produto?', placeholder: 'Ex.: 2,70 × 0,70 m' },
  quantity: { label: 'Quantidade', type: 'number', question: 'Qual a quantidade deste produto?' },
  customer_name: { label: 'Cliente', type: 'text', max: 160, question: 'Qual é o nome do cliente?' },
  customer_contact: { label: 'Contato / protocolo', type: 'text', max: 200, question: 'Quem é o contato ou qual é o protocolo do pedido?' },
  artwork_path: { label: 'Arquivo da arte', type: 'text', max: 600, question: 'Qual é o arquivo da arte?' },
  reference_at: { label: 'Arte aprovada em', type: 'datetime-local', question: 'Quando a arte foi aprovada? Informe a data e a hora.' }
};

export function createFollowup({ aiAvailable, onSaved }) {
  const dialog = $('#followup-dialog');
  const form = $('#completion-form');
  let context = null;
  let busy = false;
  let parsing = false;
  let version = 0;
  let timer;
  let controller;
  let parseController;
  let retryRequest = null;
  let previewValid = false;

  const path = () => `/orders/batch/${encodeURIComponent(context.orderId)}/items/${encodeURIComponent(context.item.preview.item_id)}`;
  function changes() {
    return Object.fromEntries([...$('#completion-fields').querySelectorAll('input')].filter(input => input.value.trim()).map(input => [
      input.name, input.type === 'number' ? Number(input.value) : input.type === 'datetime-local' ? isoTime(input.value) : input.value.trim()
    ]));
  }
  function message(value) { text('#followup-message', value); }
  function buttons() {
    $('#followup-parse').disabled = busy || parsing || !aiAvailable() || !$('#followup-answer').value.trim() || Boolean(retryRequest);
    $('#followup-save').disabled = busy || parsing || (!retryRequest && !previewValid);
    $('#followup-close').disabled = busy;
    $('#completion-fields').disabled = busy || Boolean(retryRequest);
    $('#followup-answer').disabled = busy || Boolean(retryRequest);
  }
  async function preview() {
    clearTimeout(timer); controller?.abort(); previewValid = false;
    const values = changes();
    if (!Object.keys(values).length || !form.checkValidity()) { text('#completion-target', 'Preencha ou organize uma resposta para continuar.'); buttons(); return; }
    const signature = JSON.stringify(values);
    const generation = version;
    const requestController = new AbortController(); controller = requestController;
    buttons();
    try {
      const result = await api(`${path()}/preview`, { revision: context.item.revision, changes: values }, { signal: requestController.signal });
      if (generation !== version || signature !== JSON.stringify(changes()) || !dialog.open) return;
      const card = result.preview;
      text('#completion-target', card.status === 'ready'
        ? `Mesmo card → ${card.list_name}. Entrega: ${dateTime(card.deadline.due_at)}.`
        : `Continua em ENGATILHADOS. Ainda faltam: ${card.missing_fields.join(', ')}.`);
      text('#completion-rule', card.deadline?.explanation || 'O prazo só começa quando todas as informações estiverem completas.');
      text('#followup-save', card.status === 'ready' ? 'Atualizar e liberar card' : 'Salvar informações');
      previewValid = true; buttons();
    } catch (error) { if (error.name !== 'AbortError') { message(error.message); buttons(); } }
  }
  form.addEventListener('input', event => {
    version += 1;
    if (event.target.id !== 'followup-answer') { previewValid = false; clearTimeout(timer); timer = setTimeout(preview, 180); }
    buttons();
  });
  $('#followup-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    version += 1; clearTimeout(timer); controller?.abort(); parseController?.abort();
    parseController = null; parsing = false;
  });

  $('#followup-parse').addEventListener('click', async () => {
    const generation = version;
    const requestController = new AbortController(); parseController = requestController;
    parsing = true; buttons(); message('Organizando a resposta…');
    try {
      const result = await api(`${path()}/suggest`, { revision: context.item.revision, text: $('#followup-answer').value }, { signal: requestController.signal });
      if (generation !== version || !dialog.open) return;
      for (const [key, value] of Object.entries(result.changes)) {
        const input = form.elements[key];
        if (input) input.value = key === 'reference_at' ? localTime(value) : value;
      }
      message(result.warnings.join(' ') || (Object.keys(result.changes).length ? 'Resposta organizada. Confira os campos e confirme.' : 'Não identifiquei dados suficientes. Complete os campos abaixo.'));
      await preview();
    } catch (error) { if (error.name !== 'AbortError' && generation === version) message(error.message); }
    finally { if (parseController === requestController) { parseController = null; parsing = false; buttons(); } }
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || parsing || (!previewValid && !retryRequest)) return;
    const request = retryRequest || { revision: context.item.revision, changes: changes() };
    busy = true; buttons(); message('Atualizando o mesmo card no Trello…');
    try {
      const result = await api(path(), request, { method: 'PATCH' });
      retryRequest = null; dialog.close(); onSaved(result);
    } catch (error) {
      if (error.body?.uncertain || !error.status) {
        retryRequest = request;
        message('A resposta não foi confirmada. Confirme esta mesma atualização; nenhum novo card será criado.');
        text('#followup-save', 'Confirmar atualização');
      } else { message(error.message); }
    } finally { busy = false; buttons(); }
  });

  return {
    open(orderId, item) {
      controller?.abort(); parseController?.abort(); clearTimeout(timer);
      context = { orderId, item }; version += 1; retryRequest = null; previewValid = false; busy = false; parsing = false;
      form.reset(); $('#completion-fields').replaceChildren();
      text('#followup-title', `Completar ${item.preview.product_name}`);
      const missing = item.preview.missing_keys || Object.keys(fields).filter(key => item.details[key] === null || item.details[key] === '');
      const questionKey = ['dimensions', 'quantity', 'customer_name', 'customer_contact', 'artwork_path', 'reference_at'].find(key => missing.includes(key));
      text('#followup-question', fields[questionKey]?.question || 'Confira os dados pendentes.');
      for (const key of missing) {
        const config = fields[key];
        const label = element('label', config.label);
        const input = element('input'); input.name = key; input.type = config.type;
        if (config.max) input.maxLength = config.max;
        input.placeholder = config.placeholder || '';
        if (key === 'quantity') {
          const meters = ['dtf_textil', 'sublimacao_metragem'].includes(item.details.product_code);
          input.step = meters ? '0.01' : '1'; input.min = meters ? '0.01' : '1'; input.max = meters ? '100' : '100000';
        }
        label.append(input); $('#completion-fields').append(label);
      }
      message(aiAvailable() ? 'Responda em texto ou preencha os campos.' : 'Preencha os campos manualmente.');
      text('#completion-target', 'Preencha ou organize uma resposta para continuar.'); text('#completion-rule', '');
      text('#followup-save', 'Atualizar card');
      if (item.pending_update?.state === 'unknown') {
        retryRequest = { revision: item.pending_update.revision, changes: item.pending_update.changes };
        for (const [key, value] of Object.entries(retryRequest.changes)) if (form.elements[key]) form.elements[key].value = key === 'reference_at' ? localTime(value) : value;
        message('A atualização anterior ficou sem confirmação. Vamos conferir os mesmos dados antes de continuar.');
        text('#followup-save', 'Confirmar atualização');
      }
      dialog.showModal(); buttons();
    }
  };
}
