import { $, api, dateTime, element, text } from './ui.js?v=9';

document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));

export function showHistory(events) {
  $('#activity-list').replaceChildren(...events.map(event => {
    const item = element('li', '', 'activity-event');
    const time = element('time', new Intl.DateTimeFormat('pt-BR', { timeZone: 'America/Sao_Paulo', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(event.occurred_at)));
    time.dateTime = event.occurred_at;
    item.append(time, element('p', event.message)); return item;
  }));
  $('#history-dialog').showModal();
}

export async function showCutoff(productCode) {
  const result = await api(`/demo/cutoff?product_code=${encodeURIComponent(productCode)}`);
  text('#cutoff-product', `${result.product} · ${result.business_days} ${result.business_days === 1 ? 'dia útil' : 'dias úteis'}`);
  text('#cutoff-before', dateTime(result.before.due_at));
  text('#cutoff-after', dateTime(result.after.due_at));
  text('#cutoff-before-rule', result.before.explanation);
  text('#cutoff-after-rule', result.after.explanation);
  $('#cutoff-dialog').showModal();
}
