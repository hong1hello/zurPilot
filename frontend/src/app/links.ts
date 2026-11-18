/**
 * 站点级外链的唯一来源。
 *
 * 为什么单独放一个文件：这个地址以前在 App.tsx 与 workbench.theme.tsx 里各写一份，
 * 一份是 fork 一份是上游官方仓库，点到「开源」会跳到不同的地方——用户看到的就是
 * 「项目地址被改回上游」。任何新增的「打开源码 / 项目主页」入口都必须从这里取，
 * 不要再写字面量。
 */
export const OPEN_SOURCE_URL = 'https://github.com/hong1hello/zurPilot'
