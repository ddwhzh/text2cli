# Harness system: 6 routes and composable backends

当前实现 harness system, 主流上基本可以分成 6 条路线。真正常见的不是"只有 VM 一种", 而是从"纯虚拟资源抽象"到"远程 microVM 托管"的一整条谱系。

这份文档的目标是把这些路线拆成工程上可复用的组件与决策维度, 便于你把它落到 text2cli 的"执行池 + 调度(filter/score/bind)"抽象里。

## 1) Harness 体系常见的组件拆分

业界实践里, harness 往往不是单一实现, 而是组件化组合(同一个 Agent 可以在不同任务阶段使用不同 backend)。

### 1.1 Virtual filesystem backend: 资源抽象层

作用不是"给你一台机器", 而是"给你一套受控资源接口"。

典型能力:
- `ls/read/write/edit/glob/grep` 等文件语义 API, 但底层不必是 POSIX。
- 可以把数据库/对象存储/制品库包装成"像文件一样可读写"的资源面。

价值:
- 强约束、强审计、强语义一致性。
- 适合把企业数据系统统一成一套文件语义(ACL/审计/版本/Schema 校验)。

代价:
- 通用性弱, 不适合直接跑现成 CLI/编译器/测试套件。

### 1.2 Sandbox backend: 受限执行环境

作用是解决"代码在哪跑"。它提供隔离边界与资源限制, 并通常向上暴露 `execute` 能力。

隔离手段常见从弱到强:
- OS 级限制(平台原语, 轻量, 但边界正确性更依赖策略细节)
- 容器(OCI runtime, 镜像化, 兼容工具链)
- microVM(更硬的隔离边界, 更适合无人值守/不可信代码)
- 远程托管 sandbox(把执行面做成服务, 更适合多租户与弹性)

### 1.3 Execute tool: 命令执行能力

execute tool 通常是 sandbox backend 对上暴露的"可执行接口", 用于运行 shell/python、装依赖、执行脚本等。

关键设计点:
- 不是所有 harness 都必须有 execute tool。
- 只有当你给了"执行环境"时, execute 才有意义。
- 纯虚拟资源层可以只有 read/write/list, 没有 bash/python/node。

### 1.4 Memory: 持久状态层

工程上建议把 memory 与 workspace/runtime 文件严格区分:
- sandbox/workspace 可能是临时的。
- memory 往往要跨 session 保留。

否则生命周期、权限模型、成本模型会打架。

### 1.5 Subagents: 任务编排层

subagent 是编排层能力, 用于 context quarantine 与专业化分工。它不是一种 sandbox 类型:
- 主 agent 把研究、编码、测试、汇总分给不同角色。
- 不同角色可以绑定不同的 harness/backend/policy。

因此一个完整 harness 体系至少三层:
- 资源/执行层: VFS backend、sandbox backend、execute tool
- 状态层: memory、workspace(含审计/评测状态)
- 编排层: subagents、policy、routing、approvals

## 2) 6 条主流路线, 真正区别在哪里

建议从 4 根轴来评价(前三根是你原文已有的, 第四根是补充, 对工程落地很关键):
- 资源抽象程度: "像文件/对象" vs "像完整机器"
- 隔离强度: API 约束 < OS 级限制 < 容器 < microVM
- 部署形态: 本机内嵌 < 本机容器/虚机 < 远程托管
- 同步模型: 直挂载(mount) < 单次快照(sync) < 内容寻址增量同步(CAS/MVCC 友好)

## 3) 六条路线概览

### 路线 1: 纯虚拟文件系统 / 虚拟资源层 harness

这是最"窄接口、强控制"的方案: 只暴露受控的文件/对象操作, 不给任意 shell。

优点:
- 可控、可审计、权限粒度细, 语义统一。

缺点:
- 兼容性弱, 跑不了大量现成 CLI/编译器/测试框架。

适合:
- "底层 MySQL 模拟 FS"、知识库加工、报表生成、结构化数据工作流。

### 路线 2: OS 级受限执行 harness

在近本机执行, 依赖平台原生机制做限制(轻、快、冷启动低), 但隔离硬度通常弱于容器/microVM, 并对策略细节敏感。

适合:
- 本地 coding assistant、IDE 内嵌 agent、低延迟短命令执行。

### 路线 3: 容器型 harness

给 agent 一个容器化 runtime(镜像 + 资源限制 + 网络策略 + 卷挂载), 兼容几乎所有工具链, 复现性好, 落地快。

缺点主要来自配置质量:
- 卷挂载范围、网络、capabilities、daemon 暴露若不收紧, 安全边界会被削弱。

### 路线 4: microVM / 轻量虚拟机型 harness

在"容器够快"和"虚拟机够安全"之间取平衡:
- 更硬的隔离边界, 更适合不可信代码、高自治/无人值守 agent。
- 代价是基础设施复杂度与成本更高。

实现形态常见有两类:
- microVM 作为 sandbox runtime(例如 Firecracker 系)
- container runtime 运行在 microVM 内(例如 Kata Containers 这类 VM-backed runtime)

### 路线 5: 远程托管 sandbox / sandbox-as-a-service

把 harness 从"本机组件"变成"云上执行底座", 通过 SDK/API 申请隔离环境:
- 弹性、多租户治理与并行扩展更自然。
- 需要额外评估延迟、数据驻留、成本与私有化复杂度。

### 路线 6: 混合式 harness

统一 harness API, 底层按路径/能力路由到不同 backend:
- 80% 的数据交互走受控虚拟资源层。
- 20% 真需要跑工具链时才进 sandbox。

这是更贴近生产系统的折中, 但设计最复杂(路由、身份、审计、同步一致性、缓存一致性)。

## 4) 参考架构图(组件化, 非二选一)

```mermaid
graph TD
    U[User/App/IDE/Workflow] -> A[Main Agent]
    A -> SA[Subagents/Planner/Specialists]
    A -> M[Memory/Long-term State]
    A -> H[Unified Harness API]

    subgraph Control_Plane[Harness Control Plane]
        H -> R[Policy Router/Path Routing]
        H -> P[Permissions/Approvals/Network Policy]
        H -> O[Observability/Audit/Quota/Billing]
    end

    subgraph Resource_Plane[Resource Plane]
        R -> V1[Virtual FS Backend]
        V1 -> DB[MySQL/Postgres]
        V1 -> OBJ[S3/OSS/Blob Store]
        V1 -> DOC[Docs/Artifacts/KV]
    end

    subgraph Execution_Plane[Execution Plane]
        R -> S1[Sandbox Backend]
        S1 -> E[Execute Tool]
        S1 -> OS[OS Restricted Runtime]
        S1 -> CT[Container Runtime]
        S1 -> MV[microVM Runtime]
        S1 -> RS[Remote Sandbox Service]
    end

    subgraph Infra_Plane[Infra Orchestration Plane]
        CT -> K8S[Kubernetes/Container Hosts]
        MV -> HV[Hypervisor/VM Hosts]
        RS -> CP[Cloud Control Plane/API Gateway]
        DB -> ST[Persistent Storage]
        OBJ -> ST
    end
```

## 5) 三个典型落地方案(帮助你对齐取舍)

### 方案 A: 纯虚拟 FS

- `/data/contracts/*` 映射到 MySQL/Postgres
- `/reports/*` 映射到对象存储
- agent 只能读写这些"虚拟文件"
- 没有 shell, 没有 pip install, 没有 pytest

harness 更像"可编程数据总线"。安全极强, 但 agent 不会像程序员那样跑工具链。

### 方案 B: 容器/microVM 型 coding harness

- `/workspace` 挂你的 Git repo
- agent 可以 `npm test/pytest/go build`
- 可以装依赖、运行脚本

重点是"真实开发环境", 而不是"语义化资源"。

### 方案 C: 混合式

- `/corp-data/*` -> virtual FS(审计严格、只暴露业务对象)
- `/workspace/*` -> sandbox(容器或 microVM)
- 只有进入 `/workspace` 的任务才获得 execute tool
- research/data subagent 只碰 virtual FS; coding subagent 才进 sandbox

这是"统一 harness API + 后端路由"的终局雏形: 把企业数据安全边界与通用代码执行边界分开管。

## 6) 一句话比较 6 条路线

- 虚拟 FS: 最像"数据库/对象存储的代理层"
- OS 级受限执行: 最像"带手铐的本机进程"
- 容器: 最像"给 agent 一个标准开发盒子"
- microVM: 最像"更安全的轻量电脑"
- 远程托管 sandbox: 最像"云上的隔离执行集群"
- 混合式: 最像"统一入口 + 多后端调度系统"

## 7) 对 text2cli 的改进建议(基于你当前方向)

你当前的核心问题不是"要不要 VM", 而是:
"哪类能力必须抽象, 哪类能力必须真实执行, 分别放在哪个边界里"。

建议把落地语言统一到你已采用的范式:
- 控制面: 决策"这次任务要不要执行、走哪个后端/执行池"
- 状态面: WorkspaceDB/审计/评测/会话/任务状态外部化
- 执行面: host/docker/microVM/remote 的快路径执行器
- 隔离面: 最小权限 + 最小暴露 + blast radius 控制
- 调度面: filter + score + bind, 维度至少包含安全、延迟、成本, 并逐步扩展到数据驻留、权限域、命中率等

## 参考链接(用于校验原文引用)

- LangChain Deep Agents backends: https://docs.langchain.com/oss/javascript/deepagents/backends
- OpenAI Codex sandboxing: https://developers.openai.com/codex/concepts/sandboxing/
- Docker Sandboxes: https://docs.docker.com/ai/sandboxes
- Docker alternative runtimes (Kata/gVisor 等): https://docs.docker.com/engine/daemon/alternative-runtimes/
当前实现 harness system，主流上基本可以分成 6 条路线。真正常见的不是“只有 VM 一种”，而是从“纯虚拟资源抽象”到“远程 microVM 托管”的一整条谱系。LangChain 的文档就已经把 harness 能力拆成了虚拟文件系统 backend、sandbox backend、execute tool、memory、subagents 等模块，说明当下业界不是单一实现，而是组件化组合。 ￼

第一类：纯虚拟文件系统 / 虚拟资源层 harness。
这类方案不给 agent 一个“完整机器”，而是只暴露受控的文件/目录/对象操作接口。LangChain Deep Agents 的 backend 设计就支持可插拔虚拟文件系统，文档直接举了用 S3 或 Postgres 实现自定义 virtual filesystem 的例子；非 sandbox backend 只暴露文件操作，不暴露任意 shell 执行。这个方向和你前面说的“MySQL 模拟 FS”最接近。优点是可控、可审计、语义统一；缺点是通用性弱，不适合运行现成 CLI、编译器和测试套件。 ￼

第二类：OS 级受限执行 harness。
这类方案让 agent 直接在本机或近本机环境中执行命令，但靠操作系统原生机制约束权限，而不是给它一个完整 VM。OpenAI 的 Codex 文档把 sandboxing 定义为让 agent 能自主运行、但默认不给它无限制访问；其公开说明提到本地命令运行在受约束环境里，环境决定能改哪些文件、能不能联网。公开的 Codex 相关文档还说明其平台相关 sandbox 机制在 macOS 上用 Seatbelt，在 Linux 上依赖 Landlock 和 seccomp。优点是轻、快、用户本地体验好；缺点是隔离强度通常不如容器或 microVM，对宿主环境能力边界更敏感。 ￼

第三类：容器型 harness。
这是现在最普遍的一类：给 agent 一个 Docker 容器或容器化 runtime，在里面跑命令、改文件、装依赖。OpenHands 的 runtime 文档明确写的是 client-server 架构，底层用 Docker 容器提供 sandboxed environment；它强调这种环境能带来隔离、一致性、资源控制和可复现性。优点是生态成熟、镜像定制方便、兼容开发工具链；缺点是比纯 API/虚拟 FS 更重，而且如果容器权限、卷挂载和网络策略没收紧，安全边界会比较依赖配置质量。 ￼

第四类：microVM / 轻量虚拟机型 harness。
这一类是近一年特别明显的趋势：在“容器够快”和“虚拟机够安全”之间取平衡。Docker Sandboxes 的官方文档和产品页都明确写了它是给 AI coding agents 的 isolated environments，并强调 microVM-based isolation，支持 Claude Code、Gemini、Codex、Kiro 等 agent；它的卖点就是 agent 可以更放心地无人值守运行，甚至能在沙箱内继续跑 Docker，而不接触宿主机 daemon。优点是安全边界更硬，适合高权限 agent；缺点是平台和基础设施更复杂，成本通常高于普通容器。 ￼

第五类：远程托管 sandbox / sandbox-as-a-service。
这类方案把 harness 从“本机组件”变成“云上执行底座”，通过 SDK/API 让你的 agent 动态申请一台隔离环境。E2B 文档把自己定位成 secure isolated sandboxes in the cloud，用 Python/JS SDK 启动和控制；Daytona 文档则把自己定义为 open-source, secure and elastic infrastructure for running AI-generated code，提供可编程管理的 sandboxes；阿里的 OpenSandbox 也主打 unified sandbox APIs、Docker/Kubernetes runtimes，面向 coding agents、GUI agents、agent evaluation、RL training 等场景。优点是弹性、集中治理、多租户更方便；缺点是依赖远程平台，延迟、数据驻留、成本和私有化复杂度都要单独评估。 ￼

第六类：混合式 harness。
这是我认为当前最像“终局形态”的方案：上层是统一 harness API，底层可按路径或能力路由到不同 backend。LangChain 的 backend 文档就支持 composite/custom routes：一部分路径走虚拟文件系统，一部分路径走 sandbox；并且 sandbox backend 会额外暴露 execute tool。也就是说，一个 agent 可以同时拥有“高可控的数据语义层”和“必要时的命令执行环境”。这类设计最适合你前面那种“底层 MySQL 模拟 FS，但又希望某些任务能像真实机器一样运行”的需求。 ￼



1. Virtual filesystem backend：资源抽象层
它的作用不是“给你一台机器”，而是“给你一套受控资源接口”。
LangChain 的 backend 文档明确支持可插拔 filesystem backend，还举了用 S3 或 Postgres 实现自定义 virtual filesystem 的例子；这意味着 agent 看到的是统一的文件语义，而底层其实可以根本不是 POSIX 文件系统。这个层最适合把数据库、对象存储、版本化文档包装成“像文件一样可读写”的资源面。 ￼

你可以把它理解成：
	•	agent 以为自己在 read_file("/workspace/spec.md")
	•	实际上后端可能是在读 MySQL/Postgres/S3 里的对象
	•	这样你拿到了强约束、强审计、强语义一致性，但放弃了“任意 shell 命令都能跑”的通用性。 ￼

2. Sandbox backend：受限执行环境
LangChain 对 sandbox 的定义很直接：agent 可能会生成代码、运行 shell、读写文件，所以需要一个与宿主隔离的边界；在 Deep Agents 里，sandbox 也是 backend，只是它提供的是“可执行环境”。 ￼

这意味着：
	•	virtual FS backend 解决“资源怎么暴露”；
	•	sandbox backend 解决“代码在哪跑”。
二者是并列、可组合关系，不是二选一。 ￼

3. Execute tool：命令执行能力
LangChain 文档写得很明确：当你使用 sandbox backend 时，harness 会暴露一个 execute tool，让 agent 在隔离环境里运行 shell 命令、装依赖、执行脚本。也就是说，execute tool 不是独立凭空存在，它通常是 sandbox backend 向上暴露的“可执行接口”。 ￼

所以一个很重要的设计点是：
	•	不是所有 harness 都必须有 execute tool；
	•	只有当你给了“执行环境”时，execute 才有意义；
	•	纯虚拟资源层 harness 可以只有 read/write/list，没有 bash/python/node。 ￼

4. Memory：持久状态层
在 Deep Agents 里，memory 是独立配置项，不是 sandbox 的副产品。也就是说，“长期记忆”与“运行时文件”是两类不同状态：前者服务于跨任务/跨轮次的知识保留，后者服务于当前 workspace 的中间产物。 ￼

工程上这很关键，因为：
	•	sandbox 可能是临时的；
	•	memory 往往要跨 session 保留；
	•	如果把二者混在一起，生命周期、权限模型、成本模型都会打架。 ￼

5. Subagents：任务编排层
LangChain 文档把 subagents 定义成“委派工作”的机制，用于 context quarantine 和专业化分工。换句话说，subagent 不是另一个 sandbox 类型，而是工作流/编排层能力：主 agent 把研究、编码、测试、汇总分给不同角色，各自使用同一 harness 或不同 harness。 ￼

所以完整 harness 体系其实至少有三层：
	•	资源/执行层：VFS backend、sandbox backend、execute tool
	•	状态层：memory、workspace
	•	编排层：subagents、policy、routing、approvals。 ￼

⸻

2) 6 条主流路线，真正区别在哪里

我建议把这 6 路线放到三根轴上看：
	•	资源抽象程度：是“像文件/对象”还是“像完整机器”
	•	隔离强度：API 约束 < OS 级限制 < 容器 < microVM
	•	部署形态：本机内嵌 < 本机容器/虚机 < 远程托管。

路线 1：纯虚拟文件系统 / 虚拟资源层 harness
这是最“窄接口、强控制”的方案。LangChain 的 backend 允许你实现自定义虚拟文件系统，官方直接点名 S3 / Postgres 这类后端；如果不用 sandbox backend，就只暴露文件操作，不给任意 shell。 ￼

像什么：
“让 agent 操作一个受控 API 空间”，而不是一台电脑。

优点：
可控、可审计、权限粒度细、容易把企业数据系统统一成一套文件语义。 ￼

缺点：
跑不了大量现成 CLI/编译器/测试框架，通用性弱。

适合：
你说的“底层 MySQL 模拟 FS”、知识库加工、报表生成、结构化数据工作流。

路线 2：OS 级受限执行 harness
OpenAI 对 Codex sandboxing 的定义是：让 agent 能自主行动，但默认不拿到对主机的无限制访问；最新文档说明本地命令运行在受约束环境中，默认网络关闭，能改哪些文件、能否联网都由环境与策略决定。公开部署安全说明还写到：macOS 使用 Seatbelt，Linux 使用 Landlock + seccomp。 ￼

像什么：
“还是在近本机执行，但被 OS 原生机制圈了一个安全边界”。

优点：
轻、快、冷启动低、本地开发体验好。 ￼

缺点：
隔离硬度通常不如容器/microVM；边界正确性更依赖平台实现与策略细节。

适合：
本地 coding assistant、IDE 内嵌 agent、低延迟短命令执行。

路线 3：容器型 harness
OpenHands 的 Runtime Architecture 文档明确写了：Docker Runtime 是核心执行组件，负责在 sandboxed environment 中安全运行任意代码；这是典型的 client-server/runtime 设计。 ￼

像什么：
“给 agent 一只 Docker 容器，当作可编程实验室”。

优点：
生态成熟，镜像可定制，兼容几乎所有开发工具链，CI/CD 和平台侧都容易接。 ￼

缺点：
比纯 API / 虚拟 FS 重；安全上高度依赖容器权限、卷挂载、网络与 daemon 暴露配置。

适合：
代码生成、测试、依赖安装、CI 仿真、研发平台集成。

路线 4：microVM / 轻量虚拟机型 harness
Docker 最新 Sandboxes 文档已经把定位说得很清楚：这是给 AI coding agents 的 isolated environments，并且当前架构已经从旧的容器实现迁移到 microVM-based isolation；官方架构页强调私有 Docker daemon、workspace sync，以及“可以在沙箱内继续跑 Docker，而不触碰宿主机 daemon”。 ￼

像什么：
“比容器更像一台真正隔离的小机器，但又比传统虚机更轻”。

优点：
安全边界更硬，特别适合高自治、高权限、少人工审批的 agent。Docker 官方甚至把它卖点写成：更有信心让 agent 无人值守运行。 ￼

缺点：
基础设施复杂度和成本通常高于普通容器。

适合：
无人值守 coding agent、运行不可信代码、需要 Docker-in-sandbox 的场景。

路线 5：远程托管 sandbox / Sandbox-as-a-Service
E2B 文档把自己定义为 isolated sandboxes，可通过 Python/JS SDK 创建并控制，支持运行命令、文件系统操作和代码执行；Daytona 文档把自己定义为 secure and elastic infrastructure for running AI-generated code，提供可编程 sandboxes、REST API、CLI、SDK；OpenSandbox 则强调 unified sandbox APIs 与 Docker/Kubernetes runtimes，面向 Coding Agents、GUI Agents、评测和 RL 训练。 ￼

像什么：
“把执行环境从你的系统里抽出来，变成云上的隔离计算底座”。

优点：
弹性伸缩、集中治理、多租户、并行扩展更自然。E2B 还支持 BYOC，把环境放进你自己的 VPC；Daytona 则强调 stateful sandboxes 和 API/CLI 控制面。 ￼

缺点：
有网络延迟、数据驻留、跨境与成本问题；私有化和企业集成要单独评估。

适合：
SaaS agent 平台、批量评测、云端多人协作、远程浏览器/GUI/code execution。

路线 6：混合式 harness
LangChain 的 backend 文档明确支持 composite/custom routes：不同路径可走不同 backend；同时 sandbox backend 还会向上暴露 execute tool。这个设计本质上就是“统一 harness API + 多后端路由”。 ￼

像什么：
“80% 的数据交互走受控虚拟资源层；20% 真需要跑代码时才进 sandbox”。

优点：
把“强控制的数据语义层”和“必要时的真实执行能力”组合起来，是最贴近生产系统的折中。 ￼

缺点：
设计最复杂，要处理路径路由、身份、审计、状态同步、缓存一致性。

适合：
企业级 agent 平台、需要同时接数据库/对象存储/代码执行的系统。


:::mermaid
flowchart TB
    U[User / App / IDE / Workflow] --> A[Main Agent]
    A --> SA[Subagents / Planner / Specialist Agents]
    A --> M[Memory / Long-term State]
    A --> H[Unified Harness API]

    subgraph Control_Plane[Harness Control Plane]
      H --> R[Policy Router / Path Routing]
      H --> P[Permissions / Approvals / Network Policy]
      H --> O[Observability / Audit / Quota / Billing]
    end

    subgraph Resource_Plane[Resource Plane]
      R --> V1[Virtual FS Backend]
      V1 --> DB[(MySQL / Postgres)]
      V1 --> OBJ[(S3 / OSS / Blob Store)]
      V1 --> DOC[(Docs / Artifacts / KV)]
    end

    subgraph Execution_Plane[Execution Plane]
      R --> S1[Sandbox Backend]
      S1 --> E[Execute Tool]
      S1 --> OS[OS-level Restricted Runtime\nSeatbelt / Landlock / seccomp]
      S1 --> CT[Container Runtime\nDocker / OCI]
      S1 --> MV[microVM Runtime]
      S1 --> RS[Remote Sandbox Service\nE2B / Daytona / OpenSandbox]
    end

    subgraph Infra_Plane[Infra / Orchestration Plane]
      CT --> K8S[Kubernetes / Container Hosts]
      MV --> HV[Hypervisor / VM Hosts]
      RS --> CP[Cloud Control Plane / API Gateway]
      DB --> ST[Persistent Storage]
      OBJ --> ST
    end
:::

方案 A：纯虚拟 FS
	•	/data/contracts/* 映射到 MySQL / Postgres
	•	/reports/* 映射到对象存储
	•	agent 只能读写这些“虚拟文件”
	•	没有 shell、没有 pip install、没有 pytest

这时 harness 更像“可编程数据总线”。安全极强，但 agent 不会像程序员那样跑工具链。这个模式和 LangChain 自定义 virtual filesystem 非常接近。 ￼

方案 B：容器/微虚机型 coding harness
	•	/workspace 挂你的 Git repo
	•	agent 可以 npm test、pytest、go build
	•	还能装依赖、运行脚本

这就是 OpenHands / Docker Sandboxes / Daytona / E2B 更擅长的形态：重点不是“语义化资源”，而是“真实开发环境”。 ￼

方案 C：混合式
	•	/corp-data/* → virtual FS（审计严格、只暴露业务对象）
	•	/workspace/* → sandbox（容器或 microVM）
	•	只有进入 /workspace 的任务才获得 execute tool
	•	研究类 subagent 只碰 virtual FS；编码类 subagent 才进 sandbox

这类就是“统一 harness API + 后端路由”的终局雏形。它把企业数据安全边界与通用代码执行边界分开管，通常最实用。 ￼

⸻

5) 一句话比较 6 条路线

你可以这样记：
	•	虚拟 FS：最像“数据库/对象存储的代理层”
	•	OS 级受限执行：最像“带手铐的本机进程”
	•	容器：最像“给 agent 一个标准开发盒子”
	•	microVM：最像“更安全的轻量电脑”
	•	远程托管 sandbox：最像“云上的隔离执行集群”
	•	混合式：最像“统一入口 + 多后端调度系统”。

从工程取舍上看：
	•	要极致可控、强审计：先上虚拟 FS
	•	要本地体验、低延迟：OS 级受限执行
	•	要兼容工具链、落地快：容器
	•	要高自治高隔离：microVM
	•	要弹性、多租户、云托管：remote sandbox
	•	要企业级综合能力：混合式。 ￼

⸻

6) 你这个“MySQL 模拟 FS，但又想必要时像真实机器一样运行”的设计，最推荐怎么做

我会建议你直接按混合式 harness来设计，而不是在“虚拟 FS”和“VM/容器”之间二选一：
	1.	默认路径走 virtual filesystem backend
	•	把 MySQL / Postgres / S3 映射为统一文件语义
	•	所有业务数据操作都经过这一层
	•	这里做 ACL、审计、版本、对象 schema 校验
	2.	按需切到 sandbox backend
	•	只有遇到“必须运行现成工具链”的任务才给 execute tool
	•	例如代码生成后的测试、编译、静态检查、文档转换
	3.	把 memory 从 workspace 里剥离
	•	memory 单独持久化
	•	sandbox 可以是临时的，memory 不能跟着销毁
	4.	让 subagents 分角色拿权限
	•	research/data agent：只读 virtual FS
	•	coding agent：可进 sandbox
	•	reviewer agent：读结果、做审计，不一定有执行权
	5.	执行环境优先级
	•	本地单租户开发：先容器
	•	高风险无人值守：优先 microVM
	•	多租户 SaaS：优先远程 sandbox service。 ￼

这套拆法的好处是：
你不是在问“要不要 VM”，而是在问“哪类能力必须抽象，哪类能力必须真实执行，分别放在哪个边界里”。这才是现在 harness 系统的主流设计语言。 ￼