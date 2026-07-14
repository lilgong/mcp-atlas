# 方案 C：本地维护私有 NPM 包并打包进镜像

适合需要对 MCP Server 的 npm 包进行较多修改、并长期维护的场景。

---

## 目录结构

在 `services/agent-environment/` 下新建 `local_packages/` 目录，存放修改后的包：

```
services/agent-environment/
├── local_packages/
│   └── your-package/
│       ├── package.json
│       └── index.js        ← 在这里做修改
├── dev_scripts/
│   └── install_mcp_packages.sh
└── Dockerfile
```

---

## 操作步骤

### 第一步：获取原始包内容

从已有容器中提取原始包文件：

```bash
# 启动一个临时容器
docker run --rm -d --name tmp-container agent-environment:latest sleep 3600

# 将目标包复制到本地
docker cp tmp-container:/usr/lib/node_modules/your-package \
    services/agent-environment/local_packages/your-package

# 停止临时容器
docker stop tmp-container
```

或者直接从 npm 下载原始包到本地：

```bash
cd services/agent-environment/local_packages
npm pack your-package@1.0.0
# 解压 tgz 后重命名目录为包名
```

---

### 第二步：修改包内容

进入本地包目录，按需修改文件：

```bash
# 例如修改入口文件
vim services/agent-environment/local_packages/your-package/index.js
```

确认 `package.json` 中的 `name` 字段与原包名一致，版本号可自定义：

```json
{
  "name": "your-package",
  "version": "1.0.0-patched",
  ...
}
```

---

### 第三步：修改安装脚本

编辑 `dev_scripts/install_mcp_packages.sh`，将原来通过包名安装的行替换为本地路径：

```bash
# 修改前
npm install -g your-package@1.0.0

# 修改后
npm install -g /local_packages/your-package
```

---

### 第四步：修改 Dockerfile

确保 `local_packages/` 在安装脚本执行**之前**已被 COPY 进镜像：

```dockerfile
# 将本地包目录复制进镜像
COPY local_packages/ /local_packages/

# 执行安装脚本（此时可以引用 /local_packages/ 下的内容）
COPY dev_scripts/install_mcp_packages.sh /
RUN /install_mcp_packages.sh && rm /install_mcp_packages.sh
```

---

### 第五步：构建镜像

```bash
make build
```

---

### 第六步：验证

进入容器确认包已正确安装并内容为修改后的版本：

```bash
make shell

# 容器内执行
cat /usr/lib/node_modules/your-package/index.js
```

---

## 注意事项

| 注意点 | 说明 |
|--------|------|
| **package.json 必须存在** | npm 本地安装依赖 `package.json` 中的 `name` 字段来注册包名 |
| **依赖项** | 若原包有 `dependencies`，本地安装时 npm 会自动拉取，确保构建环境有网络访问 |
| **.dockerignore** | 确认 `services/agent-environment/.dockerignore` 没有排除 `local_packages/` 目录 |
| **多包修改** | 可在 `local_packages/` 下存放多个子目录，统一管理所有需要修改的包 |
| **版本追踪** | 建议在 `package.json` 的 `version` 字段加后缀（如 `-patched`），便于区分官方版本与本地修改版 |
