/**
 * stm-ai-assistant 前端入口。
 *
 * 契约：导出 `activate(api)`，可选导出 `deactivate(api)`。
 * 本次重写后，入口**不再自绘浮动按钮**：入口在 `manifest.frontend.entries` 里声明，
 * 由宿主渲染到坞、批量操作菜单、右键菜单与设置页；面板容器由宿主提供
 * （`static/js/pluginSurfaceHost.js`，其头部注释把本插件旧版的固定定位浮层列为反面教材）。
 *
 * 入口落点（4 个入口，各自落在不同的宿主位置）：
 * - `workbench`（dock）—— 入口坞 / 命令面板，主面板，承载自检状态点与待审批角标；
 * - `analyze-folder`（context，`when.target: "folder"`）—— 右键**分类** → 分析该分类全部卡片；
 * - `analyze-selected`（batch，`when.selection: "any"`）—— 批量操作菜单 → 分析选中卡片；
 * - `assistant-settings`（settings）—— 设置页入口。
 *
 * 插件剩下三件事：
 * 1. 注册 surface 与它们的 `mount(container, ctx)` 内容渲染器；
 * 2. 把运行期状态（自检结论、待审批角标、作业进度）回报给宿主入口；
 * 3. 在 `activate` 的返回值里交出清理逻辑（D9.1）。
 */

import { createClient } from './api.js';
import { createPanelBody, createPromptPreview } from './panel.js';
import { readSelectedCardIds } from './selection.js';

/**
 * 入口运行期状态回报器。
 *
 * **为什么需要它**：`PluginHost.loadAll()` 先 `loadPlugin()`（= 调用 `activate`），
 * **之后**才 `EntryRegistry.setPlugins(items)` 建表（`pluginHost.js:183-194`）。
 * 也就是说 `activate()` 执行期间注册表还是空的，此时 `api.entries.setStatus()` 会
 * 静默返回 `false`（`entryRegistry.js` 查不到条目就 return），自检结论会丢失。
 *
 * 因此这里把「期望状态」记下来，先尝试写一次，再订阅 `entries:changed`
 * （`setPlugins` 末尾会广播）重放——这样无论宿主将来调整加载顺序都能正确落地。
 *
 * **重放必须幂等**：`entries:changed` 既是通知、也是本函数的重放触发源，所以
 * 回调里写状态等于「写 → 通知 → 再写」。宿主已做两件事兜底（写入器只在值变化
 * 时广播；`notifyChange` 有重入合并与轮数上限），但插件侧也不该依赖宿主——
 * 这里记住**上次已写成功的值**，相同就跳过，避免无谓的写入与广播。
 */
function createEntryStatusReporter(api) {
  let status = '';
  let badge;
  let hasBadge = false;
  // 上次真正写成功的值（null 表示尚未写成功，需重放）
  let writtenStatus = null;
  let writtenBadge = undefined;

  const apply = () => {
    if (status && writtenStatus !== status) {
      if (api.entries.setStatus('workbench', status)) writtenStatus = status;
    }
    if (hasBadge && writtenBadge !== badge) {
      if (api.entries.setBadge('workbench', badge)) writtenBadge = badge;
    }
  };

  apply();
  const off = api.on('entries:changed', apply);

  return {
    setStatus(next) {
      const value = String(next || '');
      if (value === status) return;
      status = value;
      if (status && writtenStatus !== status) {
        if (api.entries.setStatus('workbench', status)) writtenStatus = status;
      }
    },
    /** 待审批角标；`null` 表示清除。挂在主面板入口 `workbench` 上。 */
    setPendingBadge(value) {
      const count = Number(value) || 0;
      badge = count > 0 ? (count > 99 ? '99+' : count) : null;
      hasBadge = true;
      if (writtenBadge === badge) return;
      if (api.entries.setBadge('workbench', badge)) writtenBadge = badge;
    },
    dispose() {
      if (typeof off === 'function') off();
    },
  };
}

export async function activate(api) {
  const client = createClient(api);

  // 跨视图共享的运行上下文（选中的卡片、当前作业、参数）
  const context = {
    selectedCardIds: [],
    /**
     * 本次入口打开的作用域（`kind: "context"` 的右键目标 / `kind: "batch"` 的选中集）。
     *
     * `null` 表示「无特定作用域」（坞 / 快捷键打开），此时沿用主程序当前选中集。
     * 非 null 时形如 `{ kind: 'folder' | 'card' | 'selection', path, count }`，
     * 由 `panel.applyEntryContext()` 从宿主传入的 `ctx` 折算。
     */
    openTarget: null,
    runId: '',
    providerId: '',
    profileId: '',
    model: '',
    mode: 'both',
    applyMode: 'review',
    dryRun: false,
    force: false,
    notifyReview: null,
    /** 由本函数注入：把 Prompt 预览交给 modal surface（见下）。 */
    openPromptPreview: null,
    /** 由本函数注入：待审批角标写入器（带注册表就绪重放）。 */
    setPendingBadge: null,
  };

  const reporter = createEntryStatusReporter(api);
  context.setPendingBadge = (value) => reporter.setPendingBadge(value);
  const panel = createPanelBody(api, client, context);
  const preview = createPromptPreview(api);

  // ---------------------------------------------------------- surface 注册

  // 面板：`surface.id` 必须与 manifest 保持一致（坞按 `<pluginId>:<surface.id ?? entry.id>` 查表）。
  api.surfaces.register('workbench', {
    title: 'AI 助手',
    description: '角色卡分析工作台',
    // 图标走**插件自己的品牌资源**：同一个插件的坞入口、右键菜单、批量菜单、
    // 入口总览与弹层标题都用它，辨识度一致（宿主解析为
    // `/api/plugins/stm-ai-assistant/assets/frontend/assets/icon.svg`）。
    icon: 'frontend/assets/icon.svg',
    // modal + lg：主工作台承载批量分析表单与审批列表，需要遮罩、焦点陷阱与
    // 完整宽度；`lg` 给出**固定**的 1080×760（切换页签时容器不再跳动）。
    type: 'modal',
    size: 'lg',
    /**
     * 状态胶囊放进**宿主的标题行**（与图标、标题、关闭按钮同一行）。
     *
     * 旧版插件自绘第二条状态栏，弹层顶部因此有两行，比宿主自己的弹窗「多一层」。
     */
    headerExtra(container) {
      panel.mountHeaderExtra(container);
    },
    mount(container) {
      container.append(panel.element);
      // 宿主关闭时调用（D8 契约）：只摘 DOM，视图实例留待下次 mount 复用
      return () => panel.detach();
    },
    /**
     * 应用本次入口的作用域与页签。
     *
     * 宿主在 `mount` 之后**必定**调用一次 `onOpen`（见 `pluginSurfaceHost.js`
     * 的 `openSurface`），所以这里是唯一的落点：初次打开与「surface 已打开时
     * 再次从别的入口进入」走的是同一条路径，不会出现两套逻辑分叉。
     */
    onOpen(ctx) {
      panel.applyEntryContext(ctx);
    },
  });

  // 次级内容用 modal：Prompt 全文原本塞在页签里（长滚动内容），现在归宿主居中弹层。
  api.surfaces.register('prompt-preview', {
    title: 'Prompt 预览',
    type: 'modal',
    mount(container) {
      return preview.mount(container);
    },
  });

  context.openPromptPreview = (messages) => {
    preview.setMessages(messages);
    api.surfaces.open('prompt-preview', { source: 'workbench' });
  };

  // ---------------------------------------------------------- 与主程序联动

  // 选中集合：宿主尚未提供稳定的读取接口，这里是**唯一的** Provisional 耦合点
  // （集中放在 selection.js，便于宿主提供 API 后整体替换；设计文档已登记）。
  const syncSelection = () => {
    context.selectedCardIds = readSelectedCardIds(api);
  };
  syncSelection();

  // 卡片被更新后刷新工作台（避免显示过期信息）。
  // 只在面板真的打开时刷新：视图的 refresh() 会打后端接口，面板关着时白跑。
  const offCardUpdated = api.on('card.updated', () => {
    if (api.surfaces.isOpen('workbench')) panel.showTab('workbench');
  });

  // 插件配置变更（D10）：替代轮询，设置页改完立即反映到 UI
  const offConfigChange = typeof api.onConfigChange === 'function'
    ? api.onConfigChange(() => {
      if (api.surfaces.isOpen('workbench')) panel.refreshStatus();
    })
    : null;

  // ---------------------------------------------------------- 启动自检

  const runSelfCheck = async () => {
    try {
      const data = await client.health();
      const checks = data.checks || {};
      const providerReady = checks.provider && checks.provider.ok;
      const keyReady = checks.api_key && checks.api_key.ok;
      const bridgeReady = checks.summary_bridge && checks.summary_bridge.ok;

      if (!providerReady) {
        api.host.toast('尚未配置 Provider，请到「AI 助手 → 设置」完成配置', 5000, 'settings-help-entry');
      } else if (!keyReady) {
        api.host.toast('Provider 缺少 API Key，请到「AI 助手 → 设置」填写', 5000, 'settings-help-entry');
      }
      if (!bridgeReady) {
        api.host.toast('概览写入通道不可用，结果将只保存在插件内', 5000, 'settings-help-entry');
      }

      // 自检结论反映到坞入口的状态点（`api.entries.setStatus`）
      reporter.setStatus(!providerReady || !keyReady || !bridgeReady ? 'warn' : 'ok');
    } catch (error) {
      reporter.setStatus('error');
      api.host.toast(`插件自检失败：${error.message}`, 5000, 'close');
    }
  };

  await runSelfCheck();

  api.log?.('info', 'stm-ai-assistant 前端已激活');

  // ---------------------------------------------------------- 清理（D9.1）

  // 宿主在禁用 / 卸载时调用本函数；`deactivate` 保留为兜底（旧宿主）。
  return () => {
    if (typeof offCardUpdated === 'function') offCardUpdated();
    if (typeof offConfigChange === 'function') offConfigChange();
    reporter.dispose();
    api.surfaces.close('prompt-preview');
    api.surfaces.close('workbench');
    panel.destroy();
  };
}

export function deactivate(api) {
  api.log?.('info', 'stm-ai-assistant 前端已卸载');
}
