# OptionLens — 期权分析系统

实时期权数据分析工具，支持 T型期权链、IV曲面、模拟交易等功能。

## 一键部署到 Vercel

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new)

## 本地运行

直接用浏览器打开 `index.html` 即可（数据需要 CORS 插件或部署后使用）。

## 文件说明

```
index.html    — 主应用（纯静态，无需后端）
vercel.json   — Vercel 配置（反向代理 Yahoo Finance API，解决 CORS）
```

## 功能

- 全景数据：IV、IV Rank、HV、概率锥、期限结构
- T型期权链：实时 Bid/Ask/IV/Delta/Theta
- 波动率曲面热力图
- 策略沙盘：盈亏模拟
- 机会扫描：IV极值榜 + 财报跨式分析
- 模拟交易：从真实期权链选合约，完整账户系统
