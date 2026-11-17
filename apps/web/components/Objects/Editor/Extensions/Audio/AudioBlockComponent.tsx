import { NodeViewProps, NodeViewWrapper } from '@tiptap/react'
import React from 'react'
import { useTranslation } from 'react-i18next'
import { uploadNewAudioFile } from '@services/blocks/Audio/audio'
import { getAudioBlockStreamUrl } from '@services/media/media'
import { useOrg } from '@components/Contexts/OrgContext'
import { useCourse } from '@components/Contexts/CourseContext'
import { useEditorProvider } from '@components/Contexts/Editor/EditorContext'
import { useLHSession } from '@components/Contexts/LHSessionContext'
import InlineAudioPlayer from '@components/Objects/Media/InlineAudioPlayer'

export default function AudioBlockComponent({ node, updateAttributes, extension }: NodeViewProps) {
  const { t } = useTranslation()
  const org = useOrg() as any
  const course = useCourse() as any
  const editor = useEditorProvider() as any
  const session = useLHSession() as any
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState('')
  const token = session?.data?.tokens?.access_token
  const block = node.attrs.blockObject
  const activityUuid = extension.options.activity.activity_uuid
  const content = block?.content
  const src = content && org?.org_uuid && course?.courseStructure?.course_uuid
    ? getAudioBlockStreamUrl(org.org_uuid, course.courseStructure.course_uuid, content.activity_uuid || activityUuid, block.block_uuid || '', `${content.file_id}.${content.file_format}`)
    : null

  async function save(operation: () => Promise<any>) {
    setBusy(true)
    setError('')
    try {
      const result = await operation()
      updateAttributes({ blockObject: { ...result, source_type: 'upload' } })
    } catch (err) {
      setError(err instanceof Error ? err.message : t('editor.blocks.audio_block.upload_failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <NodeViewWrapper className="my-4 rounded-xl border p-4" contentEditable={false}>
      {src && <InlineAudioPlayer src={src} />}
      {editor?.isEditable && (
        <div className="space-y-3">
          <label className="block text-sm font-medium">
            {t('editor.blocks.audio_block.upload_tab', { defaultValue: 'Upload audio' })}
            <input type="file" accept=".mp3,.wav,.ogg,.m4a" disabled={busy} className="block mt-2" onChange={(event) => {
              const file = event.target.files?.[0]
              if (file && token) void save(() => uploadNewAudioFile(file, activityUuid, token))
            }} />
          </label>
          {block && <button type="button" disabled={busy} className="px-3 py-2" onClick={() => updateAttributes({ blockObject: null })}>{t('common.remove', { defaultValue: 'Remove' })}</button>}
        </div>
      )}
      {error && <p role="alert" className="text-red-600">{error}</p>}
    </NodeViewWrapper>
  )
}
