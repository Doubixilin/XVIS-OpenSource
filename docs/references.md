# 设计与工程参考

[返回首页](../README.md) · [产品思路](product.md) · [设计说明](design.md) · [技术架构](architecture.md)

XVIS 的设计来自个人跨设备工作需求，也参考了已有工具对阅读、移动交互和异步执行的处理。下面区分运行依赖、设计启发与文档组织参考。外部项目后续可能变化；本页说明的是本项目当前吸收的思路。

## 执行基础

### DeepSeek Harness

[官方源码](https://github.com/deepseek-ai/deepseek-harness)

DSH 是当前唯一业务执行引擎。XVIS 在它外围负责手机入口、任务持久化、节点身份、上下文快照、交互转发和成果交付。固定运行包与源码验证基线见 [技术架构](architecture.md)，升级需检查协议兼容性。

DSH 源码和运行包没有被复制进本仓库；部署者单独获取并遵守其许可。XVIS 与 DeepSeek 无隶属关系。

## 移动执行体验

### DeepSeek Harness Mobile 与扫码远程接入

[DeepSeek Harness Mobile](https://github.com/yukiykchen/deepseek-harness-mobile) · [dsh-scan-remote](https://github.com/yukiykchen/dsh-scan-remote)

最有价值的启发是把执行过程中的“人需要回来做一个决定”放进移动端：回答中途问题、核对工具操作、允许一次或拒绝，以及给仍在执行的工作补充要求。

XVIS 据此推进了 Host 的 Node → Hub → App 问答／批准链、正文增量、steer／queue、阅读位置恢复和配对身份。实现继续沿用 SwiftUI、Python Node 与自有中枢协议；没有转为 Kuikly 技术栈，也没有把参考项目源码并入仓库。

交互相似不代表能力和验收范围相同。当前 Host 仍限定测试工作区，相机扫码与第二物理节点仍待验收，见 [开发进展](progress.md)。

## 视觉与交互

| 来源 | 在 XVIS 中吸收的方向 | 对应实现 |
| --- | --- | --- |
| [Apple · Materials](https://developer.apple.com/design/human-interface-guidelines/materials) | 用系统材质表达浮动层，内容保持可读 | 导航和输入区材质、旧系统及减少透明度路径 |
| [Apple · Motion](https://developer.apple.com/design/human-interface-guidelines/motion) | 反馈动作和层级变化，尊重辅助设置 | 短按压动画、状态过渡、减少动态效果路径 |
| [Linear · UI redesign](https://linear.app/now/how-we-redesigned-the-linear-ui) | 通过层级、对齐与密度组织信息 | 安静正文、辅助元数据、集中关键动作 |
| [Things · OS 26 update](https://culturedcode.com/things/blog/2025/09/things-for-os-26/) | 原生工具吸收新系统语言时保持内容优先 | 系统交互、克制材质与明确任务状态 |

暖纸、石墨、玉青是项目选定的配色方向；X 标记由作者原创。颜色值和动效参数以本项目当前代码为准，而不是把外部文章当作通用数值标准。完整说明见 [设计文档](design.md)。

## 文档组织

[Tuomin Open Source](https://github.com/Doubixilin/tuomin-open-source) 是同一作者的另一个公开项目。本次介绍参考了它把目的、设计、架构、流程与验证边界分开讲清楚的组织方式，让读者既能快速了解产品，也能沿源码和证据继续深入。

Tuomin 的脱敏能力没有因此成为 XVIS 的功能或运行依赖。XVIS 当前不会自动脱敏照片里的文字、任务正文或文件内容。

## 关于引用与资产

本页链接用于注明启发和上游，不意味着任何合作或背书。没有复制外部文章正文、第三方 App 截图或宣传图。仓库内展示的是本项目自己的 App 与合成演示资料。

依赖清单及许可见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，图片来源见 [assets.md](assets.md)。
