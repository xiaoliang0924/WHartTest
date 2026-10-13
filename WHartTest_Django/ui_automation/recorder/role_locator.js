'use strict';

// Persisted role locators can include an accessible name. Passing the whole
// expression as an ARIA role silently yields zero matches, hiding count failures.
const ROLES = new Set(('alert alertdialog application article banner blockquote button caption cell checkbox code columnheader combobox complementary contentinfo definition deletion dialog directory document emphasis feed figure form generic grid gridcell group heading img insertion link list listbox listitem log main marquee math menu menubar menuitem menuitemcheckbox menuitemradio meter navigation none note option paragraph presentation progressbar radio radiogroup region row rowgroup rowheader scrollbar search searchbox separator slider spinbutton status strong subscript suggestion superscript switch tab table tablist tabpanel term textbox time timer toolbar tooltip tree treegrid treeitem').split(' '));

function roleLocator(container, expression) {
  const match = /^([a-z]+)(?:\[name=(.+)\])?$/.exec(String(expression).trim());
  if (!match || !ROLES.has(match[1])) throw new Error(`无效的 Role 定位器：${expression}`);
  if (!match[2]) return container.getByRole(match[1]);
  const nameValue = match[2];
  let name;
  const regex = /^\/(.*)\/([imsu]*)$/.exec(nameValue);
  if (regex) {
    name = new RegExp(regex[1], regex[2]);
  } else if (nameValue.startsWith('"') && nameValue.endsWith('"')) {
    name = JSON.parse(nameValue);
  } else if (nameValue.startsWith("'") && nameValue.endsWith("'")) {
    name = nameValue.slice(1, -1).replace(/\\(['\\])/g, '$1');
  } else {
    throw new Error(`Role 定位器的 name 必须为带引号的文本或正则：${expression}`);
  }
  return container.getByRole(match[1], { name, exact: typeof name === 'string' });
}

// Some applications append a live selection count to an otherwise stable
// action label (for example, a button rendered as “确定（1）”). Keep the
// configured exact locator first, but allow replay to try this narrowly
// bounded variant if the exact accessible name is absent.
function dynamicCountRoleLocator(expression) {
  const match = /^button\[name=("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\]$/.exec(String(expression).trim());
  if (!match) return null;
  let name;
  try {
    name = match[1].startsWith('"')
      ? JSON.parse(match[1])
      : match[1].slice(1, -1).replace(/\\(['\\])/g, '$1');
  } catch (_) {
    return null;
  }
  if (!name) return null;
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return `button[name=/^${escaped}(?:\\s*[（(]\\s*\\d+\\s*[）)])?$/]`;
}

module.exports = { roleLocator, dynamicCountRoleLocator };
