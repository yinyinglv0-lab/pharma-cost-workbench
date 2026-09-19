# 第三方组件来源与通知

本文件只记录随源码包实际携带的浏览器资源及其上游通知，不为本项目代码选择许可证，也不扩大赛题数据或模型权重的分发范围。已取得的纯源码发布授权及项目尚未选定自身开源许可证的状态见[README](README.md)；第三方组件仍各自遵循对应许可，不能由一项源码发布授权替代所有权利人的使用与分发条件。

## Apache ECharts

- 文件：`assets/echarts.min.js`。
- 本地导出版本：5.5.1。
- SHA256：`e84270bd0cd5bdf60fefc26d00c2a391cb2e81f4d26a7a9ee16185a54773a3cf`。
- 2026-09-18只读对照[固定版本公开分发资源](https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js)，字节数1030855、完整SHA256与本地一致。本次未修改该资源，原有版权和许可证头保留。
- [上游项目](https://echarts.apache.org/)、[5.5.1源码与包声明](https://github.com/apache/echarts/tree/5.5.1)。
- Apache-2.0全文及上游子组件提示见 [assets/echarts-LICENSE.txt](assets/echarts-LICENSE.txt)，从[版本标签LICENSE](https://raw.githubusercontent.com/apache/echarts/5.5.1/LICENSE)抄录（空行规范化）。这份许可只适用于相应第三方作品。
- 上游NOTICE见 [assets/echarts-NOTICE.txt](assets/echarts-NOTICE.txt)，来源为[同版本NOTICE](https://raw.githubusercontent.com/apache/echarts/5.5.1/NOTICE)：Apache ECharts，Copyright 2017–2024 The Apache Software Foundation。
- 根LICENSE所引用的D3算法许可另附 [assets/licenses/LICENSE-d3](assets/licenses/LICENSE-d3)，来源为[同版本LICENSE-d3](https://raw.githubusercontent.com/apache/echarts/5.5.1/licenses/LICENSE-d3)。文中的上游 `/licenses/LICENSE-d3`在本包对应此assets子目录。

ECharts 5.5.1的[package.json](https://raw.githubusercontent.com/apache/echarts/5.5.1/package.json)登记zrender 5.6.0和tslib 2.3.0；随打包资源包含的通知继续保留：

| 子组件 | 本包附带通知 | 来源 |
|---|---|---|
| ZRender 5.6.0 | [BSD 3-Clause](assets/licenses/LICENSE-zrender) | [zrender 5.6.0 LICENSE](https://raw.githubusercontent.com/ecomfe/zrender/5.6.0/LICENSE)；JS中保留原ZRender版权头 |
| tslib辅助代码 | [Microsoft许可通知](assets/licenses/LICENSE-tslib) | 直接抄录本地 `echarts.min.js`的Microsoft版权/许可段；不声称该通知授予ECharts以外作品的权利 |

## 安装时依赖及未附组件

Python依赖列在 `requirements.txt`、Windows CPython 3.12的 `requirements-core-win-py312.lock.txt`和可选 `requirements-models.txt`；正式框架依赖包括固定版本的LangChain-Core与LangSmith。本源码包不内嵌wheel、虚拟环境或第三方Python源码。安装或生成容器镜像时，应对实际解析版本及系统包另建组件清单、保存各许可文本。`deploy/generate_inventory.py`可记录安装环境元信息；该输出不等于许可证批准、完整SBOM、漏洞扫描或来源证明。

历史实验依赖登记在 `requirements-legacy.txt`；旧Chroma/FlagEmbedding/PyMuPDF等工具不在默认正式源码包运行范围。不能因保留该依赖说明就把其许可和运行依赖解释成当前正式链全部已验收。

字体、模型权重、赛题官方mock源码、原始CSV/PDF/DOCX与报告模板均不在包内。部署者须从合法授权渠道单独提供，并核实字体嵌入、模型及原件使用与再分发条件。
