# Firmware Image Assembly Service

FastAPI 服务：接收 Intel HEX 文本，逐行校验（字节数 + 二补校验和），按地址组装固件映像，
返回合并后的连续 Base64 段、段间空洞、入口地址与逐行诊断；并可按命名内存区域（regions）
校验映像字节与入口地址的落点。

## 快速启动（单条命令）

```bash
docker compose up --build
```

启动后访问：

- 接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

不使用 Compose 时：

```bash
docker build -t hexasm . && docker run --rm -p 8000:8000 hexasm
```

本地开发（无 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest                 # 运行测试
```

## 接口

### `POST /api/v1/assemble`

JSON 请求体：

```json
{
  "hex_text": ":020000040800F2\n:10000000214601360121470136007EFE09D2190141\n:04000005080001C12D\n:00000001FF\n"
}
```

### `POST /api/v1/assemble/raw`

`text/plain` 请求体，直接粘贴 Intel HEX 文本：

```bash
curl -s -X POST http://localhost:8000/api/v1/assemble/raw \
  -H 'Content-Type: text/plain' \
  --data-binary @firmware.hex | jq
```

### 响应

无论 HEX 是否通过校验均返回 200，结果以 `ok` 与 `diagnostics` 判定：

```json
{
  "ok": true,
  "error_count": 0,
  "warning_count": 0,
  "entry_address": 134218177,
  "total_bytes": 16,
  "segment_count": 1,
  "segments": [
    {"start": 134217728, "end": 134217744, "size": 16, "data_base64": "IUYBNgEhRwE2AH7+CdIZAQ=="}
  ],
  "gaps": [],
  "diagnostics": [
    {"line": 1, "status": "ok", "record_type": "extended_linear_address",
     "address": 134217728, "byte_count": null, "message": "linear base set to 0x8000000"},
    {"line": 2, "status": "ok", "record_type": "data",
     "address": 134217728, "byte_count": 16, "message": "data: 16 byte(s) at 0x8000000"}
  ]
}
```

- `segments[]`：按地址升序合并的连续段，`data_base64` 为段内容，`end` 为开区间。
- `gaps[]`：相邻段之间的空洞（`start` 含、`end` 不含）。
- `entry_address`：最后一个 03/05 型记录给出的入口地址，无则为 `null`。
- `diagnostics[]`：逐行结果，`line` 从 1 开始，`0` 表示文件级问题（如缺失 EOF）。

### `POST /api/v1/validate-layout`

在组装基础上校验内存布局。请求体在 `hex_text` 之外增加 `regions` 数组，每个区域为
半开区间 `[start, end)`，字段：`name`、`start`、`end`、`allow_data`（默认 `true`）、
`allow_entry`（默认 `false`）：

```json
{
  "hex_text": ":020000040800F2\n:10000000214601360121470136007EFE09D2190141\n:04000005080001C12D\n:00000001FF\n",
  "regions": [
    {"name": "flash", "start": 134217728, "end": 134218752, "allow_data": true, "allow_entry": true},
    {"name": "config", "start": 134218752, "end": 134219776, "allow_data": false}
  ]
}
```

```bash
curl -s -X POST http://localhost:8000/api/v1/validate-layout \
  -H 'Content-Type: application/json' \
  -d @layout-request.json | jq
```

校验规则：

- HEX 解析失败（存在 error 级诊断）时只返回 `diagnostics`，不执行布局判断：
  `hex_ok=false`、`layout_evaluated=false`，`regions` 与 `violations` 为空。
- HEX 有效时要求 `regions` 为互不重叠的半开区间；`start >= end` 或区域重叠分别报
  `region_invalid` / `region_overlap`，并跳过字节级检查（`layout_evaluated=false`）。
- 固件连续段按区域边界切分：未被任何区域覆盖的字节报 `uncovered`，落入
  `allow_data=false` 区域的字节报 `data_not_allowed`。
- 存在入口地址（03/05 记录）时，必须落入 `allow_entry=true` 的区域，否则报
  `entry_not_allowed`（未覆盖任何区域时 `region` 为 `null`）。

响应示例（上例请求）：

```json
{
  "ok": true,
  "hex_ok": true,
  "layout_evaluated": true,
  "error_count": 0,
  "warning_count": 0,
  "entry_address": 134218177,
  "total_bytes": 16,
  "regions": [
    {"name": "flash", "start": 134217728, "end": 134218752, "size": 1024,
     "allow_data": true, "allow_entry": true, "occupied_bytes": 16, "utilization": 0.015625},
    {"name": "config", "start": 134218752, "end": 134219776, "size": 1024,
     "allow_data": false, "allow_entry": false, "occupied_bytes": 0, "utilization": 0.0}
  ],
  "violations": [],
  "diagnostics": [
    {"line": 1, "status": "ok", "record_type": "extended_linear_address",
     "address": 134217728, "byte_count": null, "message": "linear base set to 0x8000000"}
  ]
}
```

- `regions[]`：按地址升序的区域汇总，`occupied_bytes` 为落入该区域且被允许的字节数，
  `utilization = occupied_bytes / size`。
- `violations[]`：`kind` 取值 `uncovered` / `data_not_allowed` / `entry_not_allowed` /
  `region_overlap` / `region_invalid`；`start`/`end` 为违规字节的半开区间。
- `ok`：HEX 校验通过且无任何违规时为 `true`；与 assemble 一样，解析失败也返回 200。

## 解析规则

- 每行必须形如 `:llaaaatt<data>cc`；校验 `ll`（字节数）与实际数据长度一致，
  且全部字节（含 `cc`）之和 ≡ 0 (mod 256)（二补校验和）。
- 支持的记录类型：
  - `00` 数据：写入 `当前基址 + 记录内偏移`；
  - `01` EOF：必须唯一且位于末行，其后出现任何记录（含第二个 EOF）均报错，缺失 EOF 同样报错；
  - `02` 扩展段地址：基址 = 值 << 4；
  - `03` 段入口（CS:IP）：入口 = CS << 4 + IP；
  - `04` 扩展线性地址：基址 = 值 << 16；
  - `05` 线性入口：入口 = 32 位值。
- 同一地址重复写入相同字节：接受（诊断中注明 identical rewrite）；
  写入冲突字节：报错并给出当前行号、冲突地址与首次写入的行号，保留首次写入的值。
- 校验失败的记录不会写入映像；空行忽略并记 warning。

## 项目结构

```
app/
  main.py        # FastAPI 应用与请求/响应模型
  hex_parser.py  # Intel HEX 解析与映像组装（纯逻辑，可独立测试）
  layout.py      # 内存布局校验：区域切分、占用统计与违规报告（纯逻辑）
tests/
  test_hex_service.py
  test_validate_layout.py
Dockerfile
docker-compose.yml
requirements.txt       # 运行依赖
requirements-dev.txt   # 测试依赖
```
