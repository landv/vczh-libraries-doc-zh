# vczh-libraries 文档站点

## 项目简介
本项目是 **vczh-libraries** 非官方文档站点，提供了所有组件、工具库、接口和示例的中文文档。站点使用 GitHub Pages 自动部署，访问地址为：
```
https://landv.github.io/vczh-libraries-doc-zh/
```
如果你想快速浏览文档，直接点击上述链接即可。

## 目录结构
```
📁 doc/          # 主文档目录
	├─ current/   # 当前版本文档
	│  ├─ home.html
	│  ├─ gacui/  # GACUI 相关文档
	│  ├─ scripts/ # JavaScript 代码片段
	│  └─ ...      # 其它子模块
	└─ ver1/      # 版本 1 以前的文档
📁 scripts/      # 全局脚本
📁 resources/    # 资源文件
```
每个子模块都有对应的 `home.html` 作为入口页面，其他页面通过 `category.css`、`article.css` 等样式文件统一风格。

## 快速使用
1. **访问站点**：直接打开上方链接，选择对应模块即可查看文档。  
2. **本地预览**：python start.py

