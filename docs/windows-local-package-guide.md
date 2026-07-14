# Windows 环境下本地修改 NPM 包并打包进镜像

## 背景

在 Windows 系统上，无法直接使用 `/mnt` 等 Linux 路径。但通过 `docker cp` 和 Dockerfile 的 `COPY` 指令，可以在 Windows 上维护包文件，由 Docker 构建时自动映射到容器内的 Linux 路径。

---

## 路径映射关系

```
Windows 本地路径                          容器内路径
services/agent-environment/
  local_packages/your-package/   →   /local_packages/your-package/
    index.js (已修改)                   index.js (已修改)
```

---

## 操作步骤

### 第一步：从运行中的容器提取包文件到 Windows

```bash
# 查看容器 ID
docker ps

# 将包从容器复制到 Windows 本地目录
docker cp <容器ID>:/usr/lib/node_modules/your-package \
    services/agent-environment/local_packages/your-package
```

> `docker cp` 在 Windows 上完全可用，目标路径写 Windows 相对路径即可。

---

### 第二步：在 Windows 上修改包文件

用任意编辑器修改本地文件：

```
services\agent-environment\local_packages\your-package\index.js
```

---

### 第三步：修改安装脚本，改为本地路径安装

编辑 `dev_scripts/install_mcp_packages.sh`：

```bash
# 原来（从 npm registry 下载）
npm install -g your-package@1.0.0

# 改为（使用容器内的本地路径）
npm install -g /local_packages/your-package
```

---

### 第四步：修改 Dockerfile，COPY 本地包进镜像

在安装脚本执行**之前**添加 COPY 指令：

```dockerfile
# 将 Windows 本地目录映射到容器内的 Linux 路径
COPY local_packages/ /local_packages/

COPY dev_scripts/install_mcp_packages.sh /
RUN /install_mcp_packages.sh && rm /install_mcp_packages.sh
```

---

### 第五步：重新构建镜像

```bash
make build
```

---

## 与原有工作流的对比

| | 原有工作流 | 本方案 |
|---|---|---|
| **修改位置** | 进入容器内修改 | Windows 本地修改 |
| **持久化** | 容器销毁后丢失 | 保留在 Windows 文件系统 |
| **版本追踪** | 无法用 git 追踪 | 可以用 git 追踪修改历史 |
| **重复工作** | 每次重建后需重新修改 | 一次修改，每次构建自动生效 |

---

## 注意事项

- 确认 `services/agent-environment/.dockerignore` 没有排除 `local_packages/` 目录
- `package.json` 中的 `name` 字段必须与原包名保持一致，否则 `npx` 调用时找不到包
- 如果原包有外部依赖（`dependencies`），构建时 npm 会自动拉取，需确保构建环境有网络访问
