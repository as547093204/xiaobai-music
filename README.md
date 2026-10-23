---
title: Xiaobai Music
sdk: docker
app_port: 7860
---

# 小白音乐服务

动作工具立即返回接受结果，动作由舵机任务执行；音乐由独立音乐任务播放。
新动作会替换旧动作，连续动作需要查询 `self.dog.action/get_current`，空闲后再提交。
摇摇车使用 `self.music.search` 搜索后，调用 `self.music.play(id, rocking=true)`。
不要在提交摇摇车后紧接着调用其他动作，否则会打断摇摆。

## 不需要电脑常开的部署

本服务负责搜索和把网络音源转换成 C3 已有播放器支持的单声道 24 kHz、20 ms Opus。
一般音乐 API 返回 MP3，不能把 API 地址直接填进当前播放器。

可以在 Hugging Face 创建 Docker Space（目前创建需要付费账户，CPU Basic 无小时费用），把本目录中的
`README.md`、`Dockerfile`、`requirements.txt`、`server.py` 上传到 Space 根目录。
在 Settings 的 Variables 中添加：

```
METING_URL=https://api.mxin.me/api/v1/meting
```

不设置该变量则使用原有网易公开搜索和音频外链。
运行后在机器狗控制页填写 `https://你的用户名-空间名.hf.space`。
这是你部署的转换服务地址，不能填写上面的 Meting 地址。
原有 `caiyu_music/bridge_url` 设置键保留，HTTP 局域网地址仍然可用。
Space 会在闲置时休眠；首次使用先在浏览器打开 Space 唤醒。
国内网络到 Space、Space 到曲源的可达性需要实际验证，不保证免费托管持续在线。
服务没有账户隔离，适合个人实验，勿分享地址给不可信用户。

**免费托管候选：Render Free Web Service。** 将本目录放入自己的 Git 仓库，
创建 Web Service，选择 Docker、Free 实例、端口 7860，设置同一个 `METING_URL`。
设备填写分配的 `https://xxx.onrender.com` 地址。可直接使用本目录的 Dockerfile。
Render 官方文档说明：15 分钟无请求后休眠，唤醒约一分钟；每月共享 750 小时，
流量和构建也有额度。先在浏览器访问根地址唤醒后再点歌。重启会丢失缓存，需要重新搜索。
尚未在 Render 实际部署验证，云端地区访问网易曲源可能与本机测试不同。
[Render 免费限制](https://render.com/docs/free)。
也可将部署包解压为独立 Git 仓库，使用附带的 `render.yaml` 创建 Render Blueprint。

本地或 NAS Docker：

```sh
docker build -t xiaobai-music .
docker run --rm -p 7860:7860 -e METING_URL=https://api.mxin.me/api/v1/meting xiaobai-music
```

固件使用 `python scripts/build.py caiyu-c3 --name caiyu-c3-music` 构建。
其他变体没有启用 `CONFIG_CAIYU_MUSIC`。未更改原板引脚。

## 曲目与实际验证

2026-10-23 本机通过公开 Meting 接口完成下载和 FFmpeg 转换：

| 搜索词 | 选中的版本 | 音源 ID | 转换后时长 |
| --- | --- | --- | --- |
| 哈基米南北绿豆 | 明宇 | netease-3368687705 | 138.325 秒 |
| 家族歌 | 新时代小星星 | netease-2103481915 | 258.051 秒 |

这仅验证下载、Opus 转换和 Ogg 格式检查，没有实机播放或人工试听确认版本。
搜索“哈基米南北绿豆”；“爸爸的爸爸是爷爷”也可尝试“家族歌”“亲属称呼歌”。
搜索命中不代表一定能播放：公开源可能失效、地区受限或返回试听。
有原始时长的结果会检查转换后时长；Meting 缺少时长时无法保证是整曲。
可以把自己有权使用的 MP3/WAV/OGG/M4A/FLAC 放在服务的 `library` 目录，按文件名搜索。
不使用绕过付费权限的解析功能。

刷机后需在实物上检查：播放中挥手/走路、摇摇车停止后站立、按键/唤醒中断、
断网退出和重新点歌、音频有无卡顿及舵机供电压降。编译通过不等于硬件验证。

参考：[Docker Spaces](https://huggingface.co/docs/hub/spaces-sdks-docker)、
[免费资源与休眠](https://huggingface.co/docs/hub/spaces-overview)。
