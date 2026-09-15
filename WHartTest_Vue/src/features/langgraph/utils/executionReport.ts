export const EXECUTION_REPORT_TITLE_RE = /##\s*测试执行结果[:：]\s*(通过|不通过)/;

export function isExecutionReportContent(text: string): boolean {
  return EXECUTION_REPORT_TITLE_RE.test(text || '');
}

// 报告正文之后模型额外贴出的内容（通常是「测试用例执行」提示词要求的 JSON 结果块）
const REPORT_APPENDIX_FENCE_RE = /\n+```[^\n`]*\n[\s\S]*?```[ \t]*$/;
const REPORT_APPENDIX_JSON_RE = /\n+[ \t]*[\[{][\s\S]*$/;

/** 裁掉报告正文之后的内容，报告卡片里不应出现原始 JSON */
export function stripExecutionReportAppendix(report: string): string {
  let text = (report || '').replace(/[ \t\n\r]+$/, '');
  text = text.replace(REPORT_APPENDIX_FENCE_RE, '').replace(/[ \t\n\r]+$/, '');
  const match = text.match(REPORT_APPENDIX_JSON_RE);
  if (match && match.index !== undefined && text.length - match.index > 20) {
    text = text.slice(0, match.index).replace(/[ \t\n\r]+$/, '');
  }
  return text;
}

export function extractFirstExecutionReport(summary: string): string {
  const text = (summary || '').trim();
  if (!text) return '';
  const start = text.search(EXECUTION_REPORT_TITLE_RE);
  if (start < 0) return text;
  const tail = text.slice(start);
  const next = tail.slice(1).search(EXECUTION_REPORT_TITLE_RE);
  return stripExecutionReportAppendix(next >= 0 ? tail.slice(0, next + 1).trim() : tail.trim());
}

export function stripExecutionReportFromText(text: string): string {
  if (!text) return '';
  const match = text.match(EXECUTION_REPORT_TITLE_RE);
  if (!match || match.index === undefined) return text;
  return text.slice(0, match.index).trim();
}

export function parseExecutionReportStatus(content: string): 'pass' | 'fail' | null {
  const match = content.match(/测试执行结果[:：]\s*(通过|不通过)/);
  if (!match) return null;
  return match[1] === '通过' ? 'pass' : 'fail';
}

/** 从用例管理「执行」用户消息中解析用例 ID */
export function parseTestCaseIdFromExecuteMessage(text: string): number | null {
  const match = (text || '').match(/执行ID为\s*(\d+)/);
  if (!match) return null;
  const id = parseInt(match[1], 10);
  return Number.isFinite(id) ? id : null;
}
