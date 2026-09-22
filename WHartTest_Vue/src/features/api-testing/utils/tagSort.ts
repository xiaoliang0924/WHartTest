import type { ApiTestCaseTag } from '../types/testcase'

const PARENT_TAG_ORDER = ['我的工单', '工单列表'] as const

const parentTagRank = new Map<string, number>(
  PARENT_TAG_ORDER.map((name, index) => [name, index]),
)

export const sortApiTestCaseTags = <T extends Pick<ApiTestCaseTag, 'name'>>(
  tags: T[] | undefined | null,
): T[] => {
  if (!tags?.length) {
    return []
  }

  return [...tags].sort((left, right) => {
    const leftRank = parentTagRank.get(left.name)
    const rightRank = parentTagRank.get(right.name)

    if (leftRank !== undefined || rightRank !== undefined) {
      const normalizedLeft = leftRank ?? Number.MAX_SAFE_INTEGER
      const normalizedRight = rightRank ?? Number.MAX_SAFE_INTEGER
      if (normalizedLeft !== normalizedRight) {
        return normalizedLeft - normalizedRight
      }
    }

    return left.name.localeCompare(right.name, 'zh-CN')
  })
}
