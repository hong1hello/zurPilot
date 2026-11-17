/**
 * 两个第三方包的运行时代码完整，但发布包里缺 .d.ts：
 *   - highlight.js@11.12.0 声明了 types: ./types/index.d.ts，但包里没有这个文件
 *   - echarts 的 types/dist/renderers.d.ts 同样缺失（其余 types 都在）
 * 这里按项目实际用法补最小声明，避免 noImplicitAny 报错，同时不改动运行时。
 */

declare module 'highlight.js' {
  export interface HighlightResult {
    value: string
    language?: string
    relevance: number
  }

  export interface HLJSApi {
    /** 判断语言是否已注册；MarkdownView 用它决定走高亮还是纯文本。 */
    getLanguage: (languageName: string) => boolean
    /** 指定语言高亮；ignoreIllegals 允许在代码片段不完整时不抛错。 */
    highlight: (code: string, options: {language: string; ignoreIllegals?: boolean}) => HighlightResult
    /** 自动识别语言高亮。 */
    highlightAuto: (code: string, languageSubset?: string[]) => HighlightResult
    /** 注册自定义语言。 */
    registerLanguage: (name: string, language: unknown) => void
    listLanguages: () => string[]
  }

  const hljs: HLJSApi
  export default hljs
}

declare module 'echarts/renderers' {
  // echarts.use([...]) 接受扩展安装器；这里给 any，避免把 echarts 内部类型整条链搬进来。
  export const CanvasRenderer: any
  export const SVGRenderer: any
}
