# TEMU Image Factory

跨境电商服装商品图片自动化生产系统。第一阶段支持 TEMU，可扩展至 Amazon、Shopee、TikTok Shop 与独立站。

## 核心特性

- **CLI + Web UI 双入口**：同一套 Core 逻辑，浏览器和命令行均可调用
- **Provider 抽象**：支持 API易（APIYI）Gemini / OpenAI 网关，可扩展 ComfyUI、Fashn 等
- **成本保护**：默认禁止真实 API 调用，预算超限自动拒绝
- **候选图机制**：AI 生成多候选，人工 Accept 后成为正式图
- **Docker 部署**：单容器、非 root 运行，适合飞牛 NAS 长期运行
- **单用户认证**：Argon2id 密码哈希 + 服务器签名的客户端 Session Cookie（signed client-side session cookie，非服务器端 Session），无复杂用户系统

## 快速开始

### 环境要求

- Python 3.10+
- pip

### 安装

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -e ".[dev]"
```

### 环境变量配置

复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
```

**绝对不要**把 `.env` 提交到 Git。

关键变量：

```env
APP_USERNAME=admin
APP_PASSWORD_HASH=<argon2id-hash>
SESSION_SECRET=<32+字符随机串>
COOKIE_SECURE=false

APIYI_API_KEY=sk-xxx
APIYI_GEMINI_BASE_URL=https://api.apiyi.com
APIYI_OPENAI_BASE_URL=https://api.apiyi.com/v1
```

### 生成密码 Hash

```bash
python -m src auth hash-password
```

按提示输入密码，输出 Argon2id Hash 后粘贴到 `.env` 的 `APP_PASSWORD_HASH`。

## CLI 使用

### Validate

检查 SKU 数据完整性：

```bash
python -m src validate F116-Black
```

### Build（默认 Dry Run）

```bash
python -m src build F116-Black --platform temu
```

### Build（真实 API）

```bash
python -m src build F116-Black --platform temu --live
```

### 单任务 Generate

```bash
python -m src generate F116-Black model_front --platform temu
```

### 单任务 Generate（真实 API，指定模型，1 张候选）

三条 Smoke Test（同 SKU、同任务、同 Prompt，便于横向对比）：

```bash
# Smoke Test A: Nano Banana 2 Lite (1K)
python -m src generate F116-Black 02_model_front --platform temu --model nano_banana_lite --count 1 --live

# Smoke Test B: GPT-Image-2-VIP (3:4, 1K -> 960x1280)
python -m src generate F116-Black 02_model_front --platform temu --model gpt_image_2_vip --resolution 1K --count 1 --live

# Smoke Test C: Nano Banana 2 (1K)
python -m src generate F116-Black 02_model_front --platform temu --model nano_banana_2 --resolution 1K --count 1 --live
```

### 接受候选图

```bash
python -m src accept F116-Black model_front 2 --platform temu
```

## Web UI 使用

### 启动

```bash
uvicorn src.web.app:app --host 0.0.0.0 --port 8000
```

访问 `http://localhost:8000`，使用 `APP_USERNAME` / `APP_PASSWORD_HASH` 对应的密码登录。

### Web 功能

- SKU 列表与状态查看
- SKU 详情（Validate 状态、任务列表、成本）
- Build Dry Run
- 生成候选图（需确认）
- 查看、Accept、Reject 候选图（Reject 只改 manifest 状态，文件保留）
- 新建 SKU 并上传原图

## Docker 部署

### 构建

```bash
docker compose build
```

### 持久化目录与默认配置初始化

容器内路径统一为 `/app/input`、`/app/output`、`/app/cache`、`/app/logs`、`/app/config`、`/app/templates`、`/app/data`，全部映射到宿主机 `./data/` 下。

镜像内自带只读默认配置 `/app/config-defaults` 与 `/app/templates-defaults`。**首次启动**时 entrypoint 会把缺失的 `models.yaml`、`routing.yaml`、`budget.yaml`、`platforms/temu.yaml` 及全部 Prompt 模板复制到 `/app/config` / `/app/templates`；**已存在的文件绝不覆盖**，因此更新镜像不会冲掉你修改过的配置和 Prompt。

### 启动

```bash
docker compose up -d
```

### 查看状态

```bash
docker compose ps
```

### 健康检查

```bash
curl http://localhost:8000/health
```

### 查看日志

```bash
docker compose logs -f
```

## 飞牛 NAS 部署

### 目录结构

在 NAS 上创建：

```bash
mkdir -p /vol1/docker/temu-image-factory/data/{input,output,cache,logs,config,templates,app}
```

### 推荐启动方式

在 `/vol1/docker/temu-image-factory/docker-compose.yml`：

```yaml
services:
  temu-image-factory:
    image: temu-image-factory:latest
    build: .
    restart: unless-stopped
    ports:
      - "8000:8000"
    env_file:
      - .env
    environment:
      - PUID=1000
      - PGID=1000
      - TRUST_PROXY_HEADERS=false
    volumes:
      - ./data/input:/app/input
      - ./data/output:/app/output
      - ./data/cache:/app/cache
      - ./data/logs:/app/logs
      - ./data/config:/app/config
      - ./data/templates:/app/templates
      - ./data/app:/app/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
```

`docker compose down` 后再 `up -d` 不会丢失 SKU、原图、生成结果、成本账本、配置、Prompt 模板，以及未来的 SQLite 任务数据库（`/app/data/app.db`）。

### PUID / PGID

NAS 挂载目录出现 `Permission denied` 时：

1. 确认 NAS 上目录归属：`ls -ln /vol1/docker/temu-image-factory/data`
2. 在 `.env` 中设置：
   ```env
   PUID=1000
   PGID=1000
   ```
3. 容器启动时会自动调整 `/app/data` 等目录属主。

### 备份与恢复

备份：

```bash
tar czvf tif-backup-$(date +%Y%m%d).tar.gz -C /vol1/docker/temu-image-factory data .env
```

恢复：解压到原路径，确保 `.env` 与 `data/` 存在，然后 `docker compose up -d`。

### HTTPS 反向代理

当前容器**不包含** HTTPS 证书管理。建议由 NAS 反向代理、Nginx Proxy Manager 或 Cloudflare 提供 HTTPS，并将 `COOKIE_SECURE=true` 设置到 `.env`。

只有当应用确实位于**可信反向代理**之后时，才把 `TRUST_PROXY_HEADERS=true` 设置到 `.env`。开启后登录限速会使用 `X-Forwarded-For` 中的真实客户端 IP；默认 `false` 时只信任直接连接对端地址，防止客户端伪造 `X-Forwarded-For` 绕过登录限速。

### Session 说明

Web 会话使用 Starlette `SessionMiddleware`，即**服务器签名的客户端 Session Cookie**（signed client-side session cookie），不是服务器端 Session。Cookie 中只保存 `user`、`login_at`、`csrf_token` 等必要身份状态，绝不存储 API Key、密码、`SESSION_SECRET` 或任何 Provider 敏感数据。

## 项目结构

```
TEMU-Image-Factory/
├── input/                  # 商品原图（只读）
├── output/                 # 最终输出 <SKU>/<platform>/
├── cache/                  # 中间文件
├── logs/                   # 日志与 cost-ledger.jsonl
├── config/
│   ├── models.yaml         # 模型配置（价格、能力）
│   ├── routing.yaml        # 任务路由
│   ├── budget.yaml         # 预算保护
│   └── platforms/
│       └── temu.yaml       # TEMU 平台模板
├── templates/
│   ├── prompts/            # Prompt 模板
│   └── web/                # Jinja2 模板
├── src/
│   ├── core/               # 配置、路由、成本、Pipeline、Manifest
│   ├── providers/          # Mock、APIYI Gemini、APIYI OpenAI
│   ├── processors/         # 确定性处理、尺码表
│   ├── layouts/            # 平台布局
│   ├── utils/              # 路径、密钥脱敏、图片工具、默认配置初始化
│   ├── jobs/               # Job 抽象（未来异步任务，预留 SQLite 持久化）
│   ├── web/                # FastAPI、路由、静态资源
│   └── cli.py              # CLI 入口
├── tests/                  # 测试套件
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── pyproject.toml
```

## 扩展指南

### 添加新模型

在 `config/models.yaml` 增加：

```yaml
my_new_model:
  provider: apiyi_gemini
  model: gemini-x.x
  capabilities:
    text_to_image: true
    image_edit: true
  timeout: 300
  estimated_cost_usd: 0.05
  role: draft
```

然后在 `config/routing.yaml` 中绑定任务。

### 添加新 Provider

1. 在 `src/providers/` 创建 `my_provider.py`，继承 `ImageProvider`
2. 实现 `capabilities()`、`generate()`、`edit()`
3. 在 `src/providers/registry.py` 注册

业务 Pipeline 无需修改。

### 添加新平台

1. 在 `config/platforms/` 创建 `amazon.yaml`
2. 定义画布、任务列表、内容规则
3. CLI 使用 `--platform amazon`

## 测试

```bash
# 默认测试（不产生 API 费用）
pytest

# 仅 Web 测试
pytest -m web

# 真实 API 测试（需单独标记）
pytest -m live
```

## 安全注意事项

- **不要提交 `.env`**：已加入 `.gitignore`
- **不要粘贴 API Key 到 product.yaml**
- **不要把 API Key 写入 Prompt**
- 日志自动脱敏，仅显示最后 4 位密钥
- Web 层所有 AI 调用都在服务器端，API Key 不返回前端
- 上传文件使用 Pillow 验证真实图片，限制 30MB

## 常见错误

| 错误 | 解决 |
|------|------|
| `SESSION_SECRET is required` | 在 `.env` 中设置 `SESSION_SECRET` |
| `APIYI_API_KEY environment variable is required` | 在 `.env` 中设置 `APIYI_API_KEY` |
| `Permission denied` on NAS | 调整 `PUID/PGID` 或目录属主 |
| `Prompt template not found` | 检查 `templates/prompts/` 是否存在对应 YAML |
| `Task not found in platform` | 检查 `config/platforms/<platform>.yaml` 任务 ID |

## 当前限制

- Web UI 为简单 Jinja2 + HTMX，无复杂前端框架
- 仅单用户认证，无多用户/权限系统
- 未接入 ComfyUI（预留 `COMFYUI_BASE_URL`）
- 未接入 Amazon / Shopee / TikTok（配置层已预留）
- 真实 API 测试需手动执行，CI 默认不运行
