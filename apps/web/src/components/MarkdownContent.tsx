/**
 * 助手正文 Markdown 渲染：react-markdown 默认不渲染任意原始 HTML，
 * 也不使用 innerHTML——模型文本无法注入脚本。链接统一新窗口打开并收紧 rel；
 * 危险协议（javascript: 等）由 react-markdown 的默认 URL 清洗拦截。
 * 流式期间按累积文本重新解析，尾部未闭合语法暂时按普通文本展示。
 */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function MarkdownContent({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: props => <a {...props} target="_blank" rel="noopener noreferrer nofollow" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
