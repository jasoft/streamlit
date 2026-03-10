# Stock Analysis

统一入口：

```bash
streamlit run stockview/app.py
```

当前集成的功能：

- 综合面板：成交量与情绪、龙头股活跃度、指数 40 日收益差
- IF-IM 风格配对：沪深300 / 中证1000 指数比值、TTM PE 比值、历史分位、成交占比、条件胜率
- 指数成交额风格对比：观察沪深300 / 中证1000 / 中证2000 占总成交额比例与风格超额
- 市场拥挤度：抓取乐咕乐股拥挤度页面并生成双轴图
- 创业板成交占比：观察创业板占沪深总成交额比例

脚本入口：

```bash
./.venv/bin/python scripts/if_im_style_analysis.py
```

Playwright 烟雾测试：

```bash
npm run test:playwright
```

目录说明：

- `stockview/`: Streamlit 页面与分析模块
- `scripts/`: 可单独执行的分析脚本
- `tests/`: Playwright 烟雾测试
- `outputs/`: 脚本和页面生成的结果文件
