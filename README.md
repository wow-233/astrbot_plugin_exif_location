# 图片 EXIF 定位

`astrbot_plugin_image_exif` —— 读取聊天图片 EXIF 中的 GPS 坐标，逆地理编码后以玩笑的方式回复一句话：

> 请问是中国浙江省宁波市鄞州区首南街道学府路地铁站的小明群友吗？🤔

自动回复只有这一句，不含坐标、设备、地图链接等任何额外内容；地址精确到街道和附近地点。

## 功能

- **自动监听**：群友发图后自动检查 EXIF，无需 @ 机器人。
- **一句话回复**：只输出模板这一句，`{region}` 为完整详细地址（国家+省+市+区+街道+地点）。
- **地址解析**：BigDataCloud（免 Key）、高德地图（需 Key）、OpenStreetMap Nominatim，自动按国内/海外选择顺序并自动回退。
- **附近地点**：通过 Photon（OpenStreetMap 免费服务，免 Key）识别拍摄点最近的地标或道路，让地址精确到具体地点。
- **坐标转换**：EXIF 的 WGS-84 坐标自动转换为高德/百度使用的坐标系。
- **`/exif` 指令**：显式查询时输出完整 EXIF（设备/时间/海拔等）、解析地址、附近地点和地图链接。
- **稳定性**：地址缓存、请求限速、按人冷却、附近地点服务熔断（连续失败自动暂停 10 分钟）。

## 安装

### 方式一：WebUI 上传（推荐）

1. 打开 AstrBot WebUI → **插件** → **安装插件** → **从文件上传**。
2. 选择本目录打好的 `astrbot_plugin_image_exif.zip`。
3. 安装完成后重载插件（或重启 AstrBot）。
4. 在插件配置中按需填写高德 Key（可选）。

### 方式二：手动解压

把 `astrbot_plugin_image_exif` 整个文件夹放进：

```text
AstrBot/data/plugins/astrbot_plugin_image_exif/
```

然后重载插件。

插件依赖 `Pillow` 和 `httpx`，AstrBot 本体已自带；如果缺失，AstrBot 会根据 `requirements.txt` 自动安装。

## 使用

- 直接在群里发一张带 GPS 信息的照片，插件会自动回复一句话。
- 发送 `/exif` 并附带图片，或引用一张图片后发送 `/exif`，可以查看完整 EXIF 与地图链接。

> 注意：聊天平台（QQ、微信等）经常会在发送时压缩图片并**移除 EXIF**。只有以“原图”发送、且拍摄时开启了定位的照片才可能带 GPS。

## 配置说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | 开 | 自动定位回复总开关（不影响 `/exif`） |
| `provider` | `auto` | 国内：有高德 Key 用高德，否则 BigDataCloud，失败自动回退；海外不走高德 |
| `amap_api_key` | 空 | 高德 Web 服务 Key（`secret`，不会回显） |
| `enable_poi_lookup` | 开 | 用 Photon 识别附近地点/道路（免 Key） |
| `poi_radius` | 300 | 附近地点搜索半径（米） |
| `reply_template` | `请问是{region}的{sender}群友吗？🤔` | 回复模板，见下方占位符 |
| `report_unresolved` | 关 | 解析失败时仍回复坐标 |
| `enable_groups` | 开 | 群聊中启用 |
| `enable_private` | 开 | 私聊中启用 |
| `only_when_mentioned` | 关 | 只在 @ / 唤醒词时检测 |
| `max_images` | 2 | 单条消息最多检测几张图 |
| `cooldown_seconds` | 5 | 同一个人发图的触发冷却 |
| `cache_ttl` | 86400 | 地址缓存秒数 |
| `timeout` | 10 | 请求超时秒数 |
| `contact_email` | 空 | Nominatim 建议填写联系邮箱 |
| `stop_event_after_reply` | 关 | 回复后停止事件，避免私聊中 LLM 再回复一次 |

### 回复模板占位符

`{region}` `{country}` `{province}` `{city}` `{district}` `{township}` `{poi}` `{address}` `{lat}` `{lng}` `{sender}` `{group}`

- `{region}` 是完整详细地址，例如 `中国浙江省宁波市鄞州区首南街道学府路地铁站`。
- `{sender}` 是发送者昵称，不想带昵称可以把模板改回 `请问是{region}的群友吗？🤔`。
- 想要短一点可以只写到省市：`请问是{country}{province}{city}的群友吗？🤔`。

### 关于高德 Key

1. 打开 [高德开放平台控制台](https://console.amap.com/dev/key/app)。
2. 创建应用，添加 Key，服务平台选择 **Web 服务**。
3. 把 Key 填入插件配置的 `amap_api_key`。
4. 将 `provider` 设为 `auto` 或 `amap`。

高德对国内 POI 的识别最好（可以识别到“宁波大学”这类地点）。不填 Key 时使用 BigDataCloud + Photon，同样能解析到省/市/区/街道和附近地点。

### 关于免 Key 服务

- **BigDataCloud**：免费、无需 Key，国内可直连，提供省/市/区/街道等行政区划。
- **Photon**：OpenStreetMap 的免费检索服务，无需 Key，返回拍摄点最近的地标（地铁站、学校、剧院等）或道路。服务偶发不可用时会自动跳过并暂停 10 分钟。
- **OpenStreetMap Nominatim**：免费、无需 Key，要求遵守 [使用政策](https://operations.osmfoundation.org/policies/nominatim/)（最多约 1 次/秒），海外地址更丰富。

## 隐私说明

- 插件只在本地读取 EXIF，**不会上传图片**。
- 图片中的经纬度会发送给所选的解析服务（BigDataCloud / Photon / 高德 / Nominatim），用于换取文字地址。
- 解析结果只保存在内存缓存里，重启即清空，不落盘、不上报。
- 日志做了脱敏处理：不会记录坐标、详细地址、图片链接或 API Key。
- 解析出的位置会公开回复到当前会话。如果不想在群里公开，请把 `enable_groups` 关掉，或开启 `only_when_mentioned`。
- 插件只读取 EXIF，不会修改图片。
- `amap_api_key` 在 WebUI 中以密码框显示，请勿把包含 Key 的配置文件分享给他人。

## 常见问题

**Q：发了照片没有反应？**

1. 平台可能已压缩图片并移除 EXIF（QQ 群里很常见）。
2. 照片拍摄时没有开启定位。
3. 截图、表情包、从网上下载的图片通常没有 GPS。
4. 用 `/exif` 确认图片是否真的带 GPS。
5. 检查 `cooldown_seconds`、`enable_groups` / `enable_private`、`only_when_mentioned` 配置。

**Q：完全没有回复，怎么排查是没 EXIF 还是解析失败？**

发送 `/exif` 并附带同一张图片：如果连 GPS 坐标都没有，说明图片没有位置信息；如果有坐标但没有解析地址，说明是网络/服务问题。也可以开启 `report_unresolved` 让解析失败时回复坐标。

**Q：地址里出现的是一条路的名字？**

Photon 返回的是离拍摄点最近的命名要素，郊区/路口附近可能只有道路名。它仍然比纯行政区划更精确；如果配置了高德 Key，会优先使用高德的 POI。

**Q：能识别 iPhone 的 HEIC 照片吗？**

需要额外安装 `pillow-heif`（未写入依赖，避免增加安装体积）：

```bash
pip install pillow-heif
```

安装后插件会自动启用 HEIC 支持。

## 更新记录

### 1.2.0

- 自动回复精简为一句话，不再输出详细地址、附近地点、坐标、设备、海拔、地图链接等附加行。
- `{region}` 改用完整详细地址（国家+省+市+区+街道+附近地点），直接体现在那句话里。
- 移除对应的 8 个输出开关配置；`/exif` 指令仍保留完整信息与地图链接。

### 1.1.0

- 新增「附近地点」：通过 Photon 识别最近地标/道路并显示距离（免 Key）。
- 回复模板默认带上发送者昵称：`请问是{region}的{sender}群友吗？🤔`。
- `provider=auto` 按国内/海外自动选择服务顺序，海外坐标不再请求高德。
- 冷却改为按「会话 + 发送者」。

### 1.0.0

- 首个版本：EXIF GPS 读取、逆地理编码、玩笑式回复、`/exif` 指令。

## 目录结构

```text
astrbot_plugin_image_exif/
├── main.py              # 插件入口、消息监听、/exif 指令
├── exif_utils.py        # EXIF / GPS 解析
├── geocoder.py          # 逆地理编码、附近地点、坐标转换、缓存
├── formatting.py        # 回复文案与地图链接（供 /exif）
├── _conf_schema.json    # WebUI 配置定义
├── metadata.yaml        # 插件元数据
├── requirements.txt     # 依赖
├── logo.png             # 插件图标
├── LICENSE              # MIT 许可证
├── .gitignore
└── README.md
```
