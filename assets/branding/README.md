# iCourse 正式图标

用户已选定「秩序成为海浪」作为项目图标，后续网页、桌面或移动应用沿用这一设计。

## 设计来源

来自 Anne Carson 的 *Short Talk on the Total Collection*：收藏者排除缺失的愿望形成越来越强的分类秩序，蓝色三角形最终被旁人称为海浪。
保留已选定的圆润三角外轮廓；内部三层蓝色由左侧平直排列向右逐渐弯曲、上涌。午夜蓝背景与浅蓝顶层形成小尺寸下的明暗关系。

## 正式资产

- `icourse-approved.png`：用户选定原图的完整副本，作为外观依据。
- [icourse-icon.svg](../../local_web/static/branding/icourse-icon.svg)：纯贝塞尔曲线、裁切路径和矢量渐变，可编辑、可任意缩放，不内嵌位图。
- [icourse-icon-1024.png](../../local_web/static/branding/icourse-icon-1024.png)：基于选定原图导出的 1024 × 1024 应用图标，无预置圆角。
- `local_web/static/branding/` 同时提供 512、192、180、32 像素 PNG、SVG favicon 与 ICO。

矢量版重绘原图的轮廓、分层和颜色，平滑渐变代替原图的细微位图纹理，因此并非逐像素相同。当前页面标识与主屏幕 PNG 使用选定原图的尺寸导出。

## 使用约定

原生应用使用完整方形图标，由平台添加遮罩；网页中的圆角通过 CSS 添加。不要再次裁切、拉伸三角形或另行改造海浪构图。
本地控制台和 GitHub Pages 共用 `local_web/static/`，相关资源保持相对路径。浏览器 favicon 使用同一设计的矢量版本；页面和快捷方式使用选定原图导出的 PNG。

较早的九稿及海浪初版保留为设计过程，不作为正式图标。
