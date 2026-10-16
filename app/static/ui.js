export const $ = selector => document.querySelector(selector);
export const text = (selector, value) => { $(selector).textContent = value; };

export function element(tag, content = '', className = '') {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}

export async function api(path, payload, { method = payload === undefined ? 'GET' : 'POST', signal } = {}) {
  const response = await fetch(path, {
    method, signal,
    ...(payload === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  });
  let body;
  try { body = await response.json(); }
  catch { throw new Error('A resposta não foi confirmada. Confira o Trello antes de reenviar.'); }
  if (!response.ok) {
    const detail = body.detail;
    const message = Array.isArray(detail)
      ? detail.map(error => `${error.loc.filter(part => part !== 'body').join('.')}: ${error.msg}`).join(' · ')
      : detail || 'Não foi possível concluir a operação.';
    const error = new Error(message);
    error.body = body; error.status = response.status; throw error;
  }
  return body;
}

export function localTime(value) {
  if (!value) return '';
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'America/Sao_Paulo', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  }).format(new Date(value)).replace(' ', 'T');
}

export const isoTime = value => value ? `${value}:00-03:00` : null;
export const dateTime = value => new Intl.DateTimeFormat('pt-BR', {
  timeZone: 'America/Sao_Paulo', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
}).format(new Date(value));

export function safeCardLink(url, label = 'Abrir ↗') {
  const parsed = new URL(url);
  if (parsed.protocol !== 'https:' || parsed.hostname !== 'trello.com') throw new Error('Link do card inválido. Confira o quadro.');
  const link = element('a', label);
  link.href = parsed.href; link.target = '_blank'; link.rel = 'noopener'; return link;
}

export function currentCardList(item) {
  const flow = item.production;
  const verification = item.verification;
  if (flow?.current_list && new Date(flow.checked_at || 0) > new Date(verification?.checked_at || 0)) return flow.current_list;
  return verification?.list_name || flow?.current_list || item.preview.list_name;
}
