/**
 * 任务注册表 —— 插件里「一个用户能力」的唯一声明处。
 *
 * 设计约定（见 docs/2026-10-19-task-centric-redesign.md §3）：
 * 入口、面板标题、提交参数、结果渲染器全部从这份声明派生，
 * 因此不会出现「写了入口忘了功能」或「面板里有第 7 段但菜单里没有入口」。
 */

export const TASKS = {
  analyze: {
    id: 'analyze',
    label: 'AI 打标',
    verb: '打标',
    summary: '为卡片生成一句话概览与标签建议',
    detail: '写入卡片概览；标签分「已有」与「建议新增」两类，新标签进建议队列待确认。',
    icon: 'sparkles',
    surface: 'analyze',
    modes: ['cards'],
    scopes: ['selection', 'folder', 'card', 'manual'],
    needsProfile: 'analyze',
    writes: ['note', 'tags'],
    submit: 'analyze',
  },
  polish: {
    id: 'polish',
    label: 'AI 世界书精简',
    verb: '精简',
    summary: '压缩世界书条目正文，写回前逐条校验关键信息是否丢失',
    detail: '原样返回也算合格结果；每条给出 token 变化（不判「失败」），'
      + '数据/代码类条目默认跳过，可在面板里强制处理。',
    icon: 'book',
    surface: 'polish',
    modes: ['worldinfo', 'cards'],
    scopes: ['selection', 'folder', 'manual'],
    needsProfile: 'wi_rewrite',
    writes: ['worldbook'],
    submit: 'wi-rewrite',
  },
  classify: {
    id: 'classify',
    label: 'AI 自动归类',
    verb: '归类',
    summary: '判断卡片应该放在哪个分类，并移动目录',
    detail: '先确定允许的目标分类，再让模型判断；低置信度的结果会标黄，可单独跳过。',
    icon: 'folder-tree',
    surface: 'classify',
    modes: ['cards'],
    scopes: ['selection', 'folder', 'manual'],
    needsProfile: 'classify',
    writes: ['move'],
    submit: 'classify',
  },
  propose: {
    id: 'propose',
    label: 'AI 设计分类体系',
    verb: '提议',
    summary: '分析整个卡片库，提出分类方案与标签池',
    detail: '只创建结构与标签，不动任何卡片；标签合并建议只展示，需人工处理。',
    icon: 'layers',
    surface: 'propose',
    modes: ['cards'],
    scopes: ['library', 'manual'],
    needsProfile: 'propose',
    writes: ['categories', 'tags'],
    submit: 'propose',
  },
};

/** 面板顺序（hub 与「打开任务」选择器共用）。 */
export const TASK_ORDER = ['analyze', 'polish', 'classify', 'propose'];

export function getTask(taskId) {
  return TASKS[String(taskId || '')] || null;
}

/** 该任务在某个视图里是否可用（入口 `when.mode` 的镜像，供面板内提示用）。 */
export function taskSupportsMode(task, mode) {
  if (!task) return false;
  return task.modes.includes(String(mode || ''));
}

/** 生成「任务 + 作用域」的一句话描述，用于按钮与标题。 */
export function taskActionLabel(task, scope) {
  if (!task) return '开始';
  const verb = task.verb || '处理';
  if (!scope) return `开始${verb}`;
  const unit = scope.mode === 'worldinfo' ? '本' : '张';
  return `开始${verb}（${scope.count} ${unit}）`;
}
