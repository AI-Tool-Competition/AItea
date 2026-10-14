# 茶渡前端

本目录保留并复用 `tmp/deer-flow/frontend` 的实际 Next.js 16 / React 19 工程与组件。原项目来源及许可证见 `DEERFLOW_LICENSE`（如由根目录提供则参见根项目来源说明）。新增的业务实现集中在 `src/components/tea`、`src/core/tea` 和 `src/app/tea`，没有修改 DeerFlow 的 UI 原语或原聊天执行循环。

## 启动与检查

从 AItea 根目录使用 `make dev` 同时启动前后端，后端虚拟环境和依赖统一由 uv 管理。服务通过健康检查后在后台运行，使用 `make dev-stop` 停止；需要前台模式时使用 `make dev-foreground`。独立前端开发使用 `pnpm dev`，默认 Next.js 端口 3000；茶业务 API 默认代理到 `http://127.0.0.1:8000`，可通过服务端 `TEA_API_URL` 覆盖。浏览器使用同源 HTTP-only 会话 Cookie，无需在客户端设置 API 密钥。完整说明见根目录 `docs/DEVELOPMENT.md`。

```
pnpm typecheck
pnpm exec eslint src/components/tea src/core/tea src/app/tea src/app/page.tsx src/proxy.ts tests/unit/core/tea tests/unit/components/tea
pnpm exec rstest run tests/unit/components/tea/draft-safety.dom.test.tsx tests/unit/core/tea/api.test.ts
SKIP_ENV_VALIDATION=1 NEXT_TELEMETRY_DISABLED=1 pnpm build
```

## 页面与状态

`/` 跳转到 `/tea`。茶工作台包括欢迎与登录、业务文件夹及主体子对话、办理流程、任务填报核验、文稿编辑版本、按业务划分的资料库与审核、法务模板配置/导入/导出/模拟调试、来源采集以及演示账号升级。

窗口布局直接复用原 DeerFlow `SidebarProvider`、`Sidebar`、`SidebarInset`：桌面采用单条 16rem 侧栏，可折叠为 3rem 图标栏；手机通过同一侧栏组件打开抽屉，选择入口后自动收起。侧栏按出口业务分组，当前业务默认展开，组内列出节点对话和一个资料库入口。业务资料页固定到所选业务，切换业务时取消旧资料请求并清空旧检索结果，上传默认归属当前业务；图标栏提供当前业务对话及资料库快捷入口。公共资料库、账号、管理后台及来源管理从底部账号菜单进入。业务页以节点分析、对话和输入框为主，右上角入口按需打开任务、流程、文稿和档案面板。面板支持调宽和收起，重新打开时保留宽度和未保存编辑，不在对话上方重复展示任务清单。

节点分析直接显示在主对话开头：进入已解锁节点自动执行六步，过程采用轻量时间线，点击步骤标题展开本步发现；原文依据复用资料卡片，保留版本、定位与来源入口，技术记录单独按需展开。未手动操作的过程在运行时展开、完成后收起；手动选择及本步发现的展开状态在同一节点内保留。重新分析完成后采用已保存的步骤状态，切换报告或收到新节点报告时清除过期的临时进度。

结论以领域短标题和正文分项呈现，完整问题按需展开。待补信息先显示清单，打开后逐项填写；原文依据、报告编辑、已记录事实及更多操作通过工具栏打开，同一时间显示一个操作面板。收起和切换面板保留未保存输入，资料修订或待补清单变化不自动关闭当前编辑；冲突与保留草稿的提示保持可见。报告修订记录在报告编辑中查看。账号、业务、节点隔离请求和草稿；同一节点重访读取有效记录，资料变化更新原报告并保留人工段落。

`core/tea/node-analysis.ts` 按账号、业务、节点和资料修订号共享尚未完成的请求。快速切换后 500ms 内重返同一节点可重新订阅已有进度；离开超过该窗口取消旧连接，停止和退出立即取消。仅 `analysis_in_progress`、`analysis_busy` 触发自动等待，以 500ms 起步、最长 5s 间隔退避，等待最多 5 分钟，空闲后由服务端返回有效记录或执行同一资料版本的分析。等待保持进度提示和停止入口；权限、资料版本冲突及连接失败直接保留错误与重试入口，不自动重试失败的分析。

复用原 DeerFlow `Conversation`、`MessageResponse`、`ResizablePanelGroup`、`Button`、`Input`、`Dialog`、`Sheet` 等组件。节点与对话中的分析记录通过茶业务 `AnalysisProcess` 适配器复用保留的 `ai-elements/chain-of-thought.tsx` 中的 `ChainOfThought`、`ChainOfThoughtHeader`、`ChainOfThoughtContent` 与 `ChainOfThoughtStep`（来源：`tmp/deer-flow/frontend/src/components/ai-elements/chain-of-thought.tsx`，许可：`DEERFLOW_LICENSE`），不从 `tmp/` 引入运行依赖。过程运行时展开，结束后收起，用户主动收起后不会被后续进度重新打开；执行失败、停止、沿用结果及无需执行分别显示。默认优先展示结论和后续办理，事实采用单项折叠输入；详细依据、原始执行记录、报告编辑与修订历史仍可按需展开，未保存输入保持在原有草稿中。配色、字体和控件来自未修改的 `src/styles/globals.css` 与 UI 原语；`.tea-theme` 仅补充业务布局、状态色和阅读尺寸，跟随原版浅色/深色主题，支持键盘焦点及减少动态效果设置。欢迎页、业务页与管理后台采用简洁商务排版。后台位于 `/tea/admin`，流程以多级列表编辑，节点分析使用工作流画布，编辑栏浮动覆盖在右侧；共用主题颜色、字体与控件，顶部仅保留配置选择及操作。

服务端是已保存业务状态的唯一来源。任务、文稿和配置提交遵守服务端版本检查；HTTP 409 保留本地编辑。未保存的任务、文稿、对话和模板草稿在当前页面生命周期内按业务/文稿/账号模板存于内存，以便在界面内部切换。刷新浏览器仍应先保存；文稿未保存时有离页提示。退出登录清除本地草稿。对话发送 ID 在失败重试时复用，切换节点的过期响应不覆盖当前对话。消息和资料列表提供加载更早/更多入口。

原 `/workspace`、认证、演示及 artifact 页面经 `src/proxy.ts` 导向 `/tea`。原 `/api/*` 除 `/api/tea/*` 外统一返回 404，防止保留的源工程暴露未接入的后端接口。

## 能力边界

前端根据 `/bootstrap` 返回的能力显示“演示助手”或“AI 助手”。演示模式不会宣称调用了大模型。任务、模式、主体及执行步骤来自配置；占位符为 `{{field_key}}`。上传的业务材料以业务材料分类展示，法规采集资料需要审核后进入检索。资料页面允许维护分类、法域、语言、公布与生效日期，明确勾选审核才以已审核状态保存。

API 定义见根目录 `docs/API_CONTRACT.md`。新业务固定绑定发布的模板版本，模板发布不会更改已有业务。
