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

function selectNodes(root) {
  if (!root) return [];
  const nodes = [];
  if (root.matches && root.matches('.plugin-stm-ai-assistant-select')) nodes.push(root);
  if (root.querySelectorAll) nodes.push(...root.querySelectorAll('.plugin-stm-ai-assistant-select'));
  return nodes;
}

/** 收起指定节点树中的所有自定义下拉菜单，但保留控件以便切回页签后继续使用。 */
export function closeSelectMenus(root) {
  selectNodes(root).forEach((node) => {
    if (typeof node.close === 'function') node.close();
  });
}

/** 销毁指定节点树中的自定义下拉控件，移除 document 级监听器。 */
export function destroySelects(root) {
  selectNodes(root).forEach((node) => {
    if (typeof node.destroy === 'function') node.destroy();
  });
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

/**
 * 原生 DOM 版 styledSelect：复刻主程序 components/styled_select.html 的结构与交互。
 * wrapper = div.plugin-stm-ai-assistant-select，内含 trigger 按钮 + 下拉 menu + 隐藏 <select>。
 * 对外契约：.value 读写 / .setOptions() / .disabled / change 事件（内部 source 冒泡）。
 * 因为返回的是 div 而非 select，调用方用 field() 包它时取 .value 即可。
 */
export function styledSelect(options, value, onChange = null) {
  const wrapper = el('div', 'plugin-stm-ai-assistant-select');
  wrapper.dataset.marqueeIgnore = 'true';

  const trigger = el('button', 'plugin-stm-ai-assistant-select-trigger');
  trigger.type = 'button';
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');

  const label = el('span', 'plugin-stm-ai-assistant-select-label', '请选择');
  const chevron = el('span', 'plugin-stm-ai-assistant-select-chevron');
  chevron.setAttribute('aria-hidden', 'true');
  chevron.append(icon('chevron-down', 'ui-icon--xs'));
  trigger.append(label, chevron);

  const menu = el('div', 'plugin-stm-ai-assistant-select-menu');
  menu.setAttribute('role', 'listbox');
  menu.hidden = true;

  const source = el('select', 'plugin-stm-ai-assistant-select-source');
  source.setAttribute('aria-hidden', 'true');
  source.tabIndex = -1;

  wrapper.append(trigger, menu, source);

  // -- 内部状态 --
  let currentValue = String(value ?? '');
  let currentOptions = [];
  let destroyed = false;

  function syncSource() {
    // 保持 native select 的 value 与内部状态一致（供校验/表单序列化）
    const opts = Array.from(source.options);
    const hit = opts.find((o) => o.value === currentValue);
    if (hit) source.value = currentValue;
    else if (opts.length) source.value = opts[0].value;
  }

  function syncLabel() {
    const hit = currentOptions.find((o) => String(o.value) === currentValue && !o.hidden);
    label.textContent = hit ? hit.label : '请选择';
  }

  function renderOptions() {
    clear(menu);
    currentOptions.forEach((option) => {
      if (option.hidden) return;
      const item = el('button', 'plugin-stm-ai-assistant-select-option');
      item.type = 'button';
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(option.value === currentValue));
      item.dataset.value = String(option.value);
      if (option.disabled) item.disabled = true;

      if (option.icon) {
        const iconSlot = el('span', 'plugin-stm-ai-assistant-select-option-icon');
        iconSlot.setAttribute('aria-hidden', 'true');
        iconSlot.append(icon(option.icon, 'ui-icon--sm'));
        item.append(iconSlot);
      }
      item.append(el('span', 'plugin-stm-ai-assistant-select-option-label', option.label));
      item.addEventListener('click', () => selectOption(option));
      // 键盘上下移动
      item.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') { e.preventDefault(); focusSibling(item, 1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); focusSibling(item, -1); }
        else if (e.key === 'Home') { e.preventDefault(); focusBoundary(0); }
        else if (e.key === 'End') { e.preventDefault(); focusBoundary(-1); }
        else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectOption(option); }
        else if (e.key === 'Escape') { e.preventDefault(); close(true); }
      });
      menu.append(item);
    });
  }

  function visibleItems() {
    return Array.from(menu.querySelectorAll('button[role="option"]:not([disabled])'));
  }

  function focusSibling(current, dir) {
    const items = visibleItems();
    const idx = items.indexOf(current);
    const next = items[Math.max(0, Math.min(items.length - 1, idx + dir))];
    if (next) next.focus();
  }

  function focusBoundary(idx) {
    const items = visibleItems();
    const target = items[idx < 0 ? items.length - 1 : 0];
    if (target) target.focus();
  }

  function open() {
    if (destroyed || trigger.disabled) return;
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    wrapper.classList.add('is-open');
    const selected = menu.querySelector('[aria-selected="true"]') || menu.querySelector('button:not([disabled])');
    if (selected) selected.focus();
  }

  function close(focusTrigger = false) {
    if (menu.hidden) return;
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    wrapper.classList.remove('is-open');
    if (focusTrigger) trigger.focus();
  }

  function toggle() {
    if (menu.hidden) open(); else close();
  }

  function selectOption(option) {
    if (option.disabled) return;
    currentValue = String(option.value);
    syncSource();
    syncLabel();
    renderOptions();
    close(true);
    // 对外抛 change（从隐藏的 source 冒泡，保持原生语义）
    source.dispatchEvent(new Event('input', { bubbles: true }));
    source.dispatchEvent(new Event('change', { bubbles: true }));
    if (typeof onChange === 'function') onChange(currentValue);
  }

  // -- 事件绑定 --
  trigger.addEventListener('click', toggle);
  trigger.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); open(); }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    else if (e.key === 'Escape') { e.preventDefault(); close(true); }
  });
  function onDocumentClick(event) {
    if (!wrapper.contains(event.target)) close();
  }

  // 点外部收起（用 document 委托，menu 在 wrapper 内所以 wrapper.contains 已覆盖）
  document.addEventListener('click', onDocumentClick, true); // capture 阶段，避免被内部 stopPropagation 拦截

  // -- 初始化 --
  function setOptions(next) {
    currentOptions = (next || []).map((o) => ({
      value: String(o.value),
      label: o.label,
      icon: o.icon || '',
      disabled: Boolean(o.disabled),
      hidden: Boolean(o.hidden),
    }));
    clear(source);
    currentOptions.forEach((o) => {
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.label;
      source.append(opt);
    });
    syncSource();
    syncLabel();
    renderOptions();
  }

  setOptions(options);

  // -- 对外接口 --
  Object.defineProperty(wrapper, 'value', {
    get: () => currentValue,
    set: (v) => {
      currentValue = String(v ?? '');
      syncSource();
      syncLabel();
      renderOptions();
    },
  });
  Object.defineProperty(wrapper, 'disabled', {
    get: () => trigger.disabled,
    set: (v) => { trigger.disabled = Boolean(v); },
  });
  wrapper.setOptions = (next) => {
    if (destroyed) return;
    setOptions(next);
  };
  wrapper.selectElement = source;
  wrapper.close = () => close();
  wrapper.destroy = () => {
    if (destroyed) return;
    destroyed = true;
    close();
    document.removeEventListener('click', onDocumentClick, true);
  };

  return wrapper;
}

/**
 * 兼容旧签名：select() 现在返回 styledSelect 的 wrapper（div）。
 * 调用方用 wrapper.value 取值、wrapper.addEventListener('change') 监听、
 * wrapper.setOptions() 动态刷新选项。
 */
export function select(options, value, onChange = null) {
  return styledSelect(options, value, onChange);
}

/**
 * 数字输入 + 右侧上下箭头调节（对齐 settings.html 的 settings-number-control）。
 * 返回 wrapper，可通过 wrapper.querySelector('input') 拿到真正的 input。
 */
export function numberInput(value, { min = null, max = null, step = 1, placeholder = '' } = {}, onChange = null) {
  const wrapper = el('div', 'plugin-stm-ai-assistant-number-control');
  const input = el('input', 'plugin-stm-ai-assistant-input plugin-stm-ai-assistant-number-input');
  input.type = 'number';
  input.value = value == null ? '' : String(value);
  if (min != null) input.min = String(min);
  if (max != null) input.max = String(max);
  input.step = String(step);
  if (placeholder) input.placeholder = placeholder;
  input.dataset.marqueeIgnore = 'true';

  const stepper = el('div', 'plugin-stm-ai-assistant-number-stepper');
  stepper.setAttribute('aria-label', '调整数值');

  const up = el('button', 'plugin-stm-ai-assistant-number-stepper-btn');
  up.type = 'button';
  up.setAttribute('aria-label', '增加');
  up.append(icon('arrow-up', 'ui-icon--xs'));

  const down = el('button', 'plugin-stm-ai-assistant-number-stepper-btn');
  down.type = 'button';
  down.setAttribute('aria-label', '减少');
  down.append(icon('arrow-down', 'ui-icon--xs'));

  stepper.append(up, down);
  wrapper.append(input, stepper);

  function clamp(n) {
    let v = n;
    if (min != null) v = Math.max(min, v);
    if (max != null) v = Math.min(max, v);
    return v;
  }

  function bump(delta) {
    const cur = Number(input.value);
    const next = clamp((Number.isFinite(cur) ? cur : 0) + delta * step);
    input.value = String(next);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    if (typeof onChange === 'function') onChange(next);
  }

  up.addEventListener('click', () => bump(1));
  down.addEventListener('click', () => bump(-1));

  if (typeof onChange === 'function') {
    input.addEventListener('change', () => onChange(Number(input.value)));
  }

  return wrapper;
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
  const heading = el('h4', 'plugin-stm-ai-assistant-section-title');
  // 「数字 · 标题」形态的序号拆出来渲染为等宽强调色前缀（如 "1 · 选择卡片"）
  const match = /^(\d+)\s*·\s*(.+)$/.exec(title || '');
  if (match) {
    heading.append(
      el('span', 'plugin-stm-ai-assistant-section-title-num', `${match[1].padStart(2, '0')}.`),
      el('span', '', match[2]),
    );
  } else {
    heading.textContent = title;
  }
  wrapper.append(heading);
  if (subtitle) wrapper.append(el('p', 'plugin-stm-ai-assistant-section-subtitle', subtitle));
  return wrapper;
}

export function badge(text, tone = '') {
  return el('span', `plugin-stm-ai-assistant-badge ${tone ? `is-${tone}` : ''}`.trim(), text);
}

/**
 * 生成主程序同款 ui.svg 实心图标（<svg><use href> 引用雪碧图）。
 * sprite 符号在浏览器端即时解析，插件无需把图标数据打包进来。
 */
export function icon(name, className = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', `ui-icon${className ? ` ${className}` : ''}`);
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `/static/icons/ui.svg#icon-${name}`);
  svg.appendChild(use);
  return svg;
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
