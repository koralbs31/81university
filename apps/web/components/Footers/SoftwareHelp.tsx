'use client'

import { useState } from 'react'
import dynamic from 'next/dynamic'
import { Book, Browser, ChatCircleDots, Question } from '@phosphor-icons/react'
import { useTranslation } from 'react-i18next'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@components/ui/dropdown-menu'

const FeedbackModal = dynamic(() => import('@components/Objects/Modals/FeedbackModal').then((mod) => mod.FeedbackModal), { ssr: false })

/** Help access on login, mobile, editor and other screens without a header Help menu. */
export default function SoftwareHelp() {
  const { t } = useTranslation()
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [feedbackMounted, setFeedbackMounted] = useState(false)
  return (
    <div className="software-help-fallback fixed bottom-[calc(5rem+env(safe-area-inset-bottom))] end-4 z-40 lg:bottom-4">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button type="button" aria-label={t('common.help')} className="flex h-11 w-11 items-center justify-center rounded-full border border-neutral-200 bg-white text-neutral-700 shadow-sm hover:bg-neutral-100">
            <Question size={24} weight="fill" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-64">
          <DropdownMenuItem asChild>
            <a href="https://github.com/koralbs31/81university" className="flex items-center gap-2"><Browser size={16} weight="fill" /><span>{t('common.software_source')}</span></a>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <a href="/software-notices" className="flex items-center gap-2"><Book size={16} weight="fill" /><span>AGPL-3.0</span></a>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem className="flex items-center gap-2" onClick={() => { setFeedbackMounted(true); setFeedbackOpen(true) }}>
            <ChatCircleDots size={16} weight="fill" /><span>{t('common.help_menu.report_feedback')}</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {feedbackMounted && <FeedbackModal open={feedbackOpen} onOpenChange={setFeedbackOpen} />}
    </div>
  )
}
