'use client';
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowBendUpLeft, BookOpen, Lock, MagnifyingGlass, PaperPlaneRight, PencilSimple, Smiley, X, } from '@phosphor-icons/react';
import { Popover, PopoverContent, PopoverTrigger } from '@components/ui/popover';
import EmojiPickerPopover from '@components/Messaging/ui/EmojiPicker';
import { CourseThumb, PersonAvatar } from '@components/Messaging/ui/Avatars';
import { useOrgCourses, OrgCourse } from '@components/Messaging/hooks';
import { ChatMessage, CourseCard, UserSummary, displayName } from '@services/messaging/messaging';
import { senderName } from '@components/Messaging/utils';
export type ComposerSend = {
    content: string;
    course?: CourseCard | null;
};
type Mention = {
    kind: "user";
    user: UserSummary;
    label: string;
};
const TYPING_EVERY_MS = 3000;
const TYPING_IDLE_MS = 4000;
export default function Composer({ orgslug, placeholder, disabledReason, members, replyTo, editing, initialCourse, onCancelReply, onCancelEdit, onSend, onSubmitEdit, onEditLast, onTyping, compact = false, }: {
    orgslug: string;
    placeholder: string;
    disabledReason?: string | null;
    members: UserSummary[];
    replyTo: ChatMessage | null;
    editing: ChatMessage | null;
    initialCourse?: CourseCard | null;
    onCancelReply: () => void;
    onCancelEdit: () => void;
    onSend: (payload: ComposerSend) => void;
    onSubmitEdit: (message: ChatMessage, content: string) => void;
    onEditLast: () => void;
    onTyping: (typing: boolean) => void;
    compact?: boolean;
}) {
    const { t } = useTranslation();
    const [text, setText] = useState('');
    const [course, setCourse] = useState<CourseCard | null>(initialCourse ?? null);
    const [mentionQuery, setMentionQuery] = useState<string | null>(null);
    const [mentionIndex, setMentionIndex] = useState(0);
    const [emojiOpen, setEmojiOpen] = useState(false);
    const [courseOpen, setCourseOpen] = useState(false);
    const [courseQuery, setCourseQuery] = useState('');
    const ref = useRef<HTMLTextAreaElement>(null);
    const typingState = useRef<{
        last: number;
        idle?: ReturnType<typeof setTimeout>;
        on: boolean;
    }>({ last: 0, on: false });
    const { data: courses = [] } = useOrgCourses(orgslug, courseOpen);
    useEffect(() => {
        if (initialCourse)
            setCourse(initialCourse);
    }, [initialCourse]);
    useEffect(() => {
        if (editing) {
            setText(editing.content);
            requestAnimationFrame(() => {
                ref.current?.focus();
                ref.current?.setSelectionRange(editing.content.length, editing.content.length);
            });
        }
    }, [editing]);
    useEffect(() => {
        if (replyTo)
            ref.current?.focus();
    }, [replyTo]);
    // Autosize.
    useLayoutEffect(() => {
        const el = ref.current;
        if (!el)
            return;
        el.style.height = '0px';
        el.style.height = Math.min(220, el.scrollHeight) + 'px';
    }, [text]);
    const stopTyping = () => {
        const s = typingState.current;
        if (s.idle)
            clearTimeout(s.idle);
        if (s.on) {
            s.on = false;
            s.last = 0;
            onTyping(false);
        }
    };
    const bumpTyping = () => {
        const s = typingState.current;
        const now = Date.now();
        if (!s.on || now - s.last > TYPING_EVERY_MS) {
            s.on = true;
            s.last = now;
            onTyping(true);
        }
        if (s.idle)
            clearTimeout(s.idle);
        s.idle = setTimeout(stopTyping, TYPING_IDLE_MS);
    };
    useEffect(() => () => stopTyping(), []); // eslint-disable-line react-hooks/exhaustive-deps
    const mentionOptions: Mention[] = useMemo(() => {
        if (mentionQuery === null)
            return [];
        const q = mentionQuery.toLowerCase();
        const u: Mention[] = members
            .filter((m) => displayName(m).toLowerCase().includes(q) || m.username.toLowerCase().includes(q))
            .map((user) => ({ kind: 'user', user, label: user.username }));
        return u.slice(0, 8);
    }, [mentionQuery, members]);
    const detectMention = (value: string, caret: number) => {
        const before = value.slice(0, caret);
        const m = before.match(/(?:^|\s)@([\w.-]{0,30})$/);
        setMentionQuery(m ? m[1] : null);
        setMentionIndex(0);
    };
    const applyMention = (opt: Mention) => {
        const el = ref.current;
        if (!el)
            return;
        const caret = el.selectionStart;
        const before = text.slice(0, caret).replace(/@([\w.-]{0,30})$/, `@${opt.label.replace(/\s+/g, '')} `);
        const next = before + text.slice(caret);
        setText(next);
        setMentionQuery(null);
        requestAnimationFrame(() => {
            el.focus();
            el.setSelectionRange(before.length, before.length);
        });
    };
    const submit = () => {
        const content = text.trim();
        if (editing) {
            if (content && content !== editing.content)
                onSubmitEdit(editing, content);
            else
                onCancelEdit();
            setText('');
            return;
        }
        if (!content && !course)
            return;
        onSend({ content, course });
        setText('');
        setCourse(null);
        stopTyping();
    };
    const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (mentionOptions.length) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setMentionIndex((i) => (i + 1) % mentionOptions.length);
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                setMentionIndex((i) => (i - 1 + mentionOptions.length) % mentionOptions.length);
                return;
            }
            if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                applyMention(mentionOptions[mentionIndex]);
                return;
            }
            if (e.key === 'Escape') {
                setMentionQuery(null);
                return;
            }
        }
        if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
            return;
        }
        if (e.key === 'ArrowUp' && !text && !editing) {
            e.preventDefault();
            onEditLast();
            return;
        }
        if (e.key === 'Escape') {
            if (editing) {
                setText('');
                onCancelEdit();
            }
            else if (replyTo)
                onCancelReply();
        }
    };
    const insertEmoji = (emoji: string) => {
        const el = ref.current;
        const caret = el?.selectionStart ?? text.length;
        const next = text.slice(0, caret) + emoji + text.slice(caret);
        setText(next);
        setEmojiOpen(false);
        requestAnimationFrame(() => {
            el?.focus();
            el?.setSelectionRange(caret + emoji.length, caret + emoji.length);
        });
    };
    const filteredCourses = courses.filter((c: OrgCourse) => c.name.toLowerCase().includes(courseQuery.toLowerCase()));
    if (disabledReason) {
        return (<div className={compact ? 'px-2 pb-2' : 'px-4 pb-4'}>
        <div className="flex items-center gap-2.5 rounded-lg border border-dashed border-gray-200 bg-gray-50/70 px-4 py-3 text-sm text-gray-500">
          <Lock size={15}/>
          {disabledReason}
        </div>
      </div>);
    }
    return (<div className={`relative ${compact ? 'px-2 pb-2' : 'px-3 md:px-4 pb-3 md:pb-4'}`}>
      {/* Mention popup */}
      {mentionOptions.length > 0 && (<div className="absolute bottom-full mb-1 start-4 z-20 w-72 rounded-lg bg-white nice-shadow p-1">
          {mentionOptions.map((o, i) => (<button key={o.user.user_uuid} onMouseDown={(e) => {
                    e.preventDefault();
                    applyMention(o);
                }} className={`w-full flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-start ${i === mentionIndex ? 'bg-gray-100' : 'hover:bg-gray-50'}`}>
              {<PersonAvatar user={o.user} size={24}/>}
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-medium truncate">{displayName(o.user)}</span>
                <span className="block text-[11px] text-gray-500 truncate">
                  {`@${o.user.username}`}
                </span>
              </span>
              {null}
            </button>))}
        </div>)}

      <div className="rounded-xl bg-white nice-shadow focus-within:ring-2 focus-within:ring-gray-200 transition-shadow">
        {(replyTo || editing) && (<div className="flex items-center gap-2 border-b border-gray-100 px-3.5 py-2 text-xs text-gray-500">
            {editing ? <PencilSimple size={13}/> : <ArrowBendUpLeft size={13}/>}
            <span className="min-w-0 flex-1 truncate">
              {editing ? (t('messaging.editing', { defaultValue: 'Editing message' })) : (<>
                  {t('messaging.replying_to', { defaultValue: 'Replying to' })}{' '}
                  <span className="font-semibold text-gray-700">{senderName(replyTo!)}</span>
                  {replyTo!.content ? `: ${replyTo!.content.slice(0, 80)}` : ''}
                </>)}
            </span>
            <button onClick={() => {
                if (editing) {
                    setText('');
                    onCancelEdit();
                }
                else
                    onCancelReply();
            }} className="p-0.5 rounded hover:bg-gray-100">
              <X size={13}/>
            </button>
          </div>)}

        {course && !editing && (<div className="px-3 pt-3">
            <div className="inline-flex items-center gap-2.5 rounded-lg border border-gray-200 bg-gray-50 py-1.5 ps-1.5 pe-2">
              <CourseThumb course={course} size={32}/>
              <span className="text-sm font-medium max-w-[220px] truncate">{course.name}</span>
              <button onClick={() => setCourse(null)} className="p-0.5 rounded text-gray-400 hover:text-gray-700">
                <X size={13}/>
              </button>
            </div>
          </div>)}

        <textarea ref={ref} value={text} rows={1} onChange={(e) => {
            setText(e.target.value);
            detectMention(e.target.value, e.target.selectionStart);
            if (e.target.value)
                bumpTyping();
            else
                stopTyping();
        }} onKeyDown={onKeyDown} onBlur={() => {
            stopTyping();
            setTimeout(() => setMentionQuery(null), 120);
        }} placeholder={placeholder} maxLength={4000} className="block w-full resize-none bg-transparent px-4 pt-3 pb-1 text-base sm:text-sm leading-relaxed outline-none placeholder:text-gray-400"/>

        <div className="flex items-center gap-0.5 px-2 pb-2">
          <EmojiPickerPopover onPick={insertEmoji} open={emojiOpen} onOpenChange={setEmojiOpen} align="start">
            <button className="p-2 rounded-lg text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors" title="Emoji">
              <Smiley size={18}/>
            </button>
          </EmojiPickerPopover>
          {!editing && (<Popover open={courseOpen} onOpenChange={setCourseOpen}>
              <PopoverTrigger asChild>
                <button className="p-2 rounded-lg text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors" title="Share a course">
                  <BookOpen size={18}/>
                </button>
              </PopoverTrigger>
              <PopoverContent side="top" align="start" className="w-80 p-1.5 rounded-lg">
                <div className="flex items-center gap-2 rounded-md border border-gray-200 px-2.5 py-1.5 mb-1">
                  <MagnifyingGlass size={13} className="text-gray-400"/>
                  <input autoFocus value={courseQuery} onChange={(e) => setCourseQuery(e.target.value)} placeholder={t('messaging.find_course', { defaultValue: 'Find a course to share' })} className="flex-1 bg-transparent text-base sm:text-sm outline-none"/>
                </div>
                <div className="max-h-64 overflow-y-auto">
                  {filteredCourses.length === 0 && <p className="text-xs text-gray-400 text-center py-6">No courses</p>}
                  {filteredCourses.map((c: OrgCourse) => (<button key={c.course_uuid} onClick={() => {
                    setCourse({
                        course_uuid: c.course_uuid,
                        name: c.name,
                        description: c.description || '',
                        thumbnail_image: c.thumbnail_image || '',
                    });
                    setCourseOpen(false);
                    ref.current?.focus();
                }} className="w-full flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-start hover:bg-gray-50">
                      <CourseThumb course={{ course_uuid: c.course_uuid, name: c.name, thumbnail_image: c.thumbnail_image || '' }} size={32}/>
                      <span className="text-sm truncate">{c.name}</span>
                    </button>))}
                </div>
              </PopoverContent>
            </Popover>)}
          {null}
          <span className="ms-auto hidden sm:block text-[11px] text-gray-400 me-2">
            {editing ? 'Enter to save · Esc to cancel' : 'Enter to send · Shift+Enter new line'}
          </span>
          <button onClick={submit} disabled={!text.trim() && !course} className="ms-auto sm:ms-0 h-9 w-9 sm:h-8 sm:w-8 rounded-lg bg-black text-white flex items-center justify-center disabled:bg-gray-100 disabled:text-gray-300 hover:bg-gray-800 transition-colors" aria-label="Send">
            <PaperPlaneRight size={15} weight="fill"/>
          </button>
        </div>
      </div>
    </div>);
}
