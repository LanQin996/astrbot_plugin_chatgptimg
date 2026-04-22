# astrbot_plugin_chatgptimg

一个基于 AstrBot 的 GPT Image 生图插件，调用你配置的 CLIProxy `Responses API` 来生成图片。

## 功能

- 指令触发生图：`/gptimg 画一张赛博朋克的香港，要有汉字`
- 支持和你示例一致的 `stream=true` SSE 流式解析
- 可在 AstrBot WebUI 中直接配置 `api_url`、`api_key`、`model` 等参数
- 生成完成后自动把图片保存到 AstrBot 插件数据目录并回发

## 安装

把插件目录放到 AstrBot 的 `data/plugins/` 下，然后重载插件。

如果是手动安装，请确认插件目录中包含：

- `main.py`
- `metadata.yaml`
- `_conf_schema.json`
- `requirements.txt`

## 配置

在 AstrBot WebUI 的插件配置页填写：

- `api_url`：你的 CLIProxy `/v1/responses` 地址
- `api_key`：你的 CLIProxy API Key
- `model`：默认是 `gpt-image-1536x1024`
- `stream`：默认开启；如果代理不支持流式可以关闭

如果你希望尽量贴近你给的请求体，也可以额外配置：

- `parallel_tool_calls`
- `store`
- `reasoning_effort`
- `reasoning_summary`
- `include_reasoning_encrypted_content`

## 使用

生成图片：

```text
/gptimg 画一张赛博朋克的香港，要有汉字
```

可用别名：

```text
/生图 画一只戴墨镜的柴犬
/画图 未来城市夜景
/gimg anime girl with red scarf
```

## 说明

- 图片默认保存到 `data/plugin_data/chatgptimg/generated/`
- 如果当前 AstrBot 版本较旧，插件会自动回退到系统临时目录保存图片
- 插件实现参考了 AstrBot 官方插件开发文档中的最小实例、消息发送、插件配置等用法
