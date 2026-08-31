import type { TreeNodeData } from '@arco-design/web-vue';

/** Arco TreeSelect 默认按 key(id) 搜索，这里改为按模块名称搜索。 */
export function filterTreeNodeByName(searchKey: string, node: TreeNodeData): boolean {
  const keyword = searchKey.trim().toLowerCase();
  if (!keyword) return true;
  const label = String(node.title ?? node.name ?? '').toLowerCase();
  return label.includes(keyword);
}
