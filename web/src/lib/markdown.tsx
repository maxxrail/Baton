// A deliberately small markdown renderer for agent replies: paragraphs, line breaks, bullet
// and numbered lists, **bold**, *italic*, `code` and [links](https://...).
//
// It builds React elements and never sets HTML, so a reply can't inject markup or script.
// Only http(s) links become anchors (a `javascript:` URL stays plain text), and they open in
// a new tab with rel="noopener noreferrer". A link whose text is a number, like the server's
// citation markers `[1](url)`, renders as a superscript citation.

import type { ReactNode } from "react";

const INLINE =
  /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*\n]+)\*\*|`([^`\n]+)`|(?<![\w*])\*([^*\s][^*\n]*?)\*(?![\w*])/g;

export function renderInline(text: string, keyPrefix = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    const start = m.index ?? 0;
    if (start > last) out.push(text.slice(last, start));
    const key = `${keyPrefix}-${n++}`;
    const [, linkText, href, bold, code, italic] = m;
    if (href !== undefined) {
      const isCitation = /^\d+$/.test(linkText);
      const anchor = (
        <a
          key={key}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className={isCitation ? "citation" : "underline"}
          aria-label={isCitation ? `Source ${linkText}` : undefined}
        >
          {isCitation ? `[${linkText}]` : linkText}
        </a>
      );
      out.push(isCitation ? <sup key={key}>{anchor}</sup> : anchor);
    } else if (bold !== undefined) {
      out.push(<strong key={key}>{bold}</strong>);
    } else if (code !== undefined) {
      out.push(<code key={key}>{code}</code>);
    } else if (italic !== undefined) {
      out.push(<em key={key}>{italic}</em>);
    }
    last = start + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const BULLET = /^\s*[-*•]\s+/;
const NUMBERED = /^\s*\d+[.)]\s+/;

function withBreaks(lines: string[], key: string): ReactNode[] {
  return lines.flatMap((line, i) => [
    ...(i > 0 ? [<br key={`${key}-br-${i}`} />] : []),
    ...renderInline(line, `${key}-${i}`),
  ]);
}

export function Markdown({ text }: { text: string }) {
  const blocks = text.replace(/\r\n/g, "\n").trim().split(/\n{2,}/);
  return (
    <>
      {blocks.map((block, b) => {
        const lines = block.split("\n");
        const key = `b${b}`;
        if (lines.every((l) => BULLET.test(l))) {
          return (
            <ul key={key} className="list-disc pl-5">
              {lines.map((l, i) => (
                <li key={i}>{renderInline(l.replace(BULLET, ""), `${key}-${i}`)}</li>
              ))}
            </ul>
          );
        }
        if (lines.every((l) => NUMBERED.test(l))) {
          return (
            <ol key={key} className="list-decimal pl-5">
              {lines.map((l, i) => (
                <li key={i}>{renderInline(l.replace(NUMBERED, ""), `${key}-${i}`)}</li>
              ))}
            </ol>
          );
        }
        // "Sources:" followed by a numbered list, as the server writes citations.
        if (lines.length > 1 && lines.slice(1).every((l) => NUMBERED.test(l))) {
          return (
            <div key={key} className="sources">
              <p className="text-xs font-semibold uppercase tracking-wide opacity-70">
                {renderInline(lines[0], `${key}-h`)}
              </p>
              <ol className="list-decimal pl-5 text-sm">
                {lines.slice(1).map((l, i) => (
                  <li key={i}>{renderInline(l.replace(NUMBERED, ""), `${key}-${i}`)}</li>
                ))}
              </ol>
            </div>
          );
        }
        return <p key={key}>{withBreaks(lines, key)}</p>;
      })}
    </>
  );
}
