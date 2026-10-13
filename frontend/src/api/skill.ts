/**
 * Skill API 模块
 *
 * 对应后端 app/routers/skills.py 的端点。
 * - 创建任务时拉取所有可见 skill 供用户多选(allowed_skills)
 * - 技能管理页:上传 zip / 删除 / 详情 / 文件列表 / 文件内容读取
 */
import client from './client'

/** Skill 概要(后端 SkillSummaryResponse) */
export interface SkillSummary {
  /** skill 唯一标识(来自 SKILL.md frontmatter name) */
  name: string
  /** 简短说明(给用户选择时看) */
  description: string
  /** 所属场景 id(内置=目录名,用户上传=user_<uuid>) */
  scenario_id: string
  /** 当前用户是否可管理(仅自己上传的 skill 为 true) */
  owned: boolean
}

/** Skill 详情(后端 SkillResponse) */
export interface SkillDetail extends SkillSummary {
  /** SKILL.md 正文(frontmatter 之后的指令) */
  body: string
  /** SKILL.md 磁盘路径 */
  source_path: string
}

/** zip 上传结果(后端 SkillUploadResponse) */
export interface SkillUploadResult {
  skill: SkillDetail
  /** 是否覆盖了已存在的同名 skill */
  replaced: boolean
}

/** skill 目录内的单个文件(后端 SkillFileEntry) */
export interface SkillFileEntry {
  /** 相对 skill 目录的路径,'/' 分隔 */
  path: string
  /** 字节数 */
  size: number
}

/** skill 单文件内容(后端 SkillFileContentResponse) */
export interface SkillFileContent {
  path: string
  content: string
  size: number
}

/**
 * 列出当前用户可见的 skill(内置全局共享 + 自己上传的)
 *
 * 用于创建任务时的 skill 多选 UI 与技能管理页列表。
 * 后端 GET /skills 返回已注册且当前用户可见的 skill。
 */
export function getSkills(): Promise<SkillSummary[]> {
  return client.get('/skills').then((r) => r.data)
}

/**
 * 上传 zip 格式的 skill
 *
 * @param file .zip 文件(内含 SKILL.md,支持 <skill_name>/SKILL.md 或根目录 SKILL.md)
 * @param force 与自己的同名 skill 冲突时,true=覆盖
 */
export function uploadSkill(file: File, force: boolean): Promise<SkillUploadResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('force', String(force))
  // 覆盖实例默认的 application/json(否则 axios 不会自动切 multipart),
  // 置 undefined 让 axios 为 FormData 自动生成 boundary
  return client.post('/skills/upload', form, {
    headers: { 'Content-Type': undefined },
  }).then((r) => r.data)
}

/** 删除自己上传的 skill(内置/他人 skill 后端会拒绝) */
export function deleteSkill(
  scenarioId: string,
  skillName: string,
): Promise<void> {
  return client.delete(
    `/skills/${encodeURIComponent(scenarioId)}/${encodeURIComponent(skillName)}`,
  )
}

/**
 * 列出 skill 目录内的文件(SKILL.md 置顶)
 *
 * 用于技能管理页右侧栏的文件列表。
 */
export function getSkillFiles(
  scenarioId: string,
  skillName: string,
): Promise<SkillFileEntry[]> {
  return client
    .get(
      `/skills/${encodeURIComponent(scenarioId)}/${encodeURIComponent(skillName)}/files`,
    )
    .then((r) => r.data.files)
}

/** 读取 skill 目录内单个文件的 UTF-8 文本内容 */
export function getSkillFileContent(
  scenarioId: string,
  skillName: string,
  filePath: string,
): Promise<SkillFileContent> {
  return client
    .get(
      `/skills/${encodeURIComponent(scenarioId)}/${encodeURIComponent(skillName)}/files/${filePath
        .split('/')
        .map(encodeURIComponent)
        .join('/')}`,
    )
    .then((r) => r.data)
}

/**
 * 更新自己的 skill(直写 SKILL.md 全文,含 frontmatter)
 *
 * 仅 owner 可调用;保存后后端自动热刷新注册表。
 */
export function updateSkill(
  scenarioId: string,
  skillName: string,
  content: string,
): Promise<SkillDetail> {
  return client
    .post(
      `/skills/${encodeURIComponent(scenarioId)}/${encodeURIComponent(skillName)}`,
      { content },
    )
    .then((r) => r.data)
}
