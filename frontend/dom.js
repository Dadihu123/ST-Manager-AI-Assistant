/**
 * 轻量 DOM 辅助：插件 UI 全部用原生 DOM 构建（不依赖主程序模板或 Alpine）。
 */

export function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function button(label, className = '', onClick = null) {
  const node = el('button', `plugin-stm-ai-assistant-btn ${className}`.trim(), label);
  node.type = 'button';
  node.dataset.marqueeIgnore = 'true';
  if (typeof onClick === 'function') node.addEventListener('click', onClick);
  return node;
}

export function field(labelText, control, hint = '') {
  const wrapper = el('label', 'plugin-stm-ai-assistant-field');
  wrapper.append(el('span', 'plugin-stm-ai-assistant-field-label', labelText));
  wrapper.append(control);
  if (hint) wrapper.append(el('span', 'plugin-stm-ai-assistant-field-hint', hint));
  return wrapper;
}

export function input(value = '', type = 'text', placeholder = '') {
  const node = el('input', 'plugin-stm-ai-assistant-input');
  node.type = type;
  node.value = value == null ? '' : String(value);
  if (placeholder) node.placeholder = placeholder;
  node.dataset.marqueeIgnore = 'true';
  return node;
}

export function textarea(value = '', rows = 6) {
  const node = el('textarea', 'plugin-stm-ai-assistant-textarea');
  node.rows = rows;
  node.value = value == null ? '' : String(value);
  node.dataset.marqueeIgnore = 'true';
  return node;
}

export function select(options, value, onChange = null) {
  const node = el('select', 'plugin-stm-ai-assistant-select');
  node.dataset.marqueeIgnore = 'true';

  (options || []).forEach((option) => {
    const item = el('option', '', option.label);
    item.value = String(option.value);
    if (String(option.value) === String(value)) item.selected = true;
    node.append(item);
  });

  if (typeof onChange === 'function') node.addEventListener('change', onChange);
  return node;
}

export function checkbox(checked, labelText, onChange = null) {
  const wrapper = el('label', 'plugin-stm-ai-assistant-checkbox');
  const node = el('input');
  node.type = 'checkbox';
  node.checked = Boolean(checked);
  node.dataset.marqueeIgnore = 'true';
  if (typeof onChange === 'function') node.addEventListener('change', onChange);
  wrapper.append(node, el('span', '', labelText));
  return wrapper;
}

export function section(title, subtitle = '') {
  const wrapper = el('section', 'plugin-stm-ai-assistant-section');
  wrapper.append(el('h4', 'plugin-stm-ai-assistant-section-title', title));
  if (subtitle) wrapper.append(el('p', 'plugin-stm-ai-assistant-section-subtitle', subtitle));
  return wrapper;
}

export function badge(text, tone = '') {
  return el('span', `plugin-stm-ai-assistant-badge ${tone ? `is-${tone}` : ''}`.trim(), text);
}

export function formatTime(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return '-';
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleString('zh-CN');
}

export function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '0';
  return number.toLocaleString('zh-CN');
}

export function formatCost(cost) {
  if (!cost || !cost.priced) return '（未配置单价）';
  return `${cost.total} ${cost.currency || ''}`.trim();
}
