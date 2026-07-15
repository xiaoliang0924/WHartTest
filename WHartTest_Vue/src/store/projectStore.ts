import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { getProjectList, type Project } from '@/services/projectService';

const PROJECT_STORAGE_KEY = 'selected_project_id';
const PROJECT_DEFAULT_MIGRATION_KEY = 'selected_project_default_migrated_to_work_order_ai';
const DEFAULT_PROJECT_NAMES = ['智慧AI工单系统', '智慧工单AI系统'];
const LEGACY_DEFAULT_PROJECT_NAME = '演示项目 (Demo Project)';

export const useProjectStore = defineStore('project', () => {
  const currentProject = ref<Project | null>(null);
  const projectList = ref<Project[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);

  const currentProjectId = computed(() => currentProject.value?.id || null);

  const projectOptions = computed(() =>
    projectList.value.map(project => ({
      label: project.name,
      value: project.id,
    })),
  );

  const getSavedProjectId = (): number | null => {
    try {
      const savedId = localStorage.getItem(PROJECT_STORAGE_KEY);
      return savedId ? parseInt(savedId, 10) : null;
    } catch (err) {
      console.warn('读取保存的项目ID失败:', err);
      return null;
    }
  };

  const saveProjectId = (projectId: number) => {
    try {
      localStorage.setItem(PROJECT_STORAGE_KEY, projectId.toString());
    } catch (err) {
      console.warn('保存项目ID失败:', err);
    }
  };

  const hasMigratedDefaultProject = (): boolean => {
    try {
      return localStorage.getItem(PROJECT_DEFAULT_MIGRATION_KEY) === 'true';
    } catch (err) {
      console.warn('读取默认项目迁移状态失败:', err);
      return false;
    }
  };

  const markDefaultProjectMigrated = () => {
    try {
      localStorage.setItem(PROJECT_DEFAULT_MIGRATION_KEY, 'true');
    } catch (err) {
      console.warn('保存默认项目迁移状态失败:', err);
    }
  };

  const pickDefaultProject = (savedProject: Project | null): Project | null => {
    const preferredDefaultProject = projectList.value.find(project =>
      DEFAULT_PROJECT_NAMES.includes(project.name),
    ) || null;

    if (
      preferredDefaultProject &&
      (!savedProject ||
        (savedProject.name === LEGACY_DEFAULT_PROJECT_NAME && !hasMigratedDefaultProject()))
    ) {
      markDefaultProjectMigrated();
      return preferredDefaultProject;
    }

    return savedProject || projectList.value[0] || null;
  };

  const fetchProjects = async () => {
    loading.value = true;
    error.value = null;

    try {
      const response = await getProjectList();

      if (response.success && response.data) {
        projectList.value = response.data;

        const savedProjectId = getSavedProjectId();
        const savedProject = savedProjectId
          ? projectList.value.find(project => project.id === savedProjectId) || null
          : null;
        const projectToSelect = pickDefaultProject(savedProject);

        if (projectToSelect) {
          setCurrentProject(projectToSelect);
        }
      } else {
        error.value = response.error || '获取项目列表失败';
      }
    } catch (err) {
      console.error('获取项目列表出错:', err);
      error.value = '获取项目列表时发生错误';
    } finally {
      loading.value = false;
    }
  };

  const setCurrentProject = (project: Project) => {
    currentProject.value = project;
    saveProjectId(project.id);
  };

  const setCurrentProjectById = (projectId: number) => {
    const project = projectList.value.find(project => project.id === projectId);
    if (project) {
      setCurrentProject(project);
    }
  };

  const reset = () => {
    currentProject.value = null;
    projectList.value = [];
    error.value = null;
    loading.value = false;

    try {
      localStorage.removeItem(PROJECT_STORAGE_KEY);
    } catch (err) {
      console.warn('清除保存的项目ID失败:', err);
    }
  };

  return {
    currentProject,
    currentProjectId,
    projectList,
    projectOptions,
    loading,
    error,
    fetchProjects,
    setCurrentProject,
    setCurrentProjectById,
    reset,
  };
});
