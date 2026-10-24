# 实例数据与配置管理

一个实例由一个 `TEA_DATA_DIR` 管理，默认是当前工作树的 `data/`。修改位置前先运行 `make instance-paths` 查看实际路径；该命令只显示位置，不显示配置值或密钥。目录定义集中在 `backend/app/configuration/paths.py`，后端、采集服务、RAGFlow 和运维入口共用它。

```text
data/
├── tea.sqlite3                     # 业务、账号、文档版本、解析和索引回执
├── files/                          # 内容寻址原件及历史证据
├── official-updates/ID、SG/         # 来源版本、抓取进度、请求收据
├── ragflow/                        # RAGFlow 的数据库、对象与向量索引
├── control/
│   ├── embedding-admission.sqlite3 # 跨进程配额和退避状态
│   ├── collection-service/         # 采集暂停、重试退避和监督状态
│   └── collection-bindings/        # 采集启动清单与状态目录的绑定
└── config/
    ├── application.env             # 后端私有参数、API 密钥与采集代理
    ├── ragflow/                    # RAGFlow 私有凭据、密钥与服务配置
    ├── collection/fleet.json        # 采集服务的明确命令清单
    ├── catalogs/                   # 恢复实例携带的来源及流程配置快照
    └── migrations/                 # 配置归集与迁移的操作收据

config/                             # 随 Git 管理的来源、流程和校验配置默认值
.dev/                               # 当前工作树的 PID、日志、工具和缓存
```

新 checkout 默认读取根 `config/`；恢复实例优先读取 `data/config/catalogs/`，保持原实例的来源清单、历史配置版本和工作流版本。`TEA_CONFIG_DIR` 可以显式选定配置根，热更新来源目录始终只有一个权威路径。采集清单位置由 `make instance-paths` 输出，修改该位置的 `collection-sources/id-sg-unattended.json`，不要同时维护两份活动清单。

备份记录原配置根，恢复时将自定义来源目录和模板文件的活动路径一并映射到随包恢复的配置快照。旧格式实例备份没有该字段时，按原项目的 `config/` 目录兼容处理。

`TEA_DATA_DIR`、`TEA_CONFIG_DIR`、`TEA_ENV_FILE` 是启动环境参数，相对路径统一基于当前 checkout，不能写在 `application.env` 内让实例自行改绑。其余后端参数集中在 `application.env`；进程环境优先于文件，空字符串也算显式覆盖。值按字面读取，不展开 `${...}`。环境参数修改后重启所属服务；来源清单继续按原机制热更新。

旧根 `.env` 在尚未归集时仍可读取；新旧两份私有环境文件同时存在时拒绝隐式选择，需要先整理或明确 `TEA_ENV_FILE`。普通开发工作树使用自己的数据和配置，不继承主工作树密钥。

## 已有实例归集

先停止本实例的开发服务、官方采集服务和 RAGFlow，独立启动的解析或向量任务也需要停止：

```bash
make dev-stop
systemctl --user stop aitea-collection.service
make ragflow-stop
make instance-adopt
make collection-prepare
make instance-paths
```

归集仅移动已知的旧私有配置、采集监督状态和配额状态，不搬数据库、不改原件路径、不重建 RAGFlow 密钥。发生目标冲突时拒绝覆盖；完成后再次运行不会重复迁移。旧采集 unit 保存为迁移证据，新 unit 在 `.dev/collection-service/aitea-collection.service` 生成。若使用用户级 systemd，将生成的 unit 安装到原服务位置并执行 `systemctl --user daemon-reload` 后再启动；命令不会擅自修改系统配置。

新实例可以创建 `data/config/`，将根 `.env.example` 复制为 `data/config/application.env`。私有目录使用 `700`、文件使用 `600`，不提交 Git。`make ragflow-prepare` 生成 RAGFlow 私有配置；MinerU 和 embedding 继续走 API。

## 成套备份与迁移

完整实例备份包含私有密钥，必须存放在受控目录。普通 `backup_data.py` 的在线 SQLite 备份仍可单独使用；含 RAGFlow MySQL、对象和向量文件的实例备份必须先停服。工具会检查已登记的进程、采集锁和本账号打开的实例文件；管理员启动或未登记的其他写入者仍需由运维方确认已经停止。备份过程中不要重新启动服务。

```bash
mkdir -p backups restores
chmod 700 backups restores
make instance-backup BACKUP=backups/full-instance
make instance-verify BACKUP=backups/full-instance

# 在目标仓库中运行；父目录需要存在，目标必须是新目录。
make instance-restore BACKUP=/受控备份目录/full-instance TARGET=restores/imported
TEA_DATA_DIR=restores/imported make instance-paths
TEA_DATA_DIR=restores/imported make collection-prepare
TEA_DATA_DIR=restores/imported make ragflow-prepare
```

迁移到另一台机器时一起保存代码的提交号和锁文件，在目标机器安装相应 uv、Node 与 RAGFlow 用户运行环境。RAGFlow 镜像和用户目录下的 Apptainer 是可重建工具，不属于数据包；默认不复制机器上的依赖环境或进程号。当前官方采集器仍要求实例目录位于所属项目内。`collection-prepare` 将活动路径写入生成的 systemd unit；独立命令与其他守护方式使用非默认 `TEA_DATA_DIR` 时仍须在启动环境保留该值。

备份复用 SQLite 快照、引用校验和逐文件 SHA-256 校验；来源和流程配置也纳入同一校验清单。已停止 MySQL 的 `mysql.sock` 是进程通信文件，明确排除；其他符号链接仍拒绝备份。备份目标不能放进源数据根，旧备份不会被覆盖。

恢复会重定位数据库中的活动存储路径和采集状态的根目录，保留不可变请求收据、付费解析检查点、版本、审核、账号、向量和重试计数。旧采集启动清单留作审计，活动清单重定位到目标机器，保留任务启停配置、暂停标记、失败次数和退避时间；仅清除旧机器的进程身份。共享向量额度库保留容量、统计和供应商冷却时间，释放旧机器的请求租约。`collection-prepare` 据此生成新的服务 unit。RAGFlow 凭据保留，生成配置由 `ragflow-prepare` 和启动命令刷新。恢复不自动启动服务、不自动重试失败任务、不调用外部模型。

确认新实例资料及权限后，再按原启动方式恢复 RAGFlow、采集器和后端。旧数据根与备份继续保留，完成切换验收前不要删除。细粒度备份语义见 [DATA_BACKUP.md](DATA_BACKUP.md)。
