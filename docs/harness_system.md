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