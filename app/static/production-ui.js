import { $, api, currentCardList, dateTime, element, safeCardLink, text } from './ui.js?v=9';

const stateLabels = { setup: 'Preparando checklist', active: 'Acompanhando Trello', blocked: 'Aguardando confirmação', paused: 'Automação pausada', finished: 'Etapas confirmadas' };

export function createProduction({ canWrite, onUpdated }) {
  const dialog = $('#production-dialog');
  let context = null;
  let busy = false;
  let note = '';
  let waitingStep = null;
  let checkedAt = null;

  function render() {
    if (!context) return;
    const { item } = context;
    const flow = item.production;
    text('#production-title', `Produção · ${item.preview.product_name}`);
    text('#production-current', currentCardList(item));
    text('#production-status', flow ? stateLabels[flow.status] : 'Checklist disponível');
    text('#production-intro', flow?.status === 'finished'
      ? 'As três confirmações foram processadas. As próximas etapas seguem com a equipe de produção.'
      : 'Confirme somente o que já aconteceu. O card avança automaticamente para a próxima etapa.');
    text('#production-last-check', flow?.checked_at ? `Última checagem: ${dateTime(flow.checked_at)}` : 'Acompanhamento a cada 10 segundos enquanto o servidor estiver aberto.');
    text('#production-message', note || flow?.error || (!canWrite() ? 'Conecte o Trello para registrar confirmações.' : 'Você também pode marcar este checklist diretamente no Trello.'));
    $('#production-steps').replaceChildren(...(flow?.steps || []).map((step, index) => {
      const label = element('label', '', 'production-step');
      const input = element('input'); input.type = 'checkbox'; input.checked = step.checked || waitingStep === index;
      input.disabled = busy || !canWrite() || waitingStep !== null || flow.status !== 'active' || index !== flow.step;
      input.setAttribute('aria-label', step.label);
      input.addEventListener('change', () => { if (input.checked) run('confirm', index); });
      const description = element('span', '', 'production-step-text');
      description.append(element('strong', step.label), element('small', `→ ${step.target}`));
      label.append(input, description); return label;
    }));
    $('#production-toggle').hidden = !flow || flow.status === 'finished';
    $('#production-toggle').disabled = busy || !canWrite() || waitingStep !== null;
    text('#production-toggle', flow?.status === 'paused' ? 'Retomar automação' : 'Pausar automação');
    $('#production-enable').hidden = Boolean(flow);
    $('#production-enable').disabled = busy || !canWrite();
    $('#production-close').disabled = busy;
    $('#production-link').replaceChildren(safeCardLink(item.card.url, 'Abrir card no Trello ↗'));
  }

  async function run(action, index) {
    if (busy || !context || !canWrite()) return;
    const orderId = context.orderId;
    const itemId = context.item.preview.item_id;
    busy = true; checkedAt = context.item.production?.checked_at;
    if (action === 'confirm') waitingStep = index;
    note = action === 'confirm' ? 'Registrando a confirmação no Trello…' : 'Atualizando o acompanhamento…'; render();
    try {
      const suffix = action === 'confirm' ? `checks/${index}` : action;
      const result = await api(`/orders/batch/${encodeURIComponent(orderId)}/items/${encodeURIComponent(itemId)}/production/${suffix}`,
        action === 'confirm' ? { checked: true } : {}, { method: action === 'confirm' ? 'PATCH' : 'POST' });
      context.item = result.cards.find(item => item.preview.item_id === itemId);
      note = action === 'confirm' ? 'Confirmação registrada. Aguardando a mudança automática…' : '';
      onUpdated(result);
    } catch (error) {
      note = error.message;
      if (!error.body?.uncertain) waitingStep = null;
    } finally { busy = false; render(); }
  }

  $('#production-enable').addEventListener('click', () => run('enable'));
  $('#production-toggle').addEventListener('click', () => run(context.item.production.status === 'paused' ? 'resume' : 'pause'));
  $('#production-close').addEventListener('click', () => { if (!busy) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });

  return {
    open(orderId, item) {
      context = { orderId, item }; note = ''; busy = false; waitingStep = null;
      render(); dialog.showModal();
    },
    refresh(result) {
      if (!dialog.open || !context || busy || !result || context.orderId !== result.order_id) return;
      const item = result.cards.find(item => item.preview.item_id === context.item.preview.item_id);
      if (!item) return;
      if (waitingStep !== null && (item.production?.step > waitingStep || item.production?.checked_at !== checkedAt)) {
        waitingStep = null; note = '';
      }
      context.item = item; render();
    }
  };
}
