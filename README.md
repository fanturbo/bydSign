
还在每天手动打开比亚迪APP点签到？忘了签一天，连续签到奖励就清零。

这个小工具把签到这件事完全自动化：**部署一次，每天自动签，全家人的账号一起签**，还有个网页管理台随时看状态——哪个账号签到成功了、连续几天了、哪个账号登录过期了（标红提醒），一目了然。

## 它能做什么

- ✅ **每日自动签到** —— 挂在服务器/NAS/树莓派上定时跑，手机不用开机
- ✅ **网页管理台** —— 筗到状态、连续天数、积分、最近执行时间，全部可视化
- ✅ **过期自动标红** —— 账号的登录凭证失效了，页面上红色高亮 + 明确提示，绝不断签了都不知道
- ✅ **页面上管理账号** —— 添加/编辑/删除账号不用碰服务器文件，抓包的 curl 命令整条粘进页面，自动解出登录凭证填好
- ✅ **签到结果弹窗** —— 点「签到」立刻告诉你结果：签到成功 / 今日已签 / 失败原因

## 工作原理（一分钟看懂）

小程序每次请求服务器时，都会带上你的登录凭证（一个叫 `session_id` 的字符串）。这个工具做的事：

1. 你抓一次包，把自己的 `session_id` 拿出来（**只需一次**，下面有教程）
2. 工具用你的 `session_id`，按照小程序的加密协议，每天定时替你发签到请求
3. 结果写在网页管理台上给你看

所以它**不需要你的微信密码**，只是替你"点签到"这个动作。

## 快速开始

### 第 0 步：装环境

需要 Python 3.8+，然后：

```bash
git clone <本仓库地址>
cd bydSign
pip install -r requirements.txt
```

### 第 1 步：抓包拿 session_id（只需一次，约 5 分钟）

用什么抓都行，推荐：

| 平台 | 推荐工具 |
|---|---|
| Mac | Proxyman / Charles |
| Windows | Charles / Fiddler |
| iPhone | Stream |
| Android | HttpCanary |

步骤（以电脑抓包为例）：

1. 打开抓包工具，确认系统代理已开启、根证书已安装并信任
2. 用**你自己的微信**登录电脑版微信
3. 打开一次「比亚迪」小程序（打开就会产生请求）
4. 在抓包工具里找到发往 `weixin90.bydauto.com.cn` 的任意一条 POST 请求
5. 右键 → Copy as cURL，复制整条命令
6. 解密出 session_id：

```bash
python tools/byd_get_session.py --curl "粘贴整条curl命令"
```

会输出：

```
解密后的明文: {"session_id":"你的32位session_id","app_version":"240",...}

>>> session_id = XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

把这个 32 位字符串记下来。

> 抓完包记得关掉系统代理，不然微信上不了网。

### 第 2 步：跑起来

**方式 A：网页管理台（推荐）**

```bash
python tools/byd_admin.py
```

首次启动会自动生成管理台密码并打印在终端（也存在 `data/byd_admin.json` 里），然后浏览器打开 `http://127.0.0.1:8098`，输入账号密码就能看到管理台：

- 点「添加账号」→ 粘贴抓包的 curl → 点「解析填充」（session_id 自动填好）→ 起个名字 → 保存
- 点「全部签到」→ 弹窗告诉你每个账号的结果

**方式 B：命令行**

```bash
# 先把 data/byd_accounts.example.json 复制为 data/byd_accounts.json，填入 session_id
python tools/byd_sign_all.py              # 签所有账号
python tools/byd_sign_all.py --name 我的比亚迪   # 只签指定账号
```

**方式 C：定时自动签（真正的"全自动"）**

Linux/macOS 用 cron，每天早上 8 点签：

```bash
crontab -e
# 加一行（注意换成你的实际路径）：
5 8 * * * cd /path/to/bydSign && python tools/byd_sign_all.py
```

或者 systemd timer（错过会自动补跑，服务器重启不怕）：

```ini
# /etc/systemd/system/byd-sign.service
[Unit]
Description=BYD miniprogram auto sign-in

[Service]
Type=oneshot
WorkingDirectory=/path/to/bydSign
ExecStart=/usr/bin/python3 tools/byd_sign_all.py

# /etc/systemd/system/byd-sign.timer
[Unit]
Description=Run byd-sign daily

[Timer]
OnCalendar=*-*-* 08:05:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now byd-sign.timer
```

管理台也一样可以做成 systemd 服务常驻，这样随时打开网页都能看状态。

## 常见问题

### 点签到显示「今日已签到」？

正常！说明今天已经签过了（定时任务签的，或者你手动点过）。这叫 `duplicate`，连续天数不受影响。

### 账号标红说 session 过期了怎么办？

**重要机制：session 是"新顶旧"的** —— 你用同一个微信重新登录比亚迪小程序时，小程序会拿到一个新的 session_id，旧的就立刻失效。

所以：

- 配好之后**别随便打开那个小程序**，一打开可能就要重新弄一次
- 真过期了也不慌：重新抓一次包（第 1 步，5 分钟），在管理台点「编辑」把新 session_id 粘进去就行

工具内置了 `checkSession` 检测：签到失败时会自动复核，明确区分「确认过期」和「偶发失败」，不会冤枉你的账号。

## 项目结构

```
bydSign/
├── README.md                    # 本文件
├── requirements.txt             # 依赖（就两个）
├── tools/
│   ├── byd_app_sign.py          # 协议核心：加密、网关、签到、checkSession
│   ├── byd_get_session.py       # 从抓包 curl 解出 session_id
│   ├── byd_sign_all.py          # 多账号批量签到
│   └── byd_admin.py             # 网页管理台（纯标准库）
└── data/                        # 运行时数据（已 gitignore，不会上传）
    ├── byd_accounts.json        #   账号配置
    ├── byd_state.json           #   签到状态
    └── byd_admin.json           #   管理台密码
```

## 免责声明

- 本项目仅供个人学习与技术研究，请勿用于任何商业用途
- 自动签到本质是模拟客户端请求，理论上存在账号风险，请自行评估
- 与比亚迪官方无关，若有侵权请联系删除
