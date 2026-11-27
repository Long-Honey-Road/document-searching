import { useId } from 'react'
import Markdown from 'react-markdown'

export type AnswerMeta = {
  verified: true
  area_used?: string | null
  area_source?: 'memory' | 'question' | 'asked' | 'none'
  sources: readonly {
    n: number
    fragment_id: string
    title: string
    clause: string
    version: number | null
    kind: 'regulation' | 'practice'
  }[]
}

export function Answer({ text, meta }: { text: string; meta?: AnswerMeta }) {
  const anchor = useId()
  const sources = meta?.sources ?? []
  const citations = new Map(sources.map((source) => [source.fragment_id, source.n]))
  const renderMarkdown = (body: string) => (
    <Markdown
      components={{
        a: ({ children, href }) => href?.startsWith('#' + anchor + '-')
          ? <a href={href} className="font-medium text-primary underline underline-offset-4">{children}</a>
          : <span>{children}</span>,
        img: ({ alt }) => <span>{alt}</span>,
      }}
    >
      {body.replace(/\[f:([^\]\n]+)\]/g, (_, id: string) => {
        const number = citations.get(id)
        return number === undefined ? '' : '[' + number + '](#' + anchor + '-' + number + ')'
      })}
    </Markdown>
  )
  const [main, details] = text.split(/^\s*(?:#{1,6}\s+|▸\s*)?(?:\*\*)?Подробнее(?:\*\*)?[ \t]*:?[ \t]*$/im, 2)

  return (
    <article className="space-y-4 rounded-xl border bg-card p-4 text-sm leading-relaxed sm:p-5">
      {meta?.area_used && (
        <p className="font-medium text-muted-foreground">
          Область: {meta.area_used}
        </p>
      )}
      <div className="space-y-3 [&_ol]:list-decimal [&_ul]:list-disc [&_li]:ml-5">
        {renderMarkdown(main)}
      </div>
      {details?.trim() && (
        <details className="space-y-2">
          <summary className="cursor-pointer font-medium">Подробнее</summary>
          {renderMarkdown(details)}
        </details>
      )}
      {sources.length > 0 && (
        <section aria-label="Источники" className="border-t pt-3">
          <h3 className="font-medium">Источники</h3>
          <ol className="mt-2 flex flex-col gap-2">
            {sources.map((source) => (
              <li
                key={source.fragment_id}
                id={anchor + '-' + source.n}
                tabIndex={-1}
                className="scroll-mt-4 rounded-lg border bg-muted/30 px-3 py-2 target:ring-2 target:ring-ring"
              >
                <span className="font-medium">[{source.n}] {source.title}</span>
                <p className="text-xs text-muted-foreground">п. {source.clause}{source.version !== null && ` · ред. ${source.version}`}</p>
              </li>
            ))}
          </ol>
        </section>
      )}
    </article>
  )
}
