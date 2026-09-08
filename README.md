# 火山引擎示例代码集

火山引擎（Volcengine/BytePlus）各产品线的 Python 示例代码集合。

## 目录

- [Seedance 2.5 — 视频生成与编辑](#seedance-25)
- [Doubao Search — 豆包搜索 API](#doubao-search)

---

## Seedance 2.5

BytePlus/火山引擎 Seedance 2.5 视频生成与编辑的 Python 示例代码。

### 环境配置

```bash
cp .env.example .env
# 编辑 .env 填入你的 ARK_API_KEY 等凭证
```

### 文件说明

| 文件 | 功能 |
|------|------|
| `seedance_demo.py` | 视频生成 Demo，支持单个/多个 asset ID 或图片+视频混合参考 |
| `seedance_video_edit.py` | 视频编辑（替换主体、增删对象、局部重绘），基于 `omni_reference_task_type=edit` |
| `seedance_video_extend.py` | 视频延长（向前/向后），基于 `omni_reference_task_type=extend` |
| `seedance_asset_upload.py` | 上传本地图片/视频到私有资产库 |
| `batch_process.py` | 批量处理：从 `clip/` 目录读取 MP4 片段，逐个调用 Seedance 生成 |
| `upload_extend_assets.py` | 批量上传指定文件到素材库 |

### 快速开始

```bash
# 视频生成
python3 seedance_demo.py

# 视频编辑
python3 seedance_video_edit.py <video_asset_id> [image_asset_id ...]

# 视频延长
EXTEND_DURATION=26 python3 seedance_video_extend.py <asset_id>

# 资产上传
ASSET_LOCAL_FILE_PATH=/path/to/video.mp4 python3 seedance_asset_upload.py
```

### 相关文档

- [Seedance 2.5 视频编辑 API](https://docs.volcengine.com/docs/82379/2607688)
- [Seedance 2.5 视频延长 API](https://docs.byteplus.com/en/docs/ModelArk/1520757)

---

## Doubao Search

豆包搜索 API 的 Python 示例代码，支持 Global/Custom 双模式搜索、AI 搜索和 Agent 联网问答。

### 环境配置

```bash
cd doubao_search
pip install -r requirements.txt
# 在 .env 中设置 DOUBAO_API_KEY 和 AGENT_API_KEY
```

### 文件说明

| 文件 | 功能 |
|------|------|
| `doubao_search_demo.py` | 豆包搜索 API 测试脚本，支持 Global 全域搜索与 Custom 自定义搜索双模式 |
| `ai_search_demo.py` | AI 搜索 Demo：搜索 + Seed Pro 大模型两阶段智能问答，支持流式输出 |
| `agent_search_demo.py` | 联网问答 Agent API 测试脚本，支持流式输出大模型总结 + 搜索参考资料 |

### 快速开始

```bash
# 基础搜索
python3 doubao_search_demo.py --query "你的搜索关键词"

# AI 搜索（搜索 + 大模型总结）
python3 ai_search_demo.py --query "你的问题"

# Agent 联网问答
python3 agent_search_demo.py --query "你的问题"
```

### 相关文档

- [豆包搜索 API 文档](https://www.volcengine.com/docs/82379/1495106)