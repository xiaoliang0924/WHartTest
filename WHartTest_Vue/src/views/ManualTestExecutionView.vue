<template>
  <div class="manual-execution">
    <a-empty v-if="!projectId" description="请先选择项目" />

    <template v-else-if="!selectedRun">
      <header class="page-heading">
        <div>
          <h2>用例执行</h2>
          <span>{{
            activeTab === 'runs' ? `${runPagination.total} 个执行任务`
            : activeTab === 'team' ? `${teamPagination.total} 条团队待办`
            : `${todoPagination.total} 条待办用例`
          }}</span>
        </div>
        <a-space>
          <a-button v-if="isManager && activeTab === 'runs'" type="primary" @click="openCreate">
            <template #icon><icon-plus /></template>分派用例
          </a-button>
        </a-space>
      </header>

      <section class="surface">
        <a-tabs v-model:active-key="activeTab" @change="onTabChange">
          <a-tab-pane key="runs" title="任务列表">
            <div class="toolbar">
              <a-space wrap :size="10">
                <a-input-search v-model="filters.search" allow-clear placeholder="搜索执行批次" style="width:240px" @search="searchRuns" @clear="searchRuns" @keyup.enter="searchRuns" />
                <a-select v-model="filters.status" placeholder="执行状态" style="width:140px" allow-clear @change="searchRuns">
                  <a-option value="pending">待执行</a-option>
                  <a-option value="in_progress">执行中</a-option>
                  <a-option value="completed">已完成</a-option>
                </a-select>
                <a-select v-if="isManager" v-model="filters.assignee_id" placeholder="测试人员" style="width:160px" allow-clear @change="searchRuns">
                  <a-option v-for="member in members" :key="member.user" :value="member.user">{{ member.user_detail.username }}</a-option>
                </a-select>
                <a-select v-model="filters.environment" placeholder="执行环境" style="width:120px" allow-clear @change="searchRuns">
                  <a-option v-for="env in environmentOptions" :key="env" :value="env">{{ env }}</a-option>
                </a-select>
                <a-input v-model="filters.version" allow-clear placeholder="版本号" style="width:120px" @keyup.enter="searchRuns" />
                <a-range-picker v-model="filters.dateRange" show-time format="YYYY-MM-DD HH:mm:ss" value-format="YYYY-MM-DD HH:mm:ss" style="width:340px" :placeholder="['创建开始时间','创建结束时间']" />
                <a-button type="primary" @click="searchRuns"><template #icon><icon-search /></template>查询</a-button>
                <a-button @click="resetFilters"><template #icon><icon-refresh /></template>重置</a-button>
              </a-space>
            </div>

            <a-table :columns="runColumns" :data="runs" :loading="loading" row-key="id" size="small" stripe :pagination="false" :scroll="{ x: 1280 }" :bordered="{ cell: true }">
              <template #name="{ record }">
                <a-link @click="openRun(record)">{{ record.name }}</a-link>
                <div class="subtle">{{ record.description || '未填写说明' }}</div>
                <div v-if="record.test_suite_name" class="subtle">来源套件：{{ record.test_suite_name }}</div>
              </template>
              <template #meta="{ record }">
                <div>{{ record.environment || '-' }} / {{ record.version || '-' }}</div>
                <div :class="['deadline-text', { overdue: record.is_overdue }]">
                  {{ record.deadline ? formatRunDate(record.deadline) : '无截止' }}
                  <a-tag v-if="record.is_overdue" size="small" color="red">已逾期</a-tag>
                </div>
              </template>
              <template #assignee="{ record }">{{ record.assignee_detail?.username || '-' }}</template>
              <template #creator="{ record }">{{ record.creator_detail?.username || '-' }}</template>
              <template #progress="{ record }">
                <div class="run-progress-stack">
                  <div class="run-progress-bar">
                    <span v-if="runSegment(record, 'pass')" class="seg pass" :style="{ width: `${runSegment(record, 'pass')}%` }" />
                    <span v-if="runSegment(record, 'fail')" class="seg fail" :style="{ width: `${runSegment(record, 'fail')}%` }" />
                    <span v-if="runSegment(record, 'blocked')" class="seg blocked" :style="{ width: `${runSegment(record, 'blocked')}%` }" />
                    <span v-if="runSegment(record, 'skip')" class="seg skip" :style="{ width: `${runSegment(record, 'skip')}%` }" />
                    <span v-if="runSegment(record, 'pending')" class="seg pending" :style="{ width: `${runSegment(record, 'pending')}%` }" />
                  </div>
                  <div class="run-progress-meta">
                    <span class="subtle">{{ executedCount(record) }}/{{ record.total_count }}（{{ progressPercent(record) }}%）</span>
                    <span v-if="record.failed_count" class="fail-count">失败 {{ record.failed_count }}</span>
                  </div>
                </div>
              </template>
              <template #passRate="{ record }">{{ record.pass_rate ?? progressPercent(record) }}%</template>
              <template #status="{ record }"><a-tag :color="runStatusColor(record.status)">{{ runStatusText(record.status) }}</a-tag></template>
              <template #createdAt="{ record }">{{ formatRunDate(record.created_at) }}</template>
              <template #actions="{ record }">
                <a-space wrap>
                  <a-button type="text" @click="openRun(record)">进入任务</a-button>
                  <a-button type="text" @click="openReport(record)">报告</a-button>
                  <a-button v-if="isManager" type="text" @click="openTaskEdit(record)">编辑</a-button>
                  <a-button v-if="isManager" type="text" status="danger" @click="removeTask(record)">删除</a-button>
                </a-space>
              </template>
            </a-table>
            <div v-if="runPagination.total > 0" class="table-pagination">
              <a-pagination
                v-model:current="runPagination.current"
                v-model:page-size="runPagination.pageSize"
                :total="runPagination.total"
                show-total
                show-jumper
                show-page-size
                :page-size-options="runPagination.pageSizeOptions"
                @change="onRunPageChange"
                @page-size-change="onRunPageSizeChange"
              />
            </div>
          </a-tab-pane>

          <a-tab-pane key="todo">
            <template #title>
              <a-badge :count="todoSummary.pending_count" :max-count="99" :offset="[6, -2]">
                <span>我的待办</span>
              </a-badge>
            </template>

            <div class="todo-panel">
              <div class="todo-top-bar">
                <div class="todo-stats-block">
                  <div class="todo-stats-inline">
                    <span class="todo-stat-item"><strong>{{ todoSummary.pending_count }}</strong>待执行</span>
                    <span class="todo-stat-divider">|</span>
                    <span class="todo-stat-item"><strong>{{ todoSummary.today_completed_count ?? 0 }}</strong>今日完成</span>
                    <span class="todo-stat-divider">|</span>
                    <span class="todo-stat-item"><strong>{{ todoSummary.run_count }}</strong>个批次</span>
                    <template v-if="todoSummary.overdue_count">
                      <span class="todo-stat-divider">|</span>
                      <span class="todo-stat-item todo-stat-warn"><strong>{{ todoSummary.overdue_count }}</strong>条积压</span>
                    </template>
                  </div>
                  <div v-if="todoLevelChips.length" class="todo-level-chips">
                    <a-tag v-for="item in todoLevelChips" :key="item.level" size="small" :color="levelColor(item.level)">
                      {{ item.level }} {{ item.count }}
                    </a-tag>
                  </div>
                </div>
                <a-space wrap :size="8" class="todo-filters">
                  <a-input-search v-model="todoFilters.search" allow-clear placeholder="搜索批次或用例" style="width:220px" @search="searchTodo" @clear="searchTodo" @keyup.enter="searchTodo" />
                  <a-select v-model="todoFilters.run_id" placeholder="执行批次" style="width:170px" allow-clear @change="searchTodo">
                    <a-option v-for="batch in todoSummary.runs" :key="batch.id" :value="batch.id">{{ batch.name }}（{{ batch.pending_count }}）</a-option>
                  </a-select>
                  <a-select v-model="todoFilters.level" placeholder="优先级" style="width:100px" allow-clear @change="searchTodo">
                    <a-option value="P0">P0</a-option>
                    <a-option value="P1">P1</a-option>
                    <a-option value="P2">P2</a-option>
                    <a-option value="P3">P3</a-option>
                  </a-select>
                  <a-select v-model="todoFilters.module_id" placeholder="所属模块" style="width:160px" allow-clear @change="searchTodo">
                    <a-option v-for="m in modules" :key="m.id" :value="m.id">{{ m.name }}</a-option>
                  </a-select>
                  <a-select v-model="todoFilters.status" placeholder="执行结果" style="width:120px" allow-clear @change="searchTodo">
                    <a-option value="pending">待执行</a-option>
                    <a-option value="pass">通过</a-option>
                    <a-option value="fail">不通过</a-option>
                    <a-option value="blocked">阻塞</a-option>
                    <a-option value="skip">跳过</a-option>
                  </a-select>
                  <a-button type="primary" @click="searchTodo"><template #icon><icon-search /></template>查询</a-button>
                  <a-button @click="resetTodoFilters"><template #icon><icon-refresh /></template>重置</a-button>
                  <a-button v-if="nextPendingAssignment" type="outline" @click="openRunById(nextPendingAssignment.run, nextPendingAssignment.id, true)">
                    <template #icon><icon-play-arrow /></template>继续执行
                  </a-button>
                </a-space>
              </div>

              <div v-if="todoSummary.runs.length > 1" class="todo-batch-chips">
                <span class="chips-label">批次：</span>
                <a-tag
                  v-for="batch in todoSummary.runs"
                  :key="batch.id"
                  :color="todoFilters.run_id === batch.id ? 'arcoblue' : undefined"
                  checkable
                  :checked="todoFilters.run_id === batch.id"
                  @click="toggleTodoBatch(batch.id)"
                >
                  {{ batch.name }} · 待执行 {{ batch.pending_count }}/{{ batch.total_count }}
                </a-tag>
                <a-tag v-if="todoFilters.run_id" checkable @click="toggleTodoBatch(undefined)">全部</a-tag>
              </div>

              <div class="table-section">
                <a-table
                  :columns="todoColumns"
                  :data="todoAssignments"
                  :loading="todoLoading"
                  row-key="id"
                  size="small"
                  :pagination="false"
                  :scroll="{ x: 1080 }"
                  :bordered="{ cell: true }"
                  stripe
                >
                  <template #case="{ record }">
                    <div class="case-cell">
                      <strong>{{ record.testcase_detail?.name }}</strong>
                      <div class="subtle">#{{ record.testcase }} · {{ formatModuleShort(record.testcase_detail?.module_detail) }}</div>
                    </div>
                  </template>
                  <template #run="{ record }">
                    <a-link @click="openRunById(record.run, undefined, true)">{{ record.run_name }}</a-link>
                    <div class="run-progress-line">
                      <a-progress :percent="runProgressPercent(record)" :show-text="false" size="small" :style="{ width: '88px' }" />
                      <span class="subtle">{{ (record.run_passed_count || 0) + (record.run_failed_count || 0) }}/{{ record.run_total_count || 0 }}</span>
                    </div>
                  </template>
                  <template #level="{ record }">
                    <a-tag v-if="record.testcase_detail?.level" size="small" :color="levelColor(record.testcase_detail.level)">{{ record.testcase_detail.level }}</a-tag>
                    <span v-else>-</span>
                  </template>
                  <template #result="{ record }"><a-tag :color="resultColor(record.status)">{{ resultText(record.status) }}</a-tag></template>
                  <template #assignedAt="{ record }">{{ formatRunDate(record.created_at) }}</template>
                  <template #executedAt="{ record }">{{ formatRunDate(record.executed_at) }}</template>
                  <template #actions="{ record }">
                    <a-space v-if="record.status === 'pending'" :size="4" wrap>
                      <a-button type="outline" size="mini" status="success" :loading="quickSubmittingId === record.id" @click="quickSubmitResult(record, 'pass')">通过</a-button>
                      <a-button type="outline" size="mini" status="danger" :loading="quickSubmittingId === record.id" @click="openQuickFail(record)">不通过</a-button>
                      <a-button type="outline" size="mini" :loading="quickSubmittingId === record.id" @click="quickSubmitResult(record, 'skip')">跳过</a-button>
                      <a-button type="outline" size="mini" status="warning" :loading="quickSubmittingId === record.id" @click="openQuickBlocked(record)">阻塞</a-button>
                      <a-button type="primary" size="mini" @click="openRunById(record.run, record.id, true)">详情</a-button>
                    </a-space>
                    <a-button v-else type="primary" size="mini" @click="openRunById(record.run, record.id, true)">查看</a-button>
                  </template>
                  <template #empty>
                    <a-empty description="暂无待办用例">
                      <a-button v-if="hasActiveTodoFilters" @click="resetTodoFilters">查看全部待执行</a-button>
                    </a-empty>
                  </template>
                </a-table>

                <div v-if="todoPagination.total > 0" class="table-pagination">
                  <a-pagination
                    v-model:current="todoPagination.current"
                    v-model:page-size="todoPagination.pageSize"
                    :total="todoPagination.total"
                    show-total
                    show-jumper
                    show-page-size
                    :page-size-options="todoPagination.pageSizeOptions"
                    @change="onTodoPageChange"
                    @page-size-change="onTodoPageSizeChange"
                  />
                </div>
              </div>
            </div>
          </a-tab-pane>

          <a-tab-pane v-if="isManager" key="team" title="团队待办">
            <div class="todo-panel">
              <div class="todo-top-bar">
                <div class="todo-stats-block">
                  <div class="todo-stats-inline">
                    <span class="todo-stat-item"><strong>{{ teamSummary.pending_count }}</strong>条待执行</span>
                    <span class="todo-stat-divider">|</span>
                    <span class="todo-stat-item"><strong>{{ teamSummary.members.length }}</strong>位成员</span>
                    <span class="todo-stat-divider">|</span>
                    <span class="todo-stat-item"><strong>{{ teamSummary.run_count }}</strong>个批次</span>
                    <template v-if="teamSummary.overdue_count">
                      <span class="todo-stat-divider">|</span>
                      <span class="todo-stat-item todo-stat-warn"><strong>{{ teamSummary.overdue_count }}</strong>条积压</span>
                    </template>
                  </div>
                  <div v-if="teamSummary.members.length" class="todo-level-chips">
                    <a-tag
                      v-for="member in teamSummary.members"
                      :key="member.assignee_id"
                      checkable
                      :checked="teamFilters.assignee_id === member.assignee_id"
                      @click="toggleTeamMember(member.assignee_id)"
                    >
                      {{ member.username }} {{ member.pending_count }}
                    </a-tag>
                    <a-tag v-if="teamFilters.assignee_id" checkable @click="toggleTeamMember(undefined)">全部成员</a-tag>
                  </div>
                </div>
                <a-space wrap :size="8" class="todo-filters">
                  <a-input-search v-model="teamFilters.search" allow-clear placeholder="搜索批次或用例" style="width:220px" @search="searchTeam" @clear="searchTeam" @keyup.enter="searchTeam" />
                  <a-select v-model="teamFilters.assignee_id" placeholder="测试人员" style="width:160px" allow-clear @change="searchTeam">
                    <a-option v-for="member in members" :key="member.user" :value="member.user">{{ member.user_detail.username }}</a-option>
                  </a-select>
                  <a-select v-model="teamFilters.status" placeholder="执行结果" style="width:120px" allow-clear @change="searchTeam">
                    <a-option value="pending">待执行</a-option>
                    <a-option value="pass">通过</a-option>
                    <a-option value="fail">不通过</a-option>
                    <a-option value="blocked">阻塞</a-option>
                    <a-option value="skip">跳过</a-option>
                  </a-select>
                  <a-button type="primary" @click="searchTeam"><template #icon><icon-search /></template>查询</a-button>
                  <a-button @click="resetTeamFilters"><template #icon><icon-refresh /></template>重置</a-button>
                </a-space>
              </div>

              <div class="table-section">
                <a-table
                  :columns="teamColumns"
                  :data="teamAssignments"
                  :loading="teamLoading"
                  row-key="id"
                  size="small"
                  :pagination="false"
                  :scroll="{ x: 1180 }"
                  :bordered="{ cell: true }"
                  stripe
                >
                  <template #case="{ record }">
                    <div class="case-cell">
                      <strong>{{ record.testcase_detail?.name }}</strong>
                      <div class="subtle">#{{ record.testcase }} · {{ formatModuleShort(record.testcase_detail?.module_detail) }}</div>
                    </div>
                  </template>
                  <template #assignee="{ record }">{{ record.assignee_detail?.username || '-' }}</template>
                  <template #run="{ record }">
                    <a-link @click="openRunById(record.run)">{{ record.run_name }}</a-link>
                  </template>
                  <template #level="{ record }">
                    <a-tag v-if="record.testcase_detail?.level" size="small" :color="levelColor(record.testcase_detail.level)">{{ record.testcase_detail.level }}</a-tag>
                    <span v-else>-</span>
                  </template>
                  <template #result="{ record }"><a-tag :color="resultColor(record.status)">{{ resultText(record.status) }}</a-tag></template>
                  <template #assignedAt="{ record }">{{ formatRunDate(record.created_at) }}</template>
                  <template #actions="{ record }">
                    <a-button type="primary" size="mini" @click="openRunById(record.run, record.id, false)">查看</a-button>
                  </template>
                </a-table>
                <div v-if="teamPagination.total > 0" class="table-pagination">
                  <a-pagination
                    v-model:current="teamPagination.current"
                    v-model:page-size="teamPagination.pageSize"
                    :total="teamPagination.total"
                    show-total
                    show-jumper
                    show-page-size
                    :page-size-options="teamPagination.pageSizeOptions"
                    @change="onTeamPageChange"
                    @page-size-change="onTeamPageSizeChange"
                  />
                </div>
              </div>
            </div>
          </a-tab-pane>
        </a-tabs>
      </section>
    </template>

    <template v-else>
      <header class="page-heading">
        <div class="title-line">
          <a-button type="text" @click="closeRunDetail"><template #icon><icon-left /></template>{{ returnTab === 'todo' ? '返回我的待办' : returnTab === 'team' ? '返回团队待办' : '返回任务列表' }}</a-button>
          <div>
            <h2>{{ selectedRun.name }}</h2>
            <span>
              {{ selectedRun.total_count }} 条用例，{{ selectedRun.pending_count }} 条待执行
              <template v-if="selectedRun.environment || selectedRun.version"> · {{ selectedRun.environment || '-' }} / {{ selectedRun.version || '-' }}</template>
              · 通过率 {{ selectedRun.pass_rate ?? progressPercent(selectedRun) }}%
            </span>
          </div>
        </div>
        <a-space>
          <a-tag color="green">通过 {{ selectedRun.passed_count }}</a-tag>
          <a-tag color="red">不通过 {{ selectedRun.failed_count }}</a-tag>
          <a-tag v-if="selectedRun.blocked_count" color="orangered">阻塞 {{ selectedRun.blocked_count }}</a-tag>
          <a-tag v-if="selectedRun.skip_count" color="arcoblue">跳过 {{ selectedRun.skip_count }}</a-tag>
          <a-tag color="gray">待执行 {{ selectedRun.pending_count }}</a-tag>
          <a-button type="outline" @click="openReport(selectedRun)">查看报告</a-button>
          <a-button type="outline" :loading="exporting" @click="exportCurrentRun"><template #icon><icon-download /></template>导出</a-button>
        </a-space>
      </header>

      <section class="surface">
        <div class="toolbar">
          <a-space wrap :size="10">
            <a-select v-model="caseFilters.status" placeholder="用例结果" style="width:140px" allow-clear @change="casePagination.current = 1">
              <a-option value="pending">待执行</a-option>
              <a-option value="pass">通过</a-option>
              <a-option value="fail">不通过</a-option>
              <a-option value="blocked">阻塞</a-option>
              <a-option value="skip">跳过</a-option>
            </a-select>
            <a-input-search v-model="caseFilters.keyword" allow-clear placeholder="搜索用例名称" style="width:240px" @search="casePagination.current = 1" @clear="casePagination.current = 1" />
          </a-space>
        </div>

        <a-table :columns="caseColumns" :data="filteredAssignments" row-key="id" :pagination="casePagination" :scroll="{ x: 1000 }" :bordered="{ cell: true }" @page-change="onCasePageChange" @page-size-change="onCasePageSizeChange">
          <template #case="{ record }"><strong>{{ record.testcase_detail?.name }}</strong><div class="subtle">#{{ record.testcase }} {{ record.testcase_detail?.module_detail || '未分配模块' }}</div></template>
          <template #result="{ record }"><a-tag :color="resultColor(record.status)">{{ resultText(record.status) }}</a-tag></template>
          <template #reason="{ record }">{{ record.failure_reason || '-' }}</template>
          <template #defect="{ record }">
            <a-link v-if="record.defect_url" :href="record.defect_url" target="_blank">{{ record.defect_title || '查看缺陷' }}</a-link>
            <span v-else>-</span>
          </template>
          <template #executedAt="{ record }">{{ formatRunDate(record.executed_at) }}</template>
          <template #actions="{ record }"><a-button type="text" @click="openCase(record)">{{ record.status === 'pending' ? '执行' : '查看/更新' }}</a-button></template>
        </a-table>
      </section>
    </template>

    <a-modal v-model:visible="createVisible" title="分派测试用例" :ok-loading="saving" :ok-text="saving ? '提交中...' : '确定'" :mask-closable="!saving" :closable="!saving" @before-ok="createRun">
      <a-spin :loading="saving" :tip="saveStatus || '正在提交...'" class="modal-save-spin">
        <a-form :model="form" layout="vertical">
          <a-form-item label="执行批次" required><a-input v-model="form.name" placeholder="例如：V2.4 回归测试" :disabled="saving" /></a-form-item>
          <a-row :gutter="12">
            <a-col :span="8">
              <a-form-item label="执行环境">
                <a-select v-model="form.environment" placeholder="选择环境" allow-clear :disabled="saving">
                  <a-option v-for="env in environmentOptions" :key="env" :value="env">{{ env }}</a-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :span="8">
              <a-form-item label="版本号"><a-input v-model="form.version" placeholder="如 V2.6.0" :disabled="saving" /></a-form-item>
            </a-col>
            <a-col :span="8">
              <a-form-item label="截止日期">
                <a-date-picker v-model="form.deadline" show-time format="YYYY-MM-DD HH:mm:ss" value-format="YYYY-MM-DD HH:mm:ss" style="width:100%" :disabled="saving" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-form-item label="来源测试套件">
            <a-select v-model="form.testsuite_id" placeholder="可选：从测试套件导入用例" allow-clear :disabled="saving" @change="onCreateSuiteChange">
              <a-option v-for="suite in testSuites" :key="suite.id" :value="suite.id">{{ suite.name }}（{{ suite.testcase_count }}）</a-option>
            </a-select>
          </a-form-item>
          <a-form-item label="测试人员" required>
            <a-select v-model="form.assignee_id" placeholder="请选择测试人员" :disabled="saving">
              <a-option v-for="member in members" :key="member.user" :value="member.user">{{ member.user_detail.username }}</a-option>
            </a-select>
          </a-form-item>
          <a-form-item label="测试用例" required>
            <div class="picker">
              <div class="picker-head">
                <span>模块与用例</span>
                <div class="picker-actions">
                  <a-button type="text" size="mini" :disabled="saving" @click="selectAllCases('create')">全选</a-button>
                  <a-button type="text" size="mini" :disabled="saving" @click="clearAllCases('create')">取消全选</a-button>
                  <a-tag color="arcoblue">已选 {{ form.testcase_ids.length }} 条</a-tag>
                </div>
              </div>
              <a-spin :loading="optionsLoading" class="picker-spin">
                <a-tree :data="caseTree" checkable block-node checked-strategy="child" :checked-keys="form.testcase_ids" :field-names="{ key: 'key', title: 'title', children: 'children' }" @check="(keys, event) => onTreeCheck(keys, event)">
                  <template #title="{ title, key }"><span class="tree-node-title" @click.stop="onModuleTitleClick(key, 'create')">{{ title }}</span></template>
                </a-tree>
              </a-spin>
            </div>
          </a-form-item>
          <a-form-item label="说明"><a-textarea v-model="form.description" :auto-size="{ minRows: 2, maxRows: 4 }" :disabled="saving" /></a-form-item>
        </a-form>
      </a-spin>
    </a-modal>

    <a-modal v-model:visible="editVisible" title="编辑执行任务" :ok-loading="saving" :ok-text="saving ? '保存中...' : '确定'" :mask-closable="!saving" :closable="!saving" @before-ok="saveTaskEdit">
      <a-spin :loading="saving" :tip="saveStatus || '正在保存...'" class="modal-save-spin">
        <a-form :model="editForm" layout="vertical">
          <a-form-item label="执行批次" required><a-input v-model="editForm.name" :disabled="saving" /></a-form-item>
          <a-row :gutter="12">
            <a-col :span="8">
              <a-form-item label="执行环境">
                <a-select v-model="editForm.environment" placeholder="选择环境" allow-clear :disabled="saving">
                  <a-option v-for="env in environmentOptions" :key="env" :value="env">{{ env }}</a-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :span="8">
              <a-form-item label="版本号"><a-input v-model="editForm.version" :disabled="saving" /></a-form-item>
            </a-col>
            <a-col :span="8">
              <a-form-item label="截止日期">
                <a-date-picker v-model="editForm.deadline" show-time format="YYYY-MM-DD HH:mm:ss" value-format="YYYY-MM-DD HH:mm:ss" style="width:100%" :disabled="saving" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-form-item label="测试人员" required>
            <a-select v-model="editForm.assignee_id" placeholder="请选择测试人员" :disabled="saving">
              <a-option v-for="member in members" :key="member.user" :value="member.user">{{ member.user_detail.username }}</a-option>
            </a-select>
          </a-form-item>
          <a-form-item label="说明"><a-textarea v-model="editForm.description" :auto-size="{ minRows: 2, maxRows: 4 }" :disabled="saving" /></a-form-item>
          <a-form-item label="测试用例">
            <div class="picker">
              <div class="picker-head">
                <span>模块与用例</span>
                <div class="picker-actions">
                  <a-button type="text" size="mini" :disabled="saving" @click="selectAllCases('edit')">全选</a-button>
                  <a-button type="text" size="mini" :disabled="saving" @click="clearAllCases('edit')">取消全选</a-button>
                  <a-tag color="arcoblue">已选 {{ editCaseIds.length }} 条</a-tag>
                </div>
              </div>
              <a-spin :loading="optionsLoading" class="picker-spin">
                <a-tree :data="caseTree" checkable block-node checked-strategy="child" :checked-keys="editCaseIds" :field-names="{ key: 'key', title: 'title', children: 'children' }" @check="(keys, event) => onEditTreeCheck(keys, event)">
                  <template #title="{ title, key }"><span class="tree-node-title" @click.stop="onModuleTitleClick(key, 'edit')">{{ title }}</span></template>
                </a-tree>
              </a-spin>
            </div>
          </a-form-item>
        </a-form>
      </a-spin>
    </a-modal>

    <a-drawer v-model:visible="caseVisible" :width="840" title="执行测试用例">
      <template v-if="active">
        <div class="case-nav">
          <a-button :disabled="activeIndex === 0" @click="goCase(-1)"><template #icon><icon-left /></template>上一条</a-button>
          <span>
            {{ activeIndex + 1 }} / {{ executionNavList.length || 0 }}
            <span v-if="returnTab === 'todo'" class="subtle"> · 仅待执行</span>
          </span>
          <a-button :disabled="activeIndex >= executionNavList.length - 1" @click="goCase(1)">下一条<template #icon><icon-right /></template></a-button>
        </div>
        <h3>{{ active.testcase_detail?.name }}</h3>
        <a-descriptions :column="2" bordered size="small">
          <a-descriptions-item label="所属模块">{{ active.testcase_detail?.module_detail || '-' }}</a-descriptions-item>
          <a-descriptions-item label="优先级">{{ active.testcase_detail?.level }}</a-descriptions-item>
          <a-descriptions-item label="执行时间">{{ formatRunDate(active.executed_at) }}</a-descriptions-item>
          <a-descriptions-item label="测试人员">{{ active.assignee_detail?.username || '-' }}</a-descriptions-item>
        </a-descriptions>
        <section class="detail">
          <h4>前置条件</h4><p>{{ active.testcase_detail?.precondition || '-' }}</p>
          <h4>测试步骤</h4>
          <a-table :columns="stepExecColumns" :data="active.testcase_detail?.steps || []" :pagination="false" size="small" bordered row-key="step_number">
            <template #stepResult="{ record }">
              <a-select
                :model-value="getStepResult(record.step_number).status"
                size="mini"
                style="width:108px"
                @change="(value: ManualResultStatus) => setStepResult(record.step_number, { status: value })"
              >
                <a-option value="pending">待执行</a-option>
                <a-option value="pass">通过</a-option>
                <a-option value="fail">不通过</a-option>
                <a-option value="blocked">阻塞</a-option>
                <a-option value="skip">跳过</a-option>
              </a-select>
              <a-input
                :model-value="getStepResult(record.step_number).comment"
                size="mini"
                placeholder="步骤备注"
                style="margin-top:6px"
                @update:model-value="(value: string) => setStepResult(record.step_number, { comment: value })"
              />
            </template>
          </a-table>
          <h4>备注</h4><p>{{ active.testcase_detail?.notes || '-' }}</p>
        </section>
        <a-form :model="resultForm" layout="vertical">
          <a-form-item label="执行结果" required>
            <a-radio-group v-model="resultForm.status">
              <a-radio value="pass">通过</a-radio>
              <a-radio value="fail">不通过</a-radio>
              <a-radio value="blocked">阻塞</a-radio>
              <a-radio value="skip">跳过</a-radio>
              <a-radio value="pending">恢复待执行</a-radio>
            </a-radio-group>
          </a-form-item>
          <a-form-item label="失败原因" :required="resultForm.status === 'fail'"><a-textarea v-model="resultForm.failure_reason" :auto-size="{ minRows: 2, maxRows: 4 }" /></a-form-item>
          <a-form-item v-if="resultForm.status === 'fail'" label="关联缺陷">
            <a-input v-model="resultForm.defect_title" placeholder="缺陷标题（可选）" style="margin-bottom:8px" />
            <a-input v-model="resultForm.defect_url" placeholder="缺陷链接，如 Jira / 禅道 URL（可选）" />
          </a-form-item>
          <a-form-item label="执行备注" :required="resultForm.status === 'blocked'"><a-textarea v-model="resultForm.comment" :auto-size="{ minRows: 2, maxRows: 4 }" placeholder="阻塞原因或其他备注" /></a-form-item>
          <a-form-item v-if="resultForm.status === 'fail' || resultForm.evidence_files.length" label="失败证据">
            <a-upload
              :custom-request="handleEvidenceUpload"
              :show-file-list="false"
              multiple
              accept="image/*,.png,.jpg,.jpeg,.gif,.webp,.pdf,.log,.txt"
            >
              <template #upload-button>
                <a-button type="outline" size="small" :loading="evidenceUploading"><template #icon><icon-upload /></template>上传截图/附件</a-button>
              </template>
            </a-upload>
            <div v-if="resultForm.evidence_files.length" class="evidence-list">
              <div v-for="(file, index) in resultForm.evidence_files" :key="`${file.url}-${index}`" class="evidence-item">
                <a-link :href="file.url" target="_blank">{{ file.name }}</a-link>
                <a-button type="text" size="mini" status="danger" @click="removeEvidence(index)">移除</a-button>
              </div>
            </div>
          </a-form-item>
        </a-form>
      </template>
      <template #footer>
        <a-button @click="caseVisible = false">取消</a-button>
        <a-button type="outline" :loading="saving" @click="saveResult(false)">保存结果</a-button>
        <a-button type="primary" :loading="saving" @click="saveResult(true)">保存并下一条</a-button>
      </template>
    </a-drawer>

    <ManualTestRunReportModal v-model:visible="reportVisible" :project-id="projectId" :run-id="reportRunId" :run-name="reportRunName" />

    <a-modal v-model:visible="quickFailVisible" title="标记不通过" :ok-loading="quickSubmittingId !== null" @before-ok="confirmQuickFail">
      <p class="subtle">{{ quickFailTarget?.testcase_detail?.name }}</p>
      <a-form layout="vertical">
        <a-form-item label="失败原因" required>
          <a-textarea v-model="quickFailReason" :auto-size="{ minRows: 2, maxRows: 4 }" placeholder="请填写不通过原因" />
        </a-form-item>
      </a-form>
    </a-modal>
    <a-modal v-model:visible="quickBlockedVisible" title="标记阻塞" :ok-loading="quickSubmittingId !== null" @before-ok="confirmQuickBlocked">
      <p class="subtle">{{ quickBlockedTarget?.testcase_detail?.name }}</p>
      <a-form layout="vertical">
        <a-form-item label="阻塞原因" required>
          <a-textarea v-model="quickBlockedReason" :auto-size="{ minRows: 2, maxRows: 4 }" placeholder="请填写阻塞原因，如环境不可用、依赖未就绪" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { Message, Modal } from '@arco-design/web-vue';
import type { TableColumnData } from '@arco-design/web-vue';
import ManualTestRunReportModal from '@/components/testcase/ManualTestRunReportModal.vue';
import { useProjectStore } from '@/store/projectStore';
import { useAuthStore } from '@/store/authStore';
import { getProjectMembers, type ProjectMember } from '@/services/projectService';
import { getTestCaseList, type TestCase } from '@/services/testcaseService';
import { getTestCaseModules, type TestCaseModule } from '@/services/testcaseModuleService';
import {
  addManualRunCases,
  createManualTestRun,
  deleteManualRun,
  exportManualRunExcel,
  getManualAssignments,
  getManualRun,
  getManualRuns,
  getManualTodoSummary,
  getNextPendingAssignment,
  getTeamTodoSummary,
  reassignManualRun,
  removeManualRunCase,
  submitManualResult,
  updateManualRun,
  uploadManualEvidence,
  type ManualEvidenceFile,
  type ManualResultStatus,
  type ManualStepResult,
  type ManualTestAssignment,
  type ManualTestRunDetail,
  type ManualTestRunListItem,
  type ManualTeamTodoSummary,
  type ManualTodoSummary,
} from '@/services/manualTestExecutionService';
import { getTestSuiteDetail, getTestSuiteList, type TestSuite } from '@/services/testSuiteService';

const projectStore = useProjectStore();
const authStore = useAuthStore();
const route = useRoute();
const projectId = computed(() => projectStore.currentProjectId);

const activeTab = ref('runs');
const runs = ref<ManualTestRunListItem[]>([]);
const selectedRun = ref<ManualTestRunDetail | null>(null);
const members = ref<ProjectMember[]>([]);
const testcases = ref<TestCase[]>([]);
const modules = ref<TestCaseModule[]>([]);
const todoAssignments = ref<ManualTestAssignment[]>([]);
const nextPendingAssignment = ref<ManualTestAssignment | null>(null);
const todoSummary = ref<ManualTodoSummary>({ pending_count: 0, run_count: 0, runs: [], today_completed_count: 0, overdue_count: 0, level_counts: {} });
const teamSummary = ref<ManualTeamTodoSummary>({ pending_count: 0, run_count: 0, overdue_count: 0, members: [] });
const teamAssignments = ref<ManualTestAssignment[]>([]);
const teamLoading = ref(false);
const testSuites = ref<TestSuite[]>([]);
const environmentOptions = ['开发', '测试', '预发布', '生产'];
const returnTab = ref<'runs' | 'todo' | 'team'>('runs');

const TODO_FILTERS_PREFIX = 'manual_todo_filters_';

const filters = reactive({ search: '', status: undefined as string | undefined, assignee_id: undefined as number | undefined, environment: undefined as string | undefined, version: '', dateRange: [] as string[] });
const todoFilters = reactive({
  search: '',
  status: 'pending' as string | undefined,
  run_id: undefined as number | undefined,
  level: undefined as string | undefined,
  module_id: undefined as number | undefined,
});
const teamFilters = reactive({
  search: '',
  status: 'pending' as string | undefined,
  assignee_id: undefined as number | undefined,
});
const caseFilters = reactive({ status: undefined as string | undefined, keyword: '' });

const loading = ref(false);
const todoLoading = ref(false);
const saving = ref(false);
const exporting = ref(false);
const saveStatus = ref('');
const optionsLoading = ref(false);
const optionsProjectId = ref<number | null>(null);
let optionsLoadPromise: Promise<void> | null = null;

const createVisible = ref(false);
const editVisible = ref(false);
const caseVisible = ref(false);
const reportVisible = ref(false);
const reportRunId = ref<number | null>(null);
const reportRunName = ref('');

const quickFailVisible = ref(false);
const quickFailTarget = ref<ManualTestAssignment | null>(null);
const quickFailReason = ref('');
const quickBlockedVisible = ref(false);
const quickBlockedTarget = ref<ManualTestAssignment | null>(null);
const quickBlockedReason = ref('');
const quickSubmittingId = ref<number | null>(null);
const evidenceUploading = ref(false);

const active = ref<ManualTestAssignment | null>(null);
const activeIndex = ref(0);
const editingRun = ref<ManualTestRunDetail | null>(null);
const initialEditAssigneeId = ref<number | undefined>(undefined);

const form = ref({
  name: '',
  description: '',
  environment: undefined as string | undefined,
  version: '',
  deadline: undefined as string | undefined,
  testsuite_id: undefined as number | undefined,
  assignee_id: undefined as number | undefined,
  testcase_ids: [] as number[],
});
const editForm = ref({
  name: '',
  description: '',
  environment: undefined as string | undefined,
  version: '',
  deadline: undefined as string | undefined,
  assignee_id: undefined as number | undefined,
});
const editCaseIds = ref<number[]>([]);
const orphanedSnapshotCaseIds = ref<number[]>([]);
const resultForm = ref({
  status: 'pending' as ManualResultStatus,
  failure_reason: '',
  comment: '',
  step_results: [] as ManualStepResult[],
  evidence_files: [] as ManualEvidenceFile[],
  defect_title: '',
  defect_url: '',
});

const isManager = computed(() => authStore.user?.is_staff || members.value.some(m => m.user === authStore.user?.id && ['owner', 'admin'].includes(m.role)));

const runPagination = reactive({ current: 1, pageSize: 10, total: 0, showTotal: true, showJumper: true, showPageSize: true, pageSizeOptions: [10, 20, 50] });
const todoPagination = reactive({ current: 1, pageSize: 5, total: 0, showTotal: true, showJumper: true, showPageSize: true, pageSizeOptions: [5, 10, 20, 50] });
const teamPagination = reactive({ current: 1, pageSize: 10, total: 0, showTotal: true, showJumper: true, showPageSize: true, pageSizeOptions: [10, 20, 50] });
const casePagination = reactive({ current: 1, pageSize: 10, total: 0, showTotal: true, showJumper: true, showPageSize: true, pageSizeOptions: [10, 20, 50, 100] });

const runColumns: TableColumnData[] = [
  { title: '执行批次', slotName: 'name', width: 220 },
  { title: '环境/版本', slotName: 'meta', width: 170 },
  { title: '测试人员', slotName: 'assignee', width: 100 },
  { title: '创建人', slotName: 'creator', width: 100 },
  { title: '进度', slotName: 'progress', width: 180 },
  { title: '通过率', slotName: 'passRate', width: 80 },
  { title: '状态', slotName: 'status', width: 90 },
  { title: '创建时间', slotName: 'createdAt', width: 160 },
  { title: '操作', slotName: 'actions', width: 200 },
];

const teamColumns: TableColumnData[] = [
  { title: '测试用例', slotName: 'case', ellipsis: true, tooltip: true },
  { title: '测试人员', slotName: 'assignee', width: 100 },
  { title: '执行批次', slotName: 'run', width: 150 },
  { title: '优先级', slotName: 'level', width: 80 },
  { title: '结果', slotName: 'result', width: 90 },
  { title: '分派时间', slotName: 'assignedAt', width: 150 },
  { title: '操作', slotName: 'actions', width: 80, align: 'center' },
];

const todoColumns = computed<TableColumnData[]>(() => {
  const cols: TableColumnData[] = [
    { title: '测试用例', slotName: 'case', ellipsis: true, tooltip: true },
    { title: '执行批次', slotName: 'run', width: 150 },
    { title: '优先级', slotName: 'level', width: 80 },
    { title: '分派时间', slotName: 'assignedAt', width: 150 },
  ];
  if (todoFilters.status && todoFilters.status !== 'pending') {
    cols.push({ title: '结果', slotName: 'result', width: 90 });
    cols.push({ title: '执行时间', slotName: 'executedAt', width: 150 });
  }
  cols.push({ title: '操作', slotName: 'actions', width: todoFilters.status === 'pending' || !todoFilters.status ? 320 : 88, align: 'center' });
  return cols;
});

const hasActiveTodoFilters = computed(() => (
  todoFilters.status !== 'pending'
  || !!todoFilters.search.trim()
  || !!todoFilters.run_id
  || !!todoFilters.level
  || !!todoFilters.module_id
));

const todoLevelChips = computed(() => {
  const counts = todoSummary.value.level_counts || {};
  return (['P0', 'P1', 'P2', 'P3'] as const)
    .filter(level => (counts[level] || 0) > 0)
    .map(level => ({ level, count: counts[level] || 0 }));
});

const LEVEL_ORDER: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };

function sortByPriority(list: ManualTestAssignment[]) {
  return [...list].sort((a, b) => {
    const la = LEVEL_ORDER[a.testcase_detail?.level] ?? 9;
    const lb = LEVEL_ORDER[b.testcase_detail?.level] ?? 9;
    if (la !== lb) return la - lb;
    return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
  });
}

const caseColumns: TableColumnData[] = [
  { title: '测试用例', slotName: 'case', width: 320 },
  { title: '结果', slotName: 'result', width: 90 },
  { title: '失败原因', slotName: 'reason', width: 160 },
  { title: '关联缺陷', slotName: 'defect', width: 140 },
  { title: '执行时间', slotName: 'executedAt', width: 150 },
  { title: '操作', slotName: 'actions', width: 110 },
];

const stepExecColumns: TableColumnData[] = [
  { title: '步骤', dataIndex: 'step_number', width: 60 },
  { title: '描述', dataIndex: 'description', ellipsis: true, tooltip: true },
  { title: '预期结果', dataIndex: 'expected_result', ellipsis: true, tooltip: true },
  { title: '步骤结果', slotName: 'stepResult', width: 220 },
];

const stepColumns: TableColumnData[] = [
  { title: '步骤', dataIndex: 'step_number', width: 70 },
  { title: '描述', dataIndex: 'description' },
  { title: '预期结果', dataIndex: 'expected_result' },
];

const caseTree = computed(() => {
  const map = new Map<number, any>();
  const roots: any[] = [];
  modules.value.forEach(m => map.set(m.id, { key: `m-${m.id}`, title: m.name, children: [] }));
  modules.value.forEach(m => {
    const p = m.parent_id ?? m.parent;
    if (p && map.has(p)) map.get(p).children.push(map.get(m.id));
    else roots.push(map.get(m.id));
  });
  testcases.value.forEach(t => {
    const leaf = { key: t.id, title: `#${t.id} ${t.name}` };
    if (t.module_id && map.has(t.module_id)) map.get(t.module_id).children.push(leaf);
    else roots.push(leaf);
  });
  return roots;
});

const moduleCaseIdsMap = computed(() => {
  const casesByModule = new Map<number, number[]>();
  testcases.value.forEach(t => {
    if (!t.module_id) return;
    if (!casesByModule.has(t.module_id)) casesByModule.set(t.module_id, []);
    casesByModule.get(t.module_id)!.push(t.id);
  });
  const childrenByParent = new Map<number, number[]>();
  modules.value.forEach(m => {
    const p = m.parent_id ?? m.parent;
    if (p == null) return;
    if (!childrenByParent.has(p)) childrenByParent.set(p, []);
    childrenByParent.get(p)!.push(m.id);
  });
  const collectModuleIds = (moduleId: number): number[] => {
    const ids = [moduleId];
    (childrenByParent.get(moduleId) || []).forEach(child => ids.push(...collectModuleIds(child)));
    return ids;
  };
  const map = new Map<string, number[]>();
  modules.value.forEach(m => {
    map.set(`m-${m.id}`, [...new Set(collectModuleIds(m.id).flatMap(id => casesByModule.get(id) || []))]);
  });
  return map;
});

const allCaseIds = computed(() => testcases.value.map(t => t.id));

const filteredAssignments = computed(() => {
  let list = selectedRun.value?.assignments || [];
  if (caseFilters.status) list = list.filter(item => item.status === caseFilters.status);
  if (caseFilters.keyword.trim()) {
    const keyword = caseFilters.keyword.trim().toLowerCase();
    list = list.filter(item => (item.testcase_detail?.name || '').toLowerCase().includes(keyword) || String(item.testcase).includes(keyword));
  }
  return list;
});

const executionNavList = computed(() => {
  if (!selectedRun.value) return [];
  if (returnTab.value === 'todo') {
    return sortByPriority(selectedRun.value.assignments.filter(item => item.status === 'pending'));
  }
  return filteredAssignments.value;
});

watch(filteredAssignments, (list) => {
  casePagination.total = list.length;
  if ((casePagination.current - 1) * casePagination.pageSize >= list.length) {
    casePagination.current = 1;
  }
}, { immediate: true });

function selectAllCases(mode: 'create' | 'edit') {
  const ids = [...allCaseIds.value];
  if (mode === 'create') form.value.testcase_ids = ids;
  else editCaseIds.value = ids;
}

function clearAllCases(mode: 'create' | 'edit') {
  if (mode === 'create') form.value.testcase_ids = [];
  else {
    editCaseIds.value = [];
    orphanedSnapshotCaseIds.value = [];
  }
}

function toCaseIds(keys: any[]) {
  return (keys || []).map((k: any) => (typeof k === 'number' ? k : Number(k))).filter((id: number) => Number.isInteger(id) && id > 0);
}

function splitAssignedCaseIds(run: any) {
  const availableIds = new Set(testcases.value.map(t => t.id));
  const assignedIds = toCaseIds((run?.assignments || []).map((item: any) => item.testcase));
  return {
    visible: assignedIds.filter(id => availableIds.has(id)),
    orphaned: assignedIds.filter(id => !availableIds.has(id)),
  };
}

function getCaseIds(mode: 'create' | 'edit') {
  return toCaseIds(mode === 'create' ? form.value.testcase_ids : editCaseIds.value);
}

function applyCaseIds(mode: 'create' | 'edit', ids: number[]) {
  const next = [...new Set(ids)];
  if (mode === 'create') form.value.testcase_ids = next;
  else editCaseIds.value = next;
}

function toggleModuleByKey(moduleKey: string, mode: 'create' | 'edit') {
  const moduleCaseIds = moduleCaseIdsMap.value.get(moduleKey) || [];
  if (!moduleCaseIds.length) return;
  const current = new Set(getCaseIds(mode));
  const allSelected = moduleCaseIds.every(id => current.has(id));
  if (allSelected) moduleCaseIds.forEach(id => current.delete(id));
  else moduleCaseIds.forEach(id => current.add(id));
  applyCaseIds(mode, [...current]);
}

function onModuleTitleClick(key: string | number, mode: 'create' | 'edit') {
  const moduleKey = String(key);
  if (moduleKey.startsWith('m-')) toggleModuleByKey(moduleKey, mode);
}

function handleTreeCheck(checkedKeys: any[], event: any, mode: 'create' | 'edit') {
  const moduleKey = event?.node?.key != null ? String(event.node.key) : '';
  if (moduleKey.startsWith('m-')) {
    toggleModuleByKey(moduleKey, mode);
    return;
  }
  applyCaseIds(mode, toCaseIds(checkedKeys));
}

function onTreeCheck(keys: any[], event: any) { handleTreeCheck(keys, event, 'create'); }
function onEditTreeCheck(keys: any[], event: any) { handleTreeCheck(keys, event, 'edit'); }

const resultText = (s: string) => ({ pending: '待执行', pass: '通过', fail: '不通过', blocked: '阻塞', skip: '跳过' }[s] || s);
const resultColor = (s: string) => ({ pending: 'gray', pass: 'green', fail: 'red', blocked: 'orangered', skip: 'arcoblue' }[s] || 'gray');
const runStatusText = (s: string) => ({ pending: '待执行', in_progress: '执行中', completed: '已完成' }[s] || s);
const runStatusColor = (s: string) => ({ pending: 'gray', in_progress: 'arcoblue', completed: 'green' }[s] || 'gray');

function executedCount(record: { passed_count?: number; failed_count?: number; blocked_count?: number; skip_count?: number }) {
  return (record.passed_count || 0) + (record.failed_count || 0) + (record.blocked_count || 0) + (record.skip_count || 0);
}

function runSegment(record: any, key: 'pass' | 'fail' | 'blocked' | 'skip' | 'pending') {
  const total = record.total_count || 0;
  if (!total) return 0;
  const map = {
    pass: record.passed_count || 0,
    fail: record.failed_count || 0,
    blocked: record.blocked_count || 0,
    skip: record.skip_count || 0,
    pending: record.pending_count || 0,
  };
  return (map[key] / total) * 100;
}

function buildStepResults(item: ManualTestAssignment): ManualStepResult[] {
  const steps = item.testcase_detail?.steps || [];
  const existing = new Map((item.step_results || []).map(step => [step.step_number, step]));
  return steps.map((step: { step_number: number }) => ({
    step_number: step.step_number,
    status: existing.get(step.step_number)?.status || 'pending',
    comment: existing.get(step.step_number)?.comment || '',
  }));
}

function getStepResult(stepNumber: number) {
  return resultForm.value.step_results.find(item => item.step_number === stepNumber) || { step_number: stepNumber, status: 'pending' as ManualResultStatus, comment: '' };
}

function setStepResult(stepNumber: number, patch: Partial<ManualStepResult>) {
  const index = resultForm.value.step_results.findIndex(item => item.step_number === stepNumber);
  if (index >= 0) {
    resultForm.value.step_results[index] = { ...resultForm.value.step_results[index], ...patch };
  } else {
    resultForm.value.step_results.push({ step_number: stepNumber, status: 'pending', comment: '', ...patch });
  }
}

function removeEvidence(index: number) {
  resultForm.value.evidence_files.splice(index, 1);
}

async function handleEvidenceUpload(option: any) {
  if (!projectId.value || !active.value) return;
  evidenceUploading.value = true;
  try {
    const updated = await uploadManualEvidence(projectId.value, active.value.id, [option.fileItem.file as File]);
    resultForm.value.evidence_files = updated.evidence_files || [];
    active.value.evidence_files = updated.evidence_files || [];
    option.onSuccess?.(updated);
    Message.success('证据上传成功');
  } catch (e: any) {
    option.onError?.(e);
    Message.error(e.response?.data?.message || '证据上传失败');
  } finally {
    evidenceUploading.value = false;
  }
}

function openQuickBlocked(record: ManualTestAssignment) {
  quickBlockedTarget.value = record;
  quickBlockedReason.value = '';
  quickBlockedVisible.value = true;
}

async function confirmQuickBlocked() {
  if (!quickBlockedTarget.value) return false;
  if (!quickBlockedReason.value.trim()) {
    Message.warning('请填写阻塞原因');
    return false;
  }
  const ok = await quickSubmitResult(quickBlockedTarget.value, 'blocked', undefined, quickBlockedReason.value.trim());
  if (ok) quickBlockedVisible.value = false;
  return ok;
}
function onRunPageChange(page: number) { runPagination.current = page; loadRuns(); }
function onRunPageSizeChange(pageSize: number) { runPagination.pageSize = pageSize; runPagination.current = 1; loadRuns(); }
function onTodoPageChange(page: number) { todoPagination.current = page; loadTodoAssignments(); }
function onTodoPageSizeChange(pageSize: number) { todoPagination.pageSize = pageSize; todoPagination.current = 1; loadTodoAssignments(); }
function toggleTodoBatch(runId?: number) { todoFilters.run_id = todoFilters.run_id === runId ? undefined : runId; searchTodo(); }
function formatModuleShort(module?: string) {
  if (!module) return '未分配模块';
  const parts = module.split('>').map(part => part.trim()).filter(Boolean);
  if (parts.length <= 2) return module;
  return `${parts[parts.length - 2]} > ${parts[parts.length - 1]}`;
}
function levelColor(level: string) {
  if (level === 'P0') return 'red';
  if (level === 'P1') return 'orangered';
  if (level === 'P2') return 'arcoblue';
  return 'gray';
}
function runProgressPercent(record: ManualTestAssignment) {
  const total = record.run_total_count || 0;
  if (!total) return 0;
  return ((record.run_passed_count || 0) + (record.run_failed_count || 0)) / total;
}
function onCasePageChange(page: number) { casePagination.current = page; }
function onCasePageSizeChange(pageSize: number) { casePagination.pageSize = pageSize; casePagination.current = 1; }

async function loadAllTestCases(projectIdValue: number) {
  const pageSize = 200;
  const first = await getTestCaseList(projectIdValue, { page: 1, pageSize });
  if (!first.success || !first.data) throw new Error(first.error || '获取测试用例失败');
  const total = first.total ?? first.data.length;
  const allCases = [...first.data];
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  if (totalPages > 1) {
    const responses = await Promise.all(Array.from({ length: totalPages - 1 }, (_, index) => getTestCaseList(projectIdValue, { page: index + 2, pageSize })));
    responses.forEach(response => { if (response.success && response.data) allCases.push(...response.data); });
  }
  return allCases;
}

function resetOptionsCache() {
  optionsProjectId.value = null;
  testcases.value = [];
  modules.value = [];
  members.value = [];
  optionsLoadPromise = null;
}

async function loadOptionsInternal() {
  if (!projectId.value) return;
  const currentProjectId = projectId.value;
  const [membersResp, cases, modulesResp] = await Promise.all([
    getProjectMembers(currentProjectId),
    loadAllTestCases(currentProjectId),
    getTestCaseModules(currentProjectId),
  ]);
  if (projectId.value !== currentProjectId) return;
  members.value = membersResp.data || [];
  testcases.value = cases || [];
  modules.value = modulesResp.data || [];
  optionsProjectId.value = currentProjectId;
}

async function ensureOptionsLoaded(force = false) {
  if (!projectId.value) return;
  if (!force && optionsProjectId.value === projectId.value && testcases.value.length > 0) return;
  if (optionsLoadPromise && !force) return optionsLoadPromise;
  optionsLoading.value = true;
  optionsLoadPromise = loadOptionsInternal().finally(() => {
    optionsLoading.value = false;
    optionsLoadPromise = null;
  });
  return optionsLoadPromise;
}

function applyEditSelection(run: any) {
  const split = splitAssignedCaseIds(run);
  editCaseIds.value = split.visible;
  orphanedSnapshotCaseIds.value = split.orphaned;
  editForm.value.assignee_id = run.assignments?.[0]?.assignee;
  initialEditAssigneeId.value = editForm.value.assignee_id;
}

function buildRunQueryParams() {
  const params: Record<string, any> = { page: runPagination.current, pageSize: runPagination.pageSize };
  if (filters.search.trim()) params.search = filters.search.trim();
  if (filters.status) params.status = filters.status;
  if (filters.assignee_id) params.assignee_id = filters.assignee_id;
  if (filters.environment) params.environment = filters.environment;
  if (filters.version.trim()) params.version = filters.version.trim();
  if (filters.dateRange?.[0]) params.created_from = filters.dateRange[0];
  if (filters.dateRange?.[1]) params.created_to = filters.dateRange[1];
  return params;
}

function buildTeamQueryParams() {
  const params: Record<string, any> = { page: teamPagination.current, pageSize: teamPagination.pageSize };
  if (teamFilters.search.trim()) params.search = teamFilters.search.trim();
  if (teamFilters.status) params.status = teamFilters.status;
  if (teamFilters.assignee_id) params.assignee_id = teamFilters.assignee_id;
  return params;
}

function buildTodoQueryParams() {
  const params: Record<string, any> = { page: todoPagination.current, pageSize: todoPagination.pageSize };
  if (todoFilters.search.trim()) params.search = todoFilters.search.trim();
  if (todoFilters.status) params.status = todoFilters.status;
  if (todoFilters.run_id) params.run_id = todoFilters.run_id;
  if (todoFilters.level) params.level = todoFilters.level;
  if (todoFilters.module_id) params.module_id = todoFilters.module_id;
  if (authStore.user?.id) params.assignee_id = authStore.user.id;
  return params;
}

function buildNextPendingParams() {
  const params: Record<string, any> = {};
  if (authStore.user?.id) params.assignee_id = authStore.user.id;
  if (todoFilters.run_id) params.run_id = todoFilters.run_id;
  if (todoFilters.level) params.level = todoFilters.level;
  if (todoFilters.module_id) params.module_id = todoFilters.module_id;
  if (todoFilters.search.trim()) params.search = todoFilters.search.trim();
  return params;
}

async function loadNextPending() {
  if (!projectId.value) return;
  try {
    nextPendingAssignment.value = await getNextPendingAssignment(projectId.value, buildNextPendingParams());
  } catch {
    nextPendingAssignment.value = null;
  }
}

async function loadTodoSummary() {
  if (!projectId.value) return;
  try {
    const params: Record<string, any> = {};
    if (authStore.user?.id) params.assignee_id = authStore.user.id;
    todoSummary.value = await getManualTodoSummary(projectId.value, params);
  } catch {
    todoSummary.value = { pending_count: 0, run_count: 0, runs: [], today_completed_count: 0, overdue_count: 0, level_counts: {} };
  }
}

function todoFiltersStorageKey(id: number) {
  return `${TODO_FILTERS_PREFIX}${id}`;
}

function loadTodoFiltersFromStorage(projectIdValue: number) {
  try {
    const raw = localStorage.getItem(todoFiltersStorageKey(projectIdValue));
    if (!raw) return;
    const saved = JSON.parse(raw);
    todoFilters.search = saved.search || '';
    todoFilters.status = saved.status ?? 'pending';
    todoFilters.run_id = saved.run_id;
    todoFilters.level = saved.level;
    todoFilters.module_id = saved.module_id;
    if (saved.pageSize) todoPagination.pageSize = saved.pageSize;
  } catch {
    // ignore invalid storage
  }
}

function saveTodoFiltersToStorage() {
  if (!projectId.value) return;
  try {
    localStorage.setItem(todoFiltersStorageKey(projectId.value), JSON.stringify({
      search: todoFilters.search,
      status: todoFilters.status,
      run_id: todoFilters.run_id,
      level: todoFilters.level,
      module_id: todoFilters.module_id,
      pageSize: todoPagination.pageSize,
    }));
  } catch {
    // ignore quota errors
  }
}

function resetTodoFiltersToDefault() {
  todoFilters.search = '';
  todoFilters.status = 'pending';
  todoFilters.run_id = undefined;
  todoFilters.level = undefined;
  todoFilters.module_id = undefined;
  todoPagination.pageSize = 5;
}

function openQuickFail(record: ManualTestAssignment) {
  quickFailTarget.value = record;
  quickFailReason.value = '';
  quickFailVisible.value = true;
}

async function confirmQuickFail() {
  if (!quickFailTarget.value) return false;
  if (!quickFailReason.value.trim()) {
    Message.warning('请填写失败原因');
    return false;
  }
  const ok = await quickSubmitResult(quickFailTarget.value, 'fail', quickFailReason.value.trim());
  if (ok) quickFailVisible.value = false;
  return ok;
}

async function quickSubmitResult(
  record: ManualTestAssignment,
  status: 'pass' | 'fail' | 'blocked' | 'skip',
  failureReason?: string,
  comment = '',
) {
  if (!projectId.value) return false;
  quickSubmittingId.value = record.id;
  try {
    await submitManualResult(projectId.value, record.id, {
      status,
      failure_reason: failureReason || '',
      comment,
      step_results: record.step_results || [],
      evidence_files: record.evidence_files || [],
    });
    const label = { pass: '已标记通过', fail: '已标记不通过', blocked: '已标记阻塞', skip: '已标记跳过' }[status];
    Message.success(label);
    if (selectedRun.value?.id === record.run) await refreshSelectedRun(record.run);
    await loadRuns();
    if (activeTab.value === 'todo' || returnTab.value === 'todo') await loadTodoAssignments();
    else if (activeTab.value === 'team' || returnTab.value === 'team') await loadTeamAssignments();
    else await loadTodoSummary();
    return true;
  } catch (e: any) {
    Message.error(e.response?.data?.message || '操作失败');
    return false;
  } finally {
    quickSubmittingId.value = null;
  }
}

function resetFilters() {
  filters.search = '';
  filters.status = undefined;
  filters.assignee_id = undefined;
  filters.environment = undefined;
  filters.version = '';
  filters.dateRange = [];
  searchRuns();
}

function resetTeamFilters() {
  teamFilters.search = '';
  teamFilters.status = 'pending';
  teamFilters.assignee_id = undefined;
  searchTeam();
}

function searchTeam() {
  teamPagination.current = 1;
  loadTeamAssignments();
}

function toggleTeamMember(assigneeId?: number) {
  teamFilters.assignee_id = teamFilters.assignee_id === assigneeId ? undefined : assigneeId;
  searchTeam();
}

function onTeamPageChange(page: number) { teamPagination.current = page; loadTeamAssignments(); }
function onTeamPageSizeChange(pageSize: number) { teamPagination.pageSize = pageSize; teamPagination.current = 1; loadTeamAssignments(); }

async function loadTeamSummary() {
  if (!projectId.value) return;
  try {
    const params: Record<string, any> = {};
    if (teamFilters.assignee_id) params.assignee_id = teamFilters.assignee_id;
    teamSummary.value = await getTeamTodoSummary(projectId.value, params);
  } catch {
    teamSummary.value = { pending_count: 0, run_count: 0, overdue_count: 0, members: [] };
  }
}

async function loadTeamAssignments() {
  if (!projectId.value || !isManager.value) return;
  teamLoading.value = true;
  try {
    await loadTeamSummary();
    const { results, total } = await getManualAssignments(projectId.value, buildTeamQueryParams());
    teamAssignments.value = results;
    teamPagination.total = total;
  } catch (e: any) {
    Message.error(e.response?.data?.message || '获取团队待办失败');
  } finally {
    teamLoading.value = false;
  }
}

function resetTodoFilters() {
  resetTodoFiltersToDefault();
  saveTodoFiltersToStorage();
  searchTodo();
}

function searchRuns() {
  runPagination.current = 1;
  loadRuns();
}

function searchTodo() {
  todoPagination.current = 1;
  loadTodoAssignments();
}

async function loadRuns() {
  if (!projectId.value) return;
  loading.value = true;
  try {
    const { results, total } = await getManualRuns(projectId.value, buildRunQueryParams());
    runs.value = results;
    runPagination.total = total;
  } catch (e: any) {
    Message.error(e.response?.data?.message || '获取执行任务失败');
  } finally {
    loading.value = false;
  }
}

async function loadTodoAssignments() {
  if (!projectId.value) return;
  todoLoading.value = true;
  try {
    await loadTodoSummary();
    const { results, total } = await getManualAssignments(projectId.value, buildTodoQueryParams());
    todoAssignments.value = results;
    todoPagination.total = total;
    await loadNextPending();
  } catch (e: any) {
    Message.error(e.response?.data?.message || '获取待办用例失败');
  } finally {
    todoLoading.value = false;
  }
}

async function refreshSelectedRun(runId?: number) {
  const id = runId || selectedRun.value?.id;
  if (!projectId.value || !id) return null;
  const detail = await getManualRun(projectId.value, id);
  selectedRun.value = detail;
  return detail;
}

async function openRun(run: ManualTestRunListItem | ManualTestRunDetail) {
  if (!projectId.value) return;
  casePagination.current = 1;
  caseFilters.status = undefined;
  caseFilters.keyword = '';
  loading.value = true;
  try {
    selectedRun.value = await getManualRun(projectId.value, run.id);
  } catch (e: any) {
    Message.error(e.response?.data?.message || '获取任务详情失败');
  } finally {
    loading.value = false;
  }
}

async function openRunById(runId: number, assignmentId?: number, fromTodo = false) {
  if (!projectId.value) return;
  if (fromTodo) returnTab.value = 'todo';
  else if (activeTab.value === 'team') returnTab.value = 'team';
  casePagination.current = 1;
  loading.value = true;
  try {
    const detail = await getManualRun(projectId.value, runId);
    selectedRun.value = detail;
    activeTab.value = 'runs';
    if (assignmentId) {
      const assignment = detail.assignments.find(item => item.id === assignmentId);
      if (assignment) openCase(assignment);
    }
  } catch (e: any) {
    Message.error(e.response?.data?.message || '获取任务详情失败');
  } finally {
    loading.value = false;
  }
}

function closeRunDetail() {
  selectedRun.value = null;
  caseVisible.value = false;
  active.value = null;
  activeTab.value = returnTab.value;
  if (activeTab.value === 'todo') {
    loadTodoAssignments();
  } else if (activeTab.value === 'team') {
    loadTeamAssignments();
  } else {
    loadRuns();
  }
  returnTab.value = 'runs';
}

function openReport(run: { id: number; name: string }) {
  reportRunId.value = run.id;
  reportRunName.value = run.name;
  reportVisible.value = true;
}

async function exportCurrentRun() {
  if (!projectId.value || !selectedRun.value) return;
  exporting.value = true;
  try {
    await exportManualRunExcel(projectId.value, selectedRun.value.id, selectedRun.value.name);
    Message.success('导出成功');
  } catch {
    Message.error('导出失败');
  } finally {
    exporting.value = false;
  }
}

async function loadTestSuites() {
  if (!projectId.value) return;
  const response = await getTestSuiteList(projectId.value);
  testSuites.value = response.success && response.data ? response.data : [];
}

async function onCreateSuiteChange(suiteId?: number) {
  if (!projectId.value || !suiteId) {
    form.value.testcase_ids = [];
    return;
  }
  const response = await getTestSuiteDetail(projectId.value, suiteId);
  if (response.success && response.data?.testcases_detail) {
    form.value.testcase_ids = response.data.testcases_detail.map(item => item.id);
    if (!form.value.name.trim()) form.value.name = `${response.data.name} 人工执行`;
  }
}

async function openCreate() {
  createVisible.value = true;
  await Promise.all([ensureOptionsLoaded(), loadTestSuites()]);
  const ids = String(route.query.testcase_ids || '').split(',').map(Number).filter(Boolean);
  if (ids.length) form.value.testcase_ids = ids;
  const suiteId = Number(route.query.suite_id);
  if (suiteId) {
    form.value.testsuite_id = suiteId;
    await onCreateSuiteChange(suiteId);
  }
}

async function openTaskEdit(run: ManualTestRunListItem | ManualTestRunDetail) {
  if (!projectId.value) return;
  editVisible.value = true;
  await ensureOptionsLoaded();
  try {
    const detail = 'assignments' in run && run.assignments ? run as ManualTestRunDetail : await getManualRun(projectId.value, run.id);
    editingRun.value = detail;
    editForm.value = {
      name: detail.name,
      description: detail.description || '',
      environment: detail.environment || undefined,
      version: detail.version || '',
      deadline: detail.deadline || undefined,
      assignee_id: detail.assignments?.[0]?.assignee,
    };
    applyEditSelection(detail);
  } catch (e: any) {
    Message.error(e.response?.data?.message || '获取任务详情失败');
    editVisible.value = false;
  }
}

async function createRun() {
  if (!projectId.value || !form.value.name || !form.value.assignee_id) {
    Message.warning('请填写批次和测试人员');
    return false;
  }
  if (!form.value.testsuite_id && !form.value.testcase_ids.length) {
    Message.warning('请选择测试用例或测试套件');
    return false;
  }
  saving.value = true;
  saveStatus.value = '正在创建执行任务...';
  try {
    const payload: Record<string, any> = {
      name: form.value.name,
      description: form.value.description,
      environment: form.value.environment,
      version: form.value.version,
      deadline: form.value.deadline || null,
      assignee_id: form.value.assignee_id,
    };
    if (form.value.testsuite_id) payload.testsuite_id = form.value.testsuite_id;
    else payload.testcase_ids = form.value.testcase_ids;
    await createManualTestRun(projectId.value, payload as any);
    form.value = {
      name: '', description: '', environment: undefined, version: '', deadline: undefined,
      testsuite_id: undefined, assignee_id: undefined, testcase_ids: [],
    };
    saveStatus.value = '正在刷新列表...';
    await loadRuns();
    Message.success('已创建执行任务');
    return true;
  } catch (e: any) {
    Message.error(e.response?.data?.message || '分派失败');
    return false;
  } finally {
    saving.value = false;
    saveStatus.value = '';
  }
}

function openCase(item: ManualTestAssignment) {
  const list = executionNavList.value;
  activeIndex.value = list.findIndex(x => x.id === item.id);
  active.value = item;
  resultForm.value = {
    status: item.status,
    failure_reason: item.failure_reason || '',
    comment: item.comment || '',
    step_results: buildStepResults(item),
    evidence_files: [...(item.evidence_files || [])],
    defect_title: item.defect_title || '',
    defect_url: item.defect_url || '',
  };
  caseVisible.value = true;
}

function goCase(step: number) {
  const list = executionNavList.value;
  const next = list[activeIndex.value + step];
  if (next) openCase(next);
}

async function saveResult(andNext = false) {
  if (!active.value || !projectId.value || !selectedRun.value) return;
  if (resultForm.value.status === 'fail' && !resultForm.value.failure_reason.trim()) {
    Message.warning('请填写失败原因');
    return;
  }
  if (resultForm.value.status === 'blocked' && !resultForm.value.comment.trim()) {
    Message.warning('请填写阻塞原因');
    return;
  }
  const savedId = active.value.id;
  const savedRunId = selectedRun.value.id;
  saving.value = true;
  try {
    await submitManualResult(projectId.value, active.value.id, {
      status: resultForm.value.status,
      failure_reason: resultForm.value.failure_reason,
      comment: resultForm.value.comment,
      step_results: resultForm.value.step_results,
      evidence_files: resultForm.value.evidence_files,
      defect_title: resultForm.value.defect_title,
      defect_url: resultForm.value.defect_url,
    });
    const detail = await refreshSelectedRun(selectedRun.value.id);
    await loadRuns();
    if (activeTab.value === 'todo' || returnTab.value === 'todo') {
      await loadTodoAssignments();
    } else if (activeTab.value === 'team' || returnTab.value === 'team') {
      await loadTeamAssignments();
    }
    Message.success('结果已保存');

    if (!andNext) {
      if (detail && active.value) {
        active.value = detail.assignments.find(item => item.id === savedId) || active.value;
        activeIndex.value = executionNavList.value.findIndex(item => item.id === savedId);
      }
      return;
    }

    if (returnTab.value === 'todo') {
      const pendingInRun = sortByPriority((detail?.assignments || []).filter(item => item.status === 'pending'));
      if (pendingInRun.length && detail?.id === savedRunId) {
        openCase(pendingInRun[0]);
        return;
      }
      await loadNextPending();
      if (nextPendingAssignment.value) {
        if (nextPendingAssignment.value.run !== savedRunId) {
          Message.info('本批次待办已完成，已进入下一批次');
        }
        await openRunById(nextPendingAssignment.value.run, nextPendingAssignment.value.id, true);
        return;
      }
      Message.success('全部待办已完成');
      caseVisible.value = false;
      closeRunDetail();
      return;
    }

    selectedRun.value = detail;
    const nextItem = executionNavList.value[activeIndex.value + 1];
    if (nextItem) openCase(nextItem);
    else caseVisible.value = false;
  } catch (e: any) {
    Message.error(e.response?.data?.message || '保存失败');
  } finally {
    saving.value = false;
  }
}

async function removeTask(run: ManualTestRunListItem) {
  if (!projectId.value) return;
  try {
    await new Promise<void>((resolve, reject) => Modal.confirm({
      title: '删除执行任务',
      content: `确定删除「${run.name}」吗？任务下的执行记录也会删除。`,
      okText: '确定删除',
      cancelText: '取消',
      closable: true,
      hideCancel: false,
      onOk: () => resolve(),
      onCancel: () => reject(new Error('cancel')),
    }));
    await deleteManualRun(projectId.value, run.id);
    await loadRuns();
    Message.success('任务已删除');
  } catch (e: any) {
    if (e?.message !== 'cancel') Message.error(e.response?.data?.message || '删除失败');
  }
}

async function saveTaskEdit() {
  if (!projectId.value || !editingRun.value || !editForm.value.name || !editForm.value.assignee_id) {
    Message.warning('请填写执行批次和测试人员');
    return false;
  }
  const run = editingRun.value;
  const split = splitAssignedCaseIds(run);
  const currentIds = split.visible;
  const initialOrphaned = split.orphaned;
  const availableIds = new Set(testcases.value.map(t => t.id));
  const selected = new Set(toCaseIds(editCaseIds.value));
  const assigneeId = editForm.value.assignee_id;
  const toRemove = currentIds.filter(id => !selected.has(id));
  const toRemoveOrphaned = initialOrphaned.filter(id => !orphanedSnapshotCaseIds.value.includes(id));
  const additions = [...selected].filter(id => availableIds.has(id) && !currentIds.includes(id));

  saving.value = true;
  saveStatus.value = '正在更新任务信息...';
  try {
    await updateManualRun(projectId.value, run.id, {
      name: editForm.value.name,
      description: editForm.value.description,
      environment: editForm.value.environment || '',
      version: editForm.value.version || '',
      deadline: editForm.value.deadline || null,
    });
    if (initialEditAssigneeId.value && initialEditAssigneeId.value !== assigneeId) {
      saveStatus.value = '正在转派测试人员...';
      await reassignManualRun(projectId.value, run.id, assigneeId);
    }
    if (toRemove.length || toRemoveOrphaned.length) {
      saveStatus.value = `正在移除用例（${toRemove.length + toRemoveOrphaned.length} 条）...`;
      await Promise.all([...toRemove, ...toRemoveOrphaned].map(testcaseId => removeManualRunCase(projectId.value!, run.id, testcaseId)));
    }
    if (additions.length) {
      saveStatus.value = `正在添加用例（${additions.length} 条）...`;
      await addManualRunCases(projectId.value, run.id, { testcase_ids: additions, assignee_id: assigneeId });
    }
    saveStatus.value = '正在刷新列表...';
    await loadRuns();
    if (selectedRun.value?.id === run.id) await refreshSelectedRun(run.id);
    Message.success('任务已更新');
    return true;
  } catch (e: any) {
    Message.error(e.response?.data?.message || e.message || '更新失败');
    return false;
  } finally {
    saving.value = false;
    saveStatus.value = '';
  }
}

function onTabChange(key: string | number) {
  if (key === 'todo') {
    ensureOptionsLoaded();
    loadTodoAssignments();
  } else if (key === 'team') {
    ensureOptionsLoaded();
    loadTeamSummary();
    loadTeamAssignments();
  } else {
    loadRuns();
  }
}

watch(activeTab, (key) => {
  if (key === 'todo' && projectId.value) loadTodoSummary();
});

function progressPercent(record: any) {
  if (!record.total_count) return 0;
  return Math.round(executedCount(record) / record.total_count * 100);
}

function formatRunDate(value?: string) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
}

watch(projectId, (id) => {
  selectedRun.value = null;
  resetOptionsCache();
  filters.search = '';
  filters.status = undefined;
  filters.assignee_id = undefined;
  filters.dateRange = [];
  resetTodoFiltersToDefault();
  runPagination.current = 1;
  todoPagination.current = 1;
  if (id) loadTodoFiltersFromStorage(id);
  loadRuns();
  loadTodoSummary();
  ensureOptionsLoaded();
}, { immediate: true });

watch(todoFilters, () => saveTodoFiltersToStorage(), { deep: true });
watch(() => todoPagination.pageSize, () => saveTodoFiltersToStorage());

watch(() => route.query.run_id, async (runId) => {
  const id = Number(runId);
  if (projectId.value && id) await openRunById(id);
}, { immediate: true });

watch(() => route.query.suite_id, async (suiteId) => {
  const id = Number(suiteId);
  if (projectId.value && id) {
    activeTab.value = 'runs';
    await openCreate();
  }
}, { immediate: true });
</script>

<style scoped>
.manual-execution { height: 100%; overflow-y: auto; overflow-x: hidden; box-sizing: border-box; padding: 16px 24px 24px; background: #f5f7fa; }
.page-heading { width: 100%; margin: 0 0 14px; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
.page-heading h2 { margin: 0; font-size: 22px; }
.page-heading span, .subtle { color: #86909c; font-size: 13px; }
.title-line { display: flex; align-items: center; gap: 12px; }
.surface { width: 100%; margin: 0; padding: 16px 18px 20px; background: #fff; border: 1px solid #e5e6eb; border-radius: 8px; box-sizing: border-box; }
.surface :deep(.arco-tabs-nav) { margin-bottom: 4px; }
.surface :deep(.arco-tabs-content) { padding-top: 0; }
.toolbar { display: flex; align-items: flex-start; padding-bottom: 14px; margin-bottom: 14px; border-bottom: 1px solid #f2f3f5; flex-shrink: 0; }
.toolbar :deep(.arco-space) { width: 100%; }
.surface :deep(.arco-table-th) { height: 40px; font-size: 13px; background: #f7f8fa; }
.surface :deep(.arco-table-td) { padding-top: 10px; padding-bottom: 10px; font-size: 13px; }
.surface :deep(.arco-progress-line) { min-width: 88px; }
.run-progress-stack { display: flex; flex-direction: column; gap: 4px; }
.run-progress-bar { display: flex; width: 100%; min-width: 120px; height: 8px; border-radius: 4px; overflow: hidden; background: #f2f3f5; }
.run-progress-bar .seg { display: block; height: 100%; min-width: 0; }
.run-progress-bar .seg.pass { background: #00b42a; }
.run-progress-bar .seg.fail { background: #f53f3f; }
.run-progress-bar .seg.blocked { background: #ff7d00; }
.run-progress-bar .seg.skip { background: #86909c; }
.run-progress-bar .seg.pending { background: #c9cdd4; }
.run-progress-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fail-count { color: #f53f3f; font-size: 12px; font-weight: 500; }
.evidence-list { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.evidence-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 8px; background: #f7f8fa; border-radius: 4px; }
.deadline-text { font-size: 12px; color: #86909c; }
.deadline-text.overdue { color: #f53f3f; }
.table-pagination { display: flex; justify-content: flex-end; padding: 14px 0 4px; border-top: 1px solid #f2f3f5; margin-top: 8px; }
.todo-panel { display: flex; flex-direction: column; gap: 12px; }
.todo-top-bar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; padding: 12px 14px; background: #f7f8fa; border: 1px solid #e5e6eb; border-radius: 8px; }
.todo-stats-block { display: flex; flex-direction: column; gap: 8px; }
.todo-stats-inline { display: flex; align-items: center; gap: 10px; color: #4e5969; font-size: 13px; white-space: nowrap; flex-wrap: wrap; }
.todo-stat-item strong { color: #165dff; font-size: 20px; margin-right: 4px; font-weight: 600; }
.todo-stat-item.todo-stat-warn strong { color: #f77234; }
.todo-stat-divider { color: #c9cdd4; }
.todo-level-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.todo-filters { flex: 1; justify-content: flex-end; }
.todo-batch-chips { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.chips-label { color: #86909c; font-size: 13px; }
.table-section { min-height: 200px; }
.case-cell strong { display: block; line-height: 1.4; }
.run-progress-line { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.picker { height: 340px; overflow: auto; padding: 10px; border: 1px solid #e5e6eb; border-radius: 4px; background: #fafbfc; }
.picker-head { display: flex; justify-content: space-between; align-items: center; padding: 0 4px 10px; margin-bottom: 8px; border-bottom: 1px solid #e5e6eb; }
.picker-actions { display: flex; align-items: center; gap: 4px; }
.picker-spin { display: block; min-height: 280px; }
.picker-spin :deep(.arco-spin-children) { min-height: 280px; }
.modal-save-spin { display: block; width: 100%; }
.modal-save-spin :deep(.arco-spin-mask) { background: rgba(255, 255, 255, .72); }
.tree-node-title { display: inline-block; width: 100%; cursor: pointer; }
.case-nav { display: flex; justify-content: space-between; align-items: center; padding: 0 0 16px; border-bottom: 1px solid #e5e6eb; }
.detail h4 { margin: 22px 0 8px; }
.detail p { white-space: pre-wrap; line-height: 1.7; color: #4e5969; }
@media (max-width: 900px) {
  .manual-execution { padding: 16px; }
  .surface { padding: 14px; overflow-x: auto; }
}
</style>
