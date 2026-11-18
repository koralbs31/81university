'use client'
import Link from 'next/link'
import React, { useEffect, useRef, useState, Suspense } from 'react'
import { getUriWithOrg } from '@services/config/config'
import { getCourseMetadata } from '@services/courses/courses'
import ActivityIndicators from '@components/Pages/Courses/ActivityIndicators'
import { useTrail } from '@/hooks/queries/useTrail'
import { useRouter } from 'next/navigation'
import GeneralWrapperStyled from '@components/Objects/StyledElements/Wrappers/GeneralWrapper'
import {
  getCourseThumbnailMediaDirectory,
} from '@services/media/media'
import { ArrowDown, ArrowRight, Backpack, Check, File, StickyNote, Video, Square, Image as ImageIcon, Layers, BookCopy, Lock, Globe, Package, Puzzle, Gem, Eye, Radio } from 'lucide-react'

import { MarkdownLogo } from '@phosphor-icons/react'
import { useOrg } from '@components/Contexts/OrgContext'

import CoursesActions from '@components/Objects/Courses/CourseActions/CoursesActions'

import CoursePaths from '@components/Objects/Courses/CoursePaths/CoursePaths'
import LinkifiedText from '@components/Objects/StyledElements/LinkifiedText/LinkifiedText'
import { Breadcrumbs } from '@components/Objects/Breadcrumbs/Breadcrumbs'
import { useLHSession } from '@components/Contexts/LHSessionContext'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { queryKeys } from '@/lib/query/keys'
import { getActivityWithAuthHeader } from '@services/courses/activities'
import { useTranslation } from 'react-i18next'
import CourseCommunitySection from '@components/Objects/Communities/CourseCommunitySection'
import CourseShare from '@components/Objects/Courses/CourseShare/CourseShare'
import { ShareCourseInMessagesButton } from '@components/Messaging/EntryButtons'
import CourseAccessError from '@components/Objects/Courses/CourseAccessError/CourseAccessError'
import { JsonLd } from '@components/SEO/JsonLd'
import { useLHAnalytics, AnalyticsEvent } from '@services/analytics'
import { useShortcut } from '@/lib/shortcuts/ShortcutsContext'
import { Keys } from '@components/Objects/Shortcuts/Kbd'
import DripBadge, { DripIcon, DripUntilBadge, useDripLabel } from '@components/Objects/Courses/DripBadge'
import ThumbnailImage from '@components/Objects/Thumbnails/ThumbnailImage'

const CourseClient = (props: any) => {
  const { t } = useTranslation()
  const lessonScrollRef = useRef<HTMLDivElement>(null)
  const [hasMoreChapters, setHasMoreChapters] = useState(false)
  const [learnings, setLearnings] = useState<any>([])
  const [expandedChapters, setExpandedChapters] = useState<{[key: string]: boolean}>({})
  const [activeThumbnailType, setActiveThumbnailType] = useState<'image' | 'video'>('image')
  const courseuuid = props.courseuuid
  const orgslug = props.orgslug
  const initialCourse = props.course
  const serverError = props.serverError
  const org = useOrg() as any
  const _router = useRouter()
  const session = useLHSession() as any;
  const access_token = session?.data?.tokens?.access_token;
  const queryClient = useQueryClient()
  // Which lessons are sold and not bought yet, and which are free previews.



  const { data: clientCourseData, error: courseError, isLoading: courseLoading } = useQuery({
    queryKey: queryKeys.courses.meta(courseuuid),
    queryFn: () => getCourseMetadata(courseuuid, {}, access_token, { slim: true }),
    enabled: !!courseuuid && !serverError,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const course = initialCourse || clientCourseData;

  const { track } = useLHAnalytics('learner')

  // Keyboard: `e` opens every chapter, or folds them all once they are open.
  const chapters: any[] = course?.chapters ?? []
  const dripLabel = useDripLabel()
  // First chapter the drip schedule still holds back, for the "next up" banner.
  const nextDrip = chapters.find((chapter: any) => chapter.drip_locked && chapter.drip_reason !== 'closed')
  const allChaptersExpanded = chapters.length > 0 && chapters.every(
    (chapter: any, idx: number) => expandedChapters[chapter.chapter_uuid] ?? idx === 0,
  )
  const setAllChapters = (open: boolean) =>
    setExpandedChapters(Object.fromEntries(chapters.map((chapter: any) => [chapter.chapter_uuid, open])))
  useShortcut(
    { id: 'course.chapters', keys: 'e', group: 'page',
      label: allChaptersExpanded ? t('shortcuts.collapse_chapters') : t('shortcuts.expand_chapters'),
      run: () => setAllChapters(!allChaptersExpanded) },
    chapters.length > 0,
  )

  // Track course view
  const courseId = course?.id
  const courseUuidForTracking = course?.course_uuid
  useEffect(() => {
    if (courseId && courseUuidForTracking) {
      track(AnalyticsEvent.CourseViewed, {
        course_uuid: courseUuidForTracking,
      })
    }
  }, [courseId, courseUuidForTracking, track])

  // Fetch trail data; shared cache with useTrail hook used elsewhere
  const { data: trailData } = useTrail(org?.id);

  // Must be before any early returns (React rules of hooks)
  useEffect(() => {
    if (!course) return

    getLearningTags(course)

    if (course?.chapters) {
      const totalActivities = course.chapters.reduce((sum: number, chapter: any) => sum + (chapter.activities?.length || 0), 0)
      const defaultExpanded: {[key: string]: boolean} = {}
      course.chapters.forEach((chapter: any, idx: number) => {
        defaultExpanded[chapter.chapter_uuid] = idx === 0 ? true : totalActivities <= 5
      })
      setExpandedChapters(defaultExpanded)
    }
  }, [course])

  useEffect(() => {
    const viewport = lessonScrollRef.current
    const content = viewport?.firstElementChild
    if (!viewport || !content) return
    const updateScrollHint = () => setHasMoreChapters(
      viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop > 2
    )
    const observer = new ResizeObserver(updateScrollHint)
    observer.observe(viewport)
    observer.observe(content)
    viewport.addEventListener('scroll', updateScrollHint, { passive: true })
    updateScrollHint()
    return () => {
      observer.disconnect()
      viewport.removeEventListener('scroll', updateScrollHint)
    }
  }, [course, org])

  // Show loading state if fetching course data client-side
  if (!initialCourse && !serverError && courseLoading) {
    return (
      <GeneralWrapperStyled>
        <div className="animate-pulse grid grid-cols-1 gap-6 lg:grid-cols-[300px_minmax(0,1fr)_300px] xl:grid-cols-[340px_minmax(0,1fr)_320px]">
          <div className="rounded-lg bg-gray-100 h-96" />
          <div className="space-y-4">
            <div className="h-9 bg-gray-200 rounded w-2/3" />
            <div className="h-4 bg-gray-100 rounded w-full" />
            <div className="h-4 bg-gray-100 rounded w-5/6" />
          </div>
          <div className="space-y-4">
            <div className="bg-gray-200 rounded-lg w-full aspect-video" />
            <div className="bg-gray-100 rounded-lg h-40" />
          </div>
        </div>
      </GeneralWrapperStyled>
    )
  }

  // Determine the active error (server-side or client-side)
  const activeError = serverError || courseError

  // Show error if course fetch failed
  if (!course && activeError) {
    return <CourseAccessError error={activeError} orgslug={orgslug} />
  }

  function getLearningTags(courseData: any) {
    if (!courseData?.learnings) {
      setLearnings([])
      return
    }

    try {
      // Try to parse as JSON (new format)
      const parsedLearnings = JSON.parse(courseData.learnings)
      if (Array.isArray(parsedLearnings)) {
        // New format: array of learning items with text and emoji
        setLearnings(parsedLearnings)
        return
      }
    } catch (_e) {
      // Not valid JSON, continue to legacy format handling
    }

    // Legacy format: comma-separated string (changed from pipe-separated)
    const learningItems = courseData.learnings.split(',').map((text: string) => ({
      id: crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(),
      text: text.trim(), // Trim whitespace that might be present after commas
      emoji: '📝' // Default emoji for legacy items
    }))

    setLearnings(learningItems)
  }

  const getActivityTypeLabel = (activityType: string, activitySubType?: string) => {
    if (activitySubType === 'SUBTYPE_DYNAMIC_MARKDOWN') return t('activities.markdown')
    if (activitySubType === 'SUBTYPE_DYNAMIC_EMBED') return t('activities.embed')
    switch (activityType) {
      case 'TYPE_VIDEO':
        return t('activities.video')
      case 'TYPE_DOCUMENT':
        return t('activities.document')
      case 'TYPE_DYNAMIC':
        return t('activities.page')
      case 'TYPE_ASSIGNMENT':
        return t('activities.assignment')
      case 'TYPE_SCORM':
        return t('activities.scorm')
      case 'TYPE_LIVE_SESSION':
        return t('activities.live_session')
      case 'TYPE_CUSTOM':
        return t('activities.custom')
      default:
        return t('activities.learning_material')
    }
  }

  const getActivityTypeIcon = (activityType: string, activitySubType?: string, size: number = 10) => {
    if (activitySubType === 'SUBTYPE_DYNAMIC_MARKDOWN') return <MarkdownLogo size={size} />
    if (activitySubType === 'SUBTYPE_DYNAMIC_EMBED') return <Globe size={size} />
    switch (activityType) {
      case 'TYPE_VIDEO':
        return <Video size={size} />
      case 'TYPE_DOCUMENT':
        return <File size={size} />
      case 'TYPE_DYNAMIC':
        return <StickyNote size={size} />
      case 'TYPE_ASSIGNMENT':
        return <Backpack size={size} />
      case 'TYPE_SCORM':
        return <Package size={size} />
      case 'TYPE_LIVE_SESSION':
        return <Radio size={size} />
      case 'TYPE_CUSTOM':
        return <Puzzle size={size} />
      default:
        return <Layers size={size} />
    }
  }

  const _getActivityTypeBadgeColor = (activityType: string) => {
    switch (activityType) {
      case 'TYPE_VIDEO':
        return 'bg-neutral-100 text-neutral-500'
      case 'TYPE_DOCUMENT':
        return 'bg-neutral-100 text-neutral-500'
      case 'TYPE_DYNAMIC':
        return 'bg-neutral-100 text-neutral-500'
      case 'TYPE_ASSIGNMENT':
        return 'bg-neutral-100 text-neutral-500'
      default:
        return 'bg-neutral-100 text-neutral-500'
    }
  }

  const isActivityDone = (activity: any) => {
    if (!course?.course_uuid || !trailData?.runs || !Array.isArray(trailData.runs)) {
      return false
    }
    const cleanCourseUuid = course.course_uuid.replace('course_', '')
    const run = trailData.runs.find((run: any) => {
      const cleanRunCourseUuid = run.course?.course_uuid?.replace('course_', '')
      return cleanRunCourseUuid === cleanCourseUuid
    })
    if (!run || !Array.isArray(run.steps)) return false
    return !!run.steps.find((step: any) => step.activity_id == activity.id)
  }

  const isActivityCurrent = (activity: any) => {
    if (!activity?.activity_uuid) return false
    const activity_uuid = activity.activity_uuid.replace('activity_', '')
    return props.current_activity === activity_uuid
  }

  const handleActivityMouseEnter = (activity: any) => {
    if (!activity?.activity_uuid) return
    const cleanUuid = activity.activity_uuid.replace('activity_', '')
    queryClient.prefetchQuery({
      queryKey: queryKeys.activity.detail(cleanUuid),
      queryFn: () => getActivityWithAuthHeader(cleanUuid, {}, access_token),
      staleTime: 60_000,
    })
  }

  // Generate JSON-LD structured data for SEO
  const generateJsonLd = () => {
    if (!course || !org) return null
    const seo = course.seo || {}

    // Check if JSON-LD is enabled (defaults to true if not set)
    if (seo.enable_jsonld === false) return null

    const jsonLd = {
      '@context': 'https://schema.org',
      '@type': 'Course',
      name: seo.title || course.name,
      description: seo.description || course.description || '',
      provider: {
        '@type': 'Organization',
        name: org.name,
        ...(org.description && { description: org.description }),
      },
      ...(course.thumbnail_image && {
        image: getCourseThumbnailMediaDirectory(
          org?.org_uuid,
          course?.course_uuid,
          course?.thumbnail_image
        ),
      }),
      ...(course.creation_date && { dateCreated: course.creation_date }),
      ...(course.update_date && { dateModified: course.update_date }),
    }

    return jsonLd
  }

  const jsonLd = generateJsonLd()

  return (
    <>
      {jsonLd && <JsonLd data={jsonLd} />}
      {!course || !org ? null : (
        <>
          <GeneralWrapperStyled>
            <div className="hidden sm:block pb-4">
              <Breadcrumbs items={[
                { label: t('courses.courses'), href: getUriWithOrg(orgslug, '/courses'), icon: <BookCopy size={14} /> },
                { label: course.name }
              ]} />
            </div>
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-[300px_minmax(0,1fr)_300px] lg:items-start xl:grid-cols-[340px_minmax(0,1fr)_320px]">
              <div className="min-w-0 order-1 lg:col-start-2 lg:row-start-1">
            <div className="pb-2 flex flex-col md:flex-row justify-between items-start md:items-center gap-3">
              <h1 className="text-3xl max-sm:text-[26px] max-sm:leading-tight font-bold" dir="auto">{course.name}</h1>
              <div className="flex items-center gap-2">
                <ShareCourseInMessagesButton orgslug={orgslug} courseUuid={`course_${courseuuid}`} />
                <CourseShare
                  courseUuid={course.course_uuid}
                  courseName={course.name}
                  courseUrl={getUriWithOrg(orgslug, `/course/${courseuuid}`)}
                />
              </div>
            </div>

                {trailData?.runs?.some((run: any) =>
                  run.course?.course_uuid?.replace('course_', '') === courseuuid.replace('course_', '')
                ) && (
                  <ActivityIndicators course_uuid={course.course_uuid} orgslug={orgslug} course={course} trailData={trailData} />
                )}
                <LinkifiedText text={course.about} className="py-5 whitespace-pre-line break-words w-full leading-relaxed tracking-normal text-pretty hyphens-auto" />
            {(() => {
              const displayLearnings = learnings.filter((l: any) => {
                const text = typeof l === 'string' ? l : l?.text
                return text && text.trim() !== '' && text !== 'null'
              })
              if (displayLearnings.length === 0) return null
              return (
                <div className="w-full">
                  <h2 className="py-5 text-xl md:text-2xl font-bold">{t('courses.what_you_will_learn')}</h2>
                  <div className="bg-white shadow-md shadow-gray-300/25 outline outline-1 outline-neutral-200/40 rounded-lg overflow-hidden px-5 py-5 space-y-2">
                    {displayLearnings.map((learning: any) => {
                      const learningText = typeof learning === 'string' ? learning : learning.text
                      const learningEmoji = typeof learning === 'string' ? null : learning.emoji
                      const learningId = typeof learning === 'string' ? learning : learning.id || learning.text
                      return (
                        <div
                          key={learningId}
                          className="flex space-x-2 items-center font-semibold text-gray-500"
                        >
                          <div className="px-2 py-2 rounded-full">
                            {learningEmoji ? (
                              <span>{learningEmoji}</span>
                            ) : (
                              <Check className="text-gray-400" size={15} />
                            )}
                          </div>
                          <p>{learningText}</p>
                          {learning.link && (
                            <a
                              href={learning.link}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-blue-500 hover:underline text-sm"
                            >
                              <span className="sr-only">Link to {learningText}</span>
                              <ArrowRight size={14} />
                            </a>
                          )}
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })()}

              </div>
              <aside aria-label={t('courses.course_lessons')} className="min-w-0 order-3 lg:col-start-1 lg:row-start-1 lg:sticky lg:top-24 lg:max-h-[calc(100dvh-7rem)] lg:flex lg:flex-col">
              <div className="shrink-0 pb-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="py-3 text-xl font-bold">{t('courses.course_lessons')}</h2>
                {chapters.length > 1 && (
                  <button
                    type="button"
                    onClick={() => setAllChapters(!allChaptersExpanded)}
                    className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-black/50 transition-colors hover:bg-black/5 hover:text-black/75"
                  >
                    {allChaptersExpanded ? t('shortcuts.collapse_chapters') : t('shortcuts.expand_chapters')}
                    <Keys keys="e" tone="muted" />
                  </button>
                )}
              </div>
                <p className="text-xs text-neutral-500">
                  {t('courses.chapter_count', { count: chapters.length })}
                  {' · '}
                  {t('courses.lesson_count', { count: chapters.reduce((sum: number, chapter: any) => sum + chapter.activities.length, 0) })}
                </p>
              </div>
              <div className="relative min-h-0 lg:flex-1">
                <div
                  ref={lessonScrollRef}
                  tabIndex={0}
                  role="region"
                  aria-label={t('courses.course_lessons')}
                  className="course-lessons-scroll rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-neutral-500 lg:max-h-[calc(100dvh-13rem)] lg:overflow-y-scroll lg:overscroll-contain lg:pe-2"
                >
                <div>
              {nextDrip && (
                <div
                  data-testid="drip-banner"
                  className="mb-3 flex items-center gap-3 rounded-lg border border-amber-200 bg-amber-50/70 px-4 py-3 text-sm text-amber-800"
                >
                  <div className="w-8 h-8 rounded-md bg-white border border-amber-200 text-amber-600 flex items-center justify-center shrink-0">
                    <DripIcon reason={nextDrip.drip_reason} size={16} />
                  </div>
                  <div className="min-w-0 grow">
                    <div className="font-semibold">
                      {t('course.drip.banner_title', 'New chapters are released over time')}
                    </div>
                    <div className="text-amber-700/90 truncate">
                      {t('course.drip.banner_next', {
                        defaultValue: 'Next up: {{name}}. {{when}}',
                        name: nextDrip.name,
                        when: [dripLabel(nextDrip).label, dripLabel(nextDrip).detail].filter(Boolean).join(' · '),
                      })}
                    </div>
                  </div>

                </div>
              )}
              <div className="bg-white shadow-md shadow-gray-300/25 outline outline-1 outline-neutral-200/40 rounded-lg overflow-hidden">
                {(course.chapters ?? []).map((chapter: any, idx: number) => {
                  const isExpanded = expandedChapters[chapter.chapter_uuid] ?? (idx === 0); // Default to expanded for first chapter
                  return (
                    <div key={chapter.chapter_uuid || `chapter-${chapter.name}`} className="">
                      <div
                        className="flex items-start py-4 px-4 outline outline-1 outline-neutral-200/40 font-bold bg-neutral-50 text-neutral-600 cursor-pointer hover:bg-neutral-100 transition-colors"
                        onClick={() => setExpandedChapters(prev => ({
                          ...prev,
                          [chapter.chapter_uuid]: !isExpanded
                        }))}
                      >
                        {/* Chevron on the far left, vertically centered with the title */}
                        <div className="flex flex-col justify-center me-3 pt-1">
                          <svg
                            className={`w-5 h-5 transition-transform ${isExpanded ? 'rotate-180' : ''}`}
                            fill="none"
                            stroke="currentColor"
                            viewBox="0 0 24 24"
                          >
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                          </svg>
                        </div>
                        {/* Title and badge column */}
                        <div className="flex flex-col items-start w-full">
                          <div className="flex items-center flex-wrap mb-1 w-full min-w-0">
                            {/* Numbered badge */}
                            <span className="flex items-center justify-center w-5 h-5 rounded-full bg-neutral-200 text-neutral-600 text-xs font-semibold me-2 border border-neutral-300 flex-shrink-0">
                              {idx + 1}
                            </span>
                            <h3 className="text-sm font-bold leading-tight min-w-0 break-words" style={{lineHeight: '1.2'}}>{chapter.name}</h3>
                            {chapter.drip_locked ? (
                              <DripBadge className="ms-2" drip_reason={chapter.drip_reason} drip_unlock_at={chapter.drip_unlock_at} drip_closes_at={chapter.drip_closes_at} />
                            ) : chapter.drip_closes_at && !chapter.is_locked ? (
                              <DripUntilBadge className="ms-2" closesAt={chapter.drip_closes_at} />
                            ) : chapter.is_locked && (
                              <span className="ms-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-rose-50 border border-rose-200 text-rose-600 text-[10px] font-semibold">
                                <Lock size={10} />
                                {t('course.locked', 'Locked')}
                              </span>
                            )}
                          </div>
                          <div className="flex items-center space-x-1 text-sm text-neutral-400 font-normal">
                            <Layers size={16} className="me-1" />
                            <span>{chapter.activities.length} {t('activities.activities')}</span>
                          </div>
                        </div>
                      </div>
                      <div className={`transition-all duration-200 ${isExpanded ? 'block' : 'hidden'}`}>
                        <div className="">
                          {chapter.activities.map((activity: any) => {
                            const locked = !!activity.is_locked
                            const dripped = locked && !!chapter.drip_locked
                            const RowInner = (
                              <div className="flex space-x-3 items-center">
                                <div className="flex items-center">
                                  {dripped ? (
                                    <div className="text-amber-500">
                                      <DripIcon reason={chapter.drip_reason} size={14} />
                                    </div>
                                  ) : locked ? (
                                    <div className="text-rose-400">
                                      <Lock size={14} className="stroke-[2]" />
                                    </div>
                                  ) : isActivityDone(activity) ? (
                                    <div className="relative cursor-pointer">
                                      <Square size={16} className="stroke-[2] text-teal-600" />
                                      <Check size={16} className="stroke-[2.5] text-teal-600 absolute top-0 start-0" />
                                    </div>
                                  ) : (
                                    <div className="text-neutral-300 cursor-pointer">
                                      <Square size={16} className="stroke-[2]" />
                                    </div>
                                  )}
                                </div>
                                <div className="flex flex-col grow min-w-0">
                                  <div className="flex items-center space-x-2 w-full">
                                    <p className={`text-sm font-semibold break-words transition-colors ${locked ? 'text-neutral-400' : 'text-neutral-600 group-hover:text-neutral-800'}`}>{activity.name}</p>
                                    {locked && !dripped && (
                                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-rose-50 border border-rose-200 text-rose-600 text-[10px] font-semibold">
                                        <Lock size={10} />
                                        {t('course.locked', 'Locked')}
                                      </span>
                                    )}


                                    {!locked && isActivityCurrent(activity) && (
                                      <div className="flex items-center space-x-1 text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full text-xs font-semibold animate-pulse">
                                        <span>{t('activities.current')}</span>
                                      </div>
                                    )}
                                  </div>
                                  <div className="flex items-center space-x-1.5 mt-0.5 text-neutral-400">
                                    {getActivityTypeIcon(activity.activity_type, activity.activity_sub_type, 10)}
                                    <span className="text-xs font-medium">{getActivityTypeLabel(activity.activity_type, activity.activity_sub_type)}</span>
                                  </div>
                                </div>
                                <div className={`transition-colors ${locked ? 'text-neutral-200' : 'text-neutral-300 group-hover:text-neutral-400 cursor-pointer'}`}>
                                  <ArrowRight size={14} />
                                </div>
                              </div>
                            )

                            if (locked) {
                              return (
                                <div
                                  key={activity.activity_uuid}
                                  className="block activity-container px-4 py-4 cursor-not-allowed select-none"
                                  title={dripped
                                    ? dripLabel(chapter).label
                                    : t('course.activity_locked_hint', 'Sign in or join the right user group to unlock this.')}
                                >
                                  {RowInner}
                                </div>
                              )
                            }

                            return (
                              <Link
                                key={activity.activity_uuid}
                                href={
                                  getUriWithOrg(orgslug, '') +
                                  `/course/${courseuuid}/activity/${activity.activity_uuid.replace('activity_', '')}`
                                }
                                rel="noopener noreferrer"
                                prefetch={false}
                                className="block group activity-container transition-all duration-200 px-4 py-4"
                                onMouseEnter={() => handleActivityMouseEnter(activity)}
                              >
                                {RowInner}
                              </Link>
                            )
                          })}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
                </div>
                </div>
                {hasMoreChapters && (
                  <div className="pointer-events-none absolute inset-x-0 bottom-0 hidden h-20 items-end justify-center bg-gradient-to-t from-white via-white/90 to-transparent pb-3 lg:flex">
                    <button
                      type="button"
                      onClick={() => lessonScrollRef.current?.scrollBy({ top: lessonScrollRef.current.clientHeight * 0.7, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' })}
                      className="pointer-events-auto flex items-center gap-1.5 rounded-full border border-neutral-200 bg-white px-3 py-1.5 text-xs font-medium text-neutral-600 shadow-sm hover:bg-neutral-50 focus-visible:outline-2 focus-visible:outline-neutral-500"
                    >
                      {t('courses.more_chapters')}
                      <ArrowDown size={14} aria-hidden="true" />
                    </button>
                  </div>
                )}
              </div>
              </aside>
              <aside aria-label={t('courses.start_course')} className="min-w-0 order-2 space-y-4 lg:col-start-3 lg:row-start-1 lg:sticky lg:top-24">
                {(() => {
                  const showVideo = course.thumbnail_type === 'video' || (course.thumbnail_type === 'both' && activeThumbnailType === 'video');
                  const showImage = course.thumbnail_type === 'image' || (course.thumbnail_type === 'both' && activeThumbnailType === 'image') || !course.thumbnail_type;

                  if (showVideo && course.thumbnail_video) {
                    return (
                      <div className="relative overflow-hidden rounded-lg ring-1 ring-black/10 w-full aspect-video">
                        {course.thumbnail_type === 'both' && (
                          <div className="absolute top-3 end-3 z-10">
                            <div className="bg-black/20 backdrop-blur-sm rounded-lg p-1 flex space-x-1">
                              <button
                                onClick={() => setActiveThumbnailType('image')}
                                className={`flex items-center px-2 py-1 rounded-md text-xs font-medium transition-colors ${
                                  activeThumbnailType === 'image'
                                    ? 'bg-white/90 text-gray-900 shadow-sm'
                                    : 'text-white/80 hover:text-white hover:bg-white/10'
                                }`}
                              >
                                <ImageIcon size={12} className="me-1" />
                                {t('courses.image')}
                              </button>
                              <button
                                onClick={() => setActiveThumbnailType('video')}
                                className={`flex items-center px-2 py-1 rounded-md text-xs font-medium transition-colors ${
                                  activeThumbnailType === 'video'
                                    ? 'bg-white/90 text-gray-900 shadow-sm'
                                    : 'text-white/80 hover:text-white hover:bg-white/10'
                                }`}
                              >
                                <Video size={12} className="me-1" />
                                {t('activities.video')}
                              </button>
                            </div>
                          </div>
                        )}
                        <div className="w-full h-full">
                          <video
                            src={getCourseThumbnailMediaDirectory(
                              org?.org_uuid,
                              course?.course_uuid,
                              course?.thumbnail_video
                            )}
                            className="w-full h-full bg-black sm:rounded-lg"
                            controls
                            autoPlay
                            muted
                            preload="metadata"
                            playsInline
                          />
                        </div>
                      </div>
                    );
                  } else if (showImage && course.thumbnail_image) {
                    return (
                      <div className="relative overflow-hidden rounded-lg ring-1 ring-black/10 w-full aspect-video bg-gray-50">
                        <ThumbnailImage
                          src={getCourseThumbnailMediaDirectory(org?.org_uuid, course?.course_uuid, course?.thumbnail_image)}
                          fetchPriority="high"
                          loading="eager"
                          className="absolute inset-0 w-full h-full"
                        />
                        {course.thumbnail_type === 'both' && (
                          <div className="absolute top-3 end-3 z-10">
                            <div className="bg-black/20 backdrop-blur-sm rounded-lg p-1 flex space-x-1">
                              <button
                                onClick={() => setActiveThumbnailType('image')}
                                className={`flex items-center px-2 py-1 rounded-md text-xs font-medium transition-colors ${
                                  activeThumbnailType === 'image'
                                    ? 'bg-white/90 text-gray-900 shadow-sm'
                                    : 'text-white/80 hover:text-white hover:bg-white/10'
                                }`}
                              >
                                <ImageIcon size={12} className="me-1" />
                                {t('courses.image')}
                              </button>
                              <button
                                onClick={() => setActiveThumbnailType('video')}
                                className={`flex items-center px-2 py-1 rounded-md text-xs font-medium transition-colors ${
                                  activeThumbnailType === 'video'
                                    ? 'bg-white/90 text-gray-900 shadow-sm'
                                    : 'text-white/80 hover:text-white hover:bg-white/10'
                                }`}
                              >
                                <Video size={12} className="me-1" />
                                {t('activities.video')}
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  } else {
                    return (
                      <div
                        className="relative rounded-lg ring-1 ring-black/10 w-full aspect-video bg-cover bg-center"
                        style={{
                          backgroundImage: `url('/empty_thumbnail.png')`,
                          backgroundSize: 'auto',
                        }}
                      ></div>
                    );
                  }
                })()}
                <CoursesActions courseuuid={courseuuid} orgslug={orgslug} course={course} trailData={trailData} />
                <CoursePaths courseUuid={course.course_uuid} orgslug={orgslug} />
              </aside>
            </div>

            {/* Community Section */}
            <Suspense fallback={<div className="animate-pulse h-48 bg-gray-100 rounded-lg mt-4" />}>
              <CourseCommunitySection courseUuid={course.course_uuid} orgslug={orgslug} />
            </Suspense>
          </GeneralWrapperStyled>

          {/* Room for the start / continue button docked on phones. */}
          <div className="md:hidden h-16" />
        </>
      )}
    </>
  )
}

export default CourseClient
