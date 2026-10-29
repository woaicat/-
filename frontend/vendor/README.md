# 前端本地依赖

本工作台从同源 `/assets/vendor/` 加载以下已固定版本的浏览器文件，不依赖运行时 CDN 或 Node 构建：

| 文件 | 来源版本 | 用途 | 许可证 |
| --- | --- | --- | --- |
| `markdown-it.umd.min.js` | `markdown-it@15.0.3` 的 `dist/browser/markdown-it.umd.min.js` | 渲染智能体回复、大纲预览和 Markdown 文件 | [MIT](markdown-it.LICENSE) |
| `purify.min.js` | `dompurify@3.4.17` 的 `dist/purify.min.js` | 清理渲染后的 HTML，避免模型内容和文件内容注入脚本 | [Apache-2.0](DOMPurify.LICENSE) / [MPL-2.0](DOMPurify.LICENSE-MPL) |

浏览器中的 CSV 表格由项目代码解析并使用 `textContent` 创建单元格，不执行文件内容。更新版本时替换本目录的发行文件和许可证，并复核浏览器验收。
