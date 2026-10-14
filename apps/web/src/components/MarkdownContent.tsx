import { XMarkdown } from '@ant-design/x-markdown';
import type { ComponentProps } from '@ant-design/x-markdown';

function SafeLink({ href, children }: ComponentProps) {
  const url = typeof href === 'string' && (/^(https?:|mailto:|#)/i.test(href) || href.startsWith('/')) ? href : undefined;
  return <a href={url} target="_blank" rel="noopener noreferrer nofollow">{children}</a>;
}

/** Escape model-authored HTML; DOMPurify also filters unsafe URLs and attributes. */
export function MarkdownContent({ text, streaming = false }: { text: string; streaming?: boolean }) {
  return <XMarkdown className="md" content={text} escapeRawHtml openLinksInNewTab
    components={{ a: SafeLink }} streaming={{ hasNextChunk: streaming, enableAnimation: false }} />;
}
