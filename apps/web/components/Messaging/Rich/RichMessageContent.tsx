'use client'
import React from 'react'
import ReactMarkdown from 'react-markdown'
import type { ChatMessage } from '@services/messaging/messaging'
import { BASE_REMARK_PLUGINS, MATH_HINT, loadMathPlugins, normalizeMathDelimiters, type MathPlugins } from '@/lib/messageMarkdown'
import CodeBlock from './CodeBlock'

export default function RichMessageContent({ message }: {
  message: ChatMessage
  orgslug: string
  onFollowUp?: (text: string) => void
  inverted?: boolean
}) {
  const text = React.useMemo(() => normalizeMathDelimiters(message.content), [message.content])
  const needsMath = MATH_HINT.test(text)
  const [math, setMath] = React.useState<MathPlugins | null>(null)
  React.useEffect(() => {
    if (!needsMath) return
    let cancelled = false
    loadMathPlugins().then(plugins => { if (!cancelled) setMath(plugins) }).catch(() => {})
    return () => { cancelled = true }
  }, [needsMath])
  return <div className="prose prose-sm max-w-none break-words">
    <ReactMarkdown remarkPlugins={math?.remark ?? BASE_REMARK_PLUGINS} rehypePlugins={math?.rehype ?? []} components={{
      pre({ children }) {
        const child = React.Children.toArray(children)[0]
        if (!React.isValidElement<{ children?: React.ReactNode; className?: string }>(child)) return <pre>{children}</pre>
        const language = /language-([^ ]+)/.exec(child.props.className ?? '')?.[1] ?? ''
        return <CodeBlock code={String(child.props.children ?? '').replace(/\n$/, '')} language={language} />
      },
    }}>{text}</ReactMarkdown>
  </div>
}
